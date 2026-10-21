"""TICKET-SGA-06 — Sandbox inteligente (denylist de SO) e resiliência de ambiente.

Validação 100% determinística, SEM MOCKS: motores reais
(``LocalNeedleEngine`` / ``QmdHybridSearchEngine``), filesystem temporário real
e ``importlib.util.find_spec`` real.

Cobertura:
1. Denylist rígida de sistema operacional (``SYSTEM_RESTRICTED_ROOTS``): raiz,
   descendente e ancestral de diretório crítico são bloqueados com a mensagem
   ``"Acesso negado: diretório de sistema restrito"``.
2. Fluxo padrão sem atrito: qualquer workspace de projeto/worktree é aceito sem
   cadastro em allowlist.
3. Modo estrito opt-in (``CIRURGIC_SANDBOX_STRICT=1``) e allowlist explícita
   continuam funcionando para ambientes corporativos.
4. Diagnóstico amigável de dependências ausentes no ``conftest.py``.
"""

import asyncio
from pathlib import Path

import pytest

from agent_quality_guard.memory.local_needle import (
    ALLOWED_ROOTS_ENV,
    STRICT_SANDBOX_ENV,
    SYSTEM_RESTRICTED_MESSAGE,
    SYSTEM_RESTRICTED_ROOTS,
    LocalNeedleEngine,
    SandboxViolationError,
    ensure_not_system_restricted,
    ensure_within_allowed_roots,
)
from agent_quality_guard.memory.qmd_search import QmdHybridSearchEngine

SYSTEM_PATHS = [
    "/",
    "/etc",
    "/sys",
    "/proc",
    "/dev",
    "/boot",
    "/root",
    "/bin",
    "/sbin",
    "/lib",
    "/usr/bin",
    "/usr/sbin",
]


@pytest.fixture(autouse=True)
def _clear_sandbox_env(monkeypatch):
    """Nenhum teste herda allowlist/estrito do ambiente externo."""
    monkeypatch.delenv(ALLOWED_ROOTS_ENV, raising=False)
    monkeypatch.delenv(STRICT_SANDBOX_ENV, raising=False)


@pytest.fixture
def project_layout(tmp_path):
    """Workspace real de projeto + worktree simulada no espaço do usuário."""
    project = tmp_path / ".local" / "share" / "opencode" / "worktree" / "abc"
    project.mkdir(parents=True)
    (project / "service.py").write_text(
        "def calculate_total(value: int) -> int:\n    return value * 2\n",
        encoding="utf-8",
    )
    outside = tmp_path / "outside"
    outside.mkdir()
    return project, outside


def test_system_restricted_roots_contract():
    """A denylist contém exatamente o perímetro de SO exigido pelo card."""
    expected = {Path(p) for p in SYSTEM_PATHS}
    assert expected <= set(SYSTEM_RESTRICTED_ROOTS)


def test_system_restricted_roots_include_resolved_aliases():
    """Toda raiz restrita existente também restringe seu alvo canônico resolvido.

    No macOS, ``/etc`` é um symlink para ``/private/etc``: ``Path.resolve()``
    produz o alias canônico, que precisa estar DENTRO da denylist para o
    bloqueio valer tanto pelo nome quanto pelo alvo (mecanismo do TICKET-CI-02).
    """
    for raw in SYSTEM_PATHS:
        candidate = Path(raw)
        if candidate.exists():
            resolved = candidate.resolve()
            assert resolved in SYSTEM_RESTRICTED_ROOTS, (
                f"{resolved} (resolve de {raw}) ausente da denylist: "
                "alias canônico do SO escaparia do bloqueio"
            )


def test_engines_reject_resolved_system_alias():
    """Construir motor com o alias RESOLVIDO de diretório de sistema falha igual.

    Cobre o caso macOS ``/private/etc`` (resolve de ``/etc``): mesmo que o
    chamador passe o caminho canônico em vez do nome legado, a construção do
    ``LocalNeedleEngine``/``QmdHybridSearchEngine`` é barrada.
    """
    resolved_etc = Path("/etc").resolve()
    with pytest.raises(SandboxViolationError) as excinfo:
        LocalNeedleEngine(workspace_root=str(resolved_etc))
    assert str(excinfo.value) == SYSTEM_RESTRICTED_MESSAGE

    with pytest.raises(SandboxViolationError) as qmd_excinfo:
        QmdHybridSearchEngine(workspace_root=str(resolved_etc))
    assert str(qmd_excinfo.value) == SYSTEM_RESTRICTED_MESSAGE


@pytest.mark.parametrize("restricted", SYSTEM_PATHS)
def test_engines_reject_system_workspace(restricted):
    """Workspace em diretório de sistema falha na construção dos dois motores."""
    with pytest.raises(SandboxViolationError) as excinfo:
        LocalNeedleEngine(workspace_root=restricted)
    assert str(excinfo.value) == SYSTEM_RESTRICTED_MESSAGE
    assert isinstance(excinfo.value, ValueError)

    with pytest.raises(SandboxViolationError) as qmd_excinfo:
        QmdHybridSearchEngine(workspace_root=restricted)
    assert str(qmd_excinfo.value) == SYSTEM_RESTRICTED_MESSAGE


@pytest.mark.parametrize(
    "sensitive",
    ["/etc/passwd", "/proc/self/environ", "/root/.bashrc", "/usr/bin/python3"],
)
def test_system_descendants_are_rejected(sensitive):
    """Descendentes de diretórios críticos também são bloqueados."""
    with pytest.raises(SandboxViolationError) as excinfo:
        ensure_not_system_restricted(Path(sensitive), context="scope")
    assert str(excinfo.value) == SYSTEM_RESTRICTED_MESSAGE


@pytest.mark.parametrize("ancestor", ["/usr", "/usr/bin/..", "/etc/.."])
def test_system_ancestors_are_rejected(ancestor):
    """Ancestral que contém um diretório restrito (ex.: /usr) é bloqueado."""
    with pytest.raises(SandboxViolationError) as excinfo:
        ensure_not_system_restricted(Path(ancestor), context="workspace_root")
    assert str(excinfo.value) == SYSTEM_RESTRICTED_MESSAGE


def test_denylist_overrides_permissive_allowlist():
    """Mesmo com ``/`` na allowlist, a denylist de sistema permanece inviolável."""
    with pytest.raises(SandboxViolationError) as excinfo:
        ensure_within_allowed_roots(Path("/etc"), [Path("/")], context="scope")
    assert str(excinfo.value) == SYSTEM_RESTRICTED_MESSAGE


def test_default_flow_accepts_project_worktree(project_layout):
    """Sem allowlist/estrito, worktree do OpenCode é aceita sem cadastro prévio."""
    project, _outside = project_layout
    needle = LocalNeedleEngine(workspace_root=str(project))
    qmd = QmdHybridSearchEngine(workspace_root=str(project))
    assert needle.workspace_root == project.resolve()
    assert qmd.workspace_root == project.resolve()
    assert needle.allowed_roots == [project.resolve()]


def test_strict_mode_restricts_to_current_workspace(project_layout, monkeypatch):
    """``CIRURGIC_SANDBOX_STRICT=1`` exige o perímetro do diretório corrente."""
    project, outside = project_layout
    monkeypatch.chdir(project)
    monkeypatch.setenv(STRICT_SANDBOX_ENV, "1")

    engine = LocalNeedleEngine(workspace_root=str(project))
    assert engine.workspace_root == project.resolve()

    with pytest.raises(SandboxViolationError):
        LocalNeedleEngine(workspace_root=str(outside))


def test_explicit_allowlist_remains_strict(project_layout, monkeypatch):
    """Allowlist explícita continua restringindo workspaces fora dela."""
    project, outside = project_layout
    monkeypatch.setenv(ALLOWED_ROOTS_ENV, str(project))
    assert LocalNeedleEngine(workspace_root=str(project)).workspace_root == project.resolve()
    with pytest.raises(SandboxViolationError):
        LocalNeedleEngine(workspace_root=str(outside))


def test_scope_search_rejects_system_directory(project_layout):
    """``scope`` apontando para /etc é rejeitado antes de qualquer varredura."""
    project, _outside = project_layout
    engine = QmdHybridSearchEngine(workspace_root=str(project))
    with pytest.raises(SandboxViolationError) as excinfo:
        asyncio.run(engine.search(query="calculate_total", scope=["/etc"]))
    assert str(excinfo.value) == SYSTEM_RESTRICTED_MESSAGE


def test_default_flow_search_still_works(project_layout):
    """Controle positivo: busca real no workspace aceito segue funcionando."""
    project, _outside = project_layout
    engine = QmdHybridSearchEngine(workspace_root=str(project))
    result = asyncio.run(engine.search(query="calculate_total", max_tokens=800))
    assert result["status"] == "success"
    assert result["snippets_count"] >= 1


# ---------------------------------------------------------------------------
# Diagnóstico de ambiente (conftest)
# ---------------------------------------------------------------------------


def test_environment_diagnostic_reports_missing_optional_module(capsys):
    """Módulo ausente gera bloco amigável e lista de retorno real."""
    from tests.conftest import diagnose_test_environment

    missing = diagnose_test_environment(
        [("sys", False), ("modulo_inexistente_sga06_xyz", True)]
    )
    assert missing == ["modulo_inexistente_sga06_xyz"]
    stderr = capsys.readouterr().err
    assert "Diagnóstico de ambiente" in stderr
    assert "modulo_inexistente_sga06_xyz (opcional" in stderr
    assert ".venv/bin/pytest tests/" in stderr


def test_environment_diagnostic_is_silent_when_all_present(capsys):
    """Sem ausências não há ruído no stderr da suíte."""
    from tests.conftest import diagnose_test_environment

    assert diagnose_test_environment([("sys", False)]) == []
    assert capsys.readouterr().err == ""
