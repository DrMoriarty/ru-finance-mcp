"""Тесты для ru_finance/portfolio.py — parse_assets, _num, _classify (офлайн).

Формат входа parse_assets — НЕ таблица, а маркированные строки:

    refresh date: 27.06.2026
    # ИИС                 ← счёт
    ## Акции              ← класс
    - Сбербанк (SBER): 51 шт. (321,26 ₽ -> 301 ₽)

Весов в формате нет вообще — они считаются в snapshot() из живых цен,
поэтому кейсы «вес ≤ 0» / «сумма весов ≠ 100%» здесь зафиксированы как
«веса не парсятся и не валидируются на этом уровне».
"""
import pytest

from ru_finance import moex
from ru_finance.portfolio import _classify, _num, parse_assets


# ═══════════════════════════ fixtures ═══════════════════════════

PORTFOLIO = """\
refresh date: 27.06.2026
# ИИС
## Акции
- Сбербанк (SBER): 51 шт. (321,26 ₽ -> 301 ₽)
## Облигации
- ОФЗ 26249: 125 шт. (88,837 -> 86,100)
- ГТЛК (RU000A10C6F7): 14 шт. (101,69 -> 100,80)
"""


@pytest.fixture
def parsed() -> list[dict]:
    return parse_assets(PORTFOLIO)


# ═══════════════════════════ parse_assets: корректный вход ═══════════════════════════


class TestParseValid:
    def test_three_positions(self, parsed):
        assert len(parsed) == 3

    def test_fields_share(self, parsed):
        p = parsed[0]
        assert p == {
            "account": "ИИС",
            "class": "Акции",
            "name": "Сбербанк",
            "search_key": "SBER",
            "qty": 51,
            "buy_price": 321.26,
        }

    def test_ofz_number_as_search_key(self, parsed):
        # Нет тикера в скобках — ключом становится номер ОФЗ из названия.
        p = parsed[1]
        assert p["name"] == "ОФЗ 26249"
        assert p["search_key"] == "26249"
        assert p["class"] == "Облигации"
        assert p["qty"] == 125
        # Запятая — десятичный разделитель: 88,837 → 88.837
        assert p["buy_price"] == pytest.approx(88.837)

    def test_isin_as_search_key(self, parsed):
        p = parsed[2]
        assert p["search_key"] == "RU000A10C6F7"
        assert p["qty"] == 14
        assert p["buy_price"] == pytest.approx(101.69)

    def test_account_and_class_carry_to_following_lines(self, parsed):
        assert {p["class"] for p in parsed} == {"Акции", "Облигации"}
        assert {p["account"] for p in parsed} == {"ИИС"}

    def test_current_price_part_ignored(self):
        # После «->» идёт текущая цена — берётся только первая (покупка).
        p = parse_assets("- X (XYZ): 1 шт. (100 -> 200)")[0]
        assert p["buy_price"] == 100.0

    def test_no_weight_column_parsed(self, parsed):
        # Весов в формате нет: ключ weight* в распарсенных позициях отсутствует,
        # веса считаются позже в snapshot() из живых цен.
        assert parsed
        assert all(not any(k.startswith("weight") for k in p) for p in parsed)


# ═══════════════════════════ parse_assets: деградации входа ═══════════════════════════


class TestParseDegenerate:
    def test_empty_text(self):
        assert parse_assets("") == []

    def test_whitespace_only(self):
        assert parse_assets("   \n\t\n") == []

    def test_no_headers(self):
        # Заголовки (# / ##) необязательны — счёт и класс остаются None.
        p = parse_assets("- Сбербанк (SBER): 51 шт. (321,26)")[0]
        assert p["account"] is None
        assert p["class"] is None
        assert p["search_key"] == "SBER"

    def test_headers_without_positions(self):
        # «Пустая таблица»: есть заголовки, нет позиций.
        assert parse_assets("# ИИС\n## Акции\n") == []

    def test_blank_lines_ignored(self):
        p = parse_assets("\n# A\n\n## B\n\n- X (XYZ): 1 шт. (10)\n\n")[0]
        assert p["account"] == "A"
        assert p["class"] == "B"

    def test_refresh_date_line_ignored(self):
        # Строка «refresh date: ...» не позиция и не заголовок — просто пропускается.
        assert parse_assets("refresh date: 27.06.2026") == []
        p = parse_assets("refresh date: 27.06.2026\n- X (XYZ): 1 шт. (10)")[0]
        assert p["search_key"] == "XYZ"

    def test_malformed_line_skipped(self):
        # Нет «шт.» и/или скобок с ценой — строка молча пропускается.
        text = """\
- Сбербанк (SBER): 51 шт.
- Сбербанк (SBER): 51
- просто текст без структуры
- ГТЛК (RU000A10C6F7): 14 шт. (101,69)
"""
        out = parse_assets(text)
        assert len(out) == 1
        assert out[0]["search_key"] == "RU000A10C6F7"

    def test_missing_buy_price_parens_empty(self):
        # «Колонка» цены покупки отсутствует (пустые скобки) → buy_price=None.
        p = parse_assets("- X (XYZ): 1 шт. ()")[0]
        assert p["buy_price"] is None
        assert p["qty"] == 1

    def test_extra_text_inside_price_parens(self):
        # Лишний текст в скобках: берётся первое число.
        p = parse_assets("- X (XYZ): 1 шт. (100,5 ₽ комиссия 0,3%)")[0]
        assert p["buy_price"] == pytest.approx(100.5)

    def test_trailing_text_after_parens_ok(self):
        # Хвост после закрывающей скобки не мешает (регэксп не заякорен справа).
        p = parse_assets("- X (XYZ): 1 шт. (100) ← куплено в 2024")[0]
        assert p["buy_price"] == 100.0

    def test_lowercase_paren_not_ticker(self):
        # Тикер — только ЗАГЛАВНЫЕ буквы/цифры в конце названия.
        p = parse_assets("- Фонд ликвидности (lqdt): 1 шт. (10)")[0]
        assert p["search_key"] != "LQDT"  # скобки не распознаны как тикер


# ═══════════════════════════ parse_assets: дубли и количества ═══════════════════════════


class TestParseDuplicatesAndQty:
    def test_duplicate_tickers_kept_as_is(self):
        # Дедупликации нет: один и тот же тикер дважды → две позиции.
        text = """\
- Сбербанк (SBER): 10 шт. (300)
- Сбербанк (SBER): 5 шт. (310)
"""
        out = parse_assets(text)
        assert len(out) == 2
        assert [p["qty"] for p in out] == [10, 5]

    def test_zero_qty_parsed(self):
        # Количество 0 — валидно, парсится как есть (фиксируем текущее поведение).
        p = parse_assets("- X (XYZ): 0 шт. (100)")[0]
        assert p["qty"] == 0

    def test_negative_qty_line_skipped(self):
        # Отрицательное количество не матчится регэкспом qty=([\\d ]+) — строка пропускается.
        out = parse_assets("- X (XYZ): -5 шт. (100)")
        assert out == []

    def test_negative_buy_price_parsed(self):
        # Отрицательная цена регэкспом _NUM допустима — фиксируем как есть.
        p = parse_assets("- X (XYZ): 1 шт. (-10)")[0]
        assert p["buy_price"] == -10.0

    def test_qty_with_spaces(self):
        p = parse_assets("- X (XYZ): 1 250 шт. (100)")[0]
        assert p["qty"] == 1250


# ═══════════════════════════ _num ═══════════════════════════


class TestNum:
    """_num — строгий float с заменой запятой: на вход приходят только
    совпадения регэкспа _NUM, поэтому всё остальное — исключение."""

    def test_plain(self):
        assert _num("12") == 12.0

    def test_comma_decimal(self):
        assert _num("88,837") == pytest.approx(88.837)

    def test_dot_decimal(self):
        assert _num("88.837") == pytest.approx(88.837)

    def test_negative(self):
        assert _num("-10,5") == -10.5

    def test_thousands_space_raises(self):
        # «1 234,56»: пробел-разделитель тысяч НЕ поддерживается.
        with pytest.raises(ValueError):
            _num("1 234,56")

    def test_percent_raises(self):
        with pytest.raises(ValueError):
            _num("12%")

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            _num("")

    def test_none_raises(self):
        # None.replace → AttributeError (не TypeError и не ValueError).
        with pytest.raises(AttributeError):
            _num(None)

    def test_garbage_raises(self):
        with pytest.raises(ValueError):
            _num("abc")


# ═══════════════════════════ _classify ═══════════════════════════
# _classify чистая (без сети): moex.ETF_BENCHMARK_MAP — статический словарь.


class TestClassify:
    @staticmethod
    def _info(**kw) -> dict:
        base = {"is_bond": False, "secid": "", "type": "", "duration_years": None}
        base.update(kw)
        return base

    def test_equity_common(self):
        pos = {"name": "Сбербанк"}
        assert _classify(pos, self._info(type="common_share", secid="SBER")) == "equity"

    def test_equity_preferred(self):
        pos = {"name": "Сургутнефтегаз"}
        assert _classify(pos, self._info(type="preferred_share", secid="SNGSP")) == "equity"

    def test_corp_bond(self):
        pos = {"name": "ГТЛК"}
        info = self._info(is_bond=True, type="corporate_bond", secid="RU000A10C6F7")
        assert _classify(pos, info) == "corp_bond"

    def test_ofz_by_su_secid_short(self):
        pos = {"name": "ОФЗ 26249"}
        info = self._info(is_bond=True, type="ofz_bond", secid="SU26249RMFS4",
                          duration_years=1.5)
        assert _classify(pos, info) == "ofz_short"

    def test_ofz_long_by_duration(self):
        pos = {"name": "ОФЗ 26249"}
        info = self._info(is_bond=True, type="ofz_bond", secid="SU26249RMFS4",
                          duration_years=5.2)
        assert _classify(pos, info) == "ofz_long"

    def test_ofz_duration_boundary_3y_is_short(self):
        # d > 3 → длинная; ровно 3 → короткая/средняя.
        pos = {"name": "ОФЗ 26212"}
        info = self._info(is_bond=True, type="ofz_bond", secid="SU26212RMFS4",
                          duration_years=3.0)
        assert _classify(pos, info) == "ofz_short"

    def test_ofz_missing_duration_is_short(self):
        pos = {"name": "ОФЗ 26249"}
        info = self._info(is_bond=True, type="ofz_bond", secid="SU26249RMFS4")
        assert _classify(pos, info) == "ofz_short"

    def test_money_market_by_name(self):
        # Денежный рынок определяется по имени ещё до разбора secid.
        for name in ("LQDT — Фонд ликвидности", "Фонд денежного рынка SBMM",
                     "Ликвидность ETF"):
            assert _classify({"name": name}, self._info()) == "money_market"

    def test_equity_fund(self):
        # БПИФ акций: тип не акция, в ETF_BENCHMARK_MAP категория equity_russia → fund_eq.
        pos = {"name": "Тинькофф iMOEX"}
        info = self._info(secid="TMOS", type="etf_fund")
        assert _classify(pos, info) == "fund_eq"

    def test_bond_fund(self):
        pos = {"name": "Фонд гос. облигаций"}
        info = self._info(secid="SBGB", type="etf_fund")
        assert _classify(pos, info) == "bond_fund"

    def test_commodity_fund(self):
        pos = {"name": "Золото"}
        info = self._info(secid="TGLD", type="etf_fund")
        assert _classify(pos, info) == "commodity"

    def test_fx_fund(self):
        # «Валюта» как класс — только валютные фонды (fx-категория карты);
        # прямой валютной корзины в _classify нет.
        for secid in ("TEUR", "TCNY"):
            pos = {"name": secid}
            info = self._info(secid=secid, type="etf_fund")
            assert _classify(pos, info) == "fx_fund"

    def test_unknown_fund_falls_back_to_fund_eq(self):
        # Нет ни типа акции, ни записи в ETF_BENCHMARK_MAP → fund_eq.
        pos = {"name": "Что-то неизвестное"}
        info = self._info(secid="XXXXX", type="exchange_traded_fund")
        assert _classify(pos, info) == "fund_eq"

    def test_money_market_map_entry(self):
        # money_market из карты перекрывает fallback.
        pos = {"name": "Фонд денежного рынка"}
        info = self._info(secid="AKMM", type="etf_fund")
        assert _classify(pos, info) == "money_market"
        assert "AKMM" in moex.ETF_BENCHMARK_MAP  # тест не врёт про источник
