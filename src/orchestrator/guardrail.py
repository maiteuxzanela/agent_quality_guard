"""Módulo de Guardrail e Interceptação Pré-Execução.
Utiliza a primitiva 'noul' do Open Jev para validar condições de segurança
antes que o orquestrador invoque ferramentas críticas ou destrutivas.
"""

import logging
from typing import Any, Callable, Dict, Optional

from ..core.schemas import NoulRequest, NoulResponse
from ..decision.engine import OpenJevEngine

logger = logging.getLogger("jev.orchestrator.guardrail")

# Ações que exigem validação mandatória de guardrail via Open Jev
CRITICAL_ACTIONS = {
    "run_shell_command",
    "delete_file",
    "overwrite_core_file",
    "database_migration",
    "git_force_push",
    "deploy_service",
}


class GuardrailInterceptor:
    """Validador rigoroso de segurança baseado em probabilidades calibradas."""

    def __init__(
        self,
        engine: OpenJevEngine,
        default_threshold: float = 0.85,
    ):
        self.engine = engine
        self.default_threshold = default_threshold

    def is_action_critical(self, action_name: str, payload: Optional[Dict[str, Any]] = None) -> bool:
        """Determina se uma ação proposta se enquadra nos critérios de alto risco."""
        if action_name in CRITICAL_ACTIONS:
            return True
        if payload and any(keyword in str(payload).lower() for keyword in ["rm -rf", "drop table", "truncate", "kill -9", "--force"]):
            return True
        return False

    async def verify_action(
        self,
        action_name: str,
        action_context: str,
        safety_hypothesis: Optional[str] = None,
        threshold: Optional[float] = None,
    ) -> NoulResponse:
        """Executa a checagem booleana 'noul' no Open Jev.
        Lança exceção se a engine não estiver pronta (sem fallback/mock).
        """
        cut_threshold = threshold if threshold is not None else self.default_threshold
        hypothesis = safety_hypothesis or (
            f"The proposed action '{action_name}' is safe to execute in this environment, "
            "does not cause unrecoverable data loss, and complies with system security rules."
        )

        req = NoulRequest(
            premise=action_context,
            hypothesis=hypothesis,
            threshold=cut_threshold,
        )

        logger.info(f"Validando guardrail 'noul' para ação [{action_name}] com threshold={cut_threshold}")
        response = await self.engine.evaluate_noul(req)

        if not response.passed:
            logger.warning(
                f"AÇÃO BLOQUEADA PELO GUARDRAIL: '{action_name}' obteve P={response.probability:.4f} "
                f"(exigido: >= {cut_threshold:.2f})."
            )

        return response
