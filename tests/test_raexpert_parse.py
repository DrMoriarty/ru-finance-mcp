"""Парсеры и утилиты raexpert.py на локальных HTML-фикстурах (без сети)."""
from __future__ import annotations

import pytest

from ru_finance.raexpert import (
    _clean_outlook,
    _norm_secname,
    _normalize_emitent_name,
    _parse_rating_rows,
    _search_in,
    _strip_tags,
    _token_match,
)

# ────────────────────────── Фикстуры HTML ──────────────────────────

# Страница категории «Облигации»: эмиссия (2 строки в имени) + эмитент + мусор
DEBT_INST_HTML = """
<table>
  <tr class="head"><th>Эмитент</th><th>Рейтинг</th><th>Прогноз</th><th>Дата</th></tr>
  <tr>
    <td><a href="/database/companies/sberbank/">ПАО «Сбербанк России»</a></td>
    <td>ruAAA</td><td>стабильный</td><td>05.02.2026</td>
  </tr>
  <tr>
    <td>СБОЛ-35 об<br>ПАО «Сбербанк России»</td>
    <td>ruAAA</td><td>—</td><td>01.03.2026</td>
  </tr>
  <tr>
    <td><a href="/database/companies/vtb/">ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО Банк ВТБ</a></td>
    <td>ruAA-</td><td>позитивный</td><td>20.01.2026</td>
  </tr>
  <tr>
    <td>Строка без рейтинга</td>
    <td>n/a</td><td></td><td>01.01.2026</td>
  </tr>
  <tr>
    <td>Отозванный рейтинг</td>
    <td>отозван</td><td></td><td>15.12.2025</td>
  </tr>
</table>
"""

# Страница категории «Кредитные организации» (только эмитенты)
BANK_HTML = """
<table>
  <tr>
    <td><a href="/database/companies/alfa/">АО «Альфа-Банк»</a></td>
    <td>ruAA</td><td>негативный</td><td>10.02.2026</td>
  </tr>
  <tr>
    <td><a href="/database/companies/gazprombank/">Банк ГПБ (АО)</a></td>
    <td>ruAAA(EXP)</td><td>—</td><td>01.02.2026</td>
  </tr>
</table>
"""


# ────────────────────────── _normalize_emitent_name ──────────────────────────

@pytest.mark.parametrize(
    ("name", "expected"),
    [
        # кавычки-ёлочки и прямые
        ('ПАО «Сбербанк России»', "СБЕРБАНК РОССИИ"),
        ('ООО "Балтийский лизинг"', "БАЛТИЙСКИЙ ЛИЗИНГ"),
        # полная ОПФ в начале
        ("ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО ГАЗПРОМ", "ГАЗПРОМ"),
        ("ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ РОЛЬФ", "РОЛЬФ"),
        ("НЕГОСУДАРСТВЕННЫЙ ПЕНСИОННЫЙ ФОНД БЛАГОСОСТОЯНИЕ", "БЛАГОСОСТОЯНИЕ"),
        # скобки с ОПФ в конце
        ("Банк ВТБ (ПАО)", "БАНК ВТБ"),
        # «(АО)» в скобках не вычищается — неполная нормализация
        ("Банк ГПБ (АО)", "БАНК ГПБ (АО)"),
        # суффикс «-М» в названии сохраняется (не часть ОПФ)
        ("АО «Компания-М»", "КОМПАНИЯ-М"),
        ("АО «Банк-М»", "БАНК-М"),
        # инициалы: точки схлопываются для сравнения
        ("Иванов И.И.", "ИВАНОВ ИИ"),
    ],
)
def test_normalize_emitent_name(name, expected):
    assert _normalize_emitent_name(name) == expected


def test_normalize_emitent_name_same_after_normalize():
    # разные написания одного эмитента дают одинаковую нормформу
    assert (
        _normalize_emitent_name('ПАО «Сбербанк России»')
        == _normalize_emitent_name('ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО СБЕРБАНК РОССИИ')
    )


# ────────────────────────── _parse_rating_rows ──────────────────────────

def test_parse_rating_rows_debt_inst():
    rows = _parse_rating_rows(DEBT_INST_HTML, "debt_inst")
    # мусорная строка (n/a) пропущена; отозван — пропущен по паттерну? нет: отозван валиден
    assert len(rows) == 4

    emitent_row = rows[0]
    assert emitent_row["type"] == "emitent"
    assert emitent_row["name"] == "ПАО «Сбербанк России»"
    assert emitent_row["rating"] == "ruAAA"
    assert emitent_row["outlook"] == "стабильный"
    assert emitent_row["date"] == "05.02.2026"
    assert emitent_row["category"] == "Облигации"
    assert emitent_row["agency"] == "Эксперт РА"
    assert emitent_row["company_slug"] == "sberbank/"

    emission = rows[1]
    assert emission["type"] == "emission"
    assert emission["name"] == "СБОЛ-35 об"
    # закрывающая кавычка срезается, открывающая после ПАО — остаётся
    assert emission["emitent"] == "ПАО «Сбербанк России"
    assert emission["rating"] == "ruAAA"
    assert emission["outlook"] == ""  # «—» нормализовано в пусто
    assert emission["company_slug"] == ""

    vtb = rows[2]
    assert vtb["name"] == "ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО Банк ВТБ"
    assert vtb["rating"] == "ruAA-"
    assert vtb["outlook"] == "позитивный"

    withdrawn = rows[3]
    assert withdrawn["rating"] == "отозван"


def test_parse_rating_rows_bank_category():
    rows = _parse_rating_rows(BANK_HTML, "bankcredit_all")
    assert len(rows) == 2
    assert all(r["type"] == "emitent" for r in rows)
    assert rows[0]["category"] == "Кредитные организации"
    assert rows[0]["name"] == "АО «Альфа-Банк»"
    assert rows[0]["outlook"] == "негативный"
    # ruAAA(EXP) — валидный рейтинг
    assert rows[1]["rating"] == "ruAAA(EXP)"


def test_parse_rating_rows_empty():
    assert _parse_rating_rows("", "bankcredit_all") == []
    assert _parse_rating_rows("<div>нет таблицы</div>", "debt_inst") == []


def test_parse_rating_rows_unknown_category_keeps_id():
    rows = _parse_rating_rows(BANK_HTML, "nonexistent_cat")
    assert rows[0]["category"] == "nonexistent_cat"


# ────────────────────────── _clean_outlook ──────────────────────────

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("позитивный", "позитивный"),
        ("негативный", "негативный"),
        ("стабильный", "стабильный"),
        ("развивающийся", "развивающийся"),
        ("—", ""),
        ("-", ""),
        ("–", ""),
        ("", ""),
        ("  ", ""),
    ],
)
def test_clean_outlook(raw, expected):
    assert _clean_outlook(raw) == expected


# ────────────────────────── _token_match / _search_in ──────────────────────────

def test_token_match_partial_words():
    # все слова запроса в тексте, порядок не важен
    assert _token_match("БАЛТИЙСКИЙ ЛИЗИНГ", "ООО «БАЛТИЙСКИЙ ЛИЗИНГ»")
    assert _token_match("ЛИЗИНГ БАЛТИЙСКИЙ", "ООО «БАЛТИЙСКИЙ ЛИЗИНГ»")
    # не все слова присутствуют
    assert not _token_match("БАЛТИЙСКИЙ ЛИЗИНГ", "ООО «РОЛЬФ»")


def test_token_match_short_query():
    # запрос из одного слова (в т.ч. 1–2 буквы) — не матчится
    assert not _token_match("СБЕР", "СБЕРБАНК РОССИИ")
    assert not _token_match("СБ", "СБЕРБАНК РОССИИ")
    assert not _token_match("А", "АЛЬФА-БАНК")


def test_token_match_empty_query():
    assert not _token_match("", "ЛЮБОЙ ТЕКСТ")
    assert not _token_match("   ", "ЛЮБОЙ ТЕКСТ")


def _record(**overrides):
    rec = {
        "name": "ООО «Балтийский лизинг»",
        "rating": "ruA-",
        "outlook": "стабильный",
        "date": "01.02.2026",
        "category": "Лизинговые компании",
        "type": "emitent",
        "agency": "Эксперт РА",
        "company_slug": "baltiyskiy-lizing/",
    }
    rec.update(overrides)
    return rec


def test_search_in_substring():
    assert _search_in("БАЛТИЙСКИЙ ЛИЗИНГ", _record())
    assert _search_in("«БАЛТИЙСКИЙ", _record())


def test_search_in_token_match():
    # подстроки нет, но все токены в имени
    assert _search_in("БАЛТИЙСКИЙ ЛИЗИНГ ООО", _record())


def test_search_in_emitent_field():
    emission = _record(name="СБОЛ-35 об", emitent="ПАО «Сбербанк России»", type="emission")
    assert _search_in("СБОЛ-35", emission)          # по названию выпуска
    assert _search_in("СБЕРБАНК РОССИИ", emission)  # по эмитенту
    assert _search_in("РОССИИ СБЕРБАНК", emission)  # токены по эмитенту
    assert not _search_in("БАЛТИЙСКИЙ", emission)


def test_search_in_company_slug():
    assert _search_in("BALTIYSKIY", _record())
    assert not _search_in("ALFA", _record())


def test_search_in_no_match():
    assert not _search_in("ГАЗПРОМ", _record())


def test_search_in_empty_query():
    # пустой запрос — подстрока «всё в чём угодно»: не фильтрует
    assert _search_in("", _record())


# ────────────────────────── _norm_secname / _strip_tags ──────────────────────────

@pytest.mark.parametrize(
    ("secname", "expected"),
    [
        ("СБЕРБАНК ао", "СБЕРБАНК"),
        ("ГАЗПРОМ ап", "ГАЗПРОМ"),
        ("TCS Group гдр", "TCS GROUP"),
        ("Сургутнефтегаз нр", "СУРГУТНЕФТЕГАЗ"),
        ("ПОЛИЮГ", "ПОЛИЮГ"),  # без суффикса — как есть (нормализованно)
        ("МТС", "МТС"),
    ],
)
def test_norm_secname(secname, expected):
    assert _norm_secname(secname) == expected


def test_norm_secname_normalizes_opf():
    # делегирует _normalize_emitent_name: ОПФ и кавычки убираются
    assert _norm_secname("ПАО «Сбербанк России» ао") == "СБЕРБАНК РОССИИ"


@pytest.mark.parametrize(
    ("html_str", "expected"),
    [
        ("<b>ruAA</b>", "ruAA"),
        ('<a href="/x">ПАО «Т»</a>', "ПАО «Т»"),
        ("текст&nbsp;с пробелами", "текст с пробелами"),
        ("&laquo;Т&raquo;", "«Т»"),
        ("  много   пробелов  ", "много пробелов"),
        ("", ""),
    ],
)
def test_strip_tags(html_str, expected):
    assert _strip_tags(html_str) == expected
