"""ru-finance MCP-сервер: инструменты для анализа портфеля поверх moex + cbrapi.

Запуск локально (stdio):   python -m ru_finance.mcp_server
Запуск как remote (HTTP):  MCP_TRANSPORT=streamable-http MCP_PORT=8000 python -m ru_finance.mcp_server
  → эндпоинт http://MCP_HOST:MCP_PORT/mcp  (за nginx/TLS, см. deploy/).
Все инструменты generic — конкретные бумаги передаются параметром (portfolio_* → assets).
Документация ручек: docs/TOOLS.md. Гайд для агента: AGENTS.md.
"""
from __future__ import annotations

import base64
import os
from datetime import date, datetime
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import Icon

from . import bonds, cbr, moex, portfolio, rate, smartlab, vsezpif


def _load_icons() -> list[Icon] | None:
    """Иконка сервера (PT Serif ₽, изумруд) как data-URI. Рендерят Inspector/VS Code/Desktop."""
    path = Path(__file__).parent / "icon.png"
    if not path.exists():
        return None
    data = base64.b64encode(path.read_bytes()).decode()
    return [Icon(src=f"data:image/png;base64,{data}", mimeType="image/png", sizes=["256x256"])]


mcp = FastMCP(
    "ru-finance",
    icons=_load_icons(),
    host=os.environ.get("MCP_HOST", "127.0.0.1"),
    port=int(os.environ.get("MCP_PORT", "8000")),
    stateless_http=True,  # без сессий — удобно за reverse-proxy для нескольких клиентов
    # Сервер рассчитан на работу за reverse-proxy (nginx) при remote-доступе.
    # Встроенная в SDK DNS-rebinding защита пускает только localhost-Host и режет
    # проксированные запросы (421 Invalid Host header); доступ ограничивается на
    # уровне прокси (TLS + секретный путь / IP-allowlist), поэтому отключаем её.
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


# ─────────────────────────── Утилиты ───────────────────────────
@mcp.tool()
def current_datetime() -> dict:
    """Текущая дата и время сервера (UTC+0, ISO 8601).
    
    Возвращает: {datetime, date, time, timestamp}.
    """
    now = datetime.now()
    return {
        "datetime": now.isoformat(),
        "date": now.date().isoformat(),
        "time": now.time().isoformat(),
        "timestamp": now.timestamp(),
    }


# ─────────────────────────── MOEX (Московская биржа) ───────────────────────────
@mcp.tool()
def moex_resolve(query: str) -> dict:
    """Определить бумагу по тикеру/ISIN/названию.

    Вход: query — 'SBER', 'RU000A10C6F7', '26253', 'Сбербанк'.
    Возврат: {secid, engine, market, board, type, shortname, isin}.
    Нужен, когда неясно, как ISS адресует бумагу (акция/облигация/фонд/индекс).
    """
    return moex.resolve(query)


@mcp.tool()
def moex_quote(query: str) -> dict:
    """Текущая котировка акции/фонда (нормализованная, цена с фоллбэками).

    Вход: query — тикер/название. Возврат: {secid, price, change_pct, bid, ask,
    open, low, high, value_today, vol_today, updatetime, price_field}.
    В выходные LAST пуст → отдаётся MARKETPRICE/LCLOSEPRICE (см. price_field).
    """
    return moex.quote(query)


@mcp.tool()
def moex_bond(query: str) -> dict:
    """Облигация: цена %, YTM, дюрация (годы и модиф.), купон, погашение, НКД.

    Вход: query — номер ОФЗ ('26253') или ISIN ('RU000A10C6F7').
    Возврат: {price_pct, ytm, duration_years, mod_duration_years, coupon_pct,
    annual_coupon_per_bond, next_coupon, maturity, accrued_int, face_value, ...}.
    """
    return moex.bond(query)


@mcp.tool()
def moex_bond_coupons(query: str) -> list[dict]:
    """Расписание купонов облигации (история + будущие) из НРД/MOEX.

    Вход: query — ISIN или номер ОФЗ. Возврат: [{coupondate, value, valueprc,
    facevalue, faceunit, is_past, recorddate, startdate}, ...].
    Для ОФЗ возвращает пустой список (данные ЦБ не в ISS). is_past=True — уже выплачен.
    """
    return moex.bond_coupons(query)


@mcp.tool()
def moex_candles(query: str, frm: str, till: str, interval: str = "24") -> list[dict]:
    """Свечи OHLCV за период. interval: 1,10,60(час),24(день),7(нед),31(мес),4(кв).

    Вход: query, frm/till ('YYYY-MM-DD'). Возврат: список {begin,open,high,low,close,value,volume}.
    """
    return moex.candles(query, frm, till, interval)


@mcp.tool()
def moex_history(query: str, frm: str, till: str) -> list[dict]:
    """Дневная история торгов (close/volume/value...) за интервал дат.

    Вход: query, frm/till ('YYYY-MM-DD'). Для доходностей/волатильности/просадок.
    """
    return moex.history(query, frm, till)


@mcp.tool()
def moex_search_endpoints(pattern: str) -> list[dict]:
    """Найти ISS-эндпоинты по подстроке пути (для доступа к данным без готовой ручки).

    Вход: pattern — напр. '/candles', '/dividends', 'turnovers'.
    Возврат: [{id, path, variables}]. id → потом в moex_query.
    """
    return moex.search_endpoints(pattern)


@mcp.tool()
def moex_query(template_id: int, vars: dict | None = None,
               params: dict | None = None) -> dict:
    """Generic-доступ к ЛЮБОМУ из ~252 эндпоинтов ISS по template_id.

    Вход: template_id (из moex_search_endpoints), vars — переменные пути
    (engine/market/board/security...), params — query-параметры (from/till/...).
    Возврат: {block: [строки]}. Запасной путь, когда нет именованной ручки.
    """
    return moex.query(template_id, vars, params)


# ───────────────────── MOEX: корпоративная информация (CCI/НРД) ─────────────────────
@mcp.tool()
def moex_company_info(query: str) -> dict:
    """Справка об организации по ИНН/ОГРН/названию.

    Вход: query — ИНН, ОГРН или фрагмент названия.
    Возврат: {companies: [{basis_company_id, inn, ogrn, name_short_ru, name_full_ru, ...}]}.
    """
    return moex.company_info(query)


@mcp.tool()
def moex_company_info_by_id(company_id: int) -> dict:
    """Справка об организации по внутреннему ID MOEX (basis_company_id).

    Вход: company_id — числовой ID (узнаётся из moex_company_info).
    Возврат: {inn, ogrn, name_short_ru, name_full_ru, lei_code, ...}.
    """
    return moex.company_info_by_id(company_id)


@mcp.tool()
def moex_ir_calendar(limit: int = 50) -> list[dict]:
    """Календарь IR-мероприятий (даты отчётов публичных компаний).

    Вход: limit (по умолч. 50). Возврат: [{company_name, event_type, event_date, ...}].
    """
    return moex.ir_calendar(limit)


# ───────────────────── MOEX: статистика рынка ─────────────────────
@mcp.tool()
def moex_market_capitalization() -> dict:
    """Капитализация фондового рынка (₽).

    Возврат: {capitalization, issuecapitalization, tradedate, updatetime}.
    """
    return moex.market_capitalization()


@mcp.tool()
def moex_correlations(secid: str) -> list[dict]:
    """Коэффициенты корреляции и бета для бумаги.

    Вход: secid (тикер, напр. 'SBER').
    Возврат: [{secid, fxsecid, tradedate, coeff_correlation, coeff_beta}].
    """
    return moex.correlations(secid)


@mcp.tool()
def moex_splits(secid: str | None = None) -> list[dict]:
    """Справочник дроблений и консолидаций бумаг.

    Вход: secid (опционально). Без параметра — все сплиты.
    Возврат: [{tradedate, secid, before, after}].
    """
    return moex.splits(secid)


# ───────────────────── MOEX: рынок облигаций ─────────────────────
@mcp.tool()
def moex_bond_market_aggregates(frm: str | None = None,
                                till: str | None = None) -> list[dict]:
    """Агрегированные показатели рынка облигаций.

    Вход: frm/till ('YYYY-MM-DD', опционально).
    Возврат: [{tradedate, type_bond, iss_nominal, vol_nominal, avg_years, ...}].
    """
    return moex.bond_market_aggregates(frm, till)


@mcp.tool()
def moex_zcyc_history(frm: str, till: str) -> list[dict]:
    """История параметров КБД (Кривая Бескупонной Доходности).

    Вход: frm/till ('YYYY-MM-DD'). Возврат: [{tradedate, b1,b2,b3, t1, g1..g9}].
    Параметры НСС-модели для каждого дня — для бэктестинга кривой.
    """
    return moex.zcyc_history(frm, till)


# ───────────────────── MOEX: активность и курсы ─────────────────────
@mcp.tool()
def moex_turnovers() -> list[dict]:
    """Сводные обороты по рынкам (биржевые итоги).

    Возврат: [{name, valtoday, valtoday_usd, numtrades, updatetime, title}].
    Рынки: stock, currency, futures, commodity, ...
    """
    return moex.turnovers()


@mcp.tool()
def moex_sitenews(limit: int = 20) -> list[dict]:
    """Новости Московской биржи.

    Вход: limit (по умолч. 20). Возврат: [{id, tag, title, published_at}].
    """
    return moex.sitenews(limit)


@mcp.tool()
def moex_aggregates(query: str, date: str) -> dict:
    """Агрегированные итоги торгов за дату по бумаге.

    Вход: query (тикер), date ('YYYY-MM-DD').
    Возврат: {securities: [...], marketdata: [...]} — полные итоги дня.
    """
    return moex.aggregates(query, date)


@mcp.tool()
def moex_indicative_rates(frm: str | None = None,
                          till: str | None = None) -> list[dict]:
    """Индикативные курсы валют срочного рынка.

    Вход: frm/till ('YYYY-MM-DD', опционально).
    Возврат: [{tradedate, tradetime, secid, rate, clearing}].
    """
    return moex.indicative_rates(frm, till)


# ─────────────────────────── Дивиденды (smart-lab.ru) ───────────────────────────
@mcp.tool()
def smartlab_dividends(limit: int = 50) -> list[dict]:
    """Календарь ближайших дивидендов со smart-lab.ru (данные, не ISS).

    Возврат: {name, ticker, period,
    dividend_rub, yield_pct, board_approved, last_buy_date, close_date,
    payment_date, price}. dividend_rub — ₽ за акцию; yield_pct — див. доходность %.
    """
    return smartlab.get_upcoming_dividends(limit)


@mcp.tool()
def smartlab_dividend_history(ticker: str) -> list[dict]:
    """История дивидендов по тикеру со smart-lab.ru (данные, не ISS).

    Вход: ticker — тикер («SBER», «LKOH»). Возврат: список {name, ticker, period,
    dividend_rub, yield_pct, board_approved, last_buy_date, close_date,
    payment_date, price} для этого эмитента.
    """
    return smartlab.get_dividend_history(ticker)


# ─────────────────────────── ЗПИФ выплаты (vsezpif.ru) ───────────────────────────
@mcp.tool()
def zpif_payments(
    fund_name: str | None = None,
    isin: str | None = None,
    limit: int = 50,
) -> dict:
    """Календарь выплат ЗПИФ недвижимости с vsezpif.ru.

    Источник — единственный бесплатный агрегатор данных по 40+ фондам недвижимости.
    Оценка следующей выплаты на основе календаря на 12 месяцев вперед.

    Вход (опционально):
      - fund_name — название фонда (или часть): 'Акцент', 'Парус', 'СФН', 'ВИМ';
      - isin — международный идентификатор;
      - limit — макс. количество записей (default 50).

    Без параметров — все ближайшие выплаты на 12 месяцев.

    Возврат:
      - payments[]: {date, date_iso, fund_name, amount_per_unit};
      - next_payment: {date_iso, fund_name, amount};
      - funds_total: общее число фондов в календаре;
      - cache_until: срок действия кэша (4 часа).
    """
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

    # Получить список всех фондов
    all_funds = vsezpif.list_funds()

    return {
        "payments": payments,
        "next_payment": next_pay,
        "funds_total": len(all_funds),
    }


@mcp.tool()
def zpif_funds_list() -> list[dict]:
    """Список ЗПИФ недвижимости с vsezpif.ru.

    Возвращает словари: {slug, fund_name, url}.
    slug можно использовать в zpif_payments для поиска по slug.
    """
    return vsezpif.list_funds()


# ─────────────────────────── ЦБ РФ (cbrapi) ───────────────────────────
@mcp.tool()
def cbr_key_rate(first_date: str | None = None, last_date: str | None = None,
                 tail: int = 30) -> dict:
    """Ключевая ставка ЦБ РФ (главный драйвер облигаций и рубля).

    Даты опциональны ('YYYY-MM-DD'). Возврат: {latest, latest_date, series[]}.
    """
    return cbr.key_rate(first_date, last_date, tail)


@mcp.tool()
def cbr_ruonia(first_date: str | None = None, last_date: str | None = None,
               tail: int = 30) -> dict:
    """RUONIA overnight (% годовых) — ставка денежного рынка, рыночный ориентир ставки."""
    return cbr.ruonia(first_date, last_date, tail)


@mcp.tool()
def cbr_ruonia_index(first_date: str | None = None, last_date: str | None = None,
                     tail: int = 12) -> dict:
    """RUONIA-индекс + срочные средние (1м/3м/6м, % годовых) — короткая кривая ставок.

    Живая замена прекращённому ROISfix. AVG_* — проценты; RUONIA_INDEX — уровень индекса.
    """
    return cbr.ruonia_index(first_date, last_date, tail)


@mcp.tool()
def cbr_ibor(first_date: str | None = None, last_date: str | None = None,
             tail: int = 12) -> dict:
    """MIACR — фактические средневзвешенные ставки межбанка (MosPrime/MIBOR прекращены, пустые колонки убраны)."""
    return cbr.ibor(first_date, last_date, tail)


@mcp.tool()
def cbr_currency(symbol: str, first_date: str, last_date: str, tail: int = 30) -> dict:
    """Курс валюты ЦБ к рублю. symbol: 'USD','EUR','CNY'. Даты 'YYYY-MM-DD'."""
    return cbr.currency(symbol, first_date, last_date, tail)


@mcp.tool()
def cbr_metals(first_date: str | None = None, last_date: str | None = None,
               tail: int = 12) -> dict:
    """Учётные цены ЦБ на драгметаллы (золото/серебро/платина/палладий)."""
    return cbr.metals(first_date, last_date, tail)


@mcp.tool()
def cbr_reserves(first_date: str | None = None, last_date: str | None = None,
                 tail: int = 12) -> dict:
    """Международные (золотовалютные) резервы РФ (ЗВР)."""
    return cbr.reserves(first_date, last_date, tail)


# ─────────────────────────── Облигационная математика ───────────────────────────
@mcp.tool()
def bond_report(query: str) -> dict:
    """Глубокий разбор облигации: текущие метрики + сценарии по ставке + реальная доходность.

    Вход: query — номер ОФЗ/ISIN. Возврат: {bond, scenarios, real_return}.
    scenarios — полный доход за год при сдвиге доходности на ±п.п. + точка безубытка.
    real_return — доходность к погашению за вычетом разной инфляции.
    """
    b = moex.bond(query)
    rep: dict = {"bond": b}
    if b.get("maturity") and b.get("coupon_pct") and b.get("ytm"):
        rep["scenarios"] = bonds.rate_scenarios(
            b["maturity"], b["coupon_pct"], b["ytm"], today=str(date.today()))
        rep["real_return"] = bonds.real_return(b["ytm"])
    return rep


# ─────────────────────────── Ожидания по ставке (G-кривая ОФЗ) ───────────────────────────
@mcp.tool()
def rate_expectations(key_rate: float | None = None) -> dict:
    """Рыночные ожидания по ключевой ставке из G-кривой ОФЗ (КБД). Только числа.

    key_rate опционален (иначе берётся из cbr_key_rate). Возврат: {as_of, key_rate,
    ruonia, curve[], signals, note}. signals: наклон (slope_10y_1y/2y_3m), брутто-спреды
    к ставке (включают срочную премию!), якорь short_vs_ruonia_1y, лесенка форвардов
    (fwd_1y_in_1y/2y, fwd_3m_in_1y), inverted, машинная метка read
    (cuts_priced|hikes_priced|flat). Интерпретацию под портфель делает клиент/агент.
    """
    return rate.rate_expectations(key_rate)


@mcp.tool()
def curve_yield(years: float) -> dict:
    """Доходность G-кривой ОФЗ на произвольном сроке (% годовых) — привязка к дюрации бумаги.

    Вход: years (напр. 5.9 под дюрацию ОФЗ 26253). Линейная интерполяция по узлам КБД.
    """
    return rate.curve_yield(years)


# ─────────────────────────── Портфель (доменные отчёты) ───────────────────────────
@mcp.tool()
def portfolio_snapshot(assets: str) -> dict:
    """Снимок портфеля: стоимость, P&L, позиции, распределение, риск по ставке
    (дюрация портфеля), денежный поток (купоны/running yield). Главная ручка.

    Вход: assets — портфель в markdown (формат см. в описании portfolio-ручек/TOOLS.md):
    строки '- Название (ТИКЕР/ISIN): N шт. (цена_покупки ...)', '%' = облигация.
    Сервер generic: конкретные бумаги приходят ТОЛЬКО в этом параметре.
    """
    return portfolio.snapshot(assets)


@mcp.tool()
def portfolio_rate_whatif(delta_pp: float, assets: str) -> dict:
    """Что станет с портфелем при сдвиге доходностей облигаций на delta_pp п.п.

    Вход: delta_pp (напр. -1, +2); assets — портфель в markdown (как в portfolio_snapshot).
    Возврат: изменение стоимости (₽ и %) + разбивка по бондам.
    """
    return portfolio.rate_whatif(delta_pp, assets)


@mcp.tool()
def portfolio_income_calendar(assets: str) -> dict:
    """Ближайшие поступления: следующий купон по каждой облигации + объявленные дивиденды.

    Вход: assets — портфель в markdown (как в portfolio_snapshot).
    """
    return portfolio.income_calendar(assets)


@mcp.tool()
def portfolio_movers(assets: str) -> dict:
    """Кто вырос/просел: дневное изменение и P&L против цены покупки (топ-3 в каждую сторону).

    Вход: assets — портфель в markdown (как в portfolio_snapshot).
    """
    return portfolio.movers(assets)


if __name__ == "__main__":
    mcp.run(transport=os.environ.get("MCP_TRANSPORT", "stdio"))
