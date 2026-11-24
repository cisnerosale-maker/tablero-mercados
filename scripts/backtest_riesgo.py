#!/usr/bin/env python3
"""
Backtesting del monitor de riesgo (EE.UU.) desde 1991.

Usa una versión REDUCIDA del modelo, porque no hay historia gratuita y larga del P/E forward,
la prima de riesgo, la amplitud ni las revisiones de EPS:
  - Valuación: solo CAPE (último dato publicado, ajustado por el precio del S&P 500).
  - Señales: tendencia del S&P 500, crédito (spread Baa de Moody's en lugar del High Yield) y VIX.
Mismos umbrales, misma matriz y mismos rangos de liquidez que la página.

Estrategias comparadas (rebalanceo semanal, costo 0,10% sobre lo que se mueve):
  - Comprar y mantener: 100% S&P 500 con dividendos.
  - Matriz completa: liquidez = punto medio del rango de la celda.
  - Solo señales: la columna de la matriz con la fila "Justo" (sin mirar valuación).
  - Solo valuación: la fila de la matriz con la columna "Sanas" (sin mirar señales).
La liquidez rinde la letra del Tesoro a 3 meses.

Escribe data/riesgo/backtest.json.
"""
from __future__ import annotations

import math

import pandas as pd

from riesgo_comun import DIR, MATRIZ_EEUU, asof, cape_con_respaldo, col_sen, fila_val, fred, now_ar, rnd, save_json, yahoo
from update_riesgo import Datos, evaluar, i_cape, i_credito, i_tendencia, i_volatilidad

INICIO = "1991-01-04"
COSTO = 0.001
FNS = {"val": [i_cape], "sen": [i_tendencia, i_credito, i_volatilidad]}
PV = {"cape": 1}
PS = {"tendencia": 20, "credito": 20, "volatilidad": 15}

MEDIO = [[sum(c["liq"]) / 2 for c in fila] for fila in MATRIZ_EEUU]
EPISODIOS = [
    ("Burbuja puntocom", "2000-03-24", "2002-10-11"),
    ("Crisis 2008", "2007-10-12", "2009-03-13"),
    ("Suba 2009–2021", "2009-03-13", "2021-12-31"),
    ("COVID", "2020-02-21", "2020-03-27"),
    ("Baja 2022", "2022-01-07", "2022-10-14"),
]


def metricas(v: pd.Series, cash_w: pd.Series) -> dict:
    r = v.pct_change().dropna()
    años = (v.index[-1] - v.index[0]).days / 365.25
    cagr = (v.iloc[-1] / v.iloc[0]) ** (1 / años) - 1
    vol = r.std() * math.sqrt(52)
    exc = (r - cash_w.reindex(r.index).fillna(0)).mean() * 52
    dd = (v / v.cummax() - 1).min()
    r52 = (v / v.shift(52) - 1).dropna()
    return {"cagr": rnd(cagr * 100, 2), "vol": rnd(vol * 100, 2), "max_dd": rnd(dd * 100, 1),
            "sharpe": rnd(exc / vol if vol else None, 2), "final": rnd(100000 * v.iloc[-1] / v.iloc[0], 0),
            "peor_12m": rnd(r52.min() * 100, 1)}


def main():
    d = Datos()
    avisos = []
    px = yahoo(["^GSPC", "^SP500TR", "^VIX", "^VIX3M", "^IRX"], start="1989-01-01")
    d.s["spx"], d.s["vix"] = px["^GSPC"], px["^VIX"]
    if "^VIX3M" in px:
        d.s["vix3m"] = px["^VIX3M"]
    tr = px.get("^SP500TR")
    try:
        d.s["baa"] = fred("BAA10Y", "1988-01-01")
    except Exception as e:
        avisos.append(f"Sin spread Baa de FRED ({str(e)[:60]}): las señales usan solo tendencia y VIX.")
    d.s["cape"] = cape_con_respaldo(avisos)
    try:
        tb = fred("DTB3", "1988-01-01")
    except Exception:
        tb = px.get("^IRX")

    fechas = pd.date_range(INICIO, d.s["spx"].index[-1], freq="W-FRI")
    filas = []
    for t in fechas:
        e = evaluar(d, t, FNS, PV, PS, MATRIZ_EEUU)
        a_tr = asof(tr, t, 7) if tr is not None else None
        a_px = asof(d.s["spx"], t, 7)
        if not a_px:
            continue
        a_tb = asof(tb, t, 15)
        filas.append({"f": t, "val": e["val"]["score"], "sen": e["sen"]["score"],
                      "tr": a_tr[1] if a_tr else None, "px": a_px[1], "tb": a_tb[1] if a_tb else 0.0})
    df = pd.DataFrame(filas).set_index("f")
    # Si falta el índice con dividendos, se aproxima con el precio + 2% anual de dividendos.
    if df["tr"].isna().any():
        aprox = df["px"] * (1.02 ** ((df.index - df.index[0]).days / 365.25))
        df["tr"] = df["tr"].fillna(aprox * (df["tr"].dropna().iloc[0] / aprox.loc[df["tr"].dropna().index[0]])
                                   if df["tr"].notna().any() else aprox)
    df["r_acc"] = df["tr"].pct_change().fillna(0)
    df["r_cash"] = (1 + df["tb"].shift(1).fillna(0) / 100) ** (1 / 52) - 1
    df["fila"] = df["val"].map(fila_val)
    df["col"] = df["sen"].map(col_sen)

    def liq_matriz(r):
        if pd.isna(r["fila"]) or pd.isna(r["col"]):
            return 0.0
        return MEDIO[int(r["fila"])][int(r["col"])] / 100

    df["liq_matriz"] = df.apply(liq_matriz, axis=1)
    df["liq_senales"] = df["col"].map(lambda c: 0.0 if pd.isna(c) else MEDIO[1][int(c)] / 100)
    df["liq_valuacion"] = df["fila"].map(lambda f: 0.0 if pd.isna(f) else MEDIO[int(f)][0] / 100)
    df["liq_bh"] = 0.0

    curvas = {}
    for k in ("bh", "matriz", "senales", "valuacion"):
        liq = df["liq_" + k].shift(1).fillna(0)  # la decisión del viernes se aplica a la semana siguiente
        mov = liq.diff().abs().fillna(0)
        r = (1 - liq) * df["r_acc"] + liq * df["r_cash"] - mov * COSTO
        curvas[k] = (1 + r).cumprod()

    nombres = {"bh": "Comprar y mantener", "matriz": "Matriz completa", "senales": "Solo señales", "valuacion": "Solo valuación"}
    estrategias = []
    for k, v in curvas.items():
        m = metricas(v, df["r_cash"])
        m.update({"id": k, "nombre": nombres[k], "liq_prom": rnd(df["liq_" + k].mean() * 100, 1)})
        estrategias.append(m)

    episodios = []
    t_fin = df.index[-1]
    mx26 = df.loc["2026-01-01":, "px"]
    eps = list(EPISODIOS)
    if not mx26.empty:
        eps.append(("Corrección 2026", mx26.idxmax().strftime("%Y-%m-%d"), t_fin.strftime("%Y-%m-%d")))
    for nombre, a, b in eps:
        ta, tb_ = pd.Timestamp(a), pd.Timestamp(b)
        if ta < df.index[0] or ta >= t_fin:
            continue
        fila = {"nombre": nombre, "desde": a, "hasta": min(tb_, t_fin).strftime("%Y-%m-%d")}
        for k, v in curvas.items():
            va, vb = asof(v, ta), asof(v, min(tb_, t_fin))
            fila[k] = rnd((vb[1] / va[1] - 1) * 100, 1) if va and vb else None
        x = df.loc[ta:min(tb_, t_fin)]
        fila["liq_prom"] = rnd(x["liq_matriz"].mean() * 100, 1)
        episodios.append(fila)

    celdas = df.groupby(["fila", "col"]).size()
    tiempo = [[rnd(celdas.get((f, c), 0) / len(df) * 100, 1) for c in range(3)] for f in range(3)]

    paso = 2
    sub = df.iloc[::paso]
    if sub.index[-1] != df.index[-1]:
        sub = pd.concat([sub, df.iloc[[-1]]])
    out = {
        "actualizado": now_ar().isoformat(timespec="minutes"),
        "desde": df.index[0].strftime("%Y-%m-%d"), "hasta": t_fin.strftime("%Y-%m-%d"),
        "semanas": len(df), "costo": COSTO,
        "estrategias": estrategias, "episodios": episodios, "tiempo_celdas": tiempo,
        "curvas": {"f": [x.strftime("%Y-%m-%d") for x in sub.index],
                   **{k: [rnd(v.loc[x] * 100000, 0) for x in sub.index] for k, v in curvas.items()},
                   "liq": [rnd(df.loc[x, "liq_matriz"] * 100, 1) for x in sub.index],
                   "val": [rnd(df.loc[x, "val"], 0) for x in sub.index],
                   "sen": [rnd(df.loc[x, "sen"], 0) for x in sub.index]},
        "avisos": avisos,
        "limitaciones": [
            "Modelo reducido: valuación solo con CAPE (hasta 2022 el dato de enero de cada año, ajustado por el precio del S&P 500); "
            "señales con tendencia, crédito (spread Baa) y VIX. "
            "El P/E forward, la prima de riesgo, la amplitud y las revisiones de EPS no tienen historia gratuita larga.",
            "Rebalanceo semanal sin impuestos; costo de 0,10% sobre lo que se mueve.",
            "La liquidez rinde la letra del Tesoro de EE.UU. a 3 meses.",
            "Los umbrales se definieron conociendo la historia: un resultado bueno acá es necesario, no suficiente.",
        ],
    }
    save_json(DIR / "backtest.json", out)
    for e in estrategias:
        print(e)


if __name__ == "__main__":
    main()
