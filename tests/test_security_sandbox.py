"""TICKET-SGA-03 — Sandbox de workspace/scope, resiliência de lanes e blindagem do wrapper.

Validação 100% determinística SEM MOCKS: filesystem temporário real, subprocessos
reais (binário de ripgrep inexistente, script lento, wrappers Node.js) e os motores
reais ``LocalNeedleEngine`` / ``QmdHybridSearchEngine`` / tools MCP.

Cobertura:
1. Sandbox ``CIRURGIC_ALLOWED_ROOTS``: ``..``, symlink e caminhos absolutos fora
   da allowlist viram ``SandboxViolationError`` explícita (nunca varredura silenciosa).
2. Observabilidade de timeouts/erros léxicos: ``status='partial'``, ``warnings`` e
   ``degraded_lanes=['ripgrep_lexical']``.
3. Token budget insuficiente: warning explícito no payload (QMD e Needle).
4. Blindagem do wrapper ``bin/agent-quality-guard.js``: versão do Python >= 3.10,
   módulos ``mcp``/``pydantic`` e diagnóstico amigável em cada falha pré-spawn.
"""

import asyncio
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from agent_quality_guard.core.schemas import (
    CirurgicSearchResponse,
    Needle3Query,
    Needle3Response,
)
from agent_quality_guard.memory.local_needle import (
    ALLOWED_ROOTS_ENV,
    LocalNeedleEngine,
    SandboxViolationError,
    _configured_allowed_roots,
    ensure_within_allowed_roots,
    resolve_allowed_roots,
)
from agent_quality_guard.memory.qmd_search import (
    RIPGREP_TIMEOUT_SECONDS,
    QmdHybridSearchEngine,
)
from agent_quality_guard.mcp_server import cirurgic_search

ROOT = Path(__file__).resolve().parents[1]
NODE_WRAPPER = ROOT / "bin" / "agent-quality-guard.js"
NODE_BIN = shutil.which("node")

_MODULE_CODE = (
    "import os\n\n"
    "def calculate_total_billing(patient_id: str, days: int) -> float:\n"
    "    # RN_FAT_009: regra de valoracao de diarias de UTI\n"
    "    daily_rate = 1500.0\n"
    "    multiplier = 1.0 if days <= 2 else 0.5\n"
    "    return days * daily_rate * multiplier\n\n"
    "def validate_eligibility(card_number: str) -> bool:\n"
    "    return len(card_number) == 16\n"
)


@pytest.fixture
def sandbox_layout(tmp_path):
    """Layout real: workspace interno + diretório externo saindo da sandbox."""
    ws = tmp_path / "ws"
    outside = tmp_path / "outside"
    ws.mkdir()
    outside.mkdir()
    (ws / "billing_service.py").write_text(_MODULE_CODE, encoding="utf-8")
    (outside / "secret.py").write_text(
        "TOP_SECRET_MARKER = 'fora-da-sandbox'\n", encoding="utf-8"
    )
    return ws, outside


# ---------------------------------------------------------------------------
# 1. Sandbox de workspace/scope (allowlist + path traversal)
# ---------------------------------------------------------------------------


def test_dotdot_traversal_is_rejected(sandbox_layout):
    """``..`` saindo da raiz autorizada vira SandboxViolationError com diagnóstico."""
    ws, outside = sandbox_layout
    sneaky = ws / ".." / "outside"
    with pytest.raises(SandboxViolationError) as excinfo:
        ensure_within_allowed_roots(sneaky, [ws.resolve()], context="scope 'demo'")
    err = excinfo.value
    assert isinstance(err, ValueError)  # retrocompat com quem captura ValueError
    assert err.path == outside.resolve()
    assert err.allowed_roots == [ws.resolve()]
    assert "Sandbox violation" in str(err)
    assert ALLOWED_ROOTS_ENV in str(err)


def test_absolute_path_outside_allowlist_is_rejected(sandbox_layout):
    """Caminho absoluto existente fora da allowlist é violação, não resultado."""
    ws, outside = sandbox_layout
    with pytest.raises(SandboxViolationError):
        ensure_within_allowed_roots(outside / "secret.py", [ws.resolve()], context="scope")


def test_symlink_escape_is_rejected(sandbox_layout):
    """Symlink interno apontando para fora da sandbox é resolvido e rejeitado."""
    ws, outside = sandbox_layout
    link = ws / "link_out"
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(SandboxViolationError):
        ensure_within_allowed_roots(link, [ws.resolve()], context="scope 'link_out'")


def test_path_inside_allowlist_is_returned_resolved(sandbox_layout):
    """Conteúdo legítimo continua acessível (caminho canônico devolvido)."""
    ws, _outside = sandbox_layout
    resolved = ensure_within_allowed_roots(
        ws / "billing_service.py", [ws.resolve()], context="scope"
    )
    assert resolved == (ws / "billing_service.py").resolve()


def test_empty_allowlist_blocks_everything(sandbox_layout):
    """Allowlist vazia bloqueia até o próprio workspace (prova de sensibilidade)."""
    ws, _outside = sandbox_layout
    with pytest.raises(SandboxViolationError):
        ensure_within_allowed_roots(ws, [], context="scope")


def test_allowed_roots_env_parsing_separators_and_relative(
    sandbox_layout, monkeypatch
):
    """``CIRURGIC_ALLOWED_ROOTS`` aceita ``:`` e ``,``; relativas são ignoradas."""
    root_a, outside = sandbox_layout
    monkeypatch.setenv(
        ALLOWED_ROOTS_ENV, f"relative/ignored:{root_a}:{outside}"
    )
    roots = _configured_allowed_roots()
    assert roots == [root_a.resolve(), outside.resolve()]

    monkeypatch.setenv(ALLOWED_ROOTS_ENV, f"relative/one,relative/two")
    assert _configured_allowed_roots() == []

    monkeypatch.delenv(ALLOWED_ROOTS_ENV, raising=False)
    assert _configured_allowed_roots() == []


def test_resolve_allowed_roots_fallback_to_workspace(sandbox_layout, monkeypatch):
    """Sem env, a raiz do workspace é a autoridade (comportamento legado)."""
    ws, _outside = sandbox_layout
    monkeypatch.delenv(ALLOWED_ROOTS_ENV, raising=False)
    assert resolve_allowed_roots(ws) == [ws.resolve()]


def test_engine_construction_rejects_outside_workspace(sandbox_layout, monkeypatch):
    """Workspace fora da allowlist falha na construção de ambos os motores."""
    ws, outside = sandbox_layout
    monkeypatch.setenv(ALLOWED_ROOTS_ENV, str(ws))
    with pytest.raises(SandboxViolationError):
        QmdHybridSearchEngine(workspace_root=str(outside))
    with pytest.raises(SandboxViolationError):
        LocalNeedleEngine(workspace_root=str(outside))
    # E o workspace autorizado constrói normalmente com allowed_roots resolvidas.
    engine = QmdHybridSearchEngine(workspace_root=str(ws))
    assert engine.workspace_root == ws.resolve()
    assert engine.allowed_roots == [ws.resolve()]


async def test_search_scope_dotdot_traversal_raises(sandbox_layout):
    """``search(scope=['../outside'])`` propaga SandboxViolationError (sem varrer)."""
    ws, _outside = sandbox_layout
    engine = QmdHybridSearchEngine(workspace_root=str(ws))
    with pytest.raises(SandboxViolationError):
        await engine.search(query="TOP_SECRET_MARKER", scope=["../outside"])


async def test_search_scope_absolute_outside_raises(sandbox_layout):
    """Scope absoluto fora da sandbox é violação mesmo apontando para arquivo real."""
    ws, outside = sandbox_layout
    engine = QmdHybridSearchEngine(workspace_root=str(ws))
    with pytest.raises(SandboxViolationError):
        await engine.search(query="TOP_SECRET_MARKER", scope=[str(outside)])


async def test_mcp_tool_maps_sandbox_violation_to_structured_error(
    sandbox_layout, monkeypatch
):
    """Ferramenta MCP converte violação em dict estruturado (nunca derruba stdio)."""
    ws, outside = sandbox_layout
    monkeypatch.setenv(ALLOWED_ROOTS_ENV, str(ws))
    res = await cirurgic_search(query="qualquer", workspace=str(outside))
    assert res["status"] == "error"
    assert res["error"] == "sandbox_violation"
    assert res["degraded_lanes"] == ["sandbox"]
    assert res["warnings"] and "Sandbox rejeitou" in res["warnings"][0]
    assert res["snippets_count"] == 0
    # Contrato aditivo validável pelo schema de referência.
    CirurgicSearchResponse.model_validate(res)


async def test_mcp_tool_maps_scope_violation_to_structured_error(sandbox_layout):
    """Violação vinda do scope (durante a busca) também vira erro estruturado."""
    ws, _outside = sandbox_layout
    res = await cirurgic_search(
        query="qualquer", workspace=str(ws), scope="../outside"
    )
    assert res["status"] == "error"
    assert res["error"] == "sandbox_violation"
    assert res["degraded_lanes"] == ["sandbox"]


# ---------------------------------------------------------------------------
# 2. Observabilidade de timeouts e erros léxicos (status partial + warnings)
# ---------------------------------------------------------------------------


async def test_missing_ripgrep_binary_degrades_lexical_lane(sandbox_layout):
    """Binário de rg inexistente => OSError real => partial + degraded_lanes."""
    ws, _outside = sandbox_layout
    engine = QmdHybridSearchEngine(workspace_root=str(ws))
    engine.rg_binary = "/nonexistent/rg-sga03-binary"  # caminho real inexistente
    res = await engine.search(query="calculate_total_billing", max_tokens=800)

    assert res["status"] == "partial"
    assert "ripgrep_lexical" in res["degraded_lanes"]
    assert any("lexical lane I/O failure" in w for w in res["warnings"])
    # A lane AST continua útil: sucesso parcial, não falha total.
    assert res["files_found"] >= 1
    assert res["snippets_count"] >= 1


async def test_ripgrep_timeout_degrades_lexical_lane(sandbox_layout, tmp_path):
    """Timeout real do subprocesso => warning literal + partial (nunca sucesso vazio)."""
    ws, _outside = sandbox_layout
    slow_rg = tmp_path / "slow_rg.sh"
    slow_rg.write_text("#!/bin/sh\nexec sleep 6\n", encoding="utf-8")
    slow_rg.chmod(slow_rg.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    engine = QmdHybridSearchEngine(workspace_root=str(ws))
    engine.rg_binary = str(slow_rg)
    res = await engine.search(query="calculate_total_billing", max_tokens=800)

    assert res["status"] == "partial"
    assert "ripgrep_lexical" in res["degraded_lanes"]
    expected = f"ripgrep timed out after {RIPGREP_TIMEOUT_SECONDS}s"
    assert expected in res["warnings"]


async def test_search_success_contract_control(sandbox_layout):
    """Controle: busca saudável emite status success e listas de diagnóstico vazias."""
    ws, _outside = sandbox_layout
    engine = QmdHybridSearchEngine(workspace_root=str(ws))
    res = await engine.search(query="calculate_total_billing", max_tokens=800)
    assert res["status"] == "success"
    assert res["degraded_lanes"] == []
    assert res["warnings"] == []
    assert res["snippets_count"] >= 1
    CirurgicSearchResponse.model_validate(res)


# ---------------------------------------------------------------------------
# 3. Token budget insuficiente => warning explícito no payload
# ---------------------------------------------------------------------------


async def test_qmd_token_budget_warning_in_payload(sandbox_layout):
    """max_tokens minúsculo gera warning explícito de orçamento no dict de saída."""
    ws, _outside = sandbox_layout
    engine = QmdHybridSearchEngine(workspace_root=str(ws))
    res = await engine.search(query="calculate_total_billing", max_tokens=5)
    assert any(
        w.startswith("Token budget max_tokens=") and "was too small" in w
        for w in res["warnings"]
    ), f"esperava warning de token budget, veio: {res['warnings']!r}"


async def test_needle_token_budget_warning_on_schema_fields(sandbox_layout):
    """Needle emite o alerta em response.warnings/last_warnings (campos do Túlio)."""
    ws, _outside = sandbox_layout
    engine = LocalNeedleEngine(workspace_root=str(ws))
    query = Needle3Query(query="calculate_total_billing", max_tokens=5)
    response, warnings, degraded = await engine.search_with_diagnostics(query)

    assert isinstance(response, Needle3Response)
    assert response.warnings == warnings == engine.last_warnings
    assert response.degraded_lanes == degraded == engine.last_degraded_lanes == []
    assert any(
        w.startswith("Token budget max_tokens=5") and "was too small" in w
        for w in warnings
    ), f"esperava warning de token budget max_tokens=5, veio: {warnings!r}"
    assert response.snippets == []
    Needle3Response.model_validate(response)


# ---------------------------------------------------------------------------
# 4. Blindagem e diagnóstico do interpretador no wrapper JS
# ---------------------------------------------------------------------------


def _write_fake_python(tmp_path: Path, name: str, body: str) -> Path:
    script = tmp_path / name
    script.write_text(f"#!/bin/sh\n{body}", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return script


def _run_wrapper(python_bin: Path) -> subprocess.CompletedProcess:
    assert NODE_BIN is not None, "nodejs indisponível no host"
    env = {**os.environ, "PYTHON": str(python_bin)}
    return subprocess.run(
        [NODE_BIN, str(NODE_WRAPPER)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=env,
        cwd=str(ROOT),
        timeout=60,
    )


@pytest.mark.skipif(NODE_BIN is None, reason="nodejs indisponível no host")
def test_wrapper_rejects_old_python_with_diagnostic(tmp_path):
    """Python 3.9 real (script executável) => falha amigável com a versão citada.

    O fake-python reage ao SNIPPET exato ``sys.version_info >= (3, 10)`` que o
    wrapper spawna: se o gate deixar de exigir (3, 10), o script deixa de reprovar
    e o teste falha (prova de sensibilidade à mutação do limiar).
    """
    fake_py = _write_fake_python(
        tmp_path,
        "py39.sh",
        'if [ "$1" = "--version" ]; then echo "Python 3.9.18"; exit 0; fi\n'
        'case "$1" in *mcp_server.py) exit 1 ;; esac\n'
        'if echo "$2" | grep -q \'(3, 10)\'; then exit 1; fi\n'
        "exit 0\n",
    )
    proc = _run_wrapper(fake_py)
    assert proc.returncode == 1
    assert "Python >= 3.10 é obrigatório" in proc.stderr
    assert "Python 3.9.18" in proc.stderr


@pytest.mark.skipif(NODE_BIN is None, reason="nodejs indisponível no host")
def test_wrapper_rejects_missing_modules_with_diagnostic(tmp_path):
    """Versão apta mas sem mcp/pydantic => diagnóstico de dependências ausentes."""
    fake_py = _write_fake_python(
        tmp_path,
        "py_nomod.sh",
        'case "$1" in *mcp_server.py) exit 1 ;; esac\n'
        'case "$2" in *find_spec*) exit 1 ;; *) exit 0 ;; esac\n',
    )
    proc = _run_wrapper(fake_py)
    assert proc.returncode == 1
    assert "Dependências obrigatórias (mcp, pydantic) ausentes" in proc.stderr
    assert "pip install" in proc.stderr


@pytest.mark.skipif(NODE_BIN is None, reason="nodejs indisponível no host")
def test_wrapper_rejects_unexecutable_python(tmp_path):
    """Arquivo existe mas sem bit de execução => EACCES com mensagem amigável."""
    fake_py = tmp_path / "not_executable.py"
    fake_py.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_py.chmod(0o644)
    proc = _run_wrapper(fake_py)
    assert proc.returncode == 1
    assert "Não foi possível executar o Python" in proc.stderr


def test_host_interpreter_satisfies_wrapper_requirements():
    """Controle: os predicados do preflight valem no interpretador desta suíte."""
    assert sys.version_info >= (3, 10)
    import importlib.util

    assert importlib.util.find_spec("mcp") is not None
    assert importlib.util.find_spec("pydantic") is not None


# ---------------------------------------------------------------------------
# Sanidade do caminho feliz síncrono (sem depender do spawn do servidor MCP)
# ---------------------------------------------------------------------------


def test_search_is_deterministic_across_runs(sandbox_layout):
    """Duas execuções reais no mesmo workspace devolvem o mesmo conjunto de arquivos."""
    ws, _outside = sandbox_layout
    engine = QmdHybridSearchEngine(workspace_root=str(ws))
    first = asyncio.run(engine.search(query="calculate_total_billing", max_tokens=800))
    second = asyncio.run(engine.search(query="calculate_total_billing", max_tokens=800))
    assert first["status"] == second["status"] == "success"
    assert first["snippets_count"] == second["snippets_count"]
    assert {s["file"] for s in first["snippets"]} == {s["file"] for s in second["snippets"]}
