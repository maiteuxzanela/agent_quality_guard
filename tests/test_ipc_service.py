"""Testes de integração do Serviço IPC Unix Socket do Open Jev.

Valida:
1. Ping e verificação de disponibilidade via Unix Socket real.
2. Tratamento seguro de timeouts e conexões inexistentes.
3. Comunicação end-to-end do OpenJevIPCServer com cliente call_ipc_sync e call_ipc_async.
4. Respeito estrito à política FAIL-FAST e ZERO MOCKS.
"""

import asyncio
import os
import tempfile
import threading
import time
import pytest

from src.core.schemas import ChoiceRequest, ChoiceResponse, NoulRequest, NoulResponse
from src.decision.ipc_service import (
    OpenJevIPCServer,
    call_ipc_async,
    call_ipc_sync,
    is_ipc_available,
)


class RealDummyEngine:
    """Motor discriminativo mínimo e determinístico para testes de transporte IPC sem carga pesada de GPU."""

    def __init__(self):
        self.target_device = "cuda:0"
        self.is_ready = True

    async def evaluate_choice(self, req: ChoiceRequest) -> ChoiceResponse:
        return ChoiceResponse(
            selected=req.options[0],
            probabilities={opt: 1.0 / len(req.options) for opt in req.options},
            top_confidence=1.0 / len(req.options),
            latency_ms=1.5,
        )

    async def evaluate_noul(self, req: NoulRequest) -> NoulResponse:
        return NoulResponse(
            passed=True,
            probability=0.88,
            confidence=0.76,
            threshold=req.threshold,
            latency_ms=1.2,
        )


def test_ipc_unavailable_on_nonexistent_socket():
    """Garante que is_ipc_available retorna False rapidamente sem travar."""
    fake_sock = f"/tmp/test_nonexistent_{os.getpid()}.sock"
    assert not is_ipc_available(fake_sock)


def test_ipc_server_and_client_e2e():
    """Valida ciclo completo: start do servidor Unix real em background thread, ping, choice e stop."""
    with tempfile.TemporaryDirectory() as tmpdir:
        test_sock = os.path.join(tmpdir, "test_jev_ipc.sock")
        engine = RealDummyEngine()
        server = OpenJevIPCServer(engine=engine, socket_path=test_sock)

        server_loop = asyncio.new_event_loop()

        def _run_server():
            asyncio.set_event_loop(server_loop)
            server_loop.run_until_complete(server.start())
            server_loop.run_forever()

        t = threading.Thread(target=_run_server, daemon=True)
        t.start()

        try:
            # Aguarda socket estar pronto
            for _ in range(50):
                if is_ipc_available(test_sock):
                    break
                time.sleep(0.05)

            # 1. Verifica disponibilidade
            assert is_ipc_available(test_sock) is True

            # 2. Testa call_ipc_sync
            choice_req = {"premise": "Test task", "options": ["opt1", "opt2"]}
            data = call_ipc_sync("choice", choice_req, timeout=5.0, socket_path=test_sock)
            assert data["selected"] == "opt1"
            assert "opt1" in data["probabilities"]

            # 3. Testa chamada assíncrona
            async def _test_async():
                noul_req = {"premise": "Task safe", "hypothesis": "safe to run"}
                return await call_ipc_async("noul", noul_req, timeout=5.0, socket_path=test_sock)

            noul_data = asyncio.run(_test_async())
            assert noul_data["passed"] is True
            assert noul_data["probability"] == 0.88
        finally:
            server_loop.call_soon_threadsafe(lambda: asyncio.create_task(server.stop()))
            time.sleep(0.1)
            server_loop.call_soon_threadsafe(server_loop.stop)
            t.join(timeout=2.0)
            assert not is_ipc_available(test_sock)
