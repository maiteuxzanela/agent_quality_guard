"""Daemon Permanente Open JEV (GPU / Unix Domain Socket IPC).

Projetado para execução contínua via systemd user service (openjev.service).
Mantém o modelo Laya pré-carregado na VRAM da GPU NVIDIA GTX 1050 Ti e
atende requisições de todos os 5 projetos ativos via /tmp/openjev_ipc.sock.
"""

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

# Injeta a raiz do projeto JEV no sys.path
ROOT_DIR = Path(__file__).parent.parent.parent.resolve()
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from src.decision.hardware import try_acquire_gpu_lock
from src.decision.ipc_service import OpenJevIPCServer, SOCKET_PATH
import src.mcp_server as _mcp_server
from src.mcp_server import get_engine

# O daemon é o detentor único do socket IPC: impede que get_engine() suba um
# segundo OpenJevIPCServer (corrida EADDRINUSE na thread jev-ipc-service).
_mcp_server._ipc_disabled = True

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [OpenJEV Daemon] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger("openjev.daemon")


async def run_daemon():
    logger.info("=" * 75)
    logger.info("INICIANDO DAEMON PERMANENTE OPEN JEV (GPU / IPC)")
    logger.info(f"Raiz do Repositório: {ROOT_DIR}")
    logger.info(f"Socket IPC: {SOCKET_PATH}")
    logger.info(f"PID: {os.getpid()}")
    logger.info("=" * 75)

    # 1. Adquire Lock Exclusivo da GPU
    has_gpu_lock = try_acquire_gpu_lock()
    if has_gpu_lock:
        logger.info("Lock exclusivo de GPU adquirido com sucesso (/tmp/openjev_gpu.lock).")
    else:
        logger.warning(
            "Lock de GPU indisponível ou já retido por outro processo. "
            "Tentando carregar motor com resiliência..."
        )

    # 2. Pré-carrega o modelo Laya na GPU (GTX 1050 Ti)
    logger.info("Carregando modelo Laya RLCD na GPU...")
    engine = get_engine()
    target_dev = getattr(engine, "target_device", "unknown")
    vram_free = getattr(engine.profile, "free_vram_mb", 0)
    logger.info(f"Motor carregado e aquecido com sucesso! Dispositivo: {target_dev} | VRAM Livre: {vram_free} MB")

    # 3. Inicia o Servidor IPC Socket com garantia de fechamento de recursos
    server = OpenJevIPCServer(engine=engine, socket_path=SOCKET_PATH)
    try:
        await server.start()

        # Garante permissões de leitura/escrita no socket para o usuário
        try:
            os.chmod(SOCKET_PATH, 0o666)
        except OSError as e:
            logger.debug(f"Não foi possível aplicar chmod 0666 em {SOCKET_PATH}: {e}")

        logger.info(f"Servidor IPC escutando ativamente em {SOCKET_PATH}.")

        # 4. Trata sinais de encerramento do systemd (SIGTERM, SIGINT)
        stop_event = asyncio.Event()

        def _handle_signal(sig_num):
            logger.info(f"Sinal {sig_num} recebido. Encerrando daemon de forma graciosa...")
            stop_event.set()

        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _handle_signal, sig)
            except NotImplementedError as nie:
                logger.debug(f"Signal handling not supported on current loop: {nie}")

        # 5. Mantém o serviço ativo
        logger.info("Daemon Open JEV pronto para atender requisições multi-repo.")
        await stop_event.wait()
    finally:
        # 6. Encerramento gracioso e fechamento obrigatório de recursos
        logger.info("Encerrando socket IPC e liberando recursos...")
        await server.stop()
        logger.info("Daemon Open JEV finalizado com sucesso.")


def main():
    try:
        asyncio.run(run_daemon())
    except (KeyboardInterrupt, SystemExit) as sig_exit:
        logger.info(f"Encerramento solicitado: {type(sig_exit).__name__}")
        sys.exit(0)
    except Exception as e:
        logger.critical(f"Erro fatal no daemon Open JEV: {e}", exc_info=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
