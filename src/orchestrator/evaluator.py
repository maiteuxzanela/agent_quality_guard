"""Módulo de Avaliação de Qualidade e Conformidade de Entrega.
Utiliza a primitiva 'score' do Open Jev para quantificar a aderência de artefatos
(código, refatorações, documentação) a rubricas pré-estabelecidas.
"""

import logging
from typing import Optional

from ..core.schemas import RUBRIC_CATALOG, ScoreRequest, ScoreResponse
from ..decision.engine import OpenJevEngine

logger = logging.getLogger("jev.orchestrator.evaluator")

# Rubrica calibrada única: catálogo fechado (fonte de verdade única — anti rubric gaming).
DEFAULT_CODE_RUBRIC = RUBRIC_CATALOG["codigo_producao"]


class DeliveryEvaluator:
    """Avaliador objetivo de conformidade com loop de feedback."""

    def __init__(
        self,
        engine: OpenJevEngine,
        default_pass_threshold: float = 3.40,
        default_rubric: str = DEFAULT_CODE_RUBRIC,
    ):
        self.engine = engine
        self.default_pass_threshold = default_pass_threshold
        self.default_rubric = default_rubric

    async def evaluate_delivery(
        self,
        artifact_content: str,
        pass_threshold: Optional[float] = None,
        min_score: float = 1.0,
        max_score: float = 5.0,
    ) -> ScoreResponse:
        """Avalia a entrega produzida por um subagente.
        Retorna a nota e o veredito de aprovação. Lança exceção se a engine não estiver pronta.
        Rubrica vem exclusivamente do catálogo fechado (custom_rubric removido — rubric gaming).
        """
        rubric = self.default_rubric
        threshold = pass_threshold if pass_threshold is not None else self.default_pass_threshold

        req = ScoreRequest(
            premise=artifact_content,
            rubric=rubric,
            min_score=min_score,
            max_score=max_score,
            threshold=threshold,
        )

        logger.info(f"Avaliando entrega via 'score' (corte: {threshold}/{max_score})")
        response = await self.engine.evaluate_score(req)

        status_str = "APROVADA" if response.passed else "REPROVADA (Requer Refatoração)"
        logger.info(f"Entrega {status_str}: Nota obtida {response.score}/{max_score}.")

        return response
