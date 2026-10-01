"""Adaptador Local Laya para o Typesafe Computer Use (Open JEV).

Substitui as chamadas de API da nuvem (typesafe-sdk / TYPESAFE_API_KEY)
pelo modelo local Laya (convaiinnovations/laya) executando no OpenJevEngine.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Dict, Optional, Union

from src.core.schemas import ChoiceRequest, NoulRequest
from src.decision.engine import OpenJevEngine


@dataclass
class ChoiceAnswer:
    """Resposta compatível com a primitiva Choice da TypeSafe SDK."""
    choice: str
    confidence: float
    probabilities: Dict[str, float]


@dataclass
class NoulAnswer:
    """Resposta compatível com a primitiva Noul da TypeSafe SDK."""
    noul: float
    confidence: float


@dataclass
class SystemOneResponse:
    """Objeto retornado pelo método system_one, contendo o dicionário de respostas."""
    answers: Dict[str, Union[ChoiceAnswer, NoulAnswer]]


class Choice:
    """Representação local de uma pergunta Choice (compatível com typesafe-sdk)."""
    def __init__(self, instructions: str, criteria: Dict[str, str]):
        self.instructions = instructions
        self.criteria = criteria


class Noul:
    """Representação local de uma pergunta Noul (compatível com typesafe-sdk)."""
    def __init__(self, instructions: str, threshold: float = 0.78):
        self.instructions = instructions
        self.threshold = threshold


class LayaLocalClient:
    """Cliente local do Laya (Open JEV) com interface idêntica ao TypeSafeClient da nuvem."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        model_path: str = "convaiinnovations/laya",
        engine: Optional[OpenJevEngine] = None,
    ):
        """Inicializa a engine local do Open JEV com modelo Laya."""
        if engine is not None:
            self.engine = engine
        else:
            self.engine = OpenJevEngine(model_id_or_path=model_path, allow_cpu_fallback=True)
            self.engine.load_model()

    def system_one(
        self,
        state: Dict[str, Any],
        questions: Dict[str, Any],
    ) -> SystemOneResponse:
        """Executa a decisão síncrona/assíncrona via Laya local para as perguntas fornecidas."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            loop = None

        if loop and loop.is_running():
            return asyncio.run_coroutine_threadsafe(
                self.async_system_one(state, questions), loop
            ).result()
        else:
            return asyncio.run(self.async_system_one(state, questions))

    async def async_system_one(
        self,
        state: Dict[str, Any],
        questions: Dict[str, Any],
    ) -> SystemOneResponse:
        """Processa as perguntas Choice e Noul de forma concorrente via OpenJevEngine."""
        state_str = json.dumps(state, ensure_ascii=False) if isinstance(state, dict) else str(state)
        answers: Dict[str, Union[ChoiceAnswer, NoulAnswer]] = {}

        tasks = {}
        for q_name, q_obj in questions.items():
            if isinstance(q_obj, Choice) or (hasattr(q_obj, "criteria") and hasattr(q_obj, "instructions")):
                options = list(q_obj.criteria.keys())
                req = ChoiceRequest(
                    premise=state_str,
                    options=options,
                    instructions=q_obj.instructions,
                    criteria=q_obj.criteria,
                )
                tasks[q_name] = ("choice", self.engine.evaluate_choice(req))

            elif isinstance(q_obj, Noul) or (hasattr(q_obj, "instructions") and not hasattr(q_obj, "criteria")):
                threshold = getattr(q_obj, "threshold", 0.78)
                req = NoulRequest(
                    premise=state_str,
                    hypothesis=q_obj.instructions,
                    threshold=threshold,
                )
                tasks[q_name] = ("noul", self.engine.evaluate_noul(req))

        for q_name, (q_type, coro) in tasks.items():
            res = await coro
            if q_type == "choice":
                answers[q_name] = ChoiceAnswer(
                    choice=res.selected,
                    confidence=res.top_confidence,
                    probabilities=res.probabilities,
                )
            elif q_type == "noul":
                answers[q_name] = NoulAnswer(
                    noul=res.probability,
                    confidence=res.confidence,
                )

        return SystemOneResponse(answers=answers)
