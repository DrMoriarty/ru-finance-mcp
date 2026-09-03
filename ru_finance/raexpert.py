"""Кредитные рейтинги Эксперт РА (raexpert.ru) — скрейпинг серверных таблиц.

Источник — raexpert.ru/ratings/{category}/. Каждая страница содержит таблицу
20 последних рейтинговых действий. Парсим все категории (банки, компании,
облигации, страховщики, НПФ и т.д.) и объединяем в единый индекс.

Кэш в памяти на 4 ч (как в smartlab.py). Поиск — по подстроке названия
(регистр-независимый). Для облигаций парсим название выпуска и эмитента.

Платный REST API существует (https://raexpert.ru/soap/service/export/?hash=...),
но для бесплатного доступа только HTML-скрейпинг.
"""
from __future__ import annotations

import html as _html_mod
import re
import time
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

_CACHE_TTL = 4 * 60 * 60  # 4 часа

# Категории рейтингов: id -> название
_CATEGORIES: dict[str, str] = {
    "bankcredit_all": "Кредитные организации",
    "credits_all": "Нефинансовые компании",
    "credits_fin": "Финансовые компании",
    "credits_holding": "Холдинговые компании",
    "credits_project": "Проектные компании",
    "leasing_rel": "Лизинговые компании",
    "debt_inst": "Облигации",
    "insurance_all": "Страховые компании",
    "npf": "НПФ",
    "mfi_credits_all": "МФО",
}

_cache_all: tuple[float, list[dict[str, Any]]] | None = (0.0, [])

# Паттерн рейтинга Эксперт РА:
# ruAAA, ruAA+, ruAA, ruAA-, ruA+, ruA, ruA-, ruBBB+, ..., ruB-, ruCCC
# Также: отозван, SB, ruAAA(EXP), ruBBB-(EXP)
_RATING_RE = re.compile(
    r"^ru(?:AAA|AA[+\-]?|A[+\-]?|BBB[+\-]?|BB[+\-]?|B[+\-]?|CCC|CC|C|D)"
    r"(?:\(EXP\))?$|^отозван$|^SB\b"
)

# Паттерн строки таблицы: извлекаем <td> ячейки
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)


def _fetch_page(path: str) -> str:
    """GET страницы raexpert.ru."""
    url = f"https://raexpert.ru{path}"
    resp = requests.get(url, headers=_HEADERS, timeout=30, allow_redirects=True)
    resp.raise_for_status()
    return resp.text


def _strip_tags(html_str: str) -> str:
    """Убрать HTML-теги, раскрыть сущности и нормализовать пробелы."""
    text = re.sub(r"<[^>]+>", "", html_str)
    text = _html_mod.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _parse_rating_rows(html: str, category_id: str) -> list[dict[str, Any]]:
    """Парсинг строк рейтинговой таблицы из HTML страницы категории.

    Структура <tr>:
      <td>name_html</td>  <td>rating</td>  <td>outlook</td>  <td>date</td>
    """
    rows: list[dict[str, Any]] = []
    category_name = _CATEGORIES.get(category_id, category_id)

    for tr_match in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.DOTALL):
        tr_html = tr_match.group(1)
        tds = _TD_RE.findall(tr_html)
        if len(tds) < 3:
            continue

        name_html = tds[0]
        rating_html = tds[1]

        # Рейтинг — очищаем от тегов
        rating = _strip_tags(rating_html).strip()
        # Outlook — третье поле, для облигаций может быть «—»
        outlook = _strip_tags(tds[2]).strip() if len(tds) > 2 else ""
        # Дата — четвёртое поле
        date = _strip_tags(tds[3]).strip() if len(tds) > 3 else ""

        # Пропускаем строки-заголовки и не-рейтинговые строки
        if not _RATING_RE.match(rating):
            continue

        # Парсим имя: строки разделены <br> или \n
        raw_text = re.sub(r"<br\s*/?>", "\n", name_html)
        raw_text = re.sub(r"</?\w+[^>]*>", " ", raw_text)
        lines = [l.strip() for l in raw_text.split("\n") if l.strip()]
        if not lines:
            continue

        # Определяем тип записи
        is_emission = category_id == "debt_inst" or (
            len(lines) >= 1
            and re.search(r"(?:Облигаци[ия]|серии?\s+\S+)", lines[0], re.I)
        )

        # Извлекаем company slug из ссылки на компанию
        company_slug = ""
        company_url_match = re.search(r'href="/database/companies/([^"]+)"', name_html)
        if company_url_match:
            company_slug = company_url_match.group(1)

        if is_emission and len(lines) >= 2:
            # Облигация: первая строка — название выпуска, вторая — эмитент
            emitent_name = _strip_tags(lines[1]) if len(lines) > 1 else ""
            # Убираем кавычки
            emitent_name = re.sub(r'^["\u00ab]|["\u00bb]$', "", emitent_name)
            emission_name = _strip_tags(lines[0])
            name = emission_name
            result: dict[str, Any] = {
                "name": name,
                "emitent": emitent_name,
                "rating": rating,
                "outlook": _clean_outlook(outlook),
                "date": date,
                "category": category_name,
                "type": "emission",
                "agency": "Эксперт РА",
                "company_slug": company_slug,
            }
        else:
            # Эмитент (компания/банк/страховщик)
            name = _strip_tags(lines[0])
            result = {
                "name": name,
                "rating": rating,
                "outlook": _clean_outlook(outlook),
                "date": date,
                "category": category_name,
                "type": "emitent",
                "agency": "Эксперт РА",
                "company_slug": company_slug,
            }

        rows.append(result)

    return rows


def _clean_outlook(outlook: str) -> str:
    """Нормализовать прогноз."""
    outlook = outlook.strip()
    # HTML-сущности
    outlook = (
        outlook.replace("—", "\u2014")
        .replace("—", "\u2014")
        .replace("–", "\u2013")
    )
    if outlook in ("—", "-", "\u2014", "\u2013", ""):
        return ""
    return outlook


def _fetch_all_ratings() -> list[dict[str, Any]]:
    """Загрузить рейтинги со всех категорий raexpert.ru (с кэшем 4 ч)."""
    global _cache_all
    now = time.monotonic()
    if _cache_all is not None:
        ts, data = _cache_all
        if now - ts < _CACHE_TTL and data:
            return data

    all_ratings: list[dict[str, Any]] = []
    for cat_id in _CATEGORIES:
        try:
            html = _fetch_page(f"/ratings/{cat_id}/")
            rows = _parse_rating_rows(html, cat_id)
            all_ratings.extend(rows)
        except Exception:
            continue  # пропускаем категории с ошибками

    _cache_all = (now, all_ratings)
    return all_ratings


def rating_search(query: str) -> list[dict[str, Any]]:
    """Поиск рейтинга по названию эмитента или облигации (подстрока, без учёта регистра).

    Вход: query — тикер/название эмитента ('Сбербанк', 'ЛУКОЙЛ', 'ГТЛК').
    Возврат: [{name, rating, outlook, date, category, type, agency}, ...].
    Если тип == 'emission', дополнительно {emitent}.
    """
    if not query:
        return []

    ratings = _fetch_all_ratings()
    q = query.strip().upper()

    matches: list[dict[str, Any]] = []
    for r in ratings:
        name_upper = r["name"].upper()
        # Прямое совпадение подстроки в имени
        if q in name_upper:
            matches.append(r)
            continue
        # Для облигаций ищем также в названии эмитента
        if r.get("emitent") and q in r["emitent"].upper():
            matches.append(r)
            continue
        # Для company slug
        if r.get("company_slug") and q in r["company_slug"].upper():
            matches.append(r)
            continue

    # Дедупликация: один и тот же рейтинг + дата может быть для нескольких серий
    seen: set[tuple[str, str, str, str]] = set()
    unique: list[dict[str, Any]] = []
    for r in matches:
        key = (r["name"], r["rating"], r.get("outlook", ""), r["date"])
        if key not in seen:
            seen.add(key)
            unique.append(r)

    return unique


def list_categories() -> dict[str, str]:
    """Список доступных категорий рейтингов."""
    return dict(_CATEGORIES)
