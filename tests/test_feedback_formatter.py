"""Testes reais e sem mocks para o formatador de feedback estruturado de auto-correção."""

from src.core.feedback_formatter import (
    DEFECT_ACTION_PRESCRIPTIONS,
    MANDATORY_ACTION_DIRECTIVE,
    format_ast_rejection_reason,
    format_circuit_breaker_guidance,
    format_infrastructure_error_reason,
    format_score_rejection_reason,
)


def test_format_ast_rejection_reason():
    violations = [
        {
            "rule_id": "swallowed_exception",
            "file_path": "src/auth.py",
            "line_number": 42,
            "message": "Exceção engolida silenciosamente: bloco except com apenas 'pass'.",
        },
        {
            "rule_id": "mock_in_production",
            "file_path": "tests/test_auth.py",
            "line_number": 15,
            "message": "Uso proibido de unittest.mock.",
        },
    ]

    reason = format_ast_rejection_reason(persona_name="Clara", violations=violations)
    assert "[Clara — Bloqueio Determinístico de AST / Needle 3]" in reason
    assert "[swallowed_exception] src/auth.py:42" in reason
    assert "[mock_in_production] tests/test_auth.py:15" in reason
    assert MANDATORY_ACTION_DIRECTIVE in reason


def test_format_score_rejection_reason_with_defects():
    res = {
        "score": 3.10,
        "threshold": 3.40,
        "attempts": 2,
        "diagnosis": {
            "selected": "robustez_excecoes_recursos",
            "confidence": 0.88,
            "description": "bare except, null, race, leak",
            "candidate_defects": ["falha_de_tratamento_de_excecoes", "vazamento_de_recursos"],
        },
        "stages": [
            {
                "file_path": "src/database.py",
                "part_index": 1,
                "total_parts": 2,
                "score": 2.95,
                "passed": False,
            },
            {
                "file_path": "src/database.py",
                "part_index": 2,
                "total_parts": 2,
                "score": 3.45,
                "passed": True,
            },
        ],
    }

    reason = format_score_rejection_reason(persona_name="Robin", res=res, max_attempts=5)
    assert "[Robin — Guardião da Qualidade] (Tentativa 2/5):" in reason
    assert "3.10 < 3.40" in reason
    assert "src/database.py (Bloco 1/2): Score 2.95 < 3.40" in reason
    assert "Diagnóstico JEV: 'robustez_excecoes_recursos'" in reason
    assert "falha_de_tratamento_de_excecoes" in reason
    assert DEFECT_ACTION_PRESCRIPTIONS["robustez_excecoes_recursos"] in reason
    assert MANDATORY_ACTION_DIRECTIVE in reason


def test_format_score_rejection_penultimate_attempt():
    res = {
        "score": 3.25,
        "threshold": 3.40,
        "attempts": 4,
        "diagnosis": {"selected": "qualidade_de_testes", "confidence": 0.75},
        "stages": [],
    }

    reason = format_score_rejection_reason(persona_name="Diana", res=res, max_attempts=5)
    assert "Tentativa 4/5" in reason
    assert "⚠️ ATENÇÃO: Esta é a penúltima tentativa" in reason


def test_format_infrastructure_error_reason():
    reason = format_infrastructure_error_reason(
        persona_name="Cecília",
        error_msg="ConnectionRefusedError: socket offline",
    )
    assert "[Cecília — Alerta de Infraestrutura]:" in reason
    assert "ConnectionRefusedError" in reason
    assert "openjev_ctl.sh status" in reason


def test_format_circuit_breaker_guidance():
    reason = format_circuit_breaker_guidance(
        persona_name="Silas",
        attempts=5,
        max_attempts=5,
    )
    assert "[Silas — Saída de Emergência Desbloqueada] (Tentativa 5/5):" in reason
    assert "LAUDO DE INVIABILIDADE TÉCNICA" in reason
    assert "1. Diagnóstico preciso da causa-raiz" in reason


def test_format_score_rejection_many_stages_truncation():
    stages = [
        {"file_path": f"src/module_{i}.py", "part_index": 1, "total_parts": 1, "score": 2.8, "passed": False}
        for i in range(8)
    ]
    res = {
        "score": 2.80,
        "threshold": 3.40,
        "attempts": 1,
        "stages": stages,
        "diagnosis": {"selected": "logica_ou_regra", "confidence": 0.60},
    }
    reason = format_score_rejection_reason("Clara", res=res, max_attempts=5)
    assert "src/module_0.py (Bloco 1/1): Score 2.80 < 3.40" in reason
    assert "... e mais 3 bloco(s) pendente(s)." in reason


def test_format_ast_rejection_many_violations_truncation():
    violations = [
        {"rule_id": f"rule_{i}", "file_path": f"src/f_{i}.py", "line_number": i + 1, "message": f"Erro {i}"}
        for i in range(9)
    ]
    reason = format_ast_rejection_reason("Clara", violations=violations)
    assert "... e mais 3 violação(ões) detectada(s)." in reason


def test_score_diagnose_response_schema_message_serialization():
    from src.core.schemas import ScoreDiagnoseResponse

    resp = ScoreDiagnoseResponse(
        score=3.20,
        max_score=5.0,
        normalized_score=0.55,
        passed=False,
        threshold=3.40,
        breakdown={},
        score_latency_ms=10.5,
        attempts=5,
        emergency_exit_unlocked=True,
        message="🚨 Saída de Emergência Autorizada.",
    )
    data = resp.model_dump()
    assert data["message"] == "🚨 Saída de Emergência Autorizada."
    assert data["emergency_exit_unlocked"] is True
    # Valida deserialização
    loaded = ScoreDiagnoseResponse.model_validate(data)
    assert loaded.message == resp.message
