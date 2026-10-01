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

import json
import logging
import os
import tempfile

logger = logging.getLogger("jev.score_gate")

SCORE_PREMISE_MAX_LEN = 1500
MAX_ATTEMPTS = 5

# Banda de indiferença (indifference zone) da nota de corte — decisão da
# fundadora (set/2026), lastreada na literatura de cut-scores de mastery
# testing (Fhanér 1974; Wilcox 1976; van der Linden 1980): scores dentro de
# CAVEAT_BAND abaixo do corte são tratados como zona de fronteira — aprovados
# APENAS com ressalva quando o Screener não aponta nenhuma suspeita; abaixo da
# banda o corte é estrito (passed=false mesmo com zero suspeitas).
CAVEAT_BAND = 0.20


def caveat_decision(score: float, threshold: float) -> str:
    """Decisão pura da banda de indiferença: ``"pass"`` | ``"caveat"`` | ``"fail"``.

    - ``score >= threshold``                       → "pass"
    - ``threshold - CAVEAT_BAND <= score < threshold`` → "caveat" (ressalva)
    - ``score < threshold - CAVEAT_BAND``           → "fail" (corte estrito)

    Função pura, sem I/O. A banda é o único critério da ressalva de nota; a
    presença/ausência de suspeitas do Screener é arbitrária pelo chamador.

    Exemplo:
        >>> caveat_decision(3.45, 3.40)
        'pass'
        >>> caveat_decision(3.30, 3.40)
        'caveat'
    """
    if score >= threshold:
        return "pass"
    if score >= threshold - CAVEAT_BAND:
        return "caveat"
    return "fail"

_score_history: Dict[str, dict] = {}
_stages_cache: Dict[str, Dict[str, dict]] = {}
_score_history_lock = threading.Lock()


def _state_base_dir() -> Path:
    """Diretório base do estado persistido.

    `JEV_STATE_DIR` (lido a cada chamada) permite isolar o estado por
    execução de teste — sem ele, `history_clear()` de uma suíte apagava o
    circuit breaker e o registro de homologação de TODOS os workspaces.
    """
    override = os.environ.get("JEV_STATE_DIR", "").strip()
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "openjev_state"


def _get_state_path(workspace: str) -> Path:
    """Arquivo JSON de estado do workspace, sob `JEV_STATE_DIR` (ou o tmp padrão)."""
    target = str(Path(workspace).resolve()) if workspace else "default"
    ws_hash = hashlib.sha256(target.encode("utf-8")).hexdigest()[:16]
    base_dir = _state_base_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / f"score_state_{ws_hash}.json"


def _read_persisted_state(workspace: str) -> dict:
    state_file = _get_state_path(workspace)
    if not state_file.is_file():
        return {}
    try:
        with open(state_file, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.debug("Falha ao ler estado persistido de %s: %s", workspace, e)
        return {}


def _write_persisted_state(workspace: str, state: dict) -> None:
    state_file = _get_state_path(workspace)
    tmp_file = state_file.with_suffix(".tmp")
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp_file, state_file)
    except Exception as e:
        logger.debug("Falha ao gravar estado persistido em %s: %s", state_file, e)
        if tmp_file.exists():
            try:
                tmp_file.unlink()
            except OSError as unlink_err:
                logger.debug("Falha ao remover arquivo temporario %s: %s", tmp_file, unlink_err)


def history_get(workspace: str) -> Optional[dict]:
    with _score_history_lock:
        hist = _score_history.get(workspace)
        if hist is None:
            state = _read_persisted_state(workspace)
            hist = state.get("history")
            if hist:
                _score_history[workspace] = dict(hist)
        return dict(hist) if hist else None


def history_set(workspace: str, entry: dict) -> None:
    with _score_history_lock:
        _score_history[workspace] = dict(entry)
        state = _read_persisted_state(workspace)
        state["history"] = dict(entry)
        _write_persisted_state(workspace, state)


def history_increment_attempts(workspace: str) -> int:
    with _score_history_lock:
        hist = _score_history.get(workspace)
        if hist is None:
            state = _read_persisted_state(workspace)
            hist = state.get("history")
            if hist:
                _score_history[workspace] = dict(hist)
        if not hist:
            return 0
        hist["attempts"] = int(hist.get("attempts", 0)) + 1
        state = _read_persisted_state(workspace)
        state["history"] = dict(hist)
        _write_persisted_state(workspace, state)
        return hist["attempts"]


def resolve_attempt_number(hist: Optional[dict]) -> int:
    """Próxima tentativa derivada EXCLUSIVAMENTE do histórico persistido.

    O valor enviado pelo chamador nunca participa do cálculo: aceitá-lo como
    piso liberava a Saída de Emergência na 1ª chamada (`attempt_number=5`)
    sem executar pytest, Guard nem Screener.

    - última tentativa aprovada → mantém a contagem (revalidação não consome tentativa);
    - última tentativa reprovada ou inexistente → avança +1.

    Exemplo:
        >>> resolve_attempt_number(None)
        1
        >>> resolve_attempt_number({"passed": False, "attempts": 4})
        5
        >>> resolve_attempt_number({"passed": True, "attempts": 2})
        2
    """
    prev = int((hist or {}).get("attempts", 0))
    if hist and hist.get("passed", False):
        return max(prev, 1)
    return prev + 1


def history_clear(workspace: Optional[str] = None) -> None:
    """Zera o histórico de tentativas.

    Com `workspace`, o reset é **escopado** ao estado daquele workspace
    (memória + arquivo). Sem `workspace`, limpa tudo — reservado a setup de
    teste com `JEV_STATE_DIR` isolado; nunca usado pelo caminho de homologação.

    Exemplo:
        >>> history_clear("/workspace/que_nao_existe") is None
        True
    """
    with _score_history_lock:
        if workspace is not None:
            _score_history.pop(workspace, None)
            _stages_cache.pop(workspace, None)
            state_file = _get_state_path(workspace)
            if state_file.is_file():
                try:
                    state_file.unlink()
                except OSError as unlink_err:
                    logger.debug("Falha ao remover arquivo de estado %s: %s", state_file, unlink_err)
            return
        _score_history.clear()
        _stages_cache.clear()
        base_dir = _state_base_dir()
        if base_dir.is_dir():
            for f in base_dir.glob("score_state_*.json"):
                try:
                    f.unlink()
                except OSError as unlink_err:
                    logger.debug("Falha ao remover arquivo de estado %s: %s", f, unlink_err)


def stages_cache_get(workspace: str, stage_id: str) -> Optional[dict]:
    """Retorna dados de uma etapa já aprovada em ciclo anterior se presente."""
    with _score_history_lock:
        ws_cache = _stages_cache.get(workspace)
        if ws_cache is None:
            state = _read_persisted_state(workspace)
            ws_cache = state.get("stages", {})
            _stages_cache[workspace] = dict(ws_cache)
        entry = ws_cache.get(stage_id)
        return dict(entry) if entry else None


def stages_cache_set(workspace: str, stage_id: str, entry: dict) -> None:
    """Registra uma etapa aprovada no cache do workspace."""
    with _score_history_lock:
        if workspace not in _stages_cache:
            state = _read_persisted_state(workspace)
            _stages_cache[workspace] = dict(state.get("stages", {}))
        _stages_cache[workspace][stage_id] = dict(entry)
        state = _read_persisted_state(workspace)
        if "stages" not in state:
            state["stages"] = {}
        state["stages"][stage_id] = dict(entry)
        _write_persisted_state(workspace, state)


def stages_cache_clear(workspace: str) -> None:
    """Limpa o cache de etapas de um workspace específico."""
    with _score_history_lock:
        _stages_cache.pop(workspace, None)
        state = _read_persisted_state(workspace)
        if "stages" in state:
            state["stages"] = {}
            _write_persisted_state(workspace, state)



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
    key = (task_type or "codigo_producao").strip().lower()
    # Mapeia chaves legadas transparentemente para codigo_producao
    if key in ("entrega", "auditoria"):
        key = "codigo_producao"
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
    except Exception as e:
        logger.debug("Falha ao calcular assinatura Git do workspace %s: %s", workspace_dir, e)
        return ""
