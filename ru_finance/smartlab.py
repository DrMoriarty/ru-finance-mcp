"""Данные по дивидендам со smart-lab.ru (календарь + история).

ISS-эндпоинт дивидендов (`/securities/{secid}/dividends`) не возвращает данные,
а «правильный» эндпоинт закрыт пейволом. Поэтому берём календарь и историю
дивидендов с публичного портала smart-lab.ru (скрейпинг серверных HTML-таблиц),
как сделано в mcp-smartlab.

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
