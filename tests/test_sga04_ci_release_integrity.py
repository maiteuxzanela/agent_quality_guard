"""TICKET-SGA-04 — Integridade determinística de CI, publish e paridade de versão.

Suíte 100% determinística e SEM MOCKS: os YAMLs reais são parseados com PyYAML,
o script de paridade EMBUTIDO nos workflows é extraído e EXECUTADO de verdade em
um diretório temporário com os artefatos reais copiados, e os nodeids de
``--deselect`` do job de resiliência são resolvidos pelo pytest real.

Cobre:
1. ``.github/workflows/ci.yml``: gatilhos, permissões, matriz Python 3.10–3.12 ×
   Ubuntu/macOS, ripgrep no macOS, job sem tree-sitter (filtro real de
   requirements, instalação ``--no-deps -e``, assert de ausência, smoke
   fail-open e exclusões declarativas válidas).
2. ``.github/workflows/publish.yml``: gatilho de release, ``id-token: write``,
   ``npm publish --provenance --access public`` e paridade ANTES do publish.
3. Paridade ``pyproject.toml`` ↔ ``package.json`` ↔ ``__init__.py``: aceita o
   estado real do repositório e aborta (exit != 0) em qualquer divergência ou
   ``__version__`` ausente — com fallback ``tomllib``/``tomli`` nos scripts
   embutidos (Python 3.10).
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    import tomli as tomllib

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CI_PATH = ROOT / ".github" / "workflows" / "ci.yml"
PUBLISH_PATH = ROOT / ".github" / "workflows" / "publish.yml"

PARITY_OK_SENTINEL = "OK: paridade de versao garantida"
PARITY_ABORT_SENTINEL = "ABORT:"


# ──────────────────────────────────────────────────────────────────────────────
# Helpers (parser YAML / extrator de heredoc / executor real)
# ──────────────────────────────────────────────────────────────────────────────


def _load_workflow(path: Path) -> dict:
    assert path.is_file(), f"workflow ausente: {path}"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert isinstance(data, dict), f"YAML invalido em {path}"
    return data


def _triggers(workflow: dict) -> dict:
    """PyYAML (YAML 1.1) converte a chave ``on`` para o booleano True."""
    triggers = workflow.get("on", workflow.get(True))
    assert isinstance(triggers, dict), "gatilho 'on' ausente/invalido"
    return triggers


def _job(workflow: dict, job_id: str) -> dict:
    jobs = workflow["jobs"]
    assert job_id in jobs, f"job '{job_id}' ausente; encontrados: {sorted(jobs)}"
    return jobs[job_id]


def _steps(job: dict) -> list:
    return job["steps"]


def _step_by_name(job: dict, needle: str) -> dict:
    matches = [s for s in _steps(job) if needle in s.get("name", "")]
    assert matches, (
        f"nenhum step com '{needle}' em {[s.get('name') for s in _steps(job)]}"
    )
    assert len(matches) == 1, f"step duplicado para '{needle}': {matches}"
    return matches[0]


def _extract_heredoc_script(run_block: str) -> str:
    """Extrai o corpo de ``python - <<'PY' ... PY`` de um bloco ``run: |``."""
    lines = run_block.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == "python - <<'PY'"]
    assert len(starts) == 1, f"esperava exatamente 1 heredoc, achei {len(starts)}"
    start = starts[0]
    ends = [i for i in range(start + 1, len(lines)) if lines[i].strip() == "PY"]
    assert ends, "heredoc sem terminador PY"
    body = lines[start + 1 : ends[0]]
    script = textwrap.dedent("\n".join(body)) + "\n"
    assert "__version__" in script and "tomllib" in script
    return script


def _parity_script(path: Path, workflow: dict, job_id: str) -> str:
    step = _step_by_name(_job(workflow, job_id), "version parity")
    return _extract_heredoc_script(step["run"])


def _run_parity_script(script: str, repo_root: Path) -> subprocess.CompletedProcess:
    """Executa o script REAL do workflow (`python -`) contra um repo isolado."""
    return subprocess.run(
        [sys.executable, "-"],
        input=script,
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _snapshot_parity_repo(tmp_path: Path) -> Path:
    """Copia os 3 artefatos reais de release para um diretório temporário."""
    repo = tmp_path / "parity_repo"
    (repo / "agent_quality_guard").mkdir(parents=True)
    shutil.copy2(ROOT / "pyproject.toml", repo / "pyproject.toml")
    shutil.copy2(ROOT / "package.json", repo / "package.json")
    shutil.copy2(
        ROOT / "agent_quality_guard" / "__init__.py",
        repo / "agent_quality_guard" / "__init__.py",
    )
    return repo


def _repo_versions() -> dict:
    pyproject_version = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["version"]
    package_version = json.loads(
        (ROOT / "package.json").read_text(encoding="utf-8")
    )["version"]
    init_text = (ROOT / "agent_quality_guard" / "__init__.py").read_text(
        encoding="utf-8"
    )
    match = re.search(
        r'^__version__\s*=\s*["\']([^"\']+)["\']', init_text, re.MULTILINE
    )
    assert match is not None, "__version__ ausente no __init__ real"
    return {
        "pyproject.toml": pyproject_version,
        "package.json": package_version,
        "agent_quality_guard/__init__.py": match.group(1),
    }


# ──────────────────────────────────────────────────────────────────────────────
# 1. ci.yml — estrutura, gatilhos e matriz
# ──────────────────────────────────────────────────────────────────────────────


def test_both_workflows_are_valid_yaml_with_jobs():
    for path in (CI_PATH, PUBLISH_PATH):
        workflow = _load_workflow(path)
        assert workflow["jobs"], f"{path.name} sem jobs"
        assert isinstance(workflow.get("name"), str) and workflow["name"].strip()


def test_ci_triggers_permissions_and_env_are_strict():
    workflow = _load_workflow(CI_PATH)
    triggers = _triggers(workflow)

    for event in ("push", "pull_request"):
        assert event in triggers, f"gatilho {event} ausente"
        branches = triggers[event]["branches"]
        assert branches == ["main", "master"], branches
    assert "workflow_dispatch" in triggers

    assert workflow["permissions"] == {"contents": "read"}
    env = workflow["env"]
    # TICKET-SGA-06: variáveis legadas do Projeto JEV foram expurgadas do CI.
    assert "JEV_USE_HERMES" not in env
    assert not [key for key in env if key.startswith("JEV_")], env
    # Loopback sempre fora de proxies no runner (evita flakiness de rede local).
    assert "127.0.0.1" in env["NO_PROXY"] and "::1" in env["NO_PROXY"]


def test_ci_test_matrix_covers_python_310_to_312_on_two_oses():
    workflow = _load_workflow(CI_PATH)
    job = _job(workflow, "test")
    matrix = job["strategy"]["matrix"]

    assert matrix["os"] == ["ubuntu-latest", "macos-latest"]
    # 3.13 fica de fora: o PyPI ainda nao publica wheel de
    # tree-sitter-language-pack cp313 (instalacao falharia no job com parsers).
    assert matrix["python-version"] == ["3.10", "3.11", "3.12"]
    assert job["strategy"]["fail-fast"] is False
    assert job["runs-on"] == "${{ matrix.os }}"
    assert job.get("timeout-minutes") == 30


def test_ci_jobs_install_ripgrep_on_both_oses():
    workflow = _load_workflow(CI_PATH)

    # Step unificado: cobre Linux e macOS no MESMO bloco de shell.
    test_job = _job(workflow, "test")
    step = _step_by_name(test_job, "Install ripgrep")
    assert "if" not in step, step
    script = step["run"]
    assert "RUNNER_OS" in script
    assert "Linux" in script and "apt-get install -y -qq ripgrep" in script
    assert "macOS" in script and "brew install ripgrep" in script
    # So existe uma vez no job (sem step duplicado/legado).
    assert [s.get("name") for s in _steps(test_job)].count("Install ripgrep") == 1

    # qmd_search e os testes de paridade rodam no job de resiliencia e exigem
    # `rg` real; o step precisa vir antes da instalacao das dependencias.
    resilience_job = _job(workflow, "test-without-tree-sitter")
    resilience_script = _step_by_name(resilience_job, "Install ripgrep")["run"]
    assert "RUNNER_OS" in resilience_script
    assert "macOS" in resilience_script and "brew install ripgrep" in resilience_script

    names = [s.get("name", "") for s in _steps(resilience_job)]
    assert names.index("Install ripgrep") < names.index(
        "Install core dependencies WITHOUT optional tree-sitter parsers"
    ), names


def test_ci_layer2_jitter_calibration_is_conditional_on_ci_env():
    """O teto de tempo da Camada 2 relaxa SÓ quando a variável ``CI`` está presente.

    TICKET-CI-02: runners GitHub Actions são 2-5x mais lentos que a máquina
    local; o contrato ``< 1s`` local precisa de teto ``< 3s`` no CI sem
    enfraquecer a garantia de performance local. Prova determinística via AST
    real do arquivo de teste: a constant folding da comparação usa
    ``3.0 if os.environ.get("CI") else 1.0`` — remover o ramo CI (mutação
    típica) quebra este contrato estático.
    """
    target = ROOT / "tests" / "test_agent_quality_guard.py"
    source = target.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(target))
    fn = next(
        (
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef)
            and node.name == "test_layer2_meta_rules_classify_blocking_and_warning"
        ),
        None,
    )
    assert fn is not None, "teste da Camada 2 ausente de test_agent_quality_guard.py"
    segment = ast.get_source_segment(source, fn)
    assert segment is not None
    assert 'os.environ.get("CI")' in segment, (
        "calibração de jitter ausente: o teto de tempo não é condicionado à env CI"
    )
    assert "3.0" in segment and "1.0" in segment, (
        "faixa de teto incompleta: esperado 3.0 (CI) e 1.0 (local)"
    )


def test_ci_lint_job_runs_real_gates_and_version_parity():
    workflow = _load_workflow(CI_PATH)
    job = _job(workflow, "lint-and-typecheck")

    ruff = _step_by_name(job, "Run Ruff Lint")["run"]
    mypy = _step_by_name(job, "Run Mypy Typecheck")["run"]
    assert ruff.strip() == "ruff check ."
    assert mypy.strip() == "mypy agent_quality_guard/"

    setup = [s for s in _steps(job) if "setup-python" in s.get("uses", "")]
    assert len(setup) == 1
    assert str(setup[0]["with"]["python-version"]) == "3.12"

    # O step de paridade do job de lint é o script real (não um echo).
    script = _parity_script(CI_PATH, workflow, "lint-and-typecheck")
    assert "tomllib" in script and "package.json" in script


def test_ci_test_job_installs_requirements_and_runs_full_suite():
    workflow = _load_workflow(CI_PATH)
    job = _job(workflow, "test")

    install = _step_by_name(job, "Install dependencies (with tree-sitter)")["run"]
    assert "pip install -r requirements.txt" in install
    assert 'pip install ".[dev]"' in install

    suite = _step_by_name(job, "Run Deterministic Test Suite")["run"]
    assert suite.strip() == "python -m pytest tests/ -q"


# ──────────────────────────────────────────────────────────────────────────────
# 2. ci.yml — job de resiliência (SEM tree-sitter)
# ──────────────────────────────────────────────────────────────────────────────


def test_resilience_job_matrix_and_runner():
    workflow = _load_workflow(CI_PATH)
    job = _job(workflow, "test-without-tree-sitter")

    assert job["runs-on"] == "ubuntu-latest"
    assert job["strategy"]["matrix"]["python-version"] == ["3.10", "3.13"]
    assert job["strategy"]["fail-fast"] is False
    assert job.get("timeout-minutes") == 30


def test_resilience_install_filter_removes_exactly_tree_sitter_lines(tmp_path: Path):
    workflow = _load_workflow(CI_PATH)
    job = _job(workflow, "test-without-tree-sitter")
    install = _step_by_name(job, "Install core dependencies")["run"]

    # O filtro precisa ser exatamente ``^tree-sitter`` (linha inteira): um
    # padrão mais largo como ``^tree-sitter-language`` deixaria o
    # tree-sitter-language-pack instalado e mascararia o cenário de fallback.
    assert re.search(r"grep -v -E ['\"]\^tree-sitter['\"]", install), install

    # Aplica o MESMO filtro no requirements.txt real e prova o efeito.
    lines = (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    filtered = [
        line
        for line in lines
        if not re.match(r"^tree-sitter", line)
    ]
    assert not any(line.startswith("tree-sitter") for line in filtered)
    assert any(line.startswith("mcp") for line in filtered)
    assert any(line.startswith("pytest") for line in filtered)
    assert any(line.startswith("ruff") for line in filtered)
    assert any(line.startswith("mypy") for line in filtered)
    # Nada além de tree-sitter foi removido.
    assert len(filtered) == len(
        [line for line in lines if not line.startswith("tree-sitter")]
    )

    # O arquivo de requirements é materializado e instalado de verdade no CI.
    assert "/tmp/requirements-no-ts.txt" in install
    assert "pip install -r /tmp/requirements-no-ts.txt" in install


def test_resilience_job_installs_package_editable_without_deps():
    workflow = _load_workflow(CI_PATH)
    job = _job(workflow, "test-without-tree-sitter")
    names = [s.get("name", "") for s in _steps(job)]

    install_pkg = _step_by_name(job, "Install package (editable")["run"]
    # --no-deps e obrigatorio: ``pip install -e .`` puxaria tree-sitter do
    # [project].dependencies e mataria o cenario de fallback.
    assert "pip install --no-deps -e ." in install_pkg, install_pkg
    assert "tree-sitter" not in install_pkg, install_pkg

    # Ordem: pacote instalado ANTES do assert de ausencia e do smoke import.
    pkg_index = names.index("Install package (editable, without deps)")
    assert pkg_index < names.index("Assert tree-sitter is really absent")
    assert pkg_index < names.index("Import smoke test (fail-open sem tree-sitter)")


def test_resilience_job_asserts_both_modules_are_really_absent():
    workflow = _load_workflow(CI_PATH)
    job = _job(workflow, "test-without-tree-sitter")
    step = _step_by_name(job, "Assert tree-sitter is really absent")["run"]

    assert "importlib.util" in step
    assert "find_spec" in step
    for module in ("tree_sitter", "tree_sitter_language_pack"):
        assert module in step, f"assert de ausencia nao cobre {module}"
    # Falha real (nao apenas log) quando o parser aparece no ambiente.
    assert "sys.exit" in step


def test_resilience_job_has_real_fail_open_import_smoke():
    workflow = _load_workflow(CI_PATH)
    job = _job(workflow, "test-without-tree-sitter")
    step = _step_by_name(job, "Import smoke test")["run"]

    assert "from agent_quality_guard.memory import TreeSitterGuard" in step
    assert "assert TreeSitterGuard is not None" in step


def test_resilience_pytest_exclusions_resolve_against_real_suite():
    workflow = _load_workflow(CI_PATH)
    job = _job(workflow, "test-without-tree-sitter")
    command = _step_by_name(job, "Run suite exercising fallback path")["run"]

    assert "python -m pytest tests/ -q" in command
    assert not re.search(r"(?<!m )pytest tests/ -q", command), command
    ignores = re.findall(r"--ignore=(\S+)", command)
    deselects = re.findall(r"--deselect=(\S+)", command)
    assert ignores == [
        "tests/test_tree_sitter_guard.py",
        "tests/test_tree_sitter_multilang_needle.py",
    ], ignores
    assert deselects == [
        "tests/test_multilang_needle.py::TestTreeSitterRouteGateOn",
        # A auto-aprovação do gate em audit_mode="full" roda a meta-camada
        # tree-sitter (e um pytest aninhado) — impossivel sem a lib opcional.
        "tests/test_agent_quality_guard.py::test_quality_guard_full_audit_self_approves_repo_root",
    ], deselects

    for ignored in ignores:
        assert (ROOT / ignored).is_file(), f"--ignore aponta para arquivo inexistente: {ignored}"

    # Resolve os nodeids com o pytest REAL: se o teste for renomeado/removido,
    # a exclusão do CI vira silenciosamente inócua — este assert detecta isso.
    collected = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", *deselects],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    assert collected.returncode == 0, collected.stdout + collected.stderr
    for nodeid in deselects:
        assert nodeid in collected.stdout, (
            f"nodeid nao coletado pelo pytest: {nodeid}\n{collected.stdout}"
        )


# ──────────────────────────────────────────────────────────────────────────────
# 3. publish.yml — provenance, permissões e ordem dos steps
# ──────────────────────────────────────────────────────────────────────────────


def test_publish_triggers_and_oidc_permissions():
    workflow = _load_workflow(PUBLISH_PATH)
    triggers = _triggers(workflow)

    assert triggers["release"]["types"] == ["published"]
    assert "workflow_dispatch" in triggers

    job = _job(workflow, "publish")
    permissions = job["permissions"]
    assert permissions["id-token"] == "write", (
        "id-token: write e obrigatorio para provenance OIDC/Sigstore"
    )
    assert permissions["contents"] == "read"


def test_publish_step_uses_provenance_and_registry_token():
    workflow = _load_workflow(PUBLISH_PATH)
    job = _job(workflow, "publish")

    publish = _step_by_name(job, "Publish to NPM")["run"].strip()
    assert publish == "npm publish --provenance --access public"
    assert job["steps"][-1]["env"]["NODE_AUTH_TOKEN"] == "${{ secrets.NPM_TOKEN }}"

    setup_node = [s for s in _steps(job) if "setup-node" in s.get("uses", "")]
    assert len(setup_node) == 1
    assert setup_node[0]["with"]["registry-url"] == "https://registry.npmjs.org"


def test_publish_runs_suite_through_python_m_pytest():
    workflow = _load_workflow(PUBLISH_PATH)
    job = _job(workflow, "publish")
    suite = _step_by_name(job, "Run Deterministic Test Suite")["run"]
    assert suite.strip() == "python -m pytest tests/ -q"


def test_publish_enforces_version_parity_before_publishing():
    workflow = _load_workflow(PUBLISH_PATH)
    job = _job(workflow, "publish")
    names = [s.get("name", "") for s in _steps(job)]

    parity_index = next(i for i, n in enumerate(names) if "version parity" in n)
    publish_index = next(i for i, n in enumerate(names) if "Publish to NPM" in n)
    assert parity_index < publish_index, names

    script = _parity_script(PUBLISH_PATH, workflow, "publish")
    assert "len(set(versions.values())) != 1" in script


def test_parity_script_matches_between_ci_and_publish():
    ci_workflow = _load_workflow(CI_PATH)
    publish_workflow = _load_workflow(PUBLISH_PATH)

    ci_script = _parity_script(CI_PATH, ci_workflow, "lint-and-typecheck")
    publish_script = _parity_script(PUBLISH_PATH, publish_workflow, "publish")
    assert ci_script == publish_script, (
        "scripts de paridade divergiram entre ci.yml e publish.yml"
    )


def test_parity_scripts_fall_back_to_tomli_below_python311():
    """Os heredocs precisam importar em Python 3.10 (sem ``tomllib``)."""
    for path, workflow, job_id in (
        (CI_PATH, _load_workflow(CI_PATH), "lint-and-typecheck"),
        (PUBLISH_PATH, _load_workflow(PUBLISH_PATH), "publish"),
    ):
        script = _parity_script(path, workflow, job_id)
        assert "try:" in script, path
        assert "except ModuleNotFoundError" in script, path
        assert "import tomli as tomllib" in script, path
        # O fallback nao pode esconder o import direto do stdlib em 3.11+.
        assert re.search(r"(?m)^\s*import tomllib\s*$", script), path


def test_tomli_backport_declared_for_python_below_311():
    """``tomli`` cobre o heredoc de paridade e a suíte em Python 3.10."""
    marker = 'tomli>=1.1.0; python_version < "3.11"'

    pyproject = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert marker in pyproject["project"]["dependencies"], "tomli ausente em [project].dependencies"
    assert marker in pyproject["project"]["optional-dependencies"]["dev"], (
        "tomli ausente em optional-dependencies.dev"
    )

    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert marker in requirements, "tomli ausente em requirements.txt"


# ──────────────────────────────────────────────────────────────────────────────
# 4. Paridade de versão — estado real + execução real do script embutido
# ──────────────────────────────────────────────────────────────────────────────


def test_real_repository_versions_are_in_parity():
    versions = _repo_versions()
    assert len(set(versions.values())) == 1, versions
    assert versions["pyproject.toml"] == versions["package.json"]
    assert versions["pyproject.toml"] == versions["agent_quality_guard/__init__.py"]


def test_parity_script_passes_on_real_artifacts(tmp_path: Path):
    workflow = _load_workflow(CI_PATH)
    script = _parity_script(CI_PATH, workflow, "lint-and-typecheck")
    repo = _snapshot_parity_repo(tmp_path)

    result = _run_parity_script(script, repo)
    output = result.stdout + result.stderr
    assert result.returncode == 0, output
    expected_version = _repo_versions()["pyproject.toml"]
    assert f"{PARITY_OK_SENTINEL} ({expected_version})" in result.stdout


@pytest.mark.parametrize(
    "artifact",
    ["pyproject.toml", "package.json", "agent_quality_guard/__init__.py"],
)
def test_parity_script_aborts_on_each_divergent_artifact(
    tmp_path: Path, artifact: str
):
    workflow = _load_workflow(CI_PATH)
    script = _parity_script(CI_PATH, workflow, "lint-and-typecheck")
    repo = _snapshot_parity_repo(tmp_path)
    target = repo / artifact

    if artifact == "pyproject.toml":
        text = target.read_text(encoding="utf-8")
        mutated = re.sub(
            r'(?m)^version\s*=\s*"[^"]+"', 'version = "9.9.9"', text, count=1
        )
        assert mutated != text
        target.write_text(mutated, encoding="utf-8")
    elif artifact == "package.json":
        payload = json.loads(target.read_text(encoding="utf-8"))
        payload["version"] = "9.9.9"
        target.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    else:
        text = target.read_text(encoding="utf-8")
        mutated = re.sub(
            r'(?m)^__version__\s*=\s*["\'][^"\']+["\']',
            '__version__ = "9.9.9"',
            text,
            count=1,
        )
        assert mutated != text
        target.write_text(mutated, encoding="utf-8")

    result = _run_parity_script(script, repo)
    output = result.stdout + result.stderr
    assert result.returncode != 0, f"script NAO abortou com {artifact} divergente"
    assert PARITY_ABORT_SENTINEL in output, output
    assert "divergencia de versao" in output, output
    assert "9.9.9" in output
    assert PARITY_OK_SENTINEL not in output


def test_parity_script_aborts_when_version_marker_is_missing(tmp_path: Path):
    workflow = _load_workflow(CI_PATH)
    script = _parity_script(CI_PATH, workflow, "lint-and-typecheck")
    repo = _snapshot_parity_repo(tmp_path)
    init_file = repo / "agent_quality_guard" / "__init__.py"

    text = init_file.read_text(encoding="utf-8")
    mutated = re.sub(r"(?m)^__version__.*\n?", "", text)
    assert mutated != text and "__version__" not in mutated
    init_file.write_text(mutated, encoding="utf-8")

    result = _run_parity_script(script, repo)
    output = result.stdout + result.stderr
    assert result.returncode != 0
    assert "__version__ nao encontrada" in output, output
