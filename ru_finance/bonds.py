"""Облигационная математика: цена из денежных потоков, дюрация, сценарии по ставке.

Считаем то, чего нет в либах: реакцию цены/полного дохода на сдвиг доходности
и реальную доходность к погашению при разной инфляции. Полугодовые купоны ОФЗ.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta


def _to_date(d) -> date:
    if isinstance(d, date):
        return d
    return datetime.strptime(str(d)[:10], "%Y-%m-%d").date()


def _coupon_schedule_dict(coupon_schedule: list[dict] | None) -> dict[date, float] | None:
    """Строит словарь {дата_купона -> ставка_%} из расписания.

    coupon_schedule — [{date: 'YYYY-MM-DD', rate_pct: float}, ...].
    """
    if not coupon_schedule:
        return None
    out: dict[date, float] = {}
    for s in coupon_schedule:
        d = _to_date(s["date"])
        out[d] = float(s["rate_pct"])
    return out if out else None


def _coupon_dates(valdate: date, maturity: date, freq: int = 2) -> list[date]:
    valdate = _to_date(valdate)
    maturity = _to_date(maturity)
    step = round(365 / freq)
    out, d = [], maturity
    while d > valdate:
        out.append(d)
        d = d - timedelta(days=step)
    return sorted(out)


def dirty_price(valdate: date, maturity: date, coupon_rate: float,
                ytm: float, face: float = 1000.0, freq: int = 2,
                coupon_schedule: list[dict] | None = None) -> float:
    """Грязная цена облигации (в рублях номинала) при заданной YTM, %.

    coupon_schedule — [{date, rate_pct}, ...]. Если передан, купоны per-period.
    """
    valdate = _to_date(valdate)
    maturity = _to_date(maturity)
    sched = _coupon_schedule_dict(coupon_schedule)
    c = coupon_rate / freq / 100 * face
    p = 0.0
    for d in _coupon_dates(valdate, maturity, freq):
        t = (d - valdate).days / 365
        c_period = sched.get(d, coupon_rate) / freq / 100 * face if sched else c
        cf = c_period + (face if d == maturity else 0)
        p += cf / (1 + ytm / 100 / freq) ** (freq * t)
    return p


def macaulay_duration(valdate: date, maturity: date, coupon_rate: float,
                      ytm: float, face: float = 1000.0, freq: int = 2,
                      coupon_schedule: list[dict] | None = None) -> float:
    """Дюрация Маколея в годах (из денежных потоков).

    coupon_schedule — [{date, rate_pct}, ...]. Если передан, купоны per-period.
    """
    valdate = _to_date(valdate)
    maturity = _to_date(maturity)
    sched = _coupon_schedule_dict(coupon_schedule)
    c = coupon_rate / freq / 100 * face
    pv_tot = w_tot = 0.0
    for d in _coupon_dates(valdate, maturity, freq):
        t = (d - valdate).days / 365
        c_period = sched.get(d, coupon_rate) / freq / 100 * face if sched else c
        cf = c_period + (face if d == maturity else 0)
        pv = cf / (1 + ytm / 100 / freq) ** (freq * t)
        pv_tot += pv
        w_tot += t * pv
    return round(w_tot / pv_tot, 2) if pv_tot else 0.0


def rate_scenarios(maturity, coupon_rate: float, ytm: float,
                   horizon_days: int = 365, face: float = 1000.0,
                   deltas=(-3, -2, -1, 0, 1, 2, 3), today: str | None = None,
                   freq: int = 2,
                   coupon_schedule: list[dict] | None = None) -> dict:
    """Полный доход за горизонт при сдвиге доходности на delta п.п.

    Возвращает по каждому delta: total_return_pct = (цена_конца + купоны - цена_старта)/старт.
    Плюс точку безубытка по росту доходности.
    coupon_schedule — [{date, rate_pct}, ...]. Если передан, купоны per-period.
    """
    t0 = _to_date(today) if today else date.today()
    mat = _to_date(maturity)
    t1 = t0 + timedelta(days=horizon_days)
    d0 = dirty_price(t0, mat, coupon_rate, ytm, face, freq, coupon_schedule)

    if coupon_schedule:
        total_coupon = 0.0
        for s in coupon_schedule:
            d = _to_date(s["date"])
            if t0 < d <= t1:
                total_coupon += s["rate_pct"] / 100 * face / freq
    else:
        total_coupon = coupon_rate / 100 * face * (horizon_days / 365)

    out = []
    for dy in deltas:
        d1 = dirty_price(t1, mat, coupon_rate, ytm + dy, face, freq, coupon_schedule)
        tr = (d1 + total_coupon - d0) / d0 * 100
        out.append({"delta_pp": dy, "total_return_pct": round(tr, 1)})
    lo, hi = 0.0, 100.0
    for _ in range(60):
        mid = (lo + hi) / 2
        tr = (dirty_price(t1, mat, coupon_rate, ytm + mid, face, freq, coupon_schedule)
              + total_coupon - d0) / d0
        if tr > 0:
            lo = mid
        else:
            hi = mid
    return {
        "ytm": ytm, "coupon_pct": coupon_rate, "maturity": str(mat),
        "horizon_days": horizon_days,
        "macaulay_years": macaulay_duration(t0, mat, coupon_rate, ytm, face, freq,
                                            coupon_schedule),
        "scenarios": out,
        "breakeven_yield_rise_pp": round(lo, 2),
    }


def real_return(ytm: float, inflations=(4, 6, 8, 10, 12, 14, 16),
                actual_inflation: float | None = None) -> dict | list[dict]:
    """Реальная доходность к погашению при разных допущениях по инфляции.

    actual_inflation — фактический уровень CPI (% г/г). Если передан, возвращает dict:
    {actual_inflation_pct, actual_real_return_pct, scenarios: [{inflation_pct, real_return_pct}, ...]}.
    Без actual_inflation — только list[{inflation_pct, real_return_pct}] (совместимость).
    """
    scenarios = [{"inflation_pct": i,
                  "real_return_pct": round(((1 + ytm / 100) / (1 + i / 100) - 1) * 100, 1)}
                 for i in inflations]
    if actual_inflation is None:
        return scenarios
    return {
        "actual_inflation_pct": actual_inflation,
        "actual_real_return_pct": round(
            ((1 + ytm / 100) / (1 + actual_inflation / 100) - 1) * 100, 1),
        "scenarios": scenarios,
    }


def convexity(valdate: date, maturity: date, coupon_rate: float,
              ytm: float, face: float = 1000.0, freq: int = 2,
              coupon_schedule: list[dict] | None = None) -> float:
    """Конвексность облигации (вторая производная цены по доходности, нормированная).

    Поправка к duration при больших сдвигах ставки:
    ΔP/P ≈ −D·Δy + ½·C·(Δy)²    (D — модиф. дюрация, C — конвексность).
    coupon_schedule — [{date, rate_pct}, ...]. Если передан, купоны per-period.
    """
    valdate = _to_date(valdate)
    maturity = _to_date(maturity)
    sched = _coupon_schedule_dict(coupon_schedule)
    c = coupon_rate / freq / 100 * face
    r = ytm / 100 / freq
    pv_tot = cx_tot = 0.0
    for d in _coupon_dates(valdate, maturity, freq):
        t = (d - valdate).days / 365
        c_period = sched.get(d, coupon_rate) / freq / 100 * face if sched else c
        cf = c_period + (face if d == maturity else 0)
        pv = cf / (1 + r) ** (freq * t)
        pv_tot += pv
        cx_tot += t * (t + 1 / freq) * pv
    return round(cx_tot / pv_tot / (1 + r) ** 2, 2) if pv_tot else 0.0


def accrued_interest(valdate: date, maturity: date, coupon_rate: float,
                     face: float = 1000.0, freq: int = 2,
                     last_coupon: date | str | None = None,
                     coupon_schedule: list[dict] | None = None) -> dict:
    """Накопленный купонный доход (НКД) по облигации.

    last_coupon — дата последнего купона (если None, определяется автоматически
    из списка купонных дат). Возвращает {accrued_rub, accrued_pct, days_accrued,
    coupon_period_days, last_coupon, next_coupon}.
    coupon_schedule — [{date, rate_pct}, ...]. Если передан, ставка per-period.
    """
    val = _to_date(valdate)
    mat = _to_date(maturity)
    sched = _coupon_schedule_dict(coupon_schedule)
    dates = _coupon_dates(val - timedelta(days=365 * 30), mat, freq)
    if not dates:
        return {"accrued_rub": 0, "accrued_pct": 0, "days_accrued": 0,
                "coupon_period_days": 0, "last_coupon": None, "next_coupon": None}
    if last_coupon:
        last = _to_date(last_coupon)
    else:
        past = [d for d in dates if d <= val]
        last = past[-1] if past else dates[0]
    future = [d for d in dates if d > val]
    nxt = future[0] if future else mat
    if sched and nxt in sched:
        coupon_per_period = sched[nxt] / freq / 100 * face
    else:
        coupon_per_period = coupon_rate / freq / 100 * face
    days_accrued = max(0, (val - last).days)
    period_days = (nxt - last).days if (nxt - last).days > 0 else 365
    accrued = coupon_per_period * days_accrued / period_days if period_days > 0 else 0
    return {
        "accrued_rub": round(accrued, 2),
        "accrued_pct": round(accrued / face * 100, 4),
        "days_accrued": days_accrued,
        "coupon_period_days": period_days,
        "last_coupon": str(last),
        "next_coupon": str(nxt),
    }


def gry(valdate, maturity, coupon_rate, clean_price_pct, face: float = 1000.0,
        freq: int = 2, coupon_schedule: list[dict] | None = None) -> dict:
    """Gross Redemption Yield (YTM с учётом НКД).

    Текущая чистая цена (clean) + НКД = грязная цена → YTM по DCF.
    coupon_schedule — [{date, rate_pct}, ...]. Если передан, купоны per-period.
    Возвращает {gry_pct, dirty_price, accrued_rub}.
    """
    val = _to_date(valdate)
    mat = _to_date(maturity)
    ai = accrued_interest(val, mat, coupon_rate, face, freq, coupon_schedule=coupon_schedule)
    clean = face * clean_price_pct / 100
    dp = clean + ai["accrued_rub"]
    lo, hi = -10.0, 50.0
    for _ in range(60):
        mid = (lo + hi) / 2
        p = dirty_price(val, mat, coupon_rate, mid, face, freq, coupon_schedule)
        if p > dp:
            lo = mid
        else:
            hi = mid
    return {"gry_pct": round((lo + hi) / 2, 3),
            "dirty_price": round(dp, 2),
            "accrued_rub": ai["accrued_rub"]}


def years_to_maturity(maturity, valdate=None) -> float:
    """Срок до погашения в годах (с точностью до 0.01)."""
    mat = _to_date(maturity)
    val = _to_date(valdate) if valdate else date.today()
    return round(max(0, (mat - val).days) / 365, 2)


def spread_to_curve(ytm: float, duration_years: float, curve_yield_at_duration: float) -> dict:
    """Спред доходности облигации к G-кривой на сопоставимой дюрации.

    curve_yield_at_duration — доходность OFZ с похожей дюрацией (или NSS-модель на этом сроке).
    Возвращает {spread_pp, bond_ytm, curve_yield}. Положительный = облигация дороже кривой.
    """
    return {
        "spread_pp": round(ytm - curve_yield_at_duration, 2),
        "bond_ytm": ytm,
        "curve_yield": curve_yield_at_duration,
    }


def parallel_scenarios(valdate, maturity, coupon_rate, ytm, horizon_days=365,
                       face=1000.0, deltas=(-3, -2, -1, 0, 1, 2, 3),
                       today=None, coupon_schedule=None) -> dict:
    """Параллельные сценарии (alias для rate_scenarios для единообразия)."""
    return rate_scenarios(maturity, coupon_rate, ytm, horizon_days, face, deltas, today,
                         coupon_schedule=coupon_schedule)


def twist_scenarios(maturity, coupon_rate: float, ytm: float, duration_years: float,
                    horizon_days: int = 365, face: float = 1000.0,
                    today: str | None = None, freq: int = 2,
                    coupon_schedule: list[dict] | None = None) -> list[dict]:
    """Сценарии «кривая twist»: сужение/расширение короткого/длинного конца.

    twist_short: короткий конец −2 п.п., длинный +1 п.п. (на каждый год дюрации)
    twist_long: длинный −2 п.п., короткий +0.5 п.п.
    steepener: короткий −1, длинный +1
    flattener: короткий +1, длинный −1

    Для каждой комбинации: delta_у по дюрации этой бумаги → total_return.
    coupon_schedule — [{date, rate_pct}, ...]. Если передан, купоны per-period.
    """
    t0 = _to_date(today) if today else date.today()
    mat = _to_date(maturity)
    t1 = t0 + timedelta(days=horizon_days)
    d0 = dirty_price(t0, mat, coupon_rate, ytm, face, freq, coupon_schedule)

    if coupon_schedule:
        total_coupon = 0.0
        for s in coupon_schedule:
            d = _to_date(s["date"])
            if t0 < d <= t1:
                total_coupon += s["rate_pct"] / 100 * face / freq
    else:
        total_coupon = coupon_rate / 100 * face * (horizon_days / 365)

    dur = duration_years or 1.0

    twists = [
        {"name": "steepener", "delta_short": -1.0, "delta_long": 1.0},
        {"name": "flattener", "delta_short": 1.0, "delta_long": -1.0},
        {"name": "twist_short", "delta_short": -2.0, "delta_long": 1.0},
        {"name": "twist_long", "delta_short": 0.5, "delta_long": -2.0},
    ]
    out = []
    for tw in twists:
        # Интерполяция: бумага с dur=0 двигается как короткий конец, dur>5 — как длинный
        weight_long = min(1.0, dur / 5.0)
        delta = tw["delta_short"] * (1 - weight_long) + tw["delta_long"] * weight_long
        d1 = dirty_price(t1, mat, coupon_rate, ytm + delta, face, freq, coupon_schedule)
        tr = (d1 + total_coupon - d0) / d0 * 100
        out.append({
            "name": tw["name"],
            "delta_pp": round(delta, 2),
            "total_return_pct": round(tr, 1),
            "description": (
                f"short -1 pp, long +1 pp" if tw["name"] == "steepener"
                else f"short +1 pp, long -1 pp" if tw["name"] == "flattener"
                else f"short -2 pp, long +1 pp" if tw["name"] == "twist_short"
                else f"short +0.5 pp, long -2 pp"
            ),
        })
    return out
