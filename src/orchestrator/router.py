"""Módulo de Roteamento de Subagentes Especialistas.
Utiliza a primitiva 'choice' do Open Jev para selecionar o agente adequado
baseado em probabilidades tipadas, eliminando alucinações de prompt livre.
"""

import logging
from typing import Dict, List, Optional

from ..core.schemas import ChoiceRequest, ChoiceResponse
from ..decision.engine import OpenJevEngine

logger = logging.getLogger("jev.orchestrator.router")

DEFAULT_ROLES = [
    "backend",
    "frontend",
    "security",
    "devops",
    "documentation",
]


class SpecialistRouter:
    """Roteador determinístico de tarefas para subagentes."""

    def __init__(
        self,
        engine: OpenJevEngine,
        available_roles: Optional[List[str]] = None,
        min_confidence: float = 0.40,
    ):
        self.engine = engine
        self.available_roles = available_roles or DEFAULT_ROLES
        self.min_confidence = min_confidence

    async def route_task(
        self,
        task_description: str,
        custom_roles: Optional[List[str]] = None,
        temperature: float = 1.0,
    ) -> ChoiceResponse:
        """Determina qual subagente deve receber a tarefa.
        Lança exceção se a engine não estiver pronta.
        """
        roles = custom_roles or self.available_roles
        req = ChoiceRequest(
            premise=task_description,
            options=roles,
            temperature=temperature,
        )

        logger.info(f"Roteando tarefa via 'choice' entre opções: {roles}")
        response = await self.engine.evaluate_choice(req)

        logger.info(
            f"Subagente selecionado: [{response.selected}] com confiança {response.top_confidence:.4f}. "
            f"Distribuição: {response.probabilities}"
        )

        return response
