"""Página Cartera: cotizaciones, rendimientos, ratios, beta y bonos de los activos de la cartera.

Escribe data/cartera/datos.json. NO maneja cantidades: esas están cifradas en
data/cartera/cartera.enc.json y solo las abre la página con la contraseña.

Fuentes (todas públicas, sin suscripción):
  - data912.com: cotizaciones BYMA (acciones, CEDEARs, bonos, ONs) e historia.
  - Yahoo Finance (yfinance): acciones de EE.UU., ADRs, estados contables, estimaciones, beta.
  - argentinadatos.com: valor cuotaparte de los FCI e historia del dólar MEP.
  - dolarapi.com: dólar MEP y CCL del momento.
  - data/cartera/flujos.json: flujos de bonos y ONs (para TIR, duration y paridad).
  - data/cartera/control.json: valores de control de TIKR (opcional, se cargan a mano).
  - data/cartera/manual.json: datos locales de TGNO4 si Yahoo no los tiene.

Corridas:
  - Cada 30 minutos: precios y rendimientos.
  - Una vez por día (o si falta algún dato): ratios, beta, dividendos y fecha de balance.
    Se guardan en data/cartera/fundamentos.json y se reusan en las corridas de precios.
"""
from __future__ import annotations

import datetime as dt
import json
import math
import pathlib
import sys
import time
import traceback

import requests

ROOT = pathlib.Path(__file__).resolve().parent.parent
DIR = ROOT / "data" / "cartera"
ACTIVOS = DIR / "activos.json"
FLUJOS = DIR / "flujos.json"
FUND = DIR / "fundamentos.json"
HIST = DIR / "historial.json"
CONTROL = DIR / "control.json"
MANUAL = DIR / "manual.json"
OUT = DIR / "datos.json"
FACTSET = ROOT / "data" / "riesgo" / "factset.json"
DATA_TABLERO = ROOT / "data" / "data.json"

HEADERS = {"User-Agent": "Mozilla/5.0 (Tablero de Mercados; cartera)"}
TZ = dt.timezone(dt.timedelta(hours=-3))
HORAS_FUND = 20  # cada cuántas horas se recalculan ratios y beta


# ---------------------------------------------------------------- utilidades
def ahora() -> dt.datetime:
    return dt.datetime.now(TZ)


def hoy() -> dt.date:
    return ahora().date()


def num(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return None if math.isnan(v) or math.isinf(v) else v


def rnd(x, n=4):
    v = num(x)
    return None if v is None else round(v, n)


def get_json(url, timeout=40, intentos=2):
    ult = None
    for i in range(intentos):
        try:
            r = requests.get(url, headers=HEADERS, timeout=timeout)
            r.raise_for_status()
            return r.json()
        except Exception as e:  # noqa: BLE001
            ult = e
            time.sleep(2 + 3 * i)
    raise ult


def load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return default


def save(path, obj):
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


def norm(s: str) -> str:
    import unicodedata
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return " ".join(s.replace("-", " ").replace(".", " ").split())


# ---------------------------------------------------------------- fechas base
def fechas_base(d: dt.date) -> dict:
    """Fechas contra las que se mide: día anterior (lo da cada fuente), 30 días y año."""
    return {"d30": (d - dt.timedelta(days=30)).isoformat(), "anio": f"{d.year - 1}-12-31"}


def cierre_en(serie: list[tuple[str, float]], fecha: str):
    """Último cierre con fecha <= fecha. serie ordenada ascendente."""
    best = None
    for f, v in serie:
        if f <= fecha:
            best = (f, v)
        else:
            break
    return best


# ---------------------------------------------------------------- dólar
def dolares(avisos):
    mep = ccl = None
    fuente = "dolarapi.com"
    try:
        for x in get_json("https://dolarapi.com/v1/dolares"):
            if x.get("casa") == "bolsa":
                mep = num(x.get("venta"))
            if x.get("casa") == "contadoconliqui":
                ccl = num(x.get("venta"))
    except Exception as e:  # noqa: BLE001
        avisos.append(f"Dólar (dolarapi): {e}")
    if not mep or not ccl:
        it = load(DATA_TABLERO, {}).get("items", {})
        mep = mep or num(it.get("mep", {}).get("valor"))
        ccl = ccl or num(it.get("ccl", {}).get("valor"))
        fuente = "Tablero (último dato)"
    serie = []
    try:
        for x in get_json("https://api.argentinadatos.com/v1/cotizaciones/dolares/bolsa"):
            v = num(x.get("venta"))
            if v:
                serie.append((x["fecha"][:10].replace("/", "-"), v))
        serie.sort()
    except Exception as e:  # noqa: BLE001
        avisos.append(f"Historia MEP (argentinadatos): {e}")
    return mep, ccl, serie, fuente


# ---------------------------------------------------------------- BYMA (data912)
def paneles(avisos):
    out = {}
    for p in ("arg_stocks", "arg_cedears", "arg_bonds", "arg_corp", "arg_notes"):
        try:
            for x in get_json(f"https://data912.com/live/{p}"):
                s = x.get("symbol")
                if s:
                    out[(p, s)] = x
                    out.setdefault(("*", s), x)
        except Exception as e:  # noqa: BLE001
            avisos.append(f"data912 {p}: {e}")
    return out


def precio_panel(x):
    c = num(x.get("c"))
    if not c:
        bid, ask = num(x.get("px_bid")), num(x.get("px_ask"))
        c = (bid + ask) / 2 if bid and ask else None
    pct = num(x.get("pct_change"))
    ant = c / (1 + pct / 100) if c and pct is not None and pct > -100 else None
    return c, ant, pct


def historia_data912(tipo, sym, avisos, diag):
    """Lista [(fecha, cierre)] ascendente. tipo: stocks | cedears | bonds."""
    try:
        data = get_json(f"https://data912.com/historical/{tipo}/{sym}", timeout=40, intentos=1)
    except Exception as e:  # noqa: BLE001
        return []
    if isinstance(data, dict):
        data = data.get("data") or data.get("values") or []
    if data and "formato_historia" not in diag:
        diag["formato_historia"] = {k: str(v)[:20] for k, v in data[-1].items()} if isinstance(data[-1], dict) else str(data[-1])[:80]
    serie = []
    for x in data or []:
        if not isinstance(x, dict):
            continue
        f = x.get("date") or x.get("fecha") or x.get("d") or x.get("t")
        c = num(x.get("c", x.get("close", x.get("cierre"))))
        if f and c:
            serie.append((str(f)[:10], c))
    serie.sort()
    return serie


# ---------------------------------------------------------------- FCI
FCI_TIPOS = ["mercadoDinero", "rentaFija", "rentaMixta", "rentaVariable", "otros"]


def fci_lista(fecha: str | None, avisos):
    """Todos los FCI de una fecha ('ultimo', 'penultimo' o YYYY/MM/DD)."""
    out = []
    for t in FCI_TIPOS:
        try:
            for x in get_json(f"https://api.argentinadatos.com/v1/finanzas/fci/{t}/{fecha}", timeout=40, intentos=1):
                out.append(x)
        except Exception:  # noqa: BLE001
            pass
    return out


def fci_buscar(lista, busca, nombre):
    """Busca el fondo por nombre. Devuelve (vcp, fecha, nombre_fuente)."""
    clase = None
    if " - clase " in nombre.lower():
        clase = nombre.lower().split(" - clase ")[1].strip()[:1]
    cands = []
    for x in lista:
        n = norm(x.get("fondo", ""))
        if any(norm(b) in n for b in busca):
            cands.append(x)
    if not cands:
        return None
    if clase:
        c2 = [x for x in cands if norm(x.get("fondo", "")).endswith(f"clase {clase}") or f"clase {clase} " in norm(x.get("fondo", "")) + " "]
        cands = c2 or cands
    cands.sort(key=lambda x: len(x.get("fondo", "")))
    x = cands[0]
    return num(x.get("vcp")), (x.get("fecha") or "")[:10], x.get("fondo")


def fci_en_fecha(fecha_iso: str, avisos, cache):
    """FCI del último día con datos <= fecha (hasta 7 días atrás)."""
    d = dt.date.fromisoformat(fecha_iso)
    for i in range(8):
        f = (d - dt.timedelta(days=i))
        if f.weekday() >= 5:
            continue
        k = f.strftime("%Y/%m/%d")
        if k not in cache:
            cache[k] = fci_lista(k, avisos)
        if cache[k]:
            return cache[k], f.isoformat()
    return [], None


# ---------------------------------------------------------------- bonos
def tir_bono(flujos, precio, settle: dt.date):
    fl = [x for x in flujos if dt.date.fromisoformat(x["fecha"]) > settle]
    if not fl or not precio:
        return None
    def npv(r):
        return sum((x["int"] + x["amort"]) / (1 + r) ** ((dt.date.fromisoformat(x["fecha"]) - settle).days / 365) for x in fl) - precio
    lo, hi = -0.9, 5.0
    if npv(lo) * npv(hi) > 0:
        return None
    for _ in range(200):
        m = (lo + hi) / 2
        if npv(m) > 0:
            lo = m
        else:
            hi = m
    r = (lo + hi) / 2
    pv = [((dt.date.fromisoformat(x["fecha"]) - settle).days / 365, (x["int"] + x["amort"]) / (1 + r) ** ((dt.date.fromisoformat(x["fecha"]) - settle).days / 365)) for x in fl]
    tot = sum(v for _, v in pv)
    mac = sum(t * v for t, v in pv) / tot
    residual = sum(x["amort"] for x in fl)
    prim = dt.date.fromisoformat(fl[0]["fecha"])
    m_ant = prim.month - 6
    prev = prim.replace(year=prim.year + (m_ant - 1) // 12, month=(m_ant - 1) % 12 + 1)
    tasa_per = fl[0]["int"] / residual if residual else 0  # tasa del período (semestral)
    corrido = residual * tasa_per * max(0, (settle - prev).days) / max(1, (prim - prev).days)
    vt = residual + corrido
    return {
        "tir": round(r * 100, 2), "dur_mod": round(mac / (1 + r), 2), "dur_mac": round(mac, 2),
        "paridad": round(precio / vt * 100, 1) if vt else None, "residual": round(residual, 2),
        "prox_pago": fl[0]["fecha"], "prox_monto": round(fl[0]["int"] + fl[0]["amort"], 4),
        "vencimiento": fl[-1]["fecha"],
    }


# ---------------------------------------------------------------- Yahoo
def yahoo_historia(tickers, avisos, start):
    import yfinance as yf
    out = {}
    if not tickers:
        return out
    try:
        df = yf.download(sorted(set(tickers)), start=start, interval="1d", auto_adjust=False, progress=False, threads=True, group_by="column")
        cl = df["Close"]
        for t in set(tickers):
            if t not in cl:
                continue
            s = cl[t].dropna()
            out[t] = [(i.date().isoformat(), float(v)) for i, v in s.items()]
    except Exception as e:  # noqa: BLE001
        avisos.append(f"Yahoo historia: {e}")
    return out


def fx_rate(de, a, ccl, avisos):
    """Cuántas unidades de 'a' vale 1 de 'de'."""
    import yfinance as yf
    if de == a:
        return 1.0
    if de == "USD" and a == "ARS":
        return ccl
    if de == "ARS" and a == "USD":
        return 1 / ccl
    for sym, inv in ((f"{de}{a}=X", False), (f"{a}{de}=X", True)):
        try:
            h = yf.Ticker(sym).history(period="10d")["Close"].dropna()
            if len(h):
                v = float(h.iloc[-1])
                return 1 / v if inv else v
        except Exception:  # noqa: BLE001
            pass
    avisos.append(f"Tipo de cambio {de}/{a} no disponible")
    return None


def fila(df, nombres, n=4):
    """Suma de las últimas n columnas de la primera fila encontrada, y cuántas había."""
    if df is None or getattr(df, "empty", True):
        return None, 0
    for nm in nombres:
        if nm in df.index:
            s = df.loc[nm]
            vals = [num(v) for v in list(s.values)[:n]]
            vals = [v for v in vals if v is not None]
            if vals:
                return sum(vals), len(vals)
    return None, 0


def ultimo(df, nombres):
    if df is None or getattr(df, "empty", True):
        return None
    for nm in nombres:
        if nm in df.index:
            for v in df.loc[nm].values:
                v = num(v)
                if v is not None:
                    return v
    return None


def fundamentos_de(acc_fund: str, meta: dict, ccl: float, local_price, avisos):
    """Ratios de una empresa (símbolo de Yahoo). meta: adr_ratio, financiera, fund_local."""
    import yfinance as yf
    tk = yf.Ticker(acc_fund)
    info = tk.info or {}
    r = {"fuente": "Yahoo Finance", "fecha": hoy().isoformat(), "avisos": []}
    cur = info.get("currency") or "USD"
    fin = info.get("financialCurrency") or cur
    precio = num(info.get("regularMarketPrice")) or num(info.get("currentPrice")) or num(info.get("previousClose"))
    r.update(moneda_cot=cur, moneda_balance=fin, precio=precio)

    # Ganancia, EBITDA y balance (últimos 4 trimestres; si no hay, último ejercicio)
    q = a = qb = ab = None
    try:
        q = tk.quarterly_income_stmt
        a = tk.income_stmt
        qb = tk.quarterly_balance_sheet
        ab = tk.balance_sheet
    except Exception as e:  # noqa: BLE001
        r["avisos"].append(f"estados contables: {e}")
    ni_names = ["Net Income Common Stockholders", "Net Income", "Net Income Including Noncontrolling Interests"]
    ebitda_names = ["EBITDA", "Normalized EBITDA"]
    ni, nq = fila(q, ni_names)
    eb, ne = fila(q, ebitda_names)
    base = "LTM (4 trimestres)"
    if nq < 4:
        ni2, _ = fila(a, ni_names, 1)
        if ni2 is not None:
            ni, base = ni2, "último ejercicio (Yahoo no tiene 4 trimestres)"
    if ne < 4:
        eb2, _ = fila(a, ebitda_names, 1)
        eb = eb2 if eb2 is not None else None
    bs = qb if qb is not None and not getattr(qb, "empty", True) else ab
    deuda = ultimo(bs, ["Total Debt"]) or 0
    caja = ultimo(bs, ["Cash Cash Equivalents And Short Term Investments", "Cash And Cash Equivalents"]) or 0
    minor = ultimo(bs, ["Minority Interest"]) or 0
    pn = ultimo(bs, ["Common Stock Equity", "Stockholders Equity"])
    acc_ord = ultimo(bs, ["Ordinary Shares Number", "Share Issued"])
    r.update(base_ltm=base)

    # Capitalización en la moneda del balance
    mcap_y = num(info.get("marketCap"))
    fx = fx_rate(cur, fin, ccl, r["avisos"])
    mcap = None
    if meta.get("fund_local") and local_price and acc_ord:
        mcap = local_price * acc_ord  # TGNO4: pesos, precio BYMA
        r["mcap_metodo"] = "precio BYMA × acciones"
    elif meta.get("adr_ratio") and precio and acc_ord and fx:
        mcap = precio * acc_ord / meta["adr_ratio"] * fx
        r["mcap_metodo"] = f"precio ADR × acciones/{meta['adr_ratio']} × {'CCL' if fin == 'ARS' else 'tipo de cambio'}"
        if mcap_y and fx:
            dif = mcap / (mcap_y * fx) - 1
            if abs(dif) > 0.15:
                r["avisos"].append(f"capitalización propia vs Yahoo difiere {dif:+.0%}")
        if local_price and fin == "ARS":
            mloc = local_price * acc_ord
            dif = mcap / mloc - 1
            r["dif_local_vs_adr"] = round(dif * 100, 1)
    elif mcap_y and fx:
        mcap = mcap_y * fx
        r["mcap_metodo"] = "Yahoo"
    r["mcap_balance"] = mcap

    if mcap and ni:
        r["pe_ltm"] = round(mcap / ni, 2) if ni > 0 else None
        if ni <= 0:
            r["avisos"].append("ganancia LTM negativa: sin P/E")
    if mcap and eb and not meta.get("financiera"):
        ev = mcap + deuda - caja + minor
        r["ev_ebitda"] = round(ev / eb, 2) if eb > 0 else None
    if mcap and pn and pn > 0:
        r["pvl"] = round(mcap / pn, 2)
        if ni is not None:
            r["roe"] = round(ni / pn * 100, 1)

    # Control con los ratios que publica Yahoo
    r["yahoo_pe_ltm"] = rnd(info.get("trailingPE"), 2)
    r["yahoo_ev_ebitda"] = rnd(info.get("enterpriseToEbitda"), 2)

    # P/E NTM: promedio ponderado del consenso del ejercicio actual y el siguiente
    try:
        est = tk.earnings_estimate
        e0 = num(est.loc["0y", "avg"]) if est is not None and "0y" in est.index else None
        e1 = num(est.loc["+1y", "avg"]) if est is not None and "+1y" in est.index else None
        fy = info.get("nextFiscalYearEnd") or info.get("lastFiscalYearEnd")
        w = None
        if fy:
            fy_d = dt.datetime.fromtimestamp(int(fy), dt.timezone.utc).date()
            while fy_d < hoy():
                fy_d = fy_d.replace(year=fy_d.year + 1)
            w = max(0.0, min(1.0, (fy_d - hoy()).days / 365))
        if e0 is not None and e1 is not None and w is not None:
            eps_ntm = w * e0 + (1 - w) * e1
        else:
            eps_ntm = e1 or num(info.get("forwardEps"))
        r["eps_ntm"] = rnd(eps_ntm, 4)
        te = num(info.get("trailingEps"))
        pe_a = precio / eps_ntm if precio and eps_ntm and eps_ntm > 0 else None
        pe_b = r.get("pe_ltm") * te / eps_ntm if r.get("pe_ltm") and te and te > 0 and eps_ntm and eps_ntm > 0 else None
        pe_l = r.get("pe_ltm")
        ok = lambda v: v is not None and pe_l and 0.3 <= v / pe_l <= 2.2  # noqa: E731
        if pe_a is not None and (pe_l is None or ok(pe_a)):
            r["pe_ntm"] = round(pe_a, 2)
        elif ok(pe_b):
            r["pe_ntm"] = round(pe_b, 2)
            r["avisos"].append("P/E NTM por crecimiento del consenso (el consenso viene en otra moneda o por acción ordinaria)")
        elif pe_a is not None:
            r["pe_ntm"] = round(pe_a, 2)
            r["avisos"].append("P/E NTM muy distinto del LTM: verificar")
        if eps_ntm is not None and eps_ntm <= 0:
            r["avisos"].append("consenso de ganancias negativo: sin P/E NTM")
    except Exception as e:  # noqa: BLE001
        r["avisos"].append(f"estimaciones: {e}")

    # Dividendos últimos 12 meses
    try:
        dv = tk.dividends
        if dv is not None and len(dv):
            idx = dv.index.tz_localize(None) if getattr(dv.index, "tz", None) is not None else dv.index
            desde = dt.datetime.now() - dt.timedelta(days=365)
            tot = float(dv[idx >= desde].sum())
            if precio and tot >= 0:
                y = tot / precio * 100
                r["div_yield"] = round(y, 2) if y < 25 else None
        else:
            r["div_yield"] = 0.0
    except Exception as e:  # noqa: BLE001
        r["avisos"].append(f"dividendos: {e}")

    # Próxima presentación de resultados
    try:
        cal = tk.calendar
        fechas = cal.get("Earnings Date") if isinstance(cal, dict) else None
        if fechas:
            fs = sorted(f.isoformat() if hasattr(f, "isoformat") else str(f)[:10] for f in fechas)
            r["prox_balance"] = fs[0][:10]
    except Exception:  # noqa: BLE001
        pass
    if not r.get("prox_balance"):
        ts = info.get("earningsTimestamp") or info.get("earningsTimestampStart")
        if ts:
            f = dt.datetime.fromtimestamp(int(ts), dt.timezone.utc).date()
            if f >= hoy() - dt.timedelta(days=3):
                r["prox_balance"] = f.isoformat()
    return r


def betas(tickers, avisos):
    import yfinance as yf
    out = {}
    try:
        df = yf.download(sorted(set(tickers)) + ["^GSPC"], period="5y", interval="1mo", auto_adjust=True, progress=False, threads=True)["Close"]
        ret = df.pct_change(fill_method=None)
        m = ret["^GSPC"]
        for t in set(tickers):
            if t not in ret:
                continue
            j = ret[[t]].join(m).dropna()
            if len(j) >= 24:
                cov = j[t].cov(j["^GSPC"])
                var = j["^GSPC"].var()
                out[t] = {"beta": round(cov / var, 2), "meses": len(j)}
    except Exception as e:  # noqa: BLE001
        avisos.append(f"Beta: {e}")
    return out


# ---------------------------------------------------------------- principal
def main():
    forzar = "--fund" in sys.argv
    avisos, diag = [], {}
    cfg = load(ACTIVOS, {})
    activos = cfg.get("activos", [])
    flujos = load(FLUJOS, {}).get("bonos", {})
    fund_cache = load(FUND, {"actualizado": None, "empresas": {}, "betas": {}})
    hist = load(HIST, {"_nota": "Precios de cierre guardados por la página Cartera (acumulativo).", "precios": {}})
    d = hoy()
    fb = fechas_base(d)

    mep, ccl, mep_serie, mep_fuente = dolares(avisos)
    if not mep:
        print("Sin dólar MEP: no se puede calcular", file=sys.stderr)
        sys.exit(1)
    mep_ant = mep_serie[-2][1] if len(mep_serie) >= 2 and mep_serie[-1][0] >= d.isoformat() else (mep_serie[-1][1] if mep_serie else None)
    m30 = cierre_en(mep_serie, fb["d30"])
    many = cierre_en(mep_serie, fb["anio"])

    pan = paneles(avisos)
    res = {}

    # Historia de Yahoo para acciones de EE.UU. (cotización directa)
    us = [a["yahoo"] for a in activos if a["tipo"] == "Acción EEUU"]
    yh = yahoo_historia(us, avisos, start=f"{d.year - 1}-12-01")

    fci_cache = {}
    fci_hoy = fci_lista("ultimo", avisos)
    fci_pen = fci_lista("penultimo", avisos)
    fci_30, f30 = fci_en_fecha(fb["d30"], avisos, fci_cache)
    fci_an, fan = fci_en_fecha(fb["anio"], avisos, fci_cache)
    if fci_hoy:
        diag["fci_ejemplo"] = fci_hoy[0]

    bc_all = hist.get("bases", {})
    bases_hoy = bc_all.get("valores", {}) if bc_all.get("fecha") == d.isoformat() else {}

    tipo_hist = {"arg_stocks": "stocks", "arg_cedears": "cedears", "arg_bonds": "bonds", "arg_corp": "bonds"}

    for a in activos:
        aid, t = a["id"], a["tipo"]
        r = {"moneda": a["moneda"]}
        try:
            if t == "Acción EEUU":
                s = yh.get(a["yahoo"], [])
                if s:
                    r["precio"], r["fecha"] = s[-1][1], s[-1][0]
                    r["ant"] = s[-2][1] if len(s) >= 2 else None
                    c30, can = cierre_en(s, fb["d30"]), cierre_en(s, fb["anio"])
                    r["p30"], r["f30"] = (c30[1], c30[0]) if c30 else (None, None)
                    r["panio"], r["fanio"] = (can[1], can[0]) if can else (None, None)
                    r["fuente"] = "Yahoo Finance"
            elif t == "FCI":
                h = fci_buscar(fci_hoy, a["fci_busca"], a["nombre"])
                if h:
                    r["precio"], r["fecha"], r["fondo_fuente"] = h
                    p = fci_buscar(fci_pen, a["fci_busca"], a["nombre"])
                    r["ant"] = p[0] if p else None
                    p30 = fci_buscar(fci_30, a["fci_busca"], a["nombre"])
                    pan_ = fci_buscar(fci_an, a["fci_busca"], a["nombre"])
                    r["p30"], r["f30"] = (p30[0], f30) if p30 else (None, None)
                    r["panio"], r["fanio"] = (pan_[0], fan) if pan_ else (None, None)
                    r["fuente"] = "argentinadatos.com (CAFCI)"
                else:
                    avisos.append(f"{a['ticker']}: no se encontró el fondo en argentinadatos")
            else:
                x = pan.get((a["panel"], a["simbolo"])) or pan.get(("*", a["simbolo"]))
                if x:
                    r["precio"], r["ant"], r["var_dia"] = precio_panel(x)
                    r["fecha"] = d.isoformat()
                    r["fuente"] = "data912 (BYMA)"
                bc = bases_hoy.get(aid)
                s = [] if (bc and r.get("precio")) else historia_data912(tipo_hist.get(a["panel"], "stocks"), a["simbolo"], avisos, diag)
                if bc and r.get("precio"):
                    r.update(bc)
                if s:
                    if not r.get("precio"):
                        r["precio"], r["fecha"] = s[-1][1], s[-1][0]
                        r["ant"] = s[-2][1] if len(s) >= 2 else None
                        r["fuente"] = "data912 (BYMA, cierre)"
                    c30, can = cierre_en(s, fb["d30"]), cierre_en(s, fb["anio"])
                    r["p30"], r["f30"] = (c30[1], c30[0]) if c30 else (None, None)
                    r["panio"], r["fanio"] = (can[1], can[0]) if can else (None, None)
                # Precio en dólares (para TIR) de bonos y ONs
                if a.get("simbolo_usd"):
                    xu = pan.get(("*", a["simbolo_usd"]))
                    pu = precio_panel(xu)[0] if xu else None
                    if not pu and r.get("precio") and a["moneda"] == "ARS":
                        pu = r["precio"] / mep
                        r["precio_usd_calc"] = True
                    if a["moneda"] == "USD" and not pu:
                        pu = r.get("precio")
                    r["precio_usd"] = pu
                    fl = flujos.get(a["simbolo_usd"])
                    if fl and pu:
                        r["bono"] = tir_bono(fl["flujos"], pu, d + dt.timedelta(days=1))
                    elif not fl:
                        r["bono"] = None
                        r["bono_nota"] = "Faltan las condiciones de emisión para calcular TIR."
        except Exception as e:  # noqa: BLE001
            avisos.append(f"{a['ticker']}: {e}")
            traceback.print_exc()
        # Respaldo: historia propia (cierres guardados por este programa)
        hp = hist["precios"].setdefault(aid, {})
        if r.get("precio"):
            hp[d.isoformat()] = r["precio"]
        serie = sorted(hp.items())
        if not r.get("p30"):
            c = cierre_en(serie, fb["d30"])
            if c and c[0] < d.isoformat():
                r["p30"], r["f30"], r["base_propia"] = c[1], c[0], True
        if not r.get("panio"):
            c = cierre_en(serie, fb["anio"]) or (serie[0] if serie and serie[0][0] < d.isoformat() else None)
            if c:
                r["panio"], r["fanio"], r["base_propia"] = c[1], c[0], True
        if not r.get("precio"):
            avisos.append(f"{a['ticker']}: sin cotización")
        res[aid] = {k: (rnd(v, 6) if isinstance(v, float) else v) for k, v in r.items()}

    # Bases del día (30 días y año) para no pedir la historia en cada corrida
    hist["bases"] = {"fecha": d.isoformat(), "valores": {
        k: {x: v[x] for x in ("p30", "f30", "panio", "fanio") if v.get(x) is not None}
        for k, v in res.items() if v.get("p30") and v.get("panio") and not v.get("base_propia")}}

    # Recorte del historial propio (se guardan 400 días)
    lim = (d - dt.timedelta(days=400)).isoformat()
    for aid in hist["precios"]:
        hist["precios"][aid] = {k: v for k, v in hist["precios"][aid].items() if k >= lim}

    # ---- Fundamentos (una vez por día)
    fund_tickers = sorted({a["fund"] for a in activos if a.get("fund") and not a.get("indice")})
    viejo = True
    if fund_cache.get("actualizado"):
        try:
            viejo = (ahora() - dt.datetime.fromisoformat(fund_cache["actualizado"])).total_seconds() > HORAS_FUND * 3600
        except Exception:  # noqa: BLE001
            pass
    faltan = [f for f in fund_tickers if f not in fund_cache.get("empresas", {})]
    if forzar or viejo or faltan:
        objetivo = fund_tickers if (forzar or viejo) else faltan
        locales = {a["fund"]: res.get(a["id"], {}).get("precio") for a in activos if a.get("fund")}
        # precio local BYMA de cada empresa argentina (para controlar contra el ADR)
        for a in activos:
            if a["tipo"] == "Acción argentina":
                locales[a["fund"]] = res.get(a["id"], {}).get("precio")
        meta = {a["fund"]: a for a in activos if a.get("fund")}
        for f in objetivo:
            m = meta[f]
            try:
                lp = locales.get(f) if m["tipo"] == "Acción argentina" else None
                fund_cache.setdefault("empresas", {})[f] = fundamentos_de(f, m, ccl, lp, avisos)
            except Exception as e:  # noqa: BLE001
                avisos.append(f"Ratios {f}: {e}")
            time.sleep(0.5)
        bt = betas([f for f in fund_tickers if not meta[f].get("fund_local")], avisos)
        if bt:
            fund_cache["betas"] = bt
        fund_cache["actualizado"] = ahora().isoformat(timespec="minutes")
        save(FUND, fund_cache)

    # SPY: P/E del S&P 500 (LTM de Yahoo sobre SPY; NTM del último informe de FactSet)
    spy = {"fuente": "Yahoo (LTM) y FactSet (NTM)", "beta": 1.0}
    try:
        import yfinance as yf
        spy["pe_ltm"] = rnd(yf.Ticker("SPY").info.get("trailingPE"), 2)
    except Exception:  # noqa: BLE001
        pass
    fs = load(FACTSET, {}).get("informes", [])
    if fs:
        ult = sorted(fs, key=lambda x: x["fecha"])[-1]
        spy["pe_ntm"], spy["fecha_ntm"] = ult.get("pe_fwd"), ult.get("fecha")
    fund_cache.setdefault("empresas", {})["SPY"] = {**fund_cache.get("empresas", {}).get("SPY", {}), **spy}

    out = {
        "actualizado": ahora().isoformat(timespec="minutes"),
        "fund_actualizado": fund_cache.get("actualizado"),
        "dolar": {"mep": mep, "ccl": ccl, "fuente": mep_fuente, "mep_ant": mep_ant,
                  "mep_30": m30[1] if m30 else None, "f30": m30[0] if m30 else None,
                  "mep_anio": many[1] if many else None, "fanio": many[0] if many else None},
        "bases": fb,
        "precios": res,
        "fundamentos": fund_cache.get("empresas", {}),
        "betas": fund_cache.get("betas", {}),
        "control": load(CONTROL, {}).get("empresas", {}),
        "manual": load(MANUAL, {}).get("empresas", {}),
        "avisos": avisos,
        "diagnostico": diag,
    }
    save(OUT, out)
    save(HIST, hist)
    print(f"OK: {sum(1 for v in res.values() if v.get('precio'))}/{len(res)} con precio; {len(avisos)} avisos")
    for x in avisos:
        print(" -", x)


if __name__ == "__main__":
    main()
