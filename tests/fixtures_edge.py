"""Фикстуры краевых случаев для test_bonds.py / test_rate.py.

conftest.py не меняется: общие фикстуры (nss_params, curve_points) дублируются
здесь с теми же значениями и подключаются в тестовые модули импортом —
импортированная @pytest.fixture затеняет одноимённую из conftest только
в пределах своего модуля, остальные тесты продолжают работать как раньше.
"""
from datetime import date

import pytest

from ru_finance.bonds import _coupon_dates

# ── Условные ставки ЦБ для мока сети в rate.py ──
KEY_RATE = 16.0
RUONIA = 15.8


# ── NSS-параметры КБД (те же значения, что в conftest.py) ──
@pytest.fixture
def nss_params():
    return {
        "B1": 850,    # 0.0850
        "B2": -20,    # -0.0020
        "B3": 100,    # 0.0100
        "T1": 1.5,
        "G1": 50, "G2": -30, "G3": 20, "G4": -10,
        "G5": 5, "G6": -3, "G7": 2, "G8": -1, "G9": 1,
    }


# ── Узлы кривой (те же значения, что в conftest.py) ──
@pytest.fixture
def curve_points():
    return [
        {"years": 0.25, "yield": 16.0},
        {"years": 0.5, "yield": 15.5},
        {"years": 1.0, "yield": 14.8},
        {"years": 2.0, "yield": 14.0},
        {"years": 3.0, "yield": 13.5},
        {"years": 5.0, "yield": 12.8},
        {"years": 7.0, "yield": 12.3},
        {"years": 10.0, "yield": 12.0},
        {"years": 15.0, "yield": 11.8},
        {"years": 20.0, "yield": 11.7},
    ]


# ── Длинная бумага для асимметрии выпуклости на ±300 б.п. ──
@pytest.fixture
def maturity_10y():
    return date(2034, 6, 15)


# ── Расписание с амортизацией тела ──
@pytest.fixture
def amortization_schedule(valdate, maturity_3y):
    """Купон убывает по мере погашения номинала (амортизация ОФЗ-АД).

    Даты берём из _coupon_dates — генератор считает назад от погашения шагами
    ~182 дн., поэтому расписание обязано строиться из него же, иначе даты не
    совпадут с внутренними купонными датами dirty_price (fallback на coupon_rate).
    7 купонов, средняя ставка 10.5% — с ней сравниваем плоский эквивалент.
    """
    rates = [12.0, 11.5, 11.0, 10.5, 10.0, 9.5, 9.0]
    dates = _coupon_dates(valdate, maturity_3y, freq=2)
    assert len(dates) == len(rates)
    return [{"date": str(d), "rate_pct": r} for d, r in zip(dates, rates)]


# ── Локальное (без сети) окружение для rate_expectations / curve_yield ──
@pytest.fixture
def rate_env(monkeypatch, nss_params):
    """Подменяет _curve (NSS-модель из nss_params, узлы не нужны) и ставки ЦБ."""
    from ru_finance import cbr as cbr_mod
    from ru_finance import rate as rate_mod

    def fake_curve(engine="stock"):
        return [], dict(nss_params), "2024-06-15"

    monkeypatch.setattr(rate_mod, "_curve", fake_curve)
    monkeypatch.setattr(cbr_mod, "key_rate", lambda *a, **kw: {"latest": KEY_RATE})
    monkeypatch.setattr(cbr_mod, "ruonia", lambda *a, **kw: {"latest": RUONIA})
    return {"key_rate": KEY_RATE, "ruonia": RUONIA, "as_of": "2024-06-15"}
