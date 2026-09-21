"""Testes da política estrita Fail-Fast da OpenJevEngine.
Garante que sem modelo carregado ou ambiente pronto, a engine NUNCA simula dados
e lança imediatamente exceções tipadas explícitas.
"""

import pytest

from src.core.schemas import (
    ChoiceRequest,
    NoulRequest,
    OpenJevNotReadyError,
    ScoreRequest,
)
from src.decision.engine import OpenJevEngine


@pytest.mark.asyncio
async def test_engine_unloaded_raises_noul_error():
    engine = OpenJevEngine(model_id_or_path="AlexWortega/openjev")
    assert engine.is_ready is False

    req = NoulRequest(
        premise="rm -rf /",
        hypothesis="Safe",
        threshold=0.85,
    )

    with pytest.raises(OpenJevNotReadyError) as exc_info:
        await engine.evaluate_noul(req)

    assert "não está pronta" in str(exc_info.value)


@pytest.mark.asyncio
async def test_engine_unloaded_raises_choice_error():
    engine = OpenJevEngine(model_id_or_path="AlexWortega/openjev")
    assert engine.is_ready is False

    req = ChoiceRequest(
        premise="Implement JWT authentication",
        options=["backend", "frontend", "security"],
    )

    with pytest.raises(OpenJevNotReadyError) as exc_info:
        await engine.evaluate_choice(req)

    assert "não está pronta" in str(exc_info.value)


@pytest.mark.asyncio
async def test_engine_unloaded_raises_score_error():
    engine = OpenJevEngine(model_id_or_path="AlexWortega/openjev")
    assert engine.is_ready is False

    req = ScoreRequest(
        premise="def add(a, b): return a + b",
        rubric="Strict typing and docstrings",
    )

    with pytest.raises(OpenJevNotReadyError) as exc_info:
        await engine.evaluate_score(req)

    assert "não está pronta" in str(exc_info.value)
