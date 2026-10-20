"""Testes unitários dos contratos de dados e schemas Pydantic."""

import asyncio
import tempfile
from pathlib import Path

import pytest
from pydantic import ValidationError

from agent_quality_guard.core.schemas import (
    ASTViolation,
    ChoiceRequest,
    ChoiceResponse,
    CirurgicSearchResponse,
    ConsultaRequest,
    ConsultaResponse,
    Needle3Query,
    Needle3Response,
    Needle3Snippet,
    NeedleVerifyDeliveryRequest,
    NeedleVerifyDeliveryResponse,
    NoulRequest,
    NoulResponse,
    PytestChunkResult,
)


def test_noul_request_default_threshold_is_calibrated_078():
    """Default do schema deve ser o cut calibrado Laya (0.78), não o DeBERTa 0.5/0.85."""
    req = NoulRequest(premise="cmd", hypothesis="safe")
    assert req.threshold == 0.78


def test_needle_verify_delivery_request_defaults():
    """Default do NeedleVerifyDeliveryRequest deve estar conforme a especificação."""
    req = NeedleVerifyDeliveryRequest()
    assert req.task_type == "codigo_producao"
    assert req.workspace_path == "."
    assert req.attempt_number == 1
    assert req.pedido is None
    assert req.resultado is None


def test_needle_verify_delivery_request_attempt_number_validation():
    """attempt_number deve ser int entre 1 e 5."""
    with pytest.raises(ValidationError):
        NeedleVerifyDeliveryRequest(attempt_number=0)
    with pytest.raises(ValidationError):
        NeedleVerifyDeliveryRequest(attempt_number=6)


def test_pytest_chunk_result_and_ast_violation():
    chunk = PytestChunkResult(
        chunk_id="chunk_1_isolated_ml",
        passed=True,
        total_passed=15,
        total_failed=0,
        execution_time_seconds=2.34,
    )
    assert chunk.failure_details == []
    assert chunk.passed is True

    violation = ASTViolation(
        rule_id="RULE_001",
        category="SECURITY",
        file_path="src/auth.py",
        line_number=10,
        code_snippet="eval(user_input)",
        message="uso de eval proibido",
        suggestion="usar ast.literal_eval",
    )
    assert violation.suggestion == "usar ast.literal_eval"


def test_needle_verify_delivery_response():
    chunk = PytestChunkResult(
        chunk_id="chunk_1_isolated_ml",
        passed=True,
        total_passed=5,
        total_failed=0,
        execution_time_seconds=0.8,
    )
    resp = NeedleVerifyDeliveryResponse(
        passed=True,
        status="APPROVED",
        attempt_number=1,
        pytest_success=True,
        pytest_chunks=[chunk],
        blocking_ast_violations=[],
        structural_ast_warnings=[],
        total_files_scanned=12,
        total_lines_scanned=1500,
        message="Entrega aprovada com sucesso",
    )
    assert resp.passed is True
    assert resp.status == "APPROVED"
    assert len(resp.pytest_chunks) == 1


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
    from agent_quality_guard.core.schemas import SpawnSubagentRequest, SpawnSubagentResponse

    req = SpawnSubagentRequest(
        persona="juliana",
        task="Desenvolver PWA médico",
    )
    assert req.timeout is None
    assert req.auto_validate is True
    assert req.persona == "juliana"

    req_with_timeout = SpawnSubagentRequest(
        persona="juliana",
        task="Desenvolver PWA médico",
        timeout=180.0,
    )
    assert req_with_timeout.timeout == 180.0

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


def test_needle3_query_scope_coercion():
    """Verifica coerção transparente de string, list ou None no campo scope do Needle3Query."""
    q1 = Needle3Query(query="def test", scope="src/decision")
    assert q1.scope == ["src/decision"]

    q2 = Needle3Query(query="def test", scope="src/decision, src/memory")
    assert q2.scope == ["src/decision", "src/memory"]

    q3 = Needle3Query(query="def test", scope=["src/core", "src/decision"])
    assert q3.scope == ["src/core", "src/decision"]

    q4 = Needle3Query(query="def test", scope=None)
    assert q4.scope is None

    q5 = Needle3Query(query="def test", scope="")
    assert q5.scope is None


# ==============================================================================
# TICKET-SGA-03: warnings / degraded_lanes nos contratos de busca cirúrgica
# ==============================================================================

def test_needle3_response_defaults_include_empty_warnings_and_degraded_lanes():
    """Needle3Response: warnings/degraded_lanes existem com default_factory=list (retrocompat)."""
    resp = Needle3Response(
        snippets=[],
        total_tokens=0,
        query_latency_ms=1.0,
    )
    assert resp.warnings == []
    assert resp.degraded_lanes == []


def test_needle3_response_accepts_warnings_and_degraded_lanes():
    """Needle3Response: propagação de avisos e lanes degradadas preservada no modelo."""
    resp = Needle3Response(
        snippets=[],
        total_tokens=10,
        query_latency_ms=2.0,
        warnings=["ripgrep timed out after 5.0s"],
        degraded_lanes=["ripgrep_lexical"],
    )
    assert resp.warnings == ["ripgrep timed out after 5.0s"]
    assert resp.degraded_lanes == ["ripgrep_lexical"]


def test_cirurgic_search_response_defaults_are_retrocompatible():
    """CirurgicSearchResponse: defaults preservam construções antigas (success, listas vazias)."""
    resp = CirurgicSearchResponse.model_validate(
        {
            "latency_ms": 12.3,
            "total_tokens": 100,
            "token_savings_pct": 88.5,
            "snippets_count": 1,
            "files_found": 1,
            "snippets": [],
        }
    )
    assert resp.status == "success"
    assert resp.warnings == []
    assert resp.degraded_lanes == []
    assert resp.error is None
    assert resp.message is None
    assert resp.timeout_s is None


def test_cirurgic_search_response_rejects_unknown_status():
    """Contrato estrito: status fora de success|partial|timeout|error é rejeitado."""
    with pytest.raises(ValidationError):
        CirurgicSearchResponse.model_validate({"status": "super-success"})


def test_cirurgic_search_timeout_payload_contract_matches_schema():
    """Payload de timeout global do MCP valida no schema documentado.

    Espelha exatamente o dict que `QmdSearchEngine.search` retorna ao atingir o
    timeout global (`asyncio.TimeoutError`), incluindo `status="timeout"` e o
    campo aditivo `timeout_s`.
    """
    timeout_payload = {
        "status": "timeout",
        "latency_ms": 10000.0,
        "total_tokens": 0,
        "token_savings_pct": 0.0,
        "snippets_count": 0,
        "files_found": 0,
        "snippets": [],
        "timeout_s": 10.0,
        "message": "Busca híbrida excedeu 10.0s.",
        "warnings": ["Busca híbrida excedeu o timeout global de 10.0s."],
        "degraded_lanes": ["global_timeout"],
    }
    resp = CirurgicSearchResponse.model_validate(timeout_payload)
    assert resp.status == "timeout"
    assert resp.timeout_s == 10.0
    assert resp.degraded_lanes == ["global_timeout"]


def test_cirurgic_search_error_payload_contract_matches_schema():
    """Payload de erro estruturado do MCP (sandbox_violation) valida no schema documentado.

    Espelha exatamente o dict que `_cirurgic_search_core` retorna ao capturar
    ``ValueError`` de sandbox — garante que o contrato declarado no servidor
    bate com o schema CirurgicSearchResponse.
    """
    error_payload = {
        "status": "error",
        "error": "sandbox_violation",
        "message": "workspace fora das raízes autorizadas: CIRURGIC_ALLOWED_ROOTS",
        "degraded_lanes": ["sandbox"],
        "warnings": ["Sandbox rejeitou a busca: workspace fora das raízes autorizadas"],
        "latency_ms": 0.0,
        "total_tokens": 0,
        "token_savings_pct": 0.0,
        "snippets_count": 0,
        "files_found": 0,
        "snippets": [],
    }
    resp = CirurgicSearchResponse.model_validate(error_payload)
    assert resp.status == "error"
    assert resp.error == "sandbox_violation"
    assert resp.degraded_lanes == ["sandbox"]


def test_cirurgic_search_mcp_tool_normalizes_contract_with_real_engine():
    """Tool MCP `cirurgic_search` sempre emite status/warnings/degraded_lanes (sem mocks).

    Valida a camada de normalização do servidor MCP contra o motor real de
    busca híbrida em um workspace temporário de verdade.
    """
    from agent_quality_guard.mcp_server import cirurgic_search

    with tempfile.TemporaryDirectory(prefix="sga03_schema_ws_") as tmp:
        ws = Path(tmp)
        (ws / "billing.py").write_text(
            "def calculate_total_billing(days: int) -> float:\n"
            "    return days * 1500.0\n",
            encoding="utf-8",
        )

        res = asyncio.run(
            cirurgic_search(query="calculate_total_billing", workspace=str(ws))
        )

    # Contrato normalizado pelo servidor (setdefault) — chaves sempre presentes.
    assert res["status"] in ("success", "partial")
    assert isinstance(res["warnings"], list)
    assert isinstance(res["degraded_lanes"], list)

    # O dict real do engine valida no schema documentado de referência.
    validated = CirurgicSearchResponse.model_validate(res)
    assert validated.status == res["status"]
