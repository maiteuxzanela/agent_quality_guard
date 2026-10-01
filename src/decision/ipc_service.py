"""Serviço de Comunicação Inter-Processos (IPC) via Unix Domain Socket para o Open Jev.

Permite que processos secundários (outras janelas da IDE, scripts de auditoria, CLI,
subagentes) compartilhem a instância do modelo Laya alocada na GPU (GTX 1050 Ti) pelo
processo primário do servidor MCP.

Elimina o custo oculto de fallback para CPU:
- Economiza ~2.3 GB de RAM física por processo adicional.
- Acelera a inferência de ~15-25s na CPU para ~470ms na GPU.
- Impede contenção de VRAM e disparos do OOM-killer do Linux.
"""

import asyncio
import json
import logging
import os
import socket
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import urlunsplit

logger = logging.getLogger("openjev.ipc")

SOCKET_PATH = "/tmp/openjev_ipc.sock"
HTTP_BRIDGE_URL = os.getenv(
    "OPENJEV_HTTP_BRIDGE_URL",
    urlunsplit(("http", "127.0.0.1:8000", "", "", "")),
)


def is_http_available(url: str = HTTP_BRIDGE_URL) -> bool:
    """Verifica se a API HTTP do motor na GPU está respondendo em loopback.

    Exemplo:
        >>> isinstance(is_http_available(), bool)
        True
    """
    try:
        req = urllib.request.Request(f"{url}/v1/decision/health", method="GET")
        with urllib.request.urlopen(req, timeout=0.4) as resp:
            return resp.status == 200
    except Exception:
        return False


def is_ipc_available(socket_path: str = SOCKET_PATH) -> bool:
    """Verifica de forma rápida e não bloqueante se o servidor IPC ou a ponte HTTP está ativo.

    Exemplo:
        >>> isinstance(is_ipc_available(), bool)
        True
    """
    if os.path.exists(socket_path):
        sock: Optional[socket.socket] = None
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(0.3)
            sock.connect(socket_path)
            # Envia ping rápido
            sock.sendall(json.dumps({"action": "ping"}).encode("utf-8") + b"\n")
            data = sock.recv(1024)
            if data:
                resp = json.loads(data.decode("utf-8").strip())
                return resp.get("status") == "ok"
            return False
        except Exception:
            return False
        finally:
            if sock is not None:
                sock.close()
    # Se testando um socket customizado explícito inexistente, não faz fallback para HTTP
    if socket_path != SOCKET_PATH:
        return False
    return is_http_available()


def _read_ipc_line(sock: socket.socket, max_reads: int = 10000) -> str:
    """Lê do socket até a quebra de linha, com limite defensivo de iterações."""
    buffer = bytearray()
    for _ in range(max_reads):
        chunk = sock.recv(4096)
        if not chunk:
            break
        buffer.extend(chunk)
        if b"\n" in chunk:
            break
    return buffer.decode("utf-8").strip()


def call_ipc_sync(action: str, payload: Dict[str, Any], timeout: float = 120.0, socket_path: str = SOCKET_PATH) -> Dict[str, Any]:
    """Envia requisição síncrona ao servidor IPC ou via HTTP Bridge se o socket não estiver acessível.

    Exemplo:
        >>> try:
        ...     call_ipc_sync("ping", {}, socket_path="/nonexistent/openjev.sock")
        ... except ConnectionError as exc:
        ...     "indisponíveis" in str(exc)
        True
    """
    if os.path.exists(socket_path):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(timeout)
                sock.connect(socket_path)
                msg = json.dumps({"action": action, "payload": payload}, ensure_ascii=False) + "\n"
                sock.sendall(msg.encode("utf-8"))

                line = _read_ipc_line(sock)
                if not line:
                    raise RuntimeError("Servidor IPC retornou resposta vazia.")
                res = json.loads(line)
                if "error" in res:
                    raise RuntimeError(f"Erro no IPC Open Jev: {res['error']}")
                return res.get("data", res)
        except Exception as e:
            logger.debug(f"Falha de conexão no socket IPC ({e}), tentando HTTP Bridge...")

    # Fallback transparente para HTTP Bridge (suporta sandbox bwrap)
    if action in {"noul", "choice", "score"}:
        endpoint = f"{HTTP_BRIDGE_URL}/v1/decision/{action}"
        data_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        req = urllib.request.Request(endpoint, data=data_bytes, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    raise ConnectionError(f"Servidor IPC e HTTP Bridge indisponíveis para ação '{action}'.")


async def call_ipc_async(action: str, payload: Dict[str, Any], timeout: float = 60.0, socket_path: str = SOCKET_PATH) -> Dict[str, Any]:
    """Envia requisição assíncrona ao servidor IPC usando asyncio.to_thread para I/O não bloqueante."""
    return await asyncio.to_thread(call_ipc_sync, action, payload, timeout, socket_path)


class OpenJevIPCServer:
    """Servidor Unix Domain Socket hospedado pelo processo detentor do modelo na GPU."""

    def __init__(self, engine: Any, socket_path: str = SOCKET_PATH):
        self.engine = engine
        self.socket_path = socket_path
        self._server: Optional[asyncio.Server] = None
        self._running = False

    async def start(self) -> None:
        """Inicia o servidor socket."""
        # Limpa socket órfão anterior
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError as e:
                logger.warning(f"Não foi possível remover socket anterior {self.socket_path}: {e}")

        self._server = await asyncio.start_unix_server(
            self._handle_client,
            path=self.socket_path,
        )
        # Permissões do socket para o usuário local
        try:
            os.chmod(self.socket_path, 0o600)
        except OSError as e:
            logger.debug(f"Não foi possível ajustar permissões de {self.socket_path}: {e}")

        self._running = True
        logger.info(f"Servidor IPC Open Jev iniciado em {self.socket_path} (Device: {getattr(self.engine, 'target_device', 'unknown')})")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        """Trata requisições de clientes locais."""
        try:
            line = await reader.readline()
            if not line:
                return

            req = json.loads(line.decode("utf-8").strip())
            action = req.get("action", "")
            payload = req.get("payload", {})

            if action == "ping":
                resp = {"status": "ok", "device": getattr(self.engine, "target_device", "unknown")}
            elif action == "choice":
                from src.core.schemas import ChoiceRequest
                req_obj = ChoiceRequest(**payload)
                choice_res = await self.engine.evaluate_choice(req_obj)
                resp = {"status": "ok", "data": choice_res.model_dump()}
            elif action == "noul":
                from src.core.schemas import NoulRequest
                req_obj = NoulRequest(**payload)
                noul_res = await self.engine.evaluate_noul(req_obj)
                resp = {"status": "ok", "data": noul_res.model_dump()}
            elif action == "score":
                from src.core.schemas import ScoreRequest
                req_obj = ScoreRequest(**payload)
                score_res = await self.engine.evaluate_score(req_obj)
                resp = {"status": "ok", "data": score_res.model_dump()}
            elif action == "consulta":
                from src.core.schemas import ConsultaRequest
                req_obj = ConsultaRequest(**payload)
                consulta_res = await self.engine.evaluate_consulta(req_obj)
                resp = {"status": "ok", "data": consulta_res.model_dump()}
            elif action == "needle3_search":
                from src.memory.local_needle import LocalNeedleEngine
                from src.core.schemas import Needle3Query
                query_str = str(payload.get("query", ""))
                max_tok = int(payload.get("max_tokens", 1000))
                scope_val = payload.get("scope") or []
                ws_dir = str(payload.get("workspace", "")) or os.getcwd()
                needle_engine = LocalNeedleEngine(workspace_root=ws_dir)
                needle_query = Needle3Query(query=query_str, max_tokens=max_tok, scope=scope_val)
                needle_res = await needle_engine.search(needle_query)
                resp = {"status": "ok", "data": needle_res.model_dump()}
            elif action == "verify_delivery":
                from src.mcp_server import needle_verify_delivery
                workspace = str(payload.get("workspace", ""))
                attempt_number = int(payload.get("attempt_number", 1))
                task_type = str(payload.get("task_type", "codigo_producao"))
                pedido = str(payload.get("pedido", ""))
                resultado = str(payload.get("resultado", ""))
                delivery_res = await needle_verify_delivery(
                    task_type=task_type,
                    workspace=workspace,
                    attempt_number=attempt_number,
                    pedido=pedido,
                    resultado=resultado,
                )
                resp = {"status": "ok", "data": delivery_res}
            else:
                resp = {"status": "error", "error": f"Ação desconhecida: {action}"}

            out_bytes = json.dumps(resp, ensure_ascii=False).encode("utf-8") + b"\n"
            writer.write(out_bytes)
            await writer.drain()
        except Exception as e:
            logger.error(f"Erro ao processar requisição IPC: {e}", exc_info=True)
            err_bytes = json.dumps({"status": "error", "error": str(e)}).encode("utf-8") + b"\n"
            try:
                writer.write(err_bytes)
                await writer.drain()
            except Exception as write_err:
                logger.debug(f"Falha ao enviar mensagem de erro para o cliente IPC: {write_err}")
        finally:
            try:
                writer.close()
                await writer.wait_closed()
            except Exception as close_err:
                logger.debug(f"Erro ao fechar conexão com cliente IPC: {close_err}")

    async def stop(self) -> None:
        """Encerra o servidor e remove o arquivo socket."""
        self._running = False
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        if os.path.exists(self.socket_path):
            try:
                os.unlink(self.socket_path)
            except OSError as unlink_err:
                logger.debug(f"Não foi possível remover arquivo socket {self.socket_path}: {unlink_err}")
        logger.info("Servidor IPC Open Jev encerrado com sucesso.")
