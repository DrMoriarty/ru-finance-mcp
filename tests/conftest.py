from datetime import date

import pytest

# ── Даты ──
@pytest.fixture
def valdate():
    return date(2024, 6, 15)


@pytest.fixture
def maturity_3y():
    return date(2027, 6, 15)


@pytest.fixture
def maturity_1y():
    return date(2025, 6, 15)


@pytest.fixture
def maturity_5y():
    return date(2029, 6, 15)


# ── Параметры облигации ──
@pytest.fixture
def coupon_rate():
    return 7.0  # %


@pytest.fixture
def ytm():
    return 8.0  # %


@pytest.fixture
def face():
    return 1000.0


# ── NSS-параметры КБД (условные, но реалистичные) ──
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


# ── Узлы кривой ──
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
