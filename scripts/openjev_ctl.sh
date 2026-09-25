#!/usr/bin/env bash
# Utilitário de Gerenciamento do Daemon Permanente Open JEV
# Suporta: start, stop, restart, status, ping, health, logs

set -euo pipefail

SERVICE_NAME="openjev.service"
SOCKET_PATH="/tmp/openjev_ipc.sock"
REPO_DIR="/home/maiteuxzanela/projeto JEV"
VENV_PYTHON="$REPO_DIR/.venv/bin/python"

case "${1:-status}" in
    start)
        echo "Iniciando $SERVICE_NAME..."
        systemctl --user start "$SERVICE_NAME"
        sleep 1
        systemctl --user status "$SERVICE_NAME" --no-pager
        ;;
    stop)
        echo "Parando $SERVICE_NAME..."
        systemctl --user stop "$SERVICE_NAME"
        echo "Serviço parado."
        ;;
    restart)
        echo "Reiniciando $SERVICE_NAME..."
        systemctl --user restart "$SERVICE_NAME"
        sleep 1
        systemctl --user status "$SERVICE_NAME" --no-pager
        ;;
    status)
        systemctl --user status "$SERVICE_NAME" --no-pager || true
        ;;
    logs)
        journalctl --user -u "$SERVICE_NAME" -n "${2:-50}" -f
        ;;
    ping)
        if [[ ! -S "$SOCKET_PATH" ]]; then
            echo "❌ Socket IPC $SOCKET_PATH inexistente ou não é um socket Unix."
            exit 1
        fi
        "$VENV_PYTHON" -c "
import time
from src.decision.ipc_service import call_ipc_sync, is_ipc_available
t0 = time.perf_counter()
avail = is_ipc_available()
if not avail:
    print('❌ IPC indisponível via ping.')
    exit(1)
res = call_ipc_sync('ping', {})
lat = (time.perf_counter() - t0) * 1000.0
print(f'✅ Ping IPC OK em {lat:.2f} ms | Resposta: {res}')
"
        ;;
    health)
        echo "============================================================"
        echo "           DIAGNÓSTICO DE SAÚDE DO DAEMON OPEN JEV          "
        echo "============================================================"
        echo -n "1. Status do Serviço Systemd: "
        if systemctl --user is-active --quiet "$SERVICE_NAME"; then
            echo "🟢 ATIVO (running)"
        else
            echo "🔴 INATIVO / FALHA"
        fi

        echo -n "2. Arquivo Socket ($SOCKET_PATH): "
        if [[ -S "$SOCKET_PATH" ]]; then
            echo "🟢 PRESENTE"
        else
            echo "🔴 AUSENTE"
        fi

        echo -n "3. Conectividade IPC (Ping): "
        if [[ -S "$SOCKET_PATH" ]]; then
            "$VENV_PYTHON" -c "
import time
from src.decision.ipc_service import call_ipc_sync
try:
    t0 = time.perf_counter()
    res = call_ipc_sync('ping', {}, timeout=2.0)
    lat = (time.perf_counter() - t0) * 1000.0
    print(f'🟢 OK ({lat:.2f} ms | Device: {res.get(\"device\", \"unknown\")})')
except Exception as e:
    print(f'🔴 ERRO: {e}')
"
        else
            echo "🔴 PING IGNORADO (Socket ausente)"
        fi

        echo "4. Alocação de VRAM na GPU GTX 1050 Ti:"
        if command -v nvidia-smi &>/dev/null; then
            nvidia-smi --query-gpu=name,memory.used,memory.free --format=csv,noheader || true
        else
            echo "   nvidia-smi não encontrado."
        fi
        echo "============================================================"
        ;;
    *)
        echo "Uso: $0 {start|stop|restart|status|ping|health|logs [n_linhas]}"
        exit 1
        ;;
esac
