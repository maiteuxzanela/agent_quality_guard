"""Testes do catálogo de ferramentas do Orquestrador.

O catálogo espelha a superfície MCP ativa (busca cirúrgica + quality guard
determinístico, com aliases).
"""

import pytest

from agent_quality_guard.core.tools import TOOL_DEFINITIONS, get_tool_definition

ACTIVE_TOOLS = {
    "cirurgic_search",
    "qmd_search",
    "agent_quality_guard",
    "needle_verify_delivery",
}

def test_tool_catalog_matches_active_mcp_surface():
    """Catálogo = exatamente as 2 tools lógicas ativas + aliases legados."""
    tool_names = {t["function"]["name"] for t in TOOL_DEFINITIONS}
    assert tool_names == ACTIVE_TOOLS


def test_search_canonical_and_alias_share_the_same_schema():
    canonical = get_tool_definition("cirurgic_search")
    alias = get_tool_definition("qmd_search")
    assert canonical["function"]["parameters"] == alias["function"]["parameters"]
    params = canonical["function"]["parameters"]["properties"]
    assert {"query", "workspace", "scope", "max_tokens", "depth"}.issubset(params)
    assert canonical["function"]["parameters"]["required"] == ["query"]


def test_quality_guard_canonical_and_alias_share_the_same_schema():
    canonical = get_tool_definition("agent_quality_guard")
    alias = get_tool_definition("needle_verify_delivery")
    assert canonical["function"]["parameters"] == alias["function"]["parameters"]
    params = canonical["function"]["parameters"]["properties"]
    for field in ("task_type", "workspace", "attempt_number", "pedido", "resultado", "audit_mode"):
        assert field in params, f"agent_quality_guard deve expor o parâmetro '{field}'."
    assert params["attempt_number"].get("default") == 1


def test_retired_tool_lookup_raises_key_error():
    with pytest.raises(KeyError):
        get_tool_definition("openjev_noul")
    with pytest.raises(KeyError):
        get_tool_definition("openjev_consulta")


def test_get_tool_definition_not_found():
    with pytest.raises(KeyError):
        get_tool_definition("non_existent_tool")
