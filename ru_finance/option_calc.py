"""MOEX Option Calculator API client.

Endpoints:
- /assets — underlying assets list
- /assets/{code} — asset detail
- /assets/{code}/futures — futures for asset
- /assets/{code}/options — options for asset
- /assets/{code}/options/{secid} — option brief (Greeks, theor price)
- /assets/{code}/optionseries — option series
- /assets/{code}/optionseries/{code} — series detail
- /assets/{code}/optionseries/{code}/options — options in series
- /assets/{code}/optionseries/{code}/optionboard — options board with Greeks
- /assets/{code}/optionseries/{code}/volatility_graph — volatility smile
- /portfolio/ — calculate portfolio (POST)
- /portfolio/graph/{indicator} — P&L/Greeks graphs (POST)
- /portfolio/initial_margin — initial margin (POST)
"""
from __future__ import annotations

import time

import requests

BASE = "https://iss.moex.com/iss/apps/option-calc/v1"


def _extract_error(r: requests.Response) -> str:
    """Extract human-readable error from MOEX API response."""
    status = r.status_code
    try:
        body = r.json()
        if isinstance(body, dict):
            detail = body.get("detail")
            if isinstance(detail, list):
                return f"{status}: {'; '.join(str(d) for d in detail)}"
            if isinstance(detail, str):
                return f"{status}: {detail}"
    except Exception:
        pass
    text = r.text[:200].strip()
    return f"{status}: {text}" if text else f"{status} error"


def _get(path: str, params: dict | None = None, retries: int = 4) -> dict | list:
    """GET request to option calculator API with retries."""
    url = f"{BASE}/{path.lstrip('/')}"
    last: Exception | None = None
    for i in range(retries):
        try:
            r = requests.get(url, params=params, timeout=15)
            if not r.ok:
                raise requests.HTTPError(_extract_error(r), response=r)
            return r.json()
        except requests.HTTPError:
            raise  # no retry on HTTP errors
        except Exception as e:
            last = e
            time.sleep(0.5 * (i + 1))
    raise last  # type: ignore[misc]


def _post(path: str, json_body: dict, retries: int = 4) -> dict:
    """POST request to option calculator API with retries."""
    url = f"{BASE}/{path.lstrip('/')}"
    last: Exception | None = None
    for i in range(retries):
        try:
            r = requests.post(url, json=json_body, timeout=15)
            if not r.ok:
                raise requests.HTTPError(_extract_error(r), response=r)
            return r.json()
        except requests.HTTPError:
            raise  # no retry on HTTP errors
        except Exception as e:
            last = e
            time.sleep(0.5 * (i + 1))
    raise last  # type: ignore[misc]


# ── Reference data ──

def assets(
    asset_type: str | None = None,
    asset_subtype: str | None = None,
    query: str | None = None,
) -> list[dict]:
    """List of underlying assets.

    Args:
        asset_type: 'commodity'|'currency'|'futures'|'index'|'share'
        asset_subtype: 'commodity'|'currency'|'index'|'share' (for futures)
        query: filter by asset name (max 8 chars)
    """
    params = {}
    if asset_type:
        params["asset_type"] = asset_type
    if asset_subtype:
        params["asset_subtype"] = asset_subtype
    if query:
        params["query"] = query
    return _get("assets", params)


def asset_detail(asset_code: str, asset_type: str | None = None) -> dict:
    """Detail for a single underlying asset.

    Args:
        asset_code: trading code (e.g. 'Si', 'GAZR', 'SBRF')
        asset_type: optional type filter
    """
    params = {}
    if asset_type:
        params["asset_type"] = asset_type
    return _get(f"assets/{asset_code}", params)


# ── Futures ──

def futures_list(asset_code: str, expiration_date: str | None = None) -> list[dict]:
    """Futures contracts for underlying asset.

    Args:
        asset_code: trading code of underlying
        expiration_date: optional filter 'YYYY-MM-DD'
    """
    params = {}
    if expiration_date:
        params["expiration_date"] = expiration_date
    return _get(f"assets/{asset_code}/futures", params)


# ── Options ──

def options_list(
    asset_code: str,
    asset_type: str | None = None,
    expiration_date: str | None = None,
    series_type: str | None = None,
    strike: float | None = None,
    option_type: str | None = None,
) -> list[dict]:
    """Options for underlying asset with filters.

    Args:
        asset_code: trading code of underlying
        asset_type: 'commodity'|'currency'|'futures'|'index'|'share'
        expiration_date: 'YYYY-MM-DD'
        series_type: 'W'|'M'|'Q'
        strike: strike price
        option_type: 'call'|'put'
    """
    params = {}
    if asset_type:
        params["asset_type"] = asset_type
    if expiration_date:
        params["expiration_date"] = expiration_date
    if series_type:
        params["series_type"] = series_type
    if strike is not None:
        params["strike"] = strike
    if option_type:
        params["option_type"] = option_type
    return _get(f"assets/{asset_code}/options", params)


def option_brief(
    asset_code: str,
    secid: str,
    asset_type: str | None = None,
    days_until_expiring: int | None = None,
    underlying_price: float | None = None,
    volatility: float | None = None,
) -> dict:
    """Brief summary for a single option: Greeks, theor price, IV, etc.

    Args:
        asset_code: trading code of underlying
        secid: option instrument code
        asset_type: optional type filter
        days_until_expiring: override days to expiry
        underlying_price: override underlying price (RUB)
        volatility: override implied volatility (%)
    """
    params = {}
    if asset_type:
        params["asset_type"] = asset_type
    if days_until_expiring is not None:
        params["days_until_expiring"] = days_until_expiring
    if underlying_price is not None:
        params["underlying_price"] = underlying_price
    if volatility is not None:
        params["volatility"] = volatility
    return _get(f"assets/{asset_code}/options/{secid}", params)


# ── Option series ──

def option_series_list(
    asset_code: str,
    asset_type: str | None = None,
) -> list[dict]:
    """Option series for underlying asset.

    Args:
        asset_code: trading code of underlying
        asset_type: optional type filter
    """
    params = {}
    if asset_type:
        params["asset_type"] = asset_type
    return _get(f"assets/{asset_code}/optionseries", params)


def option_series_detail(
    asset_code: str,
    optionseries_code: str,
    asset_type: str | None = None,
) -> dict:
    """Detail for a single option series.

    Args:
        asset_code: trading code of underlying
        optionseries_code: series code
        asset_type: optional type filter
    """
    params = {}
    if asset_type:
        params["asset_type"] = asset_type
    return _get(f"assets/{asset_code}/optionseries/{optionseries_code}", params)


def series_options(
    asset_code: str,
    optionseries_code: str,
    asset_type: str | None = None,
    strike: int | None = None,
    option_type: str | None = None,
) -> list[dict]:
    """Options in a specific series.

    Args:
        asset_code: trading code of underlying
        optionseries_code: series code
        asset_type: optional type filter
        strike: filter by strike
        option_type: 'call'|'put'
    """
    params = {}
    if asset_type:
        params["asset_type"] = asset_type
    if strike is not None:
        params["strike"] = strike
    if option_type:
        params["option_type"] = option_type
    return _get(f"assets/{asset_code}/optionseries/{optionseries_code}/options", params)


def option_board(
    asset_code: str,
    optionseries_code: str,
    asset_type: str | None = None,
    rows: int | None = None,
) -> dict:
    """Option board (strikes with Greeks, IV, bid/ask) for a series.

    Args:
        asset_code: trading code of underlying
        optionseries_code: series code
        asset_type: optional type filter
        rows: number of rows from central strike
    """
    params = {}
    if asset_type:
        params["asset_type"] = asset_type
    if rows is not None:
        params["rows"] = rows
    return _get(f"assets/{asset_code}/optionseries/{optionseries_code}/optionboard", params)


def volatility_graph(
    asset_code: str,
    optionseries_code: str,
    asset_type: str | None = None,
) -> list[dict]:
    """Volatility smile (strike vs IV) for a series.

    Args:
        asset_code: trading code of underlying
        optionseries_code: series code
        asset_type: optional type filter
    """
    params = {}
    if asset_type:
        params["asset_type"] = asset_type
    return _get(f"assets/{asset_code}/optionseries/{optionseries_code}/volatility_graph", params)


# ── Portfolio calculation ──

def calculate_portfolio(
    asset_code: str,
    positions: list[dict],
    asset_type: str | None = None,
    what_if: dict | None = None,
) -> dict:
    """Calculate option portfolio: Greeks, P&L, initial margin.

    Args:
        asset_code: trading code of underlying
        positions: list of {secid, quantity, [price], [volatility], [netted_im]}
        asset_type: optional type filter
        what_if: optional {delta_sigma, date_of_calculation}
    """
    body: dict = {
        "asset_code": asset_code,
        "positions": positions,
    }
    if asset_type:
        body["asset_type"] = asset_type
    if what_if:
        body["what_if"] = what_if
    return _post("portfolio/", body)


def portfolio_graph(
    asset_code: str,
    positions: list[dict],
    indicator: str,
    asset_type: str | None = None,
    what_if: dict | None = None,
) -> dict:
    """Calculate portfolio graph for selected indicator.

    Args:
        asset_code: trading code of underlying
        positions: list of {secid, quantity, [price], [volatility]}
        indicator: 'profit_and_loss'|'delta'|'gamma'|'vega'|'theta'|'rho'
        asset_type: optional type filter
        what_if: optional {delta_sigma, date_of_calculation}
    """
    body: dict = {
        "asset_code": asset_code,
        "positions": positions,
    }
    if asset_type:
        body["asset_type"] = asset_type
    if what_if:
        body["what_if"] = what_if
    return _post(f"portfolio/graph/{indicator}", body)


def initial_margin(positions: list[dict]) -> float:
    """Calculate initial margin for a set of positions.

    Args:
        positions: list of {secid, quantity, price, [netted_im]}
    """
    return _post("portfolio/initial_margin", positions)
