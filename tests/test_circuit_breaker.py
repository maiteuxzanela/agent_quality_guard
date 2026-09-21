"""Testes para o Circuit Breaker e Trava Anti-Premise Gaming do MCP Server."""

import unittest
from unittest.mock import patch, MagicMock, AsyncMock
from src.mcp_server import openjev_score, _score_history, _get_workspace_git_signature
from src.core.schemas import ScoreResponse


class TestCircuitBreakerAndAntiGaming(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        _score_history.clear()

    def tearDown(self):
        _score_history.clear()

    async def test_anti_premise_gaming_blocks_when_git_unchanged(self):
        """Verifica se o servidor MCP recusa reavaliações quando o repositório não é alterado após reprovação."""
        workspace_dir = "/tmp/test_workspace_anti_gaming"

        with patch("src.mcp_server._get_workspace_git_signature", return_value="commit_abc:status_clean"):
            mock_engine = MagicMock()
            mock_resp_fail = ScoreResponse(
                score=3.10,
                normalized_score=0.525,
                passed=False,
                breakdown={"acceptable": 0.5, "poor quality": 0.5},
                latency_ms=12.0,
            )
            mock_engine.evaluate_score = AsyncMock(return_value=mock_resp_fail)

            with patch("src.mcp_server.get_engine", return_value=mock_engine):
                # 1ª Tentativa: Executa normalmente e reprova
                res1 = await openjev_score(
                    premise="Premissa inicial",
                    workspace=workspace_dir,
                    threshold=3.75,
                )
                self.assertEqual(res1["score"], 3.10)
                self.assertFalse(res1["passed"])

                # 2ª Tentativa: Tenta reavaliar com MESMO git signature (sem alterações no disco)
                res2 = await openjev_score(
                    premise="Premissa alterada para tentar enganar o JEV",
                    workspace=workspace_dir,
                    threshold=3.75,
                )
                self.assertIn("error", res2)
                self.assertIn("AVALIAÇÃO RECUSADA", res2["error"])
                self.assertFalse(res2["emergency_exit_unlocked"])
                self.assertEqual(res2["attempts"], 1)

    async def test_circuit_breaker_unlocks_at_attempt_10(self):
        """Verifica se a Saída de Emergência é desbloqueada após 10 tentativas sem sucesso."""
        workspace_dir = "/tmp/test_workspace_circuit_breaker"

        _score_history[workspace_dir] = {
            "passed": False,
            "attempts": 9,
            "git_state": "fixed_commit:no_changes",
            "last_score": 3.20,
        }

        with patch("src.mcp_server._get_workspace_git_signature", return_value="fixed_commit:no_changes"):
            # 10ª chamada com mesmo estado (tentando sem refatorar após 9 falhas)
            res10 = await openjev_score(
                premise="Tentativa desesperada",
                workspace=workspace_dir,
                threshold=3.75,
            )
            self.assertIn("error", res10)
            self.assertEqual(res10["attempts"], 9)
            self.assertEqual(_score_history[workspace_dir]["attempts"], 10)

            # Próxima chamada (tentativa >= 10) desbloqueia o Circuit Breaker
            res_cb = await openjev_score(
                premise="Chamada que aciona o Circuit Breaker",
                workspace=workspace_dir,
                threshold=3.75,
            )
            self.assertTrue(res_cb.get("emergency_exit_unlocked"))
            self.assertIn("SAÍDA DE EMERGÊNCIA DESBLOQUEADA", res_cb.get("message", ""))

    async def test_successful_score_resets_history(self):
        """Verifica se aprovação (score >= threshold) reseta o histórico e tentativas."""
        workspace_dir = "/tmp/test_workspace_success"

        with patch("src.mcp_server._get_workspace_git_signature", return_value="commit_xyz:status_ok"):
            mock_engine = MagicMock()
            mock_resp_pass = ScoreResponse(
                score=4.20,
                normalized_score=0.80,
                passed=True,
                breakdown={"good quality": 0.6, "exceptional": 0.4},
                latency_ms=10.0,
            )
            mock_engine.evaluate_score = AsyncMock(return_value=mock_resp_pass)

            with patch("src.mcp_server.get_engine", return_value=mock_engine):
                res = await openjev_score(
                    premise="Premissa com código aprovado",
                    workspace=workspace_dir,
                    threshold=3.75,
                )
                self.assertEqual(res["score"], 4.20)
                self.assertTrue(res["passed"])
                self.assertTrue(_score_history[workspace_dir]["passed"])
                self.assertEqual(_score_history[workspace_dir]["attempts"], 0)

    async def test_workspace_isolation(self):
        """Verifica que o estado de tentativas em um workspace não contamina outro workspace."""
        ws1 = "/tmp/ws_one"
        ws2 = "/tmp/ws_two"

        _score_history[ws1] = {
            "passed": False,
            "attempts": 5,
            "git_state": "state1",
            "last_score": 3.0,
        }

        with patch("src.mcp_server._get_workspace_git_signature", return_value="state2"):
            mock_engine = MagicMock()
            mock_resp = ScoreResponse(
                score=3.90,
                normalized_score=0.725,
                passed=True,
                breakdown={"good quality": 0.8},
                latency_ms=10.0,
            )
            mock_engine.evaluate_score = AsyncMock(return_value=mock_resp)

            with patch("src.mcp_server.get_engine", return_value=mock_engine):
                res2 = await openjev_score(
                    premise="Workspace two task",
                    workspace=ws2,
                    threshold=3.75,
                )
                self.assertTrue(res2["passed"])
                self.assertEqual(res2["score"], 3.90)
                # ws1 continua com suas tentativas inalteradas
                self.assertEqual(_score_history[ws1]["attempts"], 5)

    def test_git_signature_computation(self):
        """Verifica a extração robusta da assinatura Git com subprocess."""
        sig = _get_workspace_git_signature(".")
        self.assertIsInstance(sig, str)
        self.assertIn(":", sig)


if __name__ == "__main__":
    unittest.main(verbosity=2)
