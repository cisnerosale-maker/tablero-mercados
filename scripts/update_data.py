#!/usr/bin/env python3
"""
Actualizador del Tablero de Mercados.

Junta cotizaciones de fuentes públicas y escribe data/data.json, que lee la página.
Cada indicador se procesa por separado: si una fuente falla, se conserva el último
dato bueno (marcado como "desactualizado") y el resto sigue funcionando.

Fuentes:
  - Yahoo Finance (vía yfinance): metales, energía, granos de Chicago, índices, monedas.
  - dolarapi.com: dólar oficial, MEP, CCL y mayorista al momento.
  - argentinadatos.com: historia de dólares, riesgo país, plazos fijos, inflación.
  - Bolsa de Comercio de Rosario (Cámara Arbitral): precios de pizarra.
  - data/manual.json: valores que cargás a mano (riesgo Brasil y Chile, Selic).
"""
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import re
import statistics
import time
import traceback

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
OUT_FILE = DATA_DIR / "data.json"
HIST_FILE = DATA_DIR / "historial.json"
MANUAL_FILE = DATA_DIR / "manual.json"

TZ_AR = dt.timezone(dt.timedelta(hours=-3))
HEADERS = {"User-Agent": "Mozilla/5.0 (Tablero de Mercados; actualizacion automatica)"}

# Factores para pasar granos de Chicago (centavos de dólar por bushel) a US$ por tonelada.
BUSHEL_TO_TN = {"soja": 36.7437, "trigo": 36.7437, "maiz": 39.3680}

# id: (ticker de Yahoo, nombre, unidad, decimales, factor de conversión)
YAHOO = {
    "cobre": ("HG=F", "Cobre", "US$/lb", 3, 1.0),
    "oro": ("GC=F", "Oro", "US$/oz", 1, 1.0),
    "plata": ("SI=F", "Plata", "US$/oz", 2, 1.0),
    "brent": ("BZ=F", "Petróleo Brent", "US$/barril", 2, 1.0),
    "wti": ("CL=F", "Petróleo WTI", "US$/barril", 2, 1.0),
    "soja": ("ZS=F", "Soja (Chicago)", "US$/tn", 1, BUSHEL_TO_TN["soja"] / 100),
    "maiz": ("ZC=F", "Maíz (Chicago)", "US$/tn", 1, BUSHEL_TO_TN["maiz"] / 100),
    "trigo": ("ZW=F", "Trigo (Chicago)", "US$/tn", 1, BUSHEL_TO_TN["trigo"] / 100),
    "spx": ("^GSPC", "S&P 500", "puntos", 2, 1.0),
    "nasdaq": ("^IXIC", "Nasdaq Composite", "puntos", 2, 1.0),
    "dji": ("^DJI", "Dow Jones", "puntos", 2, 1.0),
    "merval": ("^MERV", "Merval (histórico S&P)", "puntos", 0, 1.0),
    "bovespa": ("^BVSP", "Ibovespa", "puntos", 0, 1.0),
    "ipsa": ("ECH", "Chile (ETF ECH)", "US$", 2, 1.0),
    "vix": ("^VIX", "VIX", "puntos", 2, 1.0),
    "dxy": ("DX-Y.NYB", "Índice dólar (DXY)", "puntos", 2, 1.0),
    "ust10": ("^TNX", "Bono EE.UU. 10 años", "% anual", 2, 1.0),
    "usdbrl": ("BRL=X", "Real brasileño", "R$ por US$", 4, 1.0),
    "usdclp": ("CLP=X", "Peso chileno", "CLP por US$", 2, 1.0),
    "eurusd": ("EURUSD=X", "Euro", "US$ por €", 4, 1.0),
}


# ----------------------------------------------------------------- utilidades
def now_ar() -> dt.datetime:
    return dt.datetime.now(TZ_AR)


def get_json(url: str, timeout: int = 25):
    r = requests.get(url, headers=HEADERS, timeout=timeout)
    r.raise_for_status()
    return r.json()


def load_json(path: pathlib.Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def clean(x):
    """Número float válido o None."""
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(f) or math.isinf(f) else f


def series_stats(points: list[tuple[str, float]], decimals: int = 2) -> dict:
    """
    points: lista de (fecha 'YYYY-MM-DD', valor) ordenada por fecha.
    Devuelve último valor, variación diaria, medias móviles de 50 y 200 ruedas,
    promedio del año en curso, máximo y mínimo de 52 semanas y una serie corta para el gráfico.
    """
    pts = [(d, clean(v)) for d, v in points]
    pts = [(d, v) for d, v in pts if v is not None]
    if not pts:
        return {}
    vals = [v for _, v in pts]
    last_d, last = pts[-1]
    prev = vals[-2] if len(vals) >= 2 else None
    year = last_d[:4]
    ytd = [v for d, v in pts if d[:4] == year]
    last_year = vals[-252:]

    def rnd(x):
        return None if x is None else round(x, decimals)

    return {
        "valor": rnd(last),
        "anterior": rnd(prev),
        "var_pct": round((last / prev - 1) * 100, 2) if prev else None,
        "mm50": rnd(statistics.fmean(vals[-50:])) if len(vals) >= 50 else None,
        "mm200": rnd(statistics.fmean(vals[-200:])) if len(vals) >= 200 else None,
        "prom_anio": rnd(statistics.fmean(ytd)) if ytd else None,
        "max52": rnd(max(last_year)),
        "min52": rnd(min(last_year)),
        "fecha": last_d,
        "serie": [rnd(v) for v in vals[-120:]],
    }


def item(nombre: str, unidad: str, fuente: str, **extra) -> dict:
    return {"nombre": nombre, "unidad": unidad, "fuente": fuente, **extra}


# ----------------------------------------------------------------- Yahoo Finance
def fetch_yahoo(items: dict, errors: list):
    import yfinance as yf

    tickers = [t for t, *_ in YAHOO.values()]
    df = None
    for attempt in range(3):
        try:
            df = yf.download(
                tickers, period="14mo", interval="1d", group_by="ticker",
                auto_adjust=False, progress=False, threads=False,
            )
            if df is not None and not df.empty:
                break
        except Exception as e:  # rate limit u otro error transitorio
            errors.append(f"Yahoo intento {attempt + 1}: {e}")
        time.sleep(10 * (attempt + 1))
    if df is None or df.empty:
        errors.append("Yahoo Finance no respondió; se conservan los últimos datos.")
        return {}

    raw = {}
    for key, (ticker, nombre, unidad, dec, factor) in YAHOO.items():
        try:
            close = df[ticker]["Close"].dropna()
            # Se descartan las velas de sábado y domingo: futuros y monedas abren el domingo a la
            # noche y esa "vela" falsea la variación del lunes.
            pts = [(idx.strftime("%Y-%m-%d"), float(v) * factor) for idx, v in close.items() if idx.weekday() < 5]
            if not pts:
                raise ValueError("sin datos")
            raw[key] = pts
            st = series_stats(pts, dec)
            it = item(nombre, unidad, "Yahoo Finance (demora ~15 min)", **st)
            if key in BUSHEL_TO_TN:
                it["chicago_cents_bu"] = round(pts[-1][1] / factor, 2)
            items[key] = it
        except Exception as e:
            errors.append(f"{nombre} ({ticker}): {e}")
    return raw


# ----------------------------------------------------------------- Dólares (Argentina)
CASAS = {
    "oficial": ("oficial", "Dólar oficial (BNA)"),
    "mep": ("bolsa", "Dólar MEP"),
    "ccl": ("contadoconliqui", "Dólar CCL"),
    "mayorista": ("mayorista", "Dólar mayorista"),
}


def fetch_dolares(items: dict, errors: list) -> dict:
    """Devuelve {casa: {fecha: venta}} para usar en otros cálculos."""
    hist_by_casa: dict[str, dict[str, float]] = {}
    actual = {}
    try:
        for d in get_json("https://dolarapi.com/v1/dolares"):
            actual[d.get("casa")] = d
    except Exception as e:
        errors.append(f"dolarapi: {e}")

    for key, (casa, nombre) in CASAS.items():
        try:
            hist = get_json(f"https://api.argentinadatos.com/v1/cotizaciones/dolares/{casa}")
            by_date = {}
            for h in hist[-420:]:
                v = clean(h.get("venta"))
                if v:
                    by_date[h["fecha"][:10]] = v
            cur = actual.get(casa)
            if cur and clean(cur.get("venta")):
                fecha = (cur.get("fechaActualizacion") or now_ar().isoformat())[:10]
                by_date[fecha] = float(cur["venta"])
            hist_by_casa[key] = by_date
            pts = sorted(by_date.items())
            st = series_stats(pts, 2)
            it = item(nombre, "$ por US$", "dolarapi.com / argentinadatos.com", **st)
            if cur:
                it["compra"] = clean(cur.get("compra"))
                it["actualizado"] = cur.get("fechaActualizacion")
            items[key] = it
        except Exception as e:
            errors.append(f"{nombre}: {e}")

    # Brecha CCL vs oficial
    try:
        o, c = items["oficial"]["valor"], items["ccl"]["valor"]
        common = sorted(set(hist_by_casa["oficial"]) & set(hist_by_casa["ccl"]))
        pts = [(d, (hist_by_casa["ccl"][d] / hist_by_casa["oficial"][d] - 1) * 100) for d in common]
        st = series_stats(pts, 1)
        st["valor"] = round((c / o - 1) * 100, 1)
        items["brecha"] = item("Brecha CCL / oficial", "%", "Cálculo propio", **st)
    except Exception as e:
        errors.append(f"Brecha: {e}")
    return hist_by_casa


def fetch_brl_clp_en_pesos(items: dict, errors: list):
    """Real, peso chileno y euro en pesos argentinos, calculados con el dólar mayorista."""
    may = (items.get("mayorista") or {}).get("valor")
    if not may:
        errors.append("Monedas en pesos: falta el dólar mayorista")
        return
    for key in ("usdbrl", "usdclp", "eurusd"):
        v = (items.get(key) or {}).get("valor")
        if v:
            items[key]["en_pesos"] = round(may * v if key == "eurusd" else may / v, 4)


# ----------------------------------------------------------------- Rava Bursátil
def _fecha_iso(s: str):
    s = s.strip()
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"
    return None


def rava_cierres(url: str, lo: float, hi: float) -> dict:
    """
    Lee los cierres diarios que Rava publica en una página de perfil o de histórico.
    Devuelve {fecha ISO: valor}. Solo acepta valores dentro de [lo, hi] para descartar basura.
    """
    import html as htmlmod

    r = requests.get(url, headers=HEADERS, timeout=30)
    r.raise_for_status()
    page = htmlmod.unescape(r.text)
    out: dict[str, float] = {}
    # a) Objetos JSON embebidos en la página: {"fecha":"2026-10-26", ..., "cierre": 99227.3, ...}
    for obj in re.findall(r"\{[^{}]{0,800}\}", page):
        fm = re.search(r'"fecha"\s*:\s*"([^"]+)"', obj)
        if not fm:
            continue
        f = _fecha_iso(fm.group(1))
        vm = re.search(r'"(?:cierre|ultimo|valor)"\s*:\s*"?(-?[\d.,]+)"?', obj)
        if not f or not vm:
            continue
        txt = vm.group(1)
        v = clean(txt) if txt.count(",") == 0 else parse_numero_ar(txt)
        if v is not None and lo <= v <= hi:
            out[f] = v
    # b) Si no hubo JSON, tabla visible: "26/10/2026 ... 99.227,30"
    if not out:
        from bs4 import BeautifulSoup
        text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        for f, num in re.findall(r"(\d{2}/\d{2}/\d{4})\s+([\d.]+,\d+|\d[\d.]*)", text):
            v = parse_numero_ar(num)
            fi = _fecha_iso(f)
            if fi and v is not None and lo <= v <= hi:
                out.setdefault(fi, v)
    if not out:
        raise ValueError(f"no se encontraron cierres en {url} (HTTP {r.status_code}, {len(r.text)} bytes)")
    return out


# ----------------------------------------------------------------- Riesgo, tasas, inflación
def fetch_riesgo_pais(items: dict, errors: list, history: dict):
    """
    argentinadatos.com da la historia, pero publica el dato con un día de atraso.
    El cierre del día se toma de Rava y, si falla, de Ámbito. Se usa siempre la fecha más nueva.
    """
    serie: dict[str, float] = dict(history.get("riesgo_ar", {}))
    fuentes = []
    try:
        hist = get_json("https://api.argentinadatos.com/v1/finanzas/indices/riesgo-pais")
        for h in hist[-600:]:
            serie[h["fecha"][:10]] = float(h["valor"])
        fuentes.append("argentinadatos")
    except Exception as e:
        errors.append(f"Riesgo país (argentinadatos): {e}")
    nuevos = {}
    try:
        nuevos = rava_cierres("https://www.rava.com/cotizaciones/historico/riesgo-pais/hoy/", 100, 5000)
        fuentes.append("Rava")
    except Exception as e:
        errors.append(f"Riesgo país (Rava): {e}")
    if not nuevos:
        try:
            a = get_json("https://mercados.ambito.com//riesgopais/variacion")
            v = parse_numero_ar(str(a.get("ultimo") or a.get("valor") or ""))
            f = _fecha_iso(str(a.get("fecha") or "").replace("-", "/")) or now_ar().strftime("%Y-%m-%d")
            if v and 100 <= v <= 5000:
                nuevos = {f: v}
                fuentes.append("Ámbito")
        except Exception as e:
            errors.append(f"Riesgo país (Ámbito): {e}")
    # Control de cordura: el dato nuevo no puede alejarse más de 35% del último conocido.
    if serie and nuevos:
        ref_f, ref_v = sorted(serie.items())[-1]
        malos = {f: v for f, v in nuevos.items() if not (0.65 * ref_v <= v <= 1.35 * ref_v)}
        if malos:
            errors.append(f"Riesgo país: descartados valores fuera de rango {malos} (último conocido {ref_v} del {ref_f})")
        nuevos = {f: v for f, v in nuevos.items() if f not in malos}
    serie.update(nuevos)
    for f, v in nuevos.items():
        history.setdefault("riesgo_ar", {})[f] = v
    if not serie:
        raise ValueError("ninguna fuente respondió")
    pts = sorted(serie.items())
    items["riesgo_ar"] = item("Riesgo país Argentina", "puntos básicos",
                              "EMBI JP Morgan vía " + " + ".join(fuentes), **series_stats(pts, 0))


def fetch_plazo_fijo(items: dict, errors: list, history: dict):
    try:
        bancos = get_json("https://api.argentinadatos.com/v1/finanzas/tasas/plazoFijo")
        bna = next(b for b in bancos if "NACION" in (b.get("entidad") or "").upper()
                   and "ARGENTINA" in (b.get("entidad") or "").upper())
        tna = clean(bna.get("tnaClientes"))
        if tna is None:
            raise ValueError("sin TNA")
        tna = tna * 100 if tna < 1 else tna
        hoy = now_ar().strftime("%Y-%m-%d")
        h = history.setdefault("pf_bna", {})
        h[hoy] = round(tna, 2)
        st = series_stats(sorted(h.items()), 2)
        st["tem"] = round(tna / 12, 2)
        st["tea"] = round(((1 + tna / 100 * 30 / 365) ** (365 / 30) - 1) * 100, 2)
        items["pf_bna"] = item("Plazo fijo BNA 30 días", "% TNA", "argentinadatos.com (BCRA)", **st)
    except Exception as e:
        errors.append(f"Plazo fijo BNA: {e}")


def fetch_inflacion(items: dict, errors: list):
    try:
        m = get_json("https://api.argentinadatos.com/v1/finanzas/indices/inflacion")
        pts = sorted({x["fecha"][:10]: x["valor"] for x in m}.items())
        st = series_stats(pts, 1)
        # Interanual compuesta de los últimos 12 meses
        last12 = [v for _, v in pts[-12:]]
        if len(last12) == 12:
            acc = 1.0
            for v in last12:
                acc *= 1 + v / 100
            st["interanual"] = round((acc - 1) * 100, 1)
        st.pop("mm50", None); st.pop("mm200", None)
        items["inflacion"] = item("Inflación mensual (INDEC)", "% mensual", "argentinadatos.com (INDEC)", **st)
    except Exception as e:
        errors.append(f"Inflación: {e}")


# ----------------------------------------------------------------- Merval (MSCI desde el 19/10/2026)
MERVAL_EMPALME = ("2026-10-16", 100000.0)  # el MSCI Merval arrancó en 100.000 puntos al cierre del 16/10/2026


def fetch_merval(items: dict, raw_yahoo: dict, errors: list, history: dict):
    """
    Yahoo dejó de actualizar el ^MERV cuando S&P dejó el índice (16/10/2026).
    Se arma una sola serie: la histórica de Yahoo reescalada a la base nueva, más los cierres
    del MSCI Merval que publica Rava. Así los promedios y el gráfico siguen siendo comparables.
    """
    fecha_emp, base = MERVAL_EMPALME
    viejos = dict(raw_yahoo.get("merval", []))
    ancla = viejos.get(fecha_emp)
    serie: dict[str, float] = {}
    if ancla:
        k = base / ancla
        serie = {d: v * k for d, v in viejos.items() if d <= fecha_emp}
    serie.update({d: v for d, v in history.get("merval_msci", {}).items()})
    try:
        nuevos = rava_cierres("https://www.rava.com/perfil/MERVAL", 50000, 250000)
        nuevos = {d: v for d, v in nuevos.items() if d > fecha_emp}
        serie.update(nuevos)
        history.setdefault("merval_msci", {}).update(nuevos)
    except Exception as e:
        errors.append(f"Merval (Rava): {e}")
    if not serie:
        items.pop("merval", None)
        raise ValueError("sin datos del Merval")
    pts = sorted(serie.items())
    raw_yahoo["merval"] = pts
    items["merval"] = item("Merval", "puntos (MSCI, base 100.000)", "Rava (MSCI Merval) + historia S&P reescalada",
                           **series_stats(pts, 0))


# ----------------------------------------------------------------- Merval en dólares
def merval_usd(items: dict, raw_yahoo: dict, dolares: dict, errors: list):
    try:
        ccl = dolares.get("ccl") or {}
        pts = []
        for d, v in raw_yahoo.get("merval", []):
            c = ccl.get(d)
            if c:
                pts.append((d, v / c))
        if not pts:
            raise ValueError("faltan datos de Merval o CCL")
        st = series_stats(pts, 0)
        # Si el Merval es de hoy, se usa el CCL del momento; si no, el CCL de ese mismo día.
        m = items.get("merval", {})
        if m.get("valor") and m.get("fecha") == items.get("ccl", {}).get("fecha") and items["ccl"].get("valor"):
            st["valor"] = round(m["valor"] / items["ccl"]["valor"], 0)
        items["merval_usd"] = item("Merval en dólares", "puntos US$ (CCL)", "Cálculo propio: Merval / CCL", **st)
    except Exception as e:
        errors.append(f"Merval USD: {e}")


# ----------------------------------------------------------------- Pizarra Rosario
GRANOS_ROSARIO = {"soja": "Soja", "maiz": "Ma[ií]z", "trigo": "Trigo"}


def parse_numero_ar(s: str):
    s = s.strip().replace("$", "").replace(" ", "")
    if not s or "S/C" in s.upper():
        return None
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    else:
        s = s.replace(".", "")
    return clean(s)


def fetch_rosario(items: dict, errors: list, history: dict):
    try:
        from bs4 import BeautifulSoup

        r = requests.get("https://www.cac.bcr.com.ar/es/precios-de-pizarra", headers=HEADERS, timeout=30)
        r.raise_for_status()
        text = BeautifulSoup(r.text, "html.parser").get_text(" ", strip=True)
        fecha_m = re.search(r"(\d{2})/(\d{2})/(\d{4})", text)
        fecha = f"{fecha_m.group(3)}-{fecha_m.group(2)}-{fecha_m.group(1)}" if fecha_m else now_ar().strftime("%Y-%m-%d")
        mayorista = (items.get("mayorista") or {}).get("valor")
        encontrados = 0
        for key, patron in GRANOS_ROSARIO.items():
            m = re.search(patron + r"\s*(?:\(.*?\))?\s*\$?\s*([\d\.]+(?:,\d{1,2})?|S/C)", text, re.I)
            precio = parse_numero_ar(m.group(1)) if m else None
            if precio is None:
                continue
            encontrados += 1
            h = history.setdefault(f"rosario_{key}", {})
            h[fecha] = precio
            st = series_stats(sorted(h.items()), 0)
            ros = {"ars_tn": st.get("valor"), "var_pct": st.get("var_pct"), "fecha": fecha,
                   "prom_anio": st.get("prom_anio"), "usd_tn": round(precio / mayorista, 1) if mayorista else None}
            items.setdefault(key, item(key, "US$/tn", "—"))["rosario"] = ros
        if not encontrados:
            raise ValueError("no se encontraron precios en la página (¿cambió el formato o no hubo pizarra?)")
    except Exception as e:
        errors.append(f"Pizarra Rosario: {e}")


# ----------------------------------------------------------------- Manuales
def apply_manual(items: dict, errors: list):
    manual = load_json(MANUAL_FILE, {})
    for key, nombre, unidad in (("riesgo_br", "Riesgo país Brasil", "puntos básicos"),
                                ("riesgo_cl", "Riesgo país Chile", "puntos básicos"),
                                ("selic", "Tasa Selic (Brasil)", "% anual")):
        m = manual.get(key) or {}
        items[key] = item(nombre, unidad, "Carga manual (data/manual.json)",
                          valor=clean(m.get("valor")), fecha=m.get("fecha"),
                          nota=m.get("nota"), manual=True)


# ----------------------------------------------------------------- main
def main():
    DATA_DIR.mkdir(exist_ok=True)
    previous = load_json(OUT_FILE, {}).get("items", {})
    history = load_json(HIST_FILE, {})
    items: dict = {}
    errors: list = []

    raw_yahoo = {}
    try:
        raw_yahoo = fetch_yahoo(items, errors) or {}
    except Exception:
        errors.append("Yahoo: " + traceback.format_exc(limit=1))
    dolares = {}
    try:
        dolares = fetch_dolares(items, errors) or {}
    except Exception:
        errors.append("Dólares: " + traceback.format_exc(limit=1))
    for fn in (fetch_brl_clp_en_pesos, fetch_inflacion):
        try:
            fn(items, errors)
        except Exception:
            errors.append(f"{fn.__name__}: " + traceback.format_exc(limit=1))
    for fn in (fetch_plazo_fijo, fetch_rosario, fetch_riesgo_pais):
        try:
            fn(items, errors, history)
        except Exception:
            errors.append(f"{fn.__name__}: " + traceback.format_exc(limit=1))
    try:
        fetch_merval(items, raw_yahoo, errors, history)
    except Exception as e:
        errors.append(f"Merval: {e}")
    merval_usd(items, raw_yahoo, dolares, errors)
    apply_manual(items, errors)

    # Lo que falló esta vez: se conserva el último dato bueno, marcado como desactualizado.
    for key, old in previous.items():
        if key not in items:
            old = dict(old)
            old["desactualizado"] = True
            items[key] = old
        elif "rosario" in old and "rosario" not in items[key]:
            items[key]["rosario"] = dict(old["rosario"], desactualizado=True)

    out = {
        "actualizado": now_ar().isoformat(timespec="minutes"),
        "items": items,
        "errores": errors,
    }
    OUT_FILE.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
    HIST_FILE.write_text(json.dumps(history, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"OK: {len(items)} indicadores, {len(errors)} avisos")
    for e in errors:
        print(" -", e)


if __name__ == "__main__":
    main()
