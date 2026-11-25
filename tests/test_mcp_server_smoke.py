"""Smoke-тесты регистрации инструментов MCP-сервера (без сети).

Проверяется только то, что декораторы @mcp.tool() в ru_finance.mcp_server
зарегистрировали все инструменты с корректными метаданными:
  - непустые name / description / inputSchema у каждого инструмента;
  - фиксированное количество (baseline 95) — при осознанном добавлении
    инструмента число в EXPECTED_TOOL_COUNT нужно обновить;
  - присутствие якорных инструментов из всех 14 групп (см. AGENTS.md);
  - отсутствие дубликатов имён.

Список инструментов берётся из публичного API MCPServer (`mcp.list_tools()`,
mcp.server.mcpserver — официальный SDK (mcp>=2.0), см. импорты mcp_server.py).
"""

from __future__ import annotations

import asyncio
from collections import Counter

import pytest

from ru_finance.mcp_server import GROUPS, mcp

# Baseline: зафиксировано на момент написания теста. При осознанном добавлении
# нового инструмента обнови это число (и якоря ANCHOR_TOOLS при новой группе).
EXPECTED_TOOL_COUNT = 95

# По несколько якорных инструментов на каждую из 14 групп.
ANCHOR_TOOLS: dict[str, set[str]] = {
    "core_lookup": {"moex_quote", "moex_resolve", "current_datetime"},
    "price_history": {"moex_candles", "moex_history", "moex_full_history"},
    "market_data": {"moex_turnovers", "moex_indicative_rates"},
    "fundamental": {"smartlab_dividends", "stock_f_score", "company_fundamental_report"},
    "technical": {"technical_indicators", "price_volatility", "candlestick_patterns"},
    "screening": {"bond_screener", "etf_screener", "smartlab_stock_screener"},
    "discover": {"moex_query", "moex_search_endpoints"},
    "macro": {"cbr_key_rate", "rate_expectations", "curve_yield"},
    "fx_metals": {"cbr_currency", "cbr_metals"},
    "fixed_income": {"bond_report", "moex_bond_coupons", "raexpert_rating"},
    "etf": {"etf_fund_info", "etf_tracking_error"},
    "derivatives": {"moex_futures_list", "moex_option_quote", "moex_futures_basis"},
    "risk": {"portfolio_snapshot", "portfolio_rate_whatif", "portfolio_alpha_beta"},
    "option_calc": {"option_calc_assets", "option_calc_optionboard", "option_calc_portfolio"},
}


@pytest.fixture(scope="module")
def tools() -> dict[str, object]:
    """Зарегистрированные инструменты сервера: name → Tool (публичный API MCPServer)."""
    tool_list = asyncio.run(mcp.list_tools())
    return {t.name: t for t in tool_list}


def test_tool_count_matches_baseline(tools):
    assert len(tools) == EXPECTED_TOOL_COUNT, (
        f"Число инструментов изменилось: {len(tools)} вместо {EXPECTED_TOOL_COUNT}. "
        "Если инструмент(ы) добавлены/удалены осознанно — обнови EXPECTED_TOOL_COUNT "
        "в tests/test_mcp_server_smoke.py (и якоря ANCHOR_TOOLS при добавлении новой "
        "группы); иначе проверь, не потерялся ли существующий инструмент."
    )


def test_no_duplicate_names(tools):
    raw_names = [t.name for t in asyncio.run(mcp.list_tools())]
    dupes = {name: n for name, n in Counter(raw_names).items() if n > 1}
    assert not dupes, f"Дубликаты имён инструментов: {dupes}"


def test_every_tool_has_name_description_schema(tools):
    problems: list[str] = []
    for name, tool in sorted(tools.items()):
        if not name.strip():
            problems.append("<пустое имя>")
            continue
        if not (tool.description or "").strip():
            problems.append(f"{name}: пустое description")
        schema = tool.input_schema  # mcp>=2.0 (mcp_types 2.x): бывший inputSchema
        if not isinstance(schema, dict) or schema.get("type") != "object":
            problems.append(f"{name}: пустая/некорректная inputSchema ({schema!r})")
    assert not problems, "Проблемные инструменты:\n" + "\n".join(problems)


def test_anchor_tools_of_all_groups_present(tools):
    missing = {
        group: sorted(anchors - tools.keys())
        for group, anchors in ANCHOR_TOOLS.items()
        if anchors - tools.keys()
    }
    assert not missing, (
        "Отсутствуют якорные инструменты групп (новая группа? переименование?):\n"
        + "\n".join(f"  {group}: {names}" for group, names in missing.items())
    )


def test_groups_registry_covers_all_registered_tools(tools):
    """Каждый зарегистрированный инструмент должен быть в GROUPS, и наоборот:
    забытая запись в GROUPS ломает групповые серверы /g/{group}/mcp."""
    registered = set(tools)
    grouped = set().union(*GROUPS.values())
    ungrouped = registered - grouped
    ghost = grouped - registered
    assert not ungrouped, (
        "Инструменты не привязаны ни к одной группе в GROUPS (mcp_server.py): "
        f"{sorted(ungrouped)}"
    )
    assert not ghost, f"Имена в GROUPS без зарегистрированного инструмента: {sorted(ghost)}"
