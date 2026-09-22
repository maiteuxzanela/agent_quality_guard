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
        # Deve ter chamado choice exatamente 2 vezes
        self.assertEqual(mock_engine.evaluate_choice.call_count, 2)


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
