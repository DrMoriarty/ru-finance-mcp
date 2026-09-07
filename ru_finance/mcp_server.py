"""ru-finance MCP-сервер: инструменты для анализа портфеля поверх moex + cbrapi.

Запуск локально (stdio):   python -m ru_finance.mcp_server
Запуск как remote (HTTP):  MCP_TRANSPORT=streamable-http MCP_PORT=8000 python -m ru_finance.mcp_server
  → все инструменты:        http://MCP_HOST:MCP_PORT/mcp
  → группы (одна):          http://MCP_HOST:MCP_PORT/g/{group}/mcp
  → роли (набор групп):     http://MCP_HOST:MCP_PORT/{role}/mcp
    роли: screener, analyst, constructor, timer, macrotracker,
          risk_manager, instrument_specialist, portfolio_manager
  (за nginx/TLS, см. deploy/).
Все инструменты generic — конкретные бумаги передаются параметром (portfolio_* → assets).
Документация ручек: docs/TOOLS.md. Гайд для агента: AGENTS.md.
"""
from __future__ import annotations

import base64
import os
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.streamable_http import EventCallback, EventId, EventMessage, EventStore, StreamId
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon, ToolAnnotations

from . import bonds, cbr, fundamental, moex, portfolio, raexpert, rate, smartlab, vsezpif

# ─────────────────────────── Tool Groups & Roles ───────────────────────────
# Каждый инструмент принадлежит ровно одной группе. Роль = набор групп.
# Роли и группы — просто константы; сервер собирает tool-сеты динамически.
GROUPS: dict[str, set[str]] = {
    # Резолв, котировки, datetime
    "core_lookup": {
        "current_datetime", "moex_resolve", "moex_search", "moex_quote", "moex_bond",
    },
    # Рыночные показатели широкого плана
    "market_data": {
        "moex_turnovers", "moex_indicative_rates",
    },
    # OHLCV свечи и история торгов
    "price_history": {
        "moex_candles", "moex_history", "moex_full_history", "moex_aggregates",
    },
    # Фундаментал компаний + фундаментальный скоринг + дивиденды
    "fundamental": {
        "moex_company_info", "moex_company_info_by_id",
        "moex_market_capitalization", "moex_ir_calendar", "moex_sitenews",
        "smartlab_dividends", "smartlab_dividend_history",
        "smartlab_company_financials", "smartlab_company_financials_multi",
        "stock_f_score", "stock_z_score",
        "stock_peer_comparison", "stock_growth_analysis",
        "dividend_analysis", "bank_benchmark", "bank_peer_comparison",
        "company_fundamental_report",
    },
    # Индикаторы, волатильность, сплиты
    "technical": {
        "price_volatility", "liquidity_assessment", "technical_indicators", "moex_splits",
    },
    # Скринеры, ранжирование, сравнительный анализ
    "screening": {
        "smartlab_stock_screener", "bond_screener", "bond_prescreener", "etf_screener",
        "raexpert_emitent_ratings", "moex_correlations",
    },
    # Обнаружение ISS-эндпоинтов
    "discover": {
        "moex_search_endpoints", "moex_query",
    },
    # Ставки, инфляция, кривая доходности
    "macro": {
        "cbr_key_rate", "cbr_inflation", "cbr_ruonia", "cbr_ruonia_index", "cbr_ibor",
        "rate_expectations", "curve_yield",
    },
    # FX, металлы, ЗВР
    "fx_metals": {
        "cbr_currency", "cbr_metals", "cbr_reserves",
    },
    # Инструменты облигаций
    "fixed_income": {
        "moex_emitent_bonds", "moex_bond_coupons",
        "moex_bond_market_aggregates", "moex_zcyc_history",
        "bond_report", "bond_accrued_interest", "bond_synthetic_yield",
        "raexpert_rating", "zpif_payments", "zpif_funds_list",
    },
    # ETF/БПИФ
    "etf": {
        "etf_fund_info", "etf_premium_discount", "etf_tracking_error",
    },
    # Фьючерсы и опционы
    "derivatives": {
        "moex_futures_list", "moex_futures_open_interest",
        "moex_futures_series", "moex_futures_promo", "moex_futures_basis",
        "moex_options_assets", "moex_options_board",
        "moex_option_quote", "moex_option_orderbook", "moex_option_history",
    },
    # Портфельный риск: снимок, сценарии, доходы
    "risk": {
        "portfolio_snapshot", "portfolio_rate_whatif",
        "portfolio_income_calendar", "portfolio_movers",
    },
}

# Роли: каждая — набор групп, подключаемых агенту.
# group может входить в несколько ролей.
ROLES: dict[str, set[str]] = {
    # Скринер: обнаружение инструментов из широкой вселенной
    "screener": {
        "core_lookup", "discover", "screening", "fundamental", "macro",
    },
    # Аналитик: глубокий анализ конкретного инструмента
    "analyst": {
        "core_lookup", "price_history", "fundamental", "technical",
        "fixed_income", "etf",
    },
    # Конструктор: формирование портфеля из отобранных активов
    "constructor": {
        "core_lookup", "screening", "risk", "fixed_income", "etf",
    },
    # Таймер: определение оптимальной точки входа/выхода
    "timer": {
        "core_lookup", "price_history", "technical", "derivatives",
    },
    # Макро-трекер: мониторинг ставок и макроэкономических трендов
    "macrotracker": {
        "core_lookup", "macro", "fx_metals", "fixed_income",
    },
    # Риск-менеджер: контроль рисков позиций и портфеля
    "risk_manager": {
        "core_lookup", "risk", "screening", "macro", "technical", "fixed_income", "derivatives",
    },
    # Специалист по инструментам: ETF, производные
    "instrument_specialist": {
        "core_lookup", "etf", "derivatives", "technical", "price_history",
    },
    # Портфельный менеджер: обслуживание и мониторинг портфеля
    "portfolio_manager": {
        "core_lookup", "risk", "screening", "fundamental", "macro", "fixed_income",
    },
}

# Обратный индекс: tool_name → group (каждый инструмент ровно в одной группе)
_TOOL_GROUP: dict[str, str] = {}
for _g, _names in GROUPS.items():
    for _n in _names:
        _TOOL_GROUP[_n] = _g


def _assign_tool_groups(mcp_instance: FastMCP) -> None:
    """Tag every registered tool with group metadata."""
    for tool in mcp_instance._tool_manager._tools.values():
        group = _TOOL_GROUP.get(tool.name)
        if tool.meta is None:
            tool.meta = {}
        tool.meta["group"] = group


def _tools_for_groups(groups: set[str], source: FastMCP) -> dict[str, object]:
    """Return tools from *source* matching any of the given *groups*."""
    allowed: set[str] = set()
    for g in groups:
        allowed |= GROUPS.get(g, set())
    return {
        t.name: t for t in source._tool_manager._tools.values()
        if t.name in allowed
    }


def _copy_resources(source: FastMCP, target: FastMCP) -> None:
    """Copy all resources and resource templates from *source* to *target*."""
    target._resource_manager._resources = dict(source._resource_manager._resources)
    target._resource_manager._templates = dict(source._resource_manager._templates)


def _create_group_server(group: str, source: FastMCP) -> FastMCP:
    """Create a FastMCP instance for a single tool group."""
    g_mcp = FastMCP(
        name=f"ru-finance-{group}",
        icons=_load_icons(),
        host=source.settings.host,
        port=source.settings.port,
        streamable_http_path="/mcp",
        stateless_http=source.settings.stateless_http,
        event_store=InMemoryEventStore(),
        retry_interval=5,
        transport_security=source.settings.transport_security,
        warn_on_duplicate_tools=False,
    )
    g_mcp._tool_manager._tools = _tools_for_groups({group}, source)
    _copy_resources(source, g_mcp)
    return g_mcp


def _create_role_server(role: str, source: FastMCP) -> FastMCP:
    """Create a FastMCP instance for a composed role (union of its groups)."""
    r_mcp = FastMCP(
        name=f"ru-finance-{role}",
        icons=_load_icons(),
        host=source.settings.host,
        port=source.settings.port,
        streamable_http_path="/mcp",
        stateless_http=source.settings.stateless_http,
        event_store=InMemoryEventStore(),
        retry_interval=5,
        transport_security=source.settings.transport_security,
        warn_on_duplicate_tools=False,
    )
    r_mcp._tool_manager._tools = _tools_for_groups(ROLES[role], source)
    _copy_resources(source, r_mcp)
    return r_mcp


class InMemoryEventStore(EventStore):
    """Simple in-memory event store for SSE resumability.

    Stores events per stream_id, allows replay after a given event_id.
    Bounded: keeps at most _MAX_EVENTS per stream.
    """

    _MAX_EVENTS = 256

    def __init__(self) -> None:
        self._events: dict[StreamId, list[tuple[EventId, EventMessage | None]]] = defaultdict(list)
        self._counter = 0

    async def store_event(self, stream_id: StreamId, message: EventMessage | None) -> EventId:
        self._counter += 1
        event_id = f"evt-{self._counter}"
        events = self._events[stream_id]
        events.append((event_id, message))
        if len(events) > self._MAX_EVENTS:
            del events[: len(events) - self._MAX_EVENTS]
        return event_id

    async def replay_events_after(
        self,
        last_event_id: EventId,
        send_callback: EventCallback,
    ) -> StreamId | None:
        for stream_id, events in self._events.items():
            for idx, (eid, msg) in enumerate(events):
                if eid == last_event_id:
                    for _, m in events[idx + 1 :]:
                        if m is not None:
                            await send_callback(m)
                    return stream_id
        return None


def _freq_from_coupon_period(period_days: int | None) -> int:
    """Частота купонов в год (1, 2, 4, 6, 12) из периода в днях."""
    if not period_days or period_days <= 0:
        return 2
    _CANONICAL = {365: 1, 182: 2, 183: 2, 91: 4, 92: 4, 61: 6, 30: 12, 31: 12}
    if period_days in _CANONICAL:
        return _CANONICAL[period_days]
    freq = round(365 / period_days)
    return max(1, min(freq, 12))


def _load_icons() -> list[Icon] | None:
    """Иконка сервера (PT Serif ₽, изумруд) как data-URI. Рендерят Inspector/VS Code/Desktop."""
    path = Path(__file__).parent / "icon.png"
    if not path.exists():
        return None
    data = base64.b64encode(path.read_bytes()).decode()
    return [Icon(src=f"data:image/png;base64,{data}", mimeType="image/png", sizes=["256x256"])]


_event_store = InMemoryEventStore()

# ── Annotation presets (all ru-finance tools are read-only data fetchers) ──
_RO = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
_RO_OPEN = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=True)


def _tool(**kw):
    """Shorthand: @_tool() with universal read-only annotations."""
    kw.setdefault("annotations", _RO)
    return mcp.tool(**kw)


def _tool_open(**kw):
    """Shorthand: @_tool() with open-world read-only annotations (MOEX/SmartLab/RAExpert)."""
    kw.setdefault("annotations", _RO_OPEN)
    return mcp.tool(**kw)


mcp = FastMCP(
    "ru-finance",
    icons=_load_icons(),
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8000")),
    stateless_http=False,  # stateful: SSE resumability via event store
    event_store=_event_store,
    retry_interval=5,  # seconds — SSE reconnect interval for clients
    # Сервер рассчитан на работу за reverse-proxy (nginx) при remote-доступе.
    # Встроенная в SDK DNS-rebinding защита пускает только localhost-Host и режет
    # проксированные запросы (421 Invalid Host header); доступ ограничивается на
    # уровне прокси (TLS + секретный путь / IP-allowlist), поэтому отключаем её.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# ─────────────────────────── Utilities ───────────────────────────
@_tool()
def current_datetime() -> dict:
    """Server date and time (UTC+0, ISO 8601).

    IMPORTANT: Always call this FIRST before using any historical data tools.
    Many instruments require an explicit end date — use the returned `date` as
    `last_date` (or `date_to`) to avoid confusing results or off-by-one errors.
    """
    now = datetime.now()
    return {
        "datetime": now.isoformat(),
        "date": now.date().isoformat(),
        "time": now.time().isoformat(),
        "timestamp": now.timestamp(),
    }


# ─────────────────────────── MOEX (Moscow Exchange) ───────────────────────────
@_tool()
def moex_resolve(query: str) -> dict:
    """Lookup a single security by ticker/ISIN/name.

    Args: query — 'SBER', 'RU000A10C6F7', '26253', 'Сбербанк'.
    Returns: {secid, engine, market, board, type, shortname, isin, group, is_traded}.
    For multiple matches use moex_search.
    """
    return moex.resolve(query)


@_tool()
def moex_search(query: str, sec_type: str | None = None) -> list[dict]:
    """Search MOEX securities by ticker/ISIN/name.

    Args: query, sec_type — optional filter (ref://moex-sec-types).
    Returns [{secid, shortname, isin, type, group, is_traded, engine, market, board}].
    Only actively traded (is_traded=1) are returned.
    """
    return moex.resolve(query, sec_type=sec_type, as_list=True, traded_only=True)


@_tool()
def moex_quote(query: str) -> dict:
    """Normalized quote for a share/fund with fallback pricing.

    Args: query — ticker or name.
    Returns: {secid, price, change_pct, bid, ask, open, low, high, value_today,
    vol_today, updatetime, price_field}.
    On weekends LAST is empty → falls back to MARKETPRICE/LCLOSEPRICE (see price_field).
    """
    return moex.quote(query)


@_tool()
def moex_bond(query: str) -> dict:
    """Bond data: price %, YTM, duration (years & modified), coupon, maturity, accrued interest.

    For bonds with step-down/variable coupons, YTM, duration_years and mod_duration_years
    returned by MOEX ISS may be incorrect (computed assuming flat coupon_pct).
    Use bond_report for accurate calculations with real coupon schedule.

    Args: query — OFZ number ('26253') or ISIN ('RU000A10C6F7').
    Returns: {price_pct, ytm, duration_years, mod_duration_years, coupon_pct,
    annual_coupon_per_bond, next_coupon, maturity, accrued_int, face_value, ...}.
    """
    return moex.bond(query)


@_tool()
async def moex_emitent_bonds(
    query: str,
    ctx: Context,
    min_duration: float | None = None,
    max_duration: float | None = None,
) -> list[dict]:
    """All bonds of an issuer with optional duration filter.

    Args: query — issuer name/ticker ('Газпром', 'ГТЛК').
    min_duration/max_duration — Macaulay duration (years), None=unbound.
    Returns [{secid, shortname, isin, coupon_pct, maturity, price_pct, ytm,
    duration_years, accrued_int, value_today, ...}]. Sorted by duration.
    """
    await ctx.report_progress(0, 2, "Fetching issuer bonds")
    return moex.emitent_bonds(query, min_duration, max_duration)


@_tool()
def moex_bond_coupons(query: str) -> list[dict]:
    """Coupon schedule (past + future) from NSD/MOEX.

    Args: query — ISIN or OFZ number.
    Returns [{coupondate, value, valueprc, facevalue, faceunit, is_past, recorddate, startdate}].
    Returns empty list for OFZ (CBR data not in ISS). is_past=True → already paid.
    """
    return moex.bond_coupons(query)


@_tool()
def moex_candles(query: str, frm: str, till: str, interval: str = "") -> list[dict]:
    """OHLCV candles for a period.

    Args: query (required, non-empty); interval: 1,10,60(hour),24(day),
    7(week),31(month),4(quarter); empty=auto-select (≤50 candles).
    frm/till ('YYYY-MM-DD').
    Returns [{begin, open, high, low, close, value, volume}].
    """
    return moex.candles(query, frm, till, interval)


@_tool()
def moex_full_history(query: str, frm: str, till: str) -> list[dict]:
    """Daily trading history with all fields for a date range.

    Args: query; frm/till ('YYYY-MM-DD').
    Returns [{TRADEDATE, CLOSE, VOLUME, VALUE, ...}] — full row per trading day.
    WARNING: large date ranges (>1 month) may return A LOT of rows.
    """
    return moex.history(query, frm, till)


@_tool()
def moex_history(query: str, frm: str, till: str) -> list[dict]:
    """Daily trading history with minimal fields for a date range.

    Args: query; frm/till ('YYYY-MM-DD').
    Returns [{TRADEDATE, CLOSE, VOLUME}] — compact data per trading day.
    """
    full = moex.history(query, frm, till)
    return [{"TRADEDATE": r["TRADEDATE"], "CLOSE": r["CLOSE"], "VOLUME": r["VOLUME"]} for r in full]


@_tool()
def moex_search_endpoints(pattern: str) -> list[dict]:
    """Find ISS endpoints by path substring (for raw data access).

    Args: pattern — e.g. '/candles', '/dividends', 'turnovers'.
    Returns [{id, path, variables}]. Use id in moex_query.
    """
    return moex.search_endpoints(pattern)


@_tool()
def moex_query(template_id: int, path_vars: dict | None = None,
               query_params: dict | None = None) -> dict:
    """Generic access to any ISS endpoint by template_id.

    Args: template_id (from moex_search_endpoints); path_vars — (engine/market/board/security...);
    query_params — (from/till/...).
    Returns: {block: [rows]}. Fallback when no named tool exists.
    """
    return moex.query(template_id, path_vars, query_params)


# ───────────────────── MOEX: corporate info (CCI/NSD) ─────────────────────
@_tool()
def moex_company_info(query: str) -> dict:
    """Company lookup by INN/OGRN/name.

    Args: query — INN, OGRN, ticker or company name fragment.
    Returns: {companies: [{basis_company_id, inn, name_short_ru, name_full_ru, okpo, secid}]}.
    Deduplicates by emitent_id; max 20 results.
    """
    return moex.company_info(query)


@_tool()
def moex_company_info_by_id(company_id: int) -> dict:
    """Company lookup by internal MOEX ID (basis_company_id).

    Args: company_id — numeric ID (from moex_company_info).
    Returns: {basis_company_id, inn, name_short_ru, name_full_ru, okpo, secid, ...} or {}.
    """
    return moex.company_info_by_id(company_id)


@_tool()
def moex_ir_calendar(limit: int = 50) -> list[dict]:
    """IR events calendar (earnings dates for public companies).

    Args: limit (default 50).
    Returns: [{company_name, event_type, event_date, ...}].
    """
    return moex.ir_calendar(limit)


# ───────────────────── MOEX: market stats ─────────────────────
@_tool()
def moex_market_capitalization() -> dict:
    """Stock market capitalization (₽).

    Returns: {capitalization, issuecapitalization, tradedate, updatetime}.
    """
    return moex.market_capitalization()


@_tool()
def moex_correlations(secid: str) -> list[dict]:
    """Correlation coefficients and beta for a security.

    Args: secid — ticker, e.g. 'SBER'.
    Returns [{secid, fxsecid, tradedate, coeff_correlation, coeff_beta}].
    """
    return moex.correlations(secid)


@_tool()
def moex_splits(secid: str | None = None) -> list[dict]:
    """Splits and reverse-splits reference.

    Args: secid (optional). Without arg — all splits.
    Returns [{tradedate, secid, before, after}].
    """
    return moex.splits(secid)


# ───────────────────── MOEX: bond market ─────────────────────
@_tool()
def moex_bond_market_aggregates(frm: str | None = None,
                                till: str | None = None) -> list[dict]:
    """Aggregated bond market indicators.

    Args: frm/till ('YYYY-MM-DD', optional).
    Returns [{tradedate, type_bond, iss_nominal, vol_nominal, avg_years, ...}].
    """
    return moex.bond_market_aggregates(frm, till)


@_tool()
def moex_zcyc_history(frm: str, till: str) -> list[dict]:
    """ZCYC (Zero-Coupon Yield Curve) parameters history.

    Args: frm/till ('YYYY-MM-DD').
    Returns [{tradedate, b1,b2,b3, t1, g1..g9}] — NSS model params for backtesting.
    """
    return moex.zcyc_history(frm, till)


# ───────────────────── MOEX: activity and rates ─────────────────────
@_tool()
def moex_turnovers() -> list[dict]:
    """Aggregated trading volumes by market (exchange summary).

    Returns [{name, valtoday, valtoday_usd, numtrades, updatetime, title}].
    Markets: stock, currency, futures, commodity, etc.
    """
    return moex.turnovers()


@_tool()
def moex_sitenews(limit: int = 20) -> list[dict]:
    """Moscow Exchange news feed.

    Args: limit (default 20).
    Returns [{id, tag, title, published_at}].
    """
    return moex.sitenews(limit)


@_tool()
def moex_aggregates(query: str, date: str) -> dict:
    """Daily trading summary for a security.

    Args: query (ticker), date ('YYYY-MM-DD').
    Returns: {securities: [...], marketdata: [...]}.
    """
    return moex.aggregates(query, date)


@_tool()
def moex_indicative_rates(frm: str | None = None,
                          till: str | None = None) -> list[dict]:
    """Indicative FX rates from derivatives market.

    Args: frm/till ('YYYY-MM-DD', optional).
    Returns [{tradedate, tradetime, secid, rate, clearing}].
    """
    return moex.indicative_rates(frm, till)


# ───────────────────── Derivatives (futures / options) ─────────────────────
@_tool()
def moex_futures_list(asset_code: str) -> list[dict]:
    """FORTS futures contracts catalog with market data and spec.

    asset_code — ref://futures-underlying-assets (case-insensitive, e.g. 'Si', 'RTS', 'BR').
    Returns [{secid, name, expiry_date, last_settle_price, open_interest, bid, offer, ...}].
    """
    return moex.futures_list(asset_code)


@_tool()
def moex_futures_open_interest(asset: str) -> dict:
    """Open interest breakdown by legal/physical persons.

    asset — ref://futures-underlying-assets (case-insensitive).
    Returns: {asset, tradedate, juridical: {oi_long, oi_short, ...},
    physical: {...}, total_oi_long, total_oi_short}.
    """
    return moex.futures_open_interest(asset)


@_tool()
def moex_futures_series(asset: str | None = None) -> list[dict]:
    """Futures expiration calendar — contracts with settlement dates.

    Args: asset — underlying code ('Si', 'RTS', ...), optional. None → all series (up to 500).
    Returns [{secid, name, start_date, expiration_date, asset_code,
    underlying_asset, is_traded, is_expired, days_to_expiry}].
    days_to_expiry: < 0 → already expired.
    """
    return moex.futures_series(asset)


@_tool()
def moex_futures_promo() -> dict:
    """FORTS aggregated fee statistics.

    Returns: {fee_forts, fee_options, fee_all, updated_at}.
    """
    return moex.futures_promo()


@_tool()
async def moex_futures_basis(asset_code: str, ctx: Context) -> dict:
    """Futures contango / backwardation — annualised carry from basis.

    Compares futures settle to spot (FX→CBR, equity/commodity→MOEX) and annualises the spread.
    Positive → contango; negative → backwardation. asset_code — ref://futures-underlying-assets.
    Returns: {asset_code, regime, contracts: [{secid, expiry_date, futures_price,
    spot_price, basis_pct, annualized_return_pct, open_interest}]}.
    """
    await ctx.report_progress(0, 3, "Fetching futures data")
    return moex.futures_basis(asset_code)


@_tool()
def moex_options_assets() -> list[dict]:
    """FORTS options underlying assets with market data.

    Returns [{tradedate, asset, asset_name, asset_type, asset_last_price,
    asset_last_to_prev, asset_high, asset_low, val_today, vol_today, num_trades,
    open_position, oichange, option_secid}].
    """
    return moex.options_assets()


@_tool()
def moex_options_board(asset: str) -> dict:
    """Option board (volatility, strikes, OI) for underlying.

    Args: asset — underlying code ('Si', 'RTS', 'SBRF', 'GAZR', ...).
    Returns: {asset_info: {central_strike, underlying_settle, last_del_date},
    calls: [{secid, strike, iv, last, theor_price, bid, offer, oi, volume}],
    puts: [same]}.
    """
    return moex.options_board(asset)


@_tool()
def moex_option_quote(secid: str) -> dict:
    """Single option instrument quote.

    Args: secid — instrument code ('Si87000BI6A', 'GZ85CU6A', ...).
    Returns: {secid, shortname, strike, option_type,
    underlying_asset, underlying_settle, expiration_date, last_trade_date,
    last, bid, offer, spread, oi, volume, settle_price, ...,
    margin: im_np, im_sp, im_buy}.
    """
    return moex.option_quote(secid)


@_tool()
def moex_option_orderbook(secid: str) -> dict:
    """Best bid/offer for an option instrument.

    Note: full depth-of-market unavailable via ISS REST (endpoint returns HTML). Returns best bid/offer and spread.

    Args: secid — instrument code ('Si87000BI6A', 'GZ85CU6A', ...).
    Returns: {secid, bid, offer, spread, bid_depth, offer_depth}.
    """
    return moex.option_orderbook(secid)


@_tool()
def moex_option_history(secid: str, frm: str | None = None,
                        till: str | None = None) -> list[dict]:
    """Option trade history.

    Args: secid — instrument code ('Si87000BI6A', 'GZ85CU6A', ...);
    frm/till — 'YYYY-MM-DD' (optional).
    Returns [{tradedate, close, open, high, low, volume, value,
    oi, oi_value, settle_price, waprice, num_trades, theor_price, change, qty}].
    """
    return moex.option_history(secid, frm, till)


# ─────────────────────────── Dividends (smart-lab.ru) ───────────────────────────
@_tool()
async def smartlab_dividends(ctx: Context, limit: int = 50) -> list[dict]:
    """Upcoming dividends calendar from smart-lab.ru.

    Returns [{name, ticker, period, dividend_rub, yield_pct, board_approved,
    last_buy_date, close_date, payment_date, price}].
    dividend_rub — ₽ per share; yield_pct — dividend yield %.
    """
    await ctx.report_progress(0, 2, "Fetching upcoming dividends")
    return smartlab.get_upcoming_dividends(limit)


@_tool()
async def smartlab_dividend_history(ticker: str, ctx: Context) -> list[dict]:
    """Dividend history by ticker from smart-lab.ru.

    Args: ticker — e.g. 'SBER', 'LKOH'.
    Returns [{name, ticker, period, dividend_rub, yield_pct, board_approved,
    last_buy_date, close_date, payment_date, price}].
    """
    await ctx.report_progress(0, 2, "Fetching dividend history")
    return smartlab.get_dividend_history(ticker)


# ─────────────────── Fundamental screener (smart-lab.ru) ───────────────────
@_tool()
async def smartlab_stock_screener(
    ctx: Context,
    period: str = "LTM",
    report_type: str = "-1",
    sector_id: int | None = None,
    capitalization_min: int | None = None,
    capitalization_max: int | None = None,
    volume_min: int | None = None,
    volume_max: int | None = None,
    company_type: str = "",
    is_state_owned: int = -1,
    is_exporter: int = -1,
    is_raw_stuff: int = -1,
    emitent: str | None = None,
    order_by: str = "market_cap",
    order_dir: str = "desc",
    limit: int = 15,
) -> list[dict]:
    """Fast fundamental stock screener for MOEX (all stocks in one request).

    Source: smart-lab.ru/q/shares_fundamental2/ (LTM data, 4h cache).
    Args: period ('LTM'/'2025'/...), report_type ('-1'=any/'MSFO'/'RSBU'),
    sector_id (int, smart-lab sector codes), capitalization_min/max (RUB),
    volume_min/max (RUB), company_type (''/'growth'/'value'),
    is_state_owned/exporter/raw_stuff (-1=all, 1=yes, 0=no),
    emitent (name substring), order_by (market_cap/p_e/div_yield/...),
    order_dir ('asc'/'desc'), limit (default 15).
    Returns [{ticker, name, market_cap, ev, revenue, net_income, div_yield, p_e, p_s, p_b, ev_ebitda, ...}].
    """
    await ctx.report_progress(0, 2, "Fetching stock screener from smart-lab.ru")
    return smartlab.get_stock_screener(
        period=period,
        report_type=report_type,
        sector_id=sector_id,
        capitalization_min=capitalization_min,
        capitalization_max=capitalization_max,
        volume_min=volume_min,
        volume_max=volume_max,
        company_type=company_type,
        is_state_owned=is_state_owned,
        is_exporter=is_exporter,
        is_raw_stuff=is_raw_stuff,
        emitent=emitent,
        order_by=order_by,
        order_dir=order_dir,
        limit=limit,
    )


@_tool()
async def smartlab_company_financials(
    ticker: str,
    ctx: Context,
    period: str = "y",
    standard: str = "MSFO",
    fields: list[str] | None = None,
) -> dict:
    """Detailed financial profile of a single company from smart-lab.ru.

    Source: smart-lab.ru/q/{ticker}/f/{period}/{standard}/ (4h cache).
    Returns multi-year financial statements with LTM:
    {ticker, name, years, data: {field: {label, values: {year: val, "LTM": val}}}}.

    Available fields by group — see ref://smartlab-financials-fields.
    Args: ticker, period ('y'=annual, 'q'=quarterly), standard (ref://standard-values),
          fields (list[str] | None, None=[' * ']=all).
    """
    await ctx.report_progress(0, 2, f"Fetching financials for {ticker}")
    return smartlab.get_company_financials(
        ticker, period=period, standard=standard, fields=fields,
    )


@_tool()
async def smartlab_company_financials_multi(
    tickers: list[str],
    ctx: Context,
    period: str = "y",
    standard: str = "MSFO",
    fields: list[str] | None = None,
) -> list[dict]:
    """Financial profiles for multiple companies (one request per ticker).

    Same as smartlab_company_financials but batched. Fields — ref://smartlab-financials-fields.
    Args: tickers (list[str]), period ('y'/'q'), standard (ref://standard-values),
          fields (list[str] | None, None=['*']=all).
    """
    n = len(tickers)
    await ctx.report_progress(0, n, f"Fetching financials for {n} companies")
    return smartlab.get_company_financials_multi(
        tickers, period=period, standard=standard, fields=fields,
    )


# ─────────────────── Fundamental analysis tools ───────────────────
@_tool()
async def stock_f_score(ticker: str, ctx: Context,
                        standard: str = "MSFO") -> dict:
    """Piotroski F-Score (0–9) for a stock.

    9 binary signals from financials (ROA>0, OCF>0, improving ROA/debt/margins, etc.).
    ≥7=strong, 4–6=moderate, ≤3=weak.
    Args: ticker, standard (ref://standard-values).
    Returns: {ticker, name, f_score, max, signals, details, years}.
    """
    await ctx.report_progress(0, 2, f"Calculating F-Score for {ticker}")
    return fundamental.f_score(ticker, standard=standard)


@_tool()
async def stock_z_score(ticker: str, ctx: Context,
                        standard: str = "MSFO") -> dict:
    """Altman Z-Score (modified for emerging markets) for a stock.

    Z' = 6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4 (X1–X4 from financials).
    Zones: Z' > 2.9 = safe, 1.23–2.9 = grey, < 1.23 = distress. Uses proxies.
    Args: ticker, standard (ref://standard-values).
    Returns: {ticker, name, z_score, zone, thresholds, components, missing, model}.
    """
    await ctx.report_progress(0, 2, f"Calculating Z-Score for {ticker}")
    return fundamental.z_score(ticker, standard=standard)


@_tool()
async def stock_peer_comparison(ticker: str, ctx: Context,
                                limit: int = 20) -> dict:
    """Compare stock multiples against top peers by market cap.

    Ranks the ticker on P/E, P/S, P/B, EV/EBITDA, EBITDA margin,
    Debt/EBITDA, dividend yield, payout ratio vs top-N stocks from
    smart-lab screener. Shows median peer value.

    Args: ticker — stock ticker; limit — number of peer companies (default 20).
    Returns: {ticker, name, market_cap, peer_count, rankings: {metric: {value, rank, of, median}}}.
    Rank 1 = best in group.
    """
    await ctx.report_progress(0, 2, f"Comparing {ticker} vs peers")
    return fundamental.peer_comparison(ticker, limit=limit)


@_tool()
async def dividend_analysis(ticker: str, ctx: Context) -> dict:
    """Dividend analysis: CAGR, average yield, payment consistency.

    Aggregate company's dividend history into yearly totals and compute:
    - Dividend CAGR (compound annual growth rate)
    - Average dividend yield
    - Consecutive years without dividend cut
    - Min/max yearly dividends
    - Yearly breakdown

    Args: ticker — stock ticker (e.g. 'SBER', 'LKOH').
    Returns: {ticker, total_payments, years, yearly_dividends, yearly_yields_pct,
              cagr, avg_yield_pct, consistent_years, min_dividend, max_dividend}.
    """
    await ctx.report_progress(0, 2, f"Analysing dividends for {ticker}")
    return fundamental.dividend_analysis(ticker)


@_tool()
async def stock_growth_analysis(ticker: str, ctx: Context,
                                standard: str = "MSFO") -> dict:
    """Multi-year growth analysis: revenue/EBITDA/net income CAGR, ROE/ROA/margin trends.

    Uses annual financial statements to compute:
    - Revenue CAGR over available years
    - Net income CAGR
    - EBITDA CAGR
    - Historical series for ROE, ROA, EBITDA margin, net margin

    Args: ticker — stock ticker; standard — ref://standard-values.
    Returns: {ticker, name, standard, period_years, cagr: {revenue, net_income, ebitda},
              series: {revenue, net_income, ebitda, roe, roa, ebitda_margin, net_margin}}.
    """
    await ctx.report_progress(0, 2, f"Analysing growth for {ticker}")
    return fundamental.growth_analysis(ticker, standard=standard)


@_tool()
async def bank_benchmark(tickers: list[str], ctx: Context,
                         standard: str = "MSFO") -> list[dict]:
    """Benchmark banks by key banking metrics.

    Compares NIM, CIR, NPL, CAR, LDR, CoR, bank margin, ROA, ROE
    across a list of bank tickers.

    Args: tickers — list of bank tickers (e.g. ['SBER', 'VTBR', 'TCSG']).
          standard — ref://standard-values.
    Returns: [{ticker, name, net_intertest_margin, cost_to_income,
               share_of_non_performing_loans, core_capital_adequacy_ratio,
               loan_to_deposit_ratio, cost_of_risk_ratio, bank_margin, ...}].
    """
    n = len(tickers)
    await ctx.report_progress(0, n, f"Benchmarking {n} banks")
    return fundamental.bank_benchmark(tickers, standard=standard)


@_tool()
async def bank_peer_comparison(ticker: str, ctx: Context) -> dict:
    """Rank a bank against the BANKI sector on key banking metrics.

    Fetches fundamentals for ~15 largest banks and ranks the ticker on:
    NIM, CIR, NPL, CAR, LDR, CoR, bank margin.
    Shows median value for context.

    Args: ticker — bank ticker (e.g. 'SBER', 'VTBR').
    Returns: {ticker, sector, peer_count, rankings: {metric: {value, rank, of, median, lower_is_better}}}.
    """
    await ctx.report_progress(0, 2, f"Ranking {ticker} vs bank sector")
    return fundamental.bank_peer_comparison(ticker)


@_tool()
async def company_fundamental_report(ticker: str, ctx: Context,
                                     standard: str = "MSFO") -> dict:
    """All-in-one company fundamental snapshot.

    Combines in one call:
    - Key financial metrics (P/E, P/S, P/B, EV/EBITDA, market cap, ROE, ROA, margins, debt)
    - Dividend summary (CAGR, avg yield, consistency, last 3 years)
    - Credit rating (Expert RA, if available)

    Args: ticker — stock ticker; standard — ref://standard-values.
    Returns: {ticker, metrics: {...}, dividends: {...}, credit_rating: [...], standard}.
    """
    await ctx.report_progress(0, 3, f"Building report for {ticker}")
    return fundamental.company_report(ticker, standard=standard)


# ─────────────────────────── Credit ratings (raexpert.ru) ───────────────────────────
@_tool()
async def raexpert_rating(query: str, ctx: Context) -> list[dict]:
    """Credit rating of issuer or bond from Expert RA.

    Source: raexpert.ru (updated several times a week, 4h cache).
    Returns [{name, rating, outlook, date, category, type, agency}].
    For bonds (type='emission') also {emitent}. Empty list if not found.

    Rating scale: ruAAA (max) → ruCCC, ruD (default), revoked.
    Outlook: Стабильный, Позитивный, Развивающийся.
    Note: ruBBB− and above = investment grade. ruBB+ and below = speculative.

    Args: query — issuer or bond name ('Сбербанк', 'ЛУКОЙЛ', 'ГТЛК', 'Атомэнергопром'). Case-insensitive.
    """
    await ctx.report_progress(0, 2, "Searching Expert RA ratings")
    return raexpert.rating_search(query)


@_tool()
async def raexpert_emitent_ratings(
    ctx: Context,
    rating_min: str | None = None,
    sector: str | None = None,
) -> list[dict]:
    """Bond issuers (emitents) filtered by credit rating and/or industry sector.

    Source: raexpert.ru (rating data, 4h cache) + MOEX sector indices (mapping).
    Returns only emitents (companies/banks/insurers), not individual bond emissions.

    Rating filter: keeps emitents with rating >= rating_min ('ruBBB-' = investment grade).
    Rating scale — ref://raexpert-ratings. Sector values — ref://moex-sectors.
    Args: rating_min, sector (both optional — without args returns all emitents).
    Returns [{name, rating, outlook, date, category, sector?, agency}], sorted by rating.
    """
    await ctx.report_progress(0, 2, "Fetching emitent ratings")
    return raexpert.emitent_rating_search(rating_min=rating_min, sector=sector)


# ─────────────────────────── ZPIF payments (vsezpif.ru) ───────────────────────────
@_tool()
async def zpif_payments(
    ctx: Context,
    fund_name: str | None = None,
    isin: str | None = None,
    limit: int = 50,
) -> dict:
    """Payment calendar for real-estate closed-end funds (ЗПИФ) from vsezpif.ru.

    Only free aggregator for 40+ RE funds. Estimated next payment based on 12-month calendar.

    Args (all optional):
      - fund_name — name fragment: 'Акцент', 'Парус', 'СФН', 'ВИМ';
      - isin — international ID;
      - limit — max records (default 50).

    Without args — all upcoming payments for 12 months.

    Returns:
      {payments: [{date, date_iso, fund_name, amount_per_unit}],
       next_payment: {date_iso, fund_name, amount},
       funds_total: int}.
    """
    await ctx.report_progress(0, 2, "Fetching ZPIF payment calendar")
    if fund_name or isin:
        payments = vsezpif.get_payments_by_fund(
            fund_name=fund_name,
            isin=isin,
            limit=limit,
        )
        next_pay = vsezpif.estimate_next_payment(
            fund_name=fund_name,
            isin=isin,
        )
    else:
        payments = vsezpif.get_payment_calendar(limit=limit)
        next_pay = payments[0] if payments else None

    all_funds = vsezpif.list_funds()

    return {
        "payments": payments,
        "next_payment": next_pay,
        "funds_total": len(all_funds),
    }


@_tool()
def zpif_funds_list() -> list[dict]:
    """List of ЗПИФ funds from vsezpif.ru.

    Returns [{slug, fund_name, url}]. Use slug in zpif_payments.
    """
    return vsezpif.list_funds()


# ─────────────────────────── CBR (Central Bank of Russia) ───────────────────────────
@_tool()
def cbr_key_rate(first_date: str | None = None, last_date: str | None = None,
                 tail: int = 30) -> dict:
    """CBR key rate — main driver for bonds and RUB.

    Args: first_date/last_date ('YYYY-MM-DD', optional).
    Returns: {latest, latest_date, series[]}.
    """
    return cbr.key_rate(first_date, last_date, tail)


@_tool()
def cbr_ruonia(first_date: str | None = None, last_date: str | None = None,
               tail: int = 30) -> dict:
    """RUONIA overnight (% annualized) — money market rate, market rate benchmark."""
    return cbr.ruonia(first_date, last_date, tail)


@_tool()
def cbr_ruonia_index(first_date: str | None = None, last_date: str | None = None,
                     tail: int = 12) -> dict:
    """RUONIA index + term averages (1m/3m/6m, % annualized) — short end of curve.

    Replacement for discontinued ROISfix. AVG_* — %; RUONIA_INDEX — index level.
    """
    return cbr.ruonia_index(first_date, last_date, tail)


@_tool()
def cbr_ibor(first_date: str | None = None, last_date: str | None = None,
             tail: int = 12) -> dict:
    """MIACR — actual weighted interbank rates (MosPrime/MIBOR discontinued)."""
    return cbr.ibor(first_date, last_date, tail)


@_tool()
def cbr_currency(symbol: str, first_date: str, last_date: str, tail: int = 30) -> dict:
    """CBR FX rate vs RUB. symbol: 'USD','EUR','CNY'. Dates 'YYYY-MM-DD'."""
    return cbr.currency(symbol, first_date, last_date, tail)


@_tool()
def cbr_metals(first_date: str | None = None, last_date: str | None = None,
               tail: int = 12) -> dict:
    """CBR precious metals prices (gold/silver/platinum/palladium)."""
    return cbr.metals(first_date, last_date, tail)


@_tool()
def cbr_reserves(first_date: str | None = None, last_date: str | None = None,
                 tail: int = 12) -> dict:
    """Russia international reserves (gold + FX)."""
    return cbr.reserves(first_date, last_date, tail)


@_tool()
def cbr_inflation(first_date: str | None = None, last_date: str | None = None,
                  tail: int = 24) -> dict:
    """CPI inflation (YoY %) and CBR key rate (monthly, from 2013).

    Source: cbr.ru/hd_base/infl/. Cache: 4h.
    Dates: 'YYYY-MM' or 'YYYY-MM-DD'.
    Returns: {latest_inflation, latest_key_rate, latest_inflation_target, latest_date,
    series: [{date, key_rate, inflation_yoy, inflation_target}, ...]}.
    inflation_yoy — Rosstat CPI (% YoY). inflation_target — CBR target (%).
    For real yield calculations.
    """
    return cbr.inflation(first_date, last_date, tail)


# ─────────────────────────── Bond math ───────────────────────────
@_tool()
async def bond_report(query: str, ctx: Context) -> dict:
    """Deep bond analysis: metrics + rate scenarios + spread to curve + convexity.

    Args: query — OFZ number/ISIN.
    Returns: {bond, years_to_maturity, convexity, accrued_interest, gry,
    scenarios (±bp shift + breakeven), twist_scenarios (steepening/flattening),
    spread_to_curve (vs G-curve at duration), real_return (vs CPI)}.
    """
    await ctx.report_progress(0, 5, "Fetching bond data")
    b = moex.bond(query)
    rep: dict = {"bond": b}
    freq = _freq_from_coupon_period(b.get("coupon_period_days"))

    await ctx.report_progress(1, 5, "Fetching coupon schedule")
    try:
        coupon_schedule = moex.future_bond_coupons(query)
    except Exception:  # noqa: BLE001
        coupon_schedule = []
    if coupon_schedule:
        rep["coupon_schedule"] = coupon_schedule

    await ctx.report_progress(2, 5, "Fetching inflation data")
    try:
        infl_data = cbr.inflation(tail=1)
        actual_inflation = infl_data.get("latest_inflation")
    except Exception:  # noqa: BLE001
        actual_inflation = None

    if b.get("maturity"):
        rep["years_to_maturity"] = bonds.years_to_maturity(b["maturity"])

    if b.get("maturity") and b.get("coupon_pct") is not None:
        ai = bonds.accrued_interest(date.today(), b["maturity"],
                                     b["coupon_pct"], b.get("face_value") or 1000,
                                     freq)
        rep["accrued_interest"] = ai

    ytm = b.get("ytm")
    mat = b.get("maturity")
    c_pct = b.get("coupon_pct") or 0
    cs = coupon_schedule or None

    if mat and ytm and b.get("price_pct"):
        gry_result = bonds.gry(
            date.today(), mat, c_pct, b["price_pct"],
            b.get("face_value") or 1000, freq, coupon_schedule=cs)
        rep["gry"] = gry_result
        if cs and gry_result.get("gry_pct"):
            ytm = gry_result["gry_pct"]

    await ctx.report_progress(3, 5, "Computing scenarios")
    if mat and ytm:
        rep["convexity"] = bonds.convexity(
            date.today(), mat, c_pct, ytm, b.get("face_value") or 1000, freq,
            coupon_schedule=cs)
        rep["scenarios"] = bonds.rate_scenarios(
            mat, c_pct, ytm, today=str(date.today()), freq=freq,
            coupon_schedule=cs)
        rep["real_return"] = bonds.real_return(ytm, actual_inflation=actual_inflation)

    dur = b.get("duration_years")
    if mat and ytm:
        dur = bonds.macaulay_duration(
            date.today(), mat, c_pct, ytm, b.get("face_value") or 1000, freq,
            coupon_schedule=cs)
        rep["macaulay_duration_years"] = dur
        rep["modified_duration_years"] = round(dur / (1 + ytm / 100 / freq), 2)
    if mat and ytm and dur:
        rep["twist_scenarios"] = bonds.twist_scenarios(
            mat, c_pct, ytm, dur, today=str(date.today()), freq=freq,
            coupon_schedule=cs)

    await ctx.report_progress(4, 5, "Computing spread to curve")
    if dur and ytm:
        try:
            cy = rate.curve_yield(dur)
            rep["spread_to_curve"] = bonds.spread_to_curve(
                ytm, dur, cy.get("yield", 0))
        except Exception:  # noqa: BLE001
            pass

    return rep


@_tool()
def bond_accrued_interest(query: str) -> dict:
    """Accrued coupon interest (НКД) for a bond.

    Args: query — OFZ number/ISIN.
    Returns: {accrued_rub, accrued_pct, days_accrued, coupon_period_days, last_coupon, next_coupon}.
    """
    b = moex.bond(query)
    if not b.get("maturity") or b.get("coupon_pct") is None:
        return {"error": "insufficient bond data"}
    freq = _freq_from_coupon_period(b.get("coupon_period_days"))
    return bonds.accrued_interest(
        date.today(), b["maturity"], b["coupon_pct"], b.get("face_value") or 1000, freq)


@_tool()
async def bond_synthetic_yield(query: str, horizon_years: float, ctx: Context,
                         reinvest_rate: float | None = None) -> dict:
    """Synthetic yield with coupon reinvestment over investment horizon.

    Calculates IRR of full cash flow: buy at dirty price, coupons reinvested at
    reinvest_rate (default: YTM), sell at assumed YTM at horizon (or face at maturity).
    Args: query (ISIN/OFZ), horizon_years, reinvest_rate (% p.a., optional).
    Returns: {irr_pct, ytm_pct, total_return_pct, annualized_return_pct, note}.
    """
    await ctx.report_progress(0, 3, "Fetching bond data")
    b = moex.bond(query)
    if not b.get("maturity") or b.get("coupon_pct") is None or not b.get("ytm"):
        return {"error": "insufficient bond data (need maturity, coupon, ytm)"}
    freq = _freq_from_coupon_period(b.get("coupon_period_days"))
    await ctx.report_progress(1, 3, "Fetching coupon schedule")
    try:
        cs = moex.future_bond_coupons(query)
    except Exception:  # noqa: BLE001
        cs = []
    result = bonds.synthetic_yield(
        date.today(), b["maturity"], b["coupon_pct"], b["ytm"],
        horizon_years, reinvest_rate=reinvest_rate,
        face=b.get("face_value") or 1000, freq=freq,
        coupon_schedule=cs or None)
    result["query"] = query
    result["secid"] = b.get("secid")
    result["shortname"] = b.get("shortname")
    return result


@_tool()
async def bond_screener(
    ctx: Context,
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
    """Bond screener: filter MOEX bonds by multiple criteria simultaneously.

    Full param reference — see ref://bond-screener-params (types, ranges, enums, sort fields).
    Key filters: ytm/coupon/price/duration/years_to_maturity bounds, maturity range, coupon_type,
    currency, issue_volume, accrued_int, rating_min (ref://raexpert-ratings),
    sector (ref://moex-sectors), emitent, include_qualified, sort_by, limit.
    Returns: {count_shown, count_total_matching, count_all_bonds,
    bonds: [{secid, shortname, isin, board, emitent, price_pct, ytm, coupon_pct,
    duration_years, maturity, offer_date, bond_type, accrued_int, rating, sector, ...}]}.
    """
    await ctx.report_progress(0, 3, "Fetching all bonds from MOEX boards")
    result = moex.bond_screener(
        ytm_min=ytm_min, ytm_max=ytm_max,
        coupon_min=coupon_min, coupon_max=coupon_max,
        price_min=price_min, price_max=price_max,
        maturity_from=maturity_from, maturity_to=maturity_to,
        duration_min=duration_min, duration_max=duration_max,
        years_to_maturity_min=years_to_maturity_min, years_to_maturity_max=years_to_maturity_max,
        has_offer=has_offer, has_amortization=has_amortization,
        coupon_type=coupon_type,
        coupon_freq_min=coupon_freq_min, coupon_freq_max=coupon_freq_max,
        currency=currency,
        issue_volume_min=issue_volume_min, issue_volume_max=issue_volume_max,
        accrued_int_min=accrued_int_min, accrued_int_max=accrued_int_max,
        rating_min=rating_min, sector=sector, emitent=emitent,
        include_qualified=include_qualified, qualified_only=qualified_only,
        sort_by=sort_by, sort_desc=sort_desc, limit=limit,
    )
    await ctx.report_progress(3, 3, "Done")
    return result


@_tool()
async def bond_prescreener(
    ctx: Context,
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
    """Bond pre-screener: compact output with only secname + isin per bond.

    Same filter parameters as bond_screener, but returns only secname and ISIN
    for each bond. Optimized for checking bond availability without context overflow.
    Returns: {count_total_matching, count_all_bonds,
    bonds: [{secname, isin}, ...]}.
    """
    await ctx.report_progress(0, 3, "Fetching all bonds from MOEX boards")
    result = moex.bond_prescreener(
        ytm_min=ytm_min, ytm_max=ytm_max,
        coupon_min=coupon_min, coupon_max=coupon_max,
        price_min=price_min, price_max=price_max,
        maturity_from=maturity_from, maturity_to=maturity_to,
        duration_min=duration_min, duration_max=duration_max,
        years_to_maturity_min=years_to_maturity_min, years_to_maturity_max=years_to_maturity_max,
        has_offer=has_offer, has_amortization=has_amortization,
        coupon_type=coupon_type,
        coupon_freq_min=coupon_freq_min, coupon_freq_max=coupon_freq_max,
        currency=currency,
        issue_volume_min=issue_volume_min, issue_volume_max=issue_volume_max,
        accrued_int_min=accrued_int_min, accrued_int_max=accrued_int_max,
        rating_min=rating_min, sector=sector, emitent=emitent,
        include_qualified=include_qualified, qualified_only=qualified_only,
        sort_by=sort_by, sort_desc=sort_desc, limit=limit,
    )
    await ctx.report_progress(3, 3, "Done")
    return result


@_tool()
async def price_volatility(query: str, ctx: Context, days: int = 90, rf_annual: float = 16.0) -> dict:
    """Volatility, Sharpe ratio, max drawdown from daily candles.

    Args: query (ticker), days (default 90), rf_annual (risk-free rate, % annualized).
    Returns: {annual_vol_pct, daily_vol_pct, sharpe, max_drawdown_pct,
    total_return_pct, high_price, low_price, trading_days, ...}.
    sharpe = (mean_excess_return / volatility) * sqrt(252).
    """
    await ctx.report_progress(0, 2, "Fetching candle data")
    return moex.price_volatility(query, days, rf_annual)


@_tool()
async def liquidity_assessment(query: str, ctx: Context, days: int = 90) -> dict:
    """Liquidity assessment: Amihud illiquidity, spread, turnover, score 0-10.

    Args: query (ticker/ISIN), days (default 90).
    Returns: {secid, avg_daily_turnover_rub, avg_daily_volume_lots, amihud_bps_per_mln,
    spread (% and RUB), spread_sources (bid/ask + OHLC estimate), composite_score (0-10),
    grade (A-E), trading_day_ratio, zero_volume_days, ...}.
    A (≥8) = very liquid, E (<2) = minimal. amihud_bps_per_mln = mean(|r_t|/V_t) × 10^10.
    Spread: Corwin-Schultz from OHLC + actual bid/ask.
    """
    await ctx.report_progress(0, 2, "Fetching market data")
    return moex.liquidity(query, days)


@_tool()
async def technical_indicators(query: str, ctx: Context, days: int = 90) -> dict:
    """Full technical analysis suite for any MOEX instrument (stocks, ETF, bonds).

    Indicators — see ref://technical-indicators. Computes from daily OHLCV candles.
    Args: query — ticker or ISIN; days — lookback (default 90, 200+ for Ichimoku/SMA200).
    Returns: all indicators as a flat dict; keys present only when enough data.
    """
    await ctx.report_progress(0, 2, "Fetching candle data + computing indicators")
    return moex.technical_indicators(query, days)


# ─────────────────────────── ETF / БПИФ ───────────────────────────
@_tool()
async def etf_fund_info(query: str, ctx: Context) -> dict:
    """ETF/БПИФ information: iNAV, benchmark, category, premium to NAV.

    Args: query — ticker ('TMOS', 'SBMX', 'TGLD', 'TMON').
    Returns: {secid, shortname, isin, group, type, issuedate, emitent_id,
    category, category_ru, benchmark, benchmark_name,
    inav_ticker, inav_price, fund_price, premium_discount_pct, ...}.
    premium_discount_pct > 0 = premium (market > NAV), < 0 = discount.
    iNAV — indicative intraday NAV from companion instrument on INAV board.
    """
    await ctx.report_progress(0, 2, "Fetching fund data")
    return moex.etf_fund_data(query)


@_tool()
async def etf_premium_discount(query: str, ctx: Context) -> dict:
    """ETF/БПИФ premium or discount to iNAV (indicative NAV).

    Args: query — ticker ('TMOS', 'SBMX', 'TGLD').
    Returns: {secid, fund_price, inav_ticker, inav_price, premium_discount_pct, ...}.
    Positive = premium (market price > NAV), negative = discount.
    iNAV comes from companion instrument (TMOS→TMOSA) on INAV board.
    """
    await ctx.report_progress(0, 1, "Fetching iNAV")
    return moex.etf_premium_discount(query)


@_tool()
async def etf_tracking_error(query: str, ctx: Context, days: int = 90) -> dict:
    """ETF/БПИФ tracking error vs benchmark index over N days.

    Args: query — fund ticker ('TMOS', 'SBMX', 'TGLD', 'TPAY');
          days — lookback period (default 90).
    Returns: {secid, benchmark, benchmark_name, fund_return_pct,
    benchmark_return_pct, excess_return_pct, tracking_error_ann_pct, dates, ...}.
    tracking_error_ann_pct — annualized σ(r_fund − r_bench), %.
    excess_return_pct — fund return minus benchmark return over period.
    Benchmark is auto-detected from ETF_BENCHMARK_MAP.
    """
    await ctx.report_progress(0, 2, "Fetching fund + benchmark data")
    return moex.etf_tracking_error(query, days)


@_tool()
async def etf_screener(
    ctx: Context,
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
    """ETF/БПИФ screener: filter MOEX funds by multiple criteria.

    Factors & TA indicators — see ref://etf-screener-params (dropdowns, enums, sorting).
    TA suite — see ref://technical-indicators.

    Key params: category, emitent, benchmark, currency, price/volume/volatility bounds,
    sharpe/beta/performance bounds, RSI/ADX/MACD/Stochastic/CCI/Williams/Ichimoku/PSAR/CMF/ROC filters,
    premium_discount_max, tracking_error_max, sort_by, sort_desc, limit.
    Returns: funds list with full TA, liquidity scores, premium/discount, tracking error.
    """
    await ctx.report_progress(0, 4, "Loading all ETF/БПИФ from MOEX boards")
    return moex.etf_screener(
        category=category,
        emitent=emitent,
        benchmark=benchmark,
        currency=currency,
        price_min=price_min,
        price_max=price_max,
        volume_min=volume_min,
        volume_max=volume_max,
        spread_max=spread_max,
        volatility_min=volatility_min,
        volatility_max=volatility_max,
        sharpe_min=sharpe_min,
        sharpe_max=sharpe_max,
        beta_min=beta_min,
        beta_max=beta_max,
        performance_min=performance_min,
        performance_max=performance_max,
        performance_period=performance_period,
        rsi_min=rsi_min,
        rsi_max=rsi_max,
        ma_signal=ma_signal,
        adx_min=adx_min,
        macd_signal=macd_signal,
        stochastic_min=stochastic_min,
        stochastic_max=stochastic_max,
        cci_min=cci_min,
        cci_max=cci_max,
        williams_min=williams_min,
        williams_max=williams_max,
        ichimoku_signal=ichimoku_signal,
        psar_direction=psar_direction,
        cmf_signal=cmf_signal,
        roc_min=roc_min,
        roc_max=roc_max,
        premium_discount_max=premium_discount_max,
        tracking_error_max=tracking_error_max,
        include_indicators=include_indicators,
        sort_by=sort_by,
        sort_desc=sort_desc,
        limit=limit,
    )


# ─────────────────────────── Rate expectations (OFZ G-curve) ───────────────────────────
@_tool()
async def rate_expectations(ctx: Context, key_rate: float | None = None) -> dict:
    """Market rate expectations from OFZ G-curve. Numbers only.

    Args: key_rate (optional, defaults to cbr_key_rate).
    Returns: {as_of, key_rate, ruonia, curve[], signals, note}.
    signals: slope (slope_10y_1y/2y_3m), gross spreads to key rate (include term premium!),
    anchor short_vs_ruonia_1y, forward ladder (fwd_1y_in_1y/2y, fwd_3m_in_1y),
    inverted, machine label read (cuts_priced|hikes_priced|flat).
    Portfolio interpretation is done by the client/agent.
    """
    await ctx.report_progress(0, 3, "Fetching G-curve data")
    return rate.rate_expectations(key_rate)


@_tool()
def curve_yield(years: float) -> dict:
    """G-curve OFZ yield at arbitrary maturity (% annualized) — for duration mapping.

    Args: years (e.g. 5.9 for OFZ 26253 duration). Linear interpolation on ZCYC nodes.
    """
    return rate.curve_yield(years)


# ─────────────────────────── Portfolio (domain reports) ───────────────────────────
@_tool()
async def portfolio_snapshot(assets: str, ctx: Context) -> dict:
    """Portfolio snapshot: value, P&L, allocation, rate risk, income, dividend yield, real return.

    Args: assets — portfolio in markdown format (see TOOLS.md):
    '- Name (TICKER/ISIN): N pcs. (purchase_price ...)'.
    Bonds: price in % of face; shares/funds: in RUB.
    income_risk = running_yield − Rosstat CPI (fallback: key rate).
    Returns: {total_value, total_pnl_pct, positions, allocation,
    income_risk, ...}.
    """
    await ctx.report_progress(0, 3, "Fetching inflation data")
    try:
        infl_data = cbr.inflation(tail=1)
        inflation_pct = infl_data.get("latest_inflation")
    except Exception:  # noqa: BLE001
        inflation_pct = None
    await ctx.report_progress(1, 3, "Building portfolio snapshot")
    return portfolio.snapshot(assets, inflation_pct=inflation_pct)


@_tool()
async def portfolio_rate_whatif(delta_pp: float, assets: str, ctx: Context) -> dict:
    """Portfolio impact of delta_pp percentage point shift in bond yields.

    Args: delta_pp (e.g. -1, +2); assets — markdown portfolio (same as portfolio_snapshot).
    Returns: value change (RUB and %) + bond-by-bond breakdown.
    """
    await ctx.report_progress(0, 2, "Computing rate scenario")
    return portfolio.rate_whatif(delta_pp, assets)


@_tool()
async def portfolio_income_calendar(assets: str, ctx: Context) -> dict:
    """Upcoming income: next coupon per bond + declared dividends.

    Args: assets — markdown portfolio (same as portfolio_snapshot).
    """
    await ctx.report_progress(0, 2, "Fetching income calendar")
    return portfolio.income_calendar(assets)


@_tool()
def portfolio_movers(assets: str) -> dict:
    """Top gainers/losers: daily change and P&L vs purchase price (top-3 each way).

    Args: assets — markdown portfolio (same as portfolio_snapshot).
    """
    return portfolio.movers(assets)


# ─────────────────────────── Resources (reference data) ───────────────────────────
@mcp.resource(
    "ref://raexpert-ratings",
    name="raexpert_rating_scale",
    description="Full credit rating scale of Expert RA (Эксперт РА): 20 grades from ruAAA (best) to ruD (default). "
                "Investment grade: ruBBB− and above. Speculative: ruBB+ and below. "
                "Use in raexpert_emitent_ratings rating_min parameter.",
    mime_type="application/json",
)
def ref_raexpert_ratings() -> dict:
    return {
        "agency": "Эксперт РА",
        "scale": [
            {"rating": k, "order": v}
            for k, v in sorted(raexpert._RATING_ORDER.items(), key=lambda x: -x[1])
        ],
        "investment_grade_threshold": "ruBBB-",
        "special": ["отозван (revoked)", "SB (standard bonds)"],
    }


@mcp.resource(
    "ref://moex-sectors",
    name="moex_sectors",
    description="10 MOEX industry sectors for bond issuer filtering (sector parameter in "
                "raexpert_emitent_ratings). Examples: Финансовый, Нефтегазовый, Металлургия и добыча.",
    mime_type="application/json",
)
def ref_moex_sectors() -> dict:
    return {
        "sectors": raexpert.SECTORS,
        "count": len(raexpert.SECTORS),
    }


@mcp.resource(
    "ref://moex-sector-tickers",
    name="moex_sector_tickers",
    description="MOEX sector index composition: sector name → list of ticker symbols. "
                "~100 issuers, updated ~2 times per year on index rebalancing.",
    mime_type="application/json",
)
def ref_moex_sector_tickers() -> dict:
    return {
        "sectors": raexpert._MOEX_SECTOR_TICKERS,
        "total_tickers": len(raexpert._MOEX_TICKER_TO_SECTOR),
    }


@mcp.resource(
    "ref://raexpert-categories",
    name="raexpert_categories",
    description="10 Expert RA rating categories: slug (used in raexpert.ru URLs) → Russian name. "
                "Examples: bankcredit_all → Кредитные организации, credits_all → Нефинансовые компании.",
    mime_type="application/json",
)
def ref_raexpert_categories() -> dict:
    return raexpert._CATEGORIES


@mcp.resource(
    "ref://futures-underlying-assets",
    name="futures_underlying_assets",
    description="36 FORTS futures underlying asset codes. FX: Si→USD, Eu→EUR, CNY, CHF, GBP, JPY, HKD, TRY, KZT, BYN. "
                "Indices: RTS, MREI, MXI, RVI, IMOEX. Commodities: BR, GOLD, GL, SV, SLVR, PL, CU, NI. "
                "Equity: SBRF→SBER, GAZR→GAZP, LKOH, GMKN, MGNT, ROSN, SIBN, VTBR, TATN, ALRS, FEES, MTSI→MTSS, NlNK→NKNC. "
                "Use in moex_futures_list, moex_futures_basis, moex_futures_open_interest.",
    mime_type="application/json",
)
def ref_futures_underlying_assets() -> dict:
    return {
        asset: {k: v for k, v in spec.items()}
        for asset, spec in moex._UNDERLYING_MAP.items()
    }


@mcp.resource(
    "ref://cbr-currencies",
    name="cbr_currencies",
    description="10 CBR FX rate codes available in cbr_currency (ISO 4217 → pair vs RUB). "
                "USD, EUR, CNY, GBP, CHF, JPY(×100), HKD, TRY, KZT, BYN.",
    mime_type="application/json",
)
def ref_cbr_currencies() -> dict:
    return {
        "currencies": [
            {"code": "USD", "pair": "USD/RUB"},
            {"code": "EUR", "pair": "EUR/RUB"},
            {"code": "CNY", "pair": "CNY/RUB"},
            {"code": "GBP", "pair": "GBP/RUB"},
            {"code": "CHF", "pair": "CHF/RUB"},
            {"code": "JPY", "pair": "JPY(100)/RUB", "note": "per 100 JPY"},
            {"code": "HKD", "pair": "HKD/RUB"},
            {"code": "TRY", "pair": "TRY/RUB"},
            {"code": "KZT", "pair": "KZT/RUB"},
            {"code": "BYN", "pair": "BYN/RUB"},
        ],
    }


@mcp.resource(
    "ref://candle-intervals",
    name="candle_intervals",
    description="All valid interval codes for moex_candles. "
                "Empty string = auto-select (≤50 candles).",
    mime_type="application/json",
)
def ref_candle_intervals() -> dict:
    return {
        "intervals": [
            {"code": "",   "label": "auto (≤50 candles)"},
            {"code": "1",  "label": "1 minute",  "minutes": 1},
            {"code": "10", "label": "10 minutes", "minutes": 10},
            {"code": "60", "label": "1 hour",     "minutes": 60},
            {"code": "24", "label": "1 day",      "minutes": 1440},
            {"code": "7",  "label": "1 week",     "minutes": 10080},
            {"code": "31", "label": "1 month",    "minutes": 44640},
            {"code": "4",  "label": "1 quarter",  "minutes": 133920},
        ],
    }


@mcp.resource(
    "ref://etf-benchmarks",
    name="etf_benchmarks",
    description="Mapping БПИФ ticker → benchmark index + category. "
                "Use in etf_tracking_error. Categories: equity_russia, equity_foreign, "
                "equity_sector, bond_gov, bond_corp, money_market, commodity, fx, mixed. "
                "Covers ~30 major Russian ETFs/БПИФ on MOEX.",
    mime_type="application/json",
)
def ref_etf_benchmarks() -> dict:
    return {
        "mapping": {
            t: {**v, "category_ru": moex.ETF_CATEGORY_RU.get(v.get("category", ""), v.get("category", ""))}
            for t, v in moex.ETF_BENCHMARK_MAP.items()
        },
        "categories": moex.ETF_CATEGORY_RU,
    }


@mcp.resource(
    "ref://moex-sec-types",
    name="moex_sec_types",
    description="Valid sec_type filter values for moex_search: "
                "bond, share, stock, fund, etf, index.",
    mime_type="application/json",
)
def ref_moex_sec_types() -> dict:
    return {
        "sec_types": [
            {"code": "bond",   "description": "Bonds (OFZ, corporate, municipal)",   "group": "stock_bonds"},
            {"code": "share",  "description": "Equities (incl. depositary receipts)", "group": "stock_shares"},
            {"code": "stock",  "description": "Synonym for share",                    "group": "stock_shares"},
            {"code": "fund",   "description": "Mutual funds / PIFs (ПИФы)",           "group": "stock_ppif"},
            {"code": "etf",    "description": "ETF / БПИФ",                           "group": "stock_etf"},
            {"code": "index",  "description": "MOEX indices",                         "group": "stock_index"},
        ],
    }


# ── Additional reference resources (symlinked from tools) ──

@mcp.resource(
    "ref://technical-indicators",
    name="technical_indicators_suite",
    description="Full TA indicator suite computed by technical_indicators and etf_screener. "
                "Trend: RSI(14), Stochastic %K/%D(14,3,3), ADX(14)+DI, MACD(12,26,9), ATR(14), "
                "Ichimoku(9,26,52), Parabolic SAR, EMA(12/26), SMA(50/200), MA golden/death cross, "
                "Momentum(10), ROC(10). Volatility: Bollinger Bands(20,2). "
                "Volume: OBV + trend, CMF(20), VWAP. "
                "Support/Resistance: Pivot Points (classic), Fibonacci retracements.",
    mime_type="application/json",
)
def ref_technical_indicators() -> dict:
    return {
        "indicators": {
            "trend": ["RSI(14)", "Stochastic_%K/%D(14,3,3)", "ADX(14)+DI", "MACD(12,26,9)",
                      "ATR(14)", "Ichimoku(9,26,52)", "Parabolic_SAR",
                      "EMA(12)", "EMA(26)", "SMA(50)", "SMA(200)", "MA_cross",
                      "Momentum(10)", "ROC(10)"],
            "volatility": ["Bollinger_Bands(20,2)", "ATR(14)"],
            "volume": ["OBV", "CMF(20)", "VWAP"],
            "support_resistance": ["Pivot_Points(classic)", "Fibonacci_retracements"],
        },
        "minimum_bars_required": {"Ichimoku": 52, "SMA_200": 200, "SMA_50": 50,
                                  "MACD": 26, "default": 14},
    }


@mcp.resource(
    "ref://standard-values",
    name="accounting_standards",
    description="Valid standard parameter values for financial tools (stock_f_score, stock_z_score, "
                "stock_growth_analysis, bank_benchmark, company_fundamental_report, "
                "smartlab_company_financials, smartlab_company_financials_multi).",
    mime_type="application/json",
)
def ref_standard_values() -> dict:
    return {
        "standards": [
            {"code": "MSFO", "label": "IFRS (МСФО)", "note": "default"},
            {"code": "RSBU", "label": "RAS (РСБУ)", "note": "Russian accounting standards"},
        ],
    }


@mcp.resource(
    "ref://bond-screener-params",
    name="bond_screener_params",
    description="Full parameter reference for bond_screener tool: types, ranges, enums, sort fields. "
                "Includes coupon_type values, currency codes, sort_by options, qualified-bond flags.",
    mime_type="application/json",
)
def ref_bond_screener_params() -> dict:
    return {
        "filters": {
            "ytm_min/ytm_max": "float [%], inclusive",
            "coupon_min/coupon_max": "float [%], inclusive",
            "price_min/price_max": "float [% of face], inclusive",
            "maturity_from/maturity_to": "str YYYY-MM-DD, inclusive",
            "duration_min/duration_max": "float [years], Macaulay",
            "years_to_maturity_min/years_to_maturity_max": "float [years], time to maturity",
            "has_offer": "bool | None (True=with offer, False=bullet)",
            "has_amortization": "bool | None (True=amortizing, False=bullet)",
            "coupon_type": "enum: fixed, float, amortization",
            "coupon_freq_min/coupon_freq_max": "int: 1,2,4,6,12 per year",
            "currency": "enum: SUR, USD, EUR, CNY",
            "issue_volume_min/issue_volume_max": "int [number of bonds]",
            "accrued_int_min/accrued_int_max": "float [RUB/bond]",
            "rating_min": "str — minimum Expert RA rating (ref://raexpert-ratings)",
            "sector": "str — MOEX sector name (ref://moex-sectors)",
            "emitent": "str — substring match (case-insensitive)",
            "include_qualified": "bool (adds is_qualified field, per-bond ISS call)",
            "qualified_only": "bool | None (requires include_qualified=True)",
            "sort_by": "enum: ytm, duration, maturity, price, coupon, issue_volume (default: ytm)",
            "sort_desc": "bool (default True)",
            "limit": "int 1..500 (default 15)",
        },
    }


@mcp.resource(
    "ref://etf-screener-params",
    name="etf_screener_params",
    description="Full parameter reference for etf_screener tool: categories, emitents, "
                "TA signal enums, performance periods, sort fields.",
    mime_type="application/json",
)
def ref_etf_screener_params() -> dict:
    return {
        "filters": {
            "category": "enum: equity_russia, equity_foreign, equity_sector, equity_dividend, "
                        "bond_gov, bond_corp, money_market, commodity, fx, mixed",
            "emitent": "enum: Т-Капитал, Сбер, Альфа, ВТБ (exact match)",
            "benchmark": "str — MOEX index ticker (IMOEX, GOLD, RGBITR, ...)",
            "currency": "enum: SUR, USD, EUR, CNY, HKD",
            "performance_period": "enum: 1m, 3m, 6m, 1y, ytd (default: 1y)",
            "ma_signal": "enum: golden_cross (MA50>MA200), death_cross (MA50<MA200)",
            "macd_signal": "enum: bullish, bearish",
            "ichimoku_signal": "enum: bullish, bearish, in_cloud",
            "psar_direction": "enum: long, short",
            "cmf_signal": "enum: buying_pressure, selling_pressure, neutral",
            "rsi_min/rsi_max": "float (0..100)",
            "adx_min": "float (trend strength threshold)",
            "stochastic_min/stochastic_max": "float (Stochastic %K, 0..100)",
            "cci_min/cci_max": "float (CCI(20))",
            "williams_min/williams_max": "float (Williams %R, −100..0)",
            "roc_min/roc_max": "float (Rate of Change, %)",
            "premium_discount_max": "float [%] max premium/discount to NAV",
            "tracking_error_max": "float [%] max annualized tracking error",
            "sort_by": "enum: performance, volatility, sharpe, volume, spread, "
                       "premium, rsi, adx, beta, stochastic, cci, williams, roc, cmf, momentum",
        },
    }


@mcp.resource(
    "ref://smartlab-financials-fields",
    name="smartlab_financials_fields",
    description="All field names accepted by smartlab_company_financials fields parameter, "
                "grouped by statement category. Use in smartlab_company_financials and "
                "smartlab_company_financials_multi.",
    mime_type="application/json",
)
def ref_smartlab_financials_fields() -> dict:
    return {
        "groups": {
            "valuation": ["p_e", "p_s", "p_b", "p_bv", "p_fcf", "ev_ebitda", "ev",
                          "market_cap", "eps", "bv_share", "fcf_share", "free_float", "fcf_yield"],
            "income": ["revenue", "ebitda", "operating_income", "net_income", "net_income_ns",
                       "cost_of_production", "opex", "amortization", "employment_expenses",
                       "interest_expenses"],
            "cash_flow": ["ocf", "fcf", "capex", "capex_revenue"],
            "balance": ["assets", "net_assets", "book_value", "debt", "net_debt", "cash",
                        "goodwill", "intangible_assets", "investment_portfolio"],
            "profitability": ["roe", "roa", "ebitda_margin", "net_margin"],
            "leverage": ["debt_ebitda"],
            "dividends": ["dividend", "dividend_pr", "div_yield", "div_yield_priv",
                          "dividend_payout", "div_payout_ratio"],
            "share_info": ["common_share", "priv_share", "number_of_shares", "number_of_priv_shares"],
            "banking": ["net_operating_income", "net_interest_income", "commission_income",
                        "bank_assets", "capital", "loan_portfolio", "deposits",
                        "core_capital_adequacy_ratio", "total_capital_adequacy_ratio",
                        "cost_of_risk_ratio", "cost_to_income", "loan_to_deposit_ratio",
                        "share_of_non_performing_loans"],
        },
    }


# ─────────────────────────── Prompts (workflow templates) ───────────────────────────

@mcp.prompt()
def analyze_bond_portfolio(assets: str) -> str:
    """Full bond portfolio analysis workflow."""
    return (
        "You are analysing a bond portfolio. Follow these steps:\n"
        "1. Call current_datetime() to get the reference date.\n"
        "2. Call portfolio_snapshot(assets) — review allocation, duration, P&L, "
        "spread_to_curve, running_yield, income_risk.\n"
        "3. For each bond with |spread_to_curve_pp| > 0.5 or |income_risk| < running_yield: "
        "call bond_report(secid) to get convexity, scenarios, twist analysis.\n"
        "4. Call rate_expectations() — check key_rate, fwd_* forward ladder, read label. "
        "Interpret portfolio duration vs rate path.\n"
        "5. Call portfolio_rate_whatif(delta_pp=-1.0, assets) and portfolio_rate_whatif(delta_pp=+1.0, assets) "
        "for parallel shift scenarios.\n"
        "6. Summarise: allocation, rate risk (duration × rate path), credit risk (spreads), "
        "real return (vs CPI), actionable recommendations.\n\n"
        f"Portfolio:\n{assets}"
    )


@mcp.prompt()
def screen_undervalued_stocks(sector: str | None = None, limit: int = 10) -> str:
    """Stock screening workflow for undervalued companies."""
    sector_filter = f" with sector_id={sector} filter" if sector else ""
    return (
        f"You are screening for undervalued MOEX stocks{sector_filter}.\n"
        "1. Call smartlab_stock_screener(order_by='p_e', order_dir='asc', limit={limit}) "
        "to find low P/E stocks.\n"
        "2. For top candidates, call smartlab_company_financials(ticker, standard='MSFO') "
        "— check debt_ebitda < 3, roe > 15%, revenue CAGR looking at yearly growth.\n"
        "3. Call stock_f_score(ticker) — require f_score >= 5 for further analysis.\n"
        "4. Call stock_z_score(ticker) — exclude Z' < 1.23 (distress zone).\n"
        "5. Call dividend_analysis(ticker) — check payment consistency and yield.\n"
        "6. Summarise: shortlist with P/E, ROE, F-Score, Z-Score, dividend yield, "
        "and key risks (debt, negative FCF, sector headwinds)."
    )


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        import contextlib
        from collections.abc import AsyncIterator

        import uvicorn
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from starlette.applications import Starlette
        from starlette.routing import Mount

        _assign_tool_groups(mcp)

        def _make_session_mgr(m: FastMCP) -> StreamableHTTPSessionManager:
            return StreamableHTTPSessionManager(
                app=m._mcp_server,
                event_store=m._event_store,
                retry_interval=5,
                stateless=m.settings.stateless_http,
                security_settings=m.settings.transport_security,
            )

        def _make_handler(mgr: StreamableHTTPSessionManager):
            async def _handler(scope, receive, send):
                await mgr.handle_request(scope, receive, send)
            return _handler

        # ── /mcp — all tools ──
        _all_mgr = _make_session_mgr(mcp)

        # ── /g/{group}/mcp — individual tool groups ──
        _group_mgrs: dict[str, StreamableHTTPSessionManager] = {}
        for _g in GROUPS:
            _gmcp = _create_group_server(_g, mcp)
            _group_mgrs[_g] = _make_session_mgr(_gmcp)

        # ── /{role}/mcp — composed role endpoints ──
        _role_mgrs: dict[str, StreamableHTTPSessionManager] = {}
        for _r in ROLES:
            _rmcp = _create_role_server(_r, mcp)
            _role_mgrs[_r] = _make_session_mgr(_rmcp)

        @contextlib.asynccontextmanager
        async def _lifespan(app) -> AsyncIterator[None]:
            async with contextlib.AsyncExitStack() as stack:
                await stack.enter_async_context(_all_mgr.run())
                for mgr in _group_mgrs.values():
                    await stack.enter_async_context(mgr.run())
                for mgr in _role_mgrs.values():
                    await stack.enter_async_context(mgr.run())
                yield

        routes: list[Mount] = [
            Mount(f"/g/{group}", app=_make_handler(mgr))
            for group, mgr in _group_mgrs.items()
        ]
        routes += [
            Mount(f"/{role}", app=_make_handler(mgr))
            for role, mgr in _role_mgrs.items()
        ]
        routes += [Mount("/", app=_make_handler(_all_mgr))]

        app = Starlette(lifespan=_lifespan, routes=routes)

        uvicorn.run(
            app,
            host=mcp.settings.host,
            port=mcp.settings.port,
            log_level=mcp.settings.log_level.lower(),
        )
    else:
        mcp.run(transport=transport)
