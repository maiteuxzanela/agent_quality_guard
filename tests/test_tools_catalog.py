"""Testes do catálogo de ferramentas do Orquestrador."""

import pytest

from src.core.tools import TOOL_DEFINITIONS, get_tool_definition


def test_tool_catalog_contains_all_core_primitives():
    tool_names = [t["function"]["name"] for t in TOOL_DEFINITIONS]
    assert "needle3_relational_search" in tool_names
    assert "openjev_noul_guardrail" in tool_names
    assert "openjev_choice_route" in tool_names
    assert "openjev_score_evaluate" in tool_names
    assert "openjev_score_and_diagnose" in tool_names, (
        "Nova tool de diagnóstico em cascata deve estar registrada no catálogo."
    )


def test_get_tool_definition_by_name():
    tool = get_tool_definition("openjev_noul_guardrail")
    assert tool["type"] == "function"
    assert "premise" in tool["function"]["parameters"]["properties"]
    assert "hypothesis" in tool["function"]["parameters"]["properties"]


def test_get_tool_definition_not_found():
    with pytest.raises(KeyError):
        get_tool_definition("non_existent_tool")


def test_score_and_diagnose_tool_schema_completo():
    """Verifica que openjev_score_and_diagnose expõe os parâmetros obrigatórios."""
    tool = get_tool_definition("openjev_score_and_diagnose")
    params = tool["function"]["parameters"]["properties"]
    assert "premise" in params
    assert "threshold" in params
    assert "workspace" in params
    assert "rubric" in params
    # Threshold padrão deve ser 3.60 (calibrado para o Laya)
    assert params["threshold"].get("default") == 3.60
    # Parâmetro obrigatório
    assert "premise" in tool["function"]["parameters"]["required"]
