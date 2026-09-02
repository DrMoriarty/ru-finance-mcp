"""Данные по выплатам ЗПИФ недвижимости с vsezpif.ru (календарь + история).

vsezpif.ru — единственный бесплатный агрегатор выплат ЗПИФ недвижимости.
Серверный рендеринг (без __NEXT_DATA__), данные извлекаются парсингом HTML.

Данные: календарь на 12 месяцев + метаданные фондов (ISIN, цена, доходность).
Кэш в памяти на 4 ч.
"""
from __future__ import annotations

import re
import time
from datetime import datetime, date
from typing import Any

import requests

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

# Кэш: путь -> (timestamp, response)
_CACHE: dict[str, tuple[float, str]] = {}

# Кэш для перевода slug -> ISIN (после первого посещения страницы фонда)
_SLUG_TO_ISIN: dict[str, str] = {}


def _fetch(path: str, params: dict[str, str] | None = None) -> str:
    """GET страницы vsezpif.ru с кэшем (TTL 4 ч)."""
    cache_key = path + (str(params) if params else "")
    now = time.monotonic()
    cached = _CACHE.get(cache_key)
    if cached is not None:
        ts, html = cached
        if now - ts < 240 * 60:
            return html

    url = f"https://vsezpif.ru{path}"
    resp = requests.get(
        url,
        params=params,
        headers=_HEADERS,
        timeout=30,
        allow_redirects=True,
    )
    resp.raise_for_status()
    html = resp.text

    _CACHE[cache_key] = (now, html)
    return html


def _clean(text: str) -> str:
    """Убрать лишние пробелы и нормализовать."""
    return re.sub(r"\s+", " ", text).strip()


def _parse_amount(text: str) -> float | None:
    """Разобрать сумму из ячеек вида '34,00', '3 456', '37.64'."""
    text = _clean(text)
    if not text or text in ("-", "\u2014"):
        return None
    # Убрать пробелы между цифрами, заменить запятую на точку
    text = text.replace(" ", "").replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


def _parse_date_ru(text: str) -> str | None:
    """Разобрать дату из формата 'DD.MM.YYYY'."""
    text = _clean(text).strip("~")
    if not text or text in ("-", "\u2014", "\xa0"):
        return None
    return text


def _parse_date_iso(text: str) -> str | None:
    """Разобрать дату из формата 'DD.MM.YYYY' в ISO 'YYYY-MM-DD'."""
    text = _clean(text).strip("~")
    if not text or text in ("-", "\u2014", "\xa0"):
        return None
    try:
        dt = datetime.strptime(text, "%d.%m.%Y")
        return dt.strftime("%Y-%m-%d")
    except ValueError:
        return None


# ------------- КАЛЕНДАРЬ ВЫПЛАТ -------------

# Паттерн для извлечения из HTML:
# >~DD.MM</span>.....<a href="/zpif-{slug}">Fund Name</a>.....Amount ₽/пай
_PAYMENT_PATTERN = re.compile(
    r'>(~?\d{2}\.\d{2})</span>.*?<a[^>]*href="(/zpif-[^"]+)"[^>]*>([^<]+)</a>.*?'
    r'(\d[\d\s]*[,.]?\d*)\s*₽',
    re.DOTALL,
)

# Паттерт месяцев из заголовков
_MONTH_PATTERN = re.compile(
    r'>(\w+)\s+(\d{4})</span>\s*<span[^>]*>(\d+)\s+выплат',
)


def parse_calendar(html: str, year: int | None = None) -> list[dict[str, Any]]:
    """Календарь выплат ЗПИФ из HTML-страницы.

    Возвращает список: {date, date_iso, fund_name, slug, amount}.
    """
    if year is None:
        year = date.today().year

    # Извлечь все записи о выплатах
    entries = _PAYMENT_PATTERN.findall(html)

    # Определить год по заголовкам месяцев (если есть)
    month_headers = _MONTH_PATTERN.findall(html)
    # TODO: можно уточнить год по заголовкам

    results = []
    seen_slugs = set()
    for date_str, slug, fund_name, amount_str in entries:
        # Пропустить записи с "Показать выплаты этого месяца"
        if "Показать" in fund_name or "зарегист" in fund_name.lower():
            continue

        slug = slug.strip()
        date_clean = date_str.strip("~")

        # Определить день и месяц
        parts = date_clean.split(".")
        if len(parts) != 2:
            continue
        day, month = parts

        # Формат DD.MM.YYYY
        date_full = f"{day}.{month}.{year}"
        try:
            date_iso = f"{year}-{int(month):02d}-{int(day):02d}"
        except (ValueError, IndexError):
            continue

        amount = _parse_amount(amount_str)

        results.append({
            "date": date_full,
            "date_iso": date_iso,
            "fund_name": fund_name.strip(),
            "slug": slug,
            "amount": amount,
        })

        seen_slugs.add(slug)

    return results


def get_payment_calendar(limit: int = 100) -> list[dict[str, Any]]:
    """Календарь ближайших выплат ЗПИФ с vsezpif.ru.

    Возвращает: {date, date_iso, fund_name, slug, amount} на 12 месяцев.
    """
    html = _fetch("/?route=vyplaty-zpif")
    payments = parse_calendar(html)
    return payments[:limit]


# ------------- ИНДИВИДУАЛЬНЫЙ ФОНД -------------

def _get_fund_isin_from_page(slug: str) -> str | None:
    """Получить ISIN фонда со страницы /zpif-{slug}."""
    if slug in _SLUG_TO_ISIN:
        return _SLUG_TO_ISIN[slug]

    try:
        html = _fetch(f"/zpif-{slug}")
        # Ищем ISIN в данных страницы (в JSON-LD или в тексте)
        isin_match = re.search(r'isin[\":\'\\s]+([A-Z0-9]{12})', html, re.I)
        if isin_match:
            isin = isin_match.group(1)
            _SLUG_TO_ISIN[slug] = isin
            return isin
    except Exception:
        pass
    return None


def _parse_fund_page(html: str) -> dict[str, Any]:
    """Парсинг страницы конкретного фонда."""
    data: dict[str, Any] = {}

    # ISIN
    isin_match = re.search(r'isin[\":\'\\s]+([A-Z0-9]{12})', html, re.I)
    if isin_match:
        data["isin"] = isin_match.group(1)

    # Цена пая
    price_match = re.search(
        r'биржев[а-я]+ ц[а-я]+[^<>]*?(\d[\d\s]*(?:[,.]\d+)?)\s*₽',
        html,
        re.I,
    )
    if price_match:
        data["last_price"] = _parse_amount(price_match.group(1))

    # Доходность
    yld_match = re.search(r'доходност[ьи][^<>]*?([\d,.]+)\s*%', html, re.I)
    if yld_match:
        data["yield_pct"] = _parse_amount(yld_match.group(1))

    # Периодичность
    freq_match = re.search(
        r'(?:ежемесячно|поквартально|квартал|ежекварт)',
        html,
        re.I,
    )
    if freq_match:
        freq = freq_match.group(0).lower()
        if "месяц" in freq:
            data["frequency"] = "monthly"
        else:
            data["frequency"] = "quarterly"

    # История выплат из JSON-LD
    jsonld_match = re.search(
        r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>',
        html,
        re.DOTALL,
    )
    if jsonld_match:
        try:
            import json
            ld = json.loads(jsonld_match.group(1))
            # Ищем историю в FAQ
            if isinstance(ld, list) and len(ld) > 0:
                faq = ld[0]
                if faq.get("@type") == "FAQPage":
                    questions = faq.get("mainEntity", [])
                    for q in questions:
                        answer = q.get("acceptedAnswer", {}).get("text", "")
                        # Найти выплаты в ответе
                        payments_in_text = re.findall(
                            r'(\d{2}\.\d{2}\.\d{4})[^₽]*?([\d\s]+[,.]?\d*)\s*₽',
                            answer,
                        )
                        if payments_in_text:
                            data["manual_payments"] = [
                                {
                                    "date": p[0],
                                    "amount": _parse_amount(p[1]),
                                }
                                for p in payments_in_text
                            ]
        except Exception:
            pass

    return data


def get_fund_by_slug(slug: str) -> dict[str, Any] | None:
    """Получить данные фонда по slug (например, 'vim-rentnyj-dohod-pro').

    Возвращает: {isin, fund_name, last_price, yield_pct, frequency} или None.
    """
    try:
        html = _fetch(f"/zpif-{slug}")
        data = _parse_fund_page(html)

        # Название из заголовка
        title_match = re.search(r'<title>([^<]+)</title>', html)
        if title_match:
            title = title_match.group(1)
            # Извлекаем название до ( или ,
            fund_name = re.split(r'[,(]', title)[0].strip()
            data["fund_name"] = fund_name

        data["slug"] = slug
        return data
    except Exception:
        return None


def get_fund_by_isin(isin: str) -> dict[str, Any] | None:
    """Получить данные фонда по ISIN.

    Ищет в календаре и затем открывает страницу фонда.
    """
    isin = isin.upper()

    # Сначала получить календарь для построения маппинга
    payments = get_payment_calendar(limit=1000)

    # Найти slug для этого ISIN
    # Для этого нужно проверить каждую страницу, но это долго
    # Попробуем найти по названию в календаре
    for p in payments:
        if "slug" in p:
            isin_found = _get_fund_isin_from_page(p["slug"])
            if isin_found == isin:
                return get_fund_by_slug(p["slug"])

    return None


def get_payments_by_fund(
    fund_name: str | None = None,
    isin: str | None = None,
    limit: int = 50,
) -> list[dict[str, Any]]:
    """Получить выплаты по конкретному фонду.

    Ищет в календаре по названию (частичное совпадение) или ISIN.
    """
    payments = get_payment_calendar(limit=1000)

    results = []
    for p in payments:
        match = False

        if fund_name and fund_name.lower() in p.get("fund_name", "").lower():
            match = True

        if isin:
            # Получить ISIN для этого slug
            slug = p.get("slug", "")
            if slug:
                fund_isin = _get_fund_isin_from_page(slug)
                if fund_isin and fund_isin.upper() == isin.upper():
                    match = True

        if match:
            results.append(p)

    return results[:limit]


def estimate_next_payment(
    fund_name: str | None = None,
    isin: str | None = None,
) -> dict[str, Any] | None:
    """Оценить следующую выплату по фонду.

    Ищет в календаре ближайшую будущую выплату.
    """
    payments = get_payments_by_fund(fund_name=fund_name, isin=isin, limit=1000)

    if not payments:
        return None

    today = date.today()
    today_str = today.strftime("%Y-%m-%d")

    # Найти ближайшую будущую выплату
    upcoming = [p for p in payments if p.get("date_iso", "") >= today_str]

    if not upcoming:
        # Все выплаты в прошлом — взять последнюю
        return payments[-1] if payments else None

    # Вернуть ближайшую
    return upcoming[0]


# ------------- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ -------------

def list_funds() -> list[dict[str, str]]:
    """Получить список всех фондов с vsezpif.ru (slug + название).

    Извлекает данные из календаря выплат.
    """
    html = _fetch("/?route=vyplaty-zpif")

    # Найти все ссылки на фонды
    links = re.findall(r'href="(/zpif-[^"]+)"[^>]*>([^<]+)</a>', html)

    seen = set()
    results = []
    for slug_raw, name in links:
        slug = slug_raw.strip()
        if slug in seen or not slug.startswith("/zpif-") or "/zpif-?" in slug:
            continue

        # Извлечь slug (убрать /zpif-)
        fund_slug = slug.replace("/zpif-", "")
        seen.add(slug)

        results.append({
            "slug": fund_slug,
            "fund_name": name.strip(),
            "url": f"https://vsezpif.ru{slug}",
        })

    return results


def get_isin_for_slug(slug: str) -> str | None:
    """Получить ISIN для slug фонда."""
    return _get_fund_isin_from_page(slug)
