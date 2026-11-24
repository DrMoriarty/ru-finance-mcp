"""Модуль ru_finance/cbr.py на локальных фикстурах (без сети).

Тестируются чистые вспомогательные функции (_parse_ru_float, _ser, _df) и
парсинг инфляции в _fetch_inflation — через подмену requests.get локальной
HTML-фикстурой (сеть не дёргается). HTTP-функции (key_rate, ruonia, currency
и т.д.) — тонкие обёртки над cbrapi, без моков не тестируются и здесь не
трогаются.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import ru_finance.cbr as cbr
from ru_finance.cbr import _df, _fetch_inflation, _parse_ru_float, _ser

FIXTURES = Path(__file__).parent / "fixtures"


# ────────────────────────── _parse_ru_float ──────────────────────────

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("16,5", 16.5),
        ("5,98", 5.98),
        (" 5,98 ", 5.98),        # пробелы по краям отрезаются
        ("16.00", 16.0),         # точка как десятичный разделитель
        ("-3,25", -3.25),        # обычный минус
        ("−3,25", -3.25),        # минус U+2212 из таблиц ЦБ
        ("–3,25", -3.25),        # en dash
        ("42", 42.0),
        ("", None),
        ("   ", None),
        ("−", None),             # прочерк «нет значения»
        ("–", None),
        ("-", None),
        ("1 234,56", None),      # пробел-разделитель тысяч не поддержан
        (" 1 234,56 ", None),
        ("abc", None),
        ("12,3,4", None),
        ("16%", None),
    ],
)
def test_parse_ru_float(text, expected):
    assert _parse_ru_float(text) == expected


# ────────────────────────── _ser ──────────────────────────

def _daily_series(values, start="2024-06-03", freq="D"):
    idx = pd.date_range(start, periods=len(values), freq=freq)
    return pd.Series(values, index=idx, dtype=float)


def test_ser_basic_tail_and_latest():
    s = _daily_series([10.0, 11.5, 12.0, 13.5, 14.0])
    out = _ser(s, tail=3)
    assert out["latest"] == 14.0
    assert out["latest_date"] == "2024-06-07"
    # series обрезан до последних tail точек
    assert out["series"] == [
        {"date": "2024-06-05", "value": 12.0},
        {"date": "2024-06-06", "value": 13.5},
        {"date": "2024-06-07", "value": 14.0},
    ]


def test_ser_tail_larger_than_series():
    s = _daily_series([1.0, 2.0])
    out = _ser(s, tail=10)
    assert out["latest"] == 2.0
    assert [p["value"] for p in out["series"]] == [1.0, 2.0]


def test_ser_drops_nan_and_latest_skips_it():
    # NaN внутри и в конце: дропаются, latest — последнее валидное значение
    s = _daily_series([5.0, np.nan, 7.0, np.nan])
    out = _ser(s, tail=10)
    assert out["latest"] == 7.0
    assert out["latest_date"] == "2024-06-05"
    assert out["series"] == [
        {"date": "2024-06-03", "value": 5.0},
        {"date": "2024-06-05", "value": 7.0},
    ]


def test_ser_all_nan():
    s = _daily_series([np.nan, np.nan])
    assert _ser(s, tail=5) == {"latest": None, "latest_date": None, "series": []}


def test_ser_empty():
    assert _ser(pd.Series(dtype=float), tail=5) == {
        "latest": None, "latest_date": None, "series": [],
    }


def test_ser_non_datetime_index():
    # у целочисленного индекса нет .date — остаётся как есть (str(i))
    s = pd.Series([1.5, 2.5], index=[0, 1], dtype=float)
    out = _ser(s, tail=2)
    assert out["latest"] == 2.5
    assert out["latest_date"] == "1"
    assert out["series"] == [{"date": "0", "value": 1.5}, {"date": "1", "value": 2.5}]


def test_ser_period_index():
    # cbrapi отдаёт Period-индексы (месяц); у Period нет .date — берётся str()
    s = pd.Series([16.0, 15.0], index=pd.PeriodIndex(["2024-01", "2024-02"], freq="M"))
    out = _ser(s, tail=2)
    assert out["latest"] == 15.0
    assert out["latest_date"] == "2024-02"
    assert out["series"] == [
        {"date": "2024-01", "value": 16.0},
        {"date": "2024-02", "value": 15.0},
    ]


# ────────────────────────── _df ──────────────────────────

def _ruonia_df():
    idx = pd.PeriodIndex(["2024-01", "2024-02", "2024-03"], freq="M")
    return pd.DataFrame(
        {"RUONIA_INDEX": [100.0, 100.5, np.nan],
         "RUONIA_AVG_1M": [14.1, 13.8, 13.5]},
        index=idx,
    )


def test_df_basic_tail_and_latest():
    out = _df(_ruonia_df(), tail=2)
    assert out["latest_date"] == "2024-03"
    # NaN-ячейки последней строки в latest не попадают
    assert out["latest"] == {"RUONIA_AVG_1M": 13.5}
    # индекс превращён в строки, добавлена колонка date
    assert [r["date"] for r in out["rows"]] == ["2024-02", "2024-03"]
    assert out["rows"][0] == {
        "date": "2024-02", "RUONIA_INDEX": 100.5, "RUONIA_AVG_1M": 13.8,
    }


def test_df_tail_truncation():
    out = _df(_ruonia_df(), tail=1)
    assert [r["date"] for r in out["rows"]] == ["2024-03"]
    assert out["latest"] == {"RUONIA_AVG_1M": 13.5}


def test_df_empty():
    out = _df(pd.DataFrame(), tail=5)
    assert out == {"latest": {}, "latest_date": None, "rows": []}


def test_df_all_nan_last_row():
    df = pd.DataFrame(
        {"a": [1.0, np.nan], "b": [2.0, np.nan]},
        index=pd.to_datetime(["2024-01-31", "2024-02-29"]),
    )
    out = _df(df, tail=2)
    assert out["latest"] == {}
    assert out["latest_date"] == "2024-02-29"
    assert len(out["rows"]) == 2


def test_df_non_string_columns():
    df = pd.DataFrame({0: [1.0], 1: [2.0]}, index=pd.DatetimeIndex(["2024-03-01"]))
    out = _df(df, tail=1)
    assert out["latest"] == {"0": 1.0, "1": 2.0}
    assert list(out["rows"][0]) == ["date", "0", "1"]


# ────────────────────────── _fetch_inflation ──────────────────────────
# Парсинг вплетён в fetch (requests.get → BeautifulSoup → разбор строк в одном
# теле функции), чистую функцию «HTML → rows» выделить нельзя — см. отчёт.
# Тестируем доступную часть: подменяем requests.get локальной фикстурой.

class _FakeResponse:
    def __init__(self, text: str):
        self.text = text

    def raise_for_status(self):
        pass


@pytest.fixture(autouse=True)
def _clear_infl_cache():
    cbr._INFL_CACHE.clear()
    yield
    cbr._INFL_CACHE.clear()


@pytest.fixture
def infl_html() -> str:
    return (FIXTURES / "cbr_inflation.html").read_text(encoding="utf-8")


def _patch_get(monkeypatch, html, calls):
    def fake_get(url, params=None, headers=None, timeout=None):
        calls.append((url, params))
        return _FakeResponse(html)

    monkeypatch.setattr(cbr.requests, "get", fake_get)


def test_fetch_inflation_parses_fixture(monkeypatch, infl_html):
    calls = []
    _patch_get(monkeypatch, infl_html, calls)

    rows = _fetch_inflation("2023-04-01", "2024-03-31")

    # URL и параметры запроса — cbr.ru ждёт DD.MM.YYYY
    assert len(calls) == 1
    url, params = calls[0]
    assert url == cbr._INFL_URL
    assert params["UniDbQuery.Posted"] == "True"
    assert params["UniDbQuery.From"] == "01.04.2023"
    assert params["UniDbQuery.To"] == "31.03.2024"

    # шапка (th) и строка «Итого» отфильтрованы, 12 месяцев
    assert len(rows) == 12
    # пришло по убыванию — отсортировано по возрастанию
    assert rows[0]["date"] == "2023-04"
    assert rows[-1]["date"] == "2024-03"
    assert all(rows[i]["date"] < rows[i + 1]["date"] for i in range(len(rows) - 1))

    last = rows[-1]
    assert last == {"date": "2024-03", "key_rate": 16.0,
                    "inflation_yoy": 7.7, "inflation_target": 4.0}

    # «−» в ключевой ставке превращается в None
    jun = next(r for r in rows if r["date"] == "2023-06")
    assert jun == {"date": "2023-06", "key_rate": None,
                   "inflation_yoy": 3.25, "inflation_target": 4.0}


def test_fetch_inflation_month_only_dates(monkeypatch, infl_html):
    # YYYY-MM разворачивается в последний день месяца
    calls = []
    _patch_get(monkeypatch, infl_html, calls)
    _fetch_inflation("2023-04", "2023-06")
    _, params = calls[0]
    assert params["UniDbQuery.From"] == "30.04.2023"
    assert params["UniDbQuery.To"] == "30.06.2023"


def test_fetch_inflation_cache_hit(monkeypatch, infl_html):
    calls = []
    _patch_get(monkeypatch, infl_html, calls)

    first = _fetch_inflation("2023-04-01", "2024-03-31")
    second = _fetch_inflation("2023-04-01", "2024-03-31")
    # второй вызов из кэша — HTTP ровно один
    assert len(calls) == 1
    assert second == first

    # другой диапазон — новый ключ кэша, снова HTTP
    _fetch_inflation("2024-01-01", "2024-03-31")
    assert len(calls) == 2


def test_fetch_inflation_no_table(monkeypatch):
    calls = []
    _patch_get(monkeypatch, "<html><body>таблицы нет</body></html>", calls)
    assert _fetch_inflation("2023-04-01", "2024-03-31") == []
