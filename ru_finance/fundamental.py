"""Фундаментальный анализ: F-Score, Z-Score, peer-сравнение, дивидендный анализ,
рост, банковский бенчмарк, единая карточка компании.

Все расчёты поверх данных smartlab.py — новых скрейперов не требуется.
"""
from __future__ import annotations

import math
import statistics
from typing import Any

from . import raexpert, smartlab


# ─────────────────────── helpers ───────────────────────

def _val(data: dict, field: str, year: str) -> float | None:
    """Значение поля из структуры smartlab_company_financials.data."""
    node = data.get(field)
    if not node:
        return None
    v = node.get("values", {}).get(year)
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _years_sorted(fin: dict) -> list[str]:
    """Годы из финансовых данных, отсортированные chronologically, без LTM."""
    years = [y for y in fin.get("years", []) if y != "LTM"]
    return sorted(years)


def _latest(fin: dict, field: str) -> float | None:
    """Последнее значение поля: LTM если есть, иначе последний год."""
    data = fin.get("data", {})
    v = _val(data, field, "LTM")
    if v is not None:
        return v
    years = _years_sorted(fin)
    if years:
        return _val(data, field, years[-1])
    return None


def _previous(fin: dict, field: str) -> float | None:
    """Предпоследнее значение поля (год перед последним)."""
    data = fin.get("data", {})
    years = _years_sorted(fin)
    if len(years) >= 2:
        return _val(data, field, years[-2])
    return None


def _all_values(fin: dict, field: str) -> list[tuple[str, float]]:
    """Все числовые значения поля по годам (chronologically, без LTM)."""
    data = fin.get("data", {})
    out = []
    for y in _years_sorted(fin):
        v = _val(data, field, y)
        if v is not None:
            out.append((y, v))
    return out


def _cagr(first: float, last: float, n_years: float) -> float | None:
    """Compound Annual Growth Rate."""
    if first is None or last is None or first <= 0 or last <= 0 or n_years <= 0:
        return None
    return (last / first) ** (1 / n_years) - 1


# ─────────────────────── Piotroski F-Score ───────────────────────

def f_score(ticker: str, *, standard: str = "MSFO") -> dict[str, Any]:
    """Piotroski F-Score (0–9) для акции.

    Источник: smartlab_company_financials.
    9 бинарных сигналов (1 = положительный, 0 = нет):
      1. ROA > 0
      2. OCF > 0
      3. ROA растёт (y/y)
      4. OCF > net_income (accrual quality)
      5. Долг/активы снижается
      6. Текущий ratio растёт (gross: (assets - net_debt) / assets proxy — рост активов)
      7. Нет дилution (число акций не растёт)
      8. EBITDA margin растёт
      9. Asset turnover (revenue/assets) растёт
    """
    fin = smartlab.get_company_financials(ticker, period="y", standard=standard)
    if not fin or not fin.get("data"):
        return {"ticker": ticker.upper(), "error": "No financial data"}

    data = fin["data"]

    def _latest_a(f: str) -> float | None:
        v = _latest(fin, f)
        return v

    def _latest_bank(f: str, fallback: str) -> float | None:
        v = _latest(fin, f)
        if v is not None:
            return v
        return _latest(fin, fallback)

    def _prev(f: str) -> float | None:
        return _previous(fin, f)

    def _prev_bank(f: str, fallback: str) -> float | None:
        v = _previous(fin, f)
        if v is not None:
            return v
        return _previous(fin, fallback)

    signals: dict[str, int] = {}
    details: dict[str, Any] = {}

    # 1. ROA > 0
    roa = _latest_a("roa")
    details["roa"] = roa
    signals["roa_positive"] = 1 if roa is not None and roa > 0 else 0

    # 2. OCF > 0
    ocf = _latest_a("ocf")
    if ocf is None:
        ocf = _latest_a("fcf")
    if ocf is None:
        ocf = _latest_a("net_operating_income")  # for banks
    details["ocf"] = ocf
    signals["ocf_positive"] = 1 if ocf is not None and ocf > 0 else 0

    # 3. ROA растёт
    roa_prev = _prev("roa")
    details["roa_prev"] = roa_prev
    signals["roa_improving"] = 1 if roa is not None and roa_prev is not None and roa > roa_prev else 0

    # 4. OCF > net_income (accrual quality)
    ni = _latest_a("net_income")
    details["net_income"] = ni
    signals["accrual_quality"] = 1 if ocf is not None and ni is not None and ocf > ni else 0

    # 5. Долг/активы снижается
    debt = _latest_bank("debt", "loan_portfolio")
    assets = _latest_bank("assets", "bank_assets")
    debt_prev = _prev_bank("debt", "loan_portfolio")
    assets_prev = _prev_bank("assets", "bank_assets")
    da = debt / assets if debt is not None and assets is not None and assets > 0 else None
    da_prev = debt_prev / assets_prev if debt_prev is not None and assets_prev is not None and assets_prev > 0 else None
    details["debt_to_assets"] = da
    details["debt_to_assets_prev"] = da_prev
    signals["leverage_improving"] = 1 if da is not None and da_prev is not None and da < da_prev else 0

    # 6. Активы растут (proxy для liquidity improvement — нет current ratio в данных)
    assets_grew = assets is not None and assets_prev is not None and assets > assets_prev
    signals["assets_growing"] = 1 if assets_grew else 0

    # 7. Нет дилution
    shares = _latest_a("number_of_shares")
    shares_prev = _prev("number_of_shares")
    details["shares"] = shares
    details["shares_prev"] = shares_prev
    signals["no_dilution"] = 1 if shares is not None and shares_prev is not None and shares <= shares_prev else 0

    # 8. EBITDA margin растёт
    em = _latest_a("ebitda_margin")
    em_prev = _prev("ebitda_margin")
    details["ebitda_margin"] = em
    signals["margin_improving"] = 1 if em is not None and em_prev is not None and em > em_prev else 0

    # 9. Asset turnover растёт
    rev = _latest_a("revenue")
    rev_prev = _prev("revenue")
    at = rev / assets if rev is not None and assets is not None and assets > 0 else None
    at_prev = rev_prev / assets_prev if rev_prev is not None and assets_prev is not None and assets_prev > 0 else 0
    details["asset_turnover"] = at
    signals["turnover_improving"] = 1 if at is not None and at_prev is not None and at > at_prev else 0

    total = sum(signals.values())

    return {
        "ticker": ticker.upper(),
        "name": fin.get("name", ""),
        "f_score": total,
        "max": 9,
        "signals": signals,
        "details": details,
        "years": fin.get("years", []),
    }


# ─────────────────────── Altman Z-Score ───────────────────────

def z_score(ticker: str, *, standard: str = "MSFO") -> dict[str, Any]:
    """Altman Z-Score (модифицированная для EM: Z'=6.56*X1 + 3.26*X2 + 6.72*X3 + 1.05*X4).

    X1 = (assets - net_debt - liabilities) / assets ≈ working_capital / assets
        (proxy: если нет liabilities, используем net_debt/assets)
    X2 = retained_earnings / assets  (proxy: book_value / assets)
    X3 = EBIT / assets  (proxy: operating_income / assets)
    X4 = book_value_equity / total_liabilities
        (proxy: market_cap / debt, если debt > 0)

    Зоны: Z' > 2.9 → safe, 1.23 < Z' < 2.9 → grey, Z' < 1.23 → distress.
    """
    fin = smartlab.get_company_financials(ticker, period="y", standard=standard)
    if not fin or not fin.get("data"):
        return {"ticker": ticker.upper(), "error": "No financial data"}

    data = fin["data"]
    ticker_name = fin.get("name", "")

    def _first(*fields: str) -> float | None:
        for f in fields:
            v = _latest(fin, f)
            if v is not None:
                return v
        return None

    assets = _first("assets", "bank_assets")
    net_debt = _first("net_debt")
    debt = _first("debt", "loan_portfolio")
    book_value = _first("book_value", "capital")
    oi = _first("operating_income", "net_operating_income")
    ebitda = _first("ebitda")
    market_cap = _first("market_cap")
    revenue = _first("revenue")

    if not assets or assets <= 0:
        return {"ticker": ticker.upper(), "name": ticker_name, "error": "No assets data"}

    # X1: working capital proxy
    # Попробуем: assets - net_debt (грубый proxy для equity + liabilities - net_debt)
    # Если debt известен, то net_debt ≈ debt - cash
    x1 = None
    if net_debt is not None:
        wc_proxy = assets - net_debt  # ≈ assets - (debt - cash) ≈ assets + cash - debt
        x1 = wc_proxy / assets

    # X2: retained earnings proxy → book_value / assets
    x2 = book_value / assets if book_value is not None else None

    # X3: EBIT / assets
    ebit = oi if oi is not None else ebitda
    x3 = ebit / assets if ebit is not None else None

    # X4: equity / liabilities proxy → market_cap / debt (Fallen Angel model для EM)
    x4 = None
    if market_cap is not None and debt is not None and debt > 0:
        x4 = market_cap / debt
    elif market_cap is not None and net_debt is not None and net_debt > 0:
        x4 = market_cap / net_debt

    components = {}
    z = 0.0
    missing = []
    if x1 is not None:
        components["X1_working_capital_assets"] = round(x1, 4)
        z += 6.56 * x1
    else:
        missing.append("X1")

    if x2 is not None:
        components["X2_retained_earnings_assets"] = round(x2, 4)
        z += 3.26 * x2
    else:
        missing.append("X2")

    if x3 is not None:
        components["X3_ebit_assets"] = round(x3, 4)
        z += 6.72 * x3
    else:
        missing.append("X3")

    if x4 is not None:
        components["X4_equity_liabilities"] = round(x4, 4)
        z += 1.05 * x4
    else:
        missing.append("X4")

    if z > 2.9:
        zone = "safe"
    elif z > 1.23:
        zone = "grey"
    else:
        zone = "distress"

    return {
        "ticker": ticker.upper(),
        "name": ticker_name,
        "z_score": round(z, 2),
        "zone": zone,
        "thresholds": {"safe": "> 2.9", "grey": "1.23 – 2.9", "distress": "< 1.23"},
        "components": components,
        "missing": missing if missing else None,
        "model": "Z' (6.56·X1 + 3.26·X2 + 6.72·X3 + 1.05·X4) — modified for EM",
    }


# ─────────────────────── Peer Comparison ───────────────────────

def peer_comparison(ticker: str, limit: int = 20) -> dict[str, Any]:
    """Сравнение мультипликаторов тикера с сектором.

    Источник: smartlab_stock_screener (LTM).
    Возвращает ранги тикера по каждому мультипликатору внутри сектора.
    """
    # Сначала находим тикер в полном скринере, чтобы определить сектор
    all_stocks = smartlab.get_stock_screener(limit=200, order_by="market_cap")
    target = None
    for s in all_stocks:
        if s.get("ticker", "").upper() == ticker.upper():
            target = s
            break
    if not target:
        return {"ticker": ticker.upper(), "error": "Ticker not found in screener"}

    # Строим ранжирование по всему рынку (LTM-мультипликаторы)
    # Берём top-N по капитализации для контекста
    peers = all_stocks[:limit]

    metrics = ["p_e", "p_s", "p_b", "ev_ebitda", "ebitda_margin", "debt_ebitda",
               "div_yield", "div_payout_ratio", "market_cap", "revenue", "net_income"]

    rankings: dict[str, Any] = {}
    for m in metrics:
        val = target.get(m)
        if val is None:
            continue
        vals = [(p.get("ticker"), p.get(m)) for p in peers if p.get(m) is not None]
        if not vals:
            continue
        # Сортировка: для yield/payout/margin/revenue/ниcome — больше лучше;
        # для pe/ps/pb/ev_ebitda/debt — меньше лучше
        lower_better = m in ("p_e", "p_s", "p_b", "ev_ebitda", "debt_ebitda")
        vals_sorted = sorted(vals, key=lambda x: x[1], reverse=not lower_better)
        rank = next((i + 1 for i, (t, _) in enumerate(vals_sorted) if t.upper() == ticker.upper()), None)
        values = [v for _, v in vals_sorted]
        median_v = statistics.median(values) if values else None

        rankings[m] = {
            "value": val,
            "rank": rank,
            "of": len(vals_sorted),
            "median": round(median_v, 2) if median_v is not None else None,
            "better": lower_better,
        }

    return {
        "ticker": ticker.upper(),
        "name": target.get("name", ""),
        "market_cap": target.get("market_cap"),
        "peer_count": len(peers),
        "rankings": rankings,
        "note": "Rank 1 = best in peer group by metric",
    }


# ─────────────────────── Dividend Analysis ───────────────────────

def dividend_analysis(ticker: str) -> dict[str, Any]:
    """Дивидендный анализ: CAGR, средняя доходность, стабильность.

    Источник: smartlab_dividend_history.
    """
    history = smartlab.get_dividend_history(ticker)
    if not history:
        return {"ticker": ticker.upper(), "error": "No dividend history"}

    # Собираем годовые суммы дивидендов
    yearly: dict[str, float] = {}
    yearly_price: dict[str, float] = {}
    for row in history:
        cutoff = row.get("cutoff_date", "")
        if not cutoff or "." not in cutoff:
            continue
        year = cutoff.split(".")[-1]
        div = row.get("dividend_rub")
        if div is not None:
            yearly[year] = yearly.get(year, 0) + div
        # Берём цену на последнюю отсечку в году
        price = row.get("price")
        if price is not None:
            yearly_price[year] = price

    if not yearly:
        return {"ticker": ticker.upper(), "error": "No parsed dividend data"}

    years_sorted = sorted(yearly.keys())
    divs = [yearly[y] for y in years_sorted]

    # CAGR
    cagr = None
    if len(divs) >= 2 and divs[0] > 0 and divs[-1] > 0:
        n = len(divs) - 1
        cagr = (divs[-1] / divs[0]) ** (1 / n) - 1

    # Средняя доходность по годам
    yields_by_year = {}
    for y in years_sorted:
        if y in yearly_price and yearly_price[y] > 0:
            yields_by_year[y] = yearly[y] / yearly_price[y] * 100
    avg_yield = statistics.mean(yields_by_year.values()) if yields_by_year else None

    # Стабильность: кол-во лет подряд без снижения с конца
    consistent_years = 0
    if len(divs) >= 2:
        consistent_years = 1
        for i in range(len(divs) - 1, 0, -1):
            if divs[i - 1] <= divs[i]:
                consistent_years += 1
            else:
                break

    return {
        "ticker": ticker.upper(),
        "total_payments": len(history),
        "years": len(years_sorted),
        "yearly_dividends": {y: round(yearly[y], 2) for y in years_sorted},
        "yearly_yields_pct": {y: round(yields_by_year.get(y, 0), 2) for y in years_sorted if y in yields_by_year},
        "cagr": round(cagr, 4) if cagr is not None else None,
        "avg_yield_pct": round(avg_yield, 2) if avg_yield is not None else None,
        "consistent_years": consistent_years,
        "min_dividend": round(min(divs), 2),
        "max_dividend": round(max(divs), 2),
    }


# ─────────────────────── Stock Growth Analysis ───────────────────────

def growth_analysis(ticker: str, *, standard: str = "MSFO") -> dict[str, Any]:
    """Анализ роста: CAGR выручки/прибыли/EBITDA, тренды ROE/Margin.

    Источник: smartlab_company_financials (годовые).
    """
    fin = smartlab.get_company_financials(ticker, period="y", standard=standard)
    if not fin or not fin.get("data"):
        return {"ticker": ticker.upper(), "error": "No financial data"}

    data = fin["data"]
    years = _years_sorted(fin)
    if len(years) < 2:
        return {"ticker": ticker.upper(), "name": fin.get("name", ""), "error": "Need at least 2 years"}

    n_years = len(years) - 1

    def _cagr_field(field: str) -> tuple[float | None, dict]:
        vals = _all_values(fin, field)
        if len(vals) < 2:
            return None, {y: v for y, v in vals}
        first_v = vals[0][1]
        last_v = vals[-1][1]
        c = _cagr(first_v, last_v, len(vals) - 1)
        return c, {y: round(v, 2) for y, v in vals}

    rev_cagr, rev_series = _cagr_field("revenue")
    ni_cagr, ni_series = _cagr_field("net_income")
    ebitda_cagr, ebitda_series = _cagr_field("ebitda")

    # ROE / ROA тренд
    _, roe_series = _cagr_field("roe")
    _, roa_series = _cagr_field("roa")

    # Margin тренд
    _, ebitda_margin_series = _cagr_field("ebitda_margin")
    _, net_margin_series = _cagr_field("net_margin")

    return {
        "ticker": ticker.upper(),
        "name": fin.get("name", ""),
        "standard": standard,
        "period_years": years,
        "cagr": {
            "revenue": round(rev_cagr, 4) if rev_cagr is not None else None,
            "net_income": round(ni_cagr, 4) if ni_cagr is not None else None,
            "ebitda": round(ebitda_cagr, 4) if ebitda_cagr is not None else None,
        },
        "series": {
            "revenue": rev_series,
            "net_income": ni_series,
            "ebitda": ebitda_series,
            "roe": roe_series,
            "roa": roa_series,
            "ebitda_margin": ebitda_margin_series,
            "net_margin": net_margin_series,
        },
    }


# ─────────────────────── Bank Benchmark ───────────────────────

def bank_benchmark(tickers: list[str], *, standard: str = "MSFO") -> list[dict[str, Any]]:
    """Сравнение банков по ключевым метрикам: NIM, CIR, NPL, CAR, LDR, CoR.

    Источник: smartlab_company_financials для банков.
    """
    fields = [
        "net_intertest_margin", "cost_to_income", "share_of_non_performing_loans",
        "core_capital_adequacy_ratio", "loan_to_deposit_ratio", "cost_of_risk_ratio",
        "bank_margin", "bank_assets", "capital", "loan_portfolio",
        "roa", "roe", "debt_ebitda",
    ]

    results: list[dict[str, Any]] = []
    for t in tickers:
        fin = smartlab.get_company_financials(t, period="y", standard=standard, fields=fields)
        if not fin or not fin.get("data"):
            results.append({"ticker": t.upper(), "error": "No data"})
            continue

        entry: dict[str, Any] = {"ticker": t.upper(), "name": fin.get("name", "")}
        for f in fields:
            v = _latest(fin, f)
            if v is not None:
                entry[f] = round(v, 2)
        results.append(entry)

    return results


# ─────────────────────── Bank Peer Comparison ───────────────────────

def bank_peer_comparison(ticker: str) -> dict[str, Any]:
    """Ранжирование банка среди банковского сектора по банковским метрикам.

    Источник: smartlab_stock_screener(sector_id=2) + company_financials.
    """
    # Получаем банковский сектор из скринера
    bank_stocks = smartlab.get_stock_screener(sector_id=2, limit=50, order_by="market_cap")
    tickers_in_sector = [s["ticker"].upper() for s in bank_stocks if s.get("ticker")]

    if ticker.upper() not in tickers_in_sector:
        return {"ticker": ticker.upper(), "error": "Ticker not in bank sector"}

    bank_fields = [
        "net_intertest_margin", "cost_to_income", "share_of_non_performing_loans",
        "core_capital_adequacy_ratio", "loan_to_deposit_ratio", "cost_of_risk_ratio",
        "bank_margin",
    ]
    # lower_is_better для этих полей
    lower_better = {"cost_to_income", "share_of_non_performing_loans",
                    "loan_to_deposit_ratio", "cost_of_risk_ratio"}

    metrics: dict[str, list[tuple[str, float]]] = {f: [] for f in bank_fields}
    target_entry: dict[str, float | None] = {}

    for t in tickers_in_sector[:15]:  # limit to avoid too many requests
        fin = smartlab.get_company_financials(t, period="y", standard="MSFO",
                                               fields=bank_fields)
        if not fin or not fin.get("data"):
            continue
        for f in bank_fields:
            v = _latest(fin, f)
            if v is not None:
                metrics[f].append((t, v))
                if t.upper() == ticker.upper():
                    target_entry[f] = v

    rankings: dict[str, Any] = {}
    for f in bank_fields:
        if f not in target_entry or target_entry[f] is None:
            continue
        vals = metrics[f]
        if not vals:
            continue
        lb = f in lower_better
        vals_sorted = sorted(vals, key=lambda x: x[1], reverse=not lb)
        rank = next((i + 1 for i, (t, _) in enumerate(vals_sorted) if t.upper() == ticker.upper()), None)
        median_v = statistics.median([v for _, v in vals_sorted])
        rankings[f] = {
            "value": target_entry[f],
            "rank": rank,
            "of": len(vals_sorted),
            "median": round(median_v, 2),
            "lower_is_better": lb,
        }

    return {
        "ticker": ticker.upper(),
        "sector": "BANKI",
        "peer_count": len(tickers_in_sector),
        "rankings": rankings,
    }


# ─────────────────────── Company Fundamental Report ───────────────────────

def company_report(ticker: str, *, standard: str = "MSFO") -> dict[str, Any]:
    """Единая карточка компании: финансовые метрики + дивиденды + кредитный рейтинг.

    Источник: smartlab_company_financials + smartlab_dividend_history + raexpert_rating.
    """
    t = ticker.upper()

    fin = smartlab.get_company_financials(t, period="y", standard=standard)

    # Основные метрики
    latest: dict[str, Any] = {}
    if fin and fin.get("data"):
        for field in ["p_e", "p_s", "p_b", "ev_ebitda", "market_cap", "ev",
                       "revenue", "ebitda", "net_income", "ocf", "fcf",
                       "roa", "roe", "ebitda_margin", "net_margin",
                       "debt", "net_debt", "debt_ebitda", "assets",
                       "eps", "bv_share", "div_yield", "div_payout_ratio",
                       "free_float"]:
            v = _latest(fin, field)
            if v is not None:
                latest[field] = round(v, 2) if isinstance(v, float) else v
        latest["name"] = fin.get("name", "")

    # Дивиденды
    div_summary: dict[str, Any] | None = None
    try:
        div_analysis = dividend_analysis(t)
        if "error" not in div_analysis:
            div_summary = {
                "years": div_analysis.get("years"),
                "cagr": div_analysis.get("cagr"),
                "avg_yield_pct": div_analysis.get("avg_yield_pct"),
                "consistent_years": div_analysis.get("consistent_years"),
                "latest_dividends": dict(list(div_analysis.get("yearly_dividends", {}).items())[-3:]),
            }
    except Exception:
        pass

    # Рейтинг
    rating_info: list[dict] | None = None
    try:
        ratings = raexpert.get_rating(t)
        if ratings:
            rating_info = ratings[:3]
    except Exception:
        pass

    return {
        "ticker": t,
        "metrics": latest,
        "dividends": div_summary,
        "credit_rating": rating_info,
        "standard": standard,
    }
