"""TICKET-SGA-06 — Endurecimento determinístico de sandbox, identidade e stdio.

Suíte complementar à ``test_sga06_sandbox_denylist.py``, sem MOCKS: motores e
servidor REAIS, filesystem temporário real, subprocesso stdio real e cliente
MCP oficial do SDK (``mcp.client.stdio``), além de AST/scanners estáticos
reais para os invariantes de identidade.

Cobertura:
1. Sandbox: ``scope`` do Needle (não só o do QMD), bypass por symlink, modo
   estrito aceitando as grafias documentadas e mapeamento estruturado da
   violação no servidor MCP (``status="error"``/``sandbox_violation``).
2. Identidade: ``package.json``/``pyproject.toml`` canônicos, docs sem URLs do
   repositório legado e arquivamento histórico em ``docs/archive/``.
3. Saneamento: loggers do pacote sem namespace legado ``jev.*``, docstring de
   ``__init__.py`` sanitizada e CI sem variáveis ``JEV_*``.
4. Smoke real de inicialização do servidor MCP via stdio (handshake JSON-RPC
   ``initialize`` + ``tools/list`` + ``tools/call`` de busca cirúrgica).
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
from pathlib import Path

import pytest

from agent_quality_guard.memory.local_needle import (
    ALLOWED_ROOTS_ENV,
    STRICT_SANDBOX_ENV,
    SYSTEM_RESTRICTED_MESSAGE,
    LocalNeedleEngine,
    SandboxViolationError,
)
from agent_quality_guard.mcp_server import _cirurgic_search_core
from agent_quality_guard.core.schemas import Needle3Query

ROOT = Path(__file__).resolve().parents[1]
PKG_DIR = ROOT / "agent_quality_guard"
CANONICAL_REPO_URL = "https://github.com/maiteuxzanela/search-guard-agent"
LEGACY_REPO_SLUG = "maiteuxzanela/agent_quality_guard"


@pytest.fixture(autouse=True)
def _clean_sandbox_env(monkeypatch):
    """Nenhum teste herda allowlist/estrito do ambiente do runner."""
    monkeypatch.delenv(ALLOWED_ROOTS_ENV, raising=False)
    monkeypatch.delenv(STRICT_SANDBOX_ENV, raising=False)


@pytest.fixture
def project(tmp_path):
    """Projeto real com symbol distintivo e um alias symlink para /etc."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "billing.py").write_text(
        "def settle_invoice(total: int) -> int:\n    return total * 3\n",
        encoding="utf-8",
    )
    return workspace


# ---------------------------------------------------------------------------
# 1. Sandbox — superfícies que faltavam
# ---------------------------------------------------------------------------


def test_needle_scope_system_directory_is_rejected(project):
    """``scope`` do motor Needle (não só QMD) veta diretório de sistema."""
    import asyncio

    engine = LocalNeedleEngine(workspace_root=str(project))
    query = Needle3Query(query="settle_invoice", scope=["/etc"], max_tokens=400)
    with pytest.raises(SandboxViolationError) as excinfo:
        asyncio.run(engine.search(query))
    assert str(excinfo.value) == SYSTEM_RESTRICTED_MESSAGE


def test_needle_default_scope_search_still_works(project):
    """Controle positivo: o mesmo motor encontra o symbol dentro do workspace."""
    import asyncio

    engine = LocalNeedleEngine(workspace_root=str(project))
    query = Needle3Query(query="settle_invoice", max_tokens=400)
    response = asyncio.run(engine.search(query))
    assert len(response.snippets) >= 1
    assert any("settle_invoice" in snippet.content for snippet in response.snippets)


def test_symlink_escape_to_system_directory_is_rejected(project, tmp_path):
    """Symlink interno -> /etc é resolvido e barrado (não vira alvo legítimo)."""
    alias = project / "system_alias"
    try:
        alias.symlink_to("/etc")
    except (OSError, NotImplementedError):  # pragma: no cover - fs sem symlink
        pytest.skip("filesystem sem suporte a symlinks")
    engine = LocalNeedleEngine(workspace_root=str(project))
    with pytest.raises(SandboxViolationError) as excinfo:
        engine._resolve_workspace(str(alias))
    assert str(excinfo.value) == SYSTEM_RESTRICTED_MESSAGE
    assert not Path(str(alias)).resolve().is_relative_to(project.resolve())


@pytest.mark.parametrize("flag", ["1", "true", "TRUE", "yes", "on"])
def test_strict_mode_accepts_documented_truthy_spellings(project, monkeypatch, flag):
    """As grafias documentadas de ``CIRURGIC_SANDBOX_STRICT`` ativam o estrito."""
    monkeypatch.chdir(project)
    monkeypatch.setenv(STRICT_SANDBOX_ENV, flag)
    assert LocalNeedleEngine(workspace_root=str(project)).workspace_root == (
        project.resolve()
    )


@pytest.mark.parametrize("flag", ["", "0", "false", "no", "off"])
def test_strict_mode_falsey_values_keep_frictionless_flow(
    project, tmp_path, monkeypatch, flag
):
    """Valor desligado não restringe: outro projeto real continua aceito."""
    other = tmp_path / "other_project"
    other.mkdir()
    monkeypatch.chdir(project)
    monkeypatch.setenv(STRICT_SANDBOX_ENV, flag)
    assert LocalNeedleEngine(workspace_root=str(other)).workspace_root == (
        other.resolve()
    )


async def test_mcp_core_maps_system_workspace_to_structured_error():
    """O núcleo MCP nunca vaza exceção crua: vira ``sandbox_violation``."""
    result = await _cirurgic_search_core(query="settle_invoice", workspace="/etc")
    assert result["status"] == "error"
    assert result["error"] == "sandbox_violation"
    assert result["message"] == SYSTEM_RESTRICTED_MESSAGE
    assert result["degraded_lanes"] == ["sandbox"]
    assert result["snippets"] == []
    assert result["snippets_count"] == 0


async def test_mcp_core_scope_system_directory_maps_to_structured_error(project):
    """``scope`` de sistema via camada MCP também retorna erro estruturado."""
    result = await _cirurgic_search_core(
        query="settle_invoice", workspace=str(project), scope="/proc"
    )
    assert result["status"] == "error"
    assert result["error"] == "sandbox_violation"
    assert result["message"] == SYSTEM_RESTRICTED_MESSAGE


# ---------------------------------------------------------------------------
# 2. Identidade — nomes, metadados e arquivamento
# ---------------------------------------------------------------------------


def test_package_json_identity_is_canonical():
    """``package.json`` publica ``search-guard-agent`` com bin e URLs canônicos."""
    manifest = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
    assert manifest["name"] == "search-guard-agent"
    assert manifest["version"] == "0.1.0"
    assert manifest["repository"]["url"] == (
        f"git+{CANONICAL_REPO_URL}.git"
    )
    assert manifest["bugs"]["url"] == f"{CANONICAL_REPO_URL}/issues"
    assert manifest["homepage"] == f"{CANONICAL_REPO_URL}#readme"
    assert manifest["bin"] == {
        "search-guard-agent": "./bin/agent-quality-guard.js",
        "agent-quality-guard": "./bin/agent-quality-guard.js",
    }
    for alias, rel in manifest["bin"].items():
        assert (ROOT / rel).is_file(), f"bin['{alias}'] -> {rel} inexistente"
    assert "search-guard-agent" in manifest["keywords"]


def test_pyproject_identity_is_canonical():
    """``pyproject.toml`` alinha nome do pacote e URLs ao repositório novo."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert re.search(r'^name = "search-guard-agent"$', pyproject, re.MULTILINE)
    urls_block = pyproject.split("[project.urls]", 1)[1].split("[project.scripts]", 1)[0]
    for key in ("Homepage", "Repository", '"Bug Tracker"'):
        assert f'{key} = "{CANONICAL_REPO_URL}' in urls_block, urls_block
    assert LEGACY_REPO_SLUG not in pyproject
    scripts_block = pyproject.split("[project.scripts]", 1)[1]
    assert (
        'agent-quality-guard-mcp = "agent_quality_guard.mcp_server:main"'
        in scripts_block
    )
    assert 'agent-quality-guard = "agent_quality_guard.cli:main"' in scripts_block


@pytest.mark.parametrize("doc", ["README.md", "SECURITY.md", "CONTRIBUTING.md"])
def test_docs_drop_legacy_repository_urls(doc):
    """Docs públicos não apontam mais para o slug legado do GitHub."""
    text = (ROOT / doc).read_text(encoding="utf-8")
    assert LEGACY_REPO_SLUG not in text
    assert "open-jev" not in text
    assert "openjev" not in text.lower()


def test_security_disclosure_points_to_canonical_repository():
    """Canal de vulnerabilidade aponta para o repositório canônico."""
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    assert f"{CANONICAL_REPO_URL}/security/advisories/new" in security


def test_readme_install_commands_use_canonical_package_name():
    """Comandos de instalação documentados usam o nome publicado."""
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "npx -y search-guard-agent" in readme
    # Só a coluna "Before" da tabela de migração pode citar o nome antigo.
    install_lines = [
        line.strip()
        for line in readme.splitlines()
        if line.strip().startswith("npx -y")
    ]
    assert install_lines, "README sem comandos npx"
    assert all("npx -y search-guard-agent" in line for line in install_lines), install_lines


def test_legacy_docs_are_archived_and_root_stays_clean():
    """Documentação histórica vive em ``docs/archive/``, não na raiz de docs."""
    archive = ROOT / "docs" / "archive"
    assert archive.is_dir(), "docs/archive/ ausente"
    archived = {p.name for p in archive.iterdir() if p.is_file()}
    assert {
        "adr_circuit_breaker_and_anti_gaming.md",
        "auditoria_needle_verify_delivery.md",
        "projeto_jev_audit_report.md",
        "relatorio_arquitetura_validacao_jev.md",
    } <= {name.lower() for name in archived}
    root_legacy = [
        p.name
        for p in (ROOT / "docs").iterdir()
        if p.is_file() and re.search(r"jev|audit|circuit|state_machine", p.name, re.I)
    ]
    assert root_legacy == [], f"docs legados ainda na raiz de docs: {root_legacy}"


# ---------------------------------------------------------------------------
# 3. Saneamento — loggers, docstring e CI
# ---------------------------------------------------------------------------


def _logger_literal_names() -> list[tuple[Path, str]]:
    """Extrai nomes literais passados a ``logging.getLogger`` via AST real."""
    found: list[tuple[Path, str]] = []
    for path in sorted(PKG_DIR.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            is_getlogger = (
                isinstance(func, ast.Attribute)
                and func.attr == "getLogger"
                and isinstance(func.value, ast.Name)
                and func.value.id == "logging"
            )
            if not is_getlogger or not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                found.append((path, arg.value))
    return found


def test_no_logger_uses_legacy_jev_namespace():
    """Nenhum logger do pacote usa o namespace legado ``jev.*``."""
    loggers = _logger_literal_names()
    assert loggers, "nenhum logger encontrado — parser AST quebrado?"
    legacy = [(p.name, name) for p, name in loggers if name.startswith("jev.")]
    assert legacy == [], f"loggers legados jev.*: {legacy}"


def test_renamed_loggers_are_in_agent_quality_guard_namespace():
    """Os módulos renomeados declaram ``agent_quality_guard.*`` explicitamente."""
    renamed_modules = {
        "multi_stack_runner.py",
        "pytest_chunk_runner.py",
        "subagent_runner.py",
        "mcp_server.py",
        "score_gate.py",
        "transcript_sniffer.py",
        "needle_client.py",
        "needle_ast_guard.py",
        "needle_defect_screener.py",
        "ast_diff_extractor.py",
        "tree_sitter_guard.py",
        "local_needle.py",
        "qmd_search.py",
    }
    by_module = {}
    for path, name in _logger_literal_names():
        by_module.setdefault(path.name, []).append(name)
    missing = sorted(renamed_modules - set(by_module))
    assert missing == [], f"módulos sem logger literal: {missing}"
    wrong = {
        module: [n for n in names if not n.startswith("agent_quality_guard")]
        for module, names in by_module.items()
        if module in renamed_modules
    }
    assert {m: n for m, n in wrong.items() if n} == {}


def test_package_docstring_is_sanitized():
    """``__init__.py`` não carrega mais menções ao projeto legado."""
    init_source = (PKG_DIR / "__init__.py").read_text(encoding="utf-8")
    docstring = ast.get_docstring(ast.parse(init_source)) or ""
    assert "search-guard-agent" in docstring
    for legacy in ("Projeto JEV", "Tri-Camada", "Projeto JEV –"):
        assert legacy not in docstring


def test_ci_workflow_has_no_legacy_jev_env():
    """O workflow de CI não declara mais variáveis ``JEV_*`` legadas."""
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    legacy_keys = sorted(set(re.findall(r"\bJEV_[A-Z0-9_]+\b", workflow)))
    assert legacy_keys == [], f"variáveis legadas no CI: {legacy_keys}"


# ---------------------------------------------------------------------------
# 4. Smoke real de inicialização via stdio (cliente MCP oficial)
# ---------------------------------------------------------------------------


async def test_stdio_server_initialize_list_tools_and_real_search(project):
    """Handshake JSON-RPC real: initialize, tools/list e tools/call."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {ALLOWED_ROOTS_ENV, STRICT_SANDBOX_ENV}
    }
    env["PYTHONPATH"] = str(ROOT)
    params = StdioServerParameters(
        command=sys.executable,
        args=[str(PKG_DIR / "mcp_server.py")],
        env=env,
        cwd=str(ROOT),
    )
    async with stdio_client(params) as (read_stream, write_stream):
        async with ClientSession(read_stream, write_stream) as session:
            init = await session.initialize()
            assert init.server_info.name == "cirurgic-search-mcp"
            assert init.protocol_version

            tools = await session.list_tools()
            names = {tool.name for tool in tools.tools}
            assert {
                "cirurgic_search",
                "qmd_search",
                "agent_quality_guard",
                "needle_verify_delivery",
            } <= names

            search = await session.call_tool(
                "cirurgic_search",
                {"query": "settle_invoice", "workspace": str(project), "max_tokens": 600},
            )
            assert search.is_error is False
            text = "".join(
                getattr(block, "text", "") for block in search.content
            )
            payload, _end = json.JSONDecoder().raw_decode(text)
            assert payload["status"] in {"success", "partial"}
            assert payload["snippets_count"] >= 1

            denied = await session.call_tool(
                "cirurgic_search", {"query": "settle_invoice", "workspace": "/etc"}
            )
            assert denied.is_error is False
            denied_payload, _end = json.JSONDecoder().raw_decode(
                "".join(getattr(block, "text", "") for block in denied.content)
            )
            assert denied_payload["status"] == "error"
            assert denied_payload["error"] == "sandbox_violation"
            assert denied_payload["message"] == SYSTEM_RESTRICTED_MESSAGE
