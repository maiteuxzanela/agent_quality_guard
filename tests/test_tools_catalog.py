"""Testes do catálogo de ferramentas do Orquestrador."""

import pytest

from src.core.tools import TOOL_DEFINITIONS, get_tool_definition


def test_tool_catalog_contains_all_core_primitives():
    """Nomes canônicos = nomes MCP (needle3_search, openjev_noul, openjev_choice)."""
    tool_names = [t["function"]["name"] for t in TOOL_DEFINITIONS]
    assert "needle3_search" in tool_names
    assert "openjev_noul" in tool_names
    assert "openjev_choice" in tool_names
    assert "openjev_score_evaluate" not in tool_names, (
        "Tool openjev_score (aka openjev_score_evaluate) foi expurgada — só openjev_score_and_diagnose."
    )
    assert "openjev_score_and_diagnose" in tool_names, (
        "Tool de score+diagnóstico deve ser o único caminho de score no catálogo."
    )
    assert "openjev_consulta" in tool_names, (
        "Tool de consulta direta (indício textual) deve estar registrada no catálogo."
    )


def test_noul_catalog_threshold_default_is_078():
    """Catálogo de tools não pode herdar o corte DeBERTa 0.85."""
    tool = get_tool_definition("openjev_noul")
    assert tool["function"]["parameters"]["properties"]["threshold"].get("default") == 0.78


def test_noul_catalog_description_is_not_mandatory():
    """A exigência pré-ação do noul foi removida — descrição não pode ser MANDATÓRIO."""
    tool = get_tool_definition("openjev_noul")
    assert "MANDATÓRIO" not in tool["function"]["description"]
    assert "OPCIONAL" in tool["function"]["description"]


def test_get_tool_definition_by_name():
    tool = get_tool_definition("openjev_noul")
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
    # Threshold padrão deve ser 3.40 (calibrado para o Laya)
    assert params["threshold"].get("default") == 3.40
    # Parâmetro obrigatório
    assert "premise" in tool["function"]["parameters"]["required"]


def test_consulta_tool_schema_completo():
    """openjev_consulta: premise + question obrigatórios, sem parâmetros de aprovação."""
    tool = get_tool_definition("openjev_consulta")
    params = tool["function"]["parameters"]["properties"]
    assert "premise" in params
    assert "question" in params
    assert "hypothesis" not in params, (
        "Consulta não usa hipótese de guardrail — é indício, não veredito booleano."
    )
    assert "threshold" not in params, "Consulta não pode expor threshold de aprovação."
    required = tool["function"]["parameters"]["required"]
    assert required == ["premise", "question"]
