"""Резолв тикеров и нормализованный доступ к данным MOEX поверх aioboy/moex.

Наружу даём чистые dict'ы со стабильными ключами — сырые ISS-колонки
(LAST/LCLOSEPRICE/MARKETPRICE/WAPRICE, DURATION в днях и т.п.) и выбор борда
спрятаны здесь.
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

import requests as _requests

from . import bonds as _bonds
from .session import ISS, exec_template, first, get_moex, raw_get, records

# ID шаблонов ISS (определены интроспекцией aioboy/moex)
T_SEARCH = 205   # /securities                                  (поиск)
T_SPEC = 193     # /securities/{security}                       (спецификация)
T_QUOTE_BOARD = 359   # /engines/.../boards/{board}/securities/{security}
T_QUOTE_MARKET = 347  # /engines/.../markets/{market}/securities/{security}
T_CANDLES = 409  # .../boards/{board}/securities/{security}/candles
T_HISTORY = 439  # /history/engines/{engine}/markets/{market}/securities/{security}

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


# Маппинг фьючерсных asset_code -> базовый актив для расчёта basis.
# query: строка для резолва на MOEX; cbr_fx: код валюты ЦБ для spot; kind: тип актива.
_UNDERLYING_MAP: dict[str, dict] = {
    # FX-фьючерсы
    "Si":   {"cbr_fx": "USD"},
    "Eu":   {"cbr_fx": "EUR"},
    "CNY":  {"cbr_fx": "CNY"},
    "CHF":  {"cbr_fx": "CHF"},
    "GBP":  {"cbr_fx": "GBP"},
    "JPY":  {"cbr_fx": "JPY"},
    "HKD":  {"cbr_fx": "HKD"},
    "TRY":  {"cbr_fx": "TRY"},
    "KZT":  {"cbr_fx": "KZT"},
    "BYN":  {"cbr_fx": "BYN"},
    # Индексы
    "RTS":  {"query": "RTSI"},
    "MREI": {"query": "MREI"},
    "MXI":  {"query": "MIX"},
    "RVI":  {"query": "MCFTR"},
    "IMOEX": {"query": "IMOEX"},
    # Сырьё
    "BR":   {"cbr_fx": "Brent"},
    "GOLD": {"query": "GOLD", "engine": "commodity", "market": "metals"},
    "GL":   {"query": "GOLD", "engine": "commodity", "market": "metals"},
    "SV":   {"query": "SLVR", "engine": "commodity", "market": "metals"},
    "SLVR": {"query": "SLVR", "engine": "commodity", "market": "metals"},
    "PL":   {"query": "PLTD", "engine": "commodity", "market": "metals"},
    "CU":   {"query": "CU",   "engine": "commodity", "market": "metals"},
    "NI":   {"query": "NI",   "engine": "commodity", "market": "metals"},
    # Акции
    "SBRF": {"query": "SBER"},
    "GAZR": {"query": "GAZP"},
    "LKOH": {"query": "LKOH"},
    "GMKN": {"query": "GMKN"},
    "MGNT": {"query": "MGNT"},
    "ROSN": {"query": "ROSN"},
    "SIBN": {"query": "SIBN"},
    "VTBR": {"query": "VTBR"},
    "TATN": {"query": "TATN"},
    "ALRS": {"query": "ALRS"},
    "FEES": {"query": "FEES"},
    "MTSI": {"query": "MTSS"},
    "NlNK": {"query": "NKNC"},
}


def _canonical_asset(code: str) -> str:
    """Нормализовать asset_code к каноническому регистру из _UNDERLYING_MAP."""
    upper = code.upper()
    for k in _UNDERLYING_MAP:
        if k.upper() == upper:
            return k
    return code


def _resolve_spot_price(asset_code: str) -> tuple[float | None, str]:
    """Определить спот-цену базового актива futures-контракта.

    Возвращает (price, source).
    Для FX-фьючерсов (cbr_fx задан) — курс ЦБ или индикативные MOEX.
    Для не-FX (акции, индексы, товары) — quote() с MOEX.
    В обоих случаях settle_per_unit = settle / lot_volume (для basis).
    """
    from datetime import date as _date

    ref = _UNDERLYING_MAP.get(asset_code, {})
    cbr_fx = ref.get("cbr_fx")

    # --- Валютные фьючерсы (Si, Eu, CNY, CHF, GBP, JPY, HKD, TRY, KZT, BYN) ---
    if cbr_fx and cbr_fx != "Brent":
        spot, src = _spot_via_cbr(cbr_fx)
        if spot:
            return spot, src

    # --- Не-FX: quote() backend MOEX ---
    query = ref.get("query", asset_code)
    engine = ref.get("engine")
    market = ref.get("market")
    try:
        if engine and market:
            raw = raw_get(
                f"engines/{engine}/markets/{market}/boards/ALL/securities",
                {"iss.only": "marketdata", "limit": 1000})
            from .session import first as _first
            for r in records(raw, "marketdata"):
                if r.get("SECID") == query:
                    p = _first(r.get("LAST"), r.get("OFFER"), r.get("LCLOSEPRICE"))
                    if p is not None:
                        return float(p), f"moex:{engine}/{market}/{query}"
                    break  # found but no price, fall through to options_asset
            # exhausted: fall through
        else:
            q = quote(query)
            p = q.get("price")
            if p is not None:
                return float(p), f"moex:{q['secid']} ({q.get('price_field')})"
            # price=None → fall through to options_asset
    except Exception:
        pass

    # --- Фоллбэк: options_assets (spot from FORTS options underlying) ---
    try:
        ac_upper = asset_code.upper()
        for oa in options_assets():
            if (oa.get("asset") or "").upper() == ac_upper:
                p = oa.get("asset_last_price")
                if p is not None:
                    return float(p), f"options_asset:{asset_code}"
    except Exception:
        pass

    return None, "unknown"


def _spot_via_cbr(cbr_fx: str) -> tuple[float | None, str]:
    """Получить курс ЦБ: сначала CBR (последний доступный), потом indicative MOEX."""
    if cbr_fx == "Brent":
        return None, "skip"
    try:
        from datetime import date as _date
        today = _date.today()
        from . import cbr as _cbr
        fx = _cbr.currency(cbr_fx, str(today.replace(day=1)), str(today), tail=1)
        p = fx.get("latest")
        if p:
            return float(p), f"cbr:{cbr_fx} ({fx.get('latest_date')})"
    except Exception:
        pass
    pair_map = {
        "USD": "USD/RUB", "EUR": "EUR/RUB", "CNY": "CNY/RUB",
        "GBP": "GBP/RUB", "CHF": "CHF/RUB", "JPY": "JPY(100)/RUB",
        "HKD": "HKD/RUB", "TRY": "TRY/RUB", "KZT": "KZT/RUB",
        "BYN": "BYN/RUB",
    }
    pair = pair_map.get(cbr_fx)
    if pair:
        try:
            rates = indicative_rates()
            for r in reversed(rates):
                if r.get("secid") == pair and r.get("rate"):
                    return float(r["rate"]), f"moex_indicative:{pair}"
        except Exception:
            pass
    return None, "fix not found"


def resolve(query: str, sec_type: str | None = None,
            as_list: bool = False,
            traded_only: bool = False,
            limit: int | None = None) -> dict | list[dict]:
    """Тикер/ISIN/название -> {secid, engine, market, board, type, ...}.

    sec_type — если задан (напр. "bond", "share", "fund"), фильтрует по типу
    бумаги (stock_bonds, stock_shares, ...). Без фильтра возвращает лучшую
    бумагу по скору.

    as_list — если True, возвращает все совпадения (до 200) вместо одного.
    traded_only — если True, отсечь бумаги с is_traded!=1 уже в запросе к ISS.
    limit — если задан, переопределяет автоматический лимит.
    """
    limit = limit if limit is not None else (200 if as_list else 50)
    q = query.strip()
    # Убираем слова-маркеры типа инструмента из запроса, если тип уже задан.
    # Решает проблему, когда модель добавляет "облигации" к запросу "РСХБ".
    if sec_type:
        _type_words = {
            "bond": {"облигации", "облигация", "бонд", "bond"},
            "share": {"акции", "акция", "share", "stock"},
            "stock": {"акции", "акция", "share", "stock"},
            "fund": {"фонд", "фонды", "fund", "etf"},
            "etf": {"фонд", "фонды", "fund", "etf"},
            "index": {"индекс", "index"},
        }
        words_to_remove = _type_words.get(sec_type.lower(), set())
        # Разбиваем на слова, убираем маркеры, собираем обратно
        words = q.split()
        filtered_words = [w for w in words if w.lower() not in words_to_remove]
        new_q = " ".join(filtered_words).strip()
        # Если после фильтрации запрос стал пустым, используем оригинал
        q = new_q if new_q else q
    params: dict = {"q": q, "limit": limit}
    if traded_only:
        params["is_trading"] = 1
    raw = exec_template(T_SEARCH, params=params)
    rows = records(raw, "securities")
    if not rows:
        raise ValueError(f"MOEX: не найдено бумаг по запросу {query!r}")

    _GROUP_PREF = {
        "bond": "stock_bonds",
        "share": "stock_shares",
        "stock": "stock_shares",
        "fund": "stock_ppif",
        "etf": "stock_etf",
        "index": "stock_index",
    }
    target_group = _GROUP_PREF.get(sec_type.lower()) if sec_type else None

    def _fmt(r: dict) -> dict:
        engine, market = _engine_market(r.get("group"))
        return {
            "secid": r.get("secid"),
            "shortname": r.get("shortname"),
            "isin": r.get("isin"),
            "engine": engine,
            "market": market,
            "board": r.get("primary_boardid"),
            "type": r.get("type"),
            "group": r.get("group"),
            "is_traded": r.get("is_traded"),
            "emitent_id": r.get("emitent_id"),
            "emitent_title": r.get("emitent_title"),
        }

    # фильтр по типу (если задан)
    if target_group:
        rows = [r for r in rows if r.get("group") == target_group]
        if not rows:
            raise ValueError(
                f"MOEX: по запросу {query!r} не найдено бумаг типа {sec_type!r}"
            )

    if as_list:
        return [_fmt(r) for r in rows]

    qu = q.upper()

    def score(r: dict) -> int:
        s = 0
        if str(r.get("secid", "")).upper() == qu:
            s += 100
        if str(r.get("isin", "")).upper() == qu:
            s += 100
        if r.get("is_traded") == 1:
            s += 10
        if r.get("group") != "stock_index":
            s += 5
        if target_group and r.get("group") == target_group:
            s += 50
        return s

    best = max(rows, key=score)
    return _fmt(best) | {"query": query}


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
    """Облигация: цена %, YTM, дюрация (годы), модиф. дюрация, купон, погашение, НКД.

    Для облигаций со ступенчатыми/переменными купонами YTM, duration и mod_duration
    от MOEX могут быть некорректны (ISS считает по фиксированной coupon_pct).
    Используйте bond_report для корректного расчёта с реальным расписанием купонов.
    """
    r = resolve(query, sec_type="bond")
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
        "emitent_id": r.get("emitent_id"),
        "emitent": r.get("emitent_title"),
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


def bond_coupons(query: str) -> list[dict]:
    """Расписание купонов облигации (история + будущие) из НРД/MOEX bondization.

    Работает для корпоративных облигаций; ОФЗ возвращают пустой список (данные ЦБ).
    """
    r = resolve(query, sec_type="bond")
    raw = raw_get(
        f"statistics/engines/stock/markets/bonds/bondization/{r['isin']}/coupons",
        {"limit": 500})
    today = str(__import__("datetime").date.today())
    rows: list[dict] = []
    for row in records(raw, "coupons"):
        rows.append({
            "isin": row.get("isin"),
            "coupondate": row.get("coupondate"),
            "recorddate": row.get("recorddate"),
            "startdate": row.get("startdate"),
            "value": row.get("value"),
            "valueprc": row.get("valueprc"),
            "value_rub": row.get("value_rub"),
            "facevalue": row.get("facevalue"),
            "faceunit": row.get("faceunit"),
            "is_past": (row.get("coupondate") or "") < today,
        })
    return rows


def future_bond_coupons(query: str) -> list[dict]:
    """Будущие купоны облигации с датами и ставками.

    Возвращает [{date: 'YYYY-MM-DD', rate_pct: float}, ...] —
    только купоны с датой > сегодня. Для переменных купонов.
    Ставка считается из value/facevalue (годовая), т.к. valueprc в
    bondization — базовая объявленная ставка, одинаковая для всех периодов.
    """
    try:
        all_coups = bond_coupons(query)
    except Exception:
        return []
    today_str = str(__import__("datetime").date.today())
    future = []
    for c in all_coups:
        d = (c.get("coupondate") or "")[:10]
        if d > today_str:
            future.append(c)
    if not future:
        return []

    freq = 12
    if len(future) >= 2:
        from datetime import datetime as _dt
        d0 = _dt.strptime(future[0]["coupondate"][:10], "%Y-%m-%d")
        d1 = _dt.strptime(future[1]["coupondate"][:10], "%Y-%m-%d")
        gap = (d1 - d0).days
        if gap > 0:
            freq = round(365 / gap)

    out = []
    for c in future:
        d = (c.get("coupondate") or "")[:10]
        fv = c.get("facevalue")
        val = c.get("value")
        if fv and val and fv > 0:
            rate = val / fv * freq * 100
        else:
            rate = c.get("valueprc")
            if rate is None:
                continue
        out.append({"date": d, "rate_pct": round(float(rate), 4)})
    return sorted(out, key=lambda x: x["date"])


_CANDLE_INTERVALS = [
    ("24", 1),    # день
    ("7",  7),    # неделя
    ("31", 30),   # месяц
    ("4",  91),   # квартал
]

_INTERVAL_ALIASES = {
    "1": "1", "min": "1", "minute": "1", "мин": "1", "минута": "1",
    "10": "10", "10min": "10", "10мин": "10",
    "60": "60", "hour": "60", "h": "60", "час": "60",
    "24": "24", "day": "24", "d": "24", "д": "24", "день": "24",
    "7": "7", "week": "7", "w": "7", "нед": "7", "неделя": "7",
    "31": "31", "month": "31", "m": "31", "мес": "31", "месяц": "31",
    "4": "4", "quarter": "4", "q": "4", "кв": "4", "квартал": "4",
}


def _auto_interval(frm: str, till: str) -> str:
    d0 = datetime.fromisoformat(frm)
    d1 = datetime.fromisoformat(till)
    days = (d1 - d0).days
    if days <= 0:
        return "60"
    target = days / 50
    for iv, step in reversed(_CANDLE_INTERVALS):
        if target >= step:
            return iv
    return "24"


def candles(query: str, frm: str, till: str, interval: str = "") -> list[dict]:
    """Свечи OHLCV. interval: 1,10,60(час),24(день),7(нед),31(мес),4(кв).

    Пустой query — ошибка. Пустой или некорректный interval — авто-выбор (≤50 свечей).
    Принимает альтернативные наименования: day/день, week/неделя, month/мес, quarter/кв, hour/час.
    """
    if not query or not query.strip():
        raise ValueError("moex_candles: query не может быть пустым")
    if interval:
        interval = _INTERVAL_ALIASES.get(interval.lower().strip(), "")
    if not interval:
        interval = _auto_interval(frm, till)
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
        "security": r["secid"]},
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


# ─────────────────── Облигации эмитента (emitent_bonds) ───────────────────

# Основные борды облигаций на MOEX
_BOND_BOARDS = ["TQCB", "TQOB", "TQIR"]


def _fetch_board_bonds(board: str, retries: int = 4) -> list[dict]:
    """Загрузить все облигации с борда (securities + marketdata) за один запрос.

    Возвращает список merged-словарей (securities + marketdata + _board).
    Поле emitent_id бордов НЕ содержит — совершать match с emitent_id нужно
    из другого источника (ISS /securities).
    """
    path = f"engines/stock/markets/bonds/boards/{board}/securities"
    raw = raw_get(path, {"iss.only": "securities,marketdata"}, retries=retries)
    secs = records(raw, "securities")
    mds = records(raw, "marketdata")
    md_by_secid: dict[str, dict] = {}
    for m in mds:
        sid = m.get("SECID")
        if sid:
            md_by_secid[sid] = m
    out: list[dict] = []
    for s in secs:
        sid = s.get("SECID")
        s["_board"] = board
        if sid and sid in md_by_secid:
            s.update(md_by_secid[sid])
        out.append(s)
    return out


def emitent_bonds(
    query: str,
    min_duration: float | None = None,
    max_duration: float | None = None,
) -> list[dict]:
    """Все облигации эмитента с фильтрацией по дюрации/сроку до погашения.

    1. Ищет бумаги через ISS /securities (emitent_id есть только там).
    2. Определяет наиболее частый emitent_id, фиксирует множество secid.
    3. Загружает все облигации с основных бордов (TQCB, TQOB, TQIR) batch-запросами
       (рыночные данные: цена, дюрация, доходность).
    4. Сопоставляет secid, фильтрует по min_duration / max_duration (в годах).

    Фильтр по дюрации: приоритет — duration_years (Macaulay в днях / 365 от marketdata),
    fallback — years_to_maturity (от MATDATE). Обе метрики возвращаются.

    Args:
        query — название/тикер эмитента (напр. 'Газпром', 'Сбербанк').
        min_duration — минимальная дюрация в годах (включительно).
        max_duration — максимальная дюрация в годах (включительно).

    Returns:
        Список словарей: secid, shortname, isin, board, is_traded, emitent_id, emitent,
        issuer_name, issue_number, face_value, face_unit, coupon_pct, coupon_period,
        next_coupon, maturity, offer_date, accrued_int, duration_years,
        mod_duration_years, years_to_maturity, price_pct, change_pct, ytm,
        value_today, vol_today.
        Сортировка по duration_years ↑.
    """
    from datetime import date as _date

    # ── Шаг 1: резолв emitent_id (ISS /securities — единственный источник id) ──
    try:
        search_raw = raw_get("securities", {"q": query, "limit": 200})
        bond_rows = [r for r in records(search_raw, "securities")
                     if r.get("group") == "stock_bonds"]
    except Exception:  # noqa: BLE001
        return []
    if not bond_rows:
        return []

    eid_counts: dict[int, str] = {}
    for r in bond_rows:
        eid = r.get("emitent_id")
        title = r.get("emitent_title") or ""
        if eid:
            eid_counts[eid] = title
    if not eid_counts:
        return []

    target_eid: int = max(eid_counts, key=lambda e: sum(
        1 for r in bond_rows if r.get("emitent_id") == e))
    emitent_title = eid_counts[target_eid]

    # Множество secid целевого эмитента (из поиска) — для match с бордами
    target_secids: set[str] = set()
    for r in bond_rows:
        sid = r.get("secid")
        if sid and r.get("emitent_id") == target_eid:
            target_secids.add(sid)

    # ── Шаг 2: batch-загрузка рыночных данных с бордов (параллельно) ──
    board_data: list[dict] = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = [ex.submit(_fetch_board_bonds, b) for b in _BOND_BOARDS]
        for f in as_completed(futs):
            board_data.extend(f.result())

    # ── Шаг 3: match + нормализация ──
    today = _date.today()
    matches: list[dict] = []
    for row in board_data:
        sid = row.get("SECID")
        if sid not in target_secids:
            continue

        dur_days = row.get("DURATION")
        dur_years = round(dur_days / 365, 2) if dur_days else None
        ytm = first(row.get("YIELD"), row.get("YIELDATWAPRICE"))
        mod_dur = None
        if dur_years and ytm:
            mod_dur = round(dur_years / (1 + ytm / 100 / 2), 2)
        maturity = row.get("MATDATE")
        try:
            ytm_years = _bonds.years_to_maturity(maturity) if maturity else None
        except (ValueError, TypeError):
            ytm_years = None

        price = first(row.get("LAST"), row.get("WAPRICE"),
                      row.get("LCLOSEPRICE"), row.get("MARKETPRICE"))
        coupon_pct = row.get("COUPONPERCENT")
        face = row.get("FACEVALUE")

        matches.append({
            "secid": sid,
            "shortname": row.get("SHORTNAME"),
            "isin": row.get("ISIN"),
            "board": row.get("_board"),
            "is_traded": row.get("IS_TRADED"),
            "emitent_id": target_eid,
            "emitent": emitent_title,
            "issuer_name": row.get("ISSUER_NAME"),
            "issue_number": row.get("ISSUESIZE"),
            "face_value": face,
            "face_unit": row.get("FACEUNIT"),
            "coupon_pct": coupon_pct,
            "coupon_period": row.get("COUPONPERIOD"),
            "next_coupon": row.get("NEXTCOUPON"),
            "maturity": maturity,
            "offer_date": row.get("OFFERDATE"),
            "accrued_int": row.get("ACCRUEDINT"),
            "duration_years": dur_years,
            "mod_duration_years": mod_dur,
            "years_to_maturity": ytm_years,
            "price_pct": price,
            "change_pct": row.get("LASTCHANGEPRCNT"),
            "ytm": ytm,
            "value_today": first(row.get("VALTODAY")),
            "vol_today": first(row.get("VOLTODAY")),
        })

    # ── Шаг 4: фильтр по дюрации ──
    if min_duration is not None or max_duration is not None:
        filtered: list[dict] = []
        for r in matches:
            d = r.get("duration_years") or r.get("years_to_maturity")
            if d is None:
                continue
            if min_duration is not None and d < min_duration:
                continue
            if max_duration is not None and d > max_duration:
                continue
            filtered.append(r)
        matches = filtered

    matches.sort(key=lambda r: r.get("duration_years")
                 or r.get("years_to_maturity") or 999)
    return matches


# ─────────────────── Скринер облигаций (bond_screener) ───────────────────

def _freq_from_period(period_days: int | None) -> int:
    """Частота купонов в год (1, 2, 4, 6, 12) из периода в днях."""
    if not period_days or period_days <= 0:
        return 2
    _CANONICAL = {365: 1, 182: 2, 183: 2, 91: 4, 92: 4, 61: 6, 30: 12, 31: 12}
    if period_days in _CANONICAL:
        return _CANONICAL[period_days]
    freq = round(365 / period_days)
    return max(1, min(freq, 12))


# Типы купонов (BONDTYPE ISS)
_FIXED_TYPES = {"Фикс с известным купоном", "Фикс с неизвестным купоном"}
_FLOAT_TYPES = {"Флоатер"}
_AMORT_TYPES = {"Амортизируемые облигации"}

# Соответствие user-friendly названий и ISS BONDTYPE
_COUPON_TYPE_MAP = {
    "fixed": _FIXED_TYPES,
    "float": _FLOAT_TYPES,
    "amortization": _AMORT_TYPES,
}


def _match_rating(emitent_name: str, all_ratings: list) -> str | None:
    """Найти рейтинг эмитента в кеше raexpert по имени (fuzzy)."""
    if not emitent_name or not all_ratings:
        return None
    from .raexpert import _normalize_emitent_name
    norm = _normalize_emitent_name(emitent_name).upper()
    if not norm:
        return None
    for r in all_ratings:
        if r.get("type") != "emitent":
            continue
        r_norm = _normalize_emitent_name(r["name"]).upper()
        if not r_norm:
            continue
        if norm in r_norm or r_norm in norm:
            return r.get("rating")
    return None


def _match_sector(emitent_name: str) -> str | None:
    """Определить MOEX-сектор эмитента (по карте raexpert, ~100 эмитентов)."""
    try:
        from .raexpert import _sector_for_emitent
        return _sector_for_emitent(emitent_name)
    except Exception:
        return None


def _fetch_bond_spec_qualified(secid: str, retries: int = 2) -> bool | None:
    """Получить ISQUALIFIEDINVESTORS для одной облигации через T_SPEC."""
    try:
        raw = exec_template(T_SPEC, {"security": secid}, retries=retries)
        for block in ("description", "securities"):
            rows = records(raw, block)
            if rows:
                for row in rows:
                    if row.get("name") == "ISQUALIFIEDINVESTORS":
                        return str(row.get("value", "0")) == "1"
    except Exception:
        pass
    return None


def _parse_board_bond(row: dict, board: str) -> dict | None:
    """Нормализовать строку _fetch_board_bonds в словарь скринера."""
    # Цена: приоритет LAST → WAPRICE → LCLOSEPRICE → MARKETPRICE
    price = first(row.get("LAST"), row.get("WAPRICE"),
                  row.get("LCLOSEPRICE"), row.get("MARKETPRICE"))
    ytm = first(row.get("YIELD"), row.get("YIELDATWAPRICE"))
    dur_days = row.get("DURATION")
    dur_years = round(dur_days / 365, 2) if dur_days and dur_days > 0 else None
    mod_dur = None
    if dur_years and ytm:
        mod_dur = round(dur_years / (1 + ytm / 100 / 2), 2)

    coupon_period = row.get("COUPONPERIOD")
    coupon_freq = _freq_from_period(coupon_period)

    face_unit = row.get("FACEUNIT") or "SUR"

    bond_type = row.get("BONDTYPE") or ""
    bond_subtype = row.get("BONDSUBTYPE") or ""

    maturity = row.get("MATDATE")
    offer_date = row.get("OFFERDATE")
    put_date = row.get("PUTOPTIONDATE")
    call_date = row.get("CALLOPTIONDATE")
    active_offer = offer_date if (offer_date and offer_date != "0000-00-00") else None
    active_put = put_date if (put_date and put_date != "0000-00-00") else None
    active_call = call_date if (call_date and call_date != "0000-00-00") else None
    has_offer = bool(active_offer or active_put or active_call)
    first_offer = None
    for d in (active_offer, active_put, active_call):
        if d and d != "0000-00-00":
            if first_offer is None or d < first_offer:
                first_offer = d

    # Срок до погашения (years)
    ytm_years = None
    if maturity and maturity != "0000-00-00":
        try:
            ytm_years = _bonds.years_to_maturity(maturity)
        except (ValueError, TypeError):
            pass

    accrued = row.get("ACCRUEDINT")

    issue_size = row.get("ISSUESIZE")
    issue_size_placed = row.get("ISSUESIZEPLACED")

    # Ликвидность
    val_today = row.get("VALTODAY")
    vol_today = row.get("VOLTODAY")
    num_trades = row.get("NUMTRADES")
    bid = row.get("BID")
    offer_px = row.get("OFFER")
    spread = row.get("SPREAD")
    bid_ask_spread_pct = None
    if bid and offer_px and bid > 0 and offer_px > 0 and offer_px > bid:
        avg_price = (bid + offer_px) / 2
        if avg_price > 0:
            bid_ask_spread_pct = round((offer_px - bid) / avg_price * 100, 4)

    list_level = row.get("LISTLEVEL")

    return {
        "secid": row.get("SECID"),
        "shortname": row.get("SHORTNAME"),
        "isin": row.get("ISIN"),
        "secname": row.get("SECNAME"),
        "board": board,
        "emitent": row.get("SECNAME") or row.get("SHORTNAME") or "",
        "price_pct": price,
        "ytm": ytm,
        "coupon_pct": row.get("COUPONPERCENT"),
        "coupon_value": row.get("COUPONVALUE"),
        "coupon_freq": coupon_freq,
        "coupon_period_days": coupon_period,
        "maturity": maturity,
        "years_to_maturity": ytm_years,
        "offer_date": first_offer,
        "has_offer": has_offer,
        "duration_years": dur_years,
        "mod_duration_years": mod_dur,
        "bond_type": bond_type,
        "bond_subtype": bond_subtype,
        "is_amortization": bond_type in _AMORT_TYPES,
        "face_unit": face_unit,
        "face_value": row.get("FACEVALUE"),
        "accrued_int": accrued,
        "issue_size": issue_size,
        "issue_size_placed": issue_size_placed,
        "list_level": list_level,
        # Ликвидность
        "value_today": val_today,
        "vol_today": vol_today,
        "num_trades": num_trades,
        "bid_ask_spread_pct": bid_ask_spread_pct,
    }


def bond_screener(
    *,
    ytm_min: float | None = None,
    ytm_max: float | None = None,
    coupon_min: float | None = None,
    coupon_max: float | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    maturity_from: str | None = None,
    maturity_to: str | None = None,
    duration_min: float | None = None,
    duration_max: float | None = None,
    years_to_maturity_min: float | None = None,
    years_to_maturity_max: float | None = None,
    has_offer: bool | None = None,
    has_amortization: bool | None = None,
    coupon_type: str | None = None,
    coupon_freq_min: int | None = None,
    coupon_freq_max: int | None = None,
    currency: str | None = None,
    issue_volume_min: int | None = None,
    issue_volume_max: int | None = None,
    accrued_int_min: float | None = None,
    accrued_int_max: float | None = None,
    rating_min: str | None = None,
    sector: str | None = None,
    emitent: str | None = None,
    include_qualified: bool = False,
    qualified_only: bool | None = None,
    sort_by: str = "ytm",
    sort_desc: bool = True,
    limit: int = 15,
) -> dict:
    """Скринер облигаций MOEX с фильтрацией по набору параметров.

    Загружает все облигации с бордов TQCB (корпоративные) и TQOB (ОФЗ),
    применяет фильтры, опционально обогащает кредитным рейтингом и статусом
    квалифицированного инвестора.

    Args:
        ytm_min/ytm_max — доходность к погашению (%), границы включительно
        coupon_min/coupon_max — купонная ставка (%)
        price_min/price_max — цена чистая (% от номинала)
        maturity_from/maturity_to — дата погашения ('YYYY-MM-DD')
        duration_min/duration_max — дюрация Macaulay (годы)
        years_to_maturity_min/years_to_maturity_max — срок до погашения (годы)
        has_offer — True: только с офертой; False: только без
        has_amortization — True: только амортизируемые; False: только без
        coupon_type — 'fixed' (фиксированный), 'float' (плавающий),
                      'amortization' (амортизируемые). None = любой
        coupon_freq_min/coupon_freq_max — купонов в год (1,2,4,6,12)
        currency — код валюты ('SUR', 'USD', 'EUR', 'CNY')
        issue_volume_min/issue_volume_max — объём выпуска (штук бумаг)
        accrued_int_min/accrued_int_max — НКД (RUB)
        rating_min — минимальный рейтинг Эксперт РА ('ruBBB-' = investment grade)
        sector — MOEX-сектор эмитента (напр. 'Финансовый', 'Нефтегазовый').
                 Ограничение: карты секторов покрывает ~100 крупнейших эмитентов.
        emitent — подстрока названия эмитента (регистронезависимо), напр. 'Сбер', 'Газпром'.
        include_qualified — добавить поле is_qualified (ISQUALIFIEDINVESTORS).
                            Дополнительный запрос на каждую бумагу (параллельно).
        qualified_only — True: только для квалифицированных;
                         False: только для неквалифицированных; None — без фильтра.
                         Требует include_qualified=True.
        sort_by — поле сортировки: 'ytm', 'duration', 'maturity', 'price',
                  'coupon', 'issue_volume'
        sort_desc — True = по убыванию
        limit — максимум результатов (1..500, default 15)

    Returns:
        {count, bonds: [{secid, shortname, isin, board, emitent,
          price_pct, ytm, coupon_pct, coupon_freq, duration_years, mod_duration_years,
          maturity, years_to_maturity, offer_date, has_offer,
          bond_type, is_amortization, face_unit, face_value, accrued_int,
          issue_size, issue_size_placed, list_level,
          value_today, vol_today, num_trades, bid_ask_spread_pct,
          rating?, sector?, is_qualified?}]}
    """
    limit = max(1, min(limit, 500))

    # ── Шаг 1: загрузить все облигации с бордов (параллельно) ──
    boards_to_fetch = ["TQCB", "TQOB"]
    board_data: list[tuple[str, dict]] = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(_fetch_board_bonds, b): b for b in boards_to_fetch}
        for f in as_completed(futs):
            board = futs[f]
            try:
                for row in f.result():
                    board_data.append((board, row))
            except Exception:
                continue

    # ── Шаг 2: нормализация ──
    all_bonds: list[dict] = []
    for board, row in board_data:
        parsed = _parse_board_bond(row, board)
        if parsed:
            all_bonds.append(parsed)

    # ── Шаг 3: фильтрация ──
    filtered = all_bonds

    if ytm_min is not None:
        filtered = [b for b in filtered if b.get("ytm") is not None and b["ytm"] >= ytm_min]
    if ytm_max is not None:
        filtered = [b for b in filtered if b.get("ytm") is not None and b["ytm"] <= ytm_max]

    if coupon_min is not None:
        filtered = [b for b in filtered if b.get("coupon_pct") is not None and b["coupon_pct"] >= coupon_min]
    if coupon_max is not None:
        filtered = [b for b in filtered if b.get("coupon_pct") is not None and b["coupon_pct"] <= coupon_max]

    if price_min is not None:
        filtered = [b for b in filtered if b.get("price_pct") is not None and b["price_pct"] >= price_min]
    if price_max is not None:
        filtered = [b for b in filtered if b.get("price_pct") is not None and b["price_pct"] <= price_max]

    if maturity_from:
        filtered = [b for b in filtered if b.get("maturity") and b["maturity"] >= maturity_from]
    if maturity_to:
        filtered = [b for b in filtered if b.get("maturity") and b["maturity"] <= maturity_to]

    if duration_min is not None:
        filtered = [b for b in filtered
                    if (b.get("duration_years") or b.get("years_to_maturity")) is not None
                    and (b.get("duration_years") or b.get("years_to_maturity")) >= duration_min]
    if duration_max is not None:
        filtered = [b for b in filtered
                    if (b.get("duration_years") or b.get("years_to_maturity")) is not None
                    and (b.get("duration_years") or b.get("years_to_maturity")) <= duration_max]

    if years_to_maturity_min is not None:
        filtered = [b for b in filtered
                    if b.get("years_to_maturity") is not None
                    and b["years_to_maturity"] >= years_to_maturity_min]
    if years_to_maturity_max is not None:
        filtered = [b for b in filtered
                    if b.get("years_to_maturity") is not None
                    and b["years_to_maturity"] <= years_to_maturity_max]

    if has_offer is not None:
        filtered = [b for b in filtered if b["has_offer"] == has_offer]

    if has_amortization is not None:
        filtered = [b for b in filtered if b["is_amortization"] == has_amortization]

    if coupon_type:
        target_types = _COUPON_TYPE_MAP.get(coupon_type.lower())
        if target_types:
            filtered = [b for b in filtered if b.get("bond_type") in target_types]

    if coupon_freq_min is not None:
        filtered = [b for b in filtered if b["coupon_freq"] >= coupon_freq_min]
    if coupon_freq_max is not None:
        filtered = [b for b in filtered if b["coupon_freq"] <= coupon_freq_max]

    if currency:
        filtered = [b for b in filtered if b.get("face_unit") == currency.upper()]

    if issue_volume_min is not None:
        filtered = [b for b in filtered if b.get("issue_size") and b["issue_size"] >= issue_volume_min]
    if issue_volume_max is not None:
        filtered = [b for b in filtered if b.get("issue_size") and b["issue_size"] <= issue_volume_max]

    if accrued_int_min is not None:
        filtered = [b for b in filtered
                    if b.get("accrued_int") is not None and b["accrued_int"] >= accrued_int_min]
    if accrued_int_max is not None:
        filtered = [b for b in filtered
                    if b.get("accrued_int") is not None and b["accrued_int"] <= accrued_int_max]

    # ── Шаг 4: кредитный рейтинг + сектор (опционально) ──
    all_ratings_cache: list | None = None

    if rating_min is not None:
        # Рейтинги загружаются только при запросе фильтра
        try:
            from .raexpert import _fetch_all_ratings, _RATING_ORDER as ro
            all_ratings_cache = _fetch_all_ratings()
            min_score = ro.get(rating_min.strip())
            if min_score is not None:
                rated_filtered = []
                for b in filtered:
                    rating = _match_rating(b.get("emitent", ""), all_ratings_cache)
                    b["rating"] = rating
                    if rating and rating != "отозван" and ro.get(rating, -1) >= min_score:
                        rated_filtered.append(b)
                filtered = rated_filtered
        except Exception:
            for b in filtered:
                b["rating"] = None
    else:
        for b in filtered:
            b["rating"] = None

    # Сектор — только если фильтр задан
    if sector:
        sector_filtered = []
        for b in filtered:
            s = _match_sector(b.get("emitent", ""))
            b["sector"] = s
            if s == sector:
                sector_filtered.append(b)
        filtered = sector_filtered
    else:
        for b in filtered:
            b["sector"] = None

    if emitent:
        emit_lower = emitent.lower()
        filtered = [b for b in filtered if emit_lower in (b.get("emitent") or "").lower()]

    # ── Шаг 5: ISQUALIFIEDINVESTORS (опционально, параллельно) ──
    if include_qualified:
        secids = [b["secid"] for b in filtered[:200]]  # ограничим 200 запросами
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(_fetch_bond_spec_qualified, sid): sid for sid in secids}
            results: dict[str, bool | None] = {}
            for f in as_completed(futs):
                sid = futs[f]
                try:
                    results[sid] = f.result()
                except Exception:
                    results[sid] = None
        for b in filtered:
            b["is_qualified"] = results.get(b["secid"])

        # Фильтр только для квалифицированных / неквалифицированных
        if qualified_only is True:
            filtered = [b for b in filtered if b.get("is_qualified") is True]
        elif qualified_only is False:
            filtered = [b for b in filtered if b.get("is_qualified") is False]

    # ── Шаг 6: сортировка ──
    _SORT_KEYS = {
        "ytm": "ytm",
        "duration": "duration_years",
        "maturity": "years_to_maturity",
        "price": "price_pct",
        "coupon": "coupon_pct",
        "issue_volume": "issue_size",
    }
    sort_field = _SORT_KEYS.get(sort_by, "ytm")

    def _sort_key(b: dict) -> float:
        v = b.get(sort_field)
        if v is None:
            return float("-inf") if sort_desc else float("inf")
        return float(v)

    filtered.sort(key=_sort_key, reverse=sort_desc)

    # ── Шаг 7: лимит ──
    result = filtered[:limit]

    return {
        "count_shown": len(result),
        "count_total_matching": len(filtered),
        "count_all_bonds": len(all_bonds),
        "bonds": result,
    }


def bond_prescreener(
    *,
    ytm_min: float | None = None,
    ytm_max: float | None = None,
    coupon_min: float | None = None,
    coupon_max: float | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    maturity_from: str | None = None,
    maturity_to: str | None = None,
    duration_min: float | None = None,
    duration_max: float | None = None,
    years_to_maturity_min: float | None = None,
    years_to_maturity_max: float | None = None,
    has_offer: bool | None = None,
    has_amortization: bool | None = None,
    coupon_type: str | None = None,
    coupon_freq_min: int | None = None,
    coupon_freq_max: int | None = None,
    currency: str | None = None,
    issue_volume_min: int | None = None,
    issue_volume_max: int | None = None,
    accrued_int_min: float | None = None,
    accrued_int_max: float | None = None,
    rating_min: str | None = None,
    sector: str | None = None,
    emitent: str | None = None,
    include_qualified: bool = False,
    qualified_only: bool | None = None,
    sort_by: str = "ytm",
    sort_desc: bool = True,
    limit: int = 500,
) -> dict:
    """Прескринер облигаций: компактный вывод (только secname + isin).

    Те же параметры фильтрации, что и bond_screener, но возвращает только
    secname и isin по каждой облигации. Оптимизирован для проверки наличия бондов
    без переполнения контекста.

    Args:
        см. bond_screener

    Returns:
        {count_total_matching, count_all_bonds,
         bonds: [{secname, isin}, ...]}
    """
    limit = max(1, min(limit, 500))

    # ── Шаг 1: загрузить все облигации с бордов (параллельно) ──
    boards_to_fetch = ["TQCB", "TQOB"]
    board_data: list[tuple[str, dict]] = []
    with ThreadPoolExecutor(max_workers=3) as ex:
        futs = {ex.submit(_fetch_board_bonds, b): b for b in boards_to_fetch}
        for f in as_completed(futs):
            board = futs[f]
            try:
                for row in f.result():
                    board_data.append((board, row))
            except Exception:
                continue

    # ── Шаг 2: нормализация ──
    all_bonds: list[dict] = []
    for board, row in board_data:
        parsed = _parse_board_bond(row, board)
        if parsed:
            all_bonds.append(parsed)

    # ── Шаг 3: фильтрация ──
    filtered = all_bonds

    if ytm_min is not None:
        filtered = [b for b in filtered if b.get("ytm") is not None and b["ytm"] >= ytm_min]
    if ytm_max is not None:
        filtered = [b for b in filtered if b.get("ytm") is not None and b["ytm"] <= ytm_max]

    if coupon_min is not None:
        filtered = [b for b in filtered if b.get("coupon_pct") is not None and b["coupon_pct"] >= coupon_min]
    if coupon_max is not None:
        filtered = [b for b in filtered if b.get("coupon_pct") is not None and b["coupon_pct"] <= coupon_max]

    if price_min is not None:
        filtered = [b for b in filtered if b.get("price_pct") is not None and b["price_pct"] >= price_min]
    if price_max is not None:
        filtered = [b for b in filtered if b.get("price_pct") is not None and b["price_pct"] <= price_max]

    if maturity_from:
        filtered = [b for b in filtered if b.get("maturity") and b["maturity"] >= maturity_from]
    if maturity_to:
        filtered = [b for b in filtered if b.get("maturity") and b["maturity"] <= maturity_to]

    if duration_min is not None:
        filtered = [b for b in filtered
                    if (b.get("duration_years") or b.get("years_to_maturity")) is not None
                    and (b.get("duration_years") or b.get("years_to_maturity")) >= duration_min]
    if duration_max is not None:
        filtered = [b for b in filtered
                    if (b.get("duration_years") or b.get("years_to_maturity")) is not None
                    and (b.get("duration_years") or b.get("years_to_maturity")) <= duration_max]

    if years_to_maturity_min is not None:
        filtered = [b for b in filtered
                    if b.get("years_to_maturity") is not None
                    and b["years_to_maturity"] >= years_to_maturity_min]
    if years_to_maturity_max is not None:
        filtered = [b for b in filtered
                    if b.get("years_to_maturity") is not None
                    and b["years_to_maturity"] <= years_to_maturity_max]

    if has_offer is not None:
        filtered = [b for b in filtered if b["has_offer"] == has_offer]

    if has_amortization is not None:
        filtered = [b for b in filtered if b["is_amortization"] == has_amortization]

    if coupon_type:
        target_types = _COUPON_TYPE_MAP.get(coupon_type.lower())
        if target_types:
            filtered = [b for b in filtered if b.get("bond_type") in target_types]

    if coupon_freq_min is not None:
        filtered = [b for b in filtered if b["coupon_freq"] >= coupon_freq_min]
    if coupon_freq_max is not None:
        filtered = [b for b in filtered if b["coupon_freq"] <= coupon_freq_max]

    if currency:
        filtered = [b for b in filtered if b.get("face_unit") == currency.upper()]

    if issue_volume_min is not None:
        filtered = [b for b in filtered if b.get("issue_size") and b["issue_size"] >= issue_volume_min]
    if issue_volume_max is not None:
        filtered = [b for b in filtered if b.get("issue_size") and b["issue_size"] <= issue_volume_max]

    if accrued_int_min is not None:
        filtered = [b for b in filtered
                    if b.get("accrued_int") is not None and b["accrued_int"] >= accrued_int_min]
    if accrued_int_max is not None:
        filtered = [b for b in filtered
                    if b.get("accrued_int") is not None and b["accrued_int"] <= accrued_int_max]

    # ── Шаг 4: кредитный рейтинг + сектор (опционально) ──
    all_ratings_cache: list | None = None

    if rating_min is not None:
        # Рейтинги загружаются только при запросе фильтра
        try:
            from .raexpert import _fetch_all_ratings, _RATING_ORDER as ro
            all_ratings_cache = _fetch_all_ratings()
            min_score = ro.get(rating_min.strip())
            if min_score is not None:
                rated_filtered = []
                for b in filtered:
                    rating = _match_rating(b.get("emitent", ""), all_ratings_cache)
                    b["rating"] = rating
                    if rating and rating != "отозван" and ro.get(rating, -1) >= min_score:
                        rated_filtered.append(b)
                filtered = rated_filtered
        except Exception:
            for b in filtered:
                b["rating"] = None
    else:
        for b in filtered:
            b["rating"] = None

    # Сектор — только если фильтр задан
    if sector:
        sector_filtered = []
        for b in filtered:
            s = _match_sector(b.get("emitent", ""))
            b["sector"] = s
            if s == sector:
                sector_filtered.append(b)
        filtered = sector_filtered
    else:
        for b in filtered:
            b["sector"] = None

    if emitent:
        emit_lower = emitent.lower()
        filtered = [b for b in filtered if emit_lower in (b.get("emitent") or "").lower()]

    # ── Шаг 5: ISQUALIFIEDINVESTORS (опционально, параллельно) ──
    if include_qualified:
        secids = [b["secid"] for b in filtered[:200]]  # ограничим 200 запросами
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(_fetch_bond_spec_qualified, sid): sid for sid in secids}
            results: dict[str, bool | None] = {}
            for f in as_completed(futs):
                sid = futs[f]
                try:
                    results[sid] = f.result()
                except Exception:
                    results[sid] = None
        for b in filtered:
            b["is_qualified"] = results.get(b["secid"])

        # Фильтр только для квалифицированных / неквалифицированных
        if qualified_only is True:
            filtered = [b for b in filtered if b.get("is_qualified") is True]
        elif qualified_only is False:
            filtered = [b for b in filtered if b.get("is_qualified") is False]

    # ── Шаг 6: сортировка ──
    _SORT_KEYS = {
        "ytm": "ytm",
        "duration": "duration_years",
        "maturity": "years_to_maturity",
        "price": "price_pct",
        "coupon": "coupon_pct",
        "issue_volume": "issue_size",
    }
    sort_field = _SORT_KEYS.get(sort_by, "ytm")

    def _sort_key(b: dict) -> float:
        v = b.get(sort_field)
        if v is None:
            return float("-inf") if sort_desc else float("inf")
        return float(v)

    filtered.sort(key=_sort_key, reverse=sort_desc)

    # ── Шаг 7: лимит + компактный вывод ──
    limited = filtered[:limit]

    bonds = [
        {"secname": b.get("secname") or b.get("shortname") or "", "isin": b["isin"]}
        for b in limited
        if b.get("isin")
    ]

    return {
        "count_total_matching": len(filtered),
        "count_all_bonds": len(all_bonds),
        "bonds": bonds,
    }


# ─────────────────── CCI (корпоративная информация НРД) ───────────────────

def company_info(query: str) -> dict:
    """Справка об организации по ИНН/ОГРН/названию.

    Ищет через /iss/securities.json (поисковый эндпоинт ISS).
    Возвращает {companies: [...]} с дедупликацией по emitent_id.

    query может быть ИНН, ОГРН, тикером или фрагментом названия.
    """
    raw = raw_get("securities", {"q": query, "limit": 200})
    rows = records(raw, "securities")
    if not rows:
        return {"companies": []}

    q = query.strip().upper()
    seen: dict[int, dict] = {}
    for r in rows:
        eid = r.get("emitent_id")
        if eid is None:
            continue
        if eid in seen:
            continue
        # запрос ISS и так отфильтровал, но на всякий случай проверяем
        name = str(r.get("name", "") or "").upper()
        shortname = str(r.get("shortname", "") or "").upper()
        inn = str(r.get("emitent_inn", "") or "").upper()
        title = str(r.get("emitent_title", "") or "").upper()
        if not (q in name or q in shortname or q in inn or q in title
                or q in str(eid)):
            continue
        seen[eid] = {
            "basis_company_id": eid,
            "inn": r.get("emitent_inn"),
            "name_short_ru": r.get("shortname"),
            "name_full_ru": r.get("emitent_title"),
            "okpo": r.get("emitent_okpo"),
            "secid": r.get("secid"),
        }
    return {"companies": list(seen.values())[:20]}


def company_info_by_id(company_id: int) -> dict:
    """Справка об организации по внутреннему ID (basis_company_id).

    Пытается сначала через /cci/info/companies/{id}, затем — через поиск
    securities по emitent_id. Возвращает {} если ничего не найдено.
    """
    # Попробуем CCI (поля могут быть пусты — ISS бывает)
    raw = raw_get(f"cci/info/companies/{company_id}")
    rows = records(raw, "cci_company")
    if rows and rows[0].get("name_short_ru"):
        return rows[0]

    # Фоллбэк: securities search + фильтр по emitent_id
    raw = raw_get("securities", {"limit": 200})
    for r in records(raw, "securities"):
        if r.get("emitent_id") == company_id:
            return {
                "basis_company_id": company_id,
                "inn": r.get("emitent_inn"),
                "name_short_ru": r.get("shortname"),
                "name_full_ru": r.get("emitent_title"),
                "okpo": r.get("emitent_okpo"),
                "secid": r.get("secid"),
            }
    return {}


def ir_calendar(limit: int = 50) -> list[dict]:
    """Календарь IR-мероприятий (даты отчётов публичных компаний).

    Возвращает: {company_name, event_type, event_date, event_link, ...}.
    """
    raw = raw_get("cci/calendars/ir-calendar", {"limit": limit})
    return records(raw, "cci_ir_calendar")


# ─────────────────── Статистика фондового рынка ───────────────────

def market_capitalization() -> dict:
    """Капитализация фондового рынка.

    Возвращает: {capitalization (₽), issuecapitalization (₽), ...}.
    """
    raw = raw_get("statistics/engines/stock/capitalization")
    caps = records(raw, "capitalization")
    issues = records(raw, "issuecapitalization")
    return {
        "capitalization": caps[0] if caps else None,
        "issuecapitalization": issues[0] if issues else None,
    }


_corr_cache: tuple[float, dict[str, list[dict]]] | None = None
_CORR_TTL = 3600  # 1 hour


def _fetch_correlations_page(start: int, retries: int = 4) -> list[dict]:
    url = f"{ISS}/statistics/engines/stock/markets/shares/correlations.json"
    params = {"iss.meta": "off", "limit": 1000, "start": start}
    last: Exception | None = None
    for i in range(retries):
        try:
            r = _requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            b = r.json().get("coefficients") or {}
            cols = b.get("columns") or []
            return [dict(zip(cols, row)) for row in (b.get("data") or [])]
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(0.5 * (i + 1))
    raise last  # type: ignore[misc]


def _load_correlations() -> dict[str, list[dict]]:
    """Fetch all correlations pages and group by SECID (with in-memory cache)."""
    global _corr_cache
    now = time.time()
    if _corr_cache is not None and now - _corr_cache[0] < _CORR_TTL:
        return _corr_cache[1]

    # Fetch first page + cursor metadata
    raw = raw_get("statistics/engines/stock/markets/shares/correlations",
                  {"limit": 1000, "iss.meta": "off"})
    cursor = (raw.get("coefficients.cursor") or {}).get("data") or [[0, 1000]]
    total = cursor[0][1]
    starts = list(range(1000, total, 1000))

    first_rows = records(raw, "coefficients")
    by_secid: dict[str, list[dict]] = {}
    for r in first_rows:
        key = (r.get("SECID") or "").upper()
        by_secid.setdefault(key, []).append(r)

    # Fetch remaining pages concurrently (8 workers is gentle enough for ISS)
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_correlations_page, s): s for s in starts}
        for f in as_completed(futs):
            for r in f.result():
                key = (r.get("SECID") or "").upper()
                by_secid.setdefault(key, []).append(r)

    _corr_cache = (time.time(), by_secid)
    return by_secid


def correlations(secid: str) -> list[dict]:
    """Коэффициенты корреляции и бета для бумаги.

    Вход: secid (напр. 'SBER'). Возвращает: [{secid, fxsecid, tradedate,
    coeff_correlation, coeff_beta}, ...] — все пары с другими бумагами.
    """
    by_secid = _load_correlations()
    return by_secid.get(secid.upper(), [])


def splits(secid: str | None = None) -> list[dict]:
    """Справочник дроблений и консолидаций бумаг.

    Вход: secid (опционально). Без параметра — все сплиты.
    Возвращает: {tradedate, secid, before, after}.
    """
    raw = raw_get("statistics/engines/stock/splits")
    rows = records(raw, "splits")
    if secid:
        rows = [r for r in rows if r.get("secid") == secid.upper()]
    return rows


# ─────────────────── Рынок облигаций ───────────────────

def bond_market_aggregates(frm: str | None = None,
                           till: str | None = None) -> list[dict]:
    """Агрегированные показатели рынка облигаций.

    Вход: frm/till ('YYYY-MM-DD', опционально).
    Возвращает: [{tradedate, type_bond, iss_nominal, vol_nominal, avg_years, ...}].
    Типы: корпоративные, ОФЗ, муниципальные и т.д.
    """
    params: dict = {"limit": 500}
    if frm:
        params["from"] = frm
    if till:
        params["till"] = till
    raw = raw_get("statistics/engines/stock/markets/bonds/aggregates", params)
    return records(raw, "aggregates")


def zcyc_history(frm: str, till: str) -> list[dict]:
    """История параметров КБД (Кривая Бескупонной Доходности).

    Вход: frm/till ('YYYY-MM-DD'). Возвращает: [{tradedate, b1,b2,b3, t1, g1...g9}].
    Параметры НСС-модели для каждого дня — для бэктестинга кривой.
    """
    raw = raw_get("history/engines/stock/zcyc",
                  {"from": frm, "till": till, "limit": 5000})
    return records(raw, "params")


# ─────────────────── Общая рыночная активность ───────────────────

def turnovers() -> list[dict]:
    """Сводные обороты по рынкам (биржевые итоги).

    Возвращает: [{name, valtoday, valtoday_usd, numtrades, updatetime, title}].
    Рынки: stock, currency, futures, commodity, ...
    """
    raw = raw_get("turnovers")
    return records(raw, "turnovers")


def sitenews(limit: int = 20) -> list[dict]:
    """Новости Московской биржи.

    Вход: limit. Возвращает: [{id, tag, title, published_at, modified_at}].
    """
    raw = raw_get("sitenews", {"limit": limit})
    return records(raw, "sitenews")


def aggregates(query: str, date: str) -> dict:
    """Агрегированные итоги торгов за дату по бумаге.

    Вход: query (тикер), date ('YYYY-MM-DD').
    Возвращает: {securities: [...], marketdata: [...]} — полные итоги дня.
    """
    r = resolve(query)
    raw = raw_get(
        f"engines/{r['engine']}/markets/{r['market']}"
        f"/securities/{r['secid']}/aggregates",
        {"date": date})
    return {
        "securities": records(raw, "securities"),
        "marketdata": records(raw, "marketdata"),
    }


def price_volatility(query: str, days: int = 90, rf_annual: float = 16.0) -> dict:
    """Волатильность, Sharpe, MaxDD по дневным свечам за N дней.

    rf_annual — безрисковая ставка (% годовых, по умолчанию текущая ключевая ≈16%).
    Возвращает: {days, annual_vol_pct, daily_vol_pct, sharpe, max_drawdown_pct,
    total_return_pct, high_price, low_price, high_date, low_date, close_start, close_end}.
    """
    from datetime import date, timedelta
    till = date.today()
    frm = till - timedelta(days=days + 10)  # запас на выходные/праздники
    r = resolve(query)
    raw = exec_template(T_CANDLES, {
        "engine": r["engine"], "market": r["market"],
        "board": r["board"], "security": r["secid"]},
        {"from": str(frm), "till": str(till), "interval": "24"})
    rows = records(raw, "candles")
    if len(rows) < 2:
        return {"error": "недостаточно данных", "rows_found": len(rows)}
    closes = [row["close"] for row in rows if row.get("close")]
    if len(closes) < 2:
        return {"error": "недостаточно close-цен", "rows_found": len(closes)}

    # дневные лог-доходности
    import math
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes))
            if closes[i - 1] and closes[i]]
    if not rets:
        return {"error": "нет доходностей"}

    n = len(rets)
    mean_r = sum(rets) / n
    var = sum((r - mean_r) ** 2 for r in rets) / (n - 1) if n > 1 else 0
    daily_vol = var ** 0.5
    ann_vol = daily_vol * (252 ** 0.5)

    # Sharpe
    rf_daily = math.log(1 + rf_annual / 100) / 252
    excess_mean = mean_r - rf_daily
    sharpe = round(excess_mean / daily_vol * (252 ** 0.5), 2) if daily_vol else 0

    # Max drawdown
    peak = closes[0]
    max_dd = 0.0
    high_price = closes[0]
    low_price = closes[0]
    high_idx = low_idx = 0
    for i, c in enumerate(closes):
        if c > peak:
            peak = c
        dd = (peak - c) / peak
        if dd > max_dd:
            max_dd = dd
        if c > high_price:
            high_price = c
            high_idx = i
        if c < low_price:
            low_price = c
            low_idx = i

    total_ret = (closes[-1] / closes[0] - 1) * 100

    return {
        "days": days,
        "period_start": rows[0].get("begin", ""),
        "period_end": rows[-1].get("begin", ""),
        "close_start": closes[0],
        "close_end": closes[-1],
        "total_return_pct": round(total_ret, 2),
        "daily_vol_pct": round(daily_vol * 100, 2),
        "annual_vol_pct": round(ann_vol * 100, 2),
        "sharpe": sharpe,
        "max_drawdown_pct": round(max_dd * 100, 2),
        "high_price": high_price,
        "high_date": rows[high_idx].get("begin", ""),
        "low_price": low_price,
        "low_date": rows[low_idx].get("begin", ""),
        "rf_used_pct": rf_annual,
        "trading_days": len(closes),
    }


def _median(vals: list[float]) -> float:
    s = sorted(vals)
    n = len(s)
    if n == 0:
        return 0.0
    mid = n // 2
    return (s[mid] + s[mid - 1]) / 2 if n % 2 == 0 else s[mid]


def liquidity(query: str, days: int = 90) -> dict:
    """Единая оценка ликвидности бумаги: Amihud illiquidity, спред, оборот, скор 0-10.

    Источники: дневные свечи (OHLCV) + текущий bid/ask через котировку.
    Корень-история: moex.py — функция candles/_marketdata_row.
    """
    import math
    from datetime import date, timedelta

    till = date.today()
    frm = till - timedelta(days=days + 10)
    r = resolve(query)
    raw = exec_template(T_CANDLES, {
        "engine": r["engine"], "market": r["market"],
        "board": r["board"], "security": r["secid"]},
        {"from": str(frm), "till": str(till), "interval": "24"})
    rows = records(raw, "candles")
    if not rows:
        return {"error": "нет данных", "secid": r["secid"]}

    closes: list[float] = []
    ruble_turnover: list[float] = []
    lot_turnover: list[float] = []
    zero_vol_days = 0
    for row in rows:
        c = row.get("close")
        v = row.get("value") or 0
        lo = row.get("volume") or 0
        if c and c > 0:
            closes.append(c)
        ruble_turnover.append(v)
        lot_turnover.append(lo)
        if lo == 0:
            zero_vol_days += 1

    # Дневные лог-доходности
    rets = [math.log(closes[i] / closes[i - 1])
            for i in range(1, len(closes))
            if closes[i - 1] > 0 and closes[i] > 0]

    # Текущий bid/ask (реальный спред стакана)
    try:
        r_q = resolve(query)
        md = _marketdata_row(r_q["secid"], r_q["engine"], r_q["market"], r_q["board"])
        bid = md.get("BID")
        ask = md.get("OFFER")
    except Exception:  # noqa: BLE001
        bid = ask = None

    n_trading = len(rows)
    last_close = closes[-1] if closes else 0

    # 1) Средний суточный оборот, ₽ и лотов
    avg_ruble = round(sum(ruble_turnover) / n_trading, 0) if n_trading else 0
    avg_lots = round(sum(lot_turnover) / n_trading, 0) if n_trading else 0

    # 2) Фактический календарный охват
    first_date = rows[0].get("begin", "")
    last_date = rows[-1].get("begin", "")
    try:
        from datetime import datetime as _dt
        span = (_dt.fromisoformat(str(last_date)[:10]) -
                _dt.fromisoformat(str(first_date)[:10])).days + 1
    except Exception:  # noqa: BLE001
        span = days
    trading_day_ratio = round(n_trading / max(span, 1), 2)

    # 3) Amihud illiquidity ratio (bps per 1M ₽ avg daily turnover)
    #    = mean(|r_t|/V_t) × 10^6 × 10^4, V_t в рублях → bps на 1 млн₽
    if rets:
        pairs = list(zip(rets, ruble_turnover[1:]))
        n_valid = max(1, len(pairs))
        amihud_ratio = sum(abs(r) / v for r, v in pairs if v > 0) / n_valid
        amihud_bps = round(amihud_ratio * 1e10, 2)
    else:
        amihud_bps = None

    # 4) Спред — оценка из OHLC (модифицированный Corwin-Schultz) + реальный bid/ask
    spread_estimates: list[dict] = []
    if len(rows) >= 2:
        betas = []
        for i in range(1, len(rows)):
            h0 = rows[i - 1].get("high") or 0
            l0 = rows[i - 1].get("low") or 0
            h1 = rows[i].get("high") or 0
            l1 = rows[i].get("low") or 0
            if h0 > 0 and l0 > 0 and h1 > 0 and l1 > 0:
                g0 = math.log(h0 / l0)
                g1 = math.log(h1 / l1)
                betas.append((g0 * g1 + g0 * g0) / 2)
        if betas:
            beta_coef = sum(betas) / len(betas)
            beta_coef = max(0.0, min(beta_coef, 3.0))
            raw_spread = math.expm1(2 * beta_coef)
            hl_spread_pct = max(0.0, raw_spread * 100)
            if last_close > 0:
                spread_estimates.append({
                    "method": "hl_proxy",
                    "spread_pct": round(hl_spread_pct, 3),
                    "spread_rub": round(hl_spread_pct / 100 * last_close, 3),
                })
    if bid is not None and ask is not None:
        mid = (bid + ask) / 2
        if mid > 0:
            ba_pct = round((ask - bid) / mid * 100, 3)
            spread_estimates.append({
                "method": "bid_ask",
                "spread_pct": ba_pct,
                "spread_rub": round(ask - bid, 3),
            })

    # Итоговый спред
    if spread_estimates:
        main_spread_pct = round(_median([s["spread_pct"] for s in spread_estimates]), 3)
        main_spread_rub = round(_median([s["spread_rub"] for s in spread_estimates]), 3)
    else:
        main_spread_pct = None
        main_spread_rub = None

    # Композитный скор: turnover (weight 0.5) + Amihud (weight 0.5)
    def _turnover_score(avg: float) -> float:
        return max(0.0, min(10.0, math.log10(max(avg, 1)) / math.log10(1e10) * 10))

    def _amihud_score(ah: float | None) -> float | None:
        if ah is None or ah < 0.01:
            return None
        # ah = bps_per_mln₽. Скор: 0.01(liquid)→10, 1→9.7, 10→7.6, 100→3.2, 1000→0
        # k4 = 10 / (log10(1000) − (−2)) ≈ 1.67 → монотонно от ah=0.01 до ah→∞
        return max(0.0, min(10.0,
            10 - (math.log10(max(ah, 0.01)) + 2) * 1.67))

    metrics: list[tuple[float, str]] = []
    metrics.append((_turnover_score(avg_ruble), "turnover"))
    a_score = _amihud_score(amihud_bps)
    if a_score is not None:
        metrics.append((a_score, "amihud"))

    if not metrics:
        return {
            "secid": r["secid"],
            "error": "недостаточно данных для оценки",
            "period_start": rows[0].get("begin", ""),
            "period_end": rows[-1].get("begin", ""),
            "trading_days": n_trading,
            "avg_daily_turnover_rub": 0,
            "avg_daily_volume_lots": 0,
        }

    score = round(sum(v for v, _ in metrics) / len(metrics), 1)
    if score >= 8:
        grade = "A (отличная)"
    elif score >= 6:
        grade = "B (хорошая)"
    elif score >= 4:
        grade = "C (умеренная)"
    elif score >= 2:
        grade = "D (низкая)"
    else:
        grade = "E (очень низкая)"

    return {
        "secid": r["secid"],
        "period_start": rows[0].get("begin", ""),
        "period_end": rows[-1].get("begin", ""),
        "trading_days": n_trading,
        "calendar_days": days,
        "trading_day_ratio": trading_day_ratio,
        "zero_volume_days": zero_vol_days,
        "avg_daily_turnover_rub": avg_ruble,
        "avg_daily_volume_lots": avg_lots,
        "amihud_bps_per_mln": amihud_bps,
        "spread": main_spread_pct,
        "spread_rub": main_spread_rub,
        "spread_sources": spread_estimates,
        "composite_score": score,
        "grade": grade,
        "note": ("Amihud = mean(|r_t|/V_t) × 10^10 (bps per 1M₽). "
                 "Спред: оценка из OHLC (Corwin-Schultz) + фактический bid/ask. "
                 "Скор: 0-10 (выше = ликвиднее)."),
    }


# ─────────────────── ETF / БПИФ ───────────────────

# Маппинг тикер БПИФ → {benchmark, benchmark_name, category, inav_hint}.
# benchmark — тикер индекса-бенчмарка на MOEX, category — тип фонда.
ETF_BENCHMARK_MAP: dict[str, dict] = {
    "TMOS": {"benchmark": "IMOEX",  "benchmark_name": "Индекс МосБиржи",  "category": "equity_russia"},
    "SBMX": {"benchmark": "MOEXTR", "benchmark_name": "MOEX Total Return","category": "equity_russia"},
    "TGLD": {"benchmark": "GOLD",   "benchmark_name": "Золото (ЦБ)",      "category": "commodity"},
    "TEUR": {"benchmark": "EUR/RUB","benchmark_name": "EUR/RUB (ЦБ)",     "category": "fx"},
    "TCNY": {"benchmark": "CNY/RUB","benchmark_name": "CNY/RUB (ЦБ)",     "category": "fx"},
    "TMON": {"benchmark": None,     "benchmark_name": "Денежный рынок",    "category": "money_market"},
    "SBMM": {"benchmark": None,     "benchmark_name": "Денежный рынок",    "category": "money_market"},
    "AKMM": {"benchmark": None,     "benchmark_name": "Денежный рынок",    "category": "money_market"},
    "TPAY": {"benchmark": "RUCBTRNSD", "benchmark_name": "RGBITR (гос. облигации)", "category": "bond_gov"},
    "VTBA": {"benchmark": "RUCBTRRNDX","benchmark_name": "RGBI (облигации)",        "category": "bond_gov"},
    "VTBE": {"benchmark": "RGBI",   "benchmark_name": "RGBI",             "category": "bond_gov"},
    "RUSB": {"benchmark": "RUCBTRNSD","benchmark_name": "RGBITR",         "category": "bond_gov"},
    "SBGB": {"benchmark": "RUCBTRNSD","benchmark_name": "RGBITR",         "category": "bond_gov"},
    "SBGD": {"benchmark": "RUCBTRNSD","benchmark_name": "RGBITR",         "category": "bond_gov"},
    "AKBB": {"benchmark": "RUCBTRNSD","benchmark_name": "RGBITR",         "category": "bond_gov"},
    "TLCB": {"benchmark": None,     "benchmark_name": "Ликвидность ЦБ",    "category": "money_market"},
    "AKTS": {"benchmark": "IMOEX",  "benchmark_name": "Индекс МосБиржи",  "category": "equity_russia"},
    "AKGD": {"benchmark": None,     "benchmark_name": "Золото (ЦБ)",      "category": "commodity"},
    "FXRU": {"benchmark": "RUCBTRNSD","benchmark_name": "RGBITR",         "category": "bond_gov"},
    "FXMM": {"benchmark": None,     "benchmark_name": "Денежный рынок",    "category": "money_market"},
    "FXRB": {"benchmark": "RUCBTRRNDX","benchmark_name": "RGBI",          "category": "bond_corp"},
    "FXGD": {"benchmark": "GOLD",   "benchmark_name": "Золото (LBMA)",    "category": "commodity"},
    "FXCN": {"benchmark": "IMOEX",  "benchmark_name": "Индекс МосБиржи",  "category": "equity_russia"},
    "FXIT": {"benchmark": "IMOEX",  "benchmark_name": "Индекс МосБиржи",  "category": "equity_russia"},
    "FXRL": {"benchmark": "RTSI",   "benchmark_name": "Индекс РТС",       "category": "equity_russia"},
    "FXUS": {"benchmark": "S&P 500","benchmark_name": "S&P 500",          "category": "equity_foreign"},
    "FXDE": {"benchmark": "DAX",    "benchmark_name": "DAX",              "category": "equity_foreign"},
    "FXUK": {"benchmark": "FTSE 100","benchmark_name": "FTSE 100",        "category": "equity_foreign"},
    "FXKZ": {"benchmark": None,     "benchmark_name": "Казахстан",        "category": "equity_foreign"},
    "FXWO": {"benchmark": "MSCI World","benchmark_name": "MSCI World",    "category": "equity_foreign"},
    "FXIM": {"benchmark": "IMOEX",  "benchmark_name": "Индекс МосБиржи",  "category": "equity_russia"},
    "TMOS": {"benchmark": "IMOEX",  "benchmark_name": "Индекс МосБиржи",  "category": "equity_russia"},
    "TBRU": {"benchmark": "IMOEX",  "benchmark_name": "Индекс МосБиржи",  "category": "equity_russia"},
    "TECH": {"benchmark": "MOEXT",  "benchmark_name": "MOEX IT",          "category": "equity_sector"},
    "DSPB": {"benchmark": None,     "benchmark_name": "Дивидендный",       "category": "equity_dividend"},
    "DIVD": {"benchmark": None,     "benchmark_name": "Дивидендный",       "category": "equity_dividend"},
    "TITR": {"benchmark": "MOEXT",  "benchmark_name": "MOEX IT",          "category": "equity_sector"},
    "OPTE": {"benchmark": None,     "benchmark_name": "Оптимум",           "category": "mixed"},
    "KAGG": {"benchmark": "MOEXTR", "benchmark_name": "MOEX Total Return","category": "equity_russia"},
    "CATF": {"benchmark": None,     "benchmark_name": "Кэш-менеджмент",   "category": "money_market"},
}

# Человекочитаемые названия категорий БПИФ
ETF_CATEGORY_RU: dict[str, str] = {
    "equity_russia": "Акции (Россия)",
    "equity_foreign": "Акции (зарубежные)",
    "equity_sector": "Акции (секторальные)",
    "equity_dividend": "Акции (дивидендные)",
    "bond_gov": "Облигации (государственные)",
    "bond_corp": "Облигации (корпоративные)",
    "money_market": "Денежный рынок",
    "commodity": "Сырьё",
    "fx": "Валюта",
    "mixed": "Смешанный",
}


def _resolve_inav_ticker(fund_secid: str) -> str | None:
    """Найти iNAV-тикер для БПИФ.

    Каждый БПИФ имеет iNAV-спутник (TMOS→TMOSA, SBMX→SBMXA) на доске INAV.
    Пробуем: {secid}A → {secid}B → поиск по названию.
    """
    for suffix in ("A", "B"):
        candidate = f"{fund_secid}{suffix}"
        try:
            r = _resolve_single(candidate)
            if r.get("board") == "INAV":
                return candidate
        except (ValueError, Exception):
            pass
    # Фоллбэк: поиск iNAV по названию фонда
    try:
        raw = exec_template(T_SEARCH, {"q": fund_secid, "limit": 50})
        rows = records(raw, "securities")
        for row in rows:
            bid = row.get("primary_boardid") or ""
            sid = (row.get("secid") or "").upper()
            if bid == "INAV" and sid.startswith(fund_secid.upper()):
                return row["secid"]
    except Exception:
        pass
    return None


def _resolve_single(query: str) -> dict:
    """Резолв одной бумаги без word-stripping (для iNAV)."""
    raw = exec_template(T_SEARCH, {"q": query, "limit": 10})
    rows = records(raw, "securities")
    if not rows:
        raise ValueError(f"MOEX: не найдено бумаг по запросу {query!r}")
    def _score(r: dict) -> int:
        s = 0
        if str(r.get("secid", "")).upper() == query.upper():
            s += 100
        if r.get("is_traded") == 1:
            s += 10
        return s
    best = max(rows, key=_score)
    engine, market = _engine_market(best.get("group"))
    return {
        "secid": best.get("secid"),
        "shortname": best.get("shortname"),
        "isin": best.get("isin"),
        "engine": engine, "market": market,
        "board": best.get("primary_boardid"),
        "type": best.get("type"),
        "group": best.get("group"),
        "is_traded": best.get("is_traded"),
        "emitent_id": best.get("emitent_id"),
    }


def inav_quote(fund_secid: str) -> dict | None:
    """Котировка iNAV-инструмента (индикативная чистая стоимость фонда).

    iNAV — спутниковый инструмент на доске INAV (engine=stock, market=index).
    TMOS→TMOSA, SBMX→SBMXA и т.д.

    Возвращает: {secid, price, updatetime} или None если iNAV не найден.
    """
    inav_ticker = _resolve_inav_ticker(fund_secid)
    if not inav_ticker:
        return None
    try:
        # Прямой запрос на доску INAV — resolve() не находит iNAV-инструменты
        raw = exec_template(T_QUOTE_BOARD, {
            "engine": "stock", "market": "index", "board": "INAV",
            "security": inav_ticker})
        md_rows = records(raw, "marketdata")
        md = md_rows[0] if md_rows else {}
        price = first(md.get("LAST"), md.get("MARKETPRICE"),
                      md.get("LCLOSEPRICE"), md.get("WAPRICE"))
        return {
            "secid": inav_ticker,
            "price": price,
            "updatetime": md.get("UPDATETIME"),
            "price_field": ("LAST" if md.get("LAST") is not None else
                            "MARKETPRICE" if md.get("MARKETPRICE") is not None else
                            "LCLOSEPRICE" if md.get("LCLOSEPRICE") is not None else "WAPRICE"),
        }
    except Exception:
        return None


def etf_fund_data(query: str) -> dict:
    """Расширенные данные о БПИФ: iNAV, бенчмарк, тип, категория.

    Возвращает: {secid, shortname, isin, group, type, issuedate, emitent_id,
    lozsize, list_level, is_qualified, category, category_ru, benchmark,
    benchmark_name, inav_ticker, inav_price, fund_price, premium_discount_pct}.
    """
    r = resolve(query)
    is_fund = (r.get("group") or "").endswith(("_ppif", "_etf"))
    if not is_fund:
        # Проверяем, может это всё-таки фонд
        if r.get("group") not in ("stock_ppif", "stock_etf"):
            raise ValueError(f"{query!r} не является БПИФ/ETF (group={r.get('group')})")

    # Спецификация бумаги
    spec_raw = exec_template(T_SPEC, {"security": r["secid"]})
    spec = {}
    for block in ("description", "securities"):
        rows_b = records(spec_raw, block)
        if rows_b:
            spec = rows_b[0]
            break

    # iNAV
    inav_ticker = _resolve_inav_ticker(r["secid"])
    inav = inav_quote(r["secid"]) if inav_ticker else None

    # Котировка фонда
    q = quote(r["secid"])
    fund_price = q.get("price")

    # Премия/дисконт
    premium = None
    if inav and inav.get("price") and fund_price and inav["price"] > 0:
        premium = round((fund_price / inav["price"] - 1) * 100, 2)

    # Маппинг бенчмарка
    bm = ETF_BENCHMARK_MAP.get(r["secid"], {})
    category = bm.get("category", "unknown")
    category_ru = ETF_CATEGORY_RU.get(category, category)

    return {
        "secid": r["secid"],
        "shortname": r.get("shortname"),
        "isin": r.get("isin"),
        "group": r.get("group"),
        "type": r.get("type"),
        "board": r.get("board"),
        "issuedate": spec.get("ISSUEDATE"),
        "emitent_id": r.get("emitent_id"),
        "emitent_title": r.get("emitent_title"),
        "lotsize": spec.get("LOTSIZE"),
        "list_level": spec.get("LISTLEVEL"),
        "is_qualified": spec.get("ISQUALIFIEDINVESTORS"),
        "category": category,
        "category_ru": category_ru,
        "benchmark": bm.get("benchmark"),
        "benchmark_name": bm.get("benchmark_name"),
        "inav_ticker": inav_ticker,
        "inav_price": inav.get("price") if inav else None,
        "inav_updatetime": inav.get("updatetime") if inav else None,
        "fund_price": fund_price,
        "premium_discount_pct": premium,
        "price_field": q.get("price_field"),
    }


def etf_premium_discount(query: str) -> dict:
    """Текущая премия/дисконт БПИФ к iNAV.

    Сравнивает рыночную цену фонда с индикативной чистой стоимостью (iNAV).
    iNAV — спутниковый инструмент на доске INAV (TMOS→TMOSA).

    Возвращает: {secid, fund_price, inav_ticker, inav_price, premium_discount_pct,
    inav_updatetime, note}.
    """
    r = resolve(query)
    is_fund = (r.get("group") or "").endswith(("_ppif", "_etf"))
    if not is_fund:
        raise ValueError(f"{query!r} не является БПИФ/ETF")

    inav = inav_quote(r["secid"])
    q = quote(r["secid"])
    fund_price = q.get("price")

    if not inav or not inav.get("price"):
        return {
            "secid": r["secid"],
            "shortname": r.get("shortname"),
            "fund_price": fund_price,
            "inav_ticker": inav.get("secid") if inav else None,
            "inav_price": None,
            "premium_discount_pct": None,
            "note": "iNAV не найден или не содержит данных — расчёт невозможен",
        }

    inav_price = inav["price"]
    if inav_price > 0 and fund_price:
        premium = round((fund_price / inav_price - 1) * 100, 2)
    else:
        premium = None

    return {
        "secid": r["secid"],
        "shortname": r.get("shortname"),
        "fund_price": fund_price,
        "price_field": q.get("price_field"),
        "inav_ticker": inav.get("secid"),
        "inav_price": inav_price,
        "inav_updatetime": inav.get("updatetime"),
        "premium_discount_pct": premium,
        "note": ("положительное = премия (рыночная цена выше NAV), "
                 "отрицательное = дисконт. Задержка ~15 мин."),
    }


def etf_tracking_error(query: str, days: int = 90) -> dict:
    """Трекинг-ошибка БПИФ относительно индекса-бенчмарка.

    Сравнивает доходность фонда и бенчмарка за N дней.
    Бенчмарк определяется из ETF_BENCHMARK_MAP; если не найден — ищет в ISS
    MOEX по названию фонда.

    Возвращает: {secid, benchmark, benchmark_name, period_days, fund_return_pct,
    benchmark_return_pct, tracking_error_pct (annualized), premium_avg_pct, dates}.
    """
    from datetime import date, timedelta

    r = resolve(query)
    is_fund = (r.get("group") or "").endswith(("_ppif", "_etf"))
    if not is_fund:
        raise ValueError(f"{query!r} не является БПИФ/ETF")

    till = date.today()
    frm = till - timedelta(days=days + 10)

    # 1) Свечи фонда
    fund_candles = exec_template(T_CANDLES, {
        "engine": r["engine"], "market": r["market"],
        "board": r["board"], "security": r["secid"]},
        {"from": str(frm), "till": str(till), "interval": "24"})
    fund_rows = records(fund_candles, "candles")
    if len(fund_rows) < 2:
        return {"error": "недостаточно данных по фонду", "secid": r["secid"]}

    fund_closes = [row["close"] for row in fund_rows if row.get("close")]
    fund_dates = [row.get("begin", "") for row in fund_rows]

    # 2) Определяем бенчмарк
    bm_info = ETF_BENCHMARK_MAP.get(r["secid"], {})
    benchmark_query = bm_info.get("benchmark")
    benchmark_name = bm_info.get("benchmark_name", "неизвестен")

    if not benchmark_query:
        return {
            "error": "бенчмарк не определён для данного фонда",
            "secid": r["secid"],
            "fund_return_pct": round((fund_closes[-1] / fund_closes[0] - 1) * 100, 2) if fund_closes else None,
            "period_days": days,
            "note": "добавьте маппинг в ETF_BENCHMARK_MAP",
        }

    # 3) Свечи бенчмарка
    try:
        bm_r = resolve(benchmark_query)
        bm_candles = exec_template(T_CANDLES, {
            "engine": bm_r["engine"], "market": bm_r["market"],
            "board": bm_r["board"], "security": bm_r["secid"]},
            {"from": str(frm), "till": str(till), "interval": "24"})
        bm_rows = records(bm_candles, "candles")
    except Exception:
        return {
            "error": f"не удалось получить данные бенчмарка {benchmark_query!r}",
            "secid": r["secid"], "benchmark": benchmark_query,
        }

    if len(bm_rows) < 2:
        return {
            "error": "недостаточно данных по бенчмарку",
            "secid": r["secid"], "benchmark": benchmark_query,
            "benchmark_rows": len(bm_rows),
        }

    bm_closes = [row["close"] for row in bm_rows if row.get("close")]

    # 4) Выравниваем по датам
    fund_by_date: dict[str, float] = {}
    for row in fund_rows:
        dt = (row.get("begin") or "")[:10]
        c = row.get("close")
        if dt and c:
            fund_by_date[dt] = c

    bm_by_date: dict[str, float] = {}
    for row in bm_rows:
        dt = (row.get("begin") or "")[:10]
        c = row.get("close")
        if dt and c:
            bm_by_date[dt] = c

    common_dates = sorted(set(fund_by_date) & set(bm_by_date))
    if len(common_dates) < 2:
        return {
            "error": "нет пересечения дат фонда и бенчмарка",
            "secid": r["secid"], "benchmark": benchmark_query,
            "fund_dates": len(fund_by_date), "bm_dates": len(bm_by_date),
        }

    aligned_fund = [fund_by_date[d] for d in common_dates]
    aligned_bench = [bm_by_date[d] for d in common_dates]

    # 5) Доходности
    fund_ret = (aligned_fund[-1] / aligned_fund[0] - 1) * 100
    bm_ret = (aligned_bench[-1] / aligned_bench[0] - 1) * 100

    # 6) Tracking error (annualized): std(daily_diff) × sqrt(252)
    import math
    daily_diffs: list[float] = []
    for i in range(1, len(common_dates)):
        if aligned_fund[i-1] > 0 and aligned_bench[i-1] > 0:
            r_fund = math.log(aligned_fund[i] / aligned_fund[i-1])
            r_bench = math.log(aligned_bench[i] / aligned_bench[i-1])
            daily_diffs.append(r_fund - r_bench)

    tracking_err = None
    if len(daily_diffs) >= 2:
        mean_d = sum(daily_diffs) / len(daily_diffs)
        var_d = sum((d - mean_d) ** 2 for d in daily_diffs) / (len(daily_diffs) - 1)
        tracking_err = round(var_d ** 0.5 * (252 ** 0.5) * 100, 2)

    # 7) Средняя премия/дисконт за период (если есть iNAV)
    premium_avg = None
    inav = inav_quote(r["secid"])

    return {
        "secid": r["secid"],
        "shortname": r.get("shortname"),
        "benchmark": benchmark_query,
        "benchmark_name": benchmark_name,
        "period_days": (datetime.fromisoformat(common_dates[-1][:10])
                        - datetime.fromisoformat(common_dates[0][:10])).days,
        "start_date": common_dates[0],
        "end_date": common_dates[-1],
        "trading_days": len(common_dates),
        "fund_return_pct": round(fund_ret, 2),
        "benchmark_return_pct": round(bm_ret, 2),
        "excess_return_pct": round(fund_ret - bm_ret, 2),
        "tracking_error_ann_pct": tracking_err,
        "inav_ticker": inav.get("secid") if inav else None,
        "note": ("трекинг-ошибка = annualized σ(r_fund − r_bench). "
                 "Excess return = доходность фонда − доходность бенчмарка."),
    }


# ─────────────────── ETF Screener ───────────────────

# Борды для загрузки всех БПИФ/ETF
_ETF_BOARDS = ["TQIF", "TQTF", "TQFD", "TQFE", "TQTD", "TQTE"]


def _fetch_board_etf(board: str, retries: int = 4) -> list[dict]:
    """Загрузить все фонды с борда (securities + marketdata) за один запрос.

    Возвращает список merged-словарей (securities + marketdata + _board).
    """
    path = f"engines/stock/markets/shares/boards/{board}/securities"
    raw = raw_get(path, {"iss.only": "securities,marketdata"}, retries=retries)
    secs = records(raw, "securities")
    mds = records(raw, "marketdata")
    md_by_secid: dict[str, dict] = {}
    for m in mds:
        sid = m.get("SECID")
        if sid:
            md_by_secid[sid] = m
    out: list[dict] = []
    for s in secs:
        sid = s.get("SECID")
        s["_board"] = board
        if sid and sid in md_by_secid:
            s.update(md_by_secid[sid])
        out.append(s)
    return out


def technical_indicators(query: str, days: int = 90) -> dict:
    """Рассчитать технические индикаторы из дневных свечей (OHLCV).

    Args:
        query — тикер или ISIN.
        days — период для расчёта (default 90). Для MA200/Ichimoku нужно 200+ дней.

    Returns:
        {secid, period_days, trading_days,
         rsi_14, stochastic_k, stochastic_d,
         ma_50, ma_200, ma_signal, ema_12, ema_26,
         macd, macd_signal_line, macd_histogram, macd_signal,
         bollinger_upper, bollinger_middle, bollinger_lower, bollinger_width, bollinger_pct,
         adx, plus_di, minus_di, trend_strength,
         atr_14,
         obv, obv_trend,
         vwap,
         cci_20,
         williams_r,
         ichimoku_tenkan, ichimoku_kijun, ichimoku_senkou_a, ichimoku_senkou_b, ichimoku_chikou, ichimoku_signal,
         psar, psar_direction,
         pivot, pivot_s1, pivot_s2, pivot_s3, pivot_r1, pivot_r2, pivot_r3,
         fib_236, fib_382, fib_500, fib_618, fib_786, fib_high, fib_low,
         momentum_10, roc_10,
         cmf_20, cmf_signal}
    """
    from datetime import date as _date, timedelta

    r = resolve(query)
    till = _date.today()
    frm = till - timedelta(days=days + 30)

    raw_candles = exec_template(T_CANDLES, {
        "engine": r["engine"], "market": r["market"],
        "board": r["board"], "security": r["secid"]},
        {"from": str(frm), "till": str(till), "interval": "24"})
    rows = records(raw_candles, "candles")

    closes = [row["close"] for row in rows if row.get("close")]
    if len(closes) < 14:
        return {"error": "недостаточно данных", "secid": r["secid"], "trading_days": len(closes)}

    opens = [row["open"] for row in rows if row.get("open")]
    highs = [row["high"] for row in rows if row.get("high")]
    lows = [row["low"] for row in rows if row.get("low")]
    volumes = [row["volume"] for row in rows if row.get("volume")]

    result: dict = {
        "secid": r["secid"],
        "period_days": days,
        "trading_days": len(closes),
    }

    # ══════════════ Вспомогательные функции ══════════════

    def _sma(data: list[float], period: int) -> float | None:
        if len(data) < period:
            return None
        return round(sum(data[-period:]) / period, 6)

    def _ema_series(data: list[float], period: int) -> list[float]:
        if len(data) < period:
            return []
        multiplier = 2 / (period + 1)
        ema = [sum(data[:period]) / period]
        for i in range(period, len(data)):
            ema.append(data[i] * multiplier + ema[-1] * (1 - multiplier))
        return ema

    def _wilder_smooth(data: list[float], period: int) -> list[float]:
        if len(data) < period:
            return []
        result_ws = [sum(data[:period]) / period]
        for i in range(period, len(data)):
            result_ws.append((result_ws[-1] * (period - 1) + data[i]) / period)
        return result_ws

    # ══════════════ RSI(14) — Wilder's smoothed ══════════════
    rsi_period = 14
    if len(closes) >= rsi_period + 1:
        gains = []
        losses = []
        for i in range(1, len(closes)):
            delta = closes[i] - closes[i - 1]
            gains.append(max(0, delta))
            losses.append(max(0, -delta))

        avg_gain = sum(gains[:rsi_period]) / rsi_period
        avg_loss = sum(losses[:rsi_period]) / rsi_period

        for i in range(rsi_period, len(gains)):
            avg_gain = (avg_gain * (rsi_period - 1) + gains[i]) / rsi_period
            avg_loss = (avg_loss * (rsi_period - 1) + losses[i]) / rsi_period

        if avg_loss == 0:
            result["rsi_14"] = 100.0
        else:
            rs = avg_gain / avg_loss
            result["rsi_14"] = round(100 - 100 / (1 + rs), 2)

    # ══════════════ Stochastic %K/%D(14, 3, 3) ══════════════
    stoch_period = 14
    if len(closes) >= stoch_period and len(highs) >= stoch_period and len(lows) >= stoch_period:
        stoch_k_values = []
        for i in range(stoch_period - 1, len(closes)):
            window_h = highs[i - stoch_period + 1:i + 1]
            window_l = lows[i - stoch_period + 1:i + 1]
            hh = max(window_h)
            ll = min(window_l)
            if hh == ll:
                stoch_k_values.append(50.0)
            else:
                stoch_k_values.append((closes[i] - ll) / (hh - ll) * 100)

        if len(stoch_k_values) >= 3:
            stoch_k = round(sum(stoch_k_values[-3:]) / 3, 2)
            result["stochastic_k"] = stoch_k
            if len(stoch_k_values) >= 5:
                # %D = SMA(3) of %K series
                d_vals = []
                for i in range(2, len(stoch_k_values)):
                    d_vals.append(sum(stoch_k_values[i - 2:i + 1]) / 3)
                result["stochastic_d"] = round(d_vals[-1], 2)

    # ══════════════ SMA / EMA ══════════════
    result["ma_50"] = _sma(closes, 50)
    result["ma_200"] = _sma(closes, 200)

    ema12_series = _ema_series(closes, 12)
    ema26_series = _ema_series(closes, 26)
    if ema12_series:
        result["ema_12"] = round(ema12_series[-1], 4)
    if ema26_series:
        result["ema_26"] = round(ema26_series[-1], 4)

    # MA signal (Golden/Death Cross)
    ma50 = result["ma_50"]
    ma200 = result["ma_200"]
    if ma50 is not None and ma200 is not None:
        result["ma_signal"] = "golden_cross" if ma50 > ma200 else "death_cross"
    else:
        result["ma_signal"] = "insufficient_data"

    # ══════════════ MACD(12, 26, 9) ══════════════
    if len(ema12_series) >= 26 and len(ema26_series) >= 9:
        offset = len(ema12_series) - len(ema26_series)
        macd_line = [ema12_series[offset + i] - ema26_series[i] for i in range(len(ema26_series))]

        if len(macd_line) >= 9:
            signal_line = _ema_series(macd_line, 9)
            histogram = macd_line[-1] - signal_line[-1]

            result["macd"] = round(macd_line[-1], 4)
            result["macd_signal_line"] = round(signal_line[-1], 4)
            result["macd_histogram"] = round(histogram, 4)

            if histogram > 0:
                result["macd_signal"] = "bullish"
            elif histogram < 0:
                result["macd_signal"] = "bearish"
            else:
                result["macd_signal"] = "neutral"

    # ══════════════ Bollinger Bands(20, 2) ══════════════
    bb_period = 20
    if len(closes) >= bb_period:
        bb_sma = sum(closes[-bb_period:]) / bb_period
        variance = sum((c - bb_sma) ** 2 for c in closes[-bb_period:]) / bb_period
        bb_std = variance ** 0.5

        result["bollinger_upper"] = round(bb_sma + 2 * bb_std, 4)
        result["bollinger_middle"] = round(bb_sma, 4)
        result["bollinger_lower"] = round(bb_sma - 2 * bb_std, 4)
        result["bollinger_width"] = round(4 * bb_std / bb_sma * 100, 2) if bb_sma else None
        last_close = closes[-1]
        band_range = (bb_sma + 2 * bb_std) - (bb_sma - 2 * bb_std)
        if band_range > 0:
            result["bollinger_pct"] = round((last_close - (bb_sma - 2 * bb_std)) / band_range, 4)

    # ══════════════ ADX(14) + DI ══════════════
    adx_period = 14
    if len(highs) >= adx_period + 1 and len(lows) >= adx_period + 1:
        tr_list = []
        plus_dm = []
        minus_dm = []
        for i in range(1, len(highs)):
            h, l, prev_h, prev_l, prev_c = highs[i], lows[i], highs[i-1], lows[i-1], closes[i-1]
            tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
            tr_list.append(tr)

            up_move = h - prev_h
            down_move = prev_l - l
            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)

        if len(tr_list) >= adx_period:
            atr = sum(tr_list[:adx_period])
            apdm = sum(plus_dm[:adx_period])
            amdm = sum(minus_dm[:adx_period])

            dx_list = []
            for i in range(adx_period, len(tr_list)):
                atr = atr - atr / adx_period + tr_list[i]
                apdm = apdm - apdm / adx_period + plus_dm[i]
                amdm = amdm - amdm / adx_period + minus_dm[i]

                if atr > 0:
                    plus_di = 100 * apdm / atr
                    minus_di = 100 * amdm / atr
                    di_sum = plus_di + minus_di
                    if di_sum > 0:
                        dx_list.append(abs(plus_di - minus_di) / di_sum * 100)

            if len(dx_list) >= adx_period:
                adx_val = sum(dx_list[:adx_period]) / adx_period
                for i in range(adx_period, len(dx_list)):
                    adx_val = (adx_val * (adx_period - 1) + dx_list[i]) / adx_period

                result["adx"] = round(adx_val, 2)
                # Последние DI для информативности
                if atr > 0:
                    result["plus_di"] = round(100 * apdm / atr, 2)
                    result["minus_di"] = round(100 * amdm / atr, 2)
                if adx_val >= 40:
                    result["trend_strength"] = "very_strong"
                elif adx_val >= 25:
                    result["trend_strength"] = "strong"
                elif adx_val >= 20:
                    result["trend_strength"] = "moderate"
                else:
                    result["trend_strength"] = "weak"

    # ══════════════ ATR(14) — Average True Range ══════════════
    atr_period = 14
    if len(highs) >= atr_period + 1:
        tr_list_atr = []
        for i in range(1, len(highs)):
            h, l, prev_c = highs[i], lows[i], closes[i-1]
            tr_list_atr.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))

        atr_smoothed = _wilder_smooth(tr_list_atr, atr_period)
        if atr_smoothed:
            result["atr_14"] = round(atr_smoothed[-1], 4)
            result["atr_14_pct"] = round(atr_smoothed[-1] / closes[-1] * 100, 2) if closes[-1] else None

    # ══════════════ OBV — On Balance Volume ══════════════
    if len(closes) >= 2 and len(volumes) >= len(closes):
        valid_volumes = volumes[:len(closes)]
        obv = 0
        obv_series = [0]
        for i in range(1, len(closes)):
            if closes[i] > closes[i - 1]:
                obv += valid_volumes[i]
            elif closes[i] < closes[i - 1]:
                obv -= valid_volumes[i]
            obv_series.append(obv)

        result["obv"] = obv
        # OBV trend: comparing last 10 vs previous 10
        if len(obv_series) >= 20:
            recent_obv = sum(obv_series[-10:]) / 10
            prev_obv = sum(obv_series[-20:-10]) / 10
            result["obv_trend"] = "rising" if recent_obv > prev_obv else "falling"

    # ══════════════ VWAP — Volume Weighted Average Price ══════════════
    if len(closes) >= 1 and len(highs) >= 1 and len(lows) >= 1 and len(volumes) >= len(closes):
        cum_tp_vol = 0.0
        cum_vol = 0.0
        # Используем все свечи периода
        for i in range(min(len(closes), len(highs), len(lows))):
            tp = (highs[i] + lows[i] + closes[i]) / 3
            v = volumes[i]
            cum_tp_vol += tp * v
            cum_vol += v
        if cum_vol > 0:
            result["vwap"] = round(cum_tp_vol / cum_vol, 4)

    # ══════════════ CCI(20) — Commodity Channel Index ══════════════
    cci_period = 20
    if len(closes) >= cci_period and len(highs) >= cci_period and len(lows) >= cci_period:
        typical_prices = [(highs[i] + lows[i] + closes[i]) / 3
                          for i in range(len(closes))]
        tp_sma = sum(typical_prices[-cci_period:]) / cci_period
        mean_dev = sum(abs(tp - tp_sma) for tp in typical_prices[-cci_period:]) / cci_period
        if mean_dev > 0:
            result["cci_20"] = round((typical_prices[-1] - tp_sma) / (0.015 * mean_dev), 2)

    # ══════════════ Williams %R(14) ══════════════
    wr_period = 14
    if len(closes) >= wr_period and len(highs) >= wr_period and len(lows) >= wr_period:
        hh_wr = max(highs[-wr_period:])
        ll_wr = min(lows[-wr_period:])
        if hh_wr != ll_wr:
            result["williams_r"] = round((hh_wr - closes[-1]) / (hh_wr - ll_wr) * -100, 2)

    # ══════════════ Ichimoku Cloud (9, 26, 52) ══════════════
    tenkan_p, kijun_p, senkou_b_p = 9, 26, 52
    if len(highs) >= senkou_b_p:
        def _mid_point(data: list[float], period: int) -> float | None:
            if len(data) < period:
                return None
            window = data[-period:]
            return (max(window) + min(window)) / 2

        tenkan = _mid_point(highs, tenkan_p) and _mid_point(lows, tenkan_p)
        kijun = _mid_point(highs, kijun_p) and _mid_point(lows, kijun_p)

        if len(highs) >= tenkan_p and len(lows) >= tenkan_p:
            h_t = max(highs[-tenkan_p:])
            l_t = min(lows[-tenkan_p:])
            result["ichimoku_tenkan"] = round((h_t + l_t) / 2, 4)

        if len(highs) >= kijun_p and len(lows) >= kijun_p:
            h_k = max(highs[-kijun_p:])
            l_k = min(lows[-kijun_p:])
            result["ichimoku_kijun"] = round((h_k + l_k) / 2, 4)

        tk = result.get("ichimoku_tenkan")
        kj = result.get("ichimoku_kijun")
        if tk is not None and kj is not None:
            result["ichimoku_senkou_a"] = round((tk + kj) / 2, 4)

        if len(highs) >= senkou_b_p and len(lows) >= senkou_b_p:
            h_sb = max(highs[-senkou_b_p:])
            l_sb = min(lows[-senkou_b_p:])
            result["ichimoku_senkou_b"] = round((h_sb + l_sb) / 2, 4)

        if len(closes) >= kijun_p:
            result["ichimoku_chikou"] = round(closes[-1], 4)

        # Ichimoku signal: цена vs облако
        sa = result.get("ichimoku_senkou_a")
        sb = result.get("ichimoku_senkou_b")
        if sa is not None and sb is not None:
            cloud_top = max(sa, sb)
            cloud_bottom = min(sa, sb)
            last_c = closes[-1]
            if last_c > cloud_top:
                result["ichimoku_signal"] = "bullish"
            elif last_c < cloud_bottom:
                result["ichimoku_signal"] = "bearish"
            else:
                result["ichimoku_signal"] = "in_cloud"

    # ══════════════ Parabolic SAR ══════════════
    if len(highs) >= 2 and len(lows) >= 2:
        af = 0.02
        af_max = 0.20
        af_step = 0.02

        # Направление определяем по первым двум свечам
        is_long = closes[1] >= closes[0]
        if is_long:
            ep = highs[0]
            sar_val = lows[0]
        else:
            ep = lows[0]
            sar_val = highs[0]

        for i in range(1, len(highs)):
            prev_sar = sar_val

            if is_long:
                sar_val = prev_sar + af * (ep - prev_sar)
                sar_val = min(sar_val, lows[i - 1])
                if i >= 2:
                    sar_val = min(sar_val, lows[i - 2])

                if lows[i] < sar_val:
                    is_long = False
                    sar_val = ep
                    ep = lows[i]
                    af = af_step
                else:
                    if highs[i] > ep:
                        ep = highs[i]
                        af = min(af + af_step, af_max)
            else:
                sar_val = prev_sar + af * (ep - prev_sar)
                sar_val = max(sar_val, highs[i - 1])
                if i >= 2:
                    sar_val = max(sar_val, highs[i - 2])

                if highs[i] > sar_val:
                    is_long = True
                    sar_val = ep
                    ep = highs[i]
                    af = af_step
                else:
                    if lows[i] < ep:
                        ep = lows[i]
                        af = min(af + af_step, af_max)

        result["psar"] = round(sar_val, 4)
        result["psar_direction"] = "long" if is_long else "short"

    # ══════════════ Pivot Points (Classic) ══════════════
    if len(highs) >= 1 and len(lows) >= 1 and len(closes) >= 1:
        # Используем последнюю свечу (или предпоследнюю — классика использует prev day)
        h_p = highs[-1]
        l_p = lows[-1]
        c_p = closes[-1]
        pivot_val = (h_p + l_p + c_p) / 3

        result["pivot"] = round(pivot_val, 4)
        result["pivot_r1"] = round(2 * pivot_val - l_p, 4)
        result["pivot_s1"] = round(2 * pivot_val - h_p, 4)
        result["pivot_r2"] = round(pivot_val + (h_p - l_p), 4)
        result["pivot_s2"] = round(pivot_val - (h_p - l_p), 4)
        result["pivot_r3"] = round(pivot_val + 2 * (h_p - l_p), 4)
        result["pivot_s3"] = round(pivot_val - 2 * (h_p - l_p), 4)

    # ══════════════ Fibonacci Retracements ══════════════
    if len(highs) >= 2 and len(lows) >= 2:
        fib_high = max(highs)
        fib_low = min(lows)
        diff = fib_high - fib_low

        result["fib_high"] = round(fib_high, 4)
        result["fib_low"] = round(fib_low, 4)
        result["fib_236"] = round(fib_high - diff * 0.236, 4)
        result["fib_382"] = round(fib_high - diff * 0.382, 4)
        result["fib_500"] = round(fib_high - diff * 0.500, 4)
        result["fib_618"] = round(fib_high - diff * 0.618, 4)
        result["fib_786"] = round(fib_high - diff * 0.786, 4)

    # ══════════════ Momentum(10) / ROC(10) ══════════════
    roc_period = 10
    if len(closes) > roc_period:
        result["momentum_10"] = round(closes[-1] - closes[-1 - roc_period], 4)
        if closes[-1 - roc_period] != 0:
            result["roc_10"] = round((closes[-1] / closes[-1 - roc_period] - 1) * 100, 2)

    # ══════════════ Chaikin Money Flow(20) ══════════════
    cmf_period = 20
    if len(closes) >= cmf_period and len(highs) >= cmf_period and len(lows) >= cmf_period and len(volumes) >= cmf_period:
        mfv_sum = 0.0
        vol_sum = 0.0
        start = len(closes) - cmf_period
        for i in range(start, len(closes)):
            h_cmf = highs[i]
            l_cmf = lows[i]
            c_cmf = closes[i]
            v_cmf = volumes[i]
            hl_diff = h_cmf - l_cmf
            if hl_diff > 0:
                mfm = ((c_cmf - l_cmf) - (h_cmf - c_cmf)) / hl_diff
                mfv_sum += mfm * v_cmf
                vol_sum += v_cmf

        if vol_sum > 0:
            cmf_val = mfv_sum / vol_sum
            result["cmf_20"] = round(cmf_val, 4)
            if cmf_val > 0.05:
                result["cmf_signal"] = "buying_pressure"
            elif cmf_val < -0.05:
                result["cmf_signal"] = "selling_pressure"
            else:
                result["cmf_signal"] = "neutral"

    return result


def candlestick_analysis(query: str, days: int = 90) -> dict:
    """Распознавание свечных паттернов из дневных свечей (OHLCV).

    Args:
        query — тикер или ISIN.
        days — период для анализа (default 90).

    Returns:
        {secid, period_days, trading_days, candles_analyzed,
         total_bullish, total_bearish, total_neutral, signal,
         patterns: [{pattern, bar_type, signal, strength, bars_back,
                     date, signal_detail, pattern_details}]}.
    """
    from datetime import date as _date, timedelta

    r = resolve(query)
    till = _date.today()
    frm = till - timedelta(days=days + 30)

    raw_candles = exec_template(T_CANDLES, {
        "engine": r["engine"], "market": r["market"],
        "board": r["board"], "security": r["secid"]},
        {"from": str(frm), "till": str(till), "interval": "24"})
    rows = records(raw_candles, "candles")

    opens_raw = [row["open"] for row in rows if row.get("open")]
    highs_raw = [row["high"] for row in rows if row.get("high")]
    lows_raw = [row["low"] for row in rows if row.get("low")]
    closes_raw = [row["close"] for row in rows if row.get("close")]
    volumes_raw = [row["volume"] for row in rows if row.get("volume")]
    dates_raw = [row.get("begin", "") for row in rows]

    n = len(closes_raw)
    if n < 3:
        return {"error": "недостаточно данных", "secid": r["secid"], "trading_days": n}

    result: dict = {
        "secid": r["secid"],
        "period_days": days,
        "trading_days": n,
        "candles_analyzed": n,
    }

    # Нормализация свечей
    candles_all: list[dict] = []
    for i in range(n):
        o, h, l, c = opens_raw[i], highs_raw[i], lows_raw[i], closes_raw[i]
        rng = h - l
        body = abs(c - o)
        hi_val = max(o, c)
        lo_val = min(o, c)
        candles_all.append({
            "idx": i, "date": dates_raw[i] if i < len(dates_raw) else "",
            "open": o, "high": h, "low": l, "close": c,
            "volume": volumes_raw[i] if i < len(volumes_raw) else None,
            "body": body, "range": rng,
            "lower_shadow": lo_val - l, "upper_shadow": h - hi_val,
            "body_pct": body / rng if rng > 0 else 0.0,
            "direction": 1 if c > o else -1 if c < o else 0,
            "midpoint": (o + c) / 2,
        })

    max_range = max((c["range"] for c in candles_all), default=1)
    period_high = max(c["high"] for c in candles_all)
    period_low = min(c["low"] for c in candles_all)
    period_rng = period_high - period_low or 1.0

    for c in candles_all:
        c["normalized_range"] = c["range"] / max_range if max_range else 0.0
        c["period_pct_high"] = (c["high"] - period_low) / period_rng
        c["period_pct_low"] = (c["low"] - period_low) / period_rng

    # ─── Детектор односвечных паттернов ───
    def _detect_single_bar(ci: dict, context: float = 0.0) -> dict | None:
        rng, body_pct, direction = ci["range"], ci["body_pct"], ci["direction"]
        ls = ci["lower_shadow"]
        us = ci["upper_shadow"]

        if rng == 0:
            return None

        strength_raw = 1
        if ci["normalized_range"] >= 0.3:
            strength_raw = 2

        # Codji
        if body_pct <= 0.1:
            if ls / rng >= 0.3 and us / rng >= 0.3:
                sub = "long_legged"
            elif ls / rng >= 0.6 and us / rng <= 0.15:
                sub = "dragonfly"
            elif us / rng >= 0.6 and ls / rng <= 0.15:
                sub = "gravestone"
            else:
                sub = "standard"
            strength = strength_raw
            if sub in ("dragonfly", "gravestone"):
                strength = min(strength_raw + 1, 3)
            return {"pattern": "doji", "sub": sub, "signal": 0, "strength": strength}

        # Hammer / Hanging Man
        if (ls / max(body_pct * rng, 1e-12) >= 2.0 and
                ls / rng >= 0.4 and us / rng <= 0.10 and body_pct <= 0.35):
            strength = min(strength_raw + 1, 3)
            if context > 0.5:
                return {"pattern": "hanging_man", "signal": -1, "strength": strength}
            return {"pattern": "hammer", "signal": 1, "strength": strength}

        # Shooting Star
        if (us / max(body_pct * rng, 1e-12) >= 2.0 and
                us / rng >= 0.4 and ls / rng <= 0.10 and body_pct <= 0.35):
            strength = min(strength_raw + 1, 3)
            return {"pattern": "shooting_star", "signal": -1, "strength": strength}

        # Marubozu
        if body_pct >= 0.90:
            strength = min(strength_raw + 1, 3)
            if direction == 1:
                return {"pattern": "bullish_marubozu", "signal": 1, "strength": strength}
            if direction == -1:
                return {"pattern": "bearish_marubozu", "signal": -1, "strength": strength}

        return None

    # ─── Детектор двухсвечных и трёхсвечных паттернов ───
    def _detect_multi_bar(patterns: list, c: list[dict], n_bars: int) -> list:
        results: list = []

        for k in range(n_bars - 1):
            idx = n_bars - 1 - k  # текущая свеча (от конца)
            p, q = c[idx - 1], c[idx]  # предыдущая, текущая

            ca_p = abs(q["body"]) / max(p["body"], 1e-12)

            # Bullish Engulfing
            if (p["direction"] == -1 and q["direction"] == 1 and
                    p["close"] >= q["close"] and p["open"] <= q["open"] and
                    ca_p >= 1.1):
                strength = 2 if ca_p >= 1.5 else 1
                if q["period_pct_low"] <= 0.3:
                    strength = min(strength + 1, 3)
                results.append({"pattern": "bullish_engulfing", "bar_type": "double",
                                "signal": "bullish", "strength": strength, "bars_back": k})

            # Bearish Engulfing
            if (p["direction"] == 1 and q["direction"] == -1 and
                    p["close"] <= q["close"] and p["open"] >= q["open"] and
                    ca_p >= 1.1):
                strength = 2 if ca_p >= 1.5 else 1
                if q["period_pct_high"] >= 0.7:
                    strength = min(strength + 1, 3)
                results.append({"pattern": "bearish_engulfing", "bar_type": "double",
                                "signal": "bearish", "strength": strength, "bars_back": k})

            # Bullish Harami
            if (p["direction"] == -1 and q["direction"] == 1 and
                    q["close"] <= p["open"] and q["open"] >= p["close"] and ca_p <= 0.6):
                results.append({"pattern": "bullish_harami", "bar_type": "double",
                                "signal": "bullish", "strength": 1, "bars_back": k})

            # Bearish Harami
            if (p["direction"] == 1 and q["direction"] == -1 and
                    q["close"] >= p["open"] and q["open"] <= p["close"] and ca_p <= 0.6):
                results.append({"pattern": "bearish_harami", "bar_type": "double",
                                "signal": "bearish", "strength": 1, "bars_back": k})

            # Tweezer Bottom
            if (p["direction"] <= 0 and q["direction"] == 1 and
                    abs(p["low"] - q["low"]) / p["low"] < 0.002):
                strength = 2 if q["period_pct_low"] <= 0.3 else 1
                results.append({"pattern": "tweezer_bottom", "bar_type": "double",
                                "signal": "bullish", "strength": strength, "bars_back": k})

            # Tweezer Top
            if (p["direction"] >= 0 and q["direction"] == -1 and
                    abs(p["high"] - q["high"]) / p["high"] < 0.002):
                strength = 2 if q["period_pct_high"] >= 0.7 else 1
                results.append({"pattern": "tweezer_top", "bar_type": "double",
                                "signal": "bearish", "strength": strength, "bars_back": k})

        for k in range(n_bars - 2):
            idx = n_bars - 1 - k
            a, b, cc = c[idx - 2], c[idx - 1], c[idx]
            ab_range = a["high"] - a["low"] or 1e-12

            # Morning Star
            if (a["direction"] == -1 and b["body_pct"] <= 0.30 and cc["direction"] == 1 and b["close"] < a["close"]):
                gap_below = b["high"] < a["low"]
                pierce_50 = cc["close"] > (a["open"] + a["close"]) / 2
                ab_ratio = abs(cc["body"]) / max(abs(a["body"]), 1e-12)
                strength = 1
                if gap_below:
                    strength += 1
                if pierce_50 and ab_ratio >= 0.5:
                    strength += 1
                strength = min(strength, 3)
                results.append({
                    "pattern": "morning_star", "bar_type": "triple",
                    "signal": "bullish", "strength": strength, "bars_back": k,
                    "pattern_details": {"gap_below_mid": gap_below, "pierce_50pct": pierce_50,
                                        "body_ratio": round(ab_ratio, 2)},
                })

            # Evening Star
            if (a["direction"] == 1 and b["body_pct"] <= 0.30 and cc["direction"] == -1 and b["close"] > a["close"]):
                gap_above = b["low"] > a["high"]
                pierce_50 = cc["close"] < (a["open"] + a["close"]) / 2
                ab_ratio = abs(cc["body"]) / max(abs(a["body"]), 1e-12)
                strength = 1
                if gap_above:
                    strength += 1
                if pierce_50 and ab_ratio >= 0.5:
                    strength += 1
                strength = min(strength, 3)
                results.append({
                    "pattern": "evening_star", "bar_type": "triple",
                    "signal": "bearish", "strength": strength, "bars_back": k,
                    "pattern_details": {"gap_above_mid": gap_above, "pierce_50pct": pierce_50,
                                        "body_ratio": round(ab_ratio, 2)},
                })

            # Bullish Piercing Line
            if (a["direction"] == -1 and cc["direction"] == 1 and
                    a["close"] < cc["open"] and cc["close"] > (a["open"] + a["close"]) / 2 and cc["close"] < a["open"]):
                intrude = (cc["close"] - a["close"]) / max(a["open"] - a["close"], 1e-12)
                strength = 2 if intrude > 0.6 else 1
                results.append({
                    "pattern": "piercing_line", "bar_type": "double",
                    "signal": "bullish", "strength": strength, "bars_back": k,
                    "pattern_details": {"intrusion_pct": round(intrude * 100, 1)},
                })

            # Bearish Dark Cloud Cover
            if (a["direction"] == 1 and cc["direction"] == -1 and
                    a["open"] <= cc["close"] and cc["high"] > a["high"] and a["high"] < cc["open"]):
                intrude = (a["high"] - cc["close"]) / max(a["high"] - a["low"], 1e-12)
                strength = 2 if intrude > 0.6 else 1
                results.append({
                    "pattern": "dark_cloud_cover", "bar_type": "double",
                    "signal": "bearish", "strength": strength, "bars_back": k,
                    "pattern_details": {"intrusion_pct": round(intrude * 100, 1)},
                })

            # Three White Soldiers
            if (a["direction"] == 1 and b["direction"] == 1 and cc["direction"] == 1 and
                    b["body"] > 0 and cc["body"] > 0 and
                    b["open"] > a["low"] and cc["open"] > b["low"] and
                    b["close"] > a["close"] and cc["close"] > b["close"]):
                big = (a["body_pct"] > 0.6 and b["body_pct"] > 0.6 and cc["body_pct"] > 0.6)
                strength = 2 if big else 1
                if a["period_pct_low"] <= 0.3:
                    strength = min(strength + 1, 3)
                results.append({"pattern": "three_white_soldiers", "bar_type": "triple",
                                "signal": "bullish", "strength": strength, "bars_back": k})

            # Three Black Crows
            if (a["direction"] == -1 and b["direction"] == -1 and cc["direction"] == -1 and
                    b["body"] > 0 and cc["body"] > 0 and
                    b["open"] < a["high"] and cc["open"] < b["high"] and
                    b["close"] < a["close"] and cc["close"] < b["close"]):
                big = (a["body_pct"] > 0.6 and b["body_pct"] > 0.6 and cc["body_pct"] > 0.6)
                strength = 2 if big else 1
                if a["period_pct_high"] >= 0.7:
                    strength = min(strength + 1, 3)
                results.append({"pattern": "three_black_crows", "bar_type": "triple",
                                "signal": "bearish", "strength": strength, "bars_back": k})

        return results

    # ─── Обнаружение ───
    patterns_all: list = []

    for ci in candles_all:
        sp = _detect_single_bar(ci, ci["period_pct_high"])
        if sp:
            pat = {
                "pattern": sp["pattern"],
                "bar_type": "single",
                "signal": "bullish" if sp["signal"] > 0 else "bearish" if sp["signal"] < 0 else "neutral",
                "strength": sp["strength"],
                "bars_back": n - 1 - ci["idx"],
                "date": ci["date"],
                "signal_detail": (
                    f"{sp['pattern'].replace('_', ' ').title()}: "
                    f"{'bullish reversal' if sp['signal'] > 0 else 'bearish reversal' if sp['signal'] < 0 else 'neutral / indecision'}"
                    + (f" ({sp.get('sub', '')})" if sp.get("sub") else "")
                ),
            }
            if ci["volume"] is not None and ci["volume"] > 0:
                pat["volume"] = ci["volume"]
            patterns_all.append(pat)

    if n >= 2:
        mp = _detect_multi_bar(patterns_all, candles_all, n)
        for mp_item in mp:
            bi = mp_item.get("bars_back", 0)
            pat: dict = {
                "pattern": mp_item["pattern"],
                "bar_type": mp_item["bar_type"],
                "signal": mp_item["signal"],
                "strength": mp_item["strength"],
                "bars_back": bi,
                "date": candles_all[n - 1 - bi]["date"] if (n - 1 - bi) >= 0 else "",
            }
            if "pattern_details" in mp_item:
                pat["pattern_details"] = mp_item["pattern_details"]
            pat["signal_detail"] = (
                f"{mp_item['pattern'].replace('_', ' ').title()}: "
                f"{mp_item['signal']} signal"
            )
            patterns_all.append(pat)

    # Сортировка: сначала сильные, потом близкие
    patterns_all.sort(key=lambda x: (-x["strength"], x["bars_back"]))

    total_bull = sum(1 for p in patterns_all if p["signal"] == "bullish")
    total_bear = sum(1 for p in patterns_all if p["signal"] == "bearish")
    total_neut = sum(1 for p in patterns_all if p["signal"] == "neutral")

    strong_bull = sum(p["strength"] for p in patterns_all if p["signal"] == "bullish")
    strong_bear = sum(p["strength"] for p in patterns_all if p["signal"] == "bearish")

    if strong_bull > strong_bear + 2:
        signal = "bullish"
    elif strong_bear > strong_bull + 2:
        signal = "bearish"
    elif strong_bull > strong_bear:
        signal = "slightly_bullish"
    elif strong_bear > strong_bull:
        signal = "slightly_bearish"
    else:
        signal = "neutral"

    result["total_bullish"] = total_bull
    result["total_bearish"] = total_bear
    result["total_neutral"] = total_neut
    result["signal_score_bullish"] = strong_bull
    result["signal_score_bearish"] = strong_bear
    result["signal"] = signal
    result["patterns"] = patterns_all

    return result


def _parse_board_etf(row: dict) -> dict | None:
    """Нормализовать строку _fetch_board_etf в словарь скринера."""
    secid = row.get("SECID")
    if not secid:
        return None

    # Цена: LAST → MARKETPRICE → LCLOSEPRICE → WAPRICE
    price = first(row.get("LAST"), row.get("MARKETPRICE"),
                  row.get("LCLOSEPRICE"), row.get("WAPRICE"))

    # Bid/Ask
    bid = row.get("BID")
    ask = row.get("OFFER")

    # Spread (%)
    spread_pct = None
    if bid and ask and bid > 0 and ask > 0:
        spread_pct = round((ask - bid) / ((ask + bid) / 2) * 100, 4)

    # Объём
    value_today = row.get("VALUE") or row.get("VALTODAY")
    vol_today = row.get("VOLUME") or row.get("VOLTODAY")

    # Изменение
    change_pct = row.get("LASTCHANGE") or row.get("CHANGE")

    return {
        "secid": secid,
        "shortname": row.get("SHORTNAME"),
        "isin": row.get("ISIN"),
        "board": row.get("_board"),
        "price": price,
        "bid": bid,
        "ask": ask,
        "spread_pct": spread_pct,
        "change_pct": change_pct,
        "value_today": value_today,
        "vol_today": vol_today,
        "open": row.get("OPEN"),
        "low": row.get("LOW"),
        "high": row.get("HIGH"),
        "lotsize": row.get("LOTSIZE"),
        "face_value": row.get("FACEVALUE"),
        "face_unit": row.get("FACEUNIT"),
        "emitent_id": row.get("EMITENT_ID"),
        "emitent_title": row.get("EMITENT_TITLE"),
        "list_level": row.get("LISTLEVEL"),
        "is_qualified": row.get("ISQUALIFIEDINVESTORS"),
    }


def etf_screener(
    *,
    category: str | None = None,
    emitent: str | None = None,
    benchmark: str | None = None,
    currency: str | None = None,
    price_min: float | None = None,
    price_max: float | None = None,
    volume_min: float | None = None,
    volume_max: float | None = None,
    spread_max: float | None = None,
    volatility_min: float | None = None,
    volatility_max: float | None = None,
    sharpe_min: float | None = None,
    sharpe_max: float | None = None,
    beta_min: float | None = None,
    beta_max: float | None = None,
    performance_min: float | None = None,
    performance_max: float | None = None,
    performance_period: str = "1y",
    rsi_min: float | None = None,
    rsi_max: float | None = None,
    ma_signal: str | None = None,
    adx_min: float | None = None,
    macd_signal: str | None = None,
    stochastic_min: float | None = None,
    stochastic_max: float | None = None,
    cci_min: float | None = None,
    cci_max: float | None = None,
    williams_min: float | None = None,
    williams_max: float | None = None,
    ichimoku_signal: str | None = None,
    psar_direction: str | None = None,
    cmf_signal: str | None = None,
    roc_min: float | None = None,
    roc_max: float | None = None,
    premium_discount_max: float | None = None,
    tracking_error_max: float | None = None,
    include_indicators: bool = True,
    sort_by: str = "performance",
    sort_desc: bool = True,
    limit: int = 15,
) -> dict:
    """Скринер БПИФ/ETF на MOEX с фильтрацией по множеству параметров.

    Загружает все фонды с бордов TQIF/TQTF, обогащает метаданными из
    ETF_BENCHMARK_MAP, рассчитывает технические индикаторы.

    Args:
        category — класс активов: 'equity_russia', 'equity_foreign',
            'equity_sector', 'equity_dividend', 'bond_gov', 'bond_corp',
            'money_market', 'commodity', 'fx', 'mixed'
        emitent — управляющая компания ('Т-Капитал', 'Сбер', 'Альфа', 'ВТБ')
        benchmark — тикер бенчмарка на MOEX ('IMOEX', 'GOLD', 'RGBITR')
        currency — валюта ('SUR', 'USD', 'EUR', 'CNY', 'HKD')
        price_min/price_max — цена фонда (₽)
        volume_min/volume_max — среднедневной объём торгов (₽)
        spread_max — макс. Bid-Ask spread (%)
        volatility_min/volatility_max — годовая волатильность (%)
        sharpe_min/sharpe_max — коэффициент Шарпа
        beta_min/beta_max — бета (относительно IMOEX)
        performance_min/performance_max — доходность (%)
        performance_period — период доходности: '1m', '3m', '6m', '1y', 'ytd'
        rsi_min/rsi_max — RSI(14)
        ma_signal — 'golden_cross' (MA50>MA200), 'death_cross' (MA50<MA200)
        adx_min — минимальный ADX (сила тренда)
        macd_signal — 'bullish', 'bearish'
        stochastic_min/stochastic_max — Stochastic %K(14,3,3)
        cci_min/cci_max — CCI(20)
        williams_min/williams_max — Williams %R(14) (от −100 до 0)
        ichimoku_signal — 'bullish', 'bearish', 'in_cloud'
        psar_direction — 'long', 'short'
        cmf_signal — 'buying_pressure', 'selling_pressure', 'neutral'
        roc_min/roc_max — Rate of Change(10), %
        premium_discount_max — макс. премия/дисконт к NAV (%)
        tracking_error_max — макс. трекинг-ошибка (%)
        include_indicators — рассчитывать ли TA-индикаторы (default True)
        sort_by — сортировка: 'performance', 'volatility', 'sharpe', 'volume',
            'spread', 'premium', 'rsi', 'adx', 'beta', 'stochastic', 'cci',
            'williams', 'roc', 'cmf', 'momentum'
        sort_desc — True = по убыванию
        limit — максимум результатов (1..200, default 15)

    Returns:
        {count_shown, count_total_matching, count_all_funds,
         funds: [{secid, shortname, isin, emitent, category, category_ru,
           benchmark, benchmark_name, currency,
           price, change_pct, bid, ask, spread_pct,
           avg_daily_volume_rub, liquidity_score, liquidity_grade,
           inav_price, premium_discount_pct,
           performance_1m/3m/6m/1y, ytd,
           volatility_ann, sharpe, max_drawdown, beta,
           rsi_14, stochastic_k, stochastic_d,
           ma_50, ma_200, ma_signal,
           macd, macd_signal_line, macd_histogram, macd_signal,
           bollinger_pct, bollinger_width,
           adx, plus_di, minus_di, trend_strength,
           atr_14, atr_14_pct,
           obv, obv_trend,
           vwap,
           cci_20,
           williams_r,
           ichimoku_signal,
           psar, psar_direction,
           momentum_10, roc_10,
           cmf_20, cmf_signal,
           tracking_error_ann}]}
    """
    limit = max(1, min(limit, 200))

    # ── Шаг 1: загрузить все фонды с бордов (параллельно) ──
    board_data: list[dict] = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(_fetch_board_etf, b): b for b in _ETF_BOARDS}
        for f in as_completed(futs):
            try:
                board_data.extend(f.result())
            except Exception:
                continue

    # ── Шаг 2: нормализация + дедупликация по secid ──
    seen: set[str] = set()
    all_funds: list[dict] = []
    for row in board_data:
        parsed = _parse_board_etf(row)
        if parsed and parsed["secid"] not in seen:
            seen.add(parsed["secid"])
            all_funds.append(parsed)

    # ── Шаг 3: обогащение метаданными из ETF_BENCHMARK_MAP ──
    for fund in all_funds:
        sid = fund["secid"]
        bm = ETF_BENCHMARK_MAP.get(sid, {})
        fund["category"] = bm.get("category", "unknown")
        fund["category_ru"] = ETF_CATEGORY_RU.get(fund["category"], fund["category"])
        fund["benchmark"] = bm.get("benchmark")
        fund["benchmark_name"] = bm.get("benchmark_name")
        fund["currency"] = fund.get("face_unit") or "SUR"

    # ── Шаг 4: фильтрация ──
    filtered = all_funds

    if category:
        cat_lower = category.lower()
        filtered = [f for f in filtered if f.get("category", "").lower() == cat_lower]

    if emitent:
        emit_lower = emitent.lower()
        filtered = [f for f in filtered if emit_lower in (f.get("emitent_title") or "").lower()]

    if benchmark:
        bm_upper = benchmark.upper()
        filtered = [f for f in filtered if (f.get("benchmark") or "").upper() == bm_upper]

    if currency:
        cur_upper = currency.upper()
        filtered = [f for f in filtered if (f.get("currency") or "").upper() == cur_upper]

    if price_min is not None:
        filtered = [f for f in filtered if f.get("price") is not None and f["price"] >= price_min]
    if price_max is not None:
        filtered = [f for f in filtered if f.get("price") is not None and f["price"] <= price_max]

    if volume_min is not None:
        filtered = [f for f in filtered if f.get("value_today") is not None and f["value_today"] >= volume_min]
    if volume_max is not None:
        filtered = [f for f in filtered if f.get("value_today") is not None and f["value_today"] <= volume_max]

    if spread_max is not None:
        filtered = [f for f in filtered if f.get("spread_pct") is not None and f["spread_pct"] <= spread_max]

    # ── Шаг 5: расчёт доходности (performance) ──
    perf_map: dict[str, dict] = {}

    # Определяем период для расчёта
    perf_days_map = {
        "1m": 30, "3m": 90, "6m": 180, "1y": 365, "ytd": None
    }
    perf_field = f"performance_{performance_period}"

    if include_indicators or performance_min is not None or performance_max is not None:
        from datetime import date as _date, timedelta
        today = _date.today()

        # Для YTD считаем с начала года
        if performance_period == "ytd":
            frm_date = _date(today.year, 1, 1)
        else:
            days_back = perf_days_map.get(performance_period, 365)
            frm_date = today - timedelta(days=days_back + 10)

        # Параллельно загружаем свечи для всех отфильтрованных фондов
        def _fetch_perf(fund: dict) -> tuple[str, dict]:
            sid = fund["secid"]
            try:
                raw = exec_template(T_CANDLES, {
                    "engine": "stock", "market": "shares",
                    "board": fund["board"], "security": sid},
                    {"from": str(frm_date), "till": str(today), "interval": "24"})
                rows = records(raw, "candles")
                closes = [r["close"] for r in rows if r.get("close")]
                if len(closes) >= 2:
                    perf = round((closes[-1] / closes[0] - 1) * 100, 2)
                    return sid, {"candles": rows, "closes": closes, perf_field: perf}
            except Exception:
                pass
            return sid, {}

        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {ex.submit(_fetch_perf, f): f for f in filtered}
            for f in as_completed(futs):
                sid, data = f.result()
                if data:
                    perf_map[sid] = data

        # Обогащаем filtered
        for fund in filtered:
            sid = fund["secid"]
            if sid in perf_map:
                fund[perf_field] = perf_map[sid].get(perf_field)

    # Фильтрация по доходности
    if performance_min is not None:
        filtered = [f for f in filtered if f.get(perf_field) is not None and f[perf_field] >= performance_min]
    if performance_max is not None:
        filtered = [f for f in filtered if f.get(perf_field) is not None and f[perf_field] <= performance_max]

    # ── Шаг 6: расчёт волатильности, Sharpe, MDD ──
    if include_indicators or volatility_min is not None or volatility_max is not None or \
       sharpe_min is not None or sharpe_max is not None:
        for fund in filtered:
            sid = fund["secid"]
            if sid not in perf_map:
                continue
            candle_data = perf_map[sid].get("candles", [])
            closes = [r["close"] for r in candle_data if r.get("close")]
            if len(closes) < 10:
                continue

            # Daily returns
            returns = [(closes[i] / closes[i-1] - 1) for i in range(1, len(closes))]
            if not returns:
                continue

            mean_ret = sum(returns) / len(returns)
            var = sum((r - mean_ret) ** 2 for r in returns) / (len(returns) - 1) if len(returns) > 1 else 0
            daily_vol = var ** 0.5
            ann_vol = daily_vol * (252 ** 0.5) * 100

            fund["volatility_ann"] = round(ann_vol, 2)
            fund["daily_vol"] = round(daily_vol * 100, 4)

            # Sharpe (rf = key rate или 16% по умолчанию)
            rf_daily = 0.16 / 252
            excess = [r - rf_daily for r in returns]
            mean_excess = sum(excess) / len(excess)
            if daily_vol > 0:
                fund["sharpe"] = round((mean_excess / daily_vol) * (252 ** 0.5), 2)

            # Max Drawdown
            peak = closes[0]
            max_dd = 0
            for c in closes:
                if c > peak:
                    peak = c
                dd = (peak - c) / peak * 100
                if dd > max_dd:
                    max_dd = dd
            fund["max_drawdown"] = round(max_dd, 2)

    # Фильтрация по волатильности/Sharpe
    if volatility_min is not None:
        filtered = [f for f in filtered if f.get("volatility_ann") is not None and f["volatility_ann"] >= volatility_min]
    if volatility_max is not None:
        filtered = [f for f in filtered if f.get("volatility_ann") is not None and f["volatility_ann"] <= volatility_max]
    if sharpe_min is not None:
        filtered = [f for f in filtered if f.get("sharpe") is not None and f["sharpe"] >= sharpe_min]
    if sharpe_max is not None:
        filtered = [f for f in filtered if f.get("sharpe") is not None and f["sharpe"] <= sharpe_max]

    # ── Шаг 7: расчёт Beta через IMOEX ──
    if include_indicators or beta_min is not None or beta_max is not None:
        # Загружаем свечи IMOEX за тот же период
        from datetime import date as _date, timedelta
        today = _date.today()
        frm_date = today - timedelta(days=400)
        try:
            bm_raw = exec_template(T_CANDLES, {
                "engine": "stock", "market": "index",
                "board": "SNDX", "security": "IMOEX"},
                {"from": str(frm_date), "till": str(today), "interval": "24"})
            bm_rows = records(bm_raw, "candles")
            bm_closes = [r["close"] for r in bm_rows if r.get("close")]
            bm_returns = [(bm_closes[i] / bm_closes[i-1] - 1) for i in range(1, len(bm_closes))]
            bm_dates = [(r.get("begin") or "")[:10] for r in bm_rows if r.get("close")]

            if len(bm_returns) > 10:
                bm_mean = sum(bm_returns) / len(bm_returns)
                bm_var = sum((r - bm_mean) ** 2 for r in bm_returns) / (len(bm_returns) - 1)

                # Строим маппинг дата → return для IMOEX
                bm_by_date: dict[str, float] = {}
                for i in range(len(bm_dates) - 1):
                    if i + 1 < len(bm_dates):
                        bm_by_date[bm_dates[i + 1]] = bm_returns[i]

                for fund in filtered:
                    sid = fund["secid"]
                    candle_data = perf_map.get(sid, {}).get("candles", [])
                    if not candle_data:
                        continue

                    fund_closes = [r["close"] for r in candle_data if r.get("close")]
                    fund_dates = [(r.get("begin") or "")[:10] for r in candle_data if r.get("close")]
                    if len(fund_closes) < 10:
                        continue

                    fund_returns = [(fund_closes[i] / fund_closes[i-1] - 1) for i in range(1, len(fund_closes))]

                    # Выравниваем по датам
                    aligned_fund = []
                    aligned_bm = []
                    for i in range(len(fund_dates) - 1):
                        dt = fund_dates[i + 1]
                        if dt in bm_by_date and i < len(fund_returns):
                            aligned_fund.append(fund_returns[i])
                            aligned_bm.append(bm_by_date[dt])

                    if len(aligned_fund) > 10:
                        f_mean = sum(aligned_fund) / len(aligned_fund)
                        b_mean = sum(aligned_bm) / len(aligned_bm)
                        cov = sum((aligned_fund[i] - f_mean) * (aligned_bm[i] - b_mean)
                                  for i in range(len(aligned_fund))) / (len(aligned_fund) - 1)
                        if bm_var > 0:
                            fund["beta"] = round(cov / bm_var, 3)
        except Exception:
            pass

    # Фильтрация по Beta
    if beta_min is not None:
        filtered = [f for f in filtered if f.get("beta") is not None and f["beta"] >= beta_min]
    if beta_max is not None:
        filtered = [f for f in filtered if f.get("beta") is not None and f["beta"] <= beta_max]

    # ── Шаг 8: расчёт TA-индикаторов ──
    if include_indicators:
        for fund in filtered:
            sid = fund["secid"]
            candle_data = perf_map.get(sid, {})
            closes = candle_data.get("closes", [])
            candles_raw = candle_data.get("candles", [])

            if len(closes) < 14:
                continue

            highs_ta = [r["high"] for r in candles_raw if r.get("high")]
            lows_ta = [r["low"] for r in candles_raw if r.get("low")]
            volumes_ta = [r["volume"] for r in candles_raw if r.get("volume")]

            # ── RSI(14) ──
            rsi_period = 14
            if len(closes) >= rsi_period + 1:
                gains = []
                losses = []
                for i in range(1, len(closes)):
                    delta = closes[i] - closes[i - 1]
                    gains.append(max(0, delta))
                    losses.append(max(0, -delta))

                avg_gain = sum(gains[:rsi_period]) / rsi_period
                avg_loss = sum(losses[:rsi_period]) / rsi_period
                for i in range(rsi_period, len(gains)):
                    avg_gain = (avg_gain * (rsi_period - 1) + gains[i]) / rsi_period
                    avg_loss = (avg_loss * (rsi_period - 1) + losses[i]) / rsi_period

                if avg_loss == 0:
                    fund["rsi_14"] = 100.0
                else:
                    rs = avg_gain / avg_loss
                    fund["rsi_14"] = round(100 - 100 / (1 + rs), 2)

            # ── MA50, MA200 ──
            def _sma_val(data: list[float], period: int) -> float | None:
                if len(data) < period:
                    return None
                return round(sum(data[-period:]) / period, 4)

            fund["ma_50"] = _sma_val(closes, 50)
            fund["ma_200"] = _sma_val(closes, 200)

            ma50 = fund["ma_50"]
            ma200 = fund["ma_200"]
            if ma50 is not None and ma200 is not None:
                fund["ma_signal"] = "golden_cross" if ma50 > ma200 else "death_cross"
            else:
                fund["ma_signal"] = "insufficient_data"

            # ── MACD(12, 26, 9) ──
            def _ema_val(data: list[float], period: int) -> list[float]:
                if len(data) < period:
                    return []
                multiplier = 2 / (period + 1)
                ema = [sum(data[:period]) / period]
                for i in range(period, len(data)):
                    ema.append(data[i] * multiplier + ema[-1] * (1 - multiplier))
                return ema

            if len(closes) >= 26:
                ema12 = _ema_val(closes, 12)
                ema26 = _ema_val(closes, 26)
                offset = len(ema12) - len(ema26)
                macd_line = [ema12[offset + i] - ema26[i] for i in range(len(ema26))]

                if len(macd_line) >= 9:
                    signal_line = _ema_val(macd_line, 9)
                    histogram = macd_line[-1] - signal_line[-1]
                    fund["macd"] = round(macd_line[-1], 4)
                    fund["macd_signal_line"] = round(signal_line[-1], 4)
                    fund["macd_histogram"] = round(histogram, 4)
                    fund["macd_signal"] = "bullish" if histogram > 0 else ("bearish" if histogram < 0 else "neutral")

            # ── Stochastic %K/%D(14,3,3) ──
            stoch_period = 14
            if len(closes) >= stoch_period and len(highs_ta) >= stoch_period and len(lows_ta) >= stoch_period:
                stoch_k_values = []
                for i in range(stoch_period - 1, len(closes)):
                    window_h = highs_ta[i - stoch_period + 1:i + 1]
                    window_l = lows_ta[i - stoch_period + 1:i + 1]
                    hh = max(window_h)
                    ll = min(window_l)
                    if hh == ll:
                        stoch_k_values.append(50.0)
                    else:
                        stoch_k_values.append((closes[i] - ll) / (hh - ll) * 100)
                if len(stoch_k_values) >= 3:
                    fund["stochastic_k"] = round(sum(stoch_k_values[-3:]) / 3, 2)
                if len(stoch_k_values) >= 5:
                    d_vals = []
                    for i in range(2, len(stoch_k_values)):
                        d_vals.append(sum(stoch_k_values[i - 2:i + 1]) / 3)
                    fund["stochastic_d"] = round(d_vals[-1], 2)

            # ── Bollinger Bands(20,2) ──
            bb_period = 20
            if len(closes) >= bb_period:
                bb_sma = sum(closes[-bb_period:]) / bb_period
                variance = sum((c - bb_sma) ** 2 for c in closes[-bb_period:]) / bb_period
                bb_std = variance ** 0.5
                bb_upper = bb_sma + 2 * bb_std
                bb_lower = bb_sma - 2 * bb_std
                fund["bollinger_width"] = round(4 * bb_std / bb_sma * 100, 2) if bb_sma else None
                band_range = bb_upper - bb_lower
                if band_range > 0:
                    fund["bollinger_pct"] = round((closes[-1] - bb_lower) / band_range, 4)

            # ── ADX(14) ──
            if len(highs_ta) >= 2 and len(lows_ta) >= 2:
                adx_closes = closes[:min(len(highs_ta), len(lows_ta), len(closes))]
                adx_period = 14
                if len(highs_ta) >= adx_period + 1 and len(lows_ta) >= adx_period + 1:
                    tr_list = []
                    plus_dm = []
                    minus_dm = []
                    for i in range(1, len(highs_ta)):
                        h, l, prev_h, prev_l, prev_c = highs_ta[i], lows_ta[i], highs_ta[i-1], lows_ta[i-1], adx_closes[i-1]
                        tr = max(h - l, abs(h - prev_c), abs(l - prev_c))
                        tr_list.append(tr)
                        up_move = h - prev_h
                        down_move = prev_l - l
                        plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
                        minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)

                    if len(tr_list) >= adx_period:
                        atr_raw = sum(tr_list[:adx_period])
                        apdm = sum(plus_dm[:adx_period])
                        amdm = sum(minus_dm[:adx_period])
                        dx_list = []
                        for i in range(adx_period, len(tr_list)):
                            atr_raw = atr_raw - atr_raw / adx_period + tr_list[i]
                            apdm = apdm - apdm / adx_period + plus_dm[i]
                            amdm = amdm - amdm / adx_period + minus_dm[i]
                            if atr_raw > 0:
                                plus_di = 100 * apdm / atr_raw
                                minus_di = 100 * amdm / atr_raw
                                di_sum = plus_di + minus_di
                                if di_sum > 0:
                                    dx_list.append(abs(plus_di - minus_di) / di_sum * 100)

                        if len(dx_list) >= adx_period:
                            adx_val = sum(dx_list[:adx_period]) / adx_period
                            for i in range(adx_period, len(dx_list)):
                                adx_val = (adx_val * (adx_period - 1) + dx_list[i]) / adx_period
                            fund["adx"] = round(adx_val, 2)
                            if adx_val >= 40:
                                fund["trend_strength"] = "very_strong"
                            elif adx_val >= 25:
                                fund["trend_strength"] = "strong"
                            elif adx_val >= 20:
                                fund["trend_strength"] = "moderate"
                            else:
                                fund["trend_strength"] = "weak"

            # ── ATR(14) ──
            if len(highs_ta) >= 2 and len(lows_ta) >= 2:
                tr_list_atr = []
                for i in range(1, len(highs_ta)):
                    h, l, prev_c = highs_ta[i], lows_ta[i], closes[i-1]
                    tr_list_atr.append(max(h - l, abs(h - prev_c), abs(l - prev_c)))
                ws_period = 14
                if len(tr_list_atr) >= ws_period:
                    atr_s = sum(tr_list_atr[:ws_period]) / ws_period
                    for i in range(ws_period, len(tr_list_atr)):
                        atr_s = (atr_s * (ws_period - 1) + tr_list_atr[i]) / ws_period
                    fund["atr_14"] = round(atr_s, 4)
                    fund["atr_14_pct"] = round(atr_s / closes[-1] * 100, 2) if closes[-1] else None

            # ── VWAP ──
            if len(closes) >= 1 and len(highs_ta) >= 1 and len(lows_ta) >= 1 and len(volumes_ta) >= 1:
                n_tp = min(len(closes), len(highs_ta), len(lows_ta), len(volumes_ta))
                cum_tp_vol = 0.0
                cum_vol = 0.0
                for i in range(n_tp):
                    tp = (highs_ta[i] + lows_ta[i] + closes[i]) / 3
                    cum_tp_vol += tp * volumes_ta[i]
                    cum_vol += volumes_ta[i]
                if cum_vol > 0:
                    fund["vwap"] = round(cum_tp_vol / cum_vol, 4)

            # ── CCI(20) ──
            cci_period = 20
            n_cci = min(len(closes), len(highs_ta), len(lows_ta))
            if n_cci >= cci_period:
                typical_prices = [(highs_ta[i] + lows_ta[i] + closes[i]) / 3 for i in range(n_cci)]
                tp_sma = sum(typical_prices[-cci_period:]) / cci_period
                mean_dev = sum(abs(tp - tp_sma) for tp in typical_prices[-cci_period:]) / cci_period
                if mean_dev > 0:
                    fund["cci_20"] = round((typical_prices[-1] - tp_sma) / (0.015 * mean_dev), 2)

            # ── Williams %R(14) ──
            wr_period = 14
            n_wr = min(len(closes), len(highs_ta), len(lows_ta))
            if n_wr >= wr_period:
                hh_wr = max(highs_ta[-wr_period:])
                ll_wr = min(lows_ta[-wr_period:])
                if hh_wr != ll_wr:
                    fund["williams_r"] = round((hh_wr - closes[-1]) / (hh_wr - ll_wr) * -100, 2)

            # ── Ichimoku signal ──
            tenkan_p, kijun_p, senkou_b_p = 9, 26, 52
            if n_wr >= senkou_b_p:
                if len(highs_ta) >= tenkan_p and len(lows_ta) >= tenkan_p:
                    tk = (max(highs_ta[-tenkan_p:]) + min(lows_ta[-tenkan_p:])) / 2
                else:
                    tk = None
                if len(highs_ta) >= kijun_p and len(lows_ta) >= kijun_p:
                    kj = (max(highs_ta[-kijun_p:]) + min(lows_ta[-kijun_p:])) / 2
                else:
                    kj = None
                if tk is not None and kj is not None:
                    sa = (tk + kj) / 2
                    sb = (max(highs_ta[-senkou_b_p:]) + min(lows_ta[-senkou_b_p:])) / 2
                    cloud_top = max(sa, sb)
                    cloud_bottom = min(sa, sb)
                    if closes[-1] > cloud_top:
                        fund["ichimoku_signal"] = "bullish"
                    elif closes[-1] < cloud_bottom:
                        fund["ichimoku_signal"] = "bearish"
                    else:
                        fund["ichimoku_signal"] = "in_cloud"

            # ── Parabolic SAR ──
            if len(highs_ta) >= 2 and len(lows_ta) >= 2:
                af = 0.02
                af_max = 0.20
                af_step = 0.02
                is_long = closes[1] >= closes[0]
                if is_long:
                    ep = highs_ta[0]
                    sar_val = lows_ta[0]
                else:
                    ep = lows_ta[0]
                    sar_val = highs_ta[0]
                for i in range(1, len(highs_ta)):
                    prev_sar = sar_val
                    if is_long:
                        sar_val = prev_sar + af * (ep - prev_sar)
                        sar_val = min(sar_val, lows_ta[i - 1])
                        if i >= 2:
                            sar_val = min(sar_val, lows_ta[i - 2])
                        if lows_ta[i] < sar_val:
                            is_long = False
                            sar_val = ep
                            ep = lows_ta[i]
                            af = af_step
                        else:
                            if highs_ta[i] > ep:
                                ep = highs_ta[i]
                                af = min(af + af_step, af_max)
                    else:
                        sar_val = prev_sar + af * (ep - prev_sar)
                        sar_val = max(sar_val, highs_ta[i - 1])
                        if i >= 2:
                            sar_val = max(sar_val, highs_ta[i - 2])
                        if highs_ta[i] > sar_val:
                            is_long = True
                            sar_val = ep
                            ep = highs_ta[i]
                            af = af_step
                        else:
                            if lows_ta[i] < ep:
                                ep = lows_ta[i]
                                af = min(af + af_step, af_max)
                fund["psar"] = round(sar_val, 4)
                fund["psar_direction"] = "long" if is_long else "short"

            # ── Momentum(10) / ROC(10) ──
            roc_period = 10
            if len(closes) > roc_period:
                fund["momentum_10"] = round(closes[-1] - closes[-1 - roc_period], 4)
                if closes[-1 - roc_period] != 0:
                    fund["roc_10"] = round((closes[-1] / closes[-1 - roc_period] - 1) * 100, 2)

            # ── Chaikin Money Flow(20) ──
            cmf_period = 20
            n_cmf = min(len(closes), len(highs_ta), len(lows_ta), len(volumes_ta))
            if n_cmf >= cmf_period:
                mfv_sum = 0.0
                vol_sum = 0.0
                for i in range(n_cmf - cmf_period, n_cmf):
                    hl_diff = highs_ta[i] - lows_ta[i]
                    if hl_diff > 0:
                        mfm = ((closes[i] - lows_ta[i]) - (highs_ta[i] - closes[i])) / hl_diff
                        mfv_sum += mfm * volumes_ta[i]
                        vol_sum += volumes_ta[i]
                if vol_sum > 0:
                    cmf_val = mfv_sum / vol_sum
                    fund["cmf_20"] = round(cmf_val, 4)
                    if cmf_val > 0.05:
                        fund["cmf_signal"] = "buying_pressure"
                    elif cmf_val < -0.05:
                        fund["cmf_signal"] = "selling_pressure"
                    else:
                        fund["cmf_signal"] = "neutral"

    # Фильтрация по RSI
    if rsi_min is not None:
        filtered = [f for f in filtered if f.get("rsi_14") is not None and f["rsi_14"] >= rsi_min]
    if rsi_max is not None:
        filtered = [f for f in filtered if f.get("rsi_14") is not None and f["rsi_14"] <= rsi_max]

    # Фильтр по MA signal
    if ma_signal:
        ma_lower = ma_signal.lower()
        filtered = [f for f in filtered if f.get("ma_signal", "").lower() == ma_lower]

    # Фильтр по ADX
    if adx_min is not None:
        filtered = [f for f in filtered if f.get("adx") is not None and f["adx"] >= adx_min]

    # Фильтр по MACD signal
    if macd_signal:
        macd_lower = macd_signal.lower()
        filtered = [f for f in filtered if f.get("macd_signal", "").lower() == macd_lower]

    # Фильтр по Stochastic %K
    if stochastic_min is not None:
        filtered = [f for f in filtered if f.get("stochastic_k") is not None and f["stochastic_k"] >= stochastic_min]
    if stochastic_max is not None:
        filtered = [f for f in filtered if f.get("stochastic_k") is not None and f["stochastic_k"] <= stochastic_max]

    # Фильтр по CCI
    if cci_min is not None:
        filtered = [f for f in filtered if f.get("cci_20") is not None and f["cci_20"] >= cci_min]
    if cci_max is not None:
        filtered = [f for f in filtered if f.get("cci_20") is not None and f["cci_20"] <= cci_max]

    # Фильтр по Williams %R
    if williams_min is not None:
        filtered = [f for f in filtered if f.get("williams_r") is not None and f["williams_r"] >= williams_min]
    if williams_max is not None:
        filtered = [f for f in filtered if f.get("williams_r") is not None and f["williams_r"] <= williams_max]

    # Фильтр по Ichimoku signal
    if ichimoku_signal:
        ich_lower = ichimoku_signal.lower()
        filtered = [f for f in filtered if f.get("ichimoku_signal", "").lower() == ich_lower]

    # Фильтр по Parabolic SAR direction
    if psar_direction:
        psar_lower = psar_direction.lower()
        filtered = [f for f in filtered if f.get("psar_direction", "").lower() == psar_lower]

    # Фильтр по Chaikin Money Flow signal
    if cmf_signal:
        cmf_lower = cmf_signal.lower()
        filtered = [f for f in filtered if f.get("cmf_signal", "").lower() == cmf_lower]

    # Фильтр по ROC
    if roc_min is not None:
        filtered = [f for f in filtered if f.get("roc_10") is not None and f["roc_10"] >= roc_min]
    if roc_max is not None:
        filtered = [f for f in filtered if f.get("roc_10") is not None and f["roc_10"] <= roc_max]

    # ── Шаг 9: премия/дисконт и трекинг-ошибка (только для фильтров) ──
    if premium_discount_max is not None:
        # Загружаем iNAV данные
        def _check_premium(fund: dict) -> bool:
            try:
                inav = inav_quote(fund["secid"])
                if inav and inav.get("price") and fund.get("price"):
                    premium = abs((fund["price"] / inav["price"] - 1) * 100)
                    fund["inav_price"] = inav["price"]
                    fund["premium_discount_pct"] = round((fund["price"] / inav["price"] - 1) * 100, 2)
                    return premium <= premium_discount_max
            except Exception:
                pass
            return False

        filtered = [f for f in filtered if _check_premium(f)]

    # ── Шаг 10: сортировка ──
    sort_field_map = {
        "performance": perf_field,
        "volatility": "volatility_ann",
        "sharpe": "sharpe",
        "volume": "value_today",
        "spread": "spread_pct",
        "premium": "premium_discount_pct",
        "rsi": "rsi_14",
        "adx": "adx",
        "beta": "beta",
        "stochastic": "stochastic_k",
        "cci": "cci_20",
        "williams": "williams_r",
        "roc": "roc_10",
        "cmf": "cmf_20",
        "momentum": "momentum_10",
    }
    sort_field = sort_field_map.get(sort_by, perf_field)

    def _sort_key(f: dict):
        val = f.get(sort_field)
        if val is None:
            return float('-inf') if sort_desc else float('inf')
        return val

    filtered.sort(key=_sort_key, reverse=sort_desc)

    # ── Шаг 11: лимит ──
    total_matching = len(filtered)
    shown = filtered[:limit]

    # Очищаем временные поля
    for fund in shown:
        fund.pop("candles", None)
        fund.pop("closes", None)

    return {
        "count_shown": len(shown),
        "count_total_matching": total_matching,
        "count_all_funds": len(all_funds),
        "funds": shown,
    }


def indicative_rates(frm: str | None = None,
                     till: str | None = None) -> list[dict]:
    """Индикативные курсы валют срочного рынка.

    Вход: frm/till ('YYYY-MM-DD', опционально).
    Возвращает: [{tradedate, tradetime, secid, rate, clearing}].
    secid — валютная пара (напр. 'CNY/RUB').
    """
    params: dict = {"limit": 500}
    if frm:
        params["from"] = frm
    if till:
        params["till"] = till
    raw = raw_get("statistics/engines/futures/markets/indicativerates/securities",
                  params)
    return records(raw, "securities")


# ─────────────────── Срочный рынок (фьючерсы / опционы) ───────────────────

def futures_list(asset_code: str) -> list[dict]:
    """Каталог фьючерсных контрактов с рыночными данными и спецификацией.

    Вход: asset_code — код базисного актива (напр. 'Si', 'RTS', 'BR', 'GAZR').
          Регистр не важен ('si' == 'SI' == 'Si').
    Возвращает: [{secid, name, asset_code, expiry_date, lot_volume, min_step,
    step_price, initial_margin, prev_settle_price, last_settle_price,
    open_interest, prev_price, oichange, bid, offer, last, high, low,
    volume_today, value_today, num_trades}].
    """
    raw_securities = raw_get(
        "engines/futures/markets/forts/boards/RFUD/securities",
        {"iss.only": "securities,marketdata"})
    secs = records(raw_securities, "securities")
    mds = records(raw_securities, "marketdata")
    md_by_id = {r["SECID"]: r for r in mds}

    ac_upper = asset_code.upper()
    rows = []
    for s in secs:
        if (s.get("ASSETCODE") or "").upper() != ac_upper:
            continue
        md = md_by_id.get(s["SECID"], {})
        rows.append({
            "secid": s.get("SECID"),
            "name": s.get("SECNAME"),
            "shortname": s.get("SHORTNAME"),
            "asset_code": s.get("ASSETCODE"),
            "expiry_date": s.get("LASTTRADEDATE"),
            "lot_volume": s.get("LOTVOLUME"),
            "min_step": s.get("MINSTEP"),
            "step_price": s.get("STEPPRICE"),
            "initial_margin": s.get("INITIALMARGIN"),
            "prev_settle_price": s.get("PREVSETTLEPRICE"),
            "last_settle_price": s.get("LASTSETTLEPRICE"),
            "open_interest": md.get("OPENPOSITION"),
            "prev_open_interest": s.get("PREVOPENPOSITION"),
            "oichange": md.get("OICHANGE"),
            "prev_price": s.get("PREVPRICE"),
            "bid": md.get("BID"),
            "offer": md.get("OFFER"),
            "spread": md.get("SPREAD"),
            "last": md.get("LAST"),
            "high": md.get("HIGH"),
            "low": md.get("LOW"),
            "volume_today": md.get("VOLTODAY"),
            "value_today": md.get("VALTODAY"),
            "num_trades": md.get("NUMTRADES"),
            "high_limit": s.get("HIGHLIMIT"),
            "low_limit": s.get("LOWLIMIT"),
            "buy_sell_fee": s.get("BUYSELLFEE"),
            "scalper_fee": s.get("SCALPERFEE"),
        })
    return rows


def futures_open_interest(asset: str) -> dict:
    """Открытый интерес по базисному активу (юридические / физические лица).

    Вход: asset — код базисного актива ('Si', 'RTS', 'BR', 'SBRF'...).
    Возвращает: {asset, tradedate, juridical: {persons_long, persons_short,
    oi_long, oi_short, oi_change_long, oi_change_short}, physical: {...},
    total_oi_long, total_oi_short}.
    """
    raw = raw_get(f"statistics/engines/futures/markets/forts/openpositions/{asset}")
    rows = records(raw, "open_positions")
    if not rows:
        return {"asset": asset, "error": "нет данных"}
    jurid = next((r for r in rows if r.get("is_fiz") == 0), {})
    fiz = next((r for r in rows if r.get("is_fiz") == 1), {})
    oi_long = (jurid.get("open_position_long") or 0) + (fiz.get("open_position_long") or 0)
    oi_short = (jurid.get("open_position_short") or 0) + (fiz.get("open_position_short") or 0)
    return {
        "asset": rows[0].get("asset"),
        "tradedate": rows[0].get("tradedate"),
        "juridical": {
            "persons_long": jurid.get("persons_long"),
            "persons_short": jurid.get("persons_short"),
            "oi_long": jurid.get("open_position_long"),
            "oi_short": jurid.get("open_position_short"),
            "oi_change_long": jurid.get("oichange_long"),
            "oi_change_short": jurid.get("oichange_short"),
        },
        "physical": {
            "persons_long": fiz.get("persons_long"),
            "persons_short": fiz.get("persons_short"),
            "oi_long": fiz.get("open_position_long"),
            "oi_short": fiz.get("open_position_short"),
            "oi_change_long": fiz.get("oichange_long"),
            "oi_change_short": fiz.get("oichange_short"),
        },
        "total_oi_long": oi_long,
        "total_oi_short": oi_short,
    }


def futures_series(asset: str | None = None) -> list[dict]:
    """Календарь экспираций фьючерсов.

    Вход: asset — код базисного актива ('Si', 'RTS', ...), опционально.
    Без параметра — все серии.
    Возвращает: [{secid, name, start_date, expiration_date, asset_code,
    underlying_asset, is_traded}].
    """
    raw = raw_get("statistics/engines/futures/markets/forts/series",
                  {"limit": 500})
    rows = records(raw, "series")
    if asset:
        ac_upper = asset.upper()
        rows = [r for r in rows if (r.get("asset_code") or "").upper() == ac_upper]
    today = str(__import__("datetime").date.today())
    for r in rows:
        r["is_expired"] = (r.get("expiration_date") or "") < today
        r["days_to_expiry"] = None
        if r.get("expiration_date"):
            try:
                from datetime import date as _d
                r["days_to_expiry"] = (
                    _d.fromisoformat(r["expiration_date"]) - _d.fromisoformat(today)
                ).days
            except Exception:
                pass
    return rows


def futures_basis(asset_code: str) -> dict:
    """Базис (contango / backwardation) и расчётная годовая доходность.

    Сравнивает цену фьючерса (settle) со спот-ценой базового актива и
    вычисляет годовую ставку переноса (carry): (futures - spot) / spot * 365/days.
    Положительное значение → contango (futures дороже spot), можно «продать
    фьючерс + купить spot» и дождаться конвергенции к экспирации.
    Отрицательное → backwardation (futures дешевле spot).

    Вход: asset_code — код базисного актива ('Si', 'RTS', 'BR', 'GAZR', ...).
    Возвращает: {asset_code, underlying: {price, source}, regime, contracts: [...]}.
    """
    from datetime import date as _date, datetime as _dt

    asset_code = _canonical_asset(asset_code)
    contracts = futures_list(asset_code)
    if not contracts:
        return {"asset_code": asset_code, "error": "нет контрактов"}

    series_map: dict[str, int] = {}
    for sr in futures_series(asset_code):
        did = sr.get("days_to_expiry")
        if did is not None and did >= 0:
            series_map[sr["secid"]] = did

    spot_price, spot_source = _resolve_spot_price(asset_code)
    spot_info = {"price": spot_price, "source": spot_source}

    if not spot_price or spot_price <= 0:
        return {
            "asset_code": asset_code, "underlying": spot_info,
            "regime": "unknown", "contracts": [], "error": "нет спот-цены",
        }

    data = []
    for c in contracts:
        settle = c.get("last_settle_price")
        expiry_date = c.get("expiry_date", "")
        days = series_map.get(c["secid"])
        if days is None and expiry_date:
            try:
                d = _dt.strptime(str(expiry_date)[:10], "%Y-%m-%d").date()
                days = (d - _date.today()).days
            except (ValueError, TypeError):
                try:
                    d = _dt.strptime(str(expiry_date)[:10], "%d.%m.%Y").date()
                    days = (d - _date.today()).days
                except (ValueError, TypeError):
                    days = None
        if settle is None or not expiry_date or days is None or days <= 0:
            continue

        lot = c.get("lot_volume") or 1

        settle_per_unit = float(settle) / lot if lot != 0 else float(settle)
        ann = (settle_per_unit / spot_price - 1) * 36500.0 / days
        data.append({
            "secid": c["secid"], "name": c.get("name"),
            "expiry_date": str(expiry_date)[:10],
            "days_to_expiry": days,
            "futures_price": float(settle),
            "futures_price_per_unit": round(settle_per_unit, 6),
            "spot_price": spot_price,
            "basis_pct": round((settle_per_unit / spot_price - 1) * 100, 4),
            "annualized_return_pct": round(ann, 4),
            "open_interest": c.get("open_interest"),
        })

    regime = "unknown"
    if data:
        regime = "contango" if data[0]["annualized_return_pct"] >= 0 else "backwardation"

    return {
        "asset_code": asset_code,
        "underlying": spot_info,
        "regime": regime,
        "contracts": sorted(data, key=lambda x: x["expiry_date"]),
    }


def futures_promo() -> dict:
    """Агрегированная статистика срочного рынка (FORTS).

    Возвращает: {fee_forts, fee_options, fee_all, updated_at}.
    """
    raw = raw_get("statistics/engines/futures/promo")
    rows = records(raw, "futures_promo")
    return rows[0] if rows else {}


def options_assets() -> list[dict]:
    """Базисные активы опционов FORTS с рыночными данными.

    Возвращает: [{tradedate, asset, asset_name, asset_type, asset_last_price,
    asset_last_to_prev, asset_high, asset_low, val_today, vol_today, num_trades,
    open_position, oichange, option_secid, margin_style, option_on_spot}].
    """
    raw = raw_get("statistics/engines/futures/markets/options/assets",
                  {"limit": 500})
    return records(raw, "asset_volumes")


def _resolve_option_underlying(asset: str) -> str | None:
    """Найти реальный код базисного актива (фьючерсной серии) для опционной доски.

    Для акций (GAZP, SBER) код совпадает с тикером — statistics работает напрямую.
    Для фьючерсов (Si, GAZR, BR) statistics требует код серии (SiU6, GZU6, BRU6),
    а не общий код — нужен резолв через regular ISS.
    """
    raw = raw_get(
        "engines/futures/markets/options/boards/ROPD/securities",
        {"iss.only": "securities", "limit": 10000,
         "securities.columns": "ASSETCODE,UNDERLYINGASSET"})
    for row in records(raw, "securities"):
        if row.get("ASSETCODE") == asset:
            under = row.get("UNDERLYINGASSET")
            if under and under != asset:
                return under
    return None


def _build_optionboard(raw: dict) -> dict:
    """Построить dict опционной доски из raw JSON ответа statistics."""
    call_rows = records(raw, "call")
    put_rows = records(raw, "put")
    asset_rows = records(raw, "asset")
    return {
        "asset_info": asset_rows[0] if asset_rows else {},
        "calls": [{"secid": r.get("SECID"), "strike": r.get("STRIKE"),
                    "iv": r.get("VOLAT"), "last": r.get("LAST"),
                    "theor_price": r.get("THEORPRICE"),
                    "bid": r.get("BID"), "offer": r.get("OFFER"),
                    "oi": r.get("OPENPOSITION"), "volume": r.get("VOLTODAY")}
                   for r in call_rows],
        "puts": [{"secid": r.get("SECID"), "strike": r.get("STRIKE"),
                   "iv": r.get("VOLAT"), "last": r.get("LAST"),
                   "theor_price": r.get("THEORPRICE"),
                   "bid": r.get("BID"), "offer": r.get("OFFER"),
                   "oi": r.get("OPENPOSITION"), "volume": r.get("VOLTODAY")}
                  for r in put_rows],
    }


def options_board(asset: str) -> dict:
    """Опционная доска по базисному активу (call + put + параметры).

    Вход: asset — код базисного ('Si', 'GAZP', 'SBRF', 'GAZR'...).
    Возвращает: {asset_info: {central_strike, underlying_settle, last_del_date},
    calls: [{secid, strike, iv, last, theor_price, bid, offer, oi, volume}],
    puts: [同]}.
    Для фьючерсных базисных активов (Si, GAZR, BR...) автоматически
    резолвит код серии (SiU6, GZU6...) через regular ISS.
    """
    raw = raw_get(
        f"statistics/engines/futures/markets/options/assets/{asset}/optionboard",
        {"limit": 200})
    call_rows = records(raw, "call")
    put_rows = records(raw, "put")
    if call_rows or put_rows:
        return _build_optionboard(raw)

    # Фьючерсные активы: код серии != коду активу. Резолвим.
    real = _resolve_option_underlying(asset)
    if real and real != asset:
        raw = raw_get(
            f"statistics/engines/futures/markets/options/assets/{real}/optionboard",
            {"limit": 500})
        result = _build_optionboard(raw)
        if result["calls"] or result["puts"]:
            return result

    # Намеренно возвращаем пустой результат — не падаем
    return {"asset_info": {}, "calls": [], "puts": []}


def option_quote(secid: str) -> dict:
    """Котировка опционного инструмента (рыночные данные + спецификация).

    Вход: secid — код инструмента ('Si87000BI6A', 'GZ85CU6A'...).
    Возвращает: {secid, shortname, strike, option_type,
    underlying_asset, underlying_settle, expiration_date, last_trade_date,
    last, bid, offer, oi, volume,
    open, high, low, settle_price, num_trades,
    im_np, im_sp, im_buy, ...}.
    """
    raw = raw_get(
        f"engines/futures/markets/options/boards/ROPD/securities/{secid}",
        {"iss.only": "securities,marketdata"})
    sec = records(raw, "securities")
    md = records(raw, "marketdata")
    s = sec[0] if sec else {}
    m = md[0] if md else {}
    return {
        "secid": s.get("SECID"),
        "shortname": s.get("SHORTNAME"),
        "secname": s.get("SECNAME"),
        "assetcode": s.get("ASSETCODE"),
        "option_type": s.get("OPTIONTYPE"),
        "strike": s.get("STRIKE"),
        "underlying_asset": s.get("UNDERLYINGASSET"),
        "underlying_settle": s.get("UNDERLYINGSETTLEPRICE"),
        "expiration_date": s.get("LASTDELDATE"),
        "last_trade_date": s.get("LASTTRADEDATE"),
        "min_step": s.get("MINSTEP"),
        "step_price": s.get("STEPPRICE"),
        "prev_settle": s.get("PREVSETTLEPRICE"),
        "prev_oi": s.get("PREVOPENPOSITION"),
        "last": m.get("LAST"),
        "bid": m.get("BID"),
        "offer": m.get("OFFER"),
        "spread": m.get("SPREAD"),
        "open": m.get("OPEN"),
        "high": m.get("HIGH"),
        "low": m.get("LOW"),
        "volume": m.get("VOLTODAY"),
        "value": m.get("VALTODAY"),
        "num_trades": m.get("NUMTRADES"),
        "oi": m.get("OPENPOSITION"),
        "oi_change": m.get("OICHANGE"),
        "settle_price": m.get("SETTLEPRICE"),
        "last_change": m.get("LASTCHANGE"),
        "last_change_pct": m.get("LASTCHANGEPRCNT"),
        "update_time": m.get("UPDATETIME"),
        "im_np": s.get("IMNP"),
        "im_sp": s.get("IMP"),
        "im_buy": s.get("IMBUY"),
    }


def option_orderbook(secid: str) -> dict:
    """Стакан опционного инструмента (лучшие bid/offer из котировок).

    Стакан (depth-of-market) для опционов недоступен через ISS REST API
    (эндпоинт /orderbook возвращает HTML). Возвращаем лучшие bid/offer
    из блока marketdata.

    Вход: secid — код инструмента ('Si87000BI6A', 'GZ85CU6A'...).
    Возвращает: {secid, bid, offer, spread, bid_depth, offer_depth,
    bid_depth_total, offer_depth_total}.
    """
    raw = raw_get(
        f"engines/futures/markets/options/boards/ROPD/securities/{secid}",
        {"iss.only": "marketdata"})
    md = records(raw, "marketdata")
    m = md[0] if md else {}
    return {
        "secid": secid,
        "bid": m.get("BID"),
        "offer": m.get("OFFER"),
        "spread": m.get("SPREAD"),
        "bid_depth": m.get("BIDDEPTH"),
        "offer_depth": m.get("OFFERDEPTH"),
        "bid_depth_total": m.get("BIDDEPTHT"),
        "offer_depth_total": m.get("OFFERDEPTHT"),
    }


def option_history(secid: str, frm: str | None = None, till: str | None = None) -> list[dict]:
    """История сделок опционного инструмента.

    Вход: secid — код инструмента; frm/till — даты 'YYYY-MM-DD' (опционально).
    Возвращает: [{tradedate, close, open, high, low, volume, value,
    oi, oi_value, settle_price, waprice, num_trades, theor_price, change, qty}].
    """
    params: dict = {"limit": 500}
    if frm:
        params["from"] = frm
    if till:
        params["till"] = till
    raw = raw_get(
        f"history/engines/futures/markets/options/boards/ROPD/securities/{secid}",
        params)
    rows = records(raw, "history")
    result = []
    for r in rows:
        result.append({
            "tradedate": r.get("TRADEDATE"),
            "secid": r.get("SECID"),
            "close": r.get("CLOSE"),
            "open": r.get("OPEN"),
            "high": r.get("HIGH"),
            "low": r.get("LOW"),
            "volume": r.get("VOLUME"),
            "value": r.get("VALUE"),
            "oi": r.get("OPENPOSITION"),
            "oi_value": r.get("OPENPOSITIONVALUE"),
            "settle_price": r.get("SETTLEPRICE"),
            "waprice": r.get("WAPRICE"),
            "num_trades": r.get("NUMTRADES"),
            "theor_price": r.get("THEOR_PRICE"),
            "change": r.get("CHANGE"),
            "qty": r.get("QTY"),
        })
    return result
