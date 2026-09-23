"""Testes unitários dos módulos de orquestração (Guardrail, Router, Evaluator)."""

import pytest

from src.core.schemas import OpenJevNotReadyError
from src.decision.engine import OpenJevEngine
from src.orchestrator.evaluator import DeliveryEvaluator
from src.orchestrator.guardrail import GuardrailInterceptor
from src.orchestrator.router import SpecialistRouter


def test_guardrail_critical_action_detection():
    engine = OpenJevEngine()
    interceptor = GuardrailInterceptor(engine=engine, default_threshold=0.78)

    assert interceptor.is_action_critical("run_shell_command") is True
    assert interceptor.is_action_critical("delete_file") is True
    assert interceptor.is_action_critical("safe_read_file") is False

    # Detecção baseada em payload perigoso
    assert interceptor.is_action_critical("execute", {"cmd": "rm -rf /var/cache"}) is True
    assert interceptor.is_action_critical("execute", {"cmd": "echo hello"}) is False


@pytest.mark.asyncio
async def test_guardrail_verify_action_fails_fast_when_engine_unloaded():
    engine = OpenJevEngine()
    interceptor = GuardrailInterceptor(engine=engine)

    with pytest.raises(OpenJevNotReadyError):
        await interceptor.verify_action(
            action_name="run_shell_command",
            action_context="reboot",
        )


@pytest.mark.asyncio
async def test_router_fails_fast_when_engine_unloaded():
    engine = OpenJevEngine()
    router = SpecialistRouter(engine=engine)

    with pytest.raises(OpenJevNotReadyError):
        await router.route_task("Develop authentication microservice")


@pytest.mark.asyncio
async def test_evaluator_fails_fast_when_engine_unloaded():
    engine = OpenJevEngine()
    evaluator = DeliveryEvaluator(engine=engine)

    with pytest.raises(OpenJevNotReadyError):
        await evaluator.evaluate_delivery("code diff here")
