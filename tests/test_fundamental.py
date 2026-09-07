"""Тесты для ru_finance/fundamental.py — _cagr и хелперы."""
import pytest

from ru_finance.fundamental import _cagr, _latest, _previous, _val, _years_sorted, _all_values


# ═══════════════════════════ _cagr ═══════════════════════════


class TestCAGR:
    def test_double_in_one_year(self):
        assert abs(_cagr(100, 200, 1) - 1.0) < 0.001  # 100%

    def test_zero_growth(self):
        assert abs(_cagr(100, 100, 5) - 0.0) < 0.001

    def test_known_cagr(self):
        # 100 → 133.1 за 3 года = 10% CAGR
        result = _cagr(100, 133.1, 3)
        assert abs(result - 0.10) < 0.001

    def test_none_on_negative_first(self):
        assert _cagr(-10, 100, 3) is None

    def test_none_on_zero_first(self):
        assert _cagr(0, 100, 3) is None

    def test_none_on_zero_years(self):
        assert _cagr(100, 200, 0) is None

    def test_none_on_negative_years(self):
        assert _cagr(100, 200, -1) is None

    def test_none_on_none_first(self):
        assert _cagr(None, 100, 3) is None

    def test_none_on_none_last(self):
        assert _cagr(100, None, 3) is None

    def test_half_year(self):
        # 100 → 110 за 0.5 года = 21% CAGR
        result = _cagr(100, 110, 0.5)
        expected = (110 / 100) ** (1 / 0.5) - 1  # 1.1^2 - 1 = 0.21
        assert abs(result - expected) < 0.001


# ═══════════════════════════ _val ═══════════════════════════


class TestVal:
    def test_present(self):
        data = {"revenue": {"values": {"2023": 100}}}
        assert _val(data, "revenue", "2023") == 100.0

    def test_missing_year(self):
        data = {"revenue": {"values": {"2023": 100}}}
        assert _val(data, "revenue", "2024") is None

    def test_missing_field(self):
        data = {}
        assert _val(data, "revenue", "2023") is None

    def test_none_value(self):
        data = {"revenue": {"values": {"2023": None}}}
        assert _val(data, "revenue", "2023") is None

    def test_converts_to_float(self):
        data = {"revenue": {"values": {"2023": 42}}}
        assert _val(data, "revenue", "2023") == 42.0
        assert isinstance(_val(data, "revenue", "2023"), float)


# ═══════════════════════════ _years_sorted ═══════════════════════════


class TestYearsSorted:
    def test_sorted(self):
        fin = {"years": ["2023", "2020", "2022"]}
        assert _years_sorted(fin) == ["2020", "2022", "2023"]

    def test_excludes_ltm(self):
        fin = {"years": ["2020", "LTM", "2023"]}
        assert "LTM" not in _years_sorted(fin)

    def test_empty(self):
        assert _years_sorted({}) == []


# ═══════════════════════════ _latest ═══════════════════════════


class TestLatest:
    def test_prefers_ltm(self):
        fin = {
            "years": ["2022", "2023"],
            "data": {"revenue": {"values": {"2022": 90, "2023": 100, "LTM": 110}}},
        }
        assert _latest(fin, "revenue") == 110.0

    def test_falls_back_to_last_year(self):
        fin = {
            "years": ["2022", "2023"],
            "data": {"revenue": {"values": {"2022": 90, "2023": 100}}},
        }
        assert _latest(fin, "revenue") == 100.0

    def test_none_when_no_data(self):
        assert _latest({}, "revenue") is None


# ═══════════════════════════ _previous ═══════════════════════════


class TestPrevious:
    def test_second_to_last(self):
        fin = {
            "years": ["2020", "2021", "2022", "2023"],
            "data": {"revenue": {"values": {"2020": 70, "2021": 80, "2022": 90, "2023": 100}}},
        }
        assert _previous(fin, "revenue") == 90.0

    def test_none_when_one_year(self):
        fin = {
            "years": ["2023"],
            "data": {"revenue": {"values": {"2023": 100}}},
        }
        assert _previous(fin, "revenue") is None

    def test_none_when_empty(self):
        assert _previous({}, "revenue") is None


# ═══════════════════════════ _all_values ═══════════════════════════


class TestAllValues:
    def test_basic(self):
        fin = {
            "years": ["2022", "2023"],
            "data": {"revenue": {"values": {"2022": 90, "2023": 100}}},
        }
        result = _all_values(fin, "revenue")
        assert result == [("2022", 90.0), ("2023", 100.0)]

    def test_excludes_ltm(self):
        fin = {
            "years": ["2022", "2023", "LTM"],
            "data": {"revenue": {"values": {"2022": 90, "2023": 100, "LTM": 110}}},
        }
        result = _all_values(fin, "revenue")
        years = [y for y, _ in result]
        assert "LTM" not in years

    def test_skips_none(self):
        fin = {
            "years": ["2022", "2023"],
            "data": {"revenue": {"values": {"2022": None, "2023": 100}}},
        }
        result = _all_values(fin, "revenue")
        assert len(result) == 1
        assert result[0] == ("2023", 100.0)

    def test_empty(self):
        assert _all_values({}, "revenue") == []
