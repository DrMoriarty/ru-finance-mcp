"""Тесты для утилит ru_finance/moex.py — _median, _freq_from_period, _auto_interval."""
import pytest

from ru_finance.moex import _auto_interval, _freq_from_period, _median


# ═══════════════════════════ _median ═══════════════════════════


class TestMedian:
    def test_odd_count(self):
        assert _median([3, 1, 2]) == 2

    def test_even_count(self):
        assert _median([1, 2, 3, 4]) == 2.5  # (2+3)/2

    def test_single(self):
        assert _median([42]) == 42

    def test_empty(self):
        assert _median([]) == 0.0

    def test_unsorted(self):
        assert _median([10, 1, 5]) == 5

    def test_two_elements(self):
        assert _median([10, 20]) == 15.0

    def test_floats(self):
        assert abs(_median([1.5, 2.5, 3.5]) - 2.5) < 0.001

    def test_negative(self):
        assert _median([-5, -1, -3]) == -3


# ═══════════════════════════ _freq_from_period ═══════════════════════════


class TestFreqFromPeriod:
    def test_none(self):
        assert _freq_from_period(None) == 2

    def test_zero(self):
        assert _freq_from_period(0) == 2

    def test_negative(self):
        assert _freq_from_period(-10) == 2

    def test_annual(self):
        assert _freq_from_period(365) == 1

    def test_semiannual_182(self):
        assert _freq_from_period(182) == 2

    def test_semiannual_183(self):
        assert _freq_from_period(183) == 2

    def test_quarterly_91(self):
        assert _freq_from_period(91) == 4

    def test_quarterly_92(self):
        assert _freq_from_period(92) == 4

    def test_bimonthly_61(self):
        assert _freq_from_period(61) == 6

    def test_monthly_30(self):
        assert _freq_from_period(30) == 12

    def test_monthly_31(self):
        assert _freq_from_period(31) == 12

    def test_non_canonical(self):
        # 150 дней → round(365/150)=2
        assert _freq_from_period(150) == 2

    def test_non_canonical_short(self):
        # 45 дней → round(365/45)=8
        assert _freq_from_period(45) == 8

    def test_clamp_max(self):
        # Очень короткий период → clamp до 12
        assert _freq_from_period(5) == 12

    def test_clamp_min(self):
        # Очень длинный период → clamp до 1
        assert _freq_from_period(1000) == 1


# ═══════════════════════════ _auto_interval ═══════════════════════════


class TestAutoInterval:
    def test_short_period(self):
        # 30 дней → target=0.6 < 1 → "24" (день)
        assert _auto_interval("2024-01-01", "2024-01-31") == "24"

    def test_month(self):
        # 60 дней → target=1.2 ≥ 1 → "24"
        assert _auto_interval("2024-01-01", "2024-03-01") == "24"

    def test_quarter(self):
        # 90 дней → target=1.8 ≥ 1 → "24"
        assert _auto_interval("2024-01-01", "2024-04-01") == "24"

    def test_year(self):
        # 365 дней → target=7.3 ≥ 7 → "7" (неделя)
        assert _auto_interval("2024-01-01", "2025-01-01") == "7"

    def test_multi_year(self):
        # 1000 дней → target=20 ≥ 7 → "7" (неделя)
        # или "31" если target ≥ 30
        result = _auto_interval("2024-01-01", "2026-10-01")
        assert result in ("7", "31", "4")

    def test_invalid_returns_60(self):
        # till < frm → days ≤ 0
        assert _auto_interval("2025-01-01", "2024-01-01") == "60"

    def test_same_day(self):
        # 0 дней → days ≤ 0
        assert _auto_interval("2024-01-01", "2024-01-01") == "60"

    def test_returns_string(self):
        result = _auto_interval("2024-01-01", "2024-06-01")
        assert isinstance(result, str)

    def test_5_years(self):
        # ~1826 дней → target=36.5 ≥ 30 → "31" (месяц)
        assert _auto_interval("2024-01-01", "2029-01-01") == "31"
