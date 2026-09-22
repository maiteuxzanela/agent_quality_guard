"""Testes de integração para o Circuit Breaker e Trava Anti-Premise Gaming do MCP Server.

Sem mocks de engine — usa o engine Laya real via singleton do mcp_server (AGENTS.md §5.2).
Repositórios git reais criados com subprocess para produzir git_state autêntico.

Estratégia de threshold para controlar outcome sem mock:
  - threshold=5.0 → força reprovação real para testar o ciclo anti-gaming
  - threshold=1.0 → força aprovação real para testar reset de histórico
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

import src.mcp_server as _mcp_module
from src.mcp_server import openjev_score, _score_history, _get_workspace_git_signature


# ─── Engine real ──────────────────────────────────────────────────────────────

def _ensure_engine_loaded():
    if _mcp_module._engine_instance is None:
        from src.decision.engine import OpenJevEngine
        engine = OpenJevEngine(
            model_id_or_path="convaiinnovations/laya",
            allow_cpu_fallback=True,
        )
        engine.load_model()
        _mcp_module._engine_instance = engine
    return _mcp_module._engine_instance


def _make_git_repo() -> str:
    """Cria repositório git real com commit inicial."""
    tmp = tempfile.mkdtemp(prefix="jev_cb_")
    subprocess.run(["git", "init", tmp], check=True, capture_output=True)
    subprocess.run(["git", "-C", tmp, "config", "user.email", "test@jev.local"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", tmp, "config", "user.name", "JEV Test"],
                   check=True, capture_output=True)
    (Path(tmp) / "README.md").write_text("# CB test repo\n")
    subprocess.run(["git", "-C", tmp, "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", tmp, "commit", "-m", "init"], check=True, capture_output=True)
    return tmp


class TestCircuitBreakerAndAntiGaming(unittest.IsolatedAsyncioTestCase):

    @classmethod
    def setUpClass(cls):
        _ensure_engine_loaded()

    def setUp(self):
        _score_history.clear()
        self.ws = _make_git_repo()

    def tearDown(self):
        _score_history.clear()

    async def test_anti_premise_gaming_blocks_quando_git_inalterado(self):
        """Após reprovação real, nova chamada sem commit deve ser recusada."""
        # 1ª chamada: threshold=5.0 garante reprovação real pelo Laya
        res1 = await openjev_score(
            premise="Código sem testes, sem docs, sem tratamento de erros.",
            workspace=self.ws,
            threshold=5.0,
        )
        self.assertFalse(res1["passed"])
        self.assertNotIn("error", res1)

        # 2ª chamada: mesmo git_state → deve ser bloqueada
        res2 = await openjev_score(
            premise="Tentativa de re-avaliar sem alterar o repositório.",
            workspace=self.ws,
            threshold=5.0,
        )
        self.assertIn("error", res2)
        self.assertIn("AVALIAÇÃO RECUSADA", res2["error"])
        self.assertFalse(res2["emergency_exit_unlocked"])

    async def test_circuit_breaker_desbloqueia_na_tentativa_10(self):
        """Saída de emergência deve ser ativada após 10 tentativas sem mudança."""
        git_state_real = _get_workspace_git_signature(self.ws)

        # Pré-carrega histórico com 9 tentativas já realizadas
        _score_history[self.ws] = {
            "passed": False,
            "attempts": 9,
            "git_state": git_state_real,
            "last_score": 3.20,
        }

        # 10ª chamada: ainda bloqueada, incrementa para 10
        res10 = await openjev_score(
            premise="Tentativa 10 sem refatoração.",
            workspace=self.ws,
            threshold=5.0,
        )
        self.assertIn("error", res10)
        self.assertEqual(_score_history[self.ws]["attempts"], 10)

        # 11ª chamada: desbloqueia o circuit breaker
        res_cb = await openjev_score(
            premise="Chamada que deve acionar o circuit breaker.",
            workspace=self.ws,
            threshold=5.0,
        )
        self.assertTrue(res_cb.get("emergency_exit_unlocked"))
        self.assertIn("SAÍDA DE EMERGÊNCIA DESBLOQUEADA", res_cb.get("message", ""))

    async def test_aprovacao_real_reseta_historico(self):
        """Aprovação real (threshold=1.0) deve zerar tentativas no histórico."""
        # Pré-carrega histórico com reprovações anteriores
        old_git = "estado_antigo_diferente:clean"
        _score_history[self.ws] = {
            "passed": False,
            "attempts": 3,
            "git_state": old_git,
            "last_score": 2.80,
        }

        # threshold=1.0 garante aprovação — git_state atual é diferente do antigo
        res = await openjev_score(
            premise="Função add(a, b): return a + b. Teste: assert add(1, 2) == 3.",
            workspace=self.ws,
            threshold=1.0,
        )
        self.assertTrue(res["passed"])
        self.assertGreaterEqual(res["score"], 1.0)

        # Histórico deve ser resetado
        self.assertTrue(_score_history[self.ws]["passed"])
        self.assertEqual(_score_history[self.ws]["attempts"], 0)

    async def test_isolamento_entre_workspaces(self):
        """Estado de tentativas em um workspace não contamina outro workspace."""
        ws2 = _make_git_repo()

        # ws1 tem histórico de reprovação
        git_state_ws1 = _get_workspace_git_signature(self.ws)
        _score_history[self.ws] = {
            "passed": False,
            "attempts": 5,
            "git_state": git_state_ws1,
            "last_score": 3.0,
        }

        # ws2 não tem histórico — threshold=1.0 deve passar normalmente
        res_ws2 = await openjev_score(
            premise="Código limpo com testes.",
            workspace=ws2,
            threshold=1.0,
        )
        self.assertTrue(res_ws2["passed"])
        self.assertNotIn("error", res_ws2)

        # ws1 mantém tentativas inalteradas
        self.assertEqual(_score_history[self.ws]["attempts"], 5)

    async def test_novo_commit_desbloqueia_avaliacao(self):
        """Novo commit real muda git_state e deve permitir nova avaliação."""
        # 1ª chamada: reprova
        res1 = await openjev_score(
            premise="Código sem implementação.",
            workspace=self.ws,
            threshold=5.0,
        )
        self.assertFalse(res1["passed"])

        # Cria novo commit real
        (Path(self.ws) / "fix.py").write_text("def fix(): return 42\n")
        subprocess.run(["git", "-C", self.ws, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.ws, "commit", "-m", "fix: implementacao real"],
                       check=True, capture_output=True)

        # Nova avaliação após commit deve ser aceita (não bloqueada)
        res2 = await openjev_score(
            premise="Código com fix implementado.",
            workspace=self.ws,
            threshold=1.0,
        )
        self.assertNotIn("error", res2)
        self.assertTrue(res2["passed"])

    def test_git_signature_retorna_string_com_separador(self):
        """Assinatura Git deve conter ':' separando hash do status."""
        sig = _get_workspace_git_signature(self.ws)
        self.assertIsInstance(sig, str)
        self.assertIn(":", sig)
        self.assertGreater(len(sig), 3)


if __name__ == "__main__":
    unittest.main(verbosity=2)
