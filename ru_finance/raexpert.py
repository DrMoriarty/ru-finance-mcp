"""Кредитные рейтинги Эксперт РА (raexpert.ru) — скрейпинг серверных таблиц.

Источник — raexpert.ru/ratings/{category}/. Каждая страница содержит таблицу
20 рейтинговых действий с пагинацией. Парсим все категории (банки, компании,
облигации, страховщики, НПФ и т.д.) и объединяем в единый индекс.

Кэш в памяти на 4 ч (как в smartlab.py). Поиск — подстрока + токены
(все слова запроса в имени, порядок не важен). Для облигаций парсим название
выпуска и эмитента.

Категории загружаются параллельно (ThreadPoolExecutor, max_workers=3).
Первый вызов ~1-2 мин (сеть), повторные — из кэша.

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

# ── MOEX отраслевые индексы: название сектора → тикеры акций ──
# Составы из https://iss.moex.com/iss/statistics/engines/stock/markets/index/analytics/{id}.json
# Обновлять при изменении состава индексов (~2 раза в год).
_MOEX_SECTOR_TICKERS: dict[str, list[str]] = {
    "Финансовый": [
        "SBER", "SBERP", "VTBR", "T", "CBOM", "BSPB", "SVCB", "MOEX",
        "RENI", "SPBE", "SFIN", "DOMRF", "MBNK",
    ],
    "Нефтегазовый": [
        "GAZP", "LKOH", "ROSN", "TATN", "TATNP", "SNGS", "SNGSP",
        "NVTK", "BANEP", "RNFT", "TRNFP",
    ],
    "Потребительский": [
        "MGNT", "X5", "LENT", "BELU", "APTK", "GEMC", "HNFG", "EUTR",
        "FIXR", "AQUA", "MDMG", "OZPH", "PRMD", "RAGR", "SVAV", "VSEH", "WUSH",
    ],
    "Телекоммуникации": ["MTSS", "RTKM", "RTKMP", "MGTSP"],
    "Электроэнергетика": [
        "FEES", "HYDR", "IRAO", "MSNG", "OGKB", "TGKA", "UPRO", "ELFV",
        "LSNGP", "MRKC", "MRKP", "MRKU", "MRKV", "MSRS",
    ],
    "Транспорт": ["AFLT", "NMTP", "FLOT", "FESH", "NKHP"],
    "Металлургия и добыча": [
        "GMKN", "NLMK", "MAGN", "CHMF", "ALRS", "RUAL", "ENPG", "PLZL",
        "MTLR", "MTLRP", "RASP", "SELG", "TRMK", "UGLD", "VSMO",
    ],
    "Недвижимость": ["PIKK", "LSRG", "ETLN", "GLRX", "SMLT"],
    "Химия": ["PHOR", "AKRN", "NKNCP"],
    "Инновации и IT": [
        "YDEX", "ASTR", "DATA", "DIAS", "IVAT", "NAUK", "NSVZ", "OZPH",
        "POSI", "PRMD", "SOFL", "WUSH", "ABIO", "BAZA", "CNRU",
        "DELI", "ELMT", "GECO", "GEMA", "UNAC",
    ],
}

# Обратный маппинг: тикер → сектор
_MOEX_TICKER_TO_SECTOR: dict[str, str] = {
    t.upper(): sector for sector, tickers in _MOEX_SECTOR_TICKERS.items() for t in tickers
}
# Названия известных секторов (для валидации параметра sector)
SECTORS: list[str] = list(_MOEX_SECTOR_TICKERS)

# ── Порядок рейтингов (для фильтрации ≥ заданного) ──
_RATING_ORDER: dict[str, int] = {
    "ruAAA": 19, "ruAA+": 18, "ruAA": 17, "ruAA-": 16,
    "ruA+": 15, "ruA": 14, "ruA-": 13,
    "ruBBB+": 12, "ruBBB": 11, "ruBBB-": 10,
    "ruBB+": 9, "ruBB": 8, "ruBB-": 7,
    "ruB+": 6, "ruB": 5, "ruB-": 4,
    "ruCCC": 3, "ruCC": 2, "ruC": 1, "ruD": 0,
}

_cache_sector_map: dict[str, str] | None = None


def _normalize_emitent_name(name: str) -> str:
    """Нормализация названия эмитента для fuzzy-сравнения.

    Убирает организационно-правовую форму (сокращения и полные), кавычки,
    лишние пробелы.
    Пример: 'ПАО «Сбербанк России»' -> 'СБЕРБАНК РОССИИ'
            'ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО ГАЗПРОМ' -> 'ГАЗПРОМ'
    """
    n = name.upper()
    # Сокращённая ОПФ в начале
    n = re.sub(
        r"^(?:ПАО|АО|ООО|ЗАО|ОАО|НАО|ПАТ|ЧАО|ФГУП|ГУП|МУП|АНО|НПФ)\s+",
        "", n,
    )
    # Сокращённая ОПФ в конце
    n = re.sub(
        r"\s+(?:ПАО|АО|ООО|ЗАО|ОАО|НАО|ПАТ|ЧАО)$",
        "", n,
    )
    # Полная ОПФ в начале (скобки-варианты в конце)
    n = re.sub(
        r"^(?:ПУБЛИЧНОЕ\s+)?АКЦИОНЕРНОЕ\s+ОБЩЕСТВО\s*", "", n,
    )
    n = re.sub(
        r"^ОБЩЕСТВО\s+С\s+ОГРАНИЧЕННОЙ\s+ОТВЕТСТВЕННОСТЬЮ\s*", "", n,
    )
    n = re.sub(
        r"^НЕГОСУДАРСТВЕННЫЙ\s+ПЕНСИОННЫЙ\s+ФОНД\s*", "", n,
    )
    # Скобки с ОПФ в конце: (ПАО), (ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО) и т.п.
    # Включая суффиксы вроде (ПАО)АО, (ООО)ООО
    n = re.sub(
        r"(?:\s*\([^)]*(?:ПАО|АКЦИОНЕРНОЕ\s+ОБЩЕСТВО|ООО)[^)]*\))+(?:АО|ООО|ПАО)?\s*",
        "", n,
    )
    # Кавычки-ёлочки и обычные
    n = re.sub(r'[\u00ab\u00bb"\u201c\u201d]', "", n)
    n = re.sub(r"«|»", "", n)
    # Точки в инициалах (В.Д. -> ВД) — для сравнения
    n = re.sub(r"\b([А-Я])\.\s*", r"\1", n)
    # Множественные пробелы
    n = re.sub(r"\s+", " ", n).strip()
    return n


def _build_sector_map() -> dict[str, str]:
    """Нормализованное имя → MOEX-сектор для всех акций из отраслевых индексов.

    Загружает SECNAME из bulk-эндпоинта МосБиржи (один HTTP-запрос).
    Результат кэшируется в памяти.
    """
    global _cache_sector_map
    if _cache_sector_map is not None:
        return _cache_sector_map

    result: dict[str, str] = {}
    all_tickers = {t for t in _MOEX_TICKER_TO_SECTOR}

    try:
        import requests as _req
        url = "https://iss.moex.com/iss/engines/stock/markets/shares/securities.json"
        resp = _req.get(
            url,
            params={"iss.meta": "off", "iss.only": "securities", "limit": 3000},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        cols = data["securities"]["columns"]
        sci = cols.index("SECID")
        sni = cols.index("SECNAME")
        for row in data["securities"]["data"]:
            secid = row[sci]
            if secid in all_tickers and row[sni]:
                norm_name = _norm_secname(row[sni])
                if norm_name:
                    result[norm_name] = _MOEX_TICKER_TO_SECTOR[secid]
    except Exception:
        pass

    _cache_sector_map = result
    return result


def _norm_secname(name: str) -> str:
    """Упрощённая нормализация SECNAME MOEX: убирает 'ао'/'ап' суффиксы."""
    n = re.sub(r"\s+(?:ао|ап|гдр|нр)\s*$", "", name, flags=re.I)
    return _normalize_emitent_name(n)


def _sector_for_emitent(name: str) -> str | None:
    """Определить MOEX-сектор эмитента raexpert по названию.

    1) Точное совпадение после нормализации.
    2) Подстрока: нормализованное имя raexpert входит в MOEX имя или наоборот.
    """
    sector_map = _build_sector_map()
    if not sector_map:
        return None

    raexpert_norm = _normalize_emitent_name(name)
    if not raexpert_norm:
        return None

    # Точное совпадение
    if raexpert_norm in sector_map:
        return sector_map[raexpert_norm]

    # Подстрочное совпадение (минимум 3 символа)
    if len(raexpert_norm) >= 3:
        for moex_name, sector in sector_map.items():
            if raexpert_norm in moex_name or moex_name in raexpert_norm:
                return sector

    return None


def emitent_rating_search(
    rating_min: str | None = None,
    sector: str | None = None,
) -> list[dict[str, Any]]:
    """Поиск эмитентов по кредитному рейтингу и отрасли.

    Args:
        rating_min — минимальный рейтинг ('ruBBB-', 'ruA', ...).
                     Шкала: ruAAA (19) > ruAA+ (18) > ... > ruB- (4) > ruCCC (3)
                             > ruD (0). Записи с «отозван» исключаются.
        sector — название отрасли MOEX (одно из SECTORS).
                 Маппинг: ~100 крупнейших эмитентов из отраслевых индексов МосБиржи.

    Возврат: [{name, rating, outlook, date, category, sector?, agency}, ...].
    Без фильтров — все эмитенты (включая «отозван»).
    """
    all_ratings = _fetch_all_ratings()
    emitents = [r for r in all_ratings if r["type"] == "emitent"]

    # ── Фильтр по рейтингу ──
    if rating_min:
        min_score = _RATING_ORDER.get(rating_min.strip())
        if min_score is None:
            raise ValueError(
                f"Неизвестный рейтинг: {rating_min!r}. "
                f"Допустимые: {', '.join(_RATING_ORDER)}"
            )
        emitents = [
            r for r in emitents
            if r["rating"] != "отозван" and _RATING_ORDER.get(r["rating"], -1) >= min_score
        ]

    # ── Фильтр по сектору ──
    if sector:
        sector_norm = sector.strip()
        if sector_norm not in SECTORS:
            raise ValueError(
                f"Неизвестный сектор: {sector!r}. "
                f"Допустимые: {', '.join(SECTORS)}"
            )
        filtered: list[dict[str, Any]] = []
        for r in emitents:
            found_sector = _sector_for_emitent(r["name"])
            if found_sector == sector_norm:
                filtered.append(r)
        emitents = filtered

    need_sector = sector is not None

    # ── Результат ──
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for r in emitents:
        name = r["name"]
        if name in seen:
            continue
        seen.add(name)
        item: dict[str, Any] = {
            "name": name,
            "rating": r["rating"],
            "outlook": r.get("outlook", ""),
            "date": r["date"],
            "category": r["category"],
            "agency": "Эксперт РА",
        }
        if need_sector:
            s = _sector_for_emitent(name)
            if s:
                item["sector"] = s
        result.append(item)

    # Сортировка: рейтинг по убыванию (макс. первый), затем по имени
    result.sort(key=lambda x: (-_RATING_ORDER.get(x["rating"], -1), x["name"]))
    return result


def list_categories() -> dict[str, str]:
    """Список доступных категорий рейтингов."""
    return dict(_CATEGORIES)


# Паттерн рейтинга Эксперт РА:
# ruAAA, ruAA+, ruAA, ruAA-, ruA+, ruA, ruA-, ruBBB+, ..., ruB-, ruCCC
# Также: отозван, SB, ruAAA(EXP), ruBBB-(EXP)
_RATING_RE = re.compile(
    r"^ru(?:AAA|AA[+\-]?|A[+\-]?|BBB[+\-]?|BB[+\-]?|B[+\-]?|CCC|CC|C|D)"
    r"(?:\(EXP\))?$|^отозван$|^SB\b"
)

# Паттерн строки таблицы: извлекаем <td> ячейки
_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.DOTALL)


def _fetch_page(path: str, session: requests.Session | None = None) -> str:
    """GET страницы raexpert.ru."""
    s = session or requests
    url = f"https://raexpert.ru{path}"
    resp = s.get(url, headers=_HEADERS, timeout=30, allow_redirects=True)
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


CSRF_RE = re.compile(r"CSRFAjaxTokenPageHash\s*=\s*'([^']+)'")
PAGE_HASH_RE = re.compile(r"setRatingPageHash\('([^']+)'\)")


def _fetch_category_ratings(cat_id: str) -> list[dict[str, Any]]:
    """Загрузить все страницы рейтингов одной категории (с пагинацией)."""
    session = requests.Session()
    session.headers.update(_HEADERS)

    path = f"/ratings/{cat_id}/"
    html = _fetch_page(path, session)
    all_rows = _parse_rating_rows(html, cat_id)

    # Извлекаем CSRF-токен и хеши страниц из пагинатора
    csrf_match = CSRF_RE.search(html)
    page_hashes = PAGE_HASH_RE.findall(html)

    if csrf_match and page_hashes:
        csrf_token = csrf_match.group(1)
        for ph in page_hashes:
            try:
                session.post(
                    f"https://raexpert.ru/ratings/index/ajax-set-rating-page-hash/",
                    data={"rating_page_hash": ph, "CSRFAjaxToken": csrf_token},
                    timeout=15,
                )
                page_html = _fetch_page(path, session)
                all_rows.extend(_parse_rating_rows(page_html, cat_id))
            except Exception:
                continue

    return all_rows


def _fetch_all_ratings() -> list[dict[str, Any]]:
    """Загрузить рейтинги со всех категорий raexpert.ru (параллельно, с кэшем 4 ч)."""
    global _cache_all
    now = time.monotonic()
    if _cache_all is not None:
        ts, data = _cache_all
        if now - ts < _CACHE_TTL and data:
            return data

    all_ratings: list[dict[str, Any]] = []
    from concurrent.futures import ThreadPoolExecutor, as_completed
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = {pool.submit(_fetch_category_ratings, cid): cid for cid in _CATEGORIES}
        for fut in as_completed(futures):
            try:
                all_ratings.extend(fut.result())
            except Exception:
                continue

    _cache_all = (now, all_ratings)
    return all_ratings


def _token_match(query_upper: str, text_upper: str) -> bool:
    """Все слова запроса встречаются в тексте (порядок не важен)."""
    tokens = query_upper.split()
    if len(tokens) <= 1:
        return False
    return all(tok in text_upper for tok in tokens)


def _search_in(query_upper: str, r: dict) -> bool:
    """Проверить совпадение записи рейтинга с запросом (подстрока, затем токены)."""
    name_upper = r["name"].upper()
    if query_upper in name_upper:
        return True
    if _token_match(query_upper, name_upper):
        return True
    # Для облигаций ищем также в названии эмитента
    emitent = r.get("emitent")
    if emitent:
        emitent_upper = emitent.upper()
        if query_upper in emitent_upper:
            return True
        if _token_match(query_upper, emitent_upper):
            return True
    # Для company slug
    slug = r.get("company_slug")
    if slug:
        slug_upper = slug.upper()
        if query_upper in slug_upper:
            return True
        if _token_match(query_upper, slug_upper):
            return True
    return False


def rating_search(query: str) -> list[dict[str, Any]]:
    """Поиск рейтинга по названию эмитента или облигации (без учёта регистра).

    Ищет двумя способами:
    1. Подстрока: весь запрос целиком в имени.
    2. Токены: каждое слово запроса встречается в имени (порядок не важен).
       Напр. "Балтийский лизинг" найдёт "ООО «Балтийский лизинг»".

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
        if _search_in(q, r):
            matches.append(r)

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
