"""Gate de entrega de score: premissa, catálogo de rubricas, histórico anti-gaming.

Compartilhado por `src/mcp_server.py` (caminho canônico MCP) e
`src/decision/server.py` (FastAPI), para o endpoint HTTP não fugir das
garantias documentadas (Pedido|Resultado, teto 1500, rubrica fechada,
circuit breaker com assinatura de workspace).
"""

from __future__ import annotations

import hashlib
import subprocess
import threading
from pathlib import Path
from typing import Dict, Optional

from .schemas import RUBRIC_CATALOG

SCORE_PREMISE_MAX_LEN = 1500
MAX_ATTEMPTS = 5

_score_history: Dict[str, dict] = {}
_stages_cache: Dict[str, Dict[str, dict]] = {}
_score_history_lock = threading.Lock()


def history_get(workspace: str) -> Optional[dict]:
    with _score_history_lock:
        hist = _score_history.get(workspace)
        return dict(hist) if hist else None


def history_set(workspace: str, entry: dict) -> None:
    with _score_history_lock:
        _score_history[workspace] = entry


def history_increment_attempts(workspace: str) -> int:
    with _score_history_lock:
        hist = _score_history.get(workspace)
        if not hist:
            return 0
        hist["attempts"] = int(hist.get("attempts", 0)) + 1
        return hist["attempts"]


def history_clear() -> None:
    with _score_history_lock:
        _score_history.clear()
        _stages_cache.clear()


def stages_cache_get(workspace: str, stage_id: str) -> Optional[dict]:
    """Retorna dados de uma etapa já aprovada em ciclo anterior se presente."""
    with _score_history_lock:
        ws_cache = _stages_cache.get(workspace)
        if not ws_cache:
            return None
        entry = ws_cache.get(stage_id)
        return dict(entry) if entry else None


def stages_cache_set(workspace: str, stage_id: str, entry: dict) -> None:
    """Registra uma etapa aprovada no cache do workspace."""
    with _score_history_lock:
        if workspace not in _stages_cache:
            _stages_cache[workspace] = {}
        _stages_cache[workspace][stage_id] = entry


def stages_cache_clear(workspace: str) -> None:
    """Limpa o cache de etapas de um workspace específico."""
    with _score_history_lock:
        _stages_cache.pop(workspace, None)



def validate_score_premise(premise: str) -> Optional[str]:
    if not premise or not premise.strip():
        return (
            "🚨 PREMISSA RECUSADA: premissa vazia. Informe o Pedido e o Resultado "
            "Executado no padrão factual."
        )
    if len(premise) > SCORE_PREMISE_MAX_LEN:
        return (
            f"🚨 PREMISSA RECUSADA: {len(premise)} caracteres excedem o teto de "
            f"{SCORE_PREMISE_MAX_LEN}. Encurte a premissa: dados extensos (diffs, logs, "
            "relatórios) ficam no repositório, não na prosa da avaliação."
        )
    low = premise.lower()
    if "pedido" not in low or "resultado" not in low:
        return (
            "🚨 PREMISSA RECUSADA: use o padrão factual obrigatório — "
            "'Pedido do Prompt: <requisitos> | Resultado Executado: <arquivos, testes, status>'."
        )
    return None


def resolve_task_rubric(task_type: str) -> tuple:
    key = (task_type or "entrega").strip().lower()
    if key not in RUBRIC_CATALOG:
        options = ", ".join(sorted(RUBRIC_CATALOG))
        return None, (
            f"🚨 TASK_TYPE INVÁLIDO: '{task_type}'. Use exclusivamente o catálogo "
            f"fechado: {options}. Rubrica livre é rubric gaming e é ignorada."
        )
    return RUBRIC_CATALOG[key], None


def get_workspace_git_signature(workspace_dir: str) -> str:
    """Assinatura do workspace baseada em CONTEÚDO real (HEAD + diff + untracked não-vazio)."""
    try:
        root = Path(workspace_dir)
        if not root.is_dir():
            return ""

        res_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        head = res_head.stdout.strip() if res_head.returncode == 0 else "no_head"

        if head == "no_head":
            skip_dirs = {
                ".git",
                "__pycache__",
                ".venv",
                "venv",
                "node_modules",
                ".pytest_cache",
                ".mypy_cache",
                ".ruff_cache",
            }
            h = hashlib.sha256()
            h.update(b"no_head")
            files = []
            for p in root.rglob("*"):
                if not p.is_file():
                    continue
                rel_parts = p.relative_to(root).parts
                if any(part in skip_dirs for part in rel_parts):
                    continue
                files.append(p)
            for p in sorted(files, key=lambda x: x.relative_to(root).as_posix()):
                try:
                    data = p.read_bytes()
                except Exception:
                    continue
                if not data:
                    continue
                rel = p.relative_to(root).as_posix()
                h.update(rel.encode("utf-8", errors="replace"))
                h.update(data)
            return f"no_head:{h.hexdigest()}"

        res_diff = subprocess.run(
            ["git", "diff", "HEAD"],
            cwd=workspace_dir,
            capture_output=True,
            timeout=5,
        )
        diff_bytes = res_diff.stdout if res_diff.returncode == 0 else b""

        res_untracked = subprocess.run(
            ["git", "ls-files", "--others", "--exclude-standard"],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        untracked = (
            [ln for ln in res_untracked.stdout.splitlines() if ln.strip()]
            if res_untracked.returncode == 0
            else []
        )

        h = hashlib.sha256()
        h.update(head.encode("utf-8"))
        h.update(diff_bytes)

        for rel in sorted(untracked):
            try:
                data = (root / rel).read_bytes()
            except Exception:
                continue
            if not data:
                continue
            h.update(rel.encode("utf-8", errors="replace"))
            h.update(data)

        return f"{head}:{h.hexdigest()}"
    except Exception:
        pass
    return ""
