"""Тесты для ru_finance/bonds.py — облигационная математика."""
from datetime import date

import pytest

from ru_finance.bonds import (
    _coupon_dates,
    _coupon_schedule_dict,
    _to_date,
    accrued_interest,
    convexity,
    dirty_price,
    gry,
    macaulay_duration,
    rate_scenarios,
    real_return,
    spread_to_curve,
    synthetic_yield,
    twist_scenarios,
    years_to_maturity,
)


# ═══════════════════════════ helpers ═══════════════════════════


class TestToDate:
    def test_from_date(self):
        d = date(2024, 1, 15)
        assert _to_date(d) is d

    def test_from_str(self):
        assert _to_date("2024-01-15") == date(2024, 1, 15)

    def test_from_datetime_str(self):
        assert _to_date("2024-01-15T10:30:00") == date(2024, 1, 15)


class TestCouponScheduleDict:
    def test_none(self):
        assert _coupon_schedule_dict(None) is None

    def test_empty(self):
        assert _coupon_schedule_dict([]) is None

    def test_parses(self):
        sched = [
            {"date": "2024-06-15", "rate_pct": 7.0},
            {"date": "2024-12-15", "rate_pct": 7.5},
        ]
        result = _coupon_schedule_dict(sched)
        assert result == {
            date(2024, 6, 15): 7.0,
            date(2024, 12, 15): 7.5,
        }


class TestCouponDates:
    def test_frequency_2(self):
        dates = _coupon_dates(date(2024, 1, 1), date(2025, 7, 1), freq=2)
        assert len(dates) >= 2
        for d in dates:
            assert d > date(2024, 1, 1)
            assert d <= date(2025, 7, 1)

    def test_frequency_1(self):
        dates = _coupon_dates(date(2024, 1, 1), date(2026, 7, 1), freq=1)
        assert len(dates) >= 1

    def test_sorted(self):
        dates = _coupon_dates(date(2024, 1, 1), date(2027, 1, 1), freq=4)
        assert dates == sorted(dates)


# ═══════════════════════════ dirty_price ═══════════════════════════


class TestDirtyPrice:
    def test_zero_coupon_at_par(self, valdate, maturity_1y, face):
        """Zero-coupon: цена = face / (1+ytm/freq)^(freq*t)."""
        ytm = 0.0
        p = dirty_price(valdate, maturity_1y, 0.0, ytm, face, freq=2)
        assert abs(p - face) < 0.01

    def test_zero_coupon_discount(self, valdate, maturity_1y, face):
        """Zero-coupon с ytm>0 должен быть ниже номинала."""
        p = dirty_price(valdate, maturity_1y, 0.0, 10.0, face, freq=2)
        assert p < face
        assert p > 0

    def test_coupon_above_zero(self, valdate, maturity_3y, coupon_rate, ytm, face):
        """Купонная облигация — цена выше zero-coupon."""
        p_coupon = dirty_price(valdate, maturity_3y, coupon_rate, ytm, face)
        p_zero = dirty_price(valdate, maturity_3y, 0.0, ytm, face)
        assert p_coupon > p_zero

    def test_inverse_with_ytm(self, valdate, maturity_3y, coupon_rate, face):
        """Чем выше YTM, тем ниже цена."""
        p_low = dirty_price(valdate, maturity_3y, coupon_rate, 5.0, face)
        p_high = dirty_price(valdate, maturity_3y, coupon_rate, 15.0, face)
        assert p_low > p_high

    def test_at_par_when_ytm_eq_coupon(self, valdate, maturity_3y, face):
        """Когда ytm = coupon_rate, грязная цена близка к номиналу + НКД."""
        rate = 8.0
        p = dirty_price(valdate, maturity_3y, rate, rate, face, freq=2)
        ai = accrued_interest(valdate, maturity_3y, rate, face, freq=2)
        # dirty ≈ face + NKD (для freq=2 возможна погрешность ~1% из-за day-count)
        expected = face + ai["accrued_rub"]
        assert abs(p - expected) / expected < 0.05  # ±5%

    def test_with_coupon_schedule(self, valdate, maturity_3y, ytm, face):
        """Кастомное расписание купонов."""
        schedule = [
            {"date": "2024-12-15", "rate_pct": 6.0},
            {"date": "2025-06-15", "rate_pct": 7.0},
            {"date": "2025-12-15", "rate_pct": 7.5},
            {"date": "2026-06-15", "rate_pct": 8.0},
            {"date": "2026-12-15", "rate_pct": 8.5},
            {"date": "2027-06-15", "rate_pct": 9.0},
        ]
        p = dirty_price(valdate, maturity_3y, 7.0, ytm, face, freq=2,
                        coupon_schedule=schedule)
        assert p > 0
        assert p < face * 2  # sanity

    def test_monotonic_in_coupon(self, valdate, maturity_3y, ytm, face):
        """Чем выше купон, тем выше цена (при фиксированной ytm)."""
        p1 = dirty_price(valdate, maturity_3y, 5.0, ytm, face)
        p2 = dirty_price(valdate, maturity_3y, 10.0, ytm, face)
        assert p2 > p1

    def test_short_maturity_close_to_par(self, valdate, face):
        """Короткая бумага (3 мес) с ytm=coupon ≈ par."""
        mat = date(2024, 9, 15)
        rate = 10.0
        # Для короткой бумаги НКД большой, поэтому цена > face
        p = dirty_price(valdate, mat, rate, rate, face, freq=2)
        assert abs(p - face) < face * 0.10  # ±10% (НКД)


# ═══════════════════════════ macaulay_duration ═══════════════════════════


class TestMacaulayDuration:
    def test_zero_coupon_equals_maturity(self, valdate, maturity_3y, face):
        """Zero-coupon duration = time to maturity."""
        dur = macaulay_duration(valdate, maturity_3y, 0.0, 5.0, face)
        expected_years = (maturity_3y - valdate).days / 365
        assert abs(dur - expected_years) < 0.1

    def test_coupon_shorter_than_maturity(self, valdate, maturity_3y,
                                          coupon_rate, ytm, face):
        """Купонная duration < time to maturity."""
        dur = macaulay_duration(valdate, maturity_3y, coupon_rate, ytm, face)
        years = (maturity_3y - valdate).days / 365
        assert dur < years
        assert dur > 0

    def test_positive(self, valdate, maturity_3y, coupon_rate, ytm, face):
        dur = macaulay_duration(valdate, maturity_3y, coupon_rate, ytm, face)
        assert dur > 0

    def test_longer_maturity_longer_duration(self, valdate, coupon_rate, ytm, face):
        """Более длинная бумага → большая duration."""
        mat3 = date(2027, 6, 15)
        mat5 = date(2029, 6, 15)
        d3 = macaulay_duration(valdate, mat3, coupon_rate, ytm, face)
        d5 = macaulay_duration(valdate, mat5, coupon_rate, ytm, face)
        assert d5 > d3


# ═══════════════════════════ convexity ═══════════════════════════


class TestConvexity:
    def test_positive(self, valdate, maturity_3y, coupon_rate, ytm, face):
        cx = convexity(valdate, maturity_3y, coupon_rate, ytm, face)
        assert cx > 0

    def test_longer_maturity_more_convex(self, valdate, coupon_rate, ytm, face):
        mat3 = date(2027, 6, 15)
        mat5 = date(2029, 6, 15)
        c3 = convexity(valdate, mat3, coupon_rate, ytm, face)
        c5 = convexity(valdate, mat5, coupon_rate, ytm, face)
        assert c5 > c3

    def test_duration_convexity_approximation(self, valdate, maturity_3y,
                                               coupon_rate, ytm, face):
        """Формула ΔP/P ≈ −D·Δy + ½·C·(Δy)² — проверка для малого сдвига."""
        mod_dur = macaulay_duration(valdate, maturity_3y, coupon_rate, ytm, face)
        mod_dur = mod_dur / (1 + ytm / 100 / 2)  # modified
        cx = convexity(valdate, maturity_3y, coupon_rate, ytm, face)
        p0 = dirty_price(valdate, maturity_3y, coupon_rate, ytm, face)
        dy = 0.01  # 1 bp
        p1 = dirty_price(valdate, maturity_3y, coupon_rate, ytm + dy * 100, face)
        actual_change = (p1 - p0) / p0
        approx_change = -mod_dur * dy + 0.5 * cx * dy ** 2
        assert abs(actual_change - approx_change) < 0.001


# ═══════════════════════════ accrued_interest ═══════════════════════════


class TestAccruedInterest:
    def test_zero_coupon_no_accrued(self, valdate, maturity_3y, face):
        ai = accrued_interest(valdate, maturity_3y, 0.0, face, freq=2)
        assert ai["accrued_rub"] == 0

    def test_on_coupon_date(self, maturity_3y, coupon_rate, face):
        """В день купона НКД = 0 (день выплаты)."""
        # День купона — maturity
        ai = accrued_interest(maturity_3y, maturity_3y, coupon_rate, face, freq=2)
        assert ai["accrued_rub"] == 0

    def test_mid_period(self, valdate, maturity_3y, coupon_rate, face):
        """В середине периода НКД > 0."""
        ai = accrued_interest(valdate, maturity_3y, coupon_rate, face, freq=2)
        assert ai["accrued_rub"] > 0
        assert ai["accrued_pct"] > 0
        assert ai["days_accrued"] > 0

    def test_max_accrued_near_coupon(self, maturity_3y, coupon_rate, face):
        """НКД растёт в течение купонного периода и обнуляется на дату купона."""
        # Дата сразу после купона (НКД ≈ 1 день)
        from datetime import timedelta
        # Найдём реальные купонные даты
        dates = _coupon_dates(date(2020, 1, 1), maturity_3y, freq=2)
        coupon_day = dates[1]  # какой-то купон
        ai_after = accrued_interest(coupon_day + timedelta(days=3), maturity_3y,
                                     coupon_rate, face, freq=2)
        ai_before = accrued_interest(coupon_day - timedelta(days=3), maturity_3y,
                                      coupon_rate, face, freq=2)
        # Сразу после купона НКД маленький, перед купоном — большой
        assert ai_after["accrued_rub"] < ai_before["accrued_rub"]
        assert ai_after["days_accrued"] < ai_before["days_accrued"]

    def test_fields_present(self, valdate, maturity_3y, coupon_rate, face):
        ai = accrued_interest(valdate, maturity_3y, coupon_rate, face, freq=2)
        assert "accrued_rub" in ai
        assert "accrued_pct" in ai
        assert "days_accrued" in ai
        assert "coupon_period_days" in ai
        assert "last_coupon" in ai
        assert "next_coupon" in ai


# ═══════════════════════════ gry ═══════════════════════════


class TestGRY:
    def test_roundtrip(self, valdate, maturity_3y, coupon_rate, face):
        """gry → dirty_price(gry) ≈ clean + NKD."""
        clean_pct = 100.0  # at par
        result = gry(valdate, maturity_3y, coupon_rate, clean_pct, face)
        gry_pct = result["gry_pct"]
        # Цена по GRY должна быть ≈ dirty = clean + NKD
        dp = dirty_price(valdate, maturity_3y, coupon_rate, gry_pct, face)
        expected_dirty = face * clean_pct / 100 + result["accrued_rub"]
        assert abs(dp - expected_dirty) < 1.0  # ±1 руб (точность binary search)

    def test_gry_below_par_when_premium(self, valdate, maturity_3y, coupon_rate, face):
        """Если цена > 100%, GRY < coupon_rate."""
        result = gry(valdate, maturity_3y, coupon_rate, 105.0, face)
        assert result["gry_pct"] < coupon_rate

    def test_gry_above_par_when_discount(self, valdate, maturity_3y, coupon_rate, face):
        """Если цена < 100%, GRY > coupon_rate."""
        result = gry(valdate, maturity_3y, coupon_rate, 95.0, face)
        assert result["gry_pct"] > coupon_rate


# ═══════════════════════════ real_return ═══════════════════════════


class TestRealReturn:
    def test_zero_inflation(self):
        result = real_return(10.0, inflations=[0])
        assert len(result) == 1
        assert abs(result[0]["real_return_pct"] - 10.0) < 0.1

    def test_fisher_equation(self):
        """Проверка формулы Фишера: (1+y)/(1+i)-1."""
        ytm = 12.0
        infl = 8.0
        result = real_return(ytm, inflations=[infl])
        expected = ((1 + ytm / 100) / (1 + infl / 100) - 1) * 100
        assert abs(result[0]["real_return_pct"] - round(expected, 1)) < 0.01

    def test_higher_inflation_lower_real(self):
        result = real_return(10.0, inflations=[2, 6, 10, 14])
        reals = [r["real_return_pct"] for r in result]
        assert reals == sorted(reals, reverse=True)

    def test_actual_inflation(self):
        result = real_return(10.0, inflations=[4, 6], actual_inflation=5.0)
        assert isinstance(result, dict)
        assert "actual_inflation_pct" in result
        assert "actual_real_return_pct" in result
        assert result["actual_inflation_pct"] == 5.0

    def test_deflation_real_gt_nominal(self):
        """При дефляции реальная > номинальной."""
        result = real_return(5.0, inflations=[-2])
        assert result[0]["real_return_pct"] > 5.0


# ═══════════════════════════ years_to_maturity ═══════════════════════════


class TestYearsToMaturity:
    def test_basic(self):
        y = years_to_maturity("2027-06-15", "2024-06-15")
        assert abs(y - 3.0) < 0.02

    def test_past_returns_zero(self):
        y = years_to_maturity("2020-01-01", "2024-01-01")
        assert y == 0.0

    def test_precision(self):
        y = years_to_maturity("2025-01-01", "2024-01-01")
        assert abs(y - 1.0) < 0.02


# ═══════════════════════════ spread_to_curve ═══════════════════════════


class TestSpreadToCurve:
    def test_positive_spread(self):
        r = spread_to_curve(12.5, 3.0, 12.0)
        assert r["spread_pp"] == 0.5

    def test_negative_spread(self):
        r = spread_to_curve(11.0, 3.0, 12.0)
        assert r["spread_pp"] == -1.0

    def test_fields(self):
        r = spread_to_curve(10.0, 2.0, 9.5)
        assert r["bond_ytm"] == 10.0
        assert r["curve_yield"] == 9.5


# ═══════════════════════════ rate_scenarios ═══════════════════════════


class TestRateScenarios:
    def test_zero_delta(self, maturity_3y, coupon_rate, ytm, face):
        """delta=0 → total_return ≈ купонная доходность за горизонт."""
        result = rate_scenarios(maturity_3y, coupon_rate, ytm,
                                horizon_days=365, face=face, deltas=[0],
                                today="2024-06-15")
        s = result["scenarios"][0]
        # Полный доход за год ≈ купонная доходность (7% от номинала)
        assert s["delta_pp"] == 0
        assert s["total_return_pct"] > 0

    def test_breakeven_positive(self, maturity_3y, coupon_rate, ytm, face):
        """Точка безубытка > 0 (купон компенсирует рост ставки)."""
        result = rate_scenarios(maturity_3y, coupon_rate, ytm,
                                horizon_days=365, face=face,
                                today="2024-06-15")
        assert result["breakeven_yield_rise_pp"] > 0

    def test_scenarios_count(self, maturity_3y, coupon_rate, ytm, face):
        result = rate_scenarios(maturity_3y, coupon_rate, ytm,
                                horizon_days=365, face=face,
                                today="2024-06-15")
        assert len(result["scenarios"]) == 7  # default deltas

    def test_falling_rates_positive(self, maturity_3y, coupon_rate, ytm, face):
        """Снижение ставки → положительный доход."""
        result = rate_scenarios(maturity_3y, coupon_rate, ytm,
                                horizon_days=365, face=face, deltas=[-2],
                                today="2024-06-15")
        assert result["scenarios"][0]["total_return_pct"] > 0

    def test_rising_rates_may_be_negative(self, maturity_3y, coupon_rate, ytm, face):
        """Большой рост ставки → отрицательный доход."""
        result = rate_scenarios(maturity_3y, coupon_rate, ytm,
                                horizon_days=365, face=face, deltas=[5],
                                today="2024-06-15")
        # Для ytm=8, coupon=7, рост +5pp → цена упадёт значительно
        assert result["scenarios"][0]["total_return_pct"] < 0

    def test_fields_present(self, maturity_3y, coupon_rate, ytm, face):
        result = rate_scenarios(maturity_3y, coupon_rate, ytm,
                                horizon_days=365, face=face,
                                today="2024-06-15")
        assert "ytm" in result
        assert "coupon_pct" in result
        assert "maturity" in result
        assert "macaulay_years" in result
        assert "breakeven_yield_rise_pp" in result


# ═══════════════════════════ twist_scenarios ═══════════════════════════


class TestTwistScenarios:
    def test_four_scenarios(self, maturity_3y, coupon_rate, ytm, face):
        result = twist_scenarios(maturity_3y, coupon_rate, ytm, 3.0,
                                 horizon_days=365, face=face, today="2024-06-15")
        names = [s["name"] for s in result]
        assert "steepener" in names
        assert "flattener" in names
        assert "twist_short" in names
        assert "twist_long" in names

    def test_steepener_ne_flattener(self, maturity_3y, coupon_rate, ytm, face):
        result = twist_scenarios(maturity_3y, coupon_rate, ytm, 3.0,
                                 horizon_days=365, face=face, today="2024-06-15")
        by_name = {s["name"]: s for s in result}
        assert by_name["steepener"]["delta_pp"] != by_name["flattener"]["delta_pp"]

    def test_short_duration_weight(self, maturity_3y, coupon_rate, ytm, face):
        """Короткая duration → delta ≈ short конец."""
        result = twist_scenarios(maturity_3y, coupon_rate, ytm, 0.5,
                                 horizon_days=365, face=face, today="2024-06-15")
        by_name = {s["name"]: s for s in result}
        # steepener: short=-1, long=1, weight_long=0.5/5=0.1 → delta ≈ -0.8
        assert by_name["steepener"]["delta_pp"] < 0


# ═══════════════════════════ synthetic_yield ═══════════════════════════


class TestSyntheticYield:
    def test_horizon_beyond_maturity(self, valdate, maturity_1y, coupon_rate, ytm, face):
        """Горизонт > погашения → final=face."""
        result = synthetic_yield(valdate, maturity_1y, coupon_rate, ytm,
                                 horizon_years=2.0, face=face)
        assert result["final_value_rub"] == face

    def test_irr_close_to_ytm(self, valdate, maturity_3y, coupon_rate, ytm, face):
        """При reinvest=ytm IRR ≈ ytm."""
        result = synthetic_yield(valdate, maturity_3y, coupon_rate, ytm,
                                 horizon_years=2.0, reinvest_rate=ytm, face=face)
        assert abs(result["irr_pct"] - ytm) < 1.0  # ±1 п.п.

    def test_positive_coupons(self, valdate, maturity_3y, coupon_rate, ytm, face):
        result = synthetic_yield(valdate, maturity_3y, coupon_rate, ytm,
                                 horizon_years=2.0, face=face)
        assert result["total_coupons_rub"] > 0
        assert result["reinvested_coupons_rub"] > 0

    def test_fields_present(self, valdate, maturity_3y, coupon_rate, ytm, face):
        result = synthetic_yield(valdate, maturity_3y, coupon_rate, ytm,
                                 horizon_years=2.0, face=face)
        assert "irr_pct" in result
        assert "buy_price_rub" in result
        assert "total_return_pct" in result
        assert "coupon_count" in result
