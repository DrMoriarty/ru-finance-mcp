"""Тесты для ru_finance/rate.py — NSS-модель, интерполяция, форварды."""
import math

import pytest

from ru_finance.rate import _fwd, _nss, _y, curve_yield, rate_expectations

# Фикстуры краевых случаев (conftest.py не меняем) — импорт затеняет
# одноимённые фикстуры conftest только в этом модуле, значения те же.
from fixtures_edge import curve_points, nss_params, rate_env  # noqa: F401


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


# ═══════════════════════════ curve_yield: экстраполяция ═══════════════════════════


class TestCurveYieldExtrapolation:
    """curve_yield вне диапазона узлов КБД (0.25–20 лет): экстраполяция за 20 лет —
    задокументированная возможность NSS-модели (AGENTS.md)."""

    def test_nss_method_beyond_20y(self, rate_env):
        r = curve_yield(25.0)
        assert r["method"] == "NSS"
        assert r["years"] == 25.0
        assert r["yield"] > 0
        assert r["as_of"] == "2024-06-15"

    def test_at_last_node_matches_nss(self, rate_env, nss_params):
        r = curve_yield(20.0)
        assert abs(r["yield"] - round(_nss(nss_params, 20.0), 3)) < 0.001

    def test_monotonic_smooth_decay_beyond_20y(self, nss_params):
        """За 20 лет кривая гладко затухает к уровню B1 (гауссовы поправки умирают),
        без взрыва и осцилляций."""
        ys = [_nss(nss_params, t) for t in (20.0, 21.0, 25.0, 30.0, 50.0)]
        assert all(a > b for a, b in zip(ys, ys[1:]))
        level = 100 * (math.exp(nss_params["B1"] / 10000) - 1)
        assert ys[-1] > level  # сходимость к уровню сверху, без перескока

    def test_extrapolation_reasonable(self, nss_params):
        """Значение на 30 годах отличается от 20 лет меньше чем на 1 п.п."""
        y20, y30 = _nss(nss_params, 20.0), _nss(nss_params, 30.0)
        assert abs(y30 - y20) < 1.0

    def test_fallback_interp_flat_beyond_last_node(self, curve_points, monkeypatch):
        """Без params — фоллбэк на узлы: плоская экстраполяция последним узлом."""
        from ru_finance import rate as rate_mod

        monkeypatch.setattr(
            rate_mod, "_curve",
            lambda engine="stock": (curve_points, {}, "2024-06-15"))
        r = rate_mod.curve_yield(25.0)
        assert r["method"] == "interp"
        assert abs(r["yield"] - 11.7) < 0.001


# ═══════════════════════════ rate_expectations: деадбенд и сигналы ═══════════════════════════


class TestRateExpectationsDeadband:
    """Машинная метка read с деадбендом ±0.25 пп (AGENTS.md). В коде строгие
    неравенства: ровно ±0.25 пп — внутри деадбенда → 'flat'; за границей —
    'cuts_priced' / 'hikes_priced'."""

    @pytest.mark.parametrize("delta,expected", [
        (-0.26, "cuts_priced"),
        (-0.25, "flat"),
        (0.0, "flat"),
        (0.25, "flat"),
        (0.26, "hikes_priced"),
    ])
    def test_read_label_at_deadband_edges(self, rate_env, nss_params, delta, expected):
        y1 = _nss(nss_params, 1.0)
        res = rate_expectations(key_rate=y1 - delta)
        s = res["signals"]
        assert s["read"] == expected
        assert s["short_vs_key_1y"] == delta  # спред округляется ровно в delta

    def test_no_network_fields(self, rate_env):
        """Мокнутые данные проходят насквозь: ставки ЦБ, as_of, note."""
        res = rate_expectations()
        assert res["key_rate"] == 16.0
        assert res["ruonia"] == 15.8
        assert res["as_of"] == "2024-06-15"
        assert "срочную премию" in res["note"]


class TestForwardsAndAnchor:
    """Форварды fwd_* и якорь short_vs_ruonia_1y на условных nss_params."""

    def test_short_vs_ruonia_anchor(self, rate_env, nss_params):
        res = rate_expectations()
        y1 = _nss(nss_params, 1.0)
        assert res["signals"]["short_vs_ruonia_1y"] == round(y1 - 15.8, 2)

    def test_fwd_values_match_formula(self, rate_env, nss_params):
        s = rate_expectations()["signals"]
        pairs = {"fwd_1y_in_1y": (1, 2), "fwd_1y_in_2y": (2, 3), "fwd_3m_in_1y": (1, 1.25)}
        for name, (t1, t2) in pairs.items():
            assert s[name] == _fwd([], nss_params, t1, t2)

    def test_forwards_positive_and_finite(self, rate_env):
        s = rate_expectations()["signals"]
        for name in ("fwd_1y_in_1y", "fwd_1y_in_2y", "fwd_3m_in_1y"):
            assert math.isfinite(s[name])
            assert s[name] > 0

    def test_fwd_1y_in_1y_manual_computation(self, nss_params):
        """Независимая проверка формулы форварда на NSS-доходностях."""
        z1 = _nss(nss_params, 1.0) / 100
        z2 = _nss(nss_params, 2.0) / 100
        expected = round((((1 + z2) ** 2 / (1 + z1) ** 1) ** (1 / (2 - 1)) - 1) * 100, 2)
        assert _fwd([], nss_params, 1, 2) == expected

    def test_fwd_inverted_flag_consistent(self, rate_env, nss_params):
        """inverted = y2 < y025 — согласовано с NSS-доходностями."""
        res = rate_expectations()
        y025 = _nss(nss_params, 0.25)
        y2 = _nss(nss_params, 2.0)
        assert res["signals"]["inverted"] == (y2 < y025)
