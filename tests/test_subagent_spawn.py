"""Testes reais do SubagentRunner e da tool openjev_spawn_subagent.

Regra Mandatória: ZERO MOCKS. Utiliza repositórios reais temporários (git init)
e instâncias reais do Needle 3 AST Guardrail e Schemas Pydantic.
"""

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from src.core.schemas import SpawnSubagentRequest, SpawnSubagentResponse
from src.mcp_server import openjev_spawn_subagent
from src.orchestrator.subagent_runner import (
    SubagentRunner,
    classify_execution_level,
    extract_system_prompt_from_markdown,
    resolve_persona_file,
)


@pytest.fixture
def temp_git_workspace():
    """Cria um workspace Git real em diretório temporário."""
    tmp_dir = tempfile.mkdtemp(prefix="jev_test_subagent_")
    ws_path = Path(tmp_dir).resolve()

    # Inicializa repo Git real
    subprocess.run(["git", "init"], cwd=str(ws_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=str(ws_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@jev.local"], cwd=str(ws_path), check=True)

    # Cria diretório de personas
    personas_dir = ws_path / "personas"
    personas_dir.mkdir(parents=True, exist_ok=True)

    persona_md = personas_dir / "lumi_ui_ux_frontend.md"
    persona_md.write_text(
        "# Lumi — UI/UX & Frontend\n\n"
        "```markdown\n"
        "[ROLE: Especialista sênior em design system, React e CSS]\n"
        "Você desenvolve interfaces modernas e acessíveis sem degradar a arquitetura.\n"
        "```\n",
        encoding="utf-8",
    )

    # Commit inicial para ter HEAD válida
    init_file = ws_path / "README.md"
    init_file.write_text("# Project Root\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(ws_path), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(ws_path), check=True)

    yield ws_path

    # Cleanup
    shutil.rmtree(tmp_dir, ignore_errors=True)


def test_resolve_persona_and_prompt(temp_git_workspace: Path):
    """Testa a resolução de personas locais e extração de system prompt."""
    name, sys_prompt, p_file = resolve_persona_file("lumi", temp_git_workspace)
    assert "lumi" in name.lower()
    assert "[ROLE: Especialista sênior em design system" in sys_prompt
    assert p_file.exists()

    # Teste com @ prefixado
    name2, sys_prompt2, _ = resolve_persona_file("@Lumi", temp_git_workspace)
    assert "lumi" in name2.lower()
    assert sys_prompt2 == sys_prompt


def test_resolve_global_persona_fallback(temp_git_workspace: Path):
    """Testa o fallback para personas do projeto JEV quando não estiverem no workspace alvo."""
    name, sys_prompt, p_file = resolve_persona_file("kael", temp_git_workspace)
    assert "kael" in name.lower()
    assert len(sys_prompt) > 20
    assert p_file.exists()


def test_resolve_persona_not_found(temp_git_workspace: Path):
    """Testa erro amigável quando a persona não existe."""
    with pytest.raises(FileNotFoundError, match="não encontrada"):
        resolve_persona_file("persona_fantasma_inexistente_123", temp_git_workspace)


@pytest.mark.asyncio
async def test_subagent_runner_clean_execution(temp_git_workspace: Path):
    """Testa execução de subagente gerando código limpo (AST aprovada)."""
    runner = SubagentRunner(engine=None)

    # Simula alteração limpa no workspace pelo subagente
    clean_code = (
        '"""Módulo de utilitários limpos."""\n\n'
        'def somar(a: int, b: int) -> int:\n'
        '    """Soma dois inteiros com validação de tipo."""\n'
        '    return a + b\n'
    )
    (temp_git_workspace / "calculator.py").write_text(clean_code, encoding="utf-8")

    req = SpawnSubagentRequest(
        persona="lumi",
        task="Implementar módulo de soma de números inteiros",
        workspace=str(temp_git_workspace),
        auto_validate=True,
    )

    resp = await runner.spawn(req)

    assert resp.success is True
    assert resp.ast_blocked is False
    assert resp.ast_violations is None
    assert "calculator.py" in resp.files_modified
    assert resp.passed is True
    assert resp.context_tokens_used > 0


@pytest.mark.asyncio
async def test_subagent_runner_ast_guard_intercepts_mock(temp_git_workspace: Path):
    """Testa interceptação fail-fast quando o subagente tenta introduzir mocks (Política Anti-Mock)."""
    runner = SubagentRunner(engine=None)

    # Simula código violador introduzido pelo subagente
    violating_code = (
        'from unittest.mock import MagicMock\n\n'
        'def process_order():\n'
        '    mock = MagicMock()\n'
        '    return mock\n'
    )
    (temp_git_workspace / "service_order.py").write_text(violating_code, encoding="utf-8")

    req = SpawnSubagentRequest(
        persona="lumi",
        task="Escrever serviço com mock",
        workspace=str(temp_git_workspace),
        auto_validate=True,
    )

    resp = await runner.spawn(req)

    assert resp.success is False
    assert resp.ast_blocked is True
    assert resp.score == 1.00
    assert resp.passed is False
    assert resp.ast_violations is not None
    assert any(v["rule_id"] == "mock_in_production" for v in resp.ast_violations)


@pytest.mark.asyncio
async def test_subagent_runner_ast_guard_intercepts_sqli(temp_git_workspace: Path):
    """Testa interceptação fail-fast quando o subagente tenta introduzir injeção SQL."""
    runner = SubagentRunner(engine=None)

    sqli_code = (
        'def query_user(user_id):\n'
        '    sql = f"SELECT * FROM users WHERE id = {user_id}"\n'
        '    return sql\n'
    )
    (temp_git_workspace / "db.py").write_text(sqli_code, encoding="utf-8")

    req = SpawnSubagentRequest(
        persona="lumi",
        task="Criar consulta de usuário no banco de dados",
        workspace=str(temp_git_workspace),
        auto_validate=True,
    )

    resp = await runner.spawn(req)

    assert resp.success is False
    assert resp.ast_blocked is True
    assert resp.score == 1.00
    assert resp.passed is False
    assert any(v["rule_id"] == "sql_injection" for v in resp.ast_violations)


@pytest.mark.asyncio
async def test_mcp_openjev_spawn_subagent_tool(temp_git_workspace: Path):
    """Testa a tool MCP openjev_spawn_subagent diretamente."""
    # Validação de argumentos obrigatórios
    with pytest.raises(ValueError, match="exige o parametro 'persona'"):
        await openjev_spawn_subagent(persona="", task="teste")

    with pytest.raises(ValueError, match="exige o parametro 'task'"):
        await openjev_spawn_subagent(persona="lumi", task="   ")

    # Execução limpa com timeout desativado explicitamente
    res = await openjev_spawn_subagent(
        persona="lumi",
        task="Testar criação de componente limpo",
        workspace=str(temp_git_workspace),
        auto_validate=True,
        timeout=None,
    )

    assert isinstance(res, dict)
    assert res["success"] is True
    assert res["ast_blocked"] is False
    assert res["persona"] == "lumi_ui_ux_frontend"


def test_three_levels_persona_resolution():
    """Valida resolução de personas do Roster formal (Nível 2) e detecção de ausência (Nível 3)."""
    jev_root = Path("/home/maiteuxzanela/projeto JEV")
    jez_root = Path("/mnt/94CCB337CCB3130A/JEZ collections")

    # Personas formais do JEV
    for p_name in ["valerio", "kael", "maya", "tulio", "clara", "otavio"]:
        name, prompt, path = resolve_persona_file(p_name, jev_root)
        assert p_name in name.lower()
        assert len(prompt) > 20
        assert path.exists()

    # Personas formais do JEZ
    for p_name in ["alex", "lumi", "ariel", "sam", "cris", "morgan", "noa", "robin"]:
        name, prompt, path = resolve_persona_file(p_name, jez_root)
        assert p_name in name.lower()
        assert len(prompt) > 20
        assert path.exists()

    # Personas formais do Panela de Sobra (PanelaBuild)
    panela_root = Path("/home/maiteuxzanela/PanelaBuild")
    for p_name in ["zelia", "erica", "ana", "bia", "carla", "diana", "iara", "cris", "helena"]:
        name, prompt, path = resolve_persona_file(p_name, panela_root)
        assert p_name in name.lower()
        assert len(prompt) > 20
        assert path.exists()

    # Personas formais do Faturamento
    faturamento_root = Path("/mnt/94CCB337CCB3130A/Faturamento")
    for p_name in ["cecilia", "beatriz", "camila", "daniela", "fernanda", "gabriela", "isabela", "juliana", "leticia"]:
        name, prompt, path = resolve_persona_file(p_name, faturamento_root)
        assert p_name in name.lower()
        assert len(prompt) > 20
        assert path.exists()

    # Persona inexistente aciona Nível 3 (proposição de nova persona)
    with pytest.raises(FileNotFoundError):
        resolve_persona_file("especialista_inexistente_xyz", jev_root)


def test_classify_execution_level():
    """Valida a categorização precisa dos Três Níveis de Execução."""
    # Nível 1: Reflexivo, dúvidas conceituais, doc, git
    assert classify_execution_level("Explicar a arquitetura tri-camada do JEV") == 1
    assert classify_execution_level("Como funciona o cache do Firestore?") == 1
    assert classify_execution_level("Atualizar notas de versao", target_files=["CHANGELOG.md"]) == 1
    assert classify_execution_level("Inspecionar status do git") == 1

    # Nível 2: Implementação técnica, código de produção, testes, APIs
    assert classify_execution_level("Implementar novo endpoint de streaming no FastAPI") == 2
    assert classify_execution_level("Criar componente de checkout acessivel") == 2
    assert classify_execution_level("Corrigir bug de transacao no Firestore") == 2
    assert classify_execution_level("Adicionar testes unitarios de regressao") == 2
    assert classify_execution_level("Refatorar servico", target_files=["src/service.py"]) == 2


@pytest.mark.asyncio
async def test_subagent_timeout_configuration(temp_git_workspace: Path):
    """Verifica que o timeout é desativado por padrão (None) e tratável quando configurado."""
    req_default = SpawnSubagentRequest(
        persona="lumi",
        task="Tarefa rápida de verificação",
        workspace=str(temp_git_workspace),
    )
    assert req_default.timeout is None

    runner = SubagentRunner(engine=None)
    # Cria script executável simples que dorme para testar timeout sem mocks
    dummy_bin = temp_git_workspace / "fake_hermes.sh"
    dummy_bin.write_text("#!/bin/sh\nsleep 2\n", encoding="utf-8")
    dummy_bin.chmod(0o755)

    res = await runner._execute_via_hermes(
        hermes_bin=str(dummy_bin),
        persona_name="lumi",
        system_prompt="ROLE: teste",
        task="teste de timeout",
        workspace=temp_git_workspace,
        timeout=0.05,
    )
    assert "Timeout de execução atingido" in res
    assert "(0.05s)" in res


@pytest.mark.asyncio
async def test_hermes_args_and_workspace_jail(temp_git_workspace: Path):
    """Verifica que o Hermes é invocado sem --yolo por padrão, com cwd correto e restrição de diretório no prompt."""
    runner = SubagentRunner(engine=None)

    # Script real que grava seus argumentos e diretório atual em arquivo
    record_file = temp_git_workspace / "hermes_record.txt"
    probe_bin = temp_git_workspace / "probe_hermes.sh"
    probe_bin.write_text(
        f"#!/bin/sh\n"
        f"echo \"PWD: $(pwd)\" > \"{record_file}\"\n"
        f"echo \"ARGS: $@\" >> \"{record_file}\"\n"
        f"echo \"OK\"\n",
        encoding="utf-8",
    )
    probe_bin.chmod(0o755)

    res = await runner._execute_via_hermes(
        hermes_bin=str(probe_bin),
        persona_name="lumi",
        system_prompt="ROLE: Frontend Dev",
        task="Ajustar estilo CSS",
        workspace=temp_git_workspace,
        timeout=5.0,
    )
    assert res == "OK"

    record_content = record_file.read_text(encoding="utf-8")
    # 1. Garante que --yolo NÃO está presente nos argumentos por padrão
    assert "--yolo" not in record_content

    # 2. Garante que o CWD do processo é o workspace do projeto
    assert f"PWD: {temp_git_workspace}" in record_content

    # 3. Garante que --in workspace está presente
    assert f"--in {temp_git_workspace}" in record_content

    # 4. Garante que o prompt contém a cláusula explícita de confinamento
    assert "Restrição de Escopo: Opere estritamente dentro de" in record_content
    assert str(temp_git_workspace) in record_content
