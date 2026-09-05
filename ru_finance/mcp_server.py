"""ru-finance MCP-сервер: инструменты для анализа портфеля поверх moex + cbrapi.

Запуск локально (stdio):   python -m ru_finance.mcp_server
Запуск как remote (HTTP):  MCP_TRANSPORT=streamable-http MCP_PORT=8000 python -m ru_finance.mcp_server
  → все инструменты:        http://MCP_HOST:MCP_PORT/mcp
  → роль-specific:          http://MCP_HOST:MCP_PORT/{role}/mcp
    роли: market, bond, macro, portfolio, derivatives
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
from mcp.types import Icon

from . import bonds, cbr, moex, portfolio, raexpert, rate, smartlab, vsezpif

# ─────────────────────────── Roles ───────────────────────────
# Каждый инструмент принадлежит одной роли. Generic-инструменты доступны всем
# агентам; остальные — только соответствующему агенту.
ROLES: dict[str, set[str]] = {
    "generic": {
        "current_datetime", "moex_resolve", "moex_search", "moex_quote", "moex_bond",
    },
    "market": {
        "moex_candles", "moex_full_history", "moex_history", "moex_aggregates",
        "moex_company_info", "moex_company_info_by_id", "moex_ir_calendar",
        "moex_market_capitalization", "moex_correlations", "moex_splits",
        "moex_turnovers", "moex_sitenews", "moex_indicative_rates",
        "moex_search_endpoints", "moex_query",
        "smartlab_dividends", "smartlab_dividend_history",
        "price_volatility", "liquidity_assessment",
    },
    "bond": {
        "moex_emitent_bonds", "moex_bond_coupons",
        "moex_bond_market_aggregates", "moex_zcyc_history",
        "bond_report", "bond_accrued_interest", "bond_synthetic_yield",
        "raexpert_rating", "raexpert_emitent_ratings",
        "zpif_payments", "zpif_funds_list",
    },
    "macro": {
        "cbr_key_rate", "cbr_ruonia", "cbr_ruonia_index", "cbr_ibor",
        "cbr_currency", "cbr_metals", "cbr_reserves", "cbr_inflation",
        "rate_expectations", "curve_yield",
    },
    "portfolio": {
        "portfolio_snapshot", "portfolio_rate_whatif",
        "portfolio_income_calendar", "portfolio_movers",
    },
    "derivatives": {
        "moex_futures_list", "moex_futures_open_interest",
        "moex_futures_series", "moex_futures_promo", "moex_futures_basis",
        "moex_options_assets", "moex_options_board",
        "moex_option_quote", "moex_option_orderbook", "moex_option_history",
    },
}

# Обратный индекс: tool_name → role
_TOOL_ROLE: dict[str, str] = {}
for _role, _names in ROLES.items():
    for _name in _names:
        _TOOL_ROLE[_name] = _role

# Registered tool functions for role-based server creation
_ROLE_FN: dict[str, list[Callable]] = {role: [] for role in ROLES}


def _assign_tool_roles(mcp_instance: FastMCP) -> None:
    """Tag every registered tool with role metadata based on ROLES."""
    for tool in mcp_instance._tool_manager._tools.values():
        role = _TOOL_ROLE.get(tool.name, "generic")
        if tool.meta is None:
            tool.meta = {}
        tool.meta["role"] = role


def _create_role_server(role: str, source: FastMCP) -> FastMCP:
    """Create a FastMCP instance containing only tools for *role*.

    Generic tools are included in every role server.
    """
    role_mcp = FastMCP(
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
    allowed = ROLES.get(role, set()) | ROLES["generic"]
    for tool in source._tool_manager._tools.values():
        if tool.name in allowed:
            role_mcp._tool_manager._tools[tool.name] = tool
    return role_mcp


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
@mcp.tool()
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
@mcp.tool()
def moex_resolve(query: str) -> dict:
    """Lookup a single security by ticker/ISIN/name.

    Args: query — 'SBER', 'RU000A10C6F7', '26253', 'Сбербанк'.
    Returns: {secid, engine, market, board, type, shortname, isin, group, is_traded}.
    For multiple matches use moex_search.
    """
    return moex.resolve(query)


@mcp.tool()
def moex_search(query: str, sec_type: str | None = None) -> list[dict]:
    """Search MOEX securities by ticker/ISIN/name.

    Args: query — 'Сбербанк', 'Тинькофф', 'SBER', 'RU000A10C6F7'.
          sec_type — optional filter: 'bond', 'share', 'stock', 'fund', 'etf', 'index'.
    Returns [{secid, shortname, isin, type, group, is_traded, engine, market, board}].
    Only actively traded (is_traded=1) securities are returned.
    Examples:
      moex_search("Сбербанк")                      → all Sberbank securities
      moex_search("Сбербанк", sec_type="bond")      → only bonds
      moex_search("Тинькофф", sec_type="fund")      → only funds
    """
    return moex.resolve(query, sec_type=sec_type, as_list=True, traded_only=True)


@mcp.tool()
def moex_quote(query: str) -> dict:
    """Normalized quote for a share/fund with fallback pricing.

    Args: query — ticker or name.
    Returns: {secid, price, change_pct, bid, ask, open, low, high, value_today,
    vol_today, updatetime, price_field}.
    On weekends LAST is empty → falls back to MARKETPRICE/LCLOSEPRICE (see price_field).
    """
    return moex.quote(query)


@mcp.tool()
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


@mcp.tool()
async def moex_emitent_bonds(
    query: str,
    ctx: Context,
    min_duration: float | None = None,
    max_duration: float | None = None,
) -> list[dict]:
    """All bonds of an issuer with optional duration filter.

    Args: query — issuer name/ticker ('Газпром', 'Сбербанк', 'ГТЛК',
          'Атомэнергопром'). Returns bonds whose emitent_id matches the issuer.
          min_duration/max_duration — filter by duration in years
          (Macaulay duration, fallback: years-to-maturity).
          Pass None to leave a bound unbound.

    Returns [{secid, shortname, isin, board, is_traded, emitent, issuer_name,
    face_value, face_unit, coupon_pct, coupon_period, next_coupon, maturity,
    offer_date, accrued_int, duration_years, mod_duration_years,
    years_to_maturity, price_pct, ytm, value_today, vol_today}].
    Sorted by duration (shortest first).
    """
    await ctx.report_progress(0, 2, "Fetching issuer bonds")
    return moex.emitent_bonds(query, min_duration, max_duration)


@mcp.tool()
def moex_bond_coupons(query: str) -> list[dict]:
    """Coupon schedule (past + future) from NSD/MOEX.

    Args: query — ISIN or OFZ number.
    Returns [{coupondate, value, valueprc, facevalue, faceunit, is_past, recorddate, startdate}].
    Returns empty list for OFZ (CBR data not in ISS). is_past=True → already paid.
    """
    return moex.bond_coupons(query)


@mcp.tool()
def moex_candles(query: str, frm: str, till: str, interval: str = "") -> list[dict]:
    """OHLCV candles for a period.

    Args: query (required, non-empty); interval: 1,10,60(hour),24(day),
    7(week),31(month),4(quarter); empty=auto-select (≤50 candles).
    frm/till ('YYYY-MM-DD').
    Returns [{begin, open, high, low, close, value, volume}].
    """
    return moex.candles(query, frm, till, interval)


@mcp.tool()
def moex_full_history(query: str, frm: str, till: str) -> list[dict]:
    """Daily trading history with all fields for a date range.

    Args: query; frm/till ('YYYY-MM-DD').
    Returns [{TRADEDATE, CLOSE, VOLUME, VALUE, ...}] — full row per trading day.
    WARNING: large date ranges (>1 month) may return A LOT of rows.
    """
    return moex.history(query, frm, till)


@mcp.tool()
def moex_history(query: str, frm: str, till: str) -> list[dict]:
    """Daily trading history with minimal fields for a date range.

    Args: query; frm/till ('YYYY-MM-DD').
    Returns [{TRADEDATE, CLOSE, VOLUME}] — compact data per trading day.
    """
    full = moex.history(query, frm, till)
    return [{"TRADEDATE": r["TRADEDATE"], "CLOSE": r["CLOSE"], "VOLUME": r["VOLUME"]} for r in full]


@mcp.tool()
def moex_search_endpoints(pattern: str) -> list[dict]:
    """Find ISS endpoints by path substring (for raw data access).

    Args: pattern — e.g. '/candles', '/dividends', 'turnovers'.
    Returns [{id, path, variables}]. Use id in moex_query.
    """
    return moex.search_endpoints(pattern)


@mcp.tool()
def moex_query(template_id: int, path_vars: dict | None = None,
               query_params: dict | None = None) -> dict:
    """Generic access to any ISS endpoint by template_id.

    Args: template_id (from moex_search_endpoints); path_vars — (engine/market/board/security...);
    query_params — (from/till/...).
    Returns: {block: [rows]}. Fallback when no named tool exists.
    """
    return moex.query(template_id, path_vars, query_params)


# ───────────────────── MOEX: corporate info (CCI/NSD) ─────────────────────
@mcp.tool()
def moex_company_info(query: str) -> dict:
    """Company lookup by INN/OGRN/name.

    Args: query — INN, OGRN, ticker or company name fragment.
    Returns: {companies: [{basis_company_id, inn, name_short_ru, name_full_ru, okpo, secid}]}.
    Deduplicates by emitent_id; max 20 results.
    """
    return moex.company_info(query)


@mcp.tool()
def moex_company_info_by_id(company_id: int) -> dict:
    """Company lookup by internal MOEX ID (basis_company_id).

    Args: company_id — numeric ID (from moex_company_info).
    Returns: {basis_company_id, inn, name_short_ru, name_full_ru, okpo, secid, ...} or {}.
    """
    return moex.company_info_by_id(company_id)


@mcp.tool()
def moex_ir_calendar(limit: int = 50) -> list[dict]:
    """IR events calendar (earnings dates for public companies).

    Args: limit (default 50).
    Returns: [{company_name, event_type, event_date, ...}].
    """
    return moex.ir_calendar(limit)


# ───────────────────── MOEX: market stats ─────────────────────
@mcp.tool()
def moex_market_capitalization() -> dict:
    """Stock market capitalization (₽).

    Returns: {capitalization, issuecapitalization, tradedate, updatetime}.
    """
    return moex.market_capitalization()


@mcp.tool()
def moex_correlations(secid: str) -> list[dict]:
    """Correlation coefficients and beta for a security.

    Args: secid — ticker, e.g. 'SBER'.
    Returns [{secid, fxsecid, tradedate, coeff_correlation, coeff_beta}].
    """
    return moex.correlations(secid)


@mcp.tool()
def moex_splits(secid: str | None = None) -> list[dict]:
    """Splits and reverse-splits reference.

    Args: secid (optional). Without arg — all splits.
    Returns [{tradedate, secid, before, after}].
    """
    return moex.splits(secid)


# ───────────────────── MOEX: bond market ─────────────────────
@mcp.tool()
def moex_bond_market_aggregates(frm: str | None = None,
                                till: str | None = None) -> list[dict]:
    """Aggregated bond market indicators.

    Args: frm/till ('YYYY-MM-DD', optional).
    Returns [{tradedate, type_bond, iss_nominal, vol_nominal, avg_years, ...}].
    """
    return moex.bond_market_aggregates(frm, till)


@mcp.tool()
def moex_zcyc_history(frm: str, till: str) -> list[dict]:
    """ZCYC (Zero-Coupon Yield Curve) parameters history.

    Args: frm/till ('YYYY-MM-DD').
    Returns [{tradedate, b1,b2,b3, t1, g1..g9}] — NSS model params for backtesting.
    """
    return moex.zcyc_history(frm, till)


# ───────────────────── MOEX: activity and rates ─────────────────────
@mcp.tool()
def moex_turnovers() -> list[dict]:
    """Aggregated trading volumes by market (exchange summary).

    Returns [{name, valtoday, valtoday_usd, numtrades, updatetime, title}].
    Markets: stock, currency, futures, commodity, etc.
    """
    return moex.turnovers()


@mcp.tool()
def moex_sitenews(limit: int = 20) -> list[dict]:
    """Moscow Exchange news feed.

    Args: limit (default 20).
    Returns [{id, tag, title, published_at}].
    """
    return moex.sitenews(limit)


@mcp.tool()
def moex_aggregates(query: str, date: str) -> dict:
    """Daily trading summary for a security.

    Args: query (ticker), date ('YYYY-MM-DD').
    Returns: {securities: [...], marketdata: [...]}.
    """
    return moex.aggregates(query, date)


@mcp.tool()
def moex_indicative_rates(frm: str | None = None,
                          till: str | None = None) -> list[dict]:
    """Indicative FX rates from derivatives market.

    Args: frm/till ('YYYY-MM-DD', optional).
    Returns [{tradedate, tradetime, secid, rate, clearing}].
    """
    return moex.indicative_rates(frm, till)


# ───────────────────── Derivatives (futures / options) ─────────────────────
@mcp.tool()
def moex_futures_list(asset_code: str) -> list[dict]:
    """FORTS futures contracts catalog with market data and spec.

    Valid asset_code values (see ref://futures-underlying-assets):
      FX: Si→USD, Eu→EUR, CNY, CHF, GBP, JPY, HKD, TRY, KZT, BYN.
      Indices: RTS, MREI, MXI, RVI, IMOEX.
      Commodities: BR, GOLD, GL, SV, SLVR, PL, CU, NI.
      Equity: SBRF→SBER, GAZR→GAZP, LKOH, GMKN, MGNT, ROSN, SIBN, VTBR, TATN, ALRS, FEES, MTSI→MTSS, NlNK→NKNC.

    Args: asset_code — underlying code (case-insensitive, e.g. 'Si', 'RTS', 'BR', 'GAZR').
    Returns [{secid, name, asset_code, expiry_date, lot_volume, min_step,
    step_price, initial_margin, last_settle_price, open_interest, oichange,
    bid, offer, last, high, low, volume_today, value_today, ...}].
    """
    return moex.futures_list(asset_code)


@mcp.tool()
def moex_futures_open_interest(asset: str) -> dict:
    """Open interest breakdown by legal/physical persons.

    Valid asset_code values (see ref://futures-underlying-assets):
      FX: Si→USD, Eu→EUR, CNY, CHF, GBP, JPY, HKD, TRY, KZT, BYN.
      Indices: RTS, MREI, MXI, RVI, IMOEX.
      Commodities: BR, GOLD, GL, SV, SLVR, PL, CU, NI.
      Equity: SBRF→SBER, GAZR→GAZP, LKOH, GMKN, MGNT, ROSN, SIBN, VTBR, TATN, ALRS, FEES, MTSI→MTSS, NlNK→NKNC.

    Args: asset — underlying code (case-insensitive, e.g. 'Si', 'RTS', 'BR', 'SBRF').
    Returns: {asset, tradedate, juridical: {oi_long, oi_short, oi_change_...},
    physical: {...}, total_oi_long, total_oi_short}.
    Shows who (retail vs professional) is building/reducing positions.
    """
    return moex.futures_open_interest(asset)


@mcp.tool()
def moex_futures_series(asset: str | None = None) -> list[dict]:
    """Futures expiration calendar — contracts with settlement dates.

    Args: asset — underlying code ('Si', 'RTS', ...), optional. None → all series (up to 500).
    Returns [{secid, name, start_date, expiration_date, asset_code,
    underlying_asset, is_traded, is_expired, days_to_expiry}].
    days_to_expiry: < 0 → already expired.
    """
    return moex.futures_series(asset)


@mcp.tool()
def moex_futures_promo() -> dict:
    """FORTS aggregated fee statistics.

    Returns: {fee_forts, fee_options, fee_all, updated_at}.
    """
    return moex.futures_promo()


@mcp.tool()
async def moex_futures_basis(asset_code: str, ctx: Context) -> dict:
    """Futures contango / backwardation — annualised carry from basis.

    Compares futures settle price to the underlying spot price (FX rate from CBR,
    stock/index/commodity from MOEX) and annualises the spread to expiry.
    Positive → contango (sell futures + buy spot); negative → backwardation.

    Valid asset_code values (see ref://futures-underlying-assets):
      FX: Si→USD, Eu→EUR, CNY, CHF, GBP, JPY, HKD, TRY, KZT, BYN.
      Indices: RTS, MREI, MXI, RVI, IMOEX.
      Commodities: BR, GOLD, GL, SV, SLVR, PL, CU, NI.
      Equity: SBRF→SBER, GAZR→GAZP, LKOH, GMKN, MGNT, ROSN, SIBN, VTBR, TATN, ALRS, FEES, MTSI→MTSS, NlNK→NKNC.

    Args: asset_code — underlying code (case-insensitive, e.g. 'Si', 'RTS', 'BR', 'SBRF').
    Returns: {asset_code, underlying: {price, source}, regime,
    contracts: [{secid, name, expiry_date, days_to_expiry, futures_price,
    spot_price, basis_pct, annualized_return_pct, open_interest}]}.
    """
    await ctx.report_progress(0, 3, "Fetching futures data")
    return moex.futures_basis(asset_code)


@mcp.tool()
def moex_options_assets() -> list[dict]:
    """FORTS options underlying assets with market data.

    Returns [{tradedate, asset, asset_name, asset_type, asset_last_price,
    asset_last_to_prev, asset_high, asset_low, val_today, vol_today, num_trades,
    open_position, oichange, option_secid}].
    """
    return moex.options_assets()


@mcp.tool()
def moex_options_board(asset: str) -> dict:
    """Option board (volatility, strikes, OI) for underlying.

    Args: asset — underlying code ('Si', 'RTS', 'SBRF', 'GAZR', ...).
    Returns: {asset_info: {central_strike, underlying_settle, last_del_date},
    calls: [{secid, strike, iv, last, theor_price, bid, offer, oi, volume}],
    puts: [same]}.
    """
    return moex.options_board(asset)


@mcp.tool()
def moex_option_quote(secid: str) -> dict:
    """Single option instrument quote.

    Args: secid — instrument code ('Si87000BI6A', 'GZ85CU6A', ...).
    Returns: {secid, shortname, strike, option_type,
    underlying_asset, underlying_settle, expiration_date, last_trade_date,
    last, bid, offer, spread, oi, volume, settle_price, ...,
    margin: im_np, im_sp, im_buy}.
    """
    return moex.option_quote(secid)


@mcp.tool()
def moex_option_orderbook(secid: str) -> dict:
    """Best bid/offer for an option instrument.

    Note: full depth-of-market unavailable via ISS REST (endpoint returns HTML). Returns best bid/offer and spread.

    Args: secid — instrument code ('Si87000BI6A', 'GZ85CU6A', ...).
    Returns: {secid, bid, offer, spread, bid_depth, offer_depth}.
    """
    return moex.option_orderbook(secid)


@mcp.tool()
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
@mcp.tool()
async def smartlab_dividends(ctx: Context, limit: int = 50) -> list[dict]:
    """Upcoming dividends calendar from smart-lab.ru.

    Returns [{name, ticker, period, dividend_rub, yield_pct, board_approved,
    last_buy_date, close_date, payment_date, price}].
    dividend_rub — ₽ per share; yield_pct — dividend yield %.
    """
    await ctx.report_progress(0, 2, "Fetching upcoming dividends")
    return smartlab.get_upcoming_dividends(limit)


@mcp.tool()
async def smartlab_dividend_history(ticker: str, ctx: Context) -> list[dict]:
    """Dividend history by ticker from smart-lab.ru.

    Args: ticker — e.g. 'SBER', 'LKOH'.
    Returns [{name, ticker, period, dividend_rub, yield_pct, board_approved,
    last_buy_date, close_date, payment_date, price}].
    """
    await ctx.report_progress(0, 2, "Fetching dividend history")
    return smartlab.get_dividend_history(ticker)

# ─────────────────────────── Credit ratings (raexpert.ru) ───────────────────────────
@mcp.tool()
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


@mcp.tool()
async def raexpert_emitent_ratings(
    ctx: Context,
    rating_min: str | None = None,
    sector: str | None = None,
) -> list[dict]:
    """Bond issuers (emitents) filtered by credit rating and/or industry sector.

    Source: raexpert.ru (rating data, 4h cache) + MOEX sector indices (mapping).
    Returns only emitents (companies/banks/insurers), not individual bond emissions.

    Rating filter: keeps emitents with rating >= rating_min (e.g. 'ruBBB-' = investment
    grade and above). Entries with 'отозван' (revoked) are excluded when filtering.
    Rating scale (descending): ruAAA(19) > ruAA+(18) > ruAA(17) > ruAA-(16) >
      ruA+(15) > ruA(14) > ruA-(13) > ruBBB+(12) > ruBBB(11) > ruBBB-(10) >
      ruBB+(9) > ruBB(8) > ruBB-(7) > ruB+(6) > ruB(5) > ruB-(4) > ruCCC(3).

    Sector filter: matches ~100 major emitents from MOEX sectoral stock indices
      (MOEXFN, MOEXOG, etc.) by company name. Available sectors: Финансовый,
      Нефтегазовый, Потребительский, Телекоммуникации, Электроэнергетика,
      Транспорт, Металлургия и добыча, Недвижимость, Химия, Инновации и IT.
      Covers only public companies traded on MOEX; non-listed companies in these
      sectors won't match.

    Args (all optional — without args returns all emitents with revoked):
      rating_min — minimum rating ('ruBBB−', 'ruA+', 'ruA', 'ruAA-', ...).
      sector — MOEX sector name (exact match from the list above).

    Returns [{name, rating, outlook, date, category, sector?, agency}].
    Results sorted by rating (best first), then by name.
    """
    await ctx.report_progress(0, 2, "Fetching emitent ratings")
    return raexpert.emitent_rating_search(rating_min=rating_min, sector=sector)


# ─────────────────────────── ZPIF payments (vsezpif.ru) ───────────────────────────
@mcp.tool()
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


@mcp.tool()
def zpif_funds_list() -> list[dict]:
    """List of ЗПИФ funds from vsezpif.ru.

    Returns [{slug, fund_name, url}]. Use slug in zpif_payments.
    """
    return vsezpif.list_funds()


# ─────────────────────────── CBR (Central Bank of Russia) ───────────────────────────
@mcp.tool()
def cbr_key_rate(first_date: str | None = None, last_date: str | None = None,
                 tail: int = 30) -> dict:
    """CBR key rate — main driver for bonds and RUB.

    Args: first_date/last_date ('YYYY-MM-DD', optional).
    Returns: {latest, latest_date, series[]}.
    """
    return cbr.key_rate(first_date, last_date, tail)


@mcp.tool()
def cbr_ruonia(first_date: str | None = None, last_date: str | None = None,
               tail: int = 30) -> dict:
    """RUONIA overnight (% annualized) — money market rate, market rate benchmark."""
    return cbr.ruonia(first_date, last_date, tail)


@mcp.tool()
def cbr_ruonia_index(first_date: str | None = None, last_date: str | None = None,
                     tail: int = 12) -> dict:
    """RUONIA index + term averages (1m/3m/6m, % annualized) — short end of curve.

    Replacement for discontinued ROISfix. AVG_* — %; RUONIA_INDEX — index level.
    """
    return cbr.ruonia_index(first_date, last_date, tail)


@mcp.tool()
def cbr_ibor(first_date: str | None = None, last_date: str | None = None,
             tail: int = 12) -> dict:
    """MIACR — actual weighted interbank rates (MosPrime/MIBOR discontinued)."""
    return cbr.ibor(first_date, last_date, tail)


@mcp.tool()
def cbr_currency(symbol: str, first_date: str, last_date: str, tail: int = 30) -> dict:
    """CBR FX rate vs RUB. symbol: 'USD','EUR','CNY'. Dates 'YYYY-MM-DD'."""
    return cbr.currency(symbol, first_date, last_date, tail)


@mcp.tool()
def cbr_metals(first_date: str | None = None, last_date: str | None = None,
               tail: int = 12) -> dict:
    """CBR precious metals prices (gold/silver/platinum/palladium)."""
    return cbr.metals(first_date, last_date, tail)


@mcp.tool()
def cbr_reserves(first_date: str | None = None, last_date: str | None = None,
                 tail: int = 12) -> dict:
    """Russia international reserves (gold + FX)."""
    return cbr.reserves(first_date, last_date, tail)


@mcp.tool()
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
@mcp.tool()
async def bond_report(query: str, ctx: Context) -> dict:
    """Deep bond analysis: metrics + rate scenarios + spread to curve + convexity.

    Args: query — OFZ number/ISIN.
    Returns: {bond, years_to_maturity, convexity, accrued_interest, gry, spread_to_curve,
    scenarios, twist_scenarios, real_return}.
    scenarios — total return for ±bp parallel shift + breakeven point.
    twist_scenarios — curve steepening/flattening scenarios.
    spread_to_curve — YTM spread to G-curve at matching duration.
    gry — gross redemption yield (YTM + accrued).
    real_return — yield vs CPI (Rosstat) + scenarios under different assumptions.
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


@mcp.tool()
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


@mcp.tool()
async def bond_synthetic_yield(query: str, horizon_years: float, ctx: Context,
                         reinvest_rate: float | None = None) -> dict:
    """Synthetic yield with coupon reinvestment over investment horizon.

    Calculates IRR of the full cash flow: buy at dirty price, coupons
    reinvested at reinvest_rate (default: YTM), sell at assumed YTM at
    horizon (or receive face if horizon >= maturity).

    Args:
        query — OFZ number/ISIN ('26253' or 'RU000A10C6F7').
        horizon_years — investment horizon in years (e.g. 3.0).
        reinvest_rate — coupon reinvestment rate, % annualized (optional,
            defaults to current YTM).

    Returns: {irr_pct, ytm_pct, horizon_years, reinvest_rate_pct,
    buy_price_rub, total_coupons_rub, reinvested_coupons_rub,
    final_value_rub, total_at_horizon_rub, total_return_pct,
    annualized_return_pct, coupon_count, note}.

    irr_pct — annualized internal rate of return of the full strategy.
    Comparison with ytm_pct shows the impact of reinvestment assumptions.
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


@mcp.tool()
async def price_volatility(query: str, ctx: Context, days: int = 90, rf_annual: float = 16.0) -> dict:
    """Volatility, Sharpe ratio, max drawdown from daily candles.

    Args: query (ticker), days (default 90), rf_annual (risk-free rate, % annualized).
    Returns: {annual_vol_pct, daily_vol_pct, sharpe, max_drawdown_pct,
    total_return_pct, high_price, low_price, trading_days, ...}.
    sharpe = (mean_excess_return / volatility) * sqrt(252).
    """
    await ctx.report_progress(0, 2, "Fetching candle data")
    return moex.price_volatility(query, days, rf_annual)


@mcp.tool()
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


# ─────────────────────────── Rate expectations (OFZ G-curve) ───────────────────────────
@mcp.tool()
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


@mcp.tool()
def curve_yield(years: float) -> dict:
    """G-curve OFZ yield at arbitrary maturity (% annualized) — for duration mapping.

    Args: years (e.g. 5.9 for OFZ 26253 duration). Linear interpolation on ZCYC nodes.
    """
    return rate.curve_yield(years)


# ─────────────────────────── Portfolio (domain reports) ───────────────────────────
@mcp.tool()
async def portfolio_snapshot(assets: str, ctx: Context) -> dict:
    """Portfolio snapshot: value, P&L, positions, allocation, rate risk,
    income stream, dividend yield, real return.

    Args: assets — portfolio in markdown format (see TOOLS.md):
    lines '- Name (TICKER/ISIN): N pcs. (purchase_price ...)'.
    Type (share/bond) auto-detected by ISIN/ticker on MOEX.
    Bonds: price in % of face; shares/funds: in RUB.
    Server is generic: securities come ONLY in this parameter.
    Positions include spread_to_curve_pp, div_yield_pct.
    income_risk — real portfolio return (running_yield − Rosstat CPI;
    if CPI unavailable — key rate as proxy).
    """
    await ctx.report_progress(0, 3, "Fetching inflation data")
    try:
        infl_data = cbr.inflation(tail=1)
        inflation_pct = infl_data.get("latest_inflation")
    except Exception:  # noqa: BLE001
        inflation_pct = None
    await ctx.report_progress(1, 3, "Building portfolio snapshot")
    return portfolio.snapshot(assets, inflation_pct=inflation_pct)


@mcp.tool()
async def portfolio_rate_whatif(delta_pp: float, assets: str, ctx: Context) -> dict:
    """Portfolio impact of delta_pp percentage point shift in bond yields.

    Args: delta_pp (e.g. -1, +2); assets — markdown portfolio (same as portfolio_snapshot).
    Returns: value change (RUB and %) + bond-by-bond breakdown.
    """
    await ctx.report_progress(0, 2, "Computing rate scenario")
    return portfolio.rate_whatif(delta_pp, assets)


@mcp.tool()
async def portfolio_income_calendar(assets: str, ctx: Context) -> dict:
    """Upcoming income: next coupon per bond + declared dividends.

    Args: assets — markdown portfolio (same as portfolio_snapshot).
    """
    await ctx.report_progress(0, 2, "Fetching income calendar")
    return portfolio.income_calendar(assets)


@mcp.tool()
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


if __name__ == "__main__":
    transport = os.environ.get("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        import contextlib
        from collections.abc import AsyncIterator

        import uvicorn
        from mcp.server.streamable_http_manager import StreamableHTTPSessionManager
        from starlette.applications import Starlette
        from starlette.routing import Mount

        _assign_tool_roles(mcp)

        # ── Build session managers ──
        _all_mgr = StreamableHTTPSessionManager(
            app=mcp._mcp_server,
            event_store=mcp._event_store,
            retry_interval=5,
            stateless=mcp.settings.stateless_http,
            security_settings=mcp.settings.transport_security,
        )

        _role_mgrs: dict[str, tuple[FastMCP, StreamableHTTPSessionManager]] = {}
        for _role in ROLES:
            _rmcp = _create_role_server(_role, mcp)
            _role_mgrs[_role] = (_rmcp, StreamableHTTPSessionManager(
                app=_rmcp._mcp_server,
                event_store=_rmcp._event_store,
                retry_interval=5,
                stateless=_rmcp.settings.stateless_http,
                security_settings=_rmcp.settings.transport_security,
            ))

        # ASGI handlers (invoke after session_manager.run() has been entered)
        async def _all_asgi(scope, receive, send):
            await _all_mgr.handle_request(scope, receive, send)

        _role_asgi: dict[str, object] = {}
        for _role, (_rmcp, _mgr) in _role_mgrs.items():
            # Closure to capture _mgr
            def _make_handler(m):
                async def _handler(scope, receive, send):
                    await m.handle_request(scope, receive, send)
                return _handler
            _role_asgi[_role] = _make_handler(_mgr)

        @contextlib.asynccontextmanager
        async def _lifespan(app) -> AsyncIterator[None]:
            async with contextlib.AsyncExitStack() as stack:
                await stack.enter_async_context(_all_mgr.run())
                for _, (_, mgr) in _role_mgrs.items():
                    await stack.enter_async_context(mgr.run())
                yield

        routes: list[Mount] = [
            Mount(f"/{role}", app=handler)
            for role, handler in _role_asgi.items()
        ]
        routes.append(Mount("/", app=_all_asgi))

        app = Starlette(lifespan=_lifespan, routes=routes)

        uvicorn.run(
            app,
            host=mcp.settings.host,
            port=mcp.settings.port,
            log_level=mcp.settings.log_level.lower(),
        )
    else:
        mcp.run(transport=transport)
