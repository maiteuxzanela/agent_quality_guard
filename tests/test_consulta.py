"""Testes de integração para openjev_consulta — indício textual sem booleano.

Todos os testes exercitam o engine Laya real (sem mock), conforme AGENTS.md §5.2.
O modelo é carregado uma única vez por sessão via singleton do mcp_server.
"""

import unittest

from pydantic import ValidationError

from src.core.schemas import ConsultaResponse
import src.mcp_server as _mcp_module
from src.mcp_server import openjev_consulta


def _ensure_engine_loaded():
    """Garante que o engine Laya real está no singleton do mcp_server.
    Sem mock — carrega pesos reais da GPU/CPU (convaiinnovations/laya).
    """
    if _mcp_module._engine_instance is None:
        from src.decision.engine import OpenJevEngine
        engine = OpenJevEngine(
            model_id_or_path="convaiinnovations/laya",
            allow_cpu_fallback=True,
        )
        engine.load_model()
        _mcp_module._engine_instance = engine
    return _mcp_module._engine_instance


class TestConsultaIntegration(unittest.IsolatedAsyncioTestCase):
    """Fluxo completo de consulta direta contra o engine Laya real."""

    @classmethod
    def setUpClass(cls):
        _ensure_engine_loaded()

    async def test_consulta_retorna_indicio_sem_passed(self):
        """Resposta deve ser indício textual — nunca booleano de aprovação."""
        result = await openjev_consulta(
            premise="def add(a, b):\n    return a + b\nassert add(1, 2) == 3",
            question="Este script funciona para somar dois inteiros?",
        )

        self.assertNotIn("passed", result, "Consulta não pode expor campo passed.")
        self.assertIn("indicio", result)
        self.assertIn("probability", result)
        self.assertIn("confidence", result)
        self.assertIn("latency_ms", result)
        self.assertGreaterEqual(result["probability"], 0.0)
        self.assertLessEqual(result["probability"], 1.0)
        self.assertGreaterEqual(result["confidence"], 0.0)
        self.assertLessEqual(result["confidence"], 1.0)
        self.assertTrue(len(result["indicio"]) > 0)
        self.assertIn("INDÍCIO", result["indicio"])

        # Contrato Pydantic valida o payload real retornado pelo motor
        ConsultaResponse.model_validate(result)

    async def test_consulta_schema_rejects_passed_injection(self):
        """Contrato rígido: tentar validar payload com `passed` deve falhar."""
        with self.assertRaises(ValidationError):
            ConsultaResponse.model_validate(
                {
                    "indicio": "x",
                    "probability": 0.5,
                    "confidence": 0.0,
                    "latency_ms": 1.0,
                    "passed": True,
                }
            )

    async def test_consulta_rejeita_campos_vazios(self):
        """Fail-fast: premissa ou pergunta vazias são recusadas sem inferência."""
        with self.assertRaises(ValueError):
            await openjev_consulta(premise="   ", question="funciona?")
        with self.assertRaises(ValueError):
            await openjev_consulta(premise="código x", question="  ")


if __name__ == "__main__":
    unittest.main(verbosity=2)
