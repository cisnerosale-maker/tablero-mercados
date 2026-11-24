#!/usr/bin/env python3
"""
Valuación por sector (página sectores.html).

Para cada uno de los 11 sectores GICS del S&P 500 junta:
  - P/E del sector (ETF) y sus promedios de 3, 5 y 10 años ........... worldperatio.com (diario)
  - P/E forward, EV/EBITDA, P/Valor libros, CAPE y percentiles ....... Siblis Research (mensual)
  - P/E, P/Valor libros, P/Ventas y P/Flujo de caja del ETF ........... Yahoo Finance (diario)
  - Promedios de 3, 5 y 10 eneros de P/E forward, P/VL, EV/EBITDA, P/V . Damodaran (anual, damodaran.json)
  - P/FFO de los REIT ................................................ Nareit (carga de la tarea de los viernes)

Si una fuente no responde desde GitHub, se usa data/sectores/manual.json, que mantiene la tarea
programada de los viernes. Escribe data/sectores/actual.json y suma un registro diario a
data/sectores/historial.json (acumulativo: no se borra nada).
"""
from __future__ import annotations

import re
import time
import traceback

import pandas as pd

from riesgo_comun import ROOT, _get, asof, clean, load_json, now_ar, rnd, save_json, yahoo

DIR = ROOT / "data" / "sectores"
DAMODARAN = DIR / "damodaran.json"
MANUAL = DIR / "manual.json"
ACTUAL = DIR / "actual.json"
HIST = DIR / "historial.json"

# nombre GICS, ETF, ratio principal, descripción corta
SECTORES = [
    ("Communication Services", "XLC", "pe", "Alphabet, Meta, Netflix, telecomunicaciones, medios"),
    ("Consumer Discretionary", "XLY", "pe", "Amazon, Tesla, Home Depot, McDonald's, Nike"),
    ("Consumer Staples", "XLP", "pe", "Walmart, Costco, Procter & Gamble, Coca-Cola, PepsiCo"),
    ("Energy", "XLE", "ev_ebitda", "Exxon, Chevron, ConocoPhillips, servicios petroleros"),
    ("Financials", "XLF", "pb", "Berkshire, JPMorgan, Visa, Mastercard, bancos y aseguradoras"),
    ("Health Care", "XLV", "pe", "Eli Lilly, UnitedHealth, Johnson & Johnson, AbbVie"),
    ("Industrials", "XLI", "ev_ebitda", "GE Aerospace, Caterpillar, Union Pacific, defensa"),
    ("Information Technology", "XLK", "pe", "Nvidia, Apple, Microsoft, Broadcom, software"),
    ("Materials", "XLB", "ev_ebitda", "Linde, Sherwin-Williams, minería, químicos"),
    ("Real Estate", "XLRE", "p_ffo", "REIT: Prologis, American Tower, Equinix, Welltower"),
    ("Utilities", "XLU", "ev_ebitda", "NextEra, Southern, Duke: electricidad, gas y agua"),
]

RATIOS = {
    "pe": "P/E",
    "pe_fwd": "P/E forward",
    "pb": "P/Valor libros",
    "ev_ebitda": "EV/EBITDA",
    "ps": "P/Ventas",
    "p_ffo": "P/FFO",
}
# Ratios que no conviene usar en ciertos sectores (se muestran tachados como "no representativo").
NO_REPRESENTATIVO = {
    "Financials": {"ev_ebitda": "En bancos y aseguradoras la deuda es parte del negocio: EV/EBITDA no tiene sentido.",
                   "ps": "Los ingresos de un banco no son comparables con los de una empresa industrial."},
    "Real Estate": {"pe": "Las amortizaciones contables deprimen la ganancia de los REIT: se usa P/FFO."},
}

WPE_SLUGS = {
    "Communication Services": "sp-500-communication-services", "Consumer Discretionary": "sp-500-consumer-discretionary",
    "Consumer Staples": "sp-500-consumer-staples", "Energy": "sp-500-energy", "Financials": "sp-500-financials",
    "Health Care": "sp-500-health-care", "Industrials": "sp-500-industrials",
    "Information Technology": "sp-500-information-technology", "Materials": "sp-500-materials",
    "Real Estate": "sp-500-real-estate", "Utilities": "sp-500-utilities",
}


def texto_plano(html: str) -> str:
    t = re.sub(r"<script.*?</script>|<style.*?</style>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = re.sub(r"&nbsp;|&#160;", " ", t)
    return re.sub(r"\s+", " ", t)


# ----------------------------------------------------------------- fuentes
def worldperatio(errores: list) -> dict:
    """P/E del ETF de cada sector y sus promedios (μ) y desvíos (σ) de 1 a 20 años."""
    out = {}
    slugs = dict(WPE_SLUGS)
    try:
        hub = _get("https://worldperatio.com/sp-500-sectors/", timeout=40, intentos=1).text
        for href in set(re.findall(r'href="(?:https://worldperatio\.com)?/sector/([a-z0-9-]+)/?"', hub)):
            for s in slugs:
                if href.replace("sp-500-", "").replace("-", " ") == s.lower():
                    slugs[s] = href
    except Exception as e:
        errores.append(f"worldperatio (índice de sectores): {str(e)[:90]}")
        if "403" in str(e) or "refused" in str(e).lower() or "timed out" in str(e).lower():
            return out
    for s, slug in slugs.items():
        try:
            t = texto_plano(_get(f"https://worldperatio.com/sector/{slug}/", timeout=40, intentos=2).text)
            m = re.search(r"vs Current P/E\s*\(\s*([\d.]+)\s*\)", t) or re.search(r"P/E Ratio\s+([\d.]+)", t)
            if not m:
                raise ValueError("no se encontró el P/E actual")
            prom = {}
            for n, mu, sd in re.findall(r"Last\s*(\d+)\s*Y\s+([\d.]+)\s+([\d.]+)", t):
                prom[n] = {"mu": float(mu), "sd": float(sd)}
            fecha = re.search(r"(\d{1,2} (?:January|February|March|April|May|June|July|August|September|October|November|December) \d{4})", t)
            out[s] = {"pe": float(m.group(1)), "prom": prom,
                      "fecha": pd.Timestamp(fecha.group(1)).strftime("%Y-%m-%d") if fecha else now_ar().strftime("%Y-%m-%d")}
            time.sleep(1)
        except Exception as e:
            errores.append(f"worldperatio {s}: {str(e)[:90]}")
    return out


def siblis(errores: list) -> dict:
    """Tabla mensual de Siblis Research: P/E, P/E forward, CAPE, EV/EBITDA, P/VL, dividendo y percentiles."""
    out = {}
    try:
        t = texto_plano(_get("https://siblisresearch.com/data/sectors/", timeout=40, intentos=2).text)
        mes = re.search(r"Month-end ([A-Z][a-z]+\.? \d{4})", t)
        for s, *_ in SECTORES:
            m = re.search(re.escape(s) + r"\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)%\s+(\d+)%\s+(\d+)%", t)
            if m:
                g = [float(x) for x in m.groups()]
                out[s] = {"pe": g[0], "pe_fwd": g[1], "cape": g[2], "ev_ebitda": g[3], "pb": g[4], "div": g[5],
                          "pe_pct": g[6], "cape_pct": g[7], "mes": mes.group(1) if mes else None}
        if len(out) < 8:
            raise ValueError(f"solo se leyeron {len(out)} sectores")
    except Exception as e:
        errores.append(f"Siblis: {str(e)[:100]}")
        return {}
    return out


def yahoo_etf(errores: list) -> dict:
    """Ratios de las acciones de cada ETF según Yahoo Finance (datos de Morningstar)."""
    out = {}
    try:
        import yfinance as yf
    except Exception as e:
        errores.append(f"Yahoo: {e}")
        return out
    for s, etf, *_ in SECTORES:
        try:
            eh = yf.Ticker(etf).funds_data.equity_holdings
            vals = {}
            for idx, fila in eh.iterrows():
                v = clean(fila.iloc[0])
                k = str(idx).lower()
                if v is None or v <= 0:
                    continue
                # Yahoo (Morningstar) publica estos campos invertidos: ganancias/precio, libros/precio, etc.
                if "earnings" in k and "growth" not in k:
                    vals["pe"] = 1 / v
                elif "book" in k:
                    vals["pb"] = 1 / v
                elif "sales" in k:
                    vals["ps"] = 1 / v
                elif "cash" in k:
                    vals["pcf"] = 1 / v
            if vals:
                out[s] = {k: round(v, 2) for k, v in vals.items()}
            time.sleep(0.5)
        except Exception as e:
            errores.append(f"Yahoo {etf}: {str(e)[:80]}")
    return out


# ----------------------------------------------------------------- armado
def dif(a, b):
    a, b = clean(a), clean(b)
    return None if a is None or b is None or b == 0 else round((a / b - 1) * 100, 1)


ZONAS = [(-20, "Muy barato", 0), (-7, "Barato", 0), (7, "En línea", 1), (20, "Caro", 2), (1e9, "Muy caro", 3)]


def zona(d):
    if d is None:
        return {"zona": "Sin dato", "nivel": None}
    for lim, nom, niv in ZONAS:
        if d < lim:
            return {"zona": nom, "nivel": niv}
    return {"zona": "Muy caro", "nivel": 3}


def main():
    DIR.mkdir(parents=True, exist_ok=True)
    errores: list[str] = []
    manual = load_json(MANUAL, {})
    dam = load_json(DAMODARAN, {})
    if not dam:
        errores.append("Todavía no están los promedios de Damodaran (damodaran.json).")

    wpe = worldperatio(errores)
    sib = siblis(errores)
    yh = yahoo_etf(errores)

    # Respaldo manual (lo carga la tarea de los viernes) para lo que no respondió.
    fuentes_usadas = {"worldperatio": "en vivo" if wpe else None, "siblis": "en vivo" if sib else None,
                      "yahoo": "en vivo" if yh else None}
    for clave, dic in (("worldperatio", wpe), ("siblis", sib)):
        man = manual.get(clave, {})
        faltan = [s for s, *_ in SECTORES if s not in dic and s in man.get("sectores", {})]
        for s in faltan:
            dic[s] = man["sectores"][s]
        if faltan:
            fuentes_usadas[clave] = f"carga manual del {man.get('fecha', '?')}"
    nareit = manual.get("nareit", {})

    # Precio de los ETF: para llevar el último dato de Damodaran (enero) a hoy.
    precios = {}
    try:
        precios = yahoo([e for _, e, *_ in SECTORES], start=f"{now_ar().year - 1}-12-01", auto_adjust=False)
    except Exception as e:
        errores.append(f"Precios de los ETF: {e}")

    sectores = []
    for s, etf, principal, desc in SECTORES:
        w, sb, y, dm = wpe.get(s, {}), sib.get(s, {}), yh.get(s, {}), dam.get("sectores", {}).get(s, {})
        p = precios.get(etf)
        var_ene = None
        if p is not None and not p.empty and dam:
            a0 = asof(p, pd.Timestamp(f"{max(int(dm.get('pe', {}).get('ultimo_año', now_ar().year)), 2000) - 1}-12-31"))
            if a0:
                var_ene = p.iloc[-1] / a0[1] - 1
        filas = []
        for r, nombre in RATIOS.items():
            if r == "p_ffo" and s != "Real Estate":
                continue
            f = {"id": r, "nombre": nombre, "principal": r == principal,
                 "no_repr": NO_REPRESENTATIVO.get(s, {}).get(r)}
            if r == "pe":
                f.update({"actual": w.get("pe"), "fuente": "worldperatio (ETF " + etf + ")", "fecha": w.get("fecha"),
                          "p3": (w.get("prom", {}).get("3") or {}).get("mu"), "p5": (w.get("prom", {}).get("5") or {}).get("mu"),
                          "p10": (w.get("prom", {}).get("10") or {}).get("mu"), "prom_fuente": "worldperatio (diario)",
                          "sd10": (w.get("prom", {}).get("10") or {}).get("sd"),
                          "otros": [x for x in (["Siblis", sb.get("pe"), f"percentil {sb.get('pe_pct'):.0f}" if sb.get("pe_pct") is not None else ""] if sb.get("pe") else None,
                                                ["Yahoo", y.get("pe"), ""] if y.get("pe") else None) if x]})
                f["comparado"] = f["actual"]
            elif r == "p_ffo":
                f.update({"actual": nareit.get("p_ffo"), "fuente": "Nareit", "fecha": nareit.get("fecha"),
                          "p3": nareit.get("p3"), "p5": nareit.get("p5"), "p10": nareit.get("p10"),
                          "prom_fuente": nareit.get("prom_fuente", "Nareit"), "otros": []})
                f["comparado"] = f["actual"]
            else:
                pub, fuente, fecha = None, None, None
                if r in ("pe_fwd", "ev_ebitda", "pb") and sb.get(r):
                    pub, fuente, fecha = sb[r], "Siblis (S&P 500 " + s + ")", sb.get("mes")
                elif r in ("pb", "ps") and y.get(r):
                    pub, fuente, fecha = y[r], "Yahoo (ETF " + etf + ")", now_ar().strftime("%Y-%m-%d")
                # El P/E forward de Damodaran es un promedio simple por industria y no es comparable con el del
                # S&P 500 (da valores muy altos en salud o tecnología): se muestra el publicado, sin promedio.
                d = dm.get(r, {}) if r != "pe_fwd" else {}
                est = round(d["ultimo"] * (1 + var_ene), 2) if d.get("ultimo") and var_ene is not None and r != "ev_ebitda" else None
                if r == "ev_ebitda" and d.get("ultimo") and var_ene is not None:
                    est = round(d["ultimo"] * (1 + 0.85 * var_ene), 2)  # el EV se mueve algo menos que el precio (deuda)
                otros = []
                if r == "pb" and y.get("pb") and fuente and "Siblis" in fuente:
                    otros.append(["Yahoo", y["pb"], ""])
                f.update({"actual": pub, "fuente": fuente, "fecha": fecha,
                          "p3": d.get("prom", {}).get("3"), "p5": d.get("prom", {}).get("5"), "p10": d.get("prom", {}).get("10"),
                          "prom_fuente": (f"Damodaran (eneros {dam.get('años', ['?'])[0]}–{d.get('ultimo_año', '?')})" if d else
                                          ("Sin promedio comparable" if r == "pe_fwd" else None)),
                          "dam_ultimo": d.get("ultimo"), "dam_año": d.get("ultimo_año"), "dam_hoy": est,
                          "dam_serie": d.get("por_año"), "otros": otros})
                # Comparación con la misma fuente que el promedio: el último enero de Damodaran llevado a hoy por precio.
                f["comparado"] = est
            for n in ("3", "5", "10"):
                f["d" + n] = dif(f.get("comparado"), f.get("p" + n))
            f.update(zona(f["d10"] if f["d10"] is not None else f["d5"]))
            filas.append(f)
        # Puntaje del sector: promedio de las diferencias contra 10 años (5 si no hay), el principal pesa doble.
        num = den = 0.0
        for f in filas:
            dd = f["d10"] if f["d10"] is not None else f["d5"]
            if dd is None or f["no_repr"]:
                continue
            w_ = 2.0 if f["principal"] else 1.0
            num += w_ * max(-60, min(100, dd))
            den += w_
        prom_dif = round(num / den, 1) if den else None
        princ = next((f for f in filas if f["principal"]), None)
        if princ and princ.get("comparado") is None:
            # Sin dato del ratio principal (por ejemplo P/FFO sin carga): se usa P/E forward o P/VL.
            princ = next((f for f in filas if f["id"] in ("pe_fwd", "pb") and f.get("comparado") is not None), princ)
        sectores.append({"sector": s, "etf": etf, "desc": desc, "principal": principal, "ratios": filas,
                         "principal_fila": princ["id"] if princ else None,
                         "dif_prom": prom_dif, **zona(prom_dif),
                         "siblis": {k: sb.get(k) for k in ("cape", "cape_pct", "pe_pct", "div")} if sb else None,
                         "var_desde_enero": rnd(var_ene * 100, 1) if var_ene is not None else None})

    out = {"actualizado": now_ar().isoformat(timespec="minutes"), "errores": errores, "fuentes": fuentes_usadas,
           "damodaran": {"años": dam.get("años"), "actualizado": dam.get("actualizado"),
                         "sin_sector": dam.get("industrias_sin_sector"), "avisos": dam.get("avisos")} if dam else None,
           "siblis_mes": next((v.get("mes") for v in sib.values() if v.get("mes")), None),
           "sectores": sectores}
    save_json(ACTUAL, out)

    hist = load_json(HIST, {"_nota": "Un registro por día. Acumulativo: no se borra nada.", "dias": {}})
    hist.setdefault("dias", {})[now_ar().strftime("%Y-%m-%d")] = {
        x["sector"]: {"dif": x["dif_prom"], **{f["id"]: [f.get("actual"), f.get("comparado")] for f in x["ratios"]}}
        for x in sectores}
    save_json(HIST, hist)
    print("OK sectores ·", len(errores), "avisos")
    for e in errores:
        print(" -", e)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        traceback.print_exc()
        raise
