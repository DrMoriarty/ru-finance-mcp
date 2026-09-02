"""Резолв тикеров и нормализованный доступ к данным MOEX поверх aioboy/moex.

Наружу даём чистые dict'ы со стабильными ключами — сырые ISS-колонки
(LAST/LCLOSEPRICE/MARKETPRICE/WAPRICE, DURATION в днях и т.п.) и выбор борда
спрятаны здесь.
"""
from __future__ import annotations

from .session import exec_template, first, get_moex, records

# ID шаблонов ISS (определены интроспекцией aioboy/moex)
T_SEARCH = 205   # /securities                                  (поиск)
T_SPEC = 193     # /securities/{security}                       (спецификация)
T_QUOTE_BOARD = 359   # /engines/.../boards/{board}/securities/{security}
T_QUOTE_MARKET = 347  # /engines/.../markets/{market}/securities/{security}
T_CANDLES = 409  # .../boards/{board}/securities/{security}/candles
T_HISTORY = 531  # /history/.../boards/{board}/securities/{security}

# суффикс группы ISS -> рыночный код market
_MARKET = {
    "shares": "shares", "bonds": "bonds", "index": "index",
    "ppif": "shares", "etf": "shares", "dr": "shares",
    "selt": "selt", "forts": "forts", "futures": "forts",
}


def _engine_market(group: str | None) -> tuple[str, str]:
    parts = (group or "stock_shares").split("_", 1)
    engine = parts[0] or "stock"
    suffix = parts[1] if len(parts) > 1 else "shares"
    return engine, _MARKET.get(suffix, suffix)


def resolve(query: str) -> dict:
    """Тикер/ISIN/название -> {secid, engine, market, board, type, ...}.

    Берёт лучшее совпадение: точный secid/ISIN > торгуемая > не индекс/NAV.
    """
    q = query.strip()
    raw = exec_template(T_SEARCH, params={"q": q, "limit": 50})
    rows = records(raw, "securities")
    if not rows:
        raise ValueError(f"MOEX: не найдено бумаг по запросу {query!r}")
    qu = q.upper()

    def score(r: dict) -> int:
        s = 0
        if str(r.get("secid", "")).upper() == qu:
            s += 100
        if str(r.get("isin", "")).upper() == qu:
            s += 100
        if r.get("is_traded") == 1:
            s += 10
        if r.get("group") != "stock_index":  # предпочесть торгуемую бумагу, не NAV/индекс
            s += 5
        return s

    best = max(rows, key=score)
    engine, market = _engine_market(best.get("group"))
    return {
        "secid": best.get("secid"),
        "shortname": best.get("shortname"),
        "isin": best.get("isin"),
        "engine": engine,
        "market": market,
        "board": best.get("primary_boardid"),
        "type": best.get("type"),
        "group": best.get("group"),
        "is_traded": best.get("is_traded"),
        "query": query,
    }


def _marketdata_row(secid: str, engine: str, market: str, board: str) -> dict:
    """Строка marketdata: сперва по борду, при отсутствии цены — по рынку."""
    raw = exec_template(T_QUOTE_BOARD, {
        "engine": engine, "market": market, "board": board, "security": secid})
    rows = records(raw, "marketdata")
    if rows and first(rows[0].get("LAST"), rows[0].get("MARKETPRICE"),
                      rows[0].get("LCLOSEPRICE"), rows[0].get("WAPRICE")) is not None:
        return rows[0]
    # фоллбэк: рынок целиком, ищем строку с ценой
    raw = exec_template(T_QUOTE_MARKET, {
        "engine": engine, "market": market, "security": secid})
    for r in records(raw, "marketdata"):
        if first(r.get("LAST"), r.get("MARKETPRICE"),
                 r.get("LCLOSEPRICE"), r.get("WAPRICE")) is not None:
            return r
    return rows[0] if rows else {}


def quote(query: str) -> dict:
    """Текущая котировка акции/фонда (нормализованная).

    price берётся как первый доступный из LAST/MARKETPRICE/LCLOSEPRICE/WAPRICE
    (в выходные LAST пуст — поэтому фоллбэки).
    """
    r = resolve(query)
    md = _marketdata_row(r["secid"], r["engine"], r["market"], r["board"])
    price = first(md.get("LAST"), md.get("MARKETPRICE"),
                  md.get("LCLOSEPRICE"), md.get("WAPRICE"))
    return {
        "secid": r["secid"], "shortname": r["shortname"], "board": md.get("BOARDID") or r["board"],
        "price": price,
        "change_pct": md.get("LASTCHANGEPRCNT"),
        "bid": md.get("BID"), "ask": md.get("OFFER"),
        "open": md.get("OPEN"), "low": md.get("LOW"), "high": md.get("HIGH"),
        "value_today": md.get("VALTODAY"), "vol_today": md.get("VOLTODAY"),
        "updatetime": md.get("UPDATETIME"),
        "price_field": ("LAST" if md.get("LAST") is not None else
                        "MARKETPRICE" if md.get("MARKETPRICE") is not None else
                        "LCLOSEPRICE" if md.get("LCLOSEPRICE") is not None else "WAPRICE"),
    }


def bond(query: str) -> dict:
    """Облигация: цена %, YTM, дюрация (годы), модиф. дюрация, купон, погашение, НКД."""
    r = resolve(query)
    raw = exec_template(T_QUOTE_BOARD, {
        "engine": r["engine"], "market": r["market"],
        "board": r["board"], "security": r["secid"]})
    spec = (records(raw, "securities") or [{}])[0]
    md_rows = records(raw, "marketdata")
    md = md_rows[0] if md_rows else {}

    price = first(md.get("LAST"), md.get("WAPRICE"),
                  md.get("LCLOSEPRICE"), md.get("MARKETPRICE"))
    ytm = first(md.get("YIELD"), md.get("YIELDATWAPRICE"))
    dur_days = md.get("DURATION")
    dur_years = round(dur_days / 365, 2) if dur_days else None
    mod_dur = None
    if dur_years and ytm:
        mod_dur = round(dur_years / (1 + ytm / 100 / 2), 2)
    coupon_pct = spec.get("COUPONPERCENT")
    face = spec.get("FACEVALUE")
    annual_coupon = round(face * coupon_pct / 100, 2) if (face and coupon_pct) else None
    return {
        "secid": r["secid"], "shortname": r["shortname"], "isin": r["isin"],
        "board": r["board"], "type": r["type"],
        "price_pct": price,
        "change_pct": md.get("LASTCHANGEPRCNT"),
        "ytm": ytm,
        "duration_years": dur_years,
        "mod_duration_years": mod_dur,
        "coupon_pct": coupon_pct,
        "coupon_value": spec.get("COUPONVALUE"),
        "annual_coupon_per_bond": annual_coupon,
        "next_coupon": spec.get("NEXTCOUPON"),
        "coupon_period_days": spec.get("COUPONPERIOD"),
        "maturity": spec.get("MATDATE"),
        "offer_date": spec.get("OFFERDATE"),
        "accrued_int": spec.get("ACCRUEDINT"),
        "face_value": face,
        "face_unit": spec.get("FACEUNIT"),
    }


def candles(query: str, frm: str, till: str, interval: str = "24") -> list[dict]:
    """Свечи OHLCV. interval: 1,10,60(час),24(день),7(нед),31(мес),4(кв)."""
    r = resolve(query)
    raw = exec_template(T_CANDLES, {
        "engine": r["engine"], "market": r["market"],
        "board": r["board"], "security": r["secid"]},
        {"from": frm, "till": till, "interval": interval})
    return records(raw, "candles")


def history(query: str, frm: str, till: str) -> list[dict]:
    """Дневная история торгов (close, volume, value...) за интервал дат."""
    r = resolve(query)
    raw = exec_template(T_HISTORY, {
        "engine": r["engine"], "market": r["market"],
        "board": r["board"], "security": r["secid"]},
        {"from": frm, "till": till})
    return records(raw, "history")


def search_endpoints(pattern: str) -> list[dict]:
    """Найти ISS-эндпоинты (шаблоны) по подстроке пути. Для generic-доступа."""
    out = []
    for t in get_moex().find_template(pattern):
        out.append({"id": t.id, "path": t.path,
                    "variables": sorted(t.path_variables)})
    return out


def query(template_id: int, vars: dict | None = None,
          params: dict | None = None) -> dict:
    """Generic-проброс к ЛЮБОМУ ISS-эндпоинту по template_id.

    Возвращает все блоки как {block: [строки-словари]}. Используй
    search_endpoints(), чтобы найти template_id и нужные переменные пути.
    """
    raw = exec_template(template_id, vars or {}, params or {})
    out = {}
    for block in raw:
        if isinstance(raw[block], dict) and "columns" in raw[block]:
            out[block] = records(raw, block)
    return out
