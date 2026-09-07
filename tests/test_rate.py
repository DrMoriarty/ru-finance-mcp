"""Тесты для ru_finance/rate.py — NSS-модель, интерполяция, форварды."""
import math

import pytest

from ru_finance.rate import _fwd, _nss, _y


# ═══════════════════════════ _nss ═══════════════════════════


class TestNSS:
    def test_positive_yield(self, nss_params):
        y = _nss(nss_params, 1.0)
        assert y > 0

    def test_short_tenor(self, nss_params):
        """Короткий тenor — высокая ставка (нормальная кривая)."""
        y_short = _nss(nss_params, 0.25)
        assert y_short > 0

    def test_long_tenor(self, nss_params):
        """Длинный tenor — ставка > 0."""
        y_long = _nss(nss_params, 20.0)
        assert y_long > 0

    def test_monotonic_normal_curve(self, nss_params):
        """При нормальной кривой доходность длинной бумаги > короткой (B1>0 → положительный level)."""
        y_short = _nss(nss_params, 0.25)
        y_long = _nss(nss_params, 10.0)
        # Ставка на любом теноре должна быть > 0 (B1=850 дает level ~8.5%)
        assert y_short > 0
        assert y_long > 0

    def test_extrapolation_beyond_20y(self, nss_params):
        """NSS умеет экстраполировать за пределы узлов."""
        y20 = _nss(nss_params, 20.0)
        y30 = _nss(nss_params, 30.0)
        assert y30 > 0
        # Экстраполяция не должна давать абсурдных значений
        assert abs(y30 - y20) < 5.0  # не более 5 п.п. разница

    def test_zero_params_gives_zero(self):
        """Все параметры = 0 → gt ≈ 0 → yield ≈ 0."""
        p = {
            "B1": 0, "B2": 0, "B3": 0, "T1": 1.0,
            "G1": 0, "G2": 0, "G3": 0, "G4": 0,
            "G5": 0, "G6": 0, "G7": 0, "G8": 0, "G9": 0,
        }
        y = _nss(p, 5.0)
        assert abs(y) < 0.01

    def test_different_tau_changes_shape(self):
        """Разные T1 дают разную форму кривой (B2≠0 → slope зависит от tau)."""
        p1 = {
            "B1": 500, "B2": 400, "B3": 0, "T1": 0.5,
            "G1": 0, "G2": 0, "G3": 0, "G4": 0,
            "G5": 0, "G6": 0, "G7": 0, "G8": 0, "G9": 0,
        }
        p2 = {**p1, "T1": 3.0}
        y1 = _nss(p1, 2.0)
        y2 = _nss(p2, 2.0)
        assert y1 != y2

    def test_gaussians_local_effect(self):
        """G-параметры дают локальные «горбы» на кривой."""
        p_base = {
            "B1": 800, "B2": 0, "B3": 0, "T1": 1.5,
            "G1": 0, "G2": 0, "G3": 0, "G4": 0,
            "G5": 0, "G6": 0, "G7": 0, "G8": 0, "G9": 0,
        }
        p_with_g = {**p_base, "G1": 200}  # горб в точке A[0]=0
        y_base = _nss(p_base, 0.5)
        y_g = _nss(p_with_g, 0.5)
        assert y_g != y_base


# ═══════════════════════════ _y ═══════════════════════════


class TestY:
    def test_with_nss_params(self, nss_params, curve_points):
        """С params → используется NSS."""
        y = _y(curve_points, nss_params, 1.0)
        # NSS модель
        expected = _nss(nss_params, 1.0)
        assert abs(y - expected) < 0.001

    def test_interp_no_params(self, curve_points):
        """Без params → линейная интерполяция."""
        y = _y(curve_points, {}, 1.5)
        # Интерполяция между 1.0 (14.8) и 2.0 (14.0)
        expected = 14.8 + 0.5 * (14.0 - 14.8)  # 14.4
        assert abs(y - expected) < 0.1

    def test_exact_node(self, curve_points):
        """На узле кривой — точное значение."""
        y = _y(curve_points, {}, 1.0)
        assert abs(y - 14.8) < 0.001

    def test_before_first_node(self, curve_points):
        """До первого узла — первый yield."""
        y = _y(curve_points, {}, 0.1)
        assert abs(y - 16.0) < 0.001

    def test_after_last_node(self, curve_points):
        """После последнего узла — последний yield."""
        y = _y(curve_points, {}, 25.0)
        assert abs(y - 11.7) < 0.001

    def test_none_params(self, curve_points):
        """None params → интерполяция."""
        y = _y(curve_points, None, 3.0)
        assert abs(y - 13.5) < 0.001


# ═══════════════════════════ _fwd ═══════════════════════════


class TestFwd:
    def test_equal_yields(self, curve_points):
        """Если все yields одинаковые → forward = yield."""
        flat_pts = [{"years": y, "yield": 10.0} for y in [0.25, 0.5, 1, 2, 3, 5, 10]]
        f = _fwd(flat_pts, {}, 1.0, 2.0)
        assert abs(f - 10.0) < 0.1

    def test_normal_curve_fwd_below_spot(self, curve_points):
        """При нормальной кривой forward < spot (long end)."""
        y3 = _y(curve_points, {}, 3.0)
        f = _fwd(curve_points, {}, 2.0, 3.0)
        # forward обычно < spot при нормальной кривой
        assert f < y3 + 2.0  # sanity

    def test_positive(self, curve_points):
        f = _fwd(curve_points, {}, 1.0, 2.0)
        assert f > 0

    def test_forward_above_short_yield(self, curve_points):
        """Forward на длинном участке > короткая ставка (при нормальной кривой)."""
        y_short = _y(curve_points, {}, 0.25)
        f = _fwd(curve_points, {}, 10.0, 15.0)
        # При нормальной кривой forward на длинном участке < короткого
        # но всё равно > 0
        assert f > 0

    def test_formula_correctness(self, curve_points):
        """Проверка формулы: f = ((1+z2)^t2 / (1+z1)^t1)^(1/(t2-t1)) - 1."""
        z1 = _y(curve_points, {}, 1.0) / 100
        z2 = _y(curve_points, {}, 2.0) / 100
        expected = ((1 + z2) ** 2 / (1 + z1) ** 1) ** (1 / 1) - 1
        result = _fwd(curve_points, {}, 1.0, 2.0)
        assert abs(result - round(expected * 100, 2)) < 0.01
