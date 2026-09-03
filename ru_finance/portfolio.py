"""Доменные отчёты по портфелю: парсинг + snapshot/rate_whatif/income/movers.

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

import re

from . import bonds, cbr, moex, rate, smartlab

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
    return "fund_eq"


_CLASS_RU = {
    "ofz_long": "Длинные ОФЗ", "ofz_short": "Короткие/средние ОФЗ",
    "corp_bond": "Корп. облигации", "equity": "Акции (прямые)",
    "fund_eq": "Фонды акций", "money_market": "Денежный рынок",
}


def _enrich(pos: dict) -> dict:
    """Подтянуть живую цену и метрики для позиции."""
    p = dict(pos)
    # Определяем тип бумаги: если group==stock_bonds → облигация, иначе фонд/акция.
    is_bond = False
    info = None
    try:
        info = moex.resolve(pos["search_key"])
        is_bond = str(info.get("group", "")).endswith("_bonds")
    except ValueError:
        pass
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
            except Exception:  # noqa: BLE001
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
            except Exception:  # noqa: BLE001
                pass
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
        except Exception:  # noqa: BLE001
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
            except Exception:  # noqa: BLE001
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


def _refresh_date(assets_text: str) -> str | None:
    for line in assets_text.splitlines()[:3]:
        m = re.search(r"refresh date:\s*(.+)", line, re.I)
        if m:
            return m.group(1).strip()
    return None
