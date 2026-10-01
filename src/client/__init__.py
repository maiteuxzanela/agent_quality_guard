"""Cliente IPC Ultraleve do Ecossistema Open JEV.

Fornece interface de comunicação cliente-servidor de alta performance (<10ms)
via Unix Domain Socket (/tmp/openjev_ipc.sock) sem dependências externas de ML
(stdlib pura: socket, json, os, sys).
"""

from .jev_ipc_client import (
    DEFAULT_SOCKET_PATH,
    call_jev_ipc,
    is_ipc_available,
    request_verify_delivery,
)

__all__ = [
    "DEFAULT_SOCKET_PATH",
    "call_jev_ipc",
    "is_ipc_available",
    "request_verify_delivery",
]
