"""Доменные отчёты по портфелю: парсинг + snapshot/rate_whatif/income/movers/alpha_beta.

Сервер полностью generic: портфель ВСЕГДА передаётся параметром `assets_text`
(markdown в формате как в примере ниже). Кода/путей к чьим-либо конкретным
данным здесь нет — модуль можно хостить и делиться им.

Формат assets_text (markdown):

    refresh date: 27.06.2026
    # ИИС                 ← счёт (любой заголовок '# ...')
    ## Акции              ← класс (любой '## ...')
    - Сбербанк (SBER): 51 шт. (321,26 ₽ -> 301 ₽)
    ## Облигации
    - ОФЗ 26249: 125 шт. (88,837 -> 86,100)
    - ГТЛК (RU000A10C6F7): 14 шт. (101,69 -> 100,80)

Правила строки: «- Название [(ТИКЕР/ISIN)]: КОЛ-ВО шт. (ЦЕНА_ПОКУПКИ [-> тек.])».
Тип бумаги (акция/облигация) определяется автоматически через резолв ISIN/тикера на MOEX.
Для облигаций цена покупки — в % номинала, для акций/фондов — в ₽; сервер сам
отличает одно от другого при обогащении.
search_key (чем резолвим): тикер/ISIN из скобок > номер ОФЗ > само название.
P&L приблизительный (средняя цена покупки, без купонов/дивидендов и налогов).
"""
from __future__ import annotations

import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from . import cbr, moex, rate, smartlab

_LINE = re.compile(r"^-\s*(?P<name>.+?)\s*:\s*(?P<qty>[\d ]+)\s*шт\.?\s*\((?P<prices>[^)]*)\)")
_TICKER = re.compile(r"\(([A-Z0-9]{1,12})\)\s*$")
_OFZ = re.compile(r"ОФЗ\s*(\d{4,5})", re.I)
_NUM = re.compile(r"-?\d+(?:[.,]\d+)?")
_MM = re.compile(r"денежн|ликвидн|money|liquidity|LQDT|OLIQ|SBMM|AKMM", re.I)


def _num(s: str) -> float:
    return float(s.replace(",", "."))


def parse_assets(assets_text: str) -> list[dict]:
    """Распарсить текст портфеля (markdown) в список позиций.

    assets_text обязателен — конкретные бумаги приходят от вызывающего, а не
    из какого-либо файла на сервере.
    """
    account = cls = None
    out = []
    for raw in assets_text.splitlines():
        line = raw.strip()
        if line.startswith("# "):
            account = line[2:].strip()
        elif line.startswith("## "):
            cls = line[3:].strip()
        elif line.startswith("- "):
            m = _LINE.match(line)
            if not m:
                continue
            name = m.group("name").strip()
            qty = int(m.group("qty").replace(" ", ""))
            prices = m.group("prices")
            nums = _NUM.findall(prices)
            buy = _num(nums[0]) if nums else None
            tk = _TICKER.search(name)
            base = _TICKER.sub("", name).strip()
            if tk:
                key = tk.group(1)
            elif _OFZ.search(base):
                key = _OFZ.search(base).group(1)
            else:
                key = base
            out.append({
                "account": account, "class": cls, "name": base,
                "search_key": key, "qty": qty, "buy_price": buy,
            })
    return out


def _classify(pos: dict, info: dict) -> str:
    _BOND_FUND_CATS = {"bond_gov", "bond_corp"}
    _COMMODITY_CATS = {"commodity"}
    _FX_CATS = {"fx"}
    if _MM.search(pos["name"]):
        return "money_market"
    if info["is_bond"]:
        secid = str(info.get("secid", ""))
        typ = str(info.get("type", ""))
        is_ofz = typ.startswith("ofz") or secid.startswith("SU")
        if is_ofz:
            dur = info.get("duration_years") or 0
            return "ofz_long" if dur and dur > 3 else "ofz_short"
        return "corp_bond"
    if info.get("type") in ("common_share", "preferred_share"):
        return "equity"
    # БПИФ/ETF: классифицируем по категории из ETF_BENCHMARK_MAP
    secid = str(info.get("secid", ""))
    etf_cat = moex.ETF_BENCHMARK_MAP.get(secid, {}).get("category", "")
    if etf_cat == "money_market":
        return "money_market"
    if etf_cat in _BOND_FUND_CATS:
        return "bond_fund"
    if etf_cat in _COMMODITY_CATS:
        return "commodity"
    if etf_cat in _FX_CATS:
        return "fx_fund"
    return "fund_eq"


_CLASS_RU = {
    "ofz_long": "Длинные ОФЗ", "ofz_short": "Короткие/средние ОФЗ",
    "corp_bond": "Корп. облигации", "equity": "Акции (прямые)",
    "fund_eq": "Фонды акций", "money_market": "Денежный рынок",
    "bond_fund": "Облигационные фонды", "commodity": "Сырьевые фонды",
    "fx_fund": "Валютные фонды",
}


def _enrich(pos: dict) -> dict:
    """Подтянуть живую цену и метрики для позиции.

    При любой ошибке (сеть, ненайденный тикер и т.д.) возвращает позицию
    с name/search_key, но с error и без price/value.
    """
    p = dict(pos)
    p["error"] = None
    # Определяем тип бумаги: если group==stock_bonds → облигация, иначе фонд/акция.
    is_bond = False
    info = None
    try:
        info = moex.resolve(pos["search_key"])
        is_bond = str(info.get("group", "")).endswith("_bonds")
    except Exception as exc:
        p["error"] = f"resolve: {exc}"
        p["is_bond"] = False
        return p
    try:
        if is_bond:
            b = moex.bond(pos["search_key"])
            face = b.get("face_value") or 1000
            price_pct = b.get("price_pct")
            p.update({
                "secid": b["secid"], "shortname": b["shortname"], "type": b["type"],
                "price": price_pct, "unit": "%", "face": face,
                "ytm": b.get("ytm"), "duration_years": b.get("duration_years"),
                "mod_duration_years": b.get("mod_duration_years"),
                "coupon_pct": b.get("coupon_pct"),
                "annual_coupon_per_bond": b.get("annual_coupon_per_bond"),
                "maturity": b.get("maturity"), "change_pct": b.get("change_pct"),
            })
            p["value"] = pos["qty"] * face * price_pct / 100 if price_pct else None
            p["cost"] = pos["qty"] * face * pos["buy_price"] / 100 if pos["buy_price"] else None
            # Спред к G-кривой
            if b.get("ytm") and b.get("duration_years"):
                try:
                    cy = rate.curve_yield(b["duration_years"])
                    p["spread_to_curve_pp"] = round(b["ytm"] - cy.get("yield", 0), 2)
                except Exception:
                    pass
        else:
            q = moex.quote(pos["search_key"])
            price = q.get("price")
            p.update({
                "secid": q["secid"], "shortname": q["shortname"],
                "type": info.get("type") if info else None,
                "price": price, "unit": "₽", "change_pct": q.get("change_pct"),
            })
            p["value"] = pos["qty"] * price if price else None
            p["cost"] = pos["qty"] * pos["buy_price"] if pos["buy_price"] else None
            # Дивидендная доходность
            ticker = pos["search_key"]
            if price:
                try:
                    divs = smartlab.get_dividend_history(ticker)
                    if divs:
                        last_div = divs[-1].get("dividend_rub")
                        if last_div:
                            p["dividend_rub"] = last_div
                            p["div_yield_pct"] = round(last_div / price * 100, 2)
                            p["annual_dividend_per_position"] = round(pos["qty"] * last_div, 2)
                except Exception:
                    pass
    except Exception as exc:
        p["error"] = f"quote/bond: {exc}"
    p["is_bond"] = is_bond
    if p.get("value") and p.get("cost"):
        p["pnl"] = round(p["value"] - p["cost"], 2)
        p["pnl_pct"] = round(p["pnl"] / p["cost"] * 100, 2)
    p["bucket"] = _classify(pos, p)
    return p


def snapshot(assets_text: str, inflation_pct: float | None = None) -> dict:
    """Полный снимок портфеля: позиции, распределение, риск по ставке, поток.

    inflation_pct — фактическая годовая инфляция (% г/г, CPI Росстат).
    Если не передана, используется ключевая ставка как приближение.
    """
    positions = [_enrich(p) for p in parse_assets(assets_text)]
    total = sum(p["value"] for p in positions if p.get("value"))
    cost = sum(p["cost"] for p in positions if p.get("cost"))
    key = cbr.key_rate(tail=1).get("latest") or 0

    for p in positions:
        p["weight_pct"] = round(p["value"] / total * 100, 1) if p.get("value") else None

    alloc: dict[str, float] = {}
    for p in positions:
        alloc[p["bucket"]] = alloc.get(p["bucket"], 0) + (p.get("value") or 0)
    allocation = [{"bucket": k, "name_ru": _CLASS_RU.get(k, k),
                   "value": round(v, 2), "weight_pct": round(v / total * 100, 1)}
                  for k, v in sorted(alloc.items(), key=lambda x: -x[1])]

    port_mod_dur = sum((p.get("mod_duration_years") or 0) * (p.get("value") or 0)
                       for p in positions) / total if total else 0
    rate_risk = {
        "portfolio_mod_duration_years": round(port_mod_dur, 2),
        "per_plus_1pp_pct": round(-port_mod_dur, 2),
        "per_plus_1pp_rub": round(-port_mod_dur / 100 * total, 0),
        "per_minus_1pp_pct": round(port_mod_dur, 2),
        "per_minus_1pp_rub": round(port_mod_dur / 100 * total, 0),
        "note": "параллельный сдвиг доходностей облигаций на ±1 п.п.",
    }

    coupon_income = sum(p["qty"] * (p.get("annual_coupon_per_bond") or 0)
                        for p in positions if p["is_bond"])
    mm_income = sum((p.get("value") or 0) * key / 100
                    for p in positions if p["bucket"] == "money_market")
    # Дивидендный доход по акциям
    div_income = sum((p.get("annual_dividend_per_position") or 0)
                     for p in positions if not p["is_bond"])
    income = coupon_income + mm_income + div_income

    running_yield_pct = round(income / total * 100, 1) if total else None
    infl_cp = inflation_pct if inflation_pct is not None else key
    infl = round(infl_cp, 1)
    real_yield_pct = round(running_yield_pct - infl, 1) if running_yield_pct is not None else None

    return {
        "as_of": _refresh_date(assets_text),
        "key_rate": key,
        "inflation_pct": infl,
        "inflation_source": "cpi" if inflation_pct is not None else "key_rate_proxy",
        "total_value": round(total, 0),
        "total_cost": round(cost, 0),
        "pnl": round(total - cost, 0),
        "pnl_pct": round((total - cost) / cost * 100, 1) if cost else None,
        "positions": [{
            "name": p["name"], "secid": p.get("secid"), "account": p["account"],
            "bucket": p["bucket"], "qty": p["qty"],
            "price": p.get("price"), "unit": p.get("unit"),
            "value": round(p["value"], 0) if p.get("value") else None,
            "weight_pct": p.get("weight_pct"),
            "pnl_pct": p.get("pnl_pct"), "change_pct": p.get("change_pct"),
            "ytm": p.get("ytm"), "duration_years": p.get("duration_years"),
            "spread_to_curve_pp": p.get("spread_to_curve_pp"),
            "div_yield_pct": p.get("div_yield_pct"),
            "error": p.get("error"),
        } for p in positions],
        "allocation": allocation,
        "rate_risk": rate_risk,
        "income": {
            "annual_coupons": round(coupon_income, 0),
            "annual_money_market": round(mm_income, 0),
            "annual_dividends": round(div_income, 0),
            "annual_total_est": round(income, 0),
            "running_yield_pct": running_yield_pct,
            "note": ("оценка; купоны + дивиденды (последний объявленный) + "
                     "доходность денежного рынка; без налогов"),
        },
        "income_risk": {
            "running_yield_pct": running_yield_pct,
            "inflation_pct": infl,
            "inflation_source": "cpi" if inflation_pct is not None else "key_rate_proxy",
            "real_yield_est_pct": real_yield_pct,
            "note": ("реальная доходность = running_yield − инфляция "
                     "(CPI Росстат, если передана; иначе ключевая ставка)"),
        },
    }


def rate_whatif(delta_pp: float, assets_text: str) -> dict:
    """Что станет с портфелем при сдвиге доходностей облигаций на delta_pp п.п."""
    positions = [_enrich(p) for p in parse_assets(assets_text)]
    total = sum(p["value"] for p in positions if p.get("value"))
    rows, impact = [], 0.0
    for p in positions:
        md = p.get("mod_duration_years") or 0
        chg_pct = -md * delta_pp
        chg_rub = (p.get("value") or 0) * chg_pct / 100
        impact += chg_rub
        if p["is_bond"]:
            rows.append({"name": p["name"], "mod_duration_years": md,
                         "price_change_pct": round(chg_pct, 2),
                         "value_change_rub": round(chg_rub, 0)})
    return {
        "delta_pp": delta_pp,
        "portfolio_value_change_rub": round(impact, 0),
        "portfolio_value_change_pct": round(impact / total * 100, 2) if total else None,
        "new_total_value": round(total + impact, 0),
        "bond_detail": rows,
        "note": "приближение по модиф. дюрации (параллельный сдвиг); акции/деньги не двигаем",
    }


def income_calendar(assets_text: str) -> dict:
    """Ближайшие поступления: следующий купон по облигациям + объявленные дивиденды."""
    events = []
    for pos in parse_assets(assets_text):
        pos_is_bond = False
        try:
            ri = moex.resolve(pos["search_key"])
            pos_is_bond = str(ri.get("group", "")).endswith("_bonds")
        except Exception:
            pass
        if pos_is_bond:
            b = moex.bond(pos["search_key"])
            if b.get("next_coupon") and b.get("coupon_value"):
                events.append({
                    "date": b["next_coupon"], "type": "купон",
                    "name": pos["name"],
                    "amount_rub": round(pos["qty"] * b["coupon_value"], 2),
                })
        else:
            ticker = pos["search_key"]
            try:
                divs = smartlab.get_dividend_history(ticker)
            except Exception:
                divs = []
            if divs:
                last = divs[-1]
                val = last.get("dividend_rub")
                if val:
                    events.append({
                        "date": last.get("cutoff_date"),
                        "type": "дивиденд (последний объявленный)",
                        "name": pos["name"],
                        "amount_rub": round(pos["qty"] * val, 2),
                    })
    events.sort(key=lambda e: str(e.get("date") or ""))
    return {"events": events,
            "note": "купоны — ближайшая выплата; дивиденды — последняя известная (проверяй дату отсечки)"}


def movers(assets_text: str) -> dict:
    """Кто вырос/просел: дневное изменение и P&L против цены покупки."""
    positions = [_enrich(p) for p in parse_assets(assets_text)]
    rows = [{"name": p["name"], "secid": p.get("secid"),
             "change_pct": p.get("change_pct"), "pnl_pct": p.get("pnl_pct")}
            for p in positions]
    by_day = sorted([r for r in rows if r["change_pct"] is not None],
                    key=lambda r: r["change_pct"])
    by_pnl = sorted([r for r in rows if r["pnl_pct"] is not None],
                    key=lambda r: r["pnl_pct"])
    return {
        "day_losers": by_day[:3], "day_gainers": list(reversed(by_day[-3:])),
        "worst_vs_cost": by_pnl[:3], "best_vs_cost": list(reversed(by_pnl[-3:])),
    }


def alpha_beta(assets_text: str, benchmark: str = "IMOEX", days: int = 252) -> dict:
    """Alpha, Beta и корреляция (ρ) портфеля к бенчмарку.

    Использует дневные close из moex.history() за последние `days` торговых дней.
    Веса позиций считаются по текущей стоимости (value / total).
    Бенчмарк по умолчанию: IMOEX. Для ОФЗ-портфеля лучше передать 'RGBITR'.
    """
    positions = [_enrich(p) for p in parse_assets(assets_text)]
    total = sum(p["value"] for p in positions if p.get("value"))
    if not total:
        return {"error": "портфель пуст или нет цен"}

    till = datetime.now().strftime("%Y-%m-%d")
    frm = (datetime.now() - timedelta(days=days * 2)).strftime("%Y-%m-%d")  # запас на выходные

    def _fetch_hist(query: str) -> tuple[str, list[dict]]:
        try:
            return query, moex.history(query, frm, till)
        except Exception:
            return query, []

    # Параллельно тянем историю всех позиций + бенчмарк
    queries = [p["search_key"] for p in positions] + [benchmark]
    hist_map: dict[str, dict[str, float]] = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(_fetch_hist, q): q for q in queries}
        for fut in as_completed(futs):
            q, rows = fut.result()
            prices = {}
            for r in rows:
                d = (r.get("TRADEDATE") or r.get("begin", ""))[:10]
                c = r.get("CLOSE") or r.get("close")
                if d and c:
                    prices[d] = float(c)
            if prices:
                hist_map[q] = prices

    if benchmark not in hist_map:
        return {"error": f"нет истории по бенчмарку {benchmark!r}"}

    bench_hist = hist_map[benchmark]
    dates = sorted(bench_hist.keys())
    if len(dates) < 10:
        return {"error": f"слишком мало данных по бенчмарку ({len(dates)} дней)"}

    # Строим массив дневных доходностей портфеля и бенчмарка
    # Для позиций без истории — пропускаем (вес перераспределяется на остальных)
    skip_secids: list[str] = []
    active_positions = []
    for p in positions:
        q = p["search_key"]
        if q in hist_map and len(hist_map[q]) >= 2:
            active_positions.append(p)
        else:
            skip_secids.append(p.get("secid") or q)

    active_total = sum(p["value"] for p in active_positions if p.get("value"))
    if not active_total:
        return {"error": "нет исторических данных ни по одной позиции"}

    # Нормализованные веса (только по позициям с историей)
    weights = {
        p["search_key"]: (p["value"] or 0) / active_total
        for p in active_positions
    }

    port_returns: list[float] = []
    bench_returns: list[float] = []
    for i in range(1, len(dates)):
        d_prev, d_curr = dates[i - 1], dates[i]
        b_prev, b_curr = bench_hist.get(d_prev), bench_hist.get(d_curr)
        if not b_prev or not b_curr or b_prev == 0:
            continue
        b_ret = (b_curr - b_prev) / b_prev

        p_ret = 0.0
        weight_sum = 0.0
        for q, w in weights.items():
            h = hist_map.get(q, {})
            pp, pc = h.get(d_prev), h.get(d_curr)
            if pp and pc and pp > 0:
                p_ret += w * (pc - pp) / pp
                weight_sum += w
        if weight_sum < 0.01:
            continue  # в этот день нет данных по портфелю

        port_returns.append(p_ret)
        bench_returns.append(b_ret)

    n = len(port_returns)
    if n < 10:
        return {"error": f"слишком мало совпадающих дней ({n})"}

    mu_p = sum(port_returns) / n
    mu_b = sum(bench_returns) / n
    var_b = sum((b - mu_b) ** 2 for b in bench_returns) / n
    cov_pb = sum((port_returns[i] - mu_p) * (bench_returns[i] - mu_b) for i in range(n)) / n
    var_p = sum((p - mu_p) ** 2 for p in port_returns) / n

    if var_b == 0:
        return {"error": "дисперсия бенчмарка = 0"}

    beta = cov_pb / var_b
    alpha_daily = mu_p - beta * mu_b
    alpha_ann = alpha_daily * 252 * 100  # % годовых
    rho = cov_pb / (math.sqrt(var_p) * math.sqrt(var_b)) if var_p > 0 else None

    # Статистика для контроля
    mu_p_ann = mu_p * 252 * 100
    mu_b_ann = mu_b * 252 * 100
    vol_p = math.sqrt(var_p) * math.sqrt(252) * 100
    vol_b = math.sqrt(var_b) * math.sqrt(252) * 100
    sharpe = (mu_p_ann - cbr.key_rate(tail=1).get("latest", 0)) / vol_p if vol_p > 0 else None

    return {
        "benchmark": benchmark,
        "period_days": n,
        "beta": round(beta, 3),
        "alpha_ann_pct": round(alpha_ann, 2),
        "rho": round(rho, 3) if rho is not None else None,
        "portfolio_return_ann_pct": round(mu_p_ann, 2),
        "benchmark_return_ann_pct": round(mu_b_ann, 2),
        "portfolio_vol_ann_pct": round(vol_p, 2),
        "benchmark_vol_ann_pct": round(vol_b, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "weights": {q: round(w * 100, 1) for q, w in weights.items()},
        "skipped": skip_secids or None,
        "note": ("alpha = Rp − β·Rb (годовых); β = Cov(Rp,Rb)/Var(Rb); "
                 "ρ = Corr(Rp,Rb); веса — текущая доля стоимости"),
    }


def _refresh_date(assets_text: str) -> str | None:
    for line in assets_text.splitlines()[:3]:
        m = re.search(r"refresh date:\s*(.+)", line, re.I)
        if m:
            return m.group(1).strip()
    return None
