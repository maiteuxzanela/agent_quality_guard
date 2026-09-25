"""Testes de integração do cliente leve IPC do Open JEV (sem mocks)."""

from __future__ import annotations

import unittest
from pathlib import Path

from src.client.jev_ipc_client import (
    DEFAULT_SOCKET_PATH,
    call_jev_ipc,
    is_ipc_available,
    request_score_and_diagnose,
)


class TestJevIpcClient(unittest.TestCase):
    """Testa a conectividade e chamadas de protocolo contra o socket IPC ativo."""

    @unittest.skipUnless(is_ipc_available(DEFAULT_SOCKET_PATH), "Servidor IPC Open Jev não está ativo em /tmp/openjev_ipc.sock")
    def test_ipc_is_available(self):
        """Valida se o socket /tmp/openjev_ipc.sock está ativo e respondendo."""
        self.assertTrue(
            is_ipc_available(DEFAULT_SOCKET_PATH),
            f"O socket {DEFAULT_SOCKET_PATH} deveria estar ativo no ambiente.",
        )

    def test_ipc_nonexistent_socket_returns_false(self):
        """Socket inexistente retorna False imediatamente sem travar."""
        self.assertFalse(is_ipc_available("/tmp/socket_que_nao_existe_12345.sock"))

    def test_call_nonexistent_socket_raises_connection_error(self):
        """Chamada a socket inexistente levanta ConnectionError explicativo."""
        with self.assertRaises(ConnectionError) as ctx:
            call_jev_ipc("ping", socket_path="/tmp/socket_que_nao_existe_12345.sock")
        self.assertIn("não está ativo", str(ctx.exception))

    @unittest.skipUnless(is_ipc_available(DEFAULT_SOCKET_PATH), "Servidor IPC Open Jev não está ativo em /tmp/openjev_ipc.sock")
    def test_call_unknown_action_raises_runtime_error(self):
        """Ação desconhecida retorna status=error e levanta RuntimeError."""
        with self.assertRaises(RuntimeError) as ctx:
            call_jev_ipc("acao_inexistente_de_teste_xyz")
        self.assertIn("Ação desconhecida", str(ctx.exception))

    @unittest.skipUnless(is_ipc_available(DEFAULT_SOCKET_PATH), "Servidor IPC Open Jev não está ativo em /tmp/openjev_ipc.sock")
    def test_ipc_ping_action(self):
        """Envia ping síncrono e valida resposta de status do daemon."""
        res = call_jev_ipc("ping")
        self.assertEqual(res.get("status"), "ok")
        self.assertIn("device", res)

    @unittest.skipUnless(is_ipc_available(DEFAULT_SOCKET_PATH), "Servidor IPC Open Jev não está ativo em /tmp/openjev_ipc.sock")
    def test_request_score_and_diagnose_integration(self):
        """Valida que o cliente leve despacha a avaliação oficial e retorna estrutura ScoreDiagnoseResponse."""
        premise = (
            "Pedido do Prompt: Validar integridade dos contratos e suites de testes. | "
            "Resultado Executado: Testes reais 100% verdes sem mocks em tests/."
        )
        res = request_score_and_diagnose(
            premise=premise,
            workspace="/home/maiteuxzanela/projeto JEV",
            threshold=3.40,
            reset_history=True,
        )
        self.assertIn("score", res)
        self.assertIn("passed", res)
        self.assertIn("attempts", res)
        self.assertIn("stages", res)
        self.assertGreaterEqual(res["score"], 1.0)
        self.assertLessEqual(res["score"], 5.0)


if __name__ == "__main__":
    unittest.main()
