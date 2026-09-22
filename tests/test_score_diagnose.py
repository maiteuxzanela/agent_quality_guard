"""Testes unitários para openjev_score_and_diagnose — cascata Score→Choice L1→Choice L2.

Cobre:
  - Aprovação direta (sem diagnóstico)
  - Reprovação com diagnóstico L1+L2 completo para cada macro-categoria
  - Trava anti-premise gaming (sem refatoração)
  - Saída de emergência após 10 tentativas
  - Integridade da taxonomia (todas as 20 combinações L1×L2 existem nos dicionários)
"""

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.core.schemas import (
    DEFECT_MACRO_CATEGORIES,
    DEFECT_MICRO_DESCRIPTIONS,
    DEFECT_MICRO_OPTIONS,
    ChoiceResponse,
    ScoreResponse,
)
from src.mcp_server import _score_history, openjev_score_and_diagnose


# ─── Fixtures ───────────────────────────────────────────────────────────────

def _make_score_resp(score: float, threshold: float = 3.60, passed: bool | None = None) -> ScoreResponse:
    _passed = passed if passed is not None else (score >= threshold)
    return ScoreResponse(
        score=score,
        normalized_score=(score - 1.0) / 4.0,
        passed=_passed,
        breakdown={"acceptable": 0.4, "good quality": 0.4, "poor quality": 0.2},
        latency_ms=42.0,
    )


def _make_choice_resp(selected: str, options: list[str]) -> ChoiceResponse:
    probs = {o: (0.7 if o == selected else 0.075) for o in options}
    return ChoiceResponse(
        selected=selected,
        probabilities=probs,
        top_confidence=0.7,
        latency_ms=15.0,
    )


WS = "/tmp/test_score_diagnose"


class TestScoreAndDiagnoseApproved(unittest.IsolatedAsyncioTestCase):
    """Caso: score >= threshold → retorna approved sem diagnóstico."""

    def setUp(self):
        _score_history.clear()

    def tearDown(self):
        _score_history.clear()

    async def test_approved_returns_no_diagnosis(self):
        with patch("src.mcp_server._get_workspace_git_signature", return_value="abc:clean"):
            mock_engine = MagicMock()
            mock_engine.evaluate_score = AsyncMock(return_value=_make_score_resp(3.80))

            with patch("src.mcp_server.get_engine", return_value=mock_engine):
                result = await openjev_score_and_diagnose(
                    premise="Código entregue com 51 testes passando.",
                    workspace=WS,
                    threshold=3.60,
                )

        self.assertTrue(result["passed"])
        self.assertIsNone(result["diagnosis"])
        self.assertEqual(result["attempts"], 0)
        self.assertFalse(result["emergency_exit_unlocked"])
        # Choice NÃO deve ser chamado em aprovação
        mock_engine.evaluate_choice.assert_not_called()


class TestScoreAndDiagnoseReprovado(unittest.IsolatedAsyncioTestCase):
    """Caso: score < threshold → dispara L1+L2 e retorna diagnóstico completo."""

    def setUp(self):
        _score_history.clear()

    def tearDown(self):
        _score_history.clear()

    async def test_reprovado_retorna_diagnostico_l1_e_l2(self):
        macro = "qualidade_de_testes"
        micro = "falta_cobertura_caminhos_de_erro"

        with patch("src.mcp_server._get_workspace_git_signature", return_value="abc:clean"):
            mock_engine = MagicMock()
            mock_engine.evaluate_score = AsyncMock(return_value=_make_score_resp(3.20))
            mock_engine.evaluate_choice = AsyncMock(side_effect=[
                _make_choice_resp(macro, DEFECT_MACRO_CATEGORIES),
                _make_choice_resp(micro, DEFECT_MICRO_OPTIONS[macro]),
            ])

            with patch("src.mcp_server.get_engine", return_value=mock_engine):
                result = await openjev_score_and_diagnose(
                    premise="Código entregue sem testes de falha.",
                    workspace=WS,
                    threshold=3.60,
                )

        self.assertFalse(result["passed"])
        self.assertIsNotNone(result["diagnosis"])

        diag = result["diagnosis"]
        self.assertEqual(diag["macro_category"], macro)
        self.assertEqual(diag["micro_diagnosis"], micro)
        self.assertIn("description", diag)
        self.assertTrue(len(diag["description"]) > 10)
        self.assertIn("macro_probabilities", diag)
        self.assertIn("micro_probabilities", diag)
        self.assertIn("macro_confidence", diag)
        self.assertIn("micro_confidence", diag)
        self.assertIn("diagnosis_latency_ms", diag)
        # Deve ter chamado choice exatamente 2 vezes
        self.assertEqual(mock_engine.evaluate_choice.call_count, 2)

        # Valida contrato completo do ScoreDiagnoseResponse serializado via Pydantic
        self.assertIn("score", result)
        self.assertIn("normalized_score", result)
        self.assertIn("threshold", result)
        self.assertIn("breakdown", result)
        self.assertIn("score_latency_ms", result)
        self.assertIn("attempts", result)
        self.assertIn("max_attempts", result)
        self.assertIn("emergency_exit_unlocked", result)
        self.assertEqual(result["max_attempts"], 10)
        self.assertIsInstance(result["score"], float)
        self.assertIsInstance(result["normalized_score"], float)
        self.assertIsInstance(result["breakdown"], dict)

    async def test_aprovado_schema_completo(self):
        """Verifica que a aprovação também serializa todos os campos obrigatórios."""
        with patch("src.mcp_server._get_workspace_git_signature", return_value="xyz:clean"):
            mock_engine = MagicMock()
            mock_engine.evaluate_score = AsyncMock(return_value=_make_score_resp(4.00))

            with patch("src.mcp_server.get_engine", return_value=mock_engine):
                result = await openjev_score_and_diagnose(
                    premise="Código aprovado.",
                    workspace=WS,
                    threshold=3.60,
                )

        self.assertTrue(result["passed"])
        self.assertIsNone(result["diagnosis"])
        self.assertIn("score_latency_ms", result)
        self.assertIn("normalized_score", result)
        self.assertIn("breakdown", result)
        self.assertIn("max_attempts", result)
        self.assertEqual(result["attempts"], 0)
        mock_engine.evaluate_choice.assert_not_called()


class TestAntiGamingAndCircuitBreaker(unittest.IsolatedAsyncioTestCase):
    """Cobre a trava anti-premise gaming e saída de emergência."""

    def setUp(self):
        _score_history.clear()

    def tearDown(self):
        _score_history.clear()

    async def test_anti_gaming_blocks_without_code_change(self):
        ws = "/tmp/ws_gaming_diagnose"
        _score_history[ws] = {
            "passed": False,
            "attempts": 2,
            "git_state": "fixed_hash:clean",
            "last_score": 3.10,
        }

        with patch("src.mcp_server._get_workspace_git_signature", return_value="fixed_hash:clean"):
            result = await openjev_score_and_diagnose(
                premise="Tentativa sem alterar o código.",
                workspace=ws,
            )

        self.assertIn("error", result)
        self.assertIn("AVALIAÇÃO RECUSADA", result["error"])
        self.assertFalse(result["emergency_exit_unlocked"])

    async def test_circuit_breaker_unlocks_at_10(self):
        ws = "/tmp/ws_cb_diagnose"
        _score_history[ws] = {
            "passed": False,
            "attempts": 10,
            "git_state": "fixed_hash:clean",
            "last_score": 3.00,
        }

        with patch("src.mcp_server._get_workspace_git_signature", return_value="fixed_hash:clean"):
            result = await openjev_score_and_diagnose(
                premise="Décima tentativa.",
                workspace=ws,
            )

        self.assertTrue(result.get("emergency_exit_unlocked"))
        self.assertIn("SAÍDA DE EMERGÊNCIA", result.get("message", ""))


class TestTaxonomiaIntegridade(unittest.TestCase):
    """Verifica que toda a taxonomia L1×L2 está corretamente mapeada."""

    def test_todas_macro_categorias_tem_4_micro_opcoes(self):
        for macro in DEFECT_MACRO_CATEGORIES:
            self.assertIn(macro, DEFECT_MICRO_OPTIONS)
            self.assertEqual(
                len(DEFECT_MICRO_OPTIONS[macro]),
                4,
                f"Macro-categoria '{macro}' deve ter exatamente 4 micro-opções.",
            )

    def test_todos_micro_diagnosticos_tem_descricao(self):
        for macro, micros in DEFECT_MICRO_OPTIONS.items():
            for micro in micros:
                self.assertIn(
                    micro,
                    DEFECT_MICRO_DESCRIPTIONS,
                    f"Micro-diagnóstico '{micro}' (de '{macro}') sem descrição no dicionário.",
                )
                self.assertTrue(
                    len(DEFECT_MICRO_DESCRIPTIONS[micro]) > 10,
                    f"Descrição do micro-diagnóstico '{micro}' está vazia ou muito curta.",
                )

    def test_total_combinacoes_l1_l2(self):
        """5 macro-categorias × 4 micro-diagnósticos = 20 combinações."""
        total = sum(len(v) for v in DEFECT_MICRO_OPTIONS.values())
        self.assertEqual(total, 20)

    def test_tool_catalog_contains_score_and_diagnose(self):
        from src.core.tools import get_tool_definition
        tool = get_tool_definition("openjev_score_and_diagnose")
        self.assertEqual(tool["function"]["name"], "openjev_score_and_diagnose")
        params = tool["function"]["parameters"]["properties"]
        self.assertIn("premise", params)
        self.assertIn("threshold", params)
        self.assertIn("workspace", params)


if __name__ == "__main__":
    unittest.main(verbosity=2)


# ─── Testes parametrizados standalone (pytest-asyncio) ──────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("macro", DEFECT_MACRO_CATEGORIES)
async def test_cada_macro_categoria_produz_diagnostico_valido(macro: str):
    """Verifica o fluxo L1→L2 para cada uma das 5 macro-categorias."""
    micro = DEFECT_MICRO_OPTIONS[macro][0]
    ws = f"/tmp/ws_param_{macro}"

    _score_history.clear()

    with patch("src.mcp_server._get_workspace_git_signature", return_value=f"hash_{macro}:clean"):
        mock_engine = MagicMock()
        mock_engine.evaluate_score = AsyncMock(return_value=_make_score_resp(2.50))
        mock_engine.evaluate_choice = AsyncMock(side_effect=[
            _make_choice_resp(macro, DEFECT_MACRO_CATEGORIES),
            _make_choice_resp(micro, DEFECT_MICRO_OPTIONS[macro]),
        ])

        with patch("src.mcp_server.get_engine", return_value=mock_engine):
            result = await openjev_score_and_diagnose(
                premise=f"Código de teste para macro={macro}.",
                workspace=ws,
                threshold=3.60,
            )

    assert not result["passed"]
    assert result["diagnosis"] is not None
    assert result["diagnosis"]["macro_category"] == macro
    assert result["diagnosis"]["micro_diagnosis"] == micro
    assert result["diagnosis"]["description"] != ""
    assert mock_engine.evaluate_choice.call_count == 2


# ─── Testes de integração Pydantic (sem mock do engine) ─────────────────────

class TestPydanticSchemaIntegration(unittest.TestCase):
    """Verifica que os schemas Pydantic DiagnosisResult e ScoreDiagnoseResponse
    constroem, validam e serializam corretamente sem mock do engine."""

    def test_diagnosis_result_instancia_e_serializa(self):
        from src.core.schemas import DiagnosisResult
        diag = DiagnosisResult(
            macro_category="qualidade_de_testes",
            macro_confidence=0.82,
            macro_probabilities={"qualidade_de_testes": 0.82, "robustez_e_limites": 0.18},
            micro_diagnosis="falta_cobertura_caminhos_de_erro",
            micro_confidence=0.74,
            micro_probabilities={
                "ausencia_de_testes_unitarios": 0.1,
                "teste_tautologico_ou_falso_positivo": 0.1,
                "falta_cobertura_caminhos_de_erro": 0.74,
                "testes_nao_reprodutiveis_flaky": 0.06,
            },
            description="Testes cobrem apenas o caminho feliz.",
            diagnosis_latency_ms=42.5,
        )
        dumped = diag.model_dump()
        self.assertEqual(dumped["macro_category"], "qualidade_de_testes")
        self.assertEqual(dumped["micro_diagnosis"], "falta_cobertura_caminhos_de_erro")
        self.assertAlmostEqual(dumped["macro_confidence"], 0.82)
        self.assertIsInstance(dumped["macro_probabilities"], dict)
        self.assertIsInstance(dumped["micro_probabilities"], dict)

    def test_score_diagnose_response_aprovado_serializa(self):
        from src.core.schemas import ScoreDiagnoseResponse
        resp = ScoreDiagnoseResponse(
            score=4.10,
            normalized_score=0.775,
            passed=True,
            threshold=3.60,
            breakdown={"good quality": 0.6, "exceptional": 0.4},
            score_latency_ms=120.0,
            diagnosis=None,
            attempts=0,
            max_attempts=10,
            emergency_exit_unlocked=False,
        )
        dumped = resp.model_dump()
        self.assertTrue(dumped["passed"])
        self.assertIsNone(dumped["diagnosis"])
        self.assertEqual(dumped["attempts"], 0)
        self.assertFalse(dumped["emergency_exit_unlocked"])

    def test_score_diagnose_response_reprovado_com_diagnosis(self):
        from src.core.schemas import DiagnosisResult, ScoreDiagnoseResponse
        diag = DiagnosisResult(
            macro_category="integridade_e_seguranca",
            macro_confidence=0.65,
            macro_probabilities={"integridade_e_seguranca": 0.65},
            micro_diagnosis="credenciais_hardcoded",
            micro_confidence=0.58,
            micro_probabilities={"credenciais_hardcoded": 0.58},
            description="Chaves de API inseridas diretamente no código-fonte.",
            diagnosis_latency_ms=33.1,
        )
        resp = ScoreDiagnoseResponse(
            score=2.80,
            normalized_score=0.45,
            passed=False,
            threshold=3.60,
            breakdown={"critical flaws": 0.3, "poor quality": 0.3, "acceptable": 0.4},
            score_latency_ms=98.0,
            diagnosis=diag,
            attempts=2,
            max_attempts=10,
            emergency_exit_unlocked=False,
        )
        dumped = resp.model_dump()
        self.assertFalse(dumped["passed"])
        self.assertIsNotNone(dumped["diagnosis"])
        self.assertEqual(dumped["diagnosis"]["micro_diagnosis"], "credenciais_hardcoded")
        self.assertEqual(dumped["attempts"], 2)

    def test_diagnosis_result_rejeita_macro_confidence_invalida(self):
        """Valida que DiagnosisResult enforça ge=0.0, le=1.0 em macro_confidence."""
        from pydantic import ValidationError
        from src.core.schemas import DiagnosisResult
        with self.assertRaises(ValidationError):
            DiagnosisResult(
                macro_category="qualidade_de_testes",
                macro_confidence=1.5,  # inválido: > 1.0
                macro_probabilities={},
                micro_diagnosis="ausencia_de_testes_unitarios",
                micro_confidence=0.5,
                micro_probabilities={},
                description="Test",
                diagnosis_latency_ms=10.0,
            )
