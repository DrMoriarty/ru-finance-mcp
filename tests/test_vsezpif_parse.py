"""Парсеры vsezpif.py на локальных HTML-фикстурах (без сети)."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

import ru_finance.vsezpif as vsezpif
from ru_finance.vsezpif import (
    _parse_amount,
    _parse_date_iso,
    _parse_date_ru,
    _parse_fund_page,
    estimate_next_payment,
    parse_calendar,
)

# ────────────────────────── Фикстуры HTML ──────────────────────────

# Календарь выплат: >~DD.MM</span> ... <a href="/zpif-{slug}">Name</a> ... N ₽
CALENDAR_HTML = """
<div class="month">
  <span class="m-title">Март 2026</span><span>3 выплаты</span>
  <div class="row">
    <span class="day">~05.03</span>
    <span class="info"></span>
    <a href="/zpif-fund-alpha">ЗПИФ «Фонд Альфа»</a>
    <span class="sum">12,50 ₽</span>
  </div>
  <div class="row">
    <span class="day">~21.03</span>
    <span class="info"></span>
    <a href="/zpif-fund-beta">ЗПИФ «Фонд Бета»</a>
    <span class="sum">3 456,00 ₽</span>
  </div>
</div>
<div class="month">
  <span class="m-title">Апрель 2026</span><span>1 выплата</span>
  <div class="row">
    <span class="day">10.04</span>
    <span class="info"></span>
    <a href="/zpif-?month=4">Показать выплаты этого месяца</a>
    <span class="sum">100 ₽</span>
  </div>
  <div class="row">
    <span class="day">~15.04</span>
    <span class="info"></span>
    <a href="/zpif-fund-alpha">ЗПИФ «Фонд Альфа»</a>
    <span class="sum">12,50 ₽</span>
  </div>
</div>
"""

FUND_PAGE_HTML = """
<html>
  <head><title>ЗПИФ «Фонд Альфа» (ru-daily) — выплаты</title></head>
  <body>
    <div>isin:RU000A105QX7</div>
    <div>Биржевая цена пая: 1 050,50 ₽</div>
    <div>Доходность за 12 мес: 14,3 %</div>
    <div>Выплаты производятся ежемесячно</div>
    <script type="application/ld+json">
    [
      {
        "@type": "FAQPage",
        "mainEntity": [
          {
            "acceptedAnswer": {
              "text": "Выплаты: 01.02.2026 — 12,50 ₽, 01.03.2026 — 13,10 ₽."
            }
          }
        ]
      }
    ]
    </script>
  </body>
</html>
"""

# ────────────────────────── _parse_amount ──────────────────────────

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("34,00", 34.0),
        ("3 456", 3456.0),           # пробел-разделитель
        ("37.64", 37.64),            # точка как десятичный разделитель
        ("12,50", 12.5),
        ("1 050,50", 1050.5),
        ("12,50 руб.", None),        # буквенный мусор не парсится
        ("1 050,50 ₽", None),        # символ валюты не отрезается
        ("", None),
        ("-", None),
        ("—", None),
        ("abc", None),
    ],
)
def test_parse_amount(text, expected):
    assert _parse_amount(text) == expected


# ────────────────────────── даты ──────────────────────────

@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("05.03.2026", "05.03.2026"),      # валидная DD.MM.YYYY
        ("~05.03.2026", "05.03.2026"),     # с «прогнозным» тильда-префиксом
        ("  10.04.2026  ", "10.04.2026"),  # пробелы вокруг
        ("", None),
        ("-", None),
        ("—", None),
        ("\xa0", None),
        ("мусор", "мусор"),  # непустой мусор возвращается как есть
    ],
)
def test_parse_date_ru(text, expected):
    assert _parse_date_ru(text) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("05.03.2026", "2026-03-05"),
        ("31.12.2027", "2027-12-31"),
        ("~15.04.2026", "2026-04-15"),
        ("05.03.26", None),      # двухзначный год не в формате
        ("2026-03-05", None),    # ISO-вход не принимается
        ("мусор", None),
        ("", None),
        ("-", None),
    ],
)
def test_parse_date_iso(text, expected):
    assert _parse_date_iso(text) == expected


# ────────────────────────── parse_calendar ──────────────────────────

def test_parse_calendar_entries():
    rows = parse_calendar(CALENDAR_HTML, year=2026)
    # запись «Показать выплаты этого месяца» отфильтрована
    assert len(rows) == 3

    first = rows[0]
    assert first["date"] == "05.03.2026"
    assert first["date_iso"] == "2026-03-05"
    assert first["fund_name"] == "ЗПИФ «Фонд Альфа»"
    assert first["slug"] == "/zpif-fund-alpha"
    assert first["amount"] == 12.5

    second = rows[1]
    assert second["fund_name"] == "ЗПИФ «Фонд Бета»"
    assert second["date_iso"] == "2026-03-21"
    assert second["amount"] == 3456.0

    # вторая выплата того же фонда — апрель
    third = rows[2]
    assert third["slug"] == "/zpif-fund-alpha"
    assert third["date_iso"] == "2026-04-15"


def test_parse_calendar_year_filter():
    rows = parse_calendar(CALENDAR_HTML, year=2027)
    assert all(r["date_iso"].startswith("2027") for r in rows)
    assert all(r["date"].endswith(".2027") for r in rows)


def test_parse_calendar_default_year_is_current():
    rows = parse_calendar(CALENDAR_HTML)
    assert all(r["date_iso"].startswith(str(date.today().year)) for r in rows)


def test_parse_calendar_empty_html():
    assert parse_calendar("") == []
    assert parse_calendar("<div>пусто</div>") == []


# ────────────────────────── _parse_fund_page ──────────────────────────

def test_parse_fund_page_full():
    data = _parse_fund_page(FUND_PAGE_HTML)
    assert data["isin"] == "RU000A105QX7"
    assert data["last_price"] == 1050.5
    assert data["yield_pct"] == 14.3
    # квирк: «ежемесячно» не содержит «месяц» (нет буквы «ц»),
    # поэтому любая периодичность классифицируется как quarterly
    assert data["frequency"] == "quarterly"
    # история выплат из JSON-LD FAQ
    assert data["manual_payments"] == [
        {"date": "01.02.2026", "amount": 12.5},
        {"date": "01.03.2026", "amount": 13.1},
    ]


def test_parse_fund_page_quarterly_frequency():
    html = "<div>Выплаты производятся поквартально</div>"
    data = _parse_fund_page(html)
    assert data["frequency"] == "quarterly"


def test_parse_fund_page_missing_fields():
    # пустая страница — ни одно поле не найдено, но не падает
    assert _parse_fund_page("") == {}
    assert _parse_fund_page("<html><body>ничего нет</body></html>") == {}


def test_parse_fund_page_broken_jsonld():
    # битый JSON-LD не должен ронять парсинг остальных полей
    html = (
        '<div>Биржевая цена пая: 100,50 ₽</div>'
        '<script type="application/ld+json">{broken json</script>'
    )
    data = _parse_fund_page(html)
    assert data["last_price"] == 100.5
    assert "manual_payments" not in data


# ────────────────────────── estimate_next_payment ──────────────────────────

def _quarterly_payments(fund_name: str) -> list[dict]:
    """Синтетическая история с квартальной периодичностью (шаг 90 дней).

    Последняя выплата — в будущем, ближайшая — через 90 дней от сегодня.
    """
    today = date.today()
    return [
        {
            "date": (today - timedelta(days=90)).strftime("%d.%m.%Y"),
            "date_iso": (today - timedelta(days=90)).isoformat(),
            "fund_name": fund_name,
            "slug": "/zpif-fund-alpha",
            "amount": 12.5,
        },
        {
            "date": today.strftime("%d.%m.%Y"),
            "date_iso": today.isoformat(),
            "fund_name": fund_name,
            "slug": "/zpif-fund-alpha",
            "amount": 12.5,
        },
        {
            "date": (today + timedelta(days=90)).strftime("%d.%m.%Y"),
            "date_iso": (today + timedelta(days=90)).isoformat(),
            "fund_name": fund_name,
            "slug": "/zpif-fund-alpha",
            "amount": 12.5,
        },
    ]


def test_estimate_next_payment_follows_periodicity(monkeypatch):
    fund_name = "ЗПИФ «Фонд Альфа»"
    payments = _quarterly_payments(fund_name)
    monkeypatch.setattr(vsezpif, "get_payment_calendar", lambda limit=100: payments)

    result = estimate_next_payment(fund_name=fund_name)
    assert result is not None

    # прогнозная дата — ближайшая будущая выплата календаря
    # (выплата «на сегодня» тоже считается будущей)
    expected = date.today().isoformat()
    assert result["date_iso"] == expected

    # прогноз следует квартальной периодичности: шаг от предыдущей = 90 дней
    idx = payments.index(result)
    prev = date.fromisoformat(payments[idx - 1]["date_iso"])
    next_date = date.fromisoformat(result["date_iso"])
    assert (next_date - prev).days == 90


def test_estimate_next_payment_all_past(monkeypatch):
    fund_name = "ЗПИФ «Фонд Альфа»"
    today = date.today()
    payments = [
        {
            "date": (today - timedelta(days=180)).strftime("%d.%m.%Y"),
            "date_iso": (today - timedelta(days=180)).isoformat(),
            "fund_name": fund_name,
            "slug": "/zpif-fund-alpha",
            "amount": 12.5,
        },
        {
            "date": (today - timedelta(days=90)).strftime("%d.%m.%Y"),
            "date_iso": (today - timedelta(days=90)).isoformat(),
            "fund_name": fund_name,
            "slug": "/zpif-fund-alpha",
            "amount": 12.5,
        },
    ]
    monkeypatch.setattr(vsezpif, "get_payment_calendar", lambda limit=100: payments)

    result = estimate_next_payment(fund_name=fund_name)
    # все выплаты в прошлом — возвращается последняя из истории
    assert result["date_iso"] == (today - timedelta(days=90)).isoformat()


def test_estimate_next_payment_no_payments(monkeypatch):
    monkeypatch.setattr(vsezpif, "get_payment_calendar", lambda limit=100: [])
    assert estimate_next_payment(fund_name="Такого фонда нет") is None
