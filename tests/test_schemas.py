"""Testes unitários dos contratos de dados e schemas Pydantic."""

import pytest
from pydantic import ValidationError

from src.core.schemas import (
    ChoiceRequest,
    ChoiceResponse,
    Needle3Query,
    Needle3Response,
    Needle3Snippet,
    NoulRequest,
    NoulResponse,
    ScoreRequest,
    ScoreResponse,
)


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
