"""Testes unitários dos contratos de dados e schemas Pydantic."""

import pytest
from pydantic import ValidationError

from src.core.schemas import (
    ChoiceRequest,
    ChoiceResponse,
    ConsultaRequest,
    ConsultaResponse,
    Needle3Query,
    Needle3Response,
    Needle3Snippet,
    NoulRequest,
    NoulResponse,
    ScoreRequest,
    ScoreResponse,
)


def test_noul_request_default_threshold_is_calibrated_078():
    """Default do schema deve ser o cut calibrado Laya (0.78), não o DeBERTa 0.5/0.85."""
    req = NoulRequest(premise="cmd", hypothesis="safe")
    assert req.threshold == 0.78


def test_score_request_default_threshold_is_calibrated_340():
    """Default do schema deve ser o corte calibrado Laya (3.40), não o DeBERTa 3.75."""
    req = ScoreRequest(premise="Pedido: x | Resultado: y")
    assert req.threshold == 3.40


def test_noul_request_and_response():
    req = NoulRequest(
        premise="Command: rm -rf /var/log/temp",
        hypothesis="This command is safe to run",
        threshold=0.85,
    )
    assert req.threshold == 0.85

    res = NoulResponse(
        passed=True,
        probability=0.92,
        confidence=0.84,
        threshold=0.85,
        latency_ms=12.5,
    )
    assert res.passed is True
    assert res.probability == 0.92


def test_noul_request_invalid_threshold():
    with pytest.raises(ValidationError):
        NoulRequest(
            premise="test",
            hypothesis="test",
            threshold=1.5,  # Deve ser <= 1.0
        )


def test_choice_request_valid():
    req = ChoiceRequest(
        premise="Fix SQL injection vulnerability in auth.py",
        options=["backend", "frontend", "security", "docs"],
        temperature=0.8,
    )
    assert len(req.options) == 4
    assert req.temperature == 0.8
    assert req.instructions is None
    assert req.criteria is None


def test_choice_request_com_criteria_e_instructions():
    """Choice de diagnóstico serve descriptions como criteria e instrução própria."""
    req = ChoiceRequest(
        premise="Score 2.1 reprovado",
        options=["seguranca", "qualidade_de_testes"],
        instructions="Which defect explains the failure?",
        criteria={
            "seguranca": "security: injection",
            "qualidade_de_testes": "test quality",
        },
    )
    assert req.instructions == "Which defect explains the failure?"
    assert req.criteria["seguranca"] == "security: injection"


def test_choice_request_duplicate_options():
    with pytest.raises(ValidationError):
        ChoiceRequest(
            premise="Fix bug",
            options=["backend", "security", "backend"],  # Duplicata
        )


def test_score_request_valid():
    req = ScoreRequest(
        premise="def foo(): pass",
        rubric="High test coverage, strict typing",
        min_score=1.0,
        max_score=5.0,
        threshold=4.0,
    )
    assert req.threshold == 4.0


def test_score_request_invalid_scale():
    with pytest.raises(ValidationError):
        ScoreRequest(
            premise="test",
            rubric="test",
            min_score=5.0,
            max_score=1.0,  # min >= max é inválido
        )

    with pytest.raises(ValidationError):
        ScoreRequest(
            premise="test",
            rubric="test",
            min_score=1.0,
            max_score=5.0,
            threshold=6.0,  # threshold fora da escala
        )


def test_needle3_query_and_response():
    query = Needle3Query(
        query="class DatabaseConnection",
        scope=["src/database"],
        max_tokens=1000,
        relational_depth=2,
    )
    assert query.max_tokens == 1000

    snippet = Needle3Snippet(
        file_path="src/database/pool.py",
        start_line=10,
        end_line=45,
        content="class DatabaseConnection:\n    pass",
        relevance_score=0.94,
        relations=["caller: init_db", "implements: ConnectionPool"],
    )

    resp = Needle3Response(
        snippets=[snippet],
        total_tokens=120,
        query_latency_ms=18.4,
    )
    assert len(resp.snippets) == 1
    assert resp.total_tokens == 120


# ─── Consulta direta (indício textual, sem booleano de aprovação) ─────────────

def test_consulta_request_and_response():
    req = ConsultaRequest(
        premise="def add(a, b): return a + b\nassert add(1, 2) == 3",
        question="Este script funciona para somar dois inteiros?",
    )
    assert req.premise.startswith("def add")
    assert "script" in req.question

    res = ConsultaResponse(
        indicio="Indício forte a favor de que a resposta é verdadeira (P=0.9100).",
        probability=0.91,
        confidence=0.82,
        latency_ms=14.7,
    )
    dumped = res.model_dump()
    assert "passed" not in dumped, (
        "ConsultaResponse não pode expor booleano de aprovação — contrato de indício."
    )
    assert dumped["probability"] == 0.91


def test_consulta_response_rejects_passed_field():
    """Contrato extra='forbid': booleano de aprovação é rejeitado no schema."""
    with pytest.raises(ValidationError):
        ConsultaResponse.model_validate(
            {
                "indicio": "x",
                "probability": 0.5,
                "confidence": 0.0,
                "latency_ms": 1.0,
                "passed": True,
            }
        )


def test_consulta_request_empty_fields_rejected():
    with pytest.raises(ValidationError):
        ConsultaRequest(premise="", question="funciona?")
    with pytest.raises(ValidationError):
        ConsultaRequest(premise="código x", question="")


def test_spawn_subagent_request_and_response_schemas():
    """Valida schemas de despacho de subagente com timeout opcional."""
    from src.core.schemas import SpawnSubagentRequest, SpawnSubagentResponse

    # Default timeout deve ser None (desativado)
    req = SpawnSubagentRequest(
        persona="juliana",
        task="Desenvolver PWA médico",
    )
    assert req.timeout is None
    assert req.auto_validate is True
    assert req.persona == "juliana"

    # Timeout customizado
    req_with_timeout = SpawnSubagentRequest(
        persona="juliana",
        task="Desenvolver PWA médico",
        timeout=180.0,
    )
    assert req_with_timeout.timeout == 180.0

    # Response schema
    resp = SpawnSubagentResponse(
        success=True,
        persona="juliana",
        workspace="/mnt/Faturamento",
        summary="Módulo implementado com sucesso",
        execution_time_ms=1520.0,
    )
    assert resp.success is True
    assert resp.ast_blocked is False
    assert resp.files_modified == []

