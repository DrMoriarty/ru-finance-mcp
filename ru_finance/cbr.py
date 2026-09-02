"""Обёртки над cbrapi -> JSON-дружелюбные структуры.

cbrapi возвращает pandas Series/DataFrame с Period/Timestamp-индексом; здесь
приводим к спискам записей со строковыми датами и достаём «последнее значение».
Данные по инфляции (CPI) парсятся из HTML-таблицы cbr.ru/hd_base/infl/.
"""
from __future__ import annotations

import calendar
import re
import time

import cbrapi
import pandas as pd
import requests
from bs4 import BeautifulSoup


def _ser(s: pd.Series, tail: int) -> dict:
    s = s.dropna()
    if s.empty:
        return {"latest": None, "latest_date": None, "series": []}
    series = [{"date": str(getattr(i, "date", lambda: i)()), "value": float(v)}
              for i, v in s.tail(tail).items()]
    return {"latest": float(s.iloc[-1]),
            "latest_date": str(getattr(s.index[-1], "date", lambda: s.index[-1])()),
            "series": series}


def _df(df: pd.DataFrame, tail: int) -> dict:
    df = df.tail(tail).copy()
    df.index = [str(getattr(i, "date", lambda: i)()) for i in df.index]
    df.columns = [str(c) for c in df.columns]
    latest = {k: v for k, v in df.iloc[-1].to_dict().items() if pd.notna(v)} if len(df) else {}
    return {"latest": latest,
            "latest_date": df.index[-1] if len(df) else None,
            "rows": df.reset_index(names="date").to_dict("records")}


def key_rate(first_date: str | None = None, last_date: str | None = None,
             tail: int = 30) -> dict:
    """Ключевая ставка ЦБ РФ (главный драйвер рынка облигаций и рубля)."""
    return _ser(cbrapi.get_key_rate(first_date, last_date), tail)


def ruonia(first_date: str | None = None, last_date: str | None = None,
           tail: int = 30) -> dict:
    """RUONIA overnight (% годовых) — ставка денежного рынка.

    cbrapi отдаёт overnight долей (0.1412) — приводим к процентам (×100),
    чтобы единицы совпадали с key_rate.
    """
    return _ser(cbrapi.get_ruonia_overnight(first_date, last_date) * 100, tail)


def ruonia_index(first_date: str | None = None, last_date: str | None = None,
                 tail: int = 12) -> dict:
    """RUONIA-индекс + срочные средние RUONIA_AVG_1M/3M/6M (% годовых).

    Короткая кривая ставок денежного рынка (живая замена прекращённому ROISfix).
    AVG-колонки уже в процентах; RUONIA_INDEX — уровень индекса (не ставка).
    """
    return _df(cbrapi.get_ruonia_index(first_date, last_date), tail)


def ibor(first_date: str | None = None, last_date: str | None = None,
         tail: int = 12) -> dict:
    """MIACR — фактические средневзвешенные ставки межбанка (MBK).

    MosPrime/MIBOR/MIBID прекращены (пустые колонки отфильтрованы);
    актуальны MIACR по срокам D1/D7/...
    """
    df = cbrapi.get_ibor(first_date, last_date).dropna(axis=1, how="all")
    return _df(df, tail)


def currency(symbol: str, first_date: str, last_date: str, tail: int = 30) -> dict:
    """Курс валюты ЦБ к рублю. symbol — тикер валюты, напр. 'USD', 'EUR', 'CNY'."""
    return _ser(cbrapi.get_time_series(symbol, first_date, last_date), tail)


def metals(first_date: str | None = None, last_date: str | None = None,
           tail: int = 12) -> dict:
    """Учётные цены ЦБ на драгметаллы (золото/серебро/платина/палладий)."""
    return _df(cbrapi.get_metals_prices(first_date, last_date), tail)


def reserves(first_date: str | None = None, last_date: str | None = None,
             tail: int = 12) -> dict:
    """Международные (золотовалютные) резервы РФ (ЗВР)."""
    return _df(cbrapi.get_mrrf(first_date, last_date), tail)


# ─────────────── Инфляция (CPI) со страницы cbr.ru ───────────────
_INFL_URL = "https://www.cbr.ru/hd_base/infl/"
_INFL_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/136.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ru-RU,ru;q=0.9",
}
_INFL_CACHE: dict[str, tuple[float, list[dict]]] = {}


def _parse_ru_float(text: str) -> float | None:
    """'5,98' -> 5.98, '−' -> None."""
    t = text.strip().replace(",", ".").replace("−", "-").replace("–", "-")
    if not t or t == "-":
        return None
    try:
        return float(t)
    except ValueError:
        return None


def _fetch_inflation(first_date: str | None, last_date: str | None) -> list[dict]:
    """Скачать и распарсить таблицу «Инфляция и ключевая ставка» с cbr.ru.

    Параметры from/to фильтруют данные на стороне cbr.ru (YYYY-MM-DD или YYYY-MM).
    HTML-таблица приходит отсортированной по убыванию — отсортируем по возрастанию.
    Кэш по ключу (first_date, last_date) на 4 ч.
    """
    cache_key = f"{first_date or ''}:{last_date or ''}"
    now = time.monotonic()
    if cache_key in _INFL_CACHE:
        ts, data = _INFL_CACHE[cache_key]
        if now - ts < 240 * 60:
            return data

    # cbr.ru ждёт DD.MM.YYYY
    def _to_cbr(d: str) -> str:
        parts = d[:10].split("-")
        if len(parts) == 3:
            return f"{parts[2]}.{parts[1]}.{parts[0]}"
        if len(parts) == 2:
            y, m = int(parts[0]), int(parts[1])
            last_day = calendar.monthrange(y, m)[1]
            return f"{last_day:02d}.{parts[1]}.{parts[0]}"
        return d

    # Defaults: last 5 full years + YTD
    from datetime import date as _date
    today = _date.today()
    default_from = f"{today.year - 5}-01-01"
    default_to = today.isoformat()

    params: dict[str, str] = {"UniDbQuery.Posted": "True"}
    params["UniDbQuery.From"] = _to_cbr(first_date or default_from)
    params["UniDbQuery.To"] = _to_cbr(last_date or default_to)

    resp = requests.get(_INFL_URL, params=params, headers=_INFL_HEADERS, timeout=30)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "lxml")
    table = soup.find("table")
    if not table:
        return []

    rows_data: list[dict] = []
    for tr in table.find_all("tr"):
        cells = tr.find_all("td")
        if len(cells) < 4:
            continue
        raw = re.sub(r"\s+", "", cells[0].get_text())
        parts = raw.split(".")
        if len(parts) != 2:
            continue
        month, year = parts
        rows_data.append({
            "date": f"{year}-{month}",
            "key_rate": _parse_ru_float(cells[1].get_text()),
            "inflation_yoy": _parse_ru_float(cells[2].get_text()),
            "inflation_target": _parse_ru_float(cells[3].get_text()),
        })

    rows_data.sort(key=lambda r: r["date"])
    _INFL_CACHE[cache_key] = (now, rows_data)
    return rows_data


def inflation(first_date: str | None = None, last_date: str | None = None,
              tail: int = 24) -> dict:
    """Инфляция (CPI, % г/г) и ключевая ставка ЦБ РФ (помесячно, с 2013 г.).

    Источник — HTML-таблица cbr.ru/hd_base/infl/ (со скрейпингом + URL-параметрами
    для выбора диапазона дат). Кэш — 4 ч.
    first_date/last_date — 'YYYY-MM' или 'YYYY-MM-DD', фильтрация по началу/концу
    интервала (включительно, значение по умолч.: 5 лет назад / сегодня).
    tail — последние N записей (по умолчанию 24 мес).
    Возврат: {latest_inflation, latest_key_rate, latest_inflation_target, latest_date,
    series: [{date, key_rate, inflation_yoy, inflation_target}, ...]}.
    inflation_yoy — годовая инфляция Росстат (% г/г). inflation_target — цель ЦБ РФ (%).
    Для расчёта реальной доходности облигаций и портфеля.
    """
    data = _fetch_inflation(first_date, last_date)
    if not data:
        return {"latest_inflation": None, "latest_key_rate": None,
                "latest_date": None, "series": []}

    data = data[-tail:]
    latest = data[-1]
    return {
        "latest_inflation": latest["inflation_yoy"],
        "latest_key_rate": latest["key_rate"],
        "latest_inflation_target": latest["inflation_target"],
        "latest_date": latest["date"],
        "series": data,
    }
