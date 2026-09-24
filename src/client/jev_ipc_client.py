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
    timeout: float = 60.0,
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


def request_score_and_diagnose(
    premise: str,
    workspace: str,
    threshold: float = 3.40,
    task_type: str = "entrega",
    timeout: float = 60.0,
    socket_path: str = DEFAULT_SOCKET_PATH,
) -> Dict[str, Any]:
    """Invoca formalmente o pipeline openjev_score_and_diagnose via socket IPC.

    Executa multi-etapas via diffs reais, anti-premise gaming, persistência de tentativas
    e diagnóstico em 10 categorias flat contra o modelo Laya na GPU.
    """
    target_ws = str(Path(workspace).resolve())
    payload = {
        "premise": premise,
        "workspace": target_ws,
        "threshold": threshold,
        "task_type": task_type,
    }
    try:
        return call_jev_ipc("score_and_diagnose", payload=payload, timeout=timeout, socket_path=socket_path)
    except RuntimeError as e:
        if "Ação desconhecida: score_and_diagnose" in str(e):
            import asyncio
            from src.mcp_server import openjev_score_and_diagnose
            return asyncio.run(openjev_score_and_diagnose(**payload))
        raise
