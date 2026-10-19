"""Camada de Execução & Orquestração.

Coordena a LLM e subagentes especialistas através das decisões estruturadas do
Open Jev e do contexto cirúrgico do Needle 3.

TICKET-HARNESS-47: os exports públicos são resolvidos de forma preguiçosa
(PEP 562). Importar um submódulo — ex.: ``src.orchestrator.pytest_chunk_runner``
— não deve executar ``guardrail``/``router``/``evaluator``, que importam o
motor Laya/``OpenJevEngine`` e, por transitividade, instanciam pesos/VRAM.
"""

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - apenas para type checkers
    from .evaluator import DeliveryEvaluator
    from .guardrail import GuardrailInterceptor
    from .router import SpecialistRouter

__all__ = [
    "GuardrailInterceptor",
    "SpecialistRouter",
    "DeliveryEvaluator",
]

_LAZY_EXPORTS = {
    "GuardrailInterceptor": (".guardrail", "GuardrailInterceptor"),
    "SpecialistRouter": (".router", "SpecialistRouter"),
    "DeliveryEvaluator": (".evaluator", "DeliveryEvaluator"),
}


def __getattr__(name: str) -> Any:
    """Resolve exports públicos sob demanda, sem carregar o motor no import."""
    target = _LAZY_EXPORTS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    from importlib import import_module

    module_name, attr_name = target
    return getattr(import_module(module_name, __name__), attr_name)
