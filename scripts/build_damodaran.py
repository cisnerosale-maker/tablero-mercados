#!/usr/bin/env python3
"""
Promedios históricos por sector a partir de los datos anuales de Aswath Damodaran (NYU Stern).

Damodaran publica cada enero los múltiplos de unas 90 industrias de EE.UU. (todas las empresas que
cotizan, no solo el S&P 500). Este programa:
  1. baja los archivos de P/E, P/Valor libros, EV/EBITDA y P/Ventas de los últimos 10 eneros;
  2. asigna cada industria a uno de los 11 sectores GICS (reglas en SECTOR_REGLAS);
  3. combina las industrias de cada sector ponderando por capitalización (archivo DollarUS);
  4. guarda el valor de cada enero y los promedios de 3, 5 y 10 años en data/sectores/damodaran.json.

Se corre una vez por año (enero/febrero) o cuando falta el archivo. Los datos de un año no cambian.
"""
from __future__ import annotations

import io
import re
import sys

import pandas as pd

from riesgo_comun import ROOT, _get, load_json, now_ar, rnd, save_json

OUT = ROOT / "data" / "sectores" / "damodaran.json"
BASE = "https://pages.stern.nyu.edu/~adamodar/pc"
ARCHIVOS = {"pe": "pedata", "pb": "pbvdata", "ev_ebitda": "vebitda", "ps": "psdata"}

SECTORES = ["Communication Services", "Consumer Discretionary", "Consumer Staples", "Energy", "Financials",
            "Health Care", "Industrials", "Information Technology", "Materials", "Real Estate", "Utilities"]

# Reglas en orden: la primera que coincide define el sector. Nombres de industria de Damodaran en minúscula.
SECTOR_REGLAS = [
    (r"total market", None),
    (r"r\.?e\.?i\.?t|real estate", "Real Estate"),
    (r"utility|power|green & renewable", "Utilities"),
    (r"bank|brokerage|financial svcs|financial serv|insurance|reinsurance|investments? & asset|asset manag|thrift", "Financials"),
    (r"oil|coal|oilfield", "Energy"),
    (r"advertising|broadcasting|cable tv|entertainment|publishing|newspaper|telecom\.? services|telecom \(wireless\)|"
     r"software \(internet\)|software \(entertainment\)", "Communication Services"),
    (r"semiconductor|computer|software|telecom\.? equipment|electronics|information tech", "Information Technology"),
    (r"drug|biotech|pharma|health ?care|heathcare|hospital|medical", "Health Care"),
    (r"beverage|food|tobacco|household products|farming|agricult|retail \(grocery|retail \(general|personal care", "Consumer Staples"),
    (r"retail \(distributors\)|building materials|information services", "Industrials"),
    (r"apparel|auto|furn|home furnish|homebuild|hotel|gaming|recreation|restaurant|retail|rubber|shoe|education|toy", "Consumer Discretionary"),
    (r"chemical|metals|mining|precious|steel|paper|forest|packaging|container|construction supplies", "Materials"),
    (r"aerospace|defense|air transport|business & consumer|diversified|electrical equip|engineering|environmental|"
     r"machinery|office equip|shipbuilding|transportation|trucking|railroad", "Industrials"),
]

# Columna a usar en cada archivo, en orden de preferencia (texto que debe contener el encabezado, en minúscula).
COLUMNAS = {
    "pe": ["trailing net income (only money making", "aggregate mkt cap/ net income", "current pe", "trailing pe"],
    "pe_fwd": ["forward pe"],
    "pb": ["pbv"],
    "ev_ebitda": ["ev/ebitda"],
    "ps": ["price/sales"],
}


def sector_de(industria: str):
    n = industria.lower().strip()
    for pat, sec in SECTOR_REGLAS:
        if re.search(pat, n):
            return sec
    return "?"


def leer_tabla(contenido: bytes) -> pd.DataFrame:
    """Busca en el Excel la hoja con 'Industry Name' y devuelve la tabla con sus encabezados."""
    hojas = pd.read_excel(io.BytesIO(contenido), sheet_name=None, header=None)
    for _, df in hojas.items():
        for i in range(min(len(df), 120)):
            fila = [re.sub(r"\s+", " ", str(x)).strip().lower() for x in df.iloc[i].tolist()]
            j = next((k for k, c in enumerate(fila) if re.match(r"^industry ?(name|group)?$", c)), None)
            if j is not None:
                cab = [str(x).strip() for x in df.iloc[i, j:].tolist()]
                t = df.iloc[i + 1:, j:].copy()
                t.columns = cab
                t = t[t.iloc[:, 0].notna()]
                t.iloc[:, 0] = t.iloc[:, 0].astype(str).str.strip()
                return t
    raise ValueError("no se encontró la fila 'Industry Name'")


def elegir_columna(t: pd.DataFrame, claves: list[str], excluir=("r&d",)):
    cols = [str(c) for c in t.columns]
    for k in claves:
        for j, c in enumerate(cols):
            cl = c.lower()
            if k in cl and not any(e in cl for e in excluir):
                return j, c
    return None, None


def bajar(nombre: str, año_dato: int, año_actual: int) -> bytes:
    """año_dato = enero del año. El archivo del año en curso está en /datasets; los anteriores en /archives
    con el sufijo del año anterior (pedata24.xls = enero de 2025)."""
    if año_dato == año_actual:
        return _get(f"{BASE}/datasets/{nombre}.xls", timeout=90, intentos=2).content
    return _get(f"{BASE}/archives/{nombre}{(año_dato - 1) % 100:02d}.xls", timeout=90, intentos=2).content


def pesos_mcap(año_dato: int, año_actual: int, avisos: list) -> dict[str, float]:
    """Capitalización por industria (DollarUS). Hay archivo desde enero de 2020; antes se usa el de 2020."""
    for a in (año_dato, max(año_dato, 2020)):
        try:
            t = leer_tabla(bajar("DollarUS", a, año_actual))
            j, c = elegir_columna(t, ["market cap"], excluir=())
            if j is None:
                raise ValueError("sin columna de capitalización")
            vals = pd.to_numeric(t.iloc[:, j], errors="coerce")
            return {ind: float(v) for ind, v in zip(t.iloc[:, 0], vals) if pd.notna(v) and v > 0}
        except Exception as e:
            avisos.append(f"DollarUS {a}: {e}")
    return {}


def main():
    hoy = now_ar()
    # Damodaran publica en la primera quincena de enero: hasta febrero se toma como último el enero anterior.
    año_actual = hoy.year if hoy.month >= 2 else hoy.year - 1
    previo = load_json(OUT, {})
    if previo.get("años") and previo["años"][-1] >= año_actual and "--forzar" not in sys.argv:
        print(f"damodaran.json ya tiene enero de {previo['años'][-1]}: no hace falta recalcular")
        return
    # Damodaran publica en la primera semana de enero; si todavía no está, el año en curso es el anterior.
    try:
        _get(f"{BASE}/datasets/pedata.xls", timeout=60, intentos=1)
    except Exception:
        print("No se pudo leer el archivo actual de Damodaran", file=sys.stderr)
        raise
    años = list(range(año_actual - 9, año_actual + 1))
    avisos: list[str] = []
    sin_sector: set[str] = set()
    por_año: dict[int, dict] = {}

    for año in años:
        pesos = pesos_mcap(año, año_actual, avisos)
        if not pesos:
            avisos.append(f"{año}: sin capitalización por industria; se pondera por cantidad de empresas")
        res: dict[str, dict] = {s: {} for s in SECTORES}
        for ratio, archivo in ARCHIVOS.items():
            try:
                t = leer_tabla(bajar(archivo, año, año_actual))
            except Exception as e:
                avisos.append(f"{archivo} {año}: {e}")
                continue
            claves = [ratio] + (["pe_fwd"] if ratio == "pe" else [])
            for clave in claves:
                j, col = elegir_columna(t, COLUMNAS[clave])
                if j is None:
                    avisos.append(f"{archivo} {año}: no se encontró la columna de {clave}")
                    continue
                jn, _ = elegir_columna(t, ["number of firms"], excluir=())
                acum = {s: [0.0, 0.0] for s in SECTORES}  # suma de pesos, suma de peso/valor
                for _, fila in t.iterrows():
                    ind = str(fila.iloc[0])
                    sec = sector_de(ind)
                    if sec is None:
                        continue
                    if sec == "?":
                        sin_sector.add(ind)
                        continue
                    v = pd.to_numeric(fila.iloc[j], errors="coerce")
                    if pd.isna(v) or v <= 0 or v > 500:
                        continue
                    w = pesos.get(ind)
                    if w is None:
                        w = pd.to_numeric(fila.iloc[jn], errors="coerce") if jn is not None else 1.0
                        w = float(w) if pd.notna(w) else 1.0
                    acum[sec][0] += w
                    acum[sec][1] += w / float(v)
                for s, (sw, swv) in acum.items():
                    if sw > 0 and swv > 0:
                        res[s][clave] = round(sw / swv, 2)  # promedio armónico ponderado = múltiplo agregado
        por_año[año] = res
        print(año, {s: res[s] for s in ("Information Technology", "Financials", "Energy")})

    sectores = {}
    for s in SECTORES:
        d = {}
        for clave in ("pe", "pe_fwd", "pb", "ev_ebitda", "ps"):
            serie = {a: por_año[a][s].get(clave) for a in años if por_año.get(a, {}).get(s, {}).get(clave)}
            if not serie:
                continue
            ult = max(serie)
            prom = {}
            for n in (3, 5, 10):
                vals = [v for a, v in serie.items() if a > ult - n]
                if len(vals) >= max(2, n - 1):
                    prom[str(n)] = rnd(sum(vals) / len(vals), 2)
            d[clave] = {"por_año": {str(a): v for a, v in sorted(serie.items())}, "ultimo_año": ult,
                        "ultimo": serie[ult], "prom": prom}
        sectores[s] = d

    save_json(OUT, {
        "_nota": "Múltiplos por sector armados con los datos de enero de cada año de Aswath Damodaran (NYU Stern), "
                 "todas las empresas de EE.UU. Industrias agrupadas en sectores GICS y combinadas por capitalización. "
                 "Los promedios son de 3, 5 y 10 eneros (no son promedios mensuales).",
        "actualizado": now_ar().strftime("%Y-%m-%d"), "años": años, "sectores": sectores,
        "industrias_sin_sector": sorted(sin_sector), "avisos": avisos,
        "fuente": "https://pages.stern.nyu.edu/~adamodar/New_Home_Page/dataarchived.html",
    })
    print("Industrias sin sector:", sorted(sin_sector))
    print("Avisos:", avisos)


if __name__ == "__main__":
    main()
