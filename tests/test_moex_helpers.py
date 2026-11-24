"""Тесты чистых хелперов ru_finance/moex.py: резолв рынков, интервалы,
match по рейтингу/сектору, парсинг строк ISS-бордов, сборка опционной доски.
Все тесты локальные — сеть не трогают (кроме _match_sector, где карта
секторов подменяется фикстурой).
"""
import pytest

import ru_finance.raexpert as raexpert
from ru_finance.moex import (
    _build_optionboard,
    _canonical_asset,
    _engine_market,
    _estimate_candle_count,
    _match_rating,
    _match_sector,
    _parse_board_bond,
    _parse_board_etf,
)


# ═══════════════════════════ _engine_market ═══════════════════════════


class TestEngineMarket:
    def test_default_on_none(self):
        assert _engine_market(None) == ("stock", "shares")

    def test_default_on_empty(self):
        assert _engine_market("") == ("stock", "shares")

    @pytest.mark.parametrize("group,expected", [
        ("stock_shares", ("stock", "shares")),
        ("stock_bonds", ("stock", "bonds")),
        ("stock_index", ("stock", "index")),
        ("stock_selt", ("stock", "selt")),
    ])
    def test_known_groups(self, group, expected):
        assert _engine_market(group) == expected

    @pytest.mark.parametrize("group", ["stock_etf", "stock_ppif", "stock_dr"])
    def test_share_like_suffixes_map_to_shares(self, group):
        assert _engine_market(group) == ("stock", "shares")

    @pytest.mark.parametrize("group", ["forts_forts", "forts_futures"])
    def test_futures_suffixes_map_to_forts(self, group):
        assert _engine_market(group) == ("forts", "forts")

    def test_unknown_suffix_passes_through(self):
        # Неизвестный суффикс не маппится — возвращается как есть
        assert _engine_market("commodity_metals") == ("commodity", "metals")

    def test_no_suffix_defaults_to_shares(self):
        assert _engine_market("stock") == ("stock", "shares")

    def test_trailing_underscore_gives_raw_empty_suffix(self):
        # "" не в _MARKET — проходит как есть
        assert _engine_market("stock_") == ("stock", "")


# ═══════════════════════════ _canonical_asset ═══════════════════════════


class TestCanonicalAsset:
    @pytest.mark.parametrize("code,expected", [
        ("si", "Si"), ("SI", "Si"), ("Si", "Si"),
        ("eu", "Eu"), ("EU", "Eu"),
        ("cny", "CNY"), ("CNY", "CNY"),
        ("gold", "GOLD"), ("gl", "GL"),
        ("br", "BR"),
        ("rts", "RTS"), ("imoex", "IMOEX"),
        ("sbrf", "SBRF"), ("gazr", "GAZR"),
        ("nlnk", "NlNK"),  # смешанный регистр в карте
    ])
    def test_case_insensitive_normalization(self, code, expected):
        assert _canonical_asset(code) == expected

    def test_unknown_code_unchanged(self):
        assert _canonical_asset("XYZQ") == "XYZQ"

    def test_unknown_lowercase_unchanged(self):
        assert _canonical_asset("zzz") == "zzz"

    def test_already_canonical(self):
        assert _canonical_asset("GOLD") == "GOLD"


# ═══════════════════════════ _estimate_candle_count ═══════════════════════════


class TestEstimateCandleCount:
    FRM = "2024-01-01"
    TILL = "2024-01-11"  # 10 дней

    def test_daily_interval(self):
        assert _estimate_candle_count(self.FRM, self.TILL, "24") == 10

    def test_hourly_interval(self):
        assert _estimate_candle_count(self.FRM, self.TILL, "60") == 140

    def test_tenmin_interval(self):
        assert _estimate_candle_count(self.FRM, self.TILL, "10") == 1000

    def test_weekly_interval_floors(self):
        # 10 / 7 -> 1
        assert _estimate_candle_count(self.FRM, self.TILL, "7") == 1

    def test_monthly_interval_floors_to_zero(self):
        # 10 / 31 -> 0
        assert _estimate_candle_count(self.FRM, self.TILL, "31") == 0

    def test_quarterly_interval_floors_to_zero(self):
        assert _estimate_candle_count(self.FRM, self.TILL, "4") == 0

    def test_unknown_interval_defaults_to_daily(self):
        assert _estimate_candle_count(self.FRM, self.TILL, "99") == 10

    def test_same_day_clamped_to_one(self):
        # frm == till: days = 0 -> max(0, 1) = 1
        assert _estimate_candle_count(self.FRM, self.FRM, "24") == 1

    def test_inverted_range_clamped_to_one(self):
        # frm > till: отрицательные дни зажимаются к 1
        assert _estimate_candle_count(self.TILL, self.FRM, "24") == 1
        assert _estimate_candle_count(self.TILL, self.FRM, "60") == 14

    def test_long_period_monthly(self):
        # 365 дней / 31 -> 11
        assert _estimate_candle_count("2023-01-01", "2024-01-01", "31") == 11


# ═══════════════════════════ _match_rating ═══════════════════════════


_RATINGS = [
    {"type": "emitent", "name": "ПАО «Сбербанк России»", "rating": "ruAAA"},
    {"type": "emitent", "name": "ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО ГАЗПРОМ", "rating": "ruAAA"},
    {"type": "bond", "name": "Сбербанк", "rating": "ruAA+"},  # не эмитент — должен игнорироваться
    {"type": "emitent", "name": "ПАО НЛМК", "rating": "ruAA+"},
]


class TestMatchRating:
    def test_exact_normalized_match(self):
        assert _match_rating("ПАО Сбербанк", _RATINGS) == "ruAAA"

    def test_partial_substring_match(self):
        # "Сбербанк" входит в нормализованное "СБЕРБАНК РОССИИ"
        assert _match_rating("Сбербанк", _RATINGS) == "ruAAA"

    def test_full_form_vs_short_name(self):
        assert _match_rating("ПУБЛИЧНОЕ АКЦИОНЕРНОЕ ОБЩЕСТВО ГАЗПРОМ", _RATINGS) == "ruAAA"

    def test_reverse_substring(self):
        # Нормализованное имя рейтинга входит в имя эмитента из запроса
        assert _match_rating("НЛМК ПАО", _RATINGS) == "ruAA+"

    def test_no_match(self):
        assert _match_rating("Магнит", _RATINGS) is None

    def test_empty_name(self):
        assert _match_rating("", _RATINGS) is None
        assert _match_rating(None, _RATINGS) is None

    def test_empty_ratings(self):
        assert _match_rating("Сбербанк", []) is None

    def test_bond_type_rows_skipped(self):
        # Строка type="bond" с тем же именем не должна отдать свой рейтинг
        assert _match_rating("Сбербанк", [{"type": "bond", "name": "Сбербанк", "rating": "ruC"}]) is None


# ═══════════════════════════ _match_sector ═══════════════════════════


class TestMatchSector:
    @pytest.fixture
    def sector_map(self, monkeypatch):
        """Подменяем кеш карты секторов — без сети."""
        monkeypatch.setattr(
            raexpert, "_cache_sector_map",
            {"СБЕРБАНК": "financial", "ГАЗПРОМ": "oil_gas"},
        )

    def test_exact_match(self, sector_map):
        assert _match_sector("Сбербанк") == "financial"

    def test_match_with_opf_and_quotes(self, sector_map):
        # ОПФ и кавычки убираются нормализацией
        assert _match_sector("ПАО «Сбербанк»") == "financial"

    def test_partial_match(self, sector_map):
        assert _match_sector("ГАЗПРОМ ПАО") == "oil_gas"

    def test_no_match(self, sector_map):
        assert _match_sector("Незнакомец") is None

    def test_empty_name(self, sector_map):
        assert _match_sector("") is None

    def test_empty_map_returns_none(self, monkeypatch):
        monkeypatch.setattr(raexpert, "_cache_sector_map", {})
        assert _match_sector("Сбербанк") is None


# ═══════════════════════════ _parse_board_bond ═══════════════════════════


class TestParseBoardBond:
    def test_full_row(self):
        row = {
            "SECID": "SU26238RMFS4", "SHORTNAME": "ОФЗ 26238", "ISIN": "RU000A1038V6",
            "SECNAME": "ОФЗ-ПД 26238", "LAST": 98.5, "YIELD": 11.5,
            "DURATION": 365, "COUPONPERIOD": 182, "FACEUNIT": "SUR", "FACEVALUE": 1000,
            "BONDTYPE": "ОФЗ-ПД", "MATDATE": "2041-05-15",
            "OFFERDATE": "0000-00-00", "PUTOPTIONDATE": "0000-00-00",
            "CALLOPTIONDATE": "0000-00-00", "ACCRUEDINT": 12.34,
            "ISSUESIZE": 1_000_000, "ISSUESIZEPLACED": 900_000,
            "VALTODAY": 5_000_000, "VOLTODAY": 50_000, "NUMTRADES": 120,
            "BID": 98.4, "OFFER": 98.6, "LISTLEVEL": 1,
        }
        b = _parse_board_bond(row, "TQOB")
        assert b["secid"] == "SU26238RMFS4"
        assert b["board"] == "TQOB"
        assert b["price_pct"] == 98.5          # приоритет LAST
        assert b["ytm"] == 11.5
        assert b["duration_years"] == 1.0      # 365 / 365
        assert b["mod_duration_years"] == 0.95  # 1.0 / (1 + 11.5/100/2)
        assert b["coupon_freq"] == 2           # 182 дня
        assert b["coupon_period_days"] == 182
        assert b["maturity"] == "2041-05-15"
        assert b["years_to_maturity"] > 0
        assert b["offer_date"] is None
        assert b["has_offer"] is False
        assert b["is_amortization"] is False
        assert b["face_unit"] == "SUR"
        assert b["accrued_int"] == 12.34
        assert b["bid_ask_spread_pct"] == pytest.approx(0.203, abs=0.001)  # (98.6-98.4)/98.5*100
        assert b["value_today"] == 5_000_000
        assert b["num_trades"] == 120

    def test_price_fallback_chain(self):
        # LAST отсутствует -> WAPRICE
        row = {"SECID": "X", "WAPRICE": 95.0, "YIELD": 10.0}
        assert _parse_board_bond(row, "TQOB")["price_pct"] == 95.0
        # нет ни LAST, ни WAPRICE -> LCLOSEPRICE
        row = {"SECID": "X", "LCLOSEPRICE": 94.0}
        assert _parse_board_bond(row, "TQOB")["price_pct"] == 94.0
        # только MARKETPRICE
        row = {"SECID": "X", "MARKETPRICE": 93.0}
        assert _parse_board_bond(row, "TQOB")["price_pct"] == 93.0

    def test_ytm_fallback_to_yieldatwaprice(self):
        row = {"SECID": "X", "YIELDATWAPRICE": 12.25}
        assert _parse_board_bond(row, "TQOB")["ytm"] == 12.25

    def test_zeroed_offer_dates_are_inactive(self):
        row = {"SECID": "X", "OFFERDATE": "0000-00-00", "PUTOPTIONDATE": "2027-06-10"}
        b = _parse_board_bond(row, "TQOB")
        assert b["has_offer"] is True
        assert b["offer_date"] == "2027-06-10"

    def test_real_offer_date_wins_earliest(self):
        row = {"SECID": "X", "OFFERDATE": "2028-01-01", "CALLOPTIONDATE": "2027-01-01"}
        b = _parse_board_bond(row, "TQOB")
        assert b["offer_date"] == "2027-01-01"

    def test_missing_fields_yield_nones(self):
        b = _parse_board_bond({"SECID": "X"}, "TQOB")
        assert b["price_pct"] is None
        assert b["ytm"] is None
        assert b["duration_years"] is None
        assert b["mod_duration_years"] is None
        assert b["coupon_freq"] == 2            # дефолт _freq_from_period(None)
        assert b["face_unit"] == "SUR"          # дефолт
        assert b["years_to_maturity"] is None
        assert b["bid_ask_spread_pct"] is None
        assert b["bond_type"] == ""
        assert b["is_amortization"] is False

    def test_empty_row_still_returns_dict(self):
        b = _parse_board_bond({}, "TQOB")
        assert isinstance(b, dict)
        assert b["secid"] is None

    def test_bad_maturity_does_not_crash(self):
        row = {"SECID": "X", "MATDATE": "not-a-date"}
        assert _parse_board_bond(row, "TQOB")["years_to_maturity"] is None

    def test_amortization_flag(self):
        row = {"SECID": "X", "BONDTYPE": "Амортизируемые облигации"}
        assert _parse_board_bond(row, "TQOB")["is_amortization"] is True

    def test_invalid_spread_ignored(self):
        # offer <= bid -> спред не считается
        row = {"SECID": "X", "BID": 100.0, "OFFER": 99.0}
        assert _parse_board_bond(row, "TQOB")["bid_ask_spread_pct"] is None


# ═══════════════════════════ _parse_board_etf ═══════════════════════════


class TestParseBoardEtf:
    def test_full_row(self):
        row = {
            "SECID": "TMOS", "SHORTNAME": "Тинькофф iMOEX", "ISIN": "IE00BM67R935",
            "_board": "TQIF", "LAST": 7.85, "BID": 7.84, "OFFER": 7.86,
            "VALUE": 12_345_678, "VOLUME": 1_500_000, "LASTCHANGE": 0.15,
            "OPEN": 7.8, "LOW": 7.7, "HIGH": 7.9, "LOTSIZE": 1,
            "FACEVALUE": 1.0, "FACEUNIT": "USD", "EMITENT_ID": 900,
            "EMITENT_TITLE": "Тинькофф Капитал", "LISTLEVEL": 1,
            "ISQUALIFIEDINVESTORS": 0,
        }
        e = _parse_board_etf(row)
        assert e["secid"] == "TMOS"
        assert e["board"] == "TQIF"
        assert e["price"] == 7.85              # LAST — первый приоритет
        assert e["bid"] == 7.84
        assert e["ask"] == 7.86
        assert e["spread_pct"] == pytest.approx(0.2548, abs=0.001)  # 0.02 / 7.85 * 100
        assert e["change_pct"] == 0.15
        assert e["value_today"] == 12_345_678
        assert e["vol_today"] == 1_500_000
        assert e["emitent_title"] == "Тинькофф Капитал"
        assert e["face_unit"] == "USD"

    def test_no_secid_returns_none(self):
        assert _parse_board_etf({"LAST": 10.0}) is None
        assert _parse_board_etf({}) is None
        assert _parse_board_etf({"SECID": ""}) is None

    def test_price_fallback_chain(self):
        # LAST нет -> MARKETPRICE
        assert _parse_board_etf({"SECID": "X", "MARKETPRICE": 5.0})["price"] == 5.0
        # MARKETPRICE нет -> LCLOSEPRICE
        assert _parse_board_etf({"SECID": "X", "LCLOSEPRICE": 4.9})["price"] == 4.9
        # только WAPRICE
        assert _parse_board_etf({"SECID": "X", "WAPRICE": 4.8})["price"] == 4.8

    def test_missing_bid_or_ask_no_spread(self):
        assert _parse_board_etf({"SECID": "X", "BID": 10.0})["spread_pct"] is None
        assert _parse_board_etf({"SECID": "X", "OFFER": 10.0})["spread_pct"] is None
        assert _parse_board_etf({"SECID": "X", "BID": 0, "OFFER": 10.0})["spread_pct"] is None

    def test_value_fallback_to_valtoday(self):
        row = {"SECID": "X", "VALTODAY": 111, "VOLTODAY": 22, "CHANGE": 0.5}
        e = _parse_board_etf(row)
        assert e["value_today"] == 111
        assert e["vol_today"] == 22
        assert e["change_pct"] == 0.5

    def test_missing_fields_yield_nones(self):
        e = _parse_board_etf({"SECID": "X"})
        assert e["price"] is None
        assert e["bid"] is None
        assert e["ask"] is None
        assert e["spread_pct"] is None
        assert e["shortname"] is None
        assert e["isin"] is None
        assert e["board"] is None
        assert e["value_today"] is None
        assert e["list_level"] is None


# ═══════════════════════════ _build_optionboard ═══════════════════════════


def _iss_block(columns, data):
    return {"columns": columns, "data": data}


class TestBuildOptionboard:
    def test_full_raw(self):
        raw = {
            "asset": _iss_block(
                ["central_strike", "underlying_settle", "last_del_date"],
                [[100_000, 100_500.0, "2025-06-15"]]),
            "call": _iss_block(
                ["SECID", "STRIKE", "VOLAT", "LAST", "THEORPRICE", "BID", "OFFER",
                 "OPENPOSITION", "VOLTODAY"],
                [["RI250615C100000", 100_000, 0.35, 2500.0, 2450.0, 2400.0, 2500.0, 1500, 300]]),
            "put": _iss_block(
                ["SECID", "STRIKE", "VOLAT", "LAST", "THEORPRICE", "BID", "OFFER",
                 "OPENPOSITION", "VOLTODAY"],
                [["RI250615P100000", 100_000, 0.38, 2300.0, 2280.0, 2250.0, 2350.0, 1200, 250]]),
        }
        board = _build_optionboard(raw)
        assert board["asset_info"] == {
            "central_strike": 100_000,
            "underlying_settle": 100_500.0,
            "last_del_date": "2025-06-15",
        }
        assert len(board["calls"]) == 1
        call = board["calls"][0]
        assert call["secid"] == "RI250615C100000"
        assert call["strike"] == 100_000
        assert call["iv"] == 0.35
        assert call["theor_price"] == 2450.0
        assert call["oi"] == 1500
        assert call["volume"] == 300
        assert len(board["puts"]) == 1
        put = board["puts"][0]
        assert put["secid"] == "RI250615P100000"
        assert put["bid"] == 2250.0

    def test_missing_blocks_give_empty_board(self):
        board = _build_optionboard({})
        assert board == {"asset_info": {}, "calls": [], "puts": []}

    def test_only_calls(self):
        raw = {
            "call": _iss_block(["SECID", "STRIKE"], [["C1", 100]]),
        }
        board = _build_optionboard(raw)
        assert board["asset_info"] == {}
        assert len(board["calls"]) == 1
        assert board["puts"] == []

    def test_empty_data_lists(self):
        raw = {
            "call": _iss_block(["SECID"], []),
            "put": _iss_block(["SECID"], []),
        }
        board = _build_optionboard(raw)
        assert board["calls"] == []
        assert board["puts"] == []
        assert board["asset_info"] == {}

    def test_extra_columns_ignored(self):
        raw = {
            "call": _iss_block(["SECID", "STRIKE", "EXTRA"], [["C1", 100, "ignore-me"]]),
        }
        call = _build_optionboard(raw)["calls"][0]
        assert call == {"secid": "C1", "strike": 100, "iv": None, "last": None,
                        "theor_price": None, "bid": None, "offer": None,
                        "oi": None, "volume": None}
