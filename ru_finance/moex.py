"""Резолв тикеров и нормализованный доступ к данным MOEX поверх aioboy/moex.

Наружу даём чистые dict'ы со стабильными ключами — сырые ISS-колонки
(LAST/LCLOSEPRICE/MARKETPRICE/WAPRICE, DURATION в днях и т.п.) и выбор борда
спрятаны здесь.
"""
from __future__ import annotations

from .session import exec_template, first, get_moex, raw_get, records

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


def resolve(query: str, sec_type: str | None = None,
            as_list: bool = False,
            traded_only: bool = False) -> dict | list[dict]:
    """Тикер/ISIN/название -> {secid, engine, market, board, type, ...}.

    sec_type — если задан (напр. "bond", "share", "fund"), фильтрует по типу
    бумаги (stock_bonds, stock_shares, ...). Без фильтра возвращает лучшую
    бумагу по скору.

    as_list — если True, возвращает все совпадения (до 200) вместо одного.
    traded_only — если True, отсечь бумаги с is_traded!=1 уже в запросе к ISS.
    """
    limit = 200 if as_list else 50
    q = query.strip()
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
    """Облигация: цена %, YTM, дюрация (годы), модиф. дюрация, купон, погашение, НКД."""
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


def correlations(secid: str) -> list[dict]:
    """Коэффициенты корреляции и бета для бумаги.

    Вход: secid (напр. 'SBER'). Возвращает: [{secid, fxsecid, tradedate,
    coeff_correlation, coeff_beta}, ...] — все пары с другими бумагами.
    """
    raw = raw_get("statistics/engines/stock/markets/shares/correlations",
                  {"limit": 5000})
    rows = records(raw, "coefficients")
    return [r for r in rows if r.get("SECID") == secid.upper()]


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

def futures_list(asset_code: str | None = None) -> list[dict]:
    """Каталог фьючерсных контрактов с рыночными данными и спецификацией.

    Вход: asset_code — код базисного актива (напр. 'Si', 'RTS', 'BR', 'GAZR').
          Без параметра — все торгуемые контракты.
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

    rows = []
    for s in secs:
        if asset_code and s.get("ASSETCODE") != asset_code:
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
        rows = [r for r in rows if r.get("asset_code") == asset]
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
