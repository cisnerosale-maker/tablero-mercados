"""Sección Precio vs EPS: completa precios de cierre y suma los EPS nuevos.

Reglas:
- data/eps/historia.json es acumulativo. Lo que ya está cargado NO se borra ni se pisa:
  este programa solo agrega períodos nuevos, precios que faltan y splits nuevos.
- La carga inicial de cada empresa se hizo desde TIKR. Los trimestres y ejercicios
  siguientes se toman de Yahoo Finance (EPS diluido), igual que los precios.
- Si Yahoo da un EPS distinto del que ya está cargado, no se toca: se avisa.
- data/eps/actual.json (precio de hoy y avisos) sí se reescribe en cada corrida.

Para sumar una acción: agregar una línea en data/eps/acciones.json.
Si no tiene historia cargada, se arranca con lo que da Yahoo (unos 4 años y 5 trimestres)
y queda el aviso para completar la historia desde TIKR.
"""
from __future__ import annotations

import copy
import datetime as dt
import json
import math
import pathlib
import traceback

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIR = ROOT / "data" / "eps"
ACCIONES = DIR / "acciones.json"
HISTORIA = DIR / "historia.json"
ACTUAL = DIR / "actual.json"
CARGA = DIR / "carga.xlsx"   # planilla opcional para cargar historia a mano (ver README)

DIF_MAX = 0.03          # diferencia tolerada entre Yahoo y lo cargado (3%)
DIAS_PRESENTACION = 50  # días después del cierre del trimestre para marcar "EPS pendiente"


def hoy_ar() -> dt.date:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=3)).date()


def d(s: str) -> dt.date:
    return dt.date.fromisoformat(s)


def num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def fila_eps(df):
    """Devuelve {fecha_iso: eps} de la fila 'Diluted EPS' de un estado de resultados de yfinance."""
    if df is None or getattr(df, "empty", True):
        return {}
    for nombre in ("Diluted EPS", "DilutedEPS"):
        if nombre in df.index:
            fila = df.loc[nombre]
            out = {}
            for col, val in fila.items():
                v = num(val)
                if v is not None:
                    out[col.date().isoformat() if hasattr(col, "date") else str(col)[:10]] = v
            return out
    return {}


def cerca(a: str, b: str, dias: int = 10) -> bool:
    return abs((d(a) - d(b)).days) <= dias


def distinto(a: float, b: float) -> bool:
    return abs(a - b) > max(0.02, DIF_MAX * abs(b))


def procesar(acc: dict, hist: dict, actual: dict, avisos: list, hoy: dt.date):
    import yfinance as yf

    t = acc["ticker"]
    sym = acc.get("yahoo") or t
    h = hist["acciones"].setdefault(t, {"anual": [], "trimestral": [], "splits": [], "precios": {}, "alta": hoy.isoformat()})
    h.setdefault("splits", [])
    h.setdefault("precios", {})
    nuevo = not h["anual"] and not h["trimestral"]
    tk = yf.Ticker(sym)
    hoy_s = hoy.isoformat()

    # Fecha desde la que un split no está incluido en lo cargado
    cargas = [e["cargado"] for e in h["anual"] + h["trimestral"]] + [p["cargado"] for p in h["precios"].values()]
    desde = min(cargas) if cargas else hoy_s

    # 1. Splits nuevos (se registran; la página ajusta los datos cargados antes del split)
    try:
        sp = tk.splits
        for fecha, ratio in (sp.items() if sp is not None else []):
            f = fecha.date().isoformat()
            r = num(ratio)
            if r and r != 1 and f > desde and not any(s["fecha"] == f for s in h["splits"]):
                h["splits"].append({"fecha": f, "factor": r, "detectado": hoy_s})
                avisos.append(f"{t}: split {r:g} a 1 el {f}. Los datos anteriores se ajustan en pantalla (no se modifican).")
    except Exception as e:  # noqa: BLE001
        avisos.append(f"{t}: no se pudieron leer los splits ({e}).")

    # Moneda: si Yahoo reporta el EPS en otra moneda que la cotización, no se cargan EPS automáticos
    moneda_ok = True
    try:
        info = tk.info or {}
        mc, mf = info.get("currency"), info.get("financialCurrency")
        if mc and mf and mc != mf:
            moneda_ok = False
            avisos.append(f"{t}: Yahoo informa el EPS en {mf} y la cotización en {mc}. Los EPS nuevos hay que cargarlos a mano.")
    except Exception:  # noqa: BLE001
        pass

    # 2. EPS nuevos desde Yahoo
    if moneda_ok:
        try:
            q_y = fila_eps(tk.quarterly_income_stmt)
            a_y = fila_eps(tk.income_stmt)
        except Exception as e:  # noqa: BLE001
            q_y, a_y = {}, {}
            avisos.append(f"{t}: Yahoo no devolvió estados de resultados ({e}).")

        # control cruzado con lo ya cargado
        for lista, fuente_y, nombre in ((h["trimestral"], q_y, "trimestre"), (h["anual"], a_y, "ejercicio")):
            for e in lista:
                for f, v in fuente_y.items():
                    if cerca(f, e["fin"]) and distinto(v, e["eps"]):
                        avisos.append(f"{t}: {nombre} {e['fin']} cargado {e['eps']:g} ({e['fuente']}), Yahoo da {v:g}. Se deja el cargado.")

        # trimestres nuevos (incluye derivar el 4.º trimestre del anual si Yahoo no lo trae)
        trims = h["trimestral"]
        ult = trims[-1]["fin"] if trims else "1900-01-01"
        candidatos = {f: (v, "Yahoo") for f, v in q_y.items() if d(f) > d(ult) + dt.timedelta(days=20)}
        for fa, va in a_y.items():
            if d(fa) > d(ult) + dt.timedelta(days=20) and not any(cerca(fa, f) for f in candidatos):
                previos = [e for e in trims if d(fa) - dt.timedelta(days=300) < d(e["fin"]) < d(fa)]
                previos += [{"fin": f, "eps": v} for f, (v, _) in candidatos.items() if d(fa) - dt.timedelta(days=300) < d(f) < d(fa)]
                if len(previos) == 3:
                    candidatos[fa] = (round(va - sum(p["eps"] for p in previos), 4), "Yahoo (4.º trimestre = anual − 3 trimestres)")
        for f in sorted(candidatos):
            v, fuente = candidatos[f]
            ult4 = [e for e in trims if d(e["fin"]) > d(f) - dt.timedelta(days=330)][-3:]
            ltm = None
            if len(ult4) == 3 and (d(f) - d(ult4[0]["fin"])).days < 300:
                ltm = round(v + sum(e["eps"] for e in ult4), 4)
            trims.append({"fin": f, "eps": v, "eps_ltm": ltm, "fuente": fuente, "cargado": hoy_s})
            if not nuevo:
                avisos.append(f"{t}: se sumó el trimestre cerrado el {f} (EPS {v:g}, fuente {fuente}).")

        # ejercicios nuevos
        anual = h["anual"]
        ulta = anual[-1]["fin"] if anual else "1900-01-01"
        for f in sorted(a_y):
            if d(f) > d(ulta) + dt.timedelta(days=200):
                anual.append({"fin": f, "eps": a_y[f], "fuente": "Yahoo", "cargado": hoy_s})
                ulta = f
                if not nuevo:
                    avisos.append(f"{t}: se sumó el ejercicio cerrado el {f} (EPS {a_y[f]:g}, Yahoo).")
        if nuevo:
            avisos.append(f"{t}: acción nueva, cargada solo con lo que da Yahoo. Falta completar la historia desde TIKR.")

    # 3. Precios de cierre que falten y precio de hoy
    fines = sorted({e["fin"] for e in h["anual"] + h["trimestral"]})
    precio_hoy = None
    try:
        inicio = (d(fines[0]) - dt.timedelta(days=12)).isoformat() if fines else (hoy - dt.timedelta(days=30)).isoformat()
        df = tk.history(start=inicio, auto_adjust=False, actions=False)
        serie = [(i.date().isoformat(), num(c)) for i, c in zip(df.index, df["Close"])]
        serie = [(f, c) for f, c in serie if c is not None]
        for fin in fines:
            if fin in h["precios"]:
                continue
            previos = [(f, c) for f, c in serie if f <= fin]
            if previos and (d(fin) - d(previos[-1][0])).days <= 7:
                f, c = previos[-1]
                h["precios"][fin] = {"cierre": round(c, 4), "fecha": f, "cargado": hoy_s}
        if serie:
            precio_hoy = {"precio": round(serie[-1][1], 4), "fecha": serie[-1][0]}
    except Exception as e:  # noqa: BLE001
        avisos.append(f"{t}: Yahoo no devolvió precios ({e}).")
    faltan = [f for f in fines if f not in h["precios"]]
    if faltan and precio_hoy:
        avisos.append(f"{t}: sin precio de cierre para {', '.join(faltan)}.")

    # 4. Trimestre pendiente
    if h["trimestral"]:
        ultimo = d(h["trimestral"][-1]["fin"])
        if (hoy - ultimo).days > 91 + DIAS_PRESENTACION:
            avisos.append(f"{t}: EPS pendiente. El último trimestre cargado cerró el {ultimo.isoformat()} y ya debería estar el siguiente.")

    actual["acciones"][t] = precio_hoy or (actual.get("anterior", {}).get(t))


def importar_planilla(acciones: list, hist: dict, avisos: list, hoy: dt.date) -> bool:
    """Suma a la historia los períodos de data/eps/carga.xlsx que todavía no estén cargados.

    Columnas: Ticker | Nombre | Tipo (Anual/Trimestral) | Fecha de cierre | EPS diluido | EPS LTM.
    Lo que ya está cargado no se toca; si la planilla trae otro valor, solo se avisa.
    La planilla se puede dejar en el repositorio: volver a leerla no duplica nada.
    """
    if not CARGA.exists():
        return False
    import pandas as pd

    df = pd.read_excel(CARGA, sheet_name=0)
    df.columns = [str(c).strip().lower() for c in df.columns]
    col = lambda *ns: next((c for c in df.columns if any(c.startswith(n) for n in ns)), None)  # noqa: E731
    c_t, c_n, c_tipo, c_f, c_e, c_l = col("ticker"), col("nombre"), col("tipo"), col("fecha"), col("eps diluido", "eps"), col("eps ltm", "ltm")
    if not all([c_t, c_tipo, c_f, c_e]):
        avisos.append("carga.xlsx: faltan columnas (Ticker, Tipo, Fecha de cierre, EPS diluido).")
        return False
    lista_cambio = False
    sumados = {}
    for _, r in df.iterrows():
        t = str(r[c_t]).strip().upper()
        if not t or t == "NAN":
            continue
        eps = num(r[c_e])
        try:
            fin = pd.Timestamp(r[c_f]).date().isoformat()
        except Exception:  # noqa: BLE001
            fin = None
        if eps is None or fin is None:
            continue
        if not any(a["ticker"] == t for a in acciones):
            nombre = str(r[c_n]).strip() if c_n and str(r[c_n]) != "nan" else t
            acciones.append({"ticker": t, "nombre": nombre, "yahoo": t})
            lista_cambio = True
        h = hist["acciones"].setdefault(t, {"anual": [], "trimestral": [], "splits": [], "precios": {}, "alta": hoy.isoformat()})
        tipo = "trimestral" if str(r[c_tipo]).strip().lower().startswith("t") else "anual"
        existente = next((e for e in h[tipo] if cerca(e["fin"], fin, 5)), None)
        if existente:
            if distinto(eps, existente["eps"]):
                avisos.append(f"{t}: carga.xlsx trae {eps:g} para el {tipo} {fin}, cargado {existente['eps']:g}. Se deja el cargado.")
            continue
        e = {"fin": fin, "eps": eps, "fuente": "Planilla", "cargado": hoy.isoformat()}
        if tipo == "trimestral":
            e["eps_ltm"] = num(r[c_l]) if c_l else None
        h[tipo].append(e)
        h[tipo].sort(key=lambda x: x["fin"])
        sumados[t] = sumados.get(t, 0) + 1
    for t, n in sumados.items():
        avisos.append(f"{t}: se sumaron {n} períodos desde carga.xlsx.")
    return lista_cambio


def sin_cambios_en_lo_cargado(viejo: dict, nuevo: dict) -> list:
    """Verifica que no se haya modificado ni borrado nada de lo que ya estaba."""
    errores = []
    for t, hv in viejo.get("acciones", {}).items():
        hn = nuevo["acciones"].get(t)
        if hn is None:
            errores.append(f"{t}: desapareció")
            continue
        for k in ("anual", "trimestral", "splits"):
            nuevos = hn.get(k, [])
            if any(e not in nuevos for e in hv.get(k, [])):
                errores.append(f"{t}: cambió {k}")
        for f, p in hv.get("precios", {}).items():
            if hn.get("precios", {}).get(f) != p:
                errores.append(f"{t}: cambió el precio {f}")
    return errores


def main():
    hoy = hoy_ar()
    lista = json.loads(ACCIONES.read_text(encoding="utf-8"))
    acciones = lista["acciones"]
    hist = json.loads(HISTORIA.read_text(encoding="utf-8")) if HISTORIA.exists() else {"acciones": {}}
    original = copy.deepcopy(hist)
    anterior = json.loads(ACTUAL.read_text(encoding="utf-8")) if ACTUAL.exists() else {}
    actual = {"actualizado": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
              "acciones": {}, "anterior": anterior.get("acciones", {}), "avisos": []}
    try:
        if importar_planilla(acciones, hist, actual["avisos"], hoy):
            ACCIONES.write_text(json.dumps(lista, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    except Exception as e:  # noqa: BLE001
        actual["avisos"].append(f"carga.xlsx: no se pudo leer ({e}).")
    for acc in acciones:
        try:
            procesar(acc, hist, actual, actual["avisos"], hoy)
        except Exception as e:  # noqa: BLE001
            actual["avisos"].append(f"{acc.get('ticker')}: error ({e}).")
            traceback.print_exc()
    actual.pop("anterior", None)

    errores = sin_cambios_en_lo_cargado(original, hist)
    if errores:
        # Nunca se graba una historia que pise o borre datos.
        actual["avisos"].append("No se guardó la historia porque se iban a modificar datos cargados: " + "; ".join(errores))
    else:
        HISTORIA.write_text(json.dumps(hist, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    ACTUAL.write_text(json.dumps(actual, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    for a in actual["avisos"]:
        print("AVISO:", a)


if __name__ == "__main__":
    main()
