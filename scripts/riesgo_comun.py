"""
Funciones comunes del monitor de riesgo de mercado (update_riesgo.py y backtest_riesgo.py).

Cada indicador se convierte en un puntaje de 0 a 100 con "anclas": pares (valor, puntaje) que se
interpolan en forma lineal. En el eje de VALUACIÓN, 100 es muy caro; en el eje de SEÑALES,
100 es deterioro fuerte. Los umbrales son una propuesta inicial y se pueden ajustar acá.
"""
from __future__ import annotations

import datetime as dt
import io
import json
import math
import pathlib
import re
import time

import pandas as pd
import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIR = ROOT / "data" / "riesgo"
TZ_AR = dt.timezone(dt.timedelta(hours=-3))
HEADERS = {"User-Agent": "Mozilla/5.0 (Tablero de Mercados; monitor de riesgo)"}


# ----------------------------------------------------------------- utilidades
def now_ar() -> dt.datetime:
    return dt.datetime.now(TZ_AR)


def load_json(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def save_json(path: pathlib.Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1), encoding="utf-8")


def clean(x):
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def rnd(x, d=2):
    x = clean(x)
    return None if x is None else round(x, d)


def interp(x, anclas):
    """Interpolación lineal sobre anclas [(valor, puntaje), ...] ordenadas por valor."""
    x = clean(x)
    if x is None:
        return None
    if x <= anclas[0][0]:
        return float(anclas[0][1])
    if x >= anclas[-1][0]:
        return float(anclas[-1][1])
    for (x0, s0), (x1, s1) in zip(anclas, anclas[1:]):
        if x0 <= x <= x1:
            return s0 + (s1 - s0) * (x - x0) / (x1 - x0)
    return None


def percentil(serie: pd.Series, v) -> float | None:
    s = serie.dropna()
    v = clean(v)
    if v is None or len(s) < 20:
        return None
    return float(((s < v).mean() + 0.5 * (s == v).mean()) * 100)


def asof(serie: pd.Series | None, t: pd.Timestamp, max_dias: int | None = None):
    """Último (fecha, valor) de la serie con fecha <= t."""
    if serie is None or serie.empty:
        return None
    s = serie.loc[:t]
    if s.empty:
        return None
    f, v = s.index[-1], clean(s.iloc[-1])
    if v is None:
        return None
    if max_dias is not None and (t - f).days > max_dias:
        return None
    return f, v


def hasta(serie: pd.Series | None, t: pd.Timestamp) -> pd.Series:
    if serie is None:
        return pd.Series(dtype=float)
    return serie.loc[:t].dropna()


def a_serie(pts) -> pd.Series:
    """Lista de (fecha, valor) o dict fecha->valor a pd.Series ordenada."""
    if isinstance(pts, dict):
        pts = pts.items()
    d = {}
    for f, v in pts:
        v = clean(v)
        if v is None:
            continue
        try:
            d[pd.Timestamp(str(f)[:10])] = v
        except Exception:
            continue
    return pd.Series(d, dtype=float).sort_index()


def muestra(serie: pd.Series, desde: pd.Timestamp | None = None, n_max: int = 160, dec: int = 2):
    """Serie corta [[fecha, valor], ...] para los gráficos de la página."""
    s = serie.dropna()
    if desde is not None:
        s = s.loc[desde:]
    if s.empty:
        return []
    paso = max(1, math.ceil(len(s) / n_max))
    idx = list(range(len(s) - 1, -1, -paso))[::-1]
    return [[s.index[i].strftime("%Y-%m-%d"), rnd(s.iloc[i], dec)] for i in idx]


# ----------------------------------------------------------------- zonas y matriz
ZONAS_VAL = [(33, "Barato"), (67, "Justo"), (85, "Caro"), (101, "Muy caro")]
ZONAS_SEN = [(40, "Sano"), (60, "Atención"), (80, "Deterioro"), (101, "Deterioro fuerte")]


def zona(score, eje: str):
    if score is None:
        return {"zona": "Sin dato", "nivel": None}
    tabla = ZONAS_VAL if eje == "val" else ZONAS_SEN
    for i, (lim, nombre) in enumerate(tabla):
        if score < lim:
            return {"zona": nombre, "nivel": i}
    return {"zona": tabla[-1][1], "nivel": len(tabla) - 1}


def fila_val(s):
    return None if s is None else (0 if s < 33 else 1 if s < 67 else 2)


def col_sen(s):
    return None if s is None else (0 if s < 40 else 1 if s < 60 else 2)


FILAS = ["Barato", "Justo", "Caro"]
COLUMNAS = ["Señales sanas", "Atención", "Deterioro"]

# Liquidez objetivo (% de la cartera de acciones) para EE.UU. Propuesta inicial, a validar con el backtesting.
MATRIZ_EEUU = [
    [  # Barato
        {"titulo": "Invertido al máximo", "liq": [0, 5], "texto": "Valuación atractiva y mercado sano: compras normales o más grandes."},
        {"titulo": "Comprar de a poco", "liq": [5, 10], "texto": "Precio atractivo pero con señales mixtas: seguir comprando en tramos."},
        {"titulo": "Reingreso escalonado", "liq": [0, 10], "texto": "Precio bajo con el mercado golpeado: usar la liquidez por tramos según la caída desde máximos."},
    ],
    [  # Justo
        {"titulo": "Normal", "liq": [5, 10], "texto": "Sin alertas: mantener la estrategia y las compras periódicas."},
        {"titulo": "Prudente", "liq": [10, 15], "texto": "Señales mixtas: no acelerar compras y revisar posiciones débiles."},
        {"titulo": "Defensivo", "liq": [15, 25], "texto": "Mercado deteriorándose a precios razonables: subir algo la liquidez y esperar confirmación."},
    ],
    [  # Caro
        {"titulo": "No perseguir", "liq": [10, 15], "texto": "Caro pero sano: compras más chicas, no vender por valuación sola."},
        {"titulo": "Reducir de a poco", "liq": [15, 25], "texto": "Caro y con primeras señales de debilidad: recortar lo más caro en tramos."},
        {"titulo": "Subir liquidez por tramos", "liq": [25, 40], "texto": "Caro y con deterioro confirmado: es la combinación de mayor riesgo."},
    ],
]

MATRIZ_AR = [
    [
        {"titulo": "Exposición plena", "texto": "Activos argentinos baratos y macro estable: exposición completa dentro de tus topes."},
        {"titulo": "Sumar de a poco", "texto": "Baratos pero con señales mixtas: sumar en tramos, sin pasar los topes."},
        {"titulo": "Comprar con cautela", "texto": "Baratos y con la macro golpeada: oportunidad, pero de a poco y con stop de tesis."},
    ],
    [
        {"titulo": "Normal", "texto": "Sin alertas: mantener la exposición."},
        {"titulo": "Prudente", "texto": "Señales mixtas: no sumar y revisar las posiciones más cíclicas."},
        {"titulo": "Defensivo", "texto": "Precios razonables con deterioro macro: bajar exposición a lo más riesgoso."},
    ],
    [
        {"titulo": "No perseguir", "texto": "Caros pero con la macro sana: no sumar, dejar correr lo que ya está."},
        {"titulo": "Reducir de a poco", "texto": "Caros y con primeras señales de deterioro: tomar ganancia en tramos."},
        {"titulo": "Reducir exposición", "texto": "Caros y con deterioro: la combinación de mayor riesgo para Argentina."},
    ],
]


def lectura_matriz(val, sen, matriz):
    f, c = fila_val(val), col_sen(sen)
    if f is None or c is None:
        return None
    celda = dict(matriz[f][c])
    celda.update({"fila": f, "col": c, "fila_txt": FILAS[f], "col_txt": COLUMNAS[c]})
    return celda


def puntaje(inds: list[dict], pesos: dict) -> dict:
    """Promedio ponderado de los puntajes disponibles; si falta alguno, se reparten los pesos."""
    tot = sum(pesos.values())
    num = den = 0.0
    faltan = []
    for k, w in pesos.items():
        i = next((x for x in inds if x and x.get("id") == k), None)
        if i is None or i.get("score") is None:
            faltan.append(k)
            continue
        num += w * i["score"]
        den += w
    if den == 0:
        return {"score": None, "cobertura": 0, "faltan": faltan}
    return {"score": round(num / den, 1), "cobertura": round(den / tot * 100), "faltan": faltan}


# ----------------------------------------------------------------- fuentes
def _get(url, timeout=40, intentos=3, **kw):
    err = None
    for i in range(intentos):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout, **kw)
            r.raise_for_status()
            return r
        except Exception as e:
            err = e
            time.sleep(3 * (i + 1))
    raise err


def fred(serie_id: str, desde: str = "1990-01-01") -> pd.Series:
    """Serie diaria de FRED (Reserva Federal de St. Louis), descarga CSV pública sin clave."""
    r = _get(f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={serie_id}&cosd={desde}", timeout=90, intentos=2)
    df = pd.read_csv(io.StringIO(r.text))
    if df.shape[1] < 2:
        raise ValueError(f"FRED {serie_id}: formato inesperado")
    s = pd.Series(pd.to_numeric(df.iloc[:, 1], errors="coerce").values,
                  index=pd.to_datetime(df.iloc[:, 0], errors="coerce")).dropna()
    s = s[~s.index.isna()].sort_index()
    if s.empty:
        raise ValueError(f"FRED {serie_id}: sin datos")
    return s


def yahoo(tickers: list[str], start: str | None = None, period: str | None = None,
          auto_adjust: bool = False, campo: str = "Close") -> dict[str, pd.Series]:
    import yfinance as yf

    df = None
    err = None
    for i in range(3):
        try:
            df = yf.download(tickers, start=start, period=period, interval="1d", group_by="ticker",
                             auto_adjust=auto_adjust, progress=False, threads=True)
            if df is not None and not df.empty:
                break
        except Exception as e:
            err = e
        time.sleep(8 * (i + 1))
    if df is None or df.empty:
        raise RuntimeError(f"Yahoo no respondió ({err})")
    out = {}
    for t in tickers:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                s = df[t][campo]
            else:
                s = df[campo]
            s = s.dropna()
            idx = pd.to_datetime(s.index)
            if getattr(idx, "tz", None) is not None:
                idx = idx.tz_localize(None)
            s.index = idx.normalize()
            s = s[s.index.weekday < 5]
            s = s[~s.index.duplicated(keep="last")]
            if not s.empty:
                out[t] = s.astype(float)
        except Exception:
            continue
    return out


def cape_multpl() -> pd.Series:
    """CAPE (Shiller P/E) mensual desde 1871, tabla de multpl.com."""
    r = _get("https://www.multpl.com/shiller-pe/table/by-month")
    filas = re.findall(r"<td[^>]*>\s*([A-Z][a-z]{2} \d{1,2}, \d{4})\s*</td>\s*<td[^>]*>(.*?)</td>", r.text, re.S)
    d = {}
    for f, raw in filas:
        txt = re.sub(r"<[^>]+>|&[#\w]+;", " ", raw)
        m = re.search(r"\d+(?:\.\d+)?", txt)
        if not m:
            continue
        try:
            d[pd.Timestamp(dt.datetime.strptime(f, "%b %d, %Y"))] = float(m.group(0))
        except ValueError:
            continue
    s = pd.Series(d, dtype=float).sort_index()
    if len(s) < 500:
        raise ValueError(f"multpl: se leyeron solo {len(s)} meses")
    return s


def argdatos(path: str):
    return _get("https://api.argentinadatos.com/v1/" + path.lstrip("/")).json()


def _bcra_get(url):
    try:
        return _get(url, intentos=2)
    except requests.exceptions.SSLError:
        # La API del BCRA tuvo problemas de certificado en el pasado: solo para esta lectura pública.
        import urllib3
        urllib3.disable_warnings()
        return _get(url, intentos=2, verify=False)


def bcra_reservas(desde: str = "2018-01-01") -> pd.Series:
    """Reservas internacionales del BCRA (millones de US$), API de estadísticas del BCRA."""
    hoy = now_ar().strftime("%Y-%m-%d")
    urls = [
        f"https://api.bcra.gob.ar/estadisticas/v4.0/Monetarias/1?desde={desde}&hasta={hoy}&limit=3000",
        f"https://api.bcra.gob.ar/estadisticas/v3.0/monetarias/1?desde={desde}&hasta={hoy}&limit=3000",
    ]
    errores = []
    for url in urls:
        try:
            j = _bcra_get(url).json()
            pts = []

            def walk(o):
                if isinstance(o, list):
                    for x in o:
                        walk(x)
                elif isinstance(o, dict):
                    if "fecha" in o and "valor" in o:
                        pts.append((o["fecha"], o["valor"]))
                    for k in ("results", "detalle"):
                        if k in o:
                            walk(o[k])

            walk(j)
            s = a_serie(pts)
            if len(s) > 100:
                return s
            errores.append(f"{url.split('/estadisticas/')[1][:4]}: {len(s)} datos")
        except Exception as e:
            errores.append(f"{url.split('/estadisticas/')[1][:4]}: {e}")
    raise ValueError("; ".join(errores))


def bcra_itcrm() -> pd.Series:
    """Índice de Tipo de Cambio Real Multilateral del BCRA (base 17-12-15 = 100), planilla diaria."""
    import openpyxl

    r = _bcra_get("https://www.bcra.gob.ar/Pdfs/PublicacionesEstadisticas/ITCRMSerie.xlsx")
    wb = openpyxl.load_workbook(io.BytesIO(r.content), read_only=True, data_only=True)
    for ws in wb.worksheets:
        filas = list(ws.iter_rows(values_only=True))
        col = None
        for fila in filas[:40]:
            for j, c in enumerate(fila or ()):
                if isinstance(c, str) and c.strip().upper().startswith("ITCRM"):
                    col = j
                    break
            if col is not None:
                break
        if col is None:
            continue
        pts = []
        for fila in filas:
            if not fila or col >= len(fila):
                continue
            f = next((c for c in fila if isinstance(c, (dt.datetime, dt.date))), None)
            v = clean(fila[col])
            if f is not None and v is not None:
                pts.append((pd.Timestamp(f).strftime("%Y-%m-%d"), v))
        s = a_serie(pts)
        if len(s) > 500:
            return s
    raise ValueError("no se encontró la columna ITCRM en la planilla del BCRA")


def tesoro_real10(años: int = 4) -> pd.Series:
    """Tasa real a 10 años (TIPS) del Tesoro de EE.UU. (curva de rendimientos reales diaria)."""
    pts = []
    hoy = now_ar().year
    for y in range(hoy - años + 1, hoy + 1):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
               f"{y}/all?type=daily_treasury_real_yield_curve&field_tdr_date_value={y}&page&_format=csv")
        try:
            df = pd.read_csv(io.StringIO(_get(url, timeout=60, intentos=2).text))
            col = next(c for c in df.columns if c.strip().upper() in ("10 YR", "10YR", "10 YEAR"))
            for f, v in zip(df.iloc[:, 0], df[col]):
                pts.append((pd.Timestamp(f).strftime("%Y-%m-%d"), v))
        except Exception:
            continue
    s = a_serie(pts)
    if len(s) < 50:
        raise ValueError("el Tesoro no devolvió la curva real")
    return s


CAPE_FILE = DIR / "cape.json"


def cape_guardado() -> pd.Series:
    return a_serie(load_json(CAPE_FILE, {}).get("valores", {}))


def cape_con_respaldo(errores: list | None = None) -> pd.Series:
    """CAPE de multpl.com en vivo; si no responde, el archivo cape.json. Lo leído en vivo se suma al archivo."""
    base = cape_guardado()
    try:
        vivo = cape_multpl()
        datos = load_json(CAPE_FILE, {"valores": {}})
        vals = datos.setdefault("valores", {})
        for f, v in vivo.loc["2023-01-01":].items():
            vals[f.strftime("%Y-%m-%d")] = round(float(v), 2)
        datos["valores"] = dict(sorted(vals.items()))
        save_json(CAPE_FILE, datos)
        return pd.concat([base, vivo]).groupby(level=0).last().sort_index()
    except Exception as e:
        if errores is not None:
            errores.append(f"CAPE: multpl.com no respondió ({str(e)[:80]}); se usa cape.json")
        if base.empty:
            raise
        return base
