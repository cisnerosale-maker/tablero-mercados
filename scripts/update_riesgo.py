#!/usr/bin/env python3
"""
Monitor de riesgo de mercado (página riesgo.html).

Calcula dos puntajes de 0 a 100 para EE.UU. y para Argentina:
  - VALUACIÓN: ¿qué tan caro está el mercado?
  - SEÑALES:   ¿se está deteriorando?
y los cruza en una matriz que sugiere una postura (y un rango de liquidez en EE.UU.).

Escribe:
  data/riesgo/actual.json     lo que lee la página (se pisa en cada corrida)
  data/riesgo/historial.json  un registro por día con los puntajes y valores (acumulativo: nunca se borra)
  data/riesgo/sp500.json      lista de empresas del S&P 500 (respaldo si Wikipedia no responde)
Lee:
  data/riesgo/factset.json    P/E forward semanal de FactSet (lo carga la tarea programada de los viernes)
  data/historial.json         historia del Merval MSCI y del riesgo país que ya guarda el tablero
"""
from __future__ import annotations

import datetime as dt
import traceback

import pandas as pd

from riesgo_comun import (DIR, HEADERS, MATRIZ_AR, MATRIZ_EEUU, ROOT, _get, a_serie, argdatos, asof,
                          bcra_itcrm, bcra_reservas, cape_multpl, clean, fred, hasta, interp,
                          lectura_matriz, load_json, muestra, now_ar, percentil, puntaje, rnd,
                          save_json, yahoo, zona)

ACTUAL = DIR / "actual.json"
HIST = DIR / "historial.json"
FACTSET = DIR / "factset.json"
SP500 = DIR / "sp500.json"
HIST_TABLERO = ROOT / "data" / "historial.json"

MERVAL_EMPALME = ("2026-10-16", 100000.0)  # mismo empalme que usa el tablero (MSCI Merval base 100.000)
FECHA_REFERENCIA = ("2026-09-18", "18/09/2026 (tu fecha de referencia)")

# ----------------------------------------------------------------- pesos de cada puntaje
PESOS = {
    "us_val": {"pe_fwd": 30, "erp": 30, "cape": 20, "concentracion": 20},
    "us_sen": {"amplitud": 25, "tendencia": 20, "credito": 20, "volatilidad": 15, "revisiones": 20},
    "ar_val": {"merval_usd": 65, "rp_nivel": 35},
    "ar_sen": {"rp_cambio": 25, "merval_tend": 25, "reservas": 20, "tcr": 15, "brecha": 15},
}

# ----------------------------------------------------------------- anclas (valor -> puntaje)
A = {
    "pe_fwd": [(13, 0), (16.5, 33), (19.5, 67), (22, 85), (25, 100)],
    "cape": [(15, 0), (22, 33), (30, 67), (36, 85), (44, 100)],
    "erp": [(1.5, 100), (2.5, 85), (3.5, 67), (4.5, 33), (6, 0)],
    "conc_gap": [(-5, 0), (0, 25), (5, 50), (10, 75), (15, 95)],
    "amplitud": [(20, 100), (35, 85), (45, 67), (60, 33), (75, 0)],
    "dd": [(-25, 100), (-20, 90), (-10, 60), (-5, 30), (0, 0)],
    "hy_nivel": [(250, 15), (300, 20), (400, 40), (500, 70), (600, 85), (800, 100)],
    "hy_cambio": [(0, 0), (50, 35), (100, 70), (200, 100)],
    "baa_nivel": [(1.6, 15), (2.0, 30), (2.5, 50), (3.0, 70), (4.0, 90), (5.0, 100)],
    "baa_cambio": [(0, 0), (0.2, 35), (0.4, 70), (0.8, 100)],
    "vix": [(12, 5), (16, 20), (20, 40), (25, 62), (30, 78), (40, 95)],
    "rev4": [(-3, 100), (-1, 70), (0, 45), (0.5, 25), (2, 0)],
    "rp_nivel": [(400, 90), (600, 70), (900, 45), (1500, 20), (2500, 0)],
    "rp_cambio": [(-200, 0), (0, 30), (150, 60), (300, 85), (500, 100)],
    "reservas": [(-6000, 100), (-3000, 75), (0, 45), (3000, 20), (6000, 0)],
    "tcr_pct": [(5, 95), (15, 80), (35, 55), (60, 30), (85, 10)],
    "brecha": [(0, 5), (10, 30), (25, 60), (50, 85), (100, 100)],
}

# ----------------------------------------------------------------- textos de cada indicador
META = {
    "pe_fwd": {"eje": "val", "nombre": "P/E forward del S&P 500", "unidad": "veces", "dec": 1,
               "que": "Precio del índice sobre las ganancias esperadas para los próximos 12 meses.",
               "umbrales": "Barato < 16,5 · Justo 16,5–19,5 · Caro 19,5–22 · Muy caro > 22",
               "fuente": "FactSet Earnings Insight (semanal) ajustado por el precio diario",
               "url": "https://insight.factset.com/topic/earnings"},
    "erp": {"eje": "val", "nombre": "Prima de riesgo de las acciones", "unidad": "% anual", "dec": 2,
            "que": "Rendimiento por ganancias (1 / P/E forward) menos la tasa real del bono a 10 años (TIPS). Cuanto más baja, menos te pagan por el riesgo de tener acciones.",
            "umbrales": "Barato > 4,5% · Justo 3,5–4,5% · Caro 2,5–3,5% · Muy caro < 2,5%",
            "fuente": "Cálculo propio: FactSet + FRED (DFII10)",
            "url": "https://fred.stlouisfed.org/series/DFII10"},
    "cape": {"eje": "val", "nombre": "CAPE (Shiller P/E)", "unidad": "veces", "dec": 1,
             "que": "Precio sobre el promedio de ganancias reales de 10 años. Anticipa retornos de largo plazo, no el momento de una caída.",
             "umbrales": "Barato < 22 · Justo 22–30 · Caro 30–36 · Muy caro > 36",
             "fuente": "multpl.com (datos de Robert Shiller), mensual",
             "url": "https://www.multpl.com/shiller-pe/table/by-month"},
    "concentracion": {"eje": "val", "nombre": "Concentración del índice", "unidad": "pp en 12 meses", "dec": 1,
                      "que": "Cuánto le ganó el S&P 500 (ponderado por tamaño, SPY) al S&P 500 Equal Weight (RSP) en 12 meses, más el nivel histórico de esa relación. Alto = pocas empresas grandes explican la suba.",
                      "umbrales": "Puntaje: mitad por la diferencia de 12 meses (0 pp = 25, +10 pp = 75) y mitad por el percentil de SPY/RSP en 10 años",
                      "fuente": "Yahoo Finance (SPY y RSP, con dividendos)",
                      "url": "https://finance.yahoo.com/quote/RSP/"},
    "amplitud": {"eje": "sen", "nombre": "Amplitud: acciones sobre su media de 200 días", "unidad": "% del S&P 500", "dec": 1,
                 "que": "Porcentaje de empresas del S&P 500 que cotizan por encima de su media móvil de 200 ruedas. Si el índice sube y este número baja, la suba la sostienen pocas.",
                 "umbrales": "Sano > 60% · Atención 45–60% · Deterioro 35–45% · Fuerte < 35%",
                 "fuente": "Cálculo propio con las empresas actuales del índice (Yahoo Finance)",
                 "url": "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"},
    "tendencia": {"eje": "sen", "nombre": "Tendencia del S&P 500", "unidad": "% desde el máximo", "dec": 1,
                  "que": "Posición del índice respecto de sus medias de 50 y 200 ruedas y caída desde el máximo de 52 semanas. Toma la peor de las dos lecturas.",
                  "umbrales": "Precio > MM50 > MM200: sano · bajo la MM50: atención · bajo la MM200: deterioro · MM50 < MM200: fuerte",
                  "fuente": "Yahoo Finance (^GSPC)", "url": "https://finance.yahoo.com/quote/%5EGSPC/"},
    "credito": {"eje": "sen", "nombre": "Spread de bonos High Yield", "unidad": "puntos básicos", "dec": 0,
                "que": "Tasa extra que piden por prestarle a empresas de peor calificación. Suele subir antes que las caídas fuertes de las acciones. Se mira el nivel y cuánto subió en 3 meses.",
                "umbrales": "< 300 pb: complacencia · 300–400 normal · 400–500 deterioro · > 500 estrés · suba > 100 pb en 3 meses: alerta",
                "fuente": "FRED (ICE BofA US High Yield OAS, BAMLH0A0HYM2)",
                "url": "https://fred.stlouisfed.org/series/BAMLH0A0HYM2"},
    "volatilidad": {"eje": "sen", "nombre": "VIX y estructura de plazos", "unidad": "puntos", "dec": 2,
                    "que": "Volatilidad esperada a 30 días. Si el VIX supera al de 3 meses (VIX3M), el mercado está en modo pánico. Un VIX muy alto con valuación barata suele ser zona de compra.",
                    "umbrales": "< 12 complacencia · 12–20 normal · 20–30 preocupación · > 30 estrés · VIX > VIX3M suma alerta",
                    "fuente": "Yahoo Finance (^VIX, ^VIX3M)", "url": "https://finance.yahoo.com/quote/%5EVIX/"},
    "revisiones": {"eje": "sen", "nombre": "Revisiones del EPS forward", "unidad": "% en 4 semanas", "dec": 2,
                   "que": "Variación de las ganancias esperadas a 12 meses del S&P 500. Si los analistas recortan estimaciones, el P/E 'barato' puede ser una trampa.",
                   "umbrales": "Sube > 0,5%: sano · 0 a 0,5%: neutro · cae hasta 1%: atención · cae > 1%: deterioro",
                   "fuente": "FactSet Earnings Insight (semanal)", "url": "https://insight.factset.com/topic/earnings"},
    # Argentina
    "merval_usd": {"eje": "val", "nombre": "Merval en dólares (CCL)", "unidad": "puntos US$", "dec": 1,
                   "que": "Merval (MSCI, base 100.000) dividido por el dólar contado con liquidación. El puntaje es su percentil desde 2014: 90 = está más caro que el 90% del tiempo.",
                   "umbrales": "Puntaje = percentil histórico · Barato < 33 · Justo 33–67 · Caro > 67",
                   "fuente": "Yahoo + Rava (Merval) y argentinadatos.com (CCL)", "url": "https://www.rava.com/perfil/MERVAL%20USD"},
    "rp_nivel": {"eje": "val", "nombre": "Riesgo país (nivel)", "unidad": "puntos básicos", "dec": 0,
                 "que": "Con riesgo país bajo, bonos y acciones ya descuentan buenas noticias (están caros); alto, hay más margen.",
                 "umbrales": "< 600: caro · 600–900: justo · > 1.200: barato",
                 "fuente": "EMBI JP Morgan vía argentinadatos.com y Rava", "url": "https://www.rava.com/cotizaciones/historico/riesgo-pais/hoy/"},
    "rp_cambio": {"eje": "sen", "nombre": "Riesgo país (cambio en 3 meses)", "unidad": "pb en 3 meses", "dec": 0,
                  "que": "Cuánto subió o bajó el riesgo país en las últimas 63 ruedas.",
                  "umbrales": "Baja: sano · +150 pb: atención · +300 pb: deterioro · +500 pb: fuerte",
                  "fuente": "EMBI JP Morgan vía argentinadatos.com y Rava", "url": "https://www.rava.com/cotizaciones/historico/riesgo-pais/hoy/"},
    "merval_tend": {"eje": "sen", "nombre": "Tendencia del Merval en dólares", "unidad": "% desde el máximo", "dec": 1,
                    "que": "Merval en dólares contra sus medias de 50 y 200 ruedas y caída desde el máximo de 52 semanas.",
                    "umbrales": "Mismo criterio que la tendencia del S&P 500",
                    "fuente": "Cálculo propio", "url": "https://www.rava.com/perfil/MERVAL%20USD"},
    "reservas": {"eje": "sen", "nombre": "Reservas del BCRA (cambio en 3 meses)", "unidad": "millones de US$", "dec": 0,
                 "que": "Variación de las reservas internacionales brutas en 90 días. Caídas sostenidas anticipan tensión cambiaria.",
                 "umbrales": "Suben > 3.000: sano · ±0: atención · caen > 3.000: deterioro",
                 "fuente": "BCRA (API de estadísticas, variable 1)", "url": "https://www.bcra.gob.ar/PublicacionesEstadisticas/Principales_variables.asp"},
    "tcr": {"eje": "sen", "nombre": "Tipo de cambio real multilateral", "unidad": "índice (17-12-15 = 100)", "dec": 1,
            "que": "Un peso muy apreciado (índice bajo frente a su historia) es atraso cambiario: históricamente terminó en saltos del dólar.",
            "umbrales": "Puntaje por percentil desde 2003: muy bajo (< 15) = deterioro fuerte · medio = neutro · alto = sano",
            "fuente": "BCRA (ITCRM diario)", "url": "https://www.bcra.gob.ar/PublicacionesEstadisticas/Indices_tipo_cambio_multilateral.asp"},
    "brecha": {"eje": "sen", "nombre": "Brecha CCL / oficial", "unidad": "%", "dec": 1,
               "que": "Diferencia entre el dólar contado con liquidación y el oficial. Una brecha que se abre anticipa presión cambiaria.",
               "umbrales": "< 10% sano · 10–25% atención · 25–50% deterioro · > 50% fuerte",
               "fuente": "argentinadatos.com", "url": "https://www.ambito.com/contenidos/dolar.html"},
}


def na(v, d=2, signo=False) -> str:
    """Número con formato argentino (punto de miles y coma decimal)."""
    s = f"{v:+,.{d}f}" if signo else f"{v:,.{d}f}"
    return s.replace(",", "X").replace(".", ",").replace("X", ".")


def ind(id_, valor, score, fecha, **extra) -> dict:
    m = META[id_]
    z = zona(score, m["eje"])
    return {"id": id_, "valor": rnd(valor, m["dec"] + 1), "score": rnd(score, 1),
            "fecha": fecha.strftime("%Y-%m-%d") if hasattr(fecha, "strftime") else fecha, **z, **extra}


# ================================================================= Datos
class Datos:
    def __init__(self):
        self.s: dict[str, pd.Series] = {}
        self.err: list[str] = []
        self.factset: list[dict] = []
        self.n_amplitud = None

    def get(self, k):
        return self.s.get(k)

    def paso(self, nombre, fn):
        try:
            fn()
        except Exception as e:
            self.err.append(f"{nombre}: {e}")
            print(f"[aviso] {nombre}: {traceback.format_exc(limit=2)}")


def cargar_constituyentes(d: Datos) -> list[str]:
    cache = load_json(SP500, {})
    try:
        from bs4 import BeautifulSoup

        r = _get("https://en.wikipedia.org/wiki/List_of_S%26P_500_companies")
        soup = BeautifulSoup(r.text, "html.parser")
        tabla = soup.find("table", id="constituents")
        tick = []
        for tr in tabla.find_all("tr")[1:]:
            td = tr.find("td")
            if td:
                t = td.get_text(strip=True).replace(".", "-")
                if t:
                    tick.append(t)
        if len(tick) >= 480:
            save_json(SP500, {"fecha": now_ar().strftime("%Y-%m-%d"), "tickers": tick})
            return tick
        d.err.append(f"Wikipedia S&P 500: solo {len(tick)} empresas; se usa la lista guardada")
    except Exception as e:
        d.err.append(f"Wikipedia S&P 500: {e}; se usa la lista guardada")
    return cache.get("tickers", [])


def cargar_amplitud(d: Datos):
    tick = cargar_constituyentes(d)
    if len(tick) < 400:
        raise ValueError("sin lista de empresas del S&P 500")
    cierres = {}
    for i in range(0, len(tick), 100):
        lote = tick[i:i + 100]
        try:
            cierres.update(yahoo(lote, period="3y", auto_adjust=True))
        except Exception as e:
            d.err.append(f"Amplitud, lote {i // 100 + 1}: {e}")
    if len(cierres) < 400:
        raise ValueError(f"solo {len(cierres)} empresas con precios")
    df = pd.DataFrame(cierres).sort_index()
    df = df[df.notna().sum(axis=1) >= 0.8 * df.shape[1]]
    mm200 = df.rolling(200, min_periods=200).mean()
    mm50 = df.rolling(50, min_periods=50).mean()
    val200 = mm200.notna() & df.notna()
    val50 = mm50.notna() & df.notna()
    n200 = val200.sum(axis=1)
    p200 = ((df > mm200) & val200).sum(axis=1) / n200 * 100
    p50 = ((df > mm50) & val50).sum(axis=1) / val50.sum(axis=1) * 100
    d.s["amp200"] = p200[n200 >= 300]
    d.s["amp50"] = p50[n200 >= 300]
    d.n_amplitud = len(cierres)


def cargar_datos() -> Datos:
    d = Datos()

    def y1():
        r = yahoo(["^GSPC", "^VIX", "^VIX3M", "BZ=F", "^MERV"], start="1996-01-01")
        for k, t in {"spx": "^GSPC", "vix": "^VIX", "vix3m": "^VIX3M", "brent": "BZ=F", "merv_sp": "^MERV"}.items():
            if t in r:
                d.s[k] = r[t]
        if "spx" not in d.s:
            raise ValueError("sin datos del S&P 500")

    def y2():
        r = yahoo(["SPY", "RSP"], start="2003-05-01", auto_adjust=True)
        d.s["spy"], d.s["rsp"] = r["SPY"], r["RSP"]

    d.paso("Yahoo (índices)", y1)
    d.paso("Yahoo (SPY/RSP)", y2)
    d.paso("FRED tasa real 10 años (DFII10)", lambda: d.s.__setitem__("dfii10", fred("DFII10", "2003-01-01")))
    d.paso("FRED High Yield (BAMLH0A0HYM2)", lambda: d.s.__setitem__("hy", fred("BAMLH0A0HYM2", "1997-01-01") * 100))
    d.paso("FRED spread Baa (BAA10Y)", lambda: d.s.__setitem__("baa", fred("BAA10Y", "1990-01-01")))
    d.paso("FRED curva 10a-3m (T10Y3M)", lambda: d.s.__setitem__("curva", fred("T10Y3M", "1990-01-01")))
    d.paso("CAPE (multpl)", lambda: d.s.__setitem__("cape", cape_multpl()))
    d.paso("Amplitud del S&P 500", lambda: cargar_amplitud(d))

    # ---- FactSet (carga semanal)
    def fs():
        spx = d.get("spx")
        for r in load_json(FACTSET, {}).get("informes", []):
            pe = clean(r.get("pe_fwd"))
            if not pe:
                continue
            f = pd.Timestamp(r["fecha"])
            eps, precio = clean(r.get("eps_fwd")), clean(r.get("precio"))
            fp = pd.Timestamp(r["fecha_precio"]) if r.get("fecha_precio") else None
            if precio is None and spx is not None:
                a = asof(spx, fp if fp is not None else f - pd.Timedelta(days=1))
                if a:
                    fp, precio = a
            if eps is None and precio:
                eps = precio / pe
            if eps:
                d.factset.append({"fecha": f, "pe": pe, "eps": eps, "precio": precio,
                                  "fecha_precio": fp, "eps_informado": r.get("eps_fwd") is not None})
        d.factset.sort(key=lambda x: x["fecha"])
        if not d.factset:
            raise ValueError("sin informes cargados")

    d.paso("FactSet (factset.json)", fs)

    # ---- Argentina
    hist_tab = load_json(HIST_TABLERO, {})

    def rp():
        s = {}
        for h in argdatos("finanzas/indices/riesgo-pais"):
            s[h["fecha"][:10]] = h["valor"]
        s.update(hist_tab.get("riesgo_ar", {}))
        d.s["rp"] = a_serie(s)

    def dolares():
        for casa, k in (("contadoconliqui", "ccl"), ("oficial", "oficial")):
            pts = [(h["fecha"], h.get("venta")) for h in argdatos(f"cotizaciones/dolares/{casa}")]
            d.s[k] = a_serie(pts)

    d.paso("Riesgo país (argentinadatos)", rp)
    d.paso("Dólares (argentinadatos)", dolares)

    def merval():
        f_emp, base = MERVAL_EMPALME
        viejo = d.get("merv_sp")
        serie = pd.Series(dtype=float)
        if viejo is not None and not viejo.empty:
            a = asof(viejo, pd.Timestamp(f_emp))
            if a:
                serie = viejo.loc[:f_emp] * (base / a[1])
        nuevo = a_serie({k: v for k, v in hist_tab.get("merval_msci", {}).items() if k > f_emp})
        serie = pd.concat([serie, nuevo]).sort_index()
        serie = serie[~serie.index.duplicated(keep="last")]
        if serie.empty:
            raise ValueError("sin Merval")
        d.s["merval"] = serie
        ccl = d.get("ccl")
        if ccl is not None and not ccl.empty:
            c = ccl.reindex(serie.index.union(ccl.index)).ffill(limit=5).reindex(serie.index)
            d.s["merval_usd"] = (serie / c).dropna()

    d.paso("Merval en dólares", merval)
    d.paso("Reservas BCRA", lambda: d.s.__setitem__("reservas", bcra_reservas("2018-01-01")))
    d.paso("Tipo de cambio real (BCRA)", lambda: d.s.__setitem__("tcr", bcra_itcrm()))

    if d.get("ccl") is not None and d.get("oficial") is not None:
        o = d.s["oficial"].reindex(d.s["ccl"].index).ffill(limit=5)
        d.s["brecha"] = ((d.s["ccl"] / o - 1) * 100).dropna()
    return d


# ================================================================= Indicadores (evaluados a una fecha t)
def factset_a(d: Datos, t, max_dias=35):
    pts = [p for p in d.factset if p["fecha"] <= t]
    if not pts or (t - pts[-1]["fecha"]).days > max_dias:
        return None
    return pts[-1]


def pe_fwd_a(d: Datos, t):
    p = factset_a(d, t)
    a = asof(d.get("spx"), t, 7)
    if not p or not a:
        return None
    return a[1] / p["eps"], p, a


def i_pe_fwd(d, t):
    r = pe_fwd_a(d, t)
    if not r:
        return None
    pe, p, (fs, px) = r
    return ind("pe_fwd", pe, interp(pe, A["pe_fwd"]), fs,
               detalle=[["Último informe FactSet", p["fecha"].strftime("%d/%m/%Y")],
                        ["P/E informado", f"{na(p['pe'], 1)}"],
                        ["EPS forward", f"US$ {na(p['eps'], 2)}"],
                        ["Referencias FactSet", "Prom. 5 años 19,8 · 10 años 19,1 · 25 años 16,4"]])


def i_erp(d, t):
    r = pe_fwd_a(d, t)
    a = asof(d.get("dfii10"), t, 10)
    if not r or not a:
        return None
    ey = 100 / r[0]
    erp = ey - a[1]
    return ind("erp", erp, interp(erp, A["erp"]), a[0],
               detalle=[["Rendimiento por ganancias", f"{na(ey, 2)}%"], ["Tasa real 10 años (TIPS)", f"{na(a[1], 2)}%"]])


def i_cape(d, t):
    s = d.get("cape")
    a = asof(s, t, 45)
    if not a:
        return None
    pt = percentil(hasta(s, t), a[1])
    p90 = percentil(hasta(s, t).loc["1990-01-01":], a[1])
    return ind("cape", a[1], interp(a[1], A["cape"]), a[0], percentil=rnd(pt, 0), percentil_desde="1881",
               detalle=[["Percentil desde 1881", f"{na(pt, 0)}"], ["Percentil desde 1990", f"{na(p90, 0)}"],
                        ["Promedio histórico", f"{na(hasta(s, t).mean(), 1)}"]])


def i_concentracion(d, t):
    spy, rsp = d.get("spy"), d.get("rsp")
    if spy is None or rsp is None:
        return None
    ratio = (spy / rsp).dropna()
    r = hasta(ratio, t)
    if len(r) < 260 or (t - r.index[-1]).days > 7:
        return None
    s_spy, s_rsp = hasta(spy, t), hasta(rsp, t)
    gap = ((s_spy.iloc[-1] / s_spy.iloc[-253] - 1) - (s_rsp.iloc[-1] / s_rsp.iloc[-253] - 1)) * 100
    p10 = percentil(r.loc[t - pd.Timedelta(days=3653):], r.iloc[-1])
    score = 0.5 * interp(gap, A["conc_gap"]) + 0.5 * p10
    return ind("concentracion", gap, score, r.index[-1], percentil=rnd(p10, 0), percentil_desde="10 años",
               detalle=[["SPY vs RSP en 12 meses", f"{na(gap, 1, True)} pp"], ["Percentil de SPY/RSP (10 años)", f"{na(p10, 0)}"]])


def i_amplitud(d, t):
    a = asof(d.get("amp200"), t, 7)
    if not a:
        return None
    s = hasta(d.get("amp200"), t)
    hace1m = s.iloc[-22] if len(s) > 22 else None
    a50 = asof(d.get("amp50"), t, 7)
    det = [["Sobre la media de 50 días", f"{na(a50[1], 1)}%" if a50 else "—"]]
    if hace1m is not None:
        det.append(["Hace un mes (200 días)", f"{na(hace1m, 1)}%"])
    det.append(["Empresas consideradas", str(d.n_amplitud or "—")])
    return ind("amplitud", a[1], interp(a[1], A["amplitud"]), a[0], detalle=det)


def tendencia(s: pd.Series, t):
    s = hasta(s, t)
    if len(s) < 210 or (t - s.index[-1]).days > 7:
        return None
    p = s.iloc[-1]
    mm50, mm200 = s.iloc[-50:].mean(), s.iloc[-200:].mean()
    mx = s.iloc[-252:].max()
    dd = (p / mx - 1) * 100
    if p > mm50 > mm200:
        est = 10
    elif p >= mm200:
        est = 40
    elif mm50 >= mm200:
        est = 65
    else:
        est = 85
    score = max(est, interp(dd, A["dd"]))
    det = [["Último", f"{na(p, 2)}"], ["Media 50 ruedas", f"{na(mm50, 2)}"], ["Media 200 ruedas", f"{na(mm200, 2)}"],
           ["Máximo 52 semanas", f"{na(mx, 2)} ({s.iloc[-252:].idxmax():%d/%m/%Y})"]]
    return dd, score, s.index[-1], det


def i_tendencia(d, t):
    r = tendencia(d.get("spx"), t)
    return ind("tendencia", r[0], r[1], r[2], detalle=r[3]) if r else None


def credito(d, t):
    """Spread High Yield; si FRED no tiene ese dato para la fecha, usa el spread Baa (historia larga)."""
    for clave, an_n, an_c, pb in (("hy", "hy_nivel", "hy_cambio", True), ("baa", "baa_nivel", "baa_cambio", False)):
        s = hasta(d.get(clave), t)
        if len(s) < 70 or (t - s.index[-1]).days > 10:
            continue
        v, cambio = s.iloc[-1], s.iloc[-1] - s.iloc[-64]
        score = max(interp(v, A[an_n]), interp(cambio, A[an_c]))
        return v, cambio, score, s.index[-1], clave
    return None


def i_credito(d, t):
    r = credito(d, t)
    if not r:
        return None
    v, cambio, score, f, clave = r
    if clave == "hy":
        det = [["Cambio en 3 meses", f"{na(cambio, 0, True)} pb"]]
        if v < 300:
            det.append(["Lectura", "Complacencia: el crédito no ve riesgo"])
        pc = percentil(hasta(d.get("hy"), t), v)
        out = ind("credito", v, score, f, detalle=det, percentil=rnd(pc, 0),
                  percentil_desde=hasta(d.get("hy"), t).index[0].strftime("%Y"))
    else:
        out = ind("credito", v * 100, score, f, detalle=[["Serie usada", "Spread Baa (Moody's) − bono 10 años, por falta del High Yield"],
                                                       ["Cambio en 3 meses", f"{na(cambio * 100, 0, True)} pb"]])
    return out


def i_volatilidad(d, t):
    a = asof(d.get("vix"), t, 7)
    if not a:
        return None
    score = interp(a[1], A["vix"])
    det = []
    b = asof(d.get("vix3m"), t, 7)
    if b:
        rel = a[1] / b[1]
        det.append(["VIX / VIX3M", f"{na(rel, 2)}" + (" (invertida: pánico)" if rel > 1 else "")])
        score = min(100, score + (25 if rel > 1.1 else 15 if rel > 1 else 0))
    pc = percentil(hasta(d.get("vix"), t), a[1])
    det.append(["Percentil desde 1996", f"{na(pc, 0)}"])
    return ind("volatilidad", a[1], score, a[0], detalle=det, percentil=rnd(pc, 0), percentil_desde="1996")


def revision(d, t):
    p = factset_a(d, t)
    if not p:
        return None
    previos = [x for x in d.factset if x["fecha"] < p["fecha"]]

    def cerca(dias, tol):
        obj = p["fecha"] - pd.Timedelta(days=dias)
        c = [x for x in previos if abs((x["fecha"] - obj).days) <= tol]
        return min(c, key=lambda x: abs((x["fecha"] - obj).days)) if c else None

    p4, p13 = cerca(28, 8), cerca(91, 16)
    c4 = (p["eps"] / p4["eps"] - 1) * 100 if p4 else None
    c13 = (p["eps"] / p13["eps"] - 1) * 100 if p13 else None
    return p, c4, c13


def i_revisiones(d, t):
    r = revision(d, t)
    if not r:
        return None
    p, c4, c13 = r
    base = c4 if c4 is not None else (c13 / 3.25 if c13 is not None else None)
    if base is None:
        return None
    det = [["Cambio en 4 semanas", f"{na(c4, 2, True)}%" if c4 is not None else "—"],
           ["Cambio en 13 semanas", f"{na(c13, 2, True)}%" if c13 is not None else "—"],
           ["EPS forward", f"US$ {na(p['eps'], 2)}"]]
    return ind("revisiones", base, interp(base, A["rev4"]), p["fecha"], detalle=det)


# ---- Argentina
def i_merval_usd(d, t):
    s = hasta(d.get("merval_usd"), t)
    if len(s) < 300 or (t - s.index[-1]).days > 10:
        return None
    s14 = s.loc["2014-01-01":]
    pc = percentil(s14, s.iloc[-1])
    return ind("merval_usd", s.iloc[-1], pc, s.index[-1], percentil=rnd(pc, 0), percentil_desde="2014",
               detalle=[["Percentil desde 2014", f"{na(pc, 0)}"], ["Mediana desde 2014", f"{na(s14.median(), 1)}"],
                        ["Máximo desde 2014", f"{na(s14.max(), 1)} ({s14.idxmax():%m/%Y})"]])


def i_rp_nivel(d, t):
    a = asof(d.get("rp"), t, 10)
    if not a:
        return None
    pc = percentil(hasta(d.get("rp"), t).loc["2014-01-01":], a[1])
    return ind("rp_nivel", a[1], interp(a[1], A["rp_nivel"]), a[0], percentil=rnd(pc, 0), percentil_desde="2014",
               detalle=[["Percentil desde 2014", f"{na(pc, 0)}" if pc is not None else "—"]])


def i_rp_cambio(d, t):
    s = hasta(d.get("rp"), t)
    if len(s) < 70 or (t - s.index[-1]).days > 10:
        return None
    c = s.iloc[-1] - s.iloc[-64]
    return ind("rp_cambio", c, interp(c, A["rp_cambio"]), s.index[-1],
               detalle=[["Hoy", f"{na(s.iloc[-1], 0)} pb"], ["Hace 3 meses", f"{na(s.iloc[-64], 0)} pb"]])


def i_merval_tend(d, t):
    r = tendencia(d.get("merval_usd"), t)
    return ind("merval_tend", r[0], r[1], r[2], detalle=r[3]) if r else None


def i_reservas(d, t):
    s = hasta(d.get("reservas"), t)
    if len(s) < 70 or (t - s.index[-1]).days > 15:
        return None
    a = asof(s, s.index[-1] - pd.Timedelta(days=90))
    if not a:
        return None
    c = s.iloc[-1] - a[1]
    return ind("reservas", c, interp(c, A["reservas"]), s.index[-1],
               detalle=[["Reservas brutas", f"US$ {na(s.iloc[-1], 0)} M"], ["Hace 90 días", f"US$ {na(a[1], 0)} M"]])


def i_tcr(d, t):
    s = hasta(d.get("tcr"), t)
    if len(s) < 300 or (t - s.index[-1]).days > 20:
        return None
    pc = percentil(s.loc["2003-01-01":], s.iloc[-1])
    return ind("tcr", s.iloc[-1], interp(pc, A["tcr_pct"]), s.index[-1], percentil=rnd(pc, 0), percentil_desde="2003",
               detalle=[["Percentil desde 2003", f"{na(pc, 0)}"], ["Mediana desde 2003", na(s.loc['2003-01-01':].median(), 1)]])


def i_brecha(d, t):
    a = asof(d.get("brecha"), t, 10)
    if not a:
        return None
    return ind("brecha", a[1], interp(a[1], A["brecha"]), a[0])


IND_US = {"val": [i_pe_fwd, i_erp, i_cape, i_concentracion],
          "sen": [i_amplitud, i_tendencia, i_credito, i_volatilidad, i_revisiones]}
IND_AR = {"val": [i_merval_usd, i_rp_nivel],
          "sen": [i_rp_cambio, i_merval_tend, i_reservas, i_tcr, i_brecha]}


def evaluar(d: Datos, t, fns, pesos_val, pesos_sen, matriz, completo=False):
    out = {}
    for eje, lista, pesos in (("val", fns["val"], pesos_val), ("sen", fns["sen"], pesos_sen)):
        inds = []
        for fn in lista:
            try:
                r = fn(d, t)
            except Exception as e:
                if completo:
                    d.err.append(f"Cálculo {fn.__name__[2:]}: {e}")
                r = None
            if r:
                inds.append(r)
        p = puntaje(inds, pesos)
        p.update(zona(p["score"], eje))
        out[eje] = p
        out[eje + "_ind"] = inds
    out["matriz"] = lectura_matriz(out["val"]["score"], out["sen"]["score"], matriz)
    return out


def con_meta(i: dict, pesos: dict, serie: pd.Series | None, desde, dec):
    m = META[i["id"]]
    i = dict(i)
    i.update({k: m[k] for k in ("nombre", "unidad", "que", "umbrales", "fuente", "url", "eje")})
    i["dec"] = m["dec"]
    i["peso"] = pesos.get(i["id"])
    if serie is not None:
        i["serie"] = muestra(serie, desde, 160, dec)
    return i


def historia(d, fechas, fns, pv, ps, matriz):
    out = []
    for t in fechas:
        e = evaluar(d, t, fns, pv, ps, matriz)
        out.append({"f": t.strftime("%Y-%m-%d"), "val": e["val"]["score"], "sen": e["sen"]["score"],
                    "cv": e["val"]["cobertura"], "cs": e["sen"]["cobertura"]})
    return out


def viernes(desde: pd.Timestamp, hasta_: pd.Timestamp):
    return list(pd.date_range(desde, hasta_, freq="W-FRI"))


def serie_indicador(d: Datos, id_: str, t: pd.Timestamp):
    """Serie para el gráfico de cada tarjeta (valor del indicador en el tiempo)."""
    g = d.get
    if id_ == "pe_fwd" and d.factset and g("spx") is not None:
        pts = {}
        for f in pd.date_range(t - pd.Timedelta(days=730), t, freq="B"):
            r = pe_fwd_a(d, f)
            if r:
                pts[r[2][0]] = r[0]
        return pd.Series(pts, dtype=float)
    if id_ == "erp" and d.factset:
        pts = {}
        for f in pd.date_range(t - pd.Timedelta(days=730), t, freq="B"):
            r, a = pe_fwd_a(d, f), asof(g("dfii10"), f, 10)
            if r and a:
                pts[r[2][0]] = 100 / r[0] - a[1]
        return pd.Series(pts, dtype=float)
    if id_ == "revisiones":
        return pd.Series({p["fecha"]: p["eps"] for p in d.factset}, dtype=float)
    if id_ == "concentracion" and g("spy") is not None and g("rsp") is not None:
        return (g("spy") / g("rsp")).dropna()
    if id_ == "credito":
        return g("hy") if g("hy") is not None else (g("baa") * 100 if g("baa") is not None else None)
    m = {"cape": "cape", "amplitud": "amp200", "tendencia": "spx", "volatilidad": "vix", "merval_usd": "merval_usd",
         "rp_nivel": "rp", "rp_cambio": "rp", "merval_tend": "merval_usd", "reservas": "reservas", "tcr": "tcr", "brecha": "brecha"}
    return g(m[id_]) if id_ in m else None


SERIE_TITULO = {"revisiones": "EPS forward (US$)", "concentracion": "Relación SPY / RSP", "rp_cambio": "Riesgo país (pb)",
                "merval_tend": "Merval en US$", "reservas": "Reservas (M US$)", "tendencia": "S&P 500"}


def bloque(d, t, fns, pv, ps, matriz, fechas_clave, años_hist=2.5):
    e = evaluar(d, t, fns, pv, ps, matriz, completo=True)
    inds = []
    for eje, pesos in (("val", pv), ("sen", ps)):
        for i in e[eje + "_ind"]:
            s = serie_indicador(d, i["id"], t)
            desde = t - pd.Timedelta(days=1100 if i["id"] not in ("cape", "tcr", "merval_usd", "rp_nivel") else 365 * 12)
            x = con_meta(i, pesos, s, desde, 2)
            x["serie_titulo"] = SERIE_TITULO.get(i["id"], META[i["id"]]["nombre"])
            inds.append(x)
    faltan = [{"id": k, "nombre": META[k]["nombre"]} for k in e["val"]["faltan"] + e["sen"]["faltan"]]
    hist = historia(d, viernes(t - pd.Timedelta(days=int(365 * años_hist)), t - pd.Timedelta(days=1)) + [t],
                    fns, pv, ps, matriz)
    claves = []
    for f, etiqueta in fechas_clave:
        ef = evaluar(d, f, fns, pv, ps, matriz)
        claves.append({"fecha": f.strftime("%Y-%m-%d"), "etiqueta": etiqueta, "val": ef["val"], "sen": ef["sen"],
                       "matriz": ef["matriz"],
                       "inds": {i["id"]: {"valor": i["valor"], "score": i["score"], "zona": i["zona"], "nivel": i["nivel"]}
                                for i in ef["val_ind"] + ef["sen_ind"]}})
    return {"fecha": t.strftime("%Y-%m-%d"), "val": e["val"], "sen": e["sen"], "matriz": e["matriz"],
            "indicadores": inds, "faltan": faltan, "historia": hist, "claves": claves,
            "pesos": {"val": pv, "sen": ps}}


TRAMOS = [(-10, 15), (-15, 20), (-20, 25), (-25, 25), (-30, 15)]


def reingreso(d: Datos, t):
    s = hasta(d.get("spx"), t)
    if len(s) < 260:
        return None
    ventana = s.iloc[-252:]
    mx, fmx = ventana.max(), ventana.idxmax()
    desde_max = s.loc[fmx:]
    minimo = desde_max.min()
    dd_hoy, dd_min = (s.iloc[-1] / mx - 1) * 100, (minimo / mx - 1) * 100
    acum = 0
    tramos = []
    for nivel, pct in TRAMOS:
        acum += pct
        tramos.append({"caida": nivel, "nivel": round(mx * (1 + nivel / 100), 2), "pct": pct, "acum": acum,
                       "alcanzado": bool(dd_min <= nivel)})
    return {"maximo": round(mx, 2), "fecha_max": fmx.strftime("%Y-%m-%d"), "ultimo": round(s.iloc[-1], 2),
            "dd_hoy": round(dd_hoy, 2), "minimo": round(minimo, 2), "fecha_min": desde_max.idxmin().strftime("%Y-%m-%d"),
            "dd_min": round(dd_min, 2), "tramos": tramos}


def extras(d: Datos, t):
    out = []
    a = asof(d.get("dfii10"), t, 10)
    if a:
        out.append({"nombre": "Tasa real 10 años (TIPS)", "valor": f"{na(a[1], 2)}%", "fecha": a[0].strftime("%Y-%m-%d"),
                    "nota": "Componente de la prima de riesgo. Más alta = más competencia para las acciones."})
    a = asof(d.get("curva"), t, 10)
    if a:
        out.append({"nombre": "Curva 10 años − 3 meses", "valor": f"{na(a[1], 2, True)} pp", "fecha": a[0].strftime("%Y-%m-%d"),
                    "nota": "Invertida (negativa) anticipó recesiones con 6 a 24 meses de demora. Solo de referencia: no suma al puntaje."})
    return out


def extras_ar(d: Datos, t):
    out = []
    s = hasta(d.get("brent"), t)
    if len(s) > 210:
        mm200 = s.iloc[-200:].mean()
        out.append({"nombre": "Petróleo Brent (para VIST, YPF, PAMP)", "valor": f"US$ {na(s.iloc[-1], 2)}",
                    "fecha": s.index[-1].strftime("%Y-%m-%d"),
                    "nota": f"Media de 200 ruedas: US$ {na(mm200, 2)} ({na((s.iloc[-1] / mm200 - 1) * 100, 1, True)}%). Solo de referencia."})
    return out


# ================================================================= principal
def main():
    DIR.mkdir(parents=True, exist_ok=True)
    d = cargar_datos()
    anterior = load_json(ACTUAL, {})
    out = {"actualizado": now_ar().isoformat(timespec="minutes"), "errores": d.err}

    spx = d.get("spx")
    if spx is not None and not spx.empty:
        t = spx.index[-1]
        s = spx.iloc[-252:]
        fmx = s.idxmax()
        post = spx.loc[fmx:]
        claves = [(pd.Timestamp(FECHA_REFERENCIA[0]), FECHA_REFERENCIA[1]),
                  (fmx, f"Máximo del S&P 500 ({na(s.max(), 2)})")]
        if post.idxmin() != fmx and post.idxmin() != t:
            claves.append((post.idxmin(), "Mínimo desde el máximo"))
        claves.append((t, "Hoy"))
        claves = sorted({k[0]: k for k in claves}.values(), key=lambda x: x[0])
        out["eeuu"] = bloque(d, t, IND_US, PESOS["us_val"], PESOS["us_sen"], MATRIZ_EEUU, claves)
        out["eeuu"]["reingreso"] = reingreso(d, t)
        out["eeuu"]["extras"] = extras(d, t)
        out["eeuu"]["factset"] = [{"fecha": p["fecha"].strftime("%Y-%m-%d"), "pe": p["pe"], "eps": round(p["eps"], 2),
                                   "eps_informado": p["eps_informado"]} for p in d.factset][-20:]
    elif "eeuu" in anterior:
        out["eeuu"] = dict(anterior["eeuu"], desactualizado=True)

    mu = d.get("merval_usd")
    rp = d.get("rp")
    if (mu is not None and not mu.empty) or (rp is not None and not rp.empty):
        t_ar = max(x.index[-1] for x in (mu, rp) if x is not None and not x.empty)
        claves = [(pd.Timestamp(FECHA_REFERENCIA[0]), FECHA_REFERENCIA[1]), (t_ar, "Hoy")]
        out["argentina"] = bloque(d, t_ar, IND_AR, PESOS["ar_val"], PESOS["ar_sen"], MATRIZ_AR, claves)
        out["argentina"]["extras"] = extras_ar(d, t_ar)
    elif "argentina" in anterior:
        out["argentina"] = dict(anterior["argentina"], desactualizado=True)

    save_json(ACTUAL, out)

    # Historial diario acumulativo: se agrega o actualiza el día; nunca se borra nada.
    hist = load_json(HIST, {"_nota": "Un registro por día. Acumulativo: no se borra nada.", "dias": {}})
    hoy = now_ar().strftime("%Y-%m-%d")
    reg = {}
    for k in ("eeuu", "argentina"):
        b = out.get(k)
        if b and not b.get("desactualizado"):
            reg[k] = {"fecha_datos": b["fecha"], "val": b["val"]["score"], "sen": b["sen"]["score"],
                      "postura": (b.get("matriz") or {}).get("titulo"),
                      "ind": {i["id"]: [i["valor"], i["score"]] for i in b["indicadores"]}}
    if reg:
        hist.setdefault("dias", {})[hoy] = reg
    save_json(HIST, hist)

    print(f"OK · EE.UU.: {out.get('eeuu', {}).get('val', {}).get('score')} / {out.get('eeuu', {}).get('sen', {}).get('score')}"
          f" · Argentina: {out.get('argentina', {}).get('val', {}).get('score')} / {out.get('argentina', {}).get('sen', {}).get('score')}"
          f" · {len(d.err)} avisos")
    for e in d.err:
        print(" -", e)


if __name__ == "__main__":
    main()
