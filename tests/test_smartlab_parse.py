"""Парсеры smartlab.py на локальных HTML-фикстурах (без сети)."""
from __future__ import annotations

import pytest
from bs4 import BeautifulSoup

from ru_finance.smartlab import (
    _parse_date,
    _parse_number,
    _parse_screener_row,
    parse_company_financials_table,
    parse_dividend_history_table,
    parse_dividends_table,
    parse_screener_table,
)

# ────────────────────────── Фикстуры HTML ──────────────────────────

# Календарь дивидендов /dividends/: Название, Тикер, Период, Дивиденд руб,
# Див. Дох., СД, Купить До, Дата закрытия реестра, Выплата До, Цена акции.
DIVIDENDS_HTML = """
<table>
  <tr>
    <th>Название</th><th>Тикер</th><th>Период</th><th>Дивиденд руб</th>
    <th>Див. Дох.</th><th>СД</th><th>Купить До</th>
    <th>Дата закрытия реестра</th><th>Выплата До</th><th>Цена акции</th>
  </tr>
  <tr>
    <td>Сбербанк</td><td>SBER</td><td>II кв 2026</td><td>37,64</td>
    <td>10,5%</td><td>да</td><td>18.07.2026</td>
    <td>18.09.2026</td><td>05.11.2026</td><td>358,20₽</td>
  </tr>
  <tr>
    <td>ЛУКОЙЛ</td><td>LKOH</td><td>I кв 2026</td><td>—</td>
    <td>-</td><td></td><td>25.03.2026</td>
    <td>26.03.2026</td><td>—</td><td>6 900,00</td>
  </tr>
</table>
"""

# Тот же календарь, но вторая строка обрезана (9 ячеек вместо 10)
DIVIDENDS_SHORT_ROW_HTML = """
<table>
  <tr><th>Название</th><th>Тикер</th><th>Период</th><th>Дивиденд руб</th>
      <th>Див. Дох.</th><th>СД</th><th>Купить До</th>
      <th>Дата закрытия реестра</th><th>Выплата До</th><th>Цена акции</th></tr>
  <tr><td>Сбербанк</td><td>SBER</td><td>II кв 2026</td><td>37,64</td>
      <td>10,5%</td><td>да</td><td>18.07.2026</td>
      <td>18.09.2026</td><td>05.11.2026</td></tr>
</table>
"""

# Выплатная таблица /q/{ticker}/dividend/ (шапка из <th> + разделитель)
HISTORY_TABLE_HTML = """
<table>
  <tr>
    <th>Тикер</th><th>дата закр. T-1</th><th>дата отсечки</th><th>Период</th>
    <th>дивиденд, руб</th><th>цена акции</th><th>Див.доходность</th>
  </tr>
  <tr><td colspan="7">Ожидаемые</td></tr>
  <tr>
    <td>SBER</td><td>17.09.2026</td><td>18.09.2026</td><td>II кв 2026</td>
    <td>37,64</td><td>358,20</td><td>10,50%</td>
  </tr>
  <tr><td colspan="7">Выплаченные</td></tr>
  <tr>
    <td>SBER</td><td>—</td><td>10.07.2025</td><td>I кв 2025</td>
    <td>16,00</td><td>315,40</td><td>5,07%</td>
  </tr>
</table>
"""

# Фундаментальный скринер shares_fundamental2 (19 колонок, шапка из <th>)
SCREENER_HTML = """
<table class="trades-table">
  <tr>
    <th>№</th><th>Название компании</th><th>Тикер</th><th>график</th>
    <th>отчёты</th><th>Капитализация</th><th>EV</th><th>Выручка</th>
    <th>Чистая прибыль</th><th>ДД ао</th><th>ДД ап</th><th>ДД/ЧП</th>
    <th>P/E</th><th>P/S</th><th>P/B</th><th>EV/EBITDA</th>
    <th>Рентаб. EBITDA</th><th>Долг/EBITDA</th><th>Тип отчёта</th>
  </tr>
  <tr>
    <td>1</td><td>Сбербанк</td><td>SBER</td><td><img></td><td><img></td>
    <td>6 500</td><td>7 200</td><td>2 900</td><td>1 200</td>
    <td>10,5%</td><td>11,2%</td><td>50%</td>
    <td>5,4</td><td>2,2</td><td>1,1</td><td>4,0</td>
    <td>45%</td><td>2,5</td><td>LTM-МСФО</td>
  </tr>
  <tr>
    <td>2</td><td>Газпром</td><td>GAZP</td><td><img></td><td><img></td>
    <td>3 800</td><td>5 100</td><td>8 200</td><td>-150</td>
    <td>12,2%</td><td>-</td><td>—</td>
    <td>-25,3</td><td>0,5</td><td>0,4</td><td>6,1</td>
    <td>18%</td><td>2,8</td><td>LTM-МСФО</td>
  </tr>
</table>
"""

# Строка скринера короче минимума (_SC_MIN_COLS = 16)
SCREENER_SHORT_ROW = "<table><tr><td>1</td><td>X</td><td>XTICK</td></tr></table>"


def _first_table(html: str):
    return BeautifulSoup(html, "lxml").find("table")


# ────────────────────────── _parse_number ──────────────────────────

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("110", 110.0),
        ("3 456", 3456.0),          # пробел-разделитель тысяч
        ("37,64₽", 37.64),          # запятая + символ валюты
        ("1,4%", 1.4),              # процент
        ("-25,3", -25.3),           # отрицательное
        ("358,20 ", 358.2),         # хвостовой пробел
        ("-", None),                # прочерк
        ("—", None),                # em-dash
        ("", None),                 # пусто
        ("abc", None),              # мусор
    ],
)
def test_parse_number(text, expected):
    assert _parse_number(text) == expected


# ────────────────────────── _parse_date ──────────────────────────

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("18.09.2026", "18.09.2026"),   # стандартный формат — как есть
        ("  05.03.2027 ", "05.03.2027"),  # пробелы вокруг
        ("18.09.2026*", "18.09.2026*"),  # нестандартный формат — не чинится
        ("-", None),
        ("—", None),
        ("", None),
        ("\xa0", None),
    ],
)
def test_parse_date(text, expected):
    assert _parse_date(text) == expected


# ────────────────────────── parse_dividends_table ──────────────────────────

def test_parse_dividends_table_real_fragment():
    rows = parse_dividends_table(DIVIDENDS_HTML)
    assert len(rows) == 2

    sber, lkoh = rows
    assert sber["name"] == "Сбербанк"
    assert sber["ticker"] == "SBER"
    assert sber["period"] == "II кв 2026"
    assert sber["dividend_rub"] == 37.64
    assert sber["yield_pct"] == 10.5
    assert sber["board_approved"] is True
    assert sber["last_buy_date"] == "18.07.2026"
    assert sber["close_date"] == "18.09.2026"
    assert sber["payment_date"] == "05.11.2026"
    assert sber["price"] == 358.2

    # прочерки/пустые ячейки → None
    assert lkoh["dividend_rub"] is None
    assert lkoh["yield_pct"] is None
    assert lkoh["board_approved"] is False
    assert lkoh["payment_date"] is None
    assert lkoh["price"] == 6900.0


def test_parse_dividends_table_empty():
    assert parse_dividends_table("") == []
    assert parse_dividends_table("<div>нет таблицы</div>") == []
    # только шапка — данных нет
    header_only = DIVIDENDS_HTML.split("<tr>")[0] + "</table>"
    assert parse_dividends_table(header_only) == []


def test_parse_dividends_table_missing_column():
    # строка с 9 ячейками вместо 10 пропускается
    rows = parse_dividends_table(DIVIDENDS_SHORT_ROW_HTML)
    assert rows == []


# ────────────────────────── parse_dividend_history_table ──────────────────────────

def test_parse_dividend_history_table():
    rows = parse_dividend_history_table(_first_table(HISTORY_TABLE_HTML))
    assert len(rows) == 2

    future, paid = rows
    assert future["ticker"] == "SBER"
    assert future["date_t1"] == "17.09.2026"
    assert future["cutoff_date"] == "18.09.2026"
    assert future["period"] == "II кв 2026"
    assert future["dividend_rub"] == 37.64
    assert future["price"] == 358.2
    assert future["yield_pct"] == 10.5

    # строка-разделитель («Выплаченные») не попала в результат,
    # прочерки в датах → None
    assert paid["cutoff_date"] == "10.07.2025"
    assert paid["date_t1"] is None
    assert paid["dividend_rub"] == 16.0


def test_parse_dividend_history_table_no_header():
    # без строки-шапки (все <th>) — пустой результат
    html = """
    <table>
      <tr><td>SBER</td><td>17.09.2026</td><td>18.09.2026</td>
          <td>II кв 2026</td><td>37,64</td><td>358,20</td><td>10,50%</td></tr>
    </table>
    """
    assert parse_dividend_history_table(_first_table(html)) == []


def test_parse_dividend_history_table_empty():
    assert parse_dividend_history_table(_first_table("<table></table>")) == []


# ────────────────────────── скринер ──────────────────────────

def test_parse_screener_table():
    rows = parse_screener_table(SCREENER_HTML)
    assert [r["ticker"] for r in rows] == ["SBER", "GAZP"]

    sber, gazp = rows
    assert sber["name"] == "Сбербанк"
    assert sber["market_cap"] == 6500.0
    assert sber["ev"] == 7200.0
    assert sber["revenue"] == 2900.0
    assert sber["net_income"] == 1200.0
    assert sber["div_yield"] == 10.5
    assert sber["p_e"] == 5.4
    assert sber["ev_ebitda"] == 4.0
    assert sber["report_type"] == "LTM-МСФО"

    # прочерки → None, отрицательные числа парсятся
    assert gazp["div_yield_priv"] is None
    assert gazp["div_payout_ratio"] is None
    assert gazp["p_e"] == -25.3
    assert gazp["net_income"] == -150.0


def test_parse_screener_table_header_row_not_data():
    # шапка (первая ячейка <th>) не попадает в данные
    rows = parse_screener_table(SCREENER_HTML)
    assert "Тикер" not in {r["ticker"] for r in rows}
    assert "Название" not in {r["name"] for r in rows}


def test_parse_screener_table_empty():
    assert parse_screener_table("") == []
    assert parse_screener_table("<table class='other'><tr><td>x</td></tr></table>") == []


def test_parse_screener_row_short_row():
    # меньше _SC_MIN_COLS (16) ячеек → None
    cells = _first_table(SCREENER_SHORT_ROW).find("tr").find_all(["td", "th"])
    assert _parse_screener_row(cells) is None


def test_parse_screener_row_empty_ticker():
    # пустая ячейка тикера → None
    html = (
        "<table><tr>"
        + "<td>0</td><td>1</td>"
        + "<td></td>"  # колонка тикера пуста
        + "".join(f"<td>{i}</td>" for i in range(3, 16))
        + "</tr></table>"
    )
    cells = _first_table(html).find("tr").find_all(["td", "th"])
    assert len(cells) == 16
    assert _parse_screener_row(cells) is None


# ────────────────────────── parse_company_financials_table ──────────────────────────

FINANCIALS_HTML = """
<html>
  <h1>Сбербанк (SBER) — финансовая отчётность</h1>
  <table class="financials">
    <tr class="header_row">
      <td class="chartrow"></td>
      <td>2023</td>
      <td>2024</td>
      <td class="editrow">ред.</td>
      <td class="ltm_spc"></td>
    </tr>
    <tr field="p_e">
      <td class="chartrow"></td>
      <td>5,4</td>
      <td>4,8</td>
      <td>5,1</td>
      <td class="ltm_spc"></td>
    </tr>
    <tr field="revenue">
      <td class="chartrow"></td>
      <td>2 657</td>
      <td>3 102</td>
      <td>3 200</td>
      <td class="ltm_spc"></td>
    </tr>
    <tr field="unknown_metric">
      <td class="chartrow"></td>
      <td>1</td>
      <td>2</td>
      <td>3</td>
      <td class="ltm_spc"></td>
    </tr>
  </table>
</html>
"""


def test_parse_company_financials_table():
    result = parse_company_financials_table(FINANCIALS_HTML)
    assert result["ticker"] == "SBER"
    assert result["name"] == "Сбербанк (SBER) — финансовая отчётность"
    # editrow в шапке → колонка LTM
    assert result["years"] == ["2023", "2024", "LTM"]

    pe = result["data"]["p_e"]
    assert pe["label"] == "P/E"
    assert pe["values"] == {"2023": 5.4, "2024": 4.8, "LTM": 5.1}

    rev = result["data"]["revenue"]
    assert rev["values"] == {"2023": 2657.0, "2024": 3102.0, "LTM": 3200.0}

    # неизвестное поле по умолчанию отбрасывается
    assert "unknown_metric" not in result["data"]


def test_parse_company_financials_table_fields_filter():
    result = parse_company_financials_table(FINANCIALS_HTML, fields=["p_e"])
    assert set(result["data"]) == {"p_e"}
    assert result["data"]["p_e"]["values"]["2024"] == 4.8


def test_parse_company_financials_table_all_fields_star():
    result = parse_company_financials_table(FINANCIALS_HTML, fields=["*"])
    # '*' = все известные поля без фильтра по списку
    assert {"p_e", "revenue"} <= set(result["data"])
    # неизвестные field-атрибуты всё равно отбрасываются
    assert "unknown_metric" not in result["data"]


def test_parse_company_financials_table_missing_table():
    assert parse_company_financials_table("") == {}
    assert parse_company_financials_table("<div>нет таблицы</div>") == {}
