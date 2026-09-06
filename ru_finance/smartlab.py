"""Данные со smart-lab.ru: дивиденды, фундаментальный скринер, финансовая отчётность.

ISS-эндпоинт дивидендов (`/securities/{secid}/dividends`) не возвращает данные,
а «правильный» эндпоинт закрыт пейволом. Поэтому берём календарь и историю
дивидендов, а также фундаментальные данные, с публичного портала smart-lab.ru
(скрейпинг серверных HTML-таблиц), как сделано в mcp-smartlab.

Данные факт-ориентированные (объявленные выплаты), кэш в памяти на 4 ч.
"""
from __future__ import annotations

import re
import time
from typing import Any

import requests
from bs4 import BeautifulSoup, Tag

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/136.0.0.0 Safari/537.36"
)

_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
}

_CACHE: dict[str, tuple[float, str]] = {}


def _fetch(path: str) -> str:
    """GET страницы smart-lab.ru с кэшем (TTL 4 ч)."""
    now = time.monotonic()
    cached = _CACHE.get(path)
    if cached is not None:
        ts, html = cached
        if now - ts < 240 * 60:
            return html

    resp = requests.get(
        f"https://smart-lab.ru{path}",
        headers=_HEADERS,
        timeout=30,
        allow_redirects=True,
    )
    resp.raise_for_status()
    html = resp.text

    _CACHE[path] = (now, html)
    return html


def _clean(text: str) -> str:
    """Убрать лишние пробелы и нормализовать."""
    return re.sub(r"\s+", " ", text).strip()


def _parse_number(text: str) -> float | None:
    """Разобрать число из ячеек вида '110', '3 456', '37,64₽', '1,4%'."""
    text = _clean(text)
    if not text or text in ("-", "\u2014"):
        return None
    # оставить только цифры и разделители; убрать символы валюты/процента/пробелы
    text = re.sub(r"[^\d.,\-]", "", text).strip()
    if "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _parse_date(text: str) -> str | None:
    """Разобрать дату из ячеек вида '18.09.2026', вернуть как есть."""
    text = _clean(text)
    if not text or text in ("-", "\u2014", "\xa0"):
        return None
    return text


def _get_table(html: str) -> Tag | None:
    """Первая <table> в HTML."""
    return BeautifulSoup(html, "lxml").find("table")


def _table_rows(table: Tag) -> list[list[Tag]]:
    """Все строки таблицы как списки ячеек (td/th)."""
    return [tr.find_all(["td", "th"]) for tr in table.find_all("tr") if tr.find_all(["td", "th"])]


def parse_dividends_table(html: str) -> list[dict[str, Any]]:
    """Календарь дивидендов (таблица из /dividends/).

    Колонки: Название, Тикер, Период, Дивиденд руб, Див. Дох., СД,
    Купить До, Дата закрытия реестра, Выплата До, Цена акции.
    """
    table = _get_table(html)
    if not table:
        return []

    rows = _table_rows(table)
    if len(rows) < 2:
        return []

    results = []
    for row in rows[1:]:
        if len(row) < 10:
            continue
        results.append({
            "name": _clean(row[0].get_text()),
            "ticker": _clean(row[1].get_text()),
            "period": _clean(row[2].get_text()),
            "dividend_rub": _parse_number(row[3].get_text()),
            "yield_pct": _parse_number(row[4].get_text()),
            "board_approved": bool(_clean(row[5].get_text())),
            "last_buy_date": _parse_date(row[6].get_text()),
            "close_date": _parse_date(row[7].get_text()),
            "payment_date": _parse_date(row[8].get_text()),
            "price": _parse_number(row[9].get_text()),
        })
    return results


def _payout_tables(html: str) -> list[Tag]:
    """Только выплатные таблицы /q/{ticker}/dividend/ (пропускает матрицы
    сводки по годам).

    Выплатная таблица распознаётся по шапке из <th> с колонками «Тикер» и
    «Див.доходность». Матрица сводки использует смешанные th/td (годы в шапке)
    и в результат не попадает.
    """
    soup = BeautifulSoup(html, "lxml")
    out = []
    for table in soup.find_all("table"):
        header_cells = []
        for tr in table.find_all("tr"):
            cells = tr.find_all(["td", "th"])
            if cells and all(c.name == "th" for c in cells):
                header_cells = [c.get_text().strip() for c in cells]
                break
        if header_cells and "Тикер" in header_cells and "Див.доходность" in header_cells:
            out.append(table)
    return out


def parse_dividend_history_table(table: Tag) -> list[dict[str, Any]]:
    """Одна выплатная таблица /q/{ticker}/dividend/.

    Колонки: Тикер, дата T-1, дата отсечки, Период, дивенд, Цена акции,
    Див.доходность.
    """
    rows = _table_rows(table)
    # найти строку заголовка (все ячейки <th>) и пропустить её + строки-разделители
    header_idx = None
    for i, row in enumerate(rows):
        if row and all(c.name == "th" for c in row):
            header_idx = i
            break
    if header_idx is None:
        return []

    results = []
    for row in rows[header_idx + 1:]:
        if len(row) < 7:  # строки-разделители (например «Выплаченные»)
            continue
        results.append({
            "ticker": _clean(row[0].get_text()),
            "date_t1": _parse_date(row[1].get_text()),
            "cutoff_date": _parse_date(row[2].get_text()),
            "period": _clean(row[3].get_text()),
            "dividend_rub": _parse_number(row[4].get_text()),
            "price": _parse_number(row[5].get_text()),
            "yield_pct": _parse_number(row[6].get_text()),
        })
    return results


def get_upcoming_dividends(limit: int = 50) -> list[dict[str, Any]]:
    """Календарь ближайших дивидендов со smart-lab.ru.

    Возврат: {name, ticker, period, dividend_rub, yield_pct, board_approved,
    last_buy_date, close_date, payment_date, price}.
    """
    html = _fetch("/dividends/")
    return parse_dividends_table(html)[:limit]


def get_dividend_history(ticker: str) -> list[dict[str, Any]]:
    """История дивидендов по тикеру со smart-lab.ru.

    Источник — страница /q/{ticker}/dividend/. Возврат: {ticker, date_t1,
    cutoff_date, period, dividend_rub, price, yield_pct}. dividend_rub — ₽ за
    акцию; yield_pct — дивидендная доходность %.
    """
    html = _fetch(f"/q/{ticker.upper()}/dividend/")
    results: list[dict[str, Any]] = []
    for table in _payout_tables(html):
        results.extend(parse_dividend_history_table(table))

    def _sort_key(row: dict[str, Any]) -> str:
        """DD.MM.YYYY → YYYYMMDD для хронологической сортировки."""
        d = row.get("cutoff_date") or ""
        if "." in d:
            dd, mm, yyyy = d.split(".")
            return f"{yyyy}{mm}{dd}"
        return d

    # хронологический порядок (история → ожидаемые выплаты)
    return sorted(results, key=_sort_key)


# ─────────────────── Фундаментальный скринер (/q/shares_fundamental2/) ───────────────────

# Позиции колонок в таблице скринера (0-indexed)
_SC_NUM = 0       # № п/п
_SC_NAME = 1      # Название компании
_SC_TICKER = 2    # Тикер
_SC_CHART1 = 3    # иконка графика
_SC_CHART2 = 4    # иконка отчётов
_SC_CAP = 5       # Капитализация, млрд руб
_SC_EV = 6        # EV, млрд руб
_SC_REVENUE = 7   # Выручка
_SC_NET_INCOME = 8  # Чистая прибыль
_SC_DIV_YIELD = 9   # ДД ао, %
_SC_DIV_YIELD_P = 10  # ДД ап, %
_SC_DIV_PAYOUT = 11   # ДД/ЧП, %
_SC_PE = 12        # P/E
_SC_PS = 13        # P/S
_SC_PB = 14        # P/B
_SC_EV_EBITDA = 15 # EV/EBITDA
_SC_EBITDA_MARGIN = 16  # Рентаб. EBITDA
_SC_DEBT_EBITDA = 17    # Долг/EBITDA
_SC_REPORT = 18    # Тип отчёта (LTM-МСФО и т.п.)

_SC_MIN_COLS = 16  # минимальное кол-во ячеек для парсинга


def _parse_screener_row(cells: list[Tag]) -> dict[str, Any] | None:
    """Одна строка таблицы скринера → словарь с мультипликаторами."""
    if len(cells) < _SC_MIN_COLS:
        return None

    ticker = _clean(cells[_SC_TICKER].get_text())
    if not ticker:
        return None

    result: dict[str, Any] = {
        "ticker": ticker,
        "name": _clean(cells[_SC_NAME].get_text()),
    }

    if len(cells) > _SC_CAP:
        result["market_cap"] = _parse_number(cells[_SC_CAP].get_text())
    if len(cells) > _SC_EV:
        result["ev"] = _parse_number(cells[_SC_EV].get_text())
    if len(cells) > _SC_REVENUE:
        result["revenue"] = _parse_number(cells[_SC_REVENUE].get_text())
    if len(cells) > _SC_NET_INCOME:
        result["net_income"] = _parse_number(cells[_SC_NET_INCOME].get_text())
    if len(cells) > _SC_DIV_YIELD:
        result["div_yield"] = _parse_number(cells[_SC_DIV_YIELD].get_text())
    if len(cells) > _SC_DIV_YIELD_P:
        result["div_yield_priv"] = _parse_number(cells[_SC_DIV_YIELD_P].get_text())
    if len(cells) > _SC_DIV_PAYOUT:
        result["div_payout_ratio"] = _parse_number(cells[_SC_DIV_PAYOUT].get_text())
    if len(cells) > _SC_PE:
        result["p_e"] = _parse_number(cells[_SC_PE].get_text())
    if len(cells) > _SC_PS:
        result["p_s"] = _parse_number(cells[_SC_PS].get_text())
    if len(cells) > _SC_PB:
        result["p_b"] = _parse_number(cells[_SC_PB].get_text())
    if len(cells) > _SC_EV_EBITDA:
        result["ev_ebitda"] = _parse_number(cells[_SC_EV_EBITDA].get_text())
    if len(cells) > _SC_EBITDA_MARGIN:
        result["ebitda_margin"] = _parse_number(cells[_SC_EBITDA_MARGIN].get_text())
    if len(cells) > _SC_DEBT_EBITDA:
        result["debt_ebitda"] = _parse_number(cells[_SC_DEBT_EBITDA].get_text())
    if len(cells) > _SC_REPORT:
        result["report_type"] = _clean(cells[_SC_REPORT].get_text())

    return result


def parse_screener_table(html: str) -> list[dict[str, Any]]:
    """Парсинг таблицы скринера shares_fundamental2."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="trades-table")
    if not table:
        return []

    results: list[dict[str, Any]] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all(["td", "th"])
        # пропускаем заголовок: первая ячейка — <th> а в данных — <td>
        if not cells or cells[0].name == "th":
            continue
        row = _parse_screener_row(cells)
        if row is not None:
            results.append(row)
    return results


def get_stock_screener(
    *,
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
    order_by: str = "market_cap",
    order_dir: str = "desc",
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Быстрый скринер акций ММВБ со smart-lab.ru (LTM-мультипликаторы).

    Один HTTP-запрос → все акции с ключевыми мультипликаторами.
    Возврат: [{ticker, name, market_cap, ev, revenue, net_income,
    div_yield, div_yield_priv, div_payout_ratio, p_e, p_s, p_b,
    ev_ebitda, ebitda_margin, debt_ebitda, report_type}].

    Args:
        period: период отчётности ('LTM', '2025', '2024', ...)
        report_type: тип отчётности ('-1'=любой, 'MSFO', 'RSBU')
        sector_id: ID сектора (1=НЕФТЕГАЗ, 2=БАНКИ, 3=МЕТАЛЛУРГИЯ черн.,
            4=Э/ГЕНЕРАЦИЯ, 5=РИТЕЙЛ, 6=ТЕЛЕКОМ, 7=ТРАНСПОРТ, 8=СТРОИТЕЛИ,
            9=МАШИНОСТРОЕНИЕ, 13=ПОТРЕБ, 14=ФИНАНСЫ, 15=HIGH TECH, ...)
        capitalization_min: мин. капитализация (₽, напр. 10_000_000_000)
        capitalization_max: макс. капитализация (₽)
        volume_min: мин. среднедневной объём (₽)
        volume_max: макс. среднедневной объём (₽)
        company_type: тип компании (''=все, 'growth', 'value')
        is_state_owned: -1=все, 1=гос, 0=частные
        is_exporter: -1=все, 1=экспорт, 0=внутренний
        is_raw_stuff: -1=все, 1=сырьевые, 0=несырьевые
        order_by: поле сортировки (market_cap, ev, revenue, p_e, p_s, p_b,
            ev_ebitda, ebitda_margin, debt_ebitda, div_yield, net_income)
        order_dir: направление ('asc' или 'desc')
        limit: макс. количество результатов
    """
    path = f"/q/shares_fundamental2/order_by_{order_by}/{order_dir}/"
    params: list[str] = []
    params.append(f"year={period}")
    params.append(f"type={report_type}")
    if sector_id is not None:
        params.append(f"sector_id%5B%5D={sector_id}")
    params.append(f"capitalization_gt={capitalization_min or 0}")
    params.append(f"capitalization_lt={capitalization_max if capitalization_max is not None else -1}")
    params.append(f"val_middle_gt={volume_min or 0}")
    params.append(f"val_middle_lt={volume_max if volume_max is not None else -1}")
    if company_type:
        params.append(f"company_type={company_type}")
    params.append(f"is_state_owned={is_state_owned}")
    params.append(f"is_exporter={is_exporter}")
    params.append(f"is_raw_stuff={is_raw_stuff}")

    query = "&".join(params)
    full_path = f"{path}?{query}" if query else path
    html = _fetch(full_path)
    return parse_screener_table(html)[:limit]


# ─────────── Детальный финансовый профиль (/q/{ticker}/f/y/MSFO/) ───────────

# Поля, которые извлекаем из финансовой таблицы (field → русское название)
_FINANCIAL_FIELDS: dict[str, str] = {
    # Мультипликаторы
    "p_e": "P/E",
    "p_s": "P/S",
    "p_b": "P/B",
    "p_bv": "P/BV",
    "p_fcf": "P/FCF",
    "ev_ebitda": "EV/EBITDA",
    "ev": "EV",
    "market_cap": "Капитализация",
    "eps": "EPS",
    "bv_share": "BV/акцию",
    "fcf_share": "FCF/акцию",
    "free_float": "Free Float",
    "fcf_yield": "Доходность FCF",
    # Отчёт о прибылях
    "revenue": "Выручка",
    "ebitda": "EBITDA",
    "operating_income": "Операционная прибыль",
    "net_income": "Чистая прибыль",
    "net_income_ns": "Чистая прибыль н/с",
    "cost_of_production": "Себестоимость",
    "opex": "Опер. расходы",
    "amortization": "Амортизация",
    "employment_expenses": "Расх на персонал",
    "interest_expenses": "Процентные расходы",
    # Денежный поток
    "ocf": "Опер.денежный поток",
    "fcf": "FCF",
    "capex": "CAPEX",
    "capex_revenue": "CAPEX/Выручка",
    # Баланс
    "assets": "Активы",
    "net_assets": "Чистые активы",
    "book_value": "Баланс стоимость",
    "debt": "Долг",
    "net_debt": "Чистый долг",
    "cash": "Наличность",
    "goodwill": "Гудвилл",
    "intangible_assets": "Нематер.активы",
    "investment_portfolio": "Инвестиционный портфель",
    # Рентабельность
    "roe": "ROE",
    "roa": "ROA",
    "ebitda_margin": "Рентаб EBITDA",
    "net_margin": "Чистая рентаб",
    # Долг
    "debt_ebitda": "Долг/EBITDA",
    # Дивиденды
    "dividend": "Дивиденд",
    "dividend_pr": "Дивиденд ап",
    "div_yield": "Див доход, ао",
    "div_yield_priv": "Див доход, ап",
    "dividend_payout": "Див.выплата",
    "div_payout_ratio": "Дивиденды/прибыль",
    # Акции
    "common_share": "Цена акции ао",
    "priv_share": "Цена акции ап",
    "number_of_shares": "Число акций ао",
    "number_of_priv_shares": "Число акций ап",
    # Банки
    "net_operating_income": "Чистый операц доход",
    "net_interest_income": "Чист. проц. доходы",
    "commission_income": "Чист. комисс. доход",
    "bank_assets": "Активы банка",
    "capital": "Капитал",
    "loan_portfolio": "Кредитный портфель",
    "deposits": "Депозиты",
    "core_capital_adequacy_ratio": "Дост.осн капитала",
    "total_capital_adequacy_ratio": "Дост. общ капитала",
    "cost_of_risk_ratio": "Стоимость риска (CoR)",
    "cost_to_income": "Расходы/Доходы (CIR)",
    "loan_to_deposit_ratio": "Loan-to-deposit ratio",
    "share_of_non_performing_loans": "Просроченные кредиты, NPL",
    "net_intertest_margin": "Чистая процентная маржа",
    "bank_margin": "Рентабельность банка",
}


def parse_company_financials_table(
    html: str,
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Парсинг таблицы финансовой отчётности /q/{ticker}/f/y/... .

    Args:
        html: HTML страницы
        fields: список полей для извлечения (field-атрибуты <tr>).
               Если None — извлекаем все известные поля.
               Специальное значение ['*'] — все поля.

    Returns: {ticker, name, years, ltm, data: {field: {year: value, ..., "LTM": value}}, ...}}
    """
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="financials")
    if not table:
        return {}

    # заголовок: имя компании
    h1 = soup.find("h1")
    title = _clean(h1.get_text()) if h1 else ""
    ticker_m = re.search(r"\((\w+)\)", title)
    ticker = ticker_m.group(1) if ticker_m else ""

    # годы из header_row
    years: list[str] = []
    header_row = table.find("tr", class_="header_row")
    has_ltm = False
    if header_row:
        for td in header_row.find_all("td"):
            cls = td.get("class", [])
            if "chartrow" in cls or "ltm_spc" in cls:
                continue
            text = _clean(td.get_text())
            if not text:
                continue
            if "editrow" in cls:
                years.append("LTM")
                has_ltm = True
            else:
                years.append(text.split()[0])

    want_fields: set[str] | None = None
    if fields and "*" not in fields:
        want_fields = set(fields)

    result: dict[str, Any] = {
        "ticker": ticker,
        "name": title,
        "years": years,
        "data": {},
    }

    for tr in table.find_all("tr"):
        field = tr.get("field")
        if not field:
            continue
        if want_fields is not None and field not in want_fields:
            continue
        if field not in _FINANCIAL_FIELDS and want_fields is None:
            continue

        cells = tr.find_all("td")
        # пропускаем chartrow (cols[0]) и ltm_spc (cols[-2])
        data_cells: list[Tag] = []
        for c in cells:
            cls = c.get("class", [])
            if "chartrow" in cls or "ltm_spc" in cls:
                continue
            data_cells.append(c)

        # data_cells[0..N-2] = годы, data_cells[-1] = LTM (если есть)
        values: dict[str, Any] = {}
        n_years = len(years)
        year_count = n_years - 1 if has_ltm else n_years
        for i, val in enumerate(data_cells):
            text = _clean(val.get_text())
            if not text:
                continue
            num = _parse_number(text)
            if i < year_count and i < len(years):
                values[years[i]] = num if num is not None else text
            elif has_ltm and i == len(data_cells) - 1:
                values["LTM"] = num if num is not None else text

        result["data"][field] = {
            "label": _FINANCIAL_FIELDS.get(field, field),
            "values": values,
        }

    return result


def get_company_financials(
    ticker: str,
    *,
    period: str = "y",
    standard: str = "MSFO",
    fields: list[str] | None = None,
) -> dict[str, Any]:
    """Детальный финансовый профиль компании со smart-lab.ru.

    Извлекает данные из таблицы финансовой отчётности (МСФО/РСБУ,
    годовые/квартальные). Один HTTP-запрос на компанию.

    Args:
        ticker: тикер акции ('SBER', 'LKOH', ...)
        period: 'y'=годовые, 'q'=квартальные
        standard: 'MSFO'=МСФО, 'RSBU'=РСБУ
        fields: список полей для извлечения. Полный список:
            p_e, p_s, p_b, ev_ebitda, eps, revenue, net_income, ebitda,
            operating_income, roe, roa, ebitda_margin, debt_ebitda,
            debt, net_debt, cash, assets, book_value, fcf, capex,
            dividend, div_yield, market_cap, ...
            None или ['*'] = все доступные.

    Returns: {ticker, name, years, data: {field: {label, values: {year: val, "LTM": val}}}}.
    """
    path = f"/q/{ticker.upper()}/f/{period}/{standard}/"
    html = _fetch(path)
    return parse_company_financials_table(html, fields=fields)


def get_company_financials_multi(
    tickers: list[str],
    *,
    period: str = "y",
    standard: str = "MSFO",
    fields: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Детальные финансовые профили нескольких компаний.

    Каждый тикер — отдельный HTTP-запрос (данные кэшируются на 4 ч).
    Ошибки по отдельным тикерам не прерывают обработку остальных.
    """
    results: list[dict[str, Any]] = []
    for ticker in tickers:
        try:
            data = get_company_financials(
                ticker, period=period, standard=standard, fields=fields,
            )
            results.append(data)
        except Exception:
            results.append({"ticker": ticker.upper(), "error": "failed to fetch"})
    return results
