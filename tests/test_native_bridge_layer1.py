"""Suíte determinística da Camada 1 (bridge nativa com compiladores/linters).

TICKET-HARNESS-43 (frente A2 — Tales). Sem mocks: os parsers são exercitados com
saídas sintéticas no formato REAL de cada ferramenta e a execução end-to-end roda
os binários reais disponíveis no ``.venv`` (ruff/mypy) e no PATH (eslint).

Cobre:
* parsers tsc / eslint / biome / oxlint / ruff / mypy / go vet / clippy / dart;
* mapeamento de severidade (error x warning) e de regras canônicas;
* tolerância a ruído em torno do payload JSON;
* detecção do linter JS configurado (eslint > biome > oxlint);
* ``run_native_bridge`` end-to-end com ruff + mypy reais;
* orçamento sub-segundo, clamp do env e fail-open (ferramenta ausente/lenta);
* timeout com ``killpg`` sem deixar processos órfãos (processo real que dorme);
* merge com deduplicação no resultado base do AST.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

import pytest

from agent_quality_guard.orchestrator.multi_stack_runner import MultiStackRunner, NativeToolRun

JEV_ROOT = Path(__file__).resolve().parents[1]
JEV_VENV = JEV_ROOT / ".venv"


# ──────────────────────────────────────────────────────────────────────────────
# Parsers: tsc / mypy / go vet / dart (texto) e eslint/biome/oxlint/ruff/clippy (JSON)
# ──────────────────────────────────────────────────────────────────────────────

def test_parse_tsc_output_line_and_global(tmp_path: Path):
    output = (
        "a.ts(2,5): error TS2322: Type 'string' is not assignable to 'number'.\n"
        "b.ts(9,1): warning TS6133: 'x' is declared but never used.\n"
        "error TS18003: No inputs were found in config file.\n"
    )
    violations = MultiStackRunner._parse_tsc_output(tmp_path, output)
    by_rule = {v.rule_id: v for v in violations}

    assert by_rule["TSC_TS2322"].category == "type_error"
    assert by_rule["TSC_TS2322"].file_path == "a.ts"
    assert by_rule["TSC_TS2322"].line_number == 2
    assert by_rule["TSC_TS6133"].category == "lint_warning"
    # diagnóstico global sem arquivo cai em tsconfig.json
    assert by_rule["TSC_TS18003"].file_path == "tsconfig.json"
    assert by_rule["TSC_TS18003"].line_number == 1
    assert by_rule["TSC_TS2322"].suggestion is not None


def test_parse_mypy_output_codes_and_notes_ignored(tmp_path: Path):
    output = (
        "pkg/a.py:3: error: Incompatible types  [assignment]\n"
        "pkg/b.py:7:5: warning: unused ignore  [unused-ignore]\n"
        "pkg/c.py:1: note: revealed type is 'int'\n"
        "pkg/d.py:2: error: missing annotation\n"
    )
    by_rule = {v.rule_id: v for v in MultiStackRunner._parse_mypy_output(tmp_path, output)}

    assert by_rule["MYPY_ASSIGNMENT"].category == "type_error"
    assert by_rule["MYPY_ASSIGNMENT"].line_number == 3
    assert by_rule["MYPY_UNUSED_IGNORE"].category == "lint_warning"
    # sem code `[x]` o fallback é MYPY_TYPE
    assert "MYPY_TYPE" in by_rule
    assert all(v.message != "revealed type is 'int'" for v in by_rule.values())


def test_parse_go_vet_output(tmp_path: Path):
    violations = MultiStackRunner._parse_go_vet_output(
        tmp_path, "internal/a.go:12:3: bad call to fmt.Printf\nsvc/b.go:4: unreachable code\n"
    )
    assert [(v.rule_id, v.category, v.file_path, v.line_number) for v in violations] == [
        ("GOVET_DIAGNOSTIC", "lint_error", "internal/a.go", 12),
        ("GOVET_DIAGNOSTIC", "lint_error", "svc/b.go", 4),
    ]


def test_parse_dart_analyze_machine_format(tmp_path: Path):
    violations = MultiStackRunner._parse_dart_analyze_output(
        tmp_path,
        "ERROR|COMPILE|X|lib/a.dart|1|2|3|missing semicolon\n"
        "WARNING|LINT|unused_import|lib/b.dart|5|1|1|unused import\n"
        "linha invalida sem pipes\n",
    )
    assert [(v.rule_id, v.category, v.line_number) for v in violations] == [
        ("DART_X", "lint_error", 1),
        ("DART_UNUSED_IMPORT", "lint_warning", 5),
    ]


def test_parse_eslint_json_rules_and_parse_error(tmp_path: Path):
    payload = [
        {
            "filePath": str(tmp_path / "src" / "a.jsx"),
            "messages": [
                {"ruleId": "react-hooks/rules-of-hooks", "severity": 2, "line": 3, "message": "hook condicional"},
                {"ruleId": "react-hooks/exhaustive-deps", "severity": 1, "line": 7, "message": "deps"},
                {"ruleId": None, "severity": 2, "line": 1, "message": "Parsing error: unexpected token"},
                {"ruleId": "alguma/regra-nova", "severity": 2, "line": 9, "message": "x"},
            ],
        }
    ]
    violations = MultiStackRunner._parse_eslint_json(tmp_path, json.dumps(payload))
    by_rule = {v.rule_id: v for v in violations}

    assert by_rule["ESLINT_RULES_OF_HOOKS"].category == "lint_error"
    assert by_rule["ESLINT_HOOK_DEPS"].category == "lint_warning"
    assert by_rule["ESLINT_PARSE_ERROR"].category == "lint_error"
    # ruleId desconhecido virá slugificado
    assert "ESLINT_ALGUMA_REGRA_NOVA" in by_rule


def test_parse_biome_json_exhaustive_dependencies(tmp_path: Path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.tsx").write_text("// lin1\n// lin2\nconst x = 1;\n", encoding="utf-8")
    payload = {
        "diagnostics": [
            {
                "category": "lint/correctness/useExhaustiveDependencies",
                "severity": "error",
                "description": "deps ausentes",
                "location": {"path": {"file": "src/a.tsx"}, "span": [9]},
            }
        ]
    }
    violations = MultiStackRunner._parse_biome_json(tmp_path, json.dumps(payload))
    assert len(violations) == 1
    violation = violations[0]
    assert violation.rule_id == "BIOME_HOOK_DEPS"
    assert violation.category == "lint_error"
    # offset de byte 9 cai na 2ª linha do arquivo real
    assert violation.line_number == 2
    assert violation.code_snippet == "// lin2"


def test_parse_oxlint_json_span_line(tmp_path: Path):
    payload = {
        "diagnostics": [
            {
                "code": "no-unused-vars",
                "severity": "warning",
                "filename": str(tmp_path / "a.js"),
                "labels": [{"span": {"line": 4}}],
                "message": "unused",
            }
        ]
    }
    violations = MultiStackRunner._parse_oxlint_json(tmp_path, json.dumps(payload))
    assert [(v.rule_id, v.category, v.line_number) for v in violations] == [
        ("OXLINT_NO_UNUSED_VARS", "lint_warning", 4),
    ]


def test_parse_ruff_json_blocking_classification(tmp_path: Path):
    payload = [
        {"code": "F401", "filename": str(tmp_path / "a.py"), "location": {"row": 1}, "message": "unused"},
        {"code": "E501", "filename": str(tmp_path / "a.py"), "location": {"row": 2}, "message": "long"},
        {"code": "E902", "filename": str(tmp_path / "a.py"), "location": {"row": 1}, "message": "syntax"},
    ]
    by_rule = {v.rule_id: v for v in MultiStackRunner._parse_ruff_json(tmp_path, json.dumps(payload))}
    assert by_rule["RUFF_F401"].category == "lint_error"
    assert by_rule["RUFF_E501"].category == "lint_warning"
    # E9* (erro de sintaxe) também bloqueia
    assert by_rule["RUFF_E902"].category == "lint_error"


def test_ruff_blocking_helper():
    assert MultiStackRunner._is_ruff_blocking("F401") is True
    assert MultiStackRunner._is_ruff_blocking("E902") is True
    assert MultiStackRunner._is_ruff_blocking("E501") is False
    assert MultiStackRunner._is_ruff_blocking("W291") is False


def test_parse_cargo_json_clippy_line_delimited(tmp_path: Path):
    stream = (
        '{"reason":"compiler-artifact","message":{}}\n'
        '{"reason":"compiler-message","message":{"level":"warning",'
        '"code":{"code":"clippy::needless_return"},"message":"unneeded return",'
        '"spans":[{"is_primary":true,"file_name":"src/lib.rs","line_start":4}]}}\n'
        'lixo nao-json\n'
        '{"reason":"compiler-message","message":{"level":"error",'
        '"code":{"code":"E0308"},"message":"mismatched types",'
        '"spans":[{"is_primary":true,"file_name":"src/lib.rs","line_start":9}]}}\n'
    )
    violations = MultiStackRunner._parse_cargo_json(tmp_path, stream)
    assert [(v.rule_id, v.category, v.line_number) for v in violations] == [
        ("CLIPPY_CLIPPY_NEEDLESS_RETURN", "lint_warning", 4),
        ("CLIPPY_E0308", "lint_error", 9),
    ]


def test_load_json_payload_tolerates_noise():
    assert MultiStackRunner._load_json_payload("[]", is_array=True) == []
    assert MultiStackRunner._load_json_payload('log line\n[{"a": 1}]', is_array=True) == [{"a": 1}]
    assert MultiStackRunner._load_json_payload('warn\n{"diagnostics": []}') == {"diagnostics": []}
    assert MultiStackRunner._load_json_payload("sem json") is None
    assert MultiStackRunner._load_json_payload("") is None
    assert MultiStackRunner._load_json_payload("{}") == {}


def test_slugify_rule_normalizes():
    assert MultiStackRunner._slugify_rule("react-hooks/rules-of-hooks") == "REACT_HOOKS_RULES_OF_HOOKS"
    assert MultiStackRunner._slugify_rule("clippy::needless_return") == "CLIPPY_NEEDLESS_RETURN"


def test_invalid_payload_and_shape_never_raise(tmp_path: Path):
    for output in ("", "not-json", "null", "[1,2,3]", '{"x": 1}'):
        assert MultiStackRunner._parse_eslint_json(tmp_path, output) == []
        assert MultiStackRunner._parse_biome_json(tmp_path, output) == []
        assert MultiStackRunner._parse_oxlint_json(tmp_path, output) == []
        assert MultiStackRunner._parse_ruff_json(tmp_path, output) == []
    assert MultiStackRunner._parse_tsc_output(tmp_path, "linha aleatória") == []
    assert MultiStackRunner._parse_mypy_output(tmp_path, "linha aleatória") == []


# ──────────────────────────────────────────────────────────────────────────────
# Detecção do linter JS
# ──────────────────────────────────────────────────────────────────────────────

def test_detect_js_linter_requires_config(tmp_path: Path):
    if not (shutil_which := _which("eslint")):
        pytest.skip("eslint ausente no ambiente")
    (tmp_path / "package.json").write_text('{"name": "app"}', encoding="utf-8")
    assert MultiStackRunner._detect_js_linter(tmp_path) is None

    (tmp_path / ".eslintrc.json").write_text("{}", encoding="utf-8")
    assert MultiStackRunner._detect_js_linter(tmp_path) == ("eslint", shutil_which)


def test_detect_js_linter_reads_eslint_config_from_package_json(tmp_path: Path):
    eslint_bin = _which("eslint")
    if not eslint_bin:
        pytest.skip("eslint ausente no ambiente")
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "app", "eslintConfig": {"rules": {}}}), encoding="utf-8"
    )
    assert MultiStackRunner._detect_js_linter(tmp_path) == ("eslint", eslint_bin)


def _which(name: str) -> str | None:
    import shutil as _shutil

    return _shutil.which(name)


# ──────────────────────────────────────────────────────────────────────────────
# Orçamento / env / fail-open
# ──────────────────────────────────────────────────────────────────────────────

def test_native_budget_default_and_clamp(monkeypatch):
    monkeypatch.delenv("JEV_NATIVE_BRIDGE_BUDGET_SECONDS", raising=False)
    assert MultiStackRunner._native_budget_seconds() == 0.8

    monkeypatch.setenv("JEV_NATIVE_BRIDGE_BUDGET_SECONDS", "0.01")
    assert MultiStackRunner._native_budget_seconds() == 0.1

    monkeypatch.setenv("JEV_NATIVE_BRIDGE_BUDGET_SECONDS", "99")
    assert MultiStackRunner._native_budget_seconds() == 5.0

    monkeypatch.setenv("JEV_NATIVE_BRIDGE_BUDGET_SECONDS", "valor-invalido")
    assert MultiStackRunner._native_budget_seconds() == 0.8


def test_run_native_bridge_empty_workspace_fail_open(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JEV_NATIVE_BRIDGE_BUDGET_SECONDS", "0.5")
    started = time.monotonic()
    result = MultiStackRunner.run_native_bridge(tmp_path, "PYTHON_JEV", audit_mode="full")
    elapsed = time.monotonic() - started

    assert set(result) >= {"blocking", "warnings", "tools", "total_files", "engine", "duration"}
    assert result["blocking"] == [] and result["warnings"] == []
    assert result["total_files"] == 0
    assert result["engine"] == "native_toolchain_bridge"
    assert elapsed < 1.0, f"bridge sem arquivos levou {elapsed:.3f}s"


def test_run_native_bridge_budget_zero_skips_spawns(tmp_path: Path, monkeypatch):
    """Orçamento mínimo: nenhuma ferramenta é spawnada e nada bloqueia (fail-open)."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("import os\n", encoding="utf-8")
    monkeypatch.setenv("JEV_NATIVE_BRIDGE_BUDGET_SECONDS", "0.1")

    result = MultiStackRunner.run_native_bridge(
        tmp_path, "PYTHON_JEV", audit_mode="full", source_files=[tmp_path / "src" / "a.py"]
    )
    # ruff (rápido) pode executar; mypy precisa ser pulado por orçamento.
    statuses = {tool["tool"]: tool["status"] for tool in result["tools"]}
    assert statuses.get("mypy") == "skipped"
    assert result["blocking"] == [] or all(
        v["rule_id"].startswith("RUFF_") for v in result["blocking"]
    )


# ──────────────────────────────────────────────────────────────────────────────
# Execução real: ruff + mypy do venv do projeto
# ──────────────────────────────────────────────────────────────────────────────

def _link_project_venv(workspace: Path) -> None:
    if not (JEV_VENV / "bin" / "ruff").exists():
        pytest.skip("ruff ausente no .venv do projeto")
    os.symlink(JEV_VENV, workspace / ".venv")


def test_run_native_bridge_end_to_end_ruff_and_mypy(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("JEV_NATIVE_BRIDGE_BUDGET_SECONDS", "5.0")
    _link_project_venv(tmp_path)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "app.py").write_text("import os\n\n\ndef f(x):\n    return x + 1\n", encoding="utf-8")
    (tmp_path / "src" / "types_bad.py").write_text("def g(x: int) -> str:\n    return x\n", encoding="utf-8")

    result = MultiStackRunner.run_native_bridge(
        tmp_path,
        "PYTHON_JEV",
        audit_mode="full",
        source_files=[tmp_path / "src" / "app.py", tmp_path / "src" / "types_bad.py"],
    )

    statuses = {tool["tool"]: tool for tool in result["tools"]}
    assert statuses["ruff"]["status"] == "executed"
    assert statuses["ruff"]["returncode"] != 0  # ruff retorna 1 quando encontra achados
    assert statuses["mypy"]["status"] == "executed"

    rules = {v["rule_id"] for v in result["blocking"]}
    assert "RUFF_F401" in rules, result["blocking"]
    assert "MYPY_RETURN_VALUE" in rules, result["blocking"]
    f401 = next(v for v in result["blocking"] if v["rule_id"] == "RUFF_F401")
    assert f401["file_path"] == "src/app.py"
    assert f401["code_snippet"] == "import os"
    assert f401["category"] == "lint_error"


def test_run_native_bridge_eslint_end_to_end(tmp_path: Path, monkeypatch):
    eslint_bin = _which("eslint")
    if not eslint_bin:
        pytest.skip("eslint ausente no ambiente")
    monkeypatch.setenv("JEV_NATIVE_BRIDGE_BUDGET_SECONDS", "2.0")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.js").write_text(
        "var unused = 1;\nfunction f(){ return undefinedVar; }\n", encoding="utf-8"
    )
    # eslint 6.x não conhece env es2021; usa es6 (formato suportado pelo binário real)
    (tmp_path / ".eslintrc.json").write_text(
        json.dumps({"env": {"es6": True, "node": True}, "rules": {"no-unused-vars": "error", "no-undef": "warn"}}),
        encoding="utf-8",
    )

    result = MultiStackRunner.run_native_bridge(
        tmp_path, "NODE_TS_BUN", audit_mode="full", source_files=[tmp_path / "src" / "a.js"]
    )
    statuses = {tool["tool"]: tool["status"] for tool in result["tools"]}
    assert statuses.get("eslint") == "executed", result["tools"]

    blocking = {v["rule_id"] for v in result["blocking"]}
    warnings = {v["rule_id"] for v in result["warnings"]}
    assert "ESLINT_NO_UNUSED_VARS" in blocking
    assert "ESLINT_NO_UNDEF" in warnings


# ──────────────────────────────────────────────────────────────────────────────
# Timeout + killpg: nenhum processo órfão
# ──────────────────────────────────────────────────────────────────────────────

def test_execute_native_tool_timeout_kills_process_group_without_orphans(tmp_path: Path):
    marker = f"jev_orphan_{uuid.uuid4().hex}"
    runs: list[NativeToolRun] = []
    # Processo real que dorme com um argv[0] único para rastreio (sem mock).
    cmd = ["sh", "-c", f"exec -a {marker} sleep 300 & sleep 300"]

    output = MultiStackRunner._execute_native_tool(
        "sleeper", cmd, tmp_path, os.environ.copy(), time.monotonic() + 0.4, runs
    )

    assert output is None
    assert runs[-1].status == "timeout"
    assert runs[-1].returncode == -9
    assert "killpg" in (runs[-1].detail or "")

    time.sleep(0.3)
    probe = subprocess.run(["pgrep", "-f", marker], capture_output=True, text=True)
    assert probe.returncode == 1, f"processos órfãos sobreviveram: {probe.stdout!r}"


def test_kill_process_group_leaves_no_group(tmp_path: Path):
    process = subprocess.Popen(
        ["sh", "-c", "sleep 300 & sleep 300"], start_new_session=True
    )
    pgid = process.pid
    MultiStackRunner._kill_process_group(process)
    process.wait(timeout=5)
    time.sleep(0.2)
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_execute_native_tool_skips_when_budget_exhausted(tmp_path: Path):
    runs: list[NativeToolRun] = []
    output = MultiStackRunner._execute_native_tool(
        "noop", ["true"], tmp_path, {}, time.monotonic() - 1.0, runs
    )
    assert output is None
    assert runs[-1].status == "skipped"


def test_execute_native_tool_missing_binary_is_error_not_crash(tmp_path: Path):
    runs: list[NativeToolRun] = []
    output = MultiStackRunner._execute_native_tool(
        "ghost", ["binario-inexistente-jev-xyz"], tmp_path, os.environ.copy(), time.monotonic() + 1.0, runs
    )
    assert output is None
    assert runs[-1].status == "error"


def test_execute_native_tool_returns_output_for_real_command(tmp_path: Path):
    runs: list[NativeToolRun] = []
    output = MultiStackRunner._execute_native_tool(
        "echoer", ["sh", "-c", "echo ola-jev"], tmp_path, os.environ.copy(), time.monotonic() + 2.0, runs
    )
    assert output is not None and "ola-jev" in output
    assert runs[-1].status == "executed" and runs[-1].returncode == 0


# ──────────────────────────────────────────────────────────────────────────────
# Merge / dedup com o resultado base do AST
# ──────────────────────────────────────────────────────────────────────────────

def test_merge_native_bridge_dedups_and_marks_engine():
    base = {
        "blocking": [{"rule_id": "RUFF_F401", "file_path": "src/a.py", "line_number": 1}],
        "warnings": [],
        "engine": "node_ast",
    }
    native = {
        "blocking": [
            {"rule_id": "RUFF_F401", "file_path": "src/a.py", "line_number": 1},  # duplicado
            {"rule_id": "TSC_TS2322", "file_path": "src/a.ts", "line_number": 3},
        ],
        "warnings": [{"rule_id": "ESLINT_NO_UNDEF", "file_path": "src/a.js", "line_number": 2}],
        "tools": [{"tool": "ruff", "status": "executed"}],
        "duration": 0.42,
    }
    merged = MultiStackRunner._merge_native_bridge(base, native)

    assert [v["rule_id"] for v in merged["blocking"]] == ["RUFF_F401", "TSC_TS2322"]
    assert [v["rule_id"] for v in merged["warnings"]] == ["ESLINT_NO_UNDEF"]
    assert merged["engine"] == "node_ast+native_toolchain_bridge"
    assert merged["native_bridge_duration"] == 0.42
    assert merged["native_tools"] == native["tools"]


def test_merge_native_bridge_none_is_noop():
    base = {"blocking": [], "warnings": []}
    assert MultiStackRunner._merge_native_bridge(base, None) is base


def test_merge_native_bridge_no_executed_tool_keeps_engine():
    base = {"blocking": [], "warnings": [], "engine": "node_ast"}
    merged = MultiStackRunner._merge_native_bridge(
        base, {"blocking": [], "warnings": [], "tools": [{"tool": "ruff", "status": "skipped"}]}
    )
    assert merged["engine"] == "node_ast"


def test_dedup_native_violations_by_rule_file_line_message():
    from agent_quality_guard.orchestrator.multi_stack_runner import MultiStackViolation

    dup = MultiStackViolation("R", "c", "a.py", 1, "s", "m")
    other = MultiStackViolation("R", "c", "a.py", 1, "s", "m2")
    unique = MultiStackRunner._dedup_native_violations([dup, dup, other])
    assert unique == [dup, other]


def test_run_native_bridge_guarded_never_raises(tmp_path: Path):
    result = MultiStackRunner._run_native_bridge_guarded(tmp_path, "UNKNOWN", "full", [])
    assert isinstance(result, dict)
    assert result["blocking"] == []


# ──────────────────────────────────────────────────────────────────────────────
# Interface com o quality gate: _run_layer1_native_bridge
# ──────────────────────────────────────────────────────────────────────────────

def test_layer1_helper_is_fail_open_and_fast(tmp_path: Path, monkeypatch):
    from agent_quality_guard.mcp_server import _run_layer1_native_bridge

    monkeypatch.setenv("JEV_NATIVE_BRIDGE_BUDGET_SECONDS", "0.5")
    started = time.monotonic()
    result = _run_layer1_native_bridge(tmp_path, "PYTHON_JEV", "full")
    elapsed = time.monotonic() - started

    assert set(result) >= {"available", "blocking", "warnings", "tools", "total_files"}
    assert result["available"] is True
    assert result["blocking"] == []
    assert elapsed < 1.0


def _build_js_workspace_with_config(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.js").write_text("var unused = 1;\n", encoding="utf-8")
    (tmp_path / ".eslintrc.json").write_text(
        json.dumps({"env": {"es6": True}, "rules": {"no-unused-vars": "error"}}), encoding="utf-8"
    )
    return tmp_path


def test_layer1_helper_feeds_quality_guard_toolchain(tmp_path: Path, monkeypatch):
    if not _which("eslint"):
        pytest.skip("eslint ausente no ambiente")
    from agent_quality_guard.mcp_server import _run_layer1_native_bridge

    monkeypatch.setenv("JEV_NATIVE_BRIDGE_BUDGET_SECONDS", "2.0")
    workspace = _build_js_workspace_with_config(tmp_path)

    result = _run_layer1_native_bridge(workspace, "NODE_TS_BUN", "full")
    assert result["available"] is True
    tool_names = {tool["name"] for tool in result["tools"]}
    assert "eslint" in tool_names
    assert any(v["rule_id"] == "ESLINT_NO_UNUSED_VARS" for v in result["blocking"])
