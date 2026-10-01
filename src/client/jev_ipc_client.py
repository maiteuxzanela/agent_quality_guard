"""Cliente IPC nativo e ultra-leve para o ecossistema Open JEV.

Permite que scripts de Stop hook (Guards), CLI, hooks e ferramentas em qualquer
repositório local se comuniquem com o daemon do servidor MCP residente na GPU
(GTX 1050 Ti) via Unix Domain Socket (/tmp/openjev_ipc.sock).

Zero dependências externas: utiliza exclusivamente a biblioteca padrão do Python
(socket, json, os, pathlib).
"""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_SOCKET_PATH = "/tmp/openjev_ipc.sock"


def is_ipc_available(socket_path: str = DEFAULT_SOCKET_PATH) -> bool:
    """Verifica de forma rápida (<50ms) se o servidor IPC Open JEV está ativo."""
    if not os.path.exists(socket_path):
        return False
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.3)
            sock.connect(socket_path)
            sock.sendall(b'{"action": "ping"}\n')
            with sock.makefile("r", encoding="utf-8", newline="\n") as reader:
                line = reader.readline()
                if line:
                    resp = json.loads(line)
                    return resp.get("status") == "ok"
            return False
    except Exception:
        return False


def call_jev_ipc(
    action: str,
    payload: Optional[Dict[str, Any]] = None,
    timeout: float = 120.0,
    socket_path: str = DEFAULT_SOCKET_PATH,
) -> Dict[str, Any]:
    """Envia requisição síncrona ao daemon Open JEV e retorna a resposta decodificada.

    Raises:
        ConnectionError: se o socket não estiver ativo ou não puder conectar.
        RuntimeError: se o servidor retornar payload de erro.
    """
    if not os.path.exists(socket_path):
        raise ConnectionError(
            f"🚨 Servidor IPC Open Jev não está ativo em {socket_path}. "
            "Certifique-se de que o MCP server está em execução."
        )

    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        sock.connect(socket_path)
        req_msg = json.dumps({"action": action, "payload": payload or {}}, ensure_ascii=False) + "\n"
        sock.sendall(req_msg.encode("utf-8"))

        with sock.makefile("r", encoding="utf-8", newline="\n") as reader:
            line = reader.readline()
            if not line:
                raise RuntimeError("Servidor IPC retornou resposta vazia.")
            res = json.loads(line)

        if res.get("status") == "error":
            raise RuntimeError(f"Erro no IPC Open Jev: {res.get('error')}")
        return res.get("data", res)


def request_verify_delivery(
    workspace: str,
    attempt_number: int = 1,
    task_type: str = "codigo_producao",
    pedido: str = "",
    resultado: str = "",
    timeout: float = 180.0,
    socket_path: str = DEFAULT_SOCKET_PATH,
) -> Dict[str, Any]:
    """Invoca o pipeline determinístico needle_verify_delivery via socket IPC.

    Executa Pytest em chunks sequenciais + Needle 3 AST Guard/Screener.
    `attempt_number` é apenas informativo: a contagem de tentativas é
    resolvida server-side pelo circuit breaker.

    Exemplo:
        >>> try:
        ...     request_verify_delivery(".", socket_path="/nonexistent/openjev.sock")
        ... except ConnectionError as exc:
        ...     "Servidor IPC Open Jev não está ativo" in str(exc)
        True
    """
    target_ws = str(Path(workspace).resolve())
    payload = {
        "workspace": target_ws,
        "attempt_number": attempt_number,
        "task_type": task_type,
        "pedido": pedido,
        "resultado": resultado,
    }
    return call_jev_ipc("verify_delivery", payload=payload, timeout=timeout, socket_path=socket_path)
