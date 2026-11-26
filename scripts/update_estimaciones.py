"""Precio vs EPS · Crecimiento: actualiza las estimaciones de EPS (consenso) con Yahoo.

- data/eps/estimaciones.json tiene, por empresa, el EPS ajustado real del último ejercicio
  y las estimaciones de los 3 ejercicios siguientes. La carga inicial es de TIKR.
- Cada semana Yahoo actualiza los ejercicios que cubre (el actual y el siguiente). Si el
  dato de Yahoo no es comparable con el de TIKR (otra moneda, por acción ordinaria vs ADR),
  no se toca y se avisa.
- Cuando cierra un ejercicio, su estimación pasa a "real" (EPS ajustado = suma de los 4
  trimestres que informa Yahoo) y el tercer año queda pendiente hasta la próxima carga de TIKR.
- Cada actualización guarda una foto en data/eps/estimaciones_historial.json (no se borra
  nunca): sirve para ver si los analistas suben o bajan las estimaciones.
- Para las empresas que reportan en pesos (BMA, GGAL, YPF) guarda el tipo de cambio oficial
  de cada cierre, para calcular los promedios de crecimiento en dólares, y el precio en pesos.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import time
import traceback

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIR = ROOT / "data" / "eps"
EST = DIR / "estimaciones.json"
HIST = DIR / "estimaciones_historial.json"
ACCIONES = DIR / "acciones.json"
TOL = 0.25  # diferencia máxima Yahoo vs dato cargado para considerarlos comparables


def hoy() -> dt.date:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)).date()


def num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def load(p, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def save(p, o):
    p.write_text(json.dumps(o, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def cerca(a: str, b: str, dias=25) -> bool:
    return abs((dt.date.fromisoformat(a[:10]) - dt.date.fromisoformat(b[:10])).days) <= dias


def foto(H, tk, fuente, valores):
    if not valores:
        return
    lista = H["fotos"].setdefault(tk, [])
    f = hoy().isoformat()
    lista[:] = [x for x in lista if not (x["fecha"] == f and x["fuente"] == fuente)]
    lista.append({"fecha": f, "fuente": fuente, "valores": valores})


def tc_oficial(avisos):
    """{fecha: venta} del dólar oficial mayorista/BNA (argentinadatos)."""
    try:
        r = requests.get("https://api.argentinadatos.com/v1/cotizaciones/dolares/oficial", timeout=40)
        r.raise_for_status()
        return sorted((x["fecha"][:10].replace("/", "-"), num(x["venta"])) for x in r.json() if num(x.get("venta")))
    except Exception as e:  # noqa: BLE001
        avisos.append(f"Dólar oficial histórico: {e}")
        return []


def al_cierre(serie, fecha):
    best = None
    for f, v in serie:
        if f <= fecha:
            best = v
        else:
            break
    return best


def main():
    import yfinance as yf

    E = load(EST, {"acciones": {}})
    H = load(HIST, {"_nota": "Fotos de las estimaciones de EPS (consenso) en cada actualización. No se borra nunca.", "fotos": {}})
    yahoo = {a["ticker"]: a.get("yahoo") or a["ticker"] for a in load(ACCIONES, {"acciones": []})["acciones"]}
    avisos = []
    d = hoy()
    tc = None

    for tk, e in E["acciones"].items():
        try:
            sym = yahoo.get(tk, tk)
            t = yf.Ticker(sym)
            info = t.info or {}
            est = t.earnings_estimate
            fy_fin = info.get("nextFiscalYearEnd")
            fy0 = dt.datetime.fromtimestamp(int(fy_fin), dt.timezone.utc).date() if fy_fin else None
            if fy0 is not None:
                while fy0 < d - dt.timedelta(days=60):
                    fy0 = fy0.replace(year=fy0.year + 1)
            yv = {}
            if est is not None and fy0 is not None:
                for k, off in (("0y", 0), ("+1y", 1)):
                    if k in est.index:
                        v, n = num(est.loc[k, "avg"]), num(est.loc[k, "numberOfAnalysts"])
                        if v is not None:
                            yv[fy0.replace(year=fy0.year + off).isoformat()] = (v, int(n) if n else None)

            # 1) Cierre de ejercicio: la primera estimación ya terminó y Yahoo pasó al siguiente
            ests = e.get("est", [])
            if ests and dt.date.fromisoformat(ests[0]["fin"]) < d - dt.timedelta(days=10) and fy0 and fy0 > dt.date.fromisoformat(ests[0]["fin"]) + dt.timedelta(days=200):
                cerrado = ests[0]
                real = None
                try:
                    eh = t.earnings_history
                    if eh is not None and len(eh):
                        ini = dt.date.fromisoformat(cerrado["fin"]) - dt.timedelta(days=370)
                        qs = [num(r["epsActual"]) for idx, r in eh.iterrows()
                              if ini < (idx.date() if hasattr(idx, "date") else dt.date.fromisoformat(str(idx)[:10])) <= dt.date.fromisoformat(cerrado["fin"]) + dt.timedelta(days=5)]
                        qs = [q for q in qs if q is not None]
                        if len(qs) == 4:
                            real = sum(qs)
                except Exception:  # noqa: BLE001
                    pass
                if real is not None and cerrado.get("eps") and abs(real / cerrado["eps"] - 1) <= 0.4:
                    e["real"] = {"fin": cerrado["fin"], "eps_ajustado": round(real, 4), "fuente": "Yahoo (suma de 4 trimestres)", "fecha": d.isoformat()}
                    e["est"] = ests[1:]
                    avisos.append(f"{tk}: cerró el ejercicio {cerrado['fin'][:7]}; falta el tercer año estimado hasta la próxima carga de TIKR")
                else:
                    e.setdefault("avisos_carga", [])
                    avisos.append(f"{tk}: cerró el ejercicio {cerrado['fin'][:7]} pero falta el EPS ajustado real comparable: actualizar desde TIKR")

            # 2) Actualizar con Yahoo las estimaciones comparables
            vals = {}
            for x in e.get("est", []):
                m = next((k for k in yv if cerca(k, x["fin"])), None)
                if not m:
                    continue
                v, n = yv[m]
                if x.get("eps") and abs(v / x["eps"] - 1) > TOL and x.get("fuente", "").startswith("TIKR") and not x.get("yahoo_ok"):
                    x["yahoo_distinto"] = round(v, 4)
                    continue
                x.update({"eps": round(v, 4), "analistas": n or x.get("analistas"), "fuente": "Yahoo", "fecha": d.isoformat(), "yahoo_ok": True})
                vals[x["fin"]] = round(v, 4)
            foto(H, tk, "Yahoo", vals)

            # 3) Empresas en pesos: tipo de cambio de cada cierre y precio local
            if e.get("moneda") == "ARS":
                if tc is None:
                    tc = tc_oficial(avisos)
                fechas = [x["fin"] for x in e.get("historia_ars", [])] + [x["fin"] for x in e.get("est", [])] + ([e["real"]["fin"]] if e.get("real") else [])
                e["tc_cierre"] = {f: al_cierre(tc, f) for f in fechas if al_cierre(tc, f)}
                e["tc_hoy"] = tc[-1][1] if tc else e.get("tc_hoy")
                if e.get("yahoo_local"):
                    h = yf.Ticker(e["yahoo_local"]).history(period="10d")["Close"].dropna()
                    if len(h):
                        e["precio_ars"] = round(float(h.iloc[-1]), 2)
                        e["precio_ars_fecha"] = h.index[-1].date().isoformat()
        except Exception as ex:  # noqa: BLE001
            avisos.append(f"{tk}: {ex}")
            traceback.print_exc()
        time.sleep(0.4)

    E["actualizado"] = dt.datetime.now(dt.timezone.utc).isoformat(timespec="minutes")
    E["avisos"] = avisos
    save(EST, E)
    save(HIST, H)
    print(f"OK {len(E['acciones'])} empresas; {len(avisos)} avisos")
    for a in avisos:
        print(" -", a)


if __name__ == "__main__":
    main()
