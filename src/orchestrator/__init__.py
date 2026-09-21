"""Camada de Execução & Orquestração.
Coordena a LLM e subagentes especialistas através das decisões estruturadas do Open Jev
e do contexto cirúrgico do Needle 3.
"""

from .guardrail import GuardrailInterceptor
from .router import SpecialistRouter
from .evaluator import DeliveryEvaluator

__all__ = [
    "GuardrailInterceptor",
    "SpecialistRouter",
    "DeliveryEvaluator",
]
