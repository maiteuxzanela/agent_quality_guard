"""Suíte determinística da Camada 2 (meta-regras universais de AST via Tree-Sitter).

TICKET-HARNESS-43 (frente B1 — Maya). Sem mocks: exercita a API REAL
``TreeSitterGuard.scan_content`` / ``scan_file`` / ``scan_workspace_report``
sobre conteúdo e arquivos reais, usando as gramáticas tree-sitter instaladas no
``.venv`` (conteúdo em memória, sem I/O de rede/GPU).

Cobre:
* classificação de severidade/categoria das regras;
* anti-mock restrito a produção (tests/ isento);
* floating promise (JS e corrotina Python sem await);
* return assíncrono sem await dentro de try;
* vazamento de listener/timer e open() Python sem context manager;
* catch/except silencioso;
* SQL por template, concatenação, f-string, .format e operador %;
* eval/exec/new Function e command injection;
* supressões needle-ignore / needle-ignore-next-line / needle-ignore-file / ALL;
* fallback lexical para gramática ausente (c_sharp) e extensão desconhecida;
* deduplicação e relatório agregado de workspace;
* ausência de imports/telemetria de GPU e execução determinística veloz (< 1s).
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from agent_quality_guard.memory.tree_sitter_guard import TreeSitterGuard, TreeSitterViolation


def _rules(violations) -> list[str]:
    return [v.rule_id for v in violations]


def _first(violations, rule_id: str) -> TreeSitterViolation:
    for violation in violations:
        if violation.rule_id == rule_id:
            return violation
    raise AssertionError(f"regra {rule_id} ausente em {_rules(violations)}")


# ──────────────────────────────────────────────────────────────────────────────
# Contrato de severidade / categoria
# ──────────────────────────────────────────────────────────────────────────────

def test_blocking_and_warning_rule_classification():
    blocking = {
        "PROD_MOCK", "SQL_INJECTION", "DANGEROUS_EVAL", "SECURITY_EXEC", "COMMAND_INJECTION",
    }
    warnings = {
        "FLOATING_PROMISE", "ASYNC_RETURN_NO_AWAIT", "RESOURCE_LEAK", "SILENT_CATCH",
    }
    for rule_id in blocking:
        assert TreeSitterGuard.is_blocking_rule(rule_id) is True
        assert TreeSitterGuard.RULE_CATEGORIES[rule_id] in {"anti_mock_producao", "security"}
    for rule_id in warnings:
        assert TreeSitterGuard.is_blocking_rule(rule_id) is False
        assert TreeSitterGuard.RULE_CATEGORIES[rule_id] == "structural"


def test_violation_to_dict_contract():
    violation = TreeSitterViolation(
        rule_id="SQL_INJECTION", category="security", file_path="src/a.py",
        line_number=7, message="msg", code_snippet="q", language="python",
    )
    payload = violation.to_dict()
    for field in (
        "rule_id", "category", "file_path", "line_number", "code_snippet",
        "snippet", "message", "suggestion", "severity", "language",
    ):
        assert field in payload, f"campo {field} ausente em to_dict()"
    assert payload["severity"] == "blocking"
    assert payload["code_snippet"] == payload["snippet"] == "q"
    assert payload["line_number"] == 7

    # snippet e code_snippet permanecem espelhados em qualquer direção
    mirrored = TreeSitterViolation("R", "security", "a.py", 1, "m", snippet="s").to_dict()
    assert mirrored["code_snippet"] == "s"
    assert mirrored["severity"] == "warning"


def test_severity_property_matches_is_blocking():
    assert TreeSitterViolation("DANGEROUS_EVAL", "security", "a.js", 1, "m").severity == "blocking"
    assert TreeSitterViolation("SILENT_CATCH", "structural", "a.js", 1, "m").severity == "warning"


# ──────────────────────────────────────────────────────────────────────────────
# Anti-mock (PROD_MOCK) — só em produção
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "content,path",
    [
        ("import pytest\n", "src/a.py"),
        ("from unittest.mock import patch\n", "src/a.py"),
        ("from faker import Faker\n", "src/a.py"),
        ('import { vi } from "vitest"\n', "src/a.ts"),
        ('import sinon from "sinon";\n', "src/a.js"),
        ("const x = jest.fn();\n", "src/a.js"),
        ("const mockFetch = () => {};\n", "src/a.js"),
        ("def f():\n    return MagicMock()\n", "src/a.py"),
    ],
)
def test_prod_mock_detected_in_production(content: str, path: str):
    violations = TreeSitterGuard.scan_content(content, path)
    assert "PROD_MOCK" in _rules(violations), content
    assert _first(violations, "PROD_MOCK").severity == "blocking"


@pytest.mark.parametrize(
    "path",
    ["tests/test_a.py", "tests/conftest.py", "src/__tests__/a.ts", "mocks/x.js", "e2e/a.spec.ts"],
)
def test_prod_mock_is_exempt_in_tests(path: str):
    violations = TreeSitterGuard.scan_content("from unittest.mock import MagicMock\n", path)
    assert violations == [], f"caminho de teste {path} não pode gerar PROD_MOCK"


def test_fastapi_responses_import_is_not_prod_mock():
    """``from fastapi.responses import ...`` não é import de framework de mock.

    Regressão: a palavra ``responses`` precedida por ponto pertence a um módulo
    legítimo (``fastapi.responses``) e não pode ser confundida com a biblioteca
    de mock ``responses``.
    """
    violations = TreeSitterGuard.scan_content(
        "from fastapi.responses import HTMLResponse, JSONResponse\n", "src/a.py"
    )
    assert "PROD_MOCK" not in _rules(violations)


def test_bare_mock_library_imports_still_blocked():
    for source in (
        "import responses\n",
        "from unittest.mock import MagicMock\n",
        "import mock\n",
    ):
        violations = TreeSitterGuard.scan_content(source, "src/a.py")
        assert "PROD_MOCK" in _rules(violations), source


def test_is_production_path_and_test_file_classification():
    assert TreeSitterGuard.is_production_path("src/service.py") is True
    assert TreeSitterGuard.is_production_path("scripts/tool.py") is False
    assert TreeSitterGuard.is_production_path("examples/demo.js") is False
    assert TreeSitterGuard.is_test_file("tests/test_a.py") is True
    assert TreeSitterGuard.is_test_file("src/a_test.py") is True
    assert TreeSitterGuard.is_test_file("src/a.py") is False


# ──────────────────────────────────────────────────────────────────────────────
# Floating promise / async return sem await
# ──────────────────────────────────────────────────────────────────────────────

def test_js_floating_promise_detected_and_treated_variants_clean():
    leaky = TreeSitterGuard.scan_content("async function f(){\n  fetch('/x');\n}\n", "src/a.js")
    assert "FLOATING_PROMISE" in _rules(leaky)
    assert _first(leaky, "FLOATING_PROMISE").severity == "warning"

    awaited = TreeSitterGuard.scan_content("async function f(){\n  await fetch('/x');\n}\n", "src/a.js")
    assert _rules(awaited) == []

    chained = TreeSitterGuard.scan_content("function f(){\n  fetch('/x').then(r=>r).catch(()=>{});\n}\n", "src/a.js")
    assert _rules(chained) == []


def test_python_coroutine_without_await_is_floating_promise():
    content = "async def g():\n    pass\n\nasync def f():\n    g()\n"
    floating = TreeSitterGuard.scan_content(content, "src/a.py")
    assert "FLOATING_PROMISE" in _rules(floating)

    awaited = TreeSitterGuard.scan_content(
        "async def g():\n    pass\n\nasync def f():\n    await g()\n", "src/a.py"
    )
    assert _rules(awaited) == []


def test_async_return_without_await_inside_try():
    leaky = TreeSitterGuard.scan_content(
        "async function f(){\n  try {\n    return fetch('/x');\n  } catch (e) { console.error(e); }\n}\n",
        "src/a.js",
    )
    assert "ASYNC_RETURN_NO_AWAIT" in _rules(leaky)

    fixed = TreeSitterGuard.scan_content(
        "async function f(){\n  try {\n    return await fetch('/x');\n  } catch (e) { console.error(e); }\n}\n",
        "src/a.js",
    )
    assert _rules(fixed) == []


# ──────────────────────────────────────────────────────────────────────────────
# Vazamento de recursos
# ──────────────────────────────────────────────────────────────────────────────

def test_listener_leak_detected_and_cleanup_silences():
    leaky = TreeSitterGuard.scan_content(
        "function f(){\n  el.addEventListener('click', h);\n}\n", "src/a.js"
    )
    assert "RESOURCE_LEAK" in _rules(leaky)

    cleaned = TreeSitterGuard.scan_content(
        "function f(){\n  el.addEventListener('click', h);\n  el.removeEventListener('click', h);\n}\n",
        "src/a.js",
    )
    assert "RESOURCE_LEAK" not in _rules(cleaned)


def test_timer_leak_detected_and_clear_silences():
    leaky = TreeSitterGuard.scan_content(
        "function f(){\n  setInterval(()=>go(), 10);\n}\n", "src/a.js"
    )
    assert "RESOURCE_LEAK" in _rules(leaky)

    cleaned = TreeSitterGuard.scan_content(
        "function f(){\n  const t = setInterval(()=>go(), 10);\n  clearInterval(t);\n}\n",
        "src/a.js",
    )
    assert "RESOURCE_LEAK" not in _rules(cleaned)


def test_python_open_outside_context_manager_is_resource_leak():
    leaky = TreeSitterGuard.scan_content("def f(p):\n    fh = open(p)\n    return fh.read()\n", "src/a.py")
    assert "RESOURCE_LEAK" in _rules(leaky)

    with_stmt = TreeSitterGuard.scan_content(
        "def f(p):\n    with open(p) as fh:\n        return fh.read()\n", "src/a.py"
    )
    assert "RESOURCE_LEAK" not in _rules(with_stmt)


# ──────────────────────────────────────────────────────────────────────────────
# Silent catch
# ──────────────────────────────────────────────────────────────────────────────

def test_js_empty_catch_is_silent_catch():
    violations = TreeSitterGuard.scan_content(
        "function f(){\n  try { go(); } catch (e) {}\n}\n", "src/a.js"
    )
    assert "SILENT_CATCH" in _rules(violations)


def test_python_except_pass_is_silent_catch():
    violations = TreeSitterGuard.scan_content(
        "def f():\n    try:\n        go()\n    except Exception:\n        pass\n", "src/a.py"
    )
    assert "SILENT_CATCH" in _rules(violations)


def test_handled_exception_is_not_silent_catch():
    js = TreeSitterGuard.scan_content(
        "function f(){\n  try { go(); } catch (e) { console.error(e); }\n}\n", "src/a.js"
    )
    assert "SILENT_CATCH" not in _rules(js)
    py = TreeSitterGuard.scan_content(
        "import logging\ndef f():\n    try:\n        go()\n    except Exception:\n        logging.exception('x')\n",
        "src/a.py",
    )
    assert "SILENT_CATCH" not in _rules(py)


# ──────────────────────────────────────────────────────────────────────────────
# SQL Injection
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "content,path",
    [
        ("function f(uid){\n  db.query(`SELECT * FROM t WHERE id = ${uid}`);\n}\n", "src/a.js"),
        ("function f(uid){\n  db.query('SELECT * FROM t WHERE id = ' + uid);\n}\n", "src/a.js"),
        ('def f(c, uid):\n    c.execute(f"SELECT * FROM t WHERE id = {uid}")\n', "src/a.py"),
        ('def f(c, uid):\n    c.execute("SELECT * FROM t WHERE id = " + uid)\n', "src/a.py"),
        ('def f(c, uid):\n    c.execute("SELECT * FROM t WHERE id = {}".format(uid))\n', "src/a.py"),
        ('def f(c, uid):\n    c.execute("SELECT * FROM t WHERE id = %s" % uid)\n', "src/a.py"),
    ],
)
def test_sql_injection_detected_blocking(content: str, path: str):
    violations = TreeSitterGuard.scan_content(content, path)
    assert "SQL_INJECTION" in _rules(violations), content
    assert _first(violations, "SQL_INJECTION").severity == "blocking"


def test_parameterized_sql_is_clean():
    py = TreeSitterGuard.scan_content(
        'def f(c, uid):\n    c.execute("SELECT * FROM t WHERE id = %s", (uid,))\n', "src/a.py"
    )
    assert "SQL_INJECTION" not in _rules(py)
    js = TreeSitterGuard.scan_content(
        "function f(uid){\n  db.query('SELECT * FROM t WHERE id = ?', [uid]);\n}\n", "src/a.js"
    )
    assert "SQL_INJECTION" not in _rules(js)


# ──────────────────────────────────────────────────────────────────────────────
# eval / exec / new Function / command injection
# ──────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "content,path",
    [
        ("function f(x){ eval(x); }\n", "src/a.js"),
        ("def f(x):\n    return eval(x)\n", "src/a.py"),
        ("def f(src):\n    exec(src)\n", "src/a.py"),
    ],
)
def test_dangerous_eval_and_exec_blocking(content: str, path: str):
    violations = TreeSitterGuard.scan_content(content, path)
    assert "DANGEROUS_EVAL" in _rules(violations), content
    assert _first(violations, "DANGEROUS_EVAL").severity == "blocking"


def test_python_method_eval_is_not_dangerous_eval():
    """``self.model.eval()``/``model.eval()`` (PyTorch) não são eval dinâmico."""
    for source in (
        "def f(self):\n    self.model.eval()\n",
        "def f(model):\n    model.eval()\n",
        "def f(expr):\n    return expr.eval()\n",
    ):
        violations = TreeSitterGuard.scan_content(source, "src/a.py")
        assert "DANGEROUS_EVAL" not in _rules(violations), source


def test_python_global_eval_and_exec_are_still_blocking():
    for source in (
        "def f(x):\n    return eval(x)\n",
        'def f():\n    return eval("1+1")\n',
        "def f(x):\n    return builtins.eval(x)\n",
        "def f(src):\n    exec(src)\n",
        "def f(src):\n    return builtins.exec(src)\n",
    ):
        violations = TreeSitterGuard.scan_content(source, "src/a.py")
        assert "DANGEROUS_EVAL" in _rules(violations), source


@pytest.mark.parametrize(
    "path,source",
    [
        ("src/a.java", "class A { void f(Model m){ m.eval(); } }\n"),
        ("src/a.go", "package main\nfunc f(m Model){ m.eval() }\n"),
        ("src/a.rs", "fn f(m: Model) { m.eval(); }\n"),
    ],
)
def test_generic_language_method_eval_is_not_dangerous_eval(path: str, source: str):
    """O loop genérico só bloqueia o chamável global ``eval``/``exec``.

    Regressão: ``base in {'eval', 'exec'}`` bloqueava qualquer ``obj.eval()``
    em Java/Go/Rust, embora seja um método de objeto, não eval dinâmico.
    """
    violations = TreeSitterGuard.scan_content(source, path)
    assert "DANGEROUS_EVAL" not in _rules(violations), source


@pytest.mark.parametrize(
    "path,source",
    [
        ("src/a.java", "class A { void f(x){ eval(x); } }\n"),
        ("src/a.go", "package main\nfunc f(x T){ eval(x) }\n"),
        ("src/a.rs", "fn f(x: T) { eval(x); }\n"),
    ],
)
def test_generic_language_global_eval_is_still_blocking(path: str, source: str):
    violations = TreeSitterGuard.scan_content(source, path)
    assert "DANGEROUS_EVAL" in _rules(violations), source


def test_new_function_is_security_exec_blocking():
    violations = TreeSitterGuard.scan_content(
        "function f(body){\n  return new Function(body);\n}\n", "src/a.js"
    )
    assert "SECURITY_EXEC" in _rules(violations)
    assert _first(violations, "SECURITY_EXEC").severity == "blocking"


def test_js_child_process_dynamic_argument_is_command_injection():
    dynamic = TreeSitterGuard.scan_content(
        "const cp = require('child_process');\nfunction f(input){ cp.exec(input); }\n", "src/a.js"
    )
    assert "COMMAND_INJECTION" in _rules(dynamic)
    assert _first(dynamic, "COMMAND_INJECTION").severity == "blocking"

    literal = TreeSitterGuard.scan_content(
        "const cp = require('child_process');\nfunction f(){ cp.exec('ls -la'); }\n", "src/a.js"
    )
    assert "COMMAND_INJECTION" not in _rules(literal)


def test_python_os_system_is_command_injection():
    violations = TreeSitterGuard.scan_content(
        "import os\ndef f(cmd):\n    os.system(cmd)\n", "src/a.py"
    )
    assert "COMMAND_INJECTION" in _rules(violations)


def test_python_subprocess_shell_true_with_dynamic_command_is_command_injection():
    """Contrato declarado: subprocess com shell=True + comando dinâmico bloqueia.

    ``_check_python_call`` promete detectar shell injection em ``subprocess.*``
    (docstring: "Detecta eval/exec, mocks, SQL por .format, open() sem with e shell").
    """
    violations = TreeSitterGuard.scan_content(
        "import subprocess\ndef f(cmd):\n    subprocess.run(cmd, shell=True)\n", "src/a.py"
    )
    assert "COMMAND_INJECTION" in _rules(violations), (
        "subprocess.run(cmd, shell=True) com comando dinâmico NÃO foi detectado: "
        f"regras={_rules(violations)}"
    )


def test_python_subprocess_shell_true_literal_command_is_clean():
    violations = TreeSitterGuard.scan_content(
        "import subprocess\ndef f():\n    subprocess.run('ls -la', shell=True)\n", "src/a.py"
    )
    assert "COMMAND_INJECTION" not in _rules(violations)


# ──────────────────────────────────────────────────────────────────────────────
# Supressões
# ──────────────────────────────────────────────────────────────────────────────

def test_inline_needle_ignore_suppresses_rule():
    violations = TreeSitterGuard.scan_content(
        "function f(x){\n  eval(x); // needle-ignore: DANGEROUS_EVAL\n}\n", "src/a.js"
    )
    assert violations == []


def test_needle_ignore_next_line_suppresses_rule():
    violations = TreeSitterGuard.scan_content(
        "function f(x){\n  // needle-ignore-next-line: DANGEROUS_EVAL\n  eval(x);\n}\n", "src/a.js"
    )
    assert violations == []


def test_needle_ignore_file_suppresses_rule():
    violations = TreeSitterGuard.scan_content(
        "// needle-ignore-file: DANGEROUS_EVAL\nfunction f(x){ eval(x); }\n", "src/a.js"
    )
    assert violations == []


def test_needle_ignore_all_suppresses_everything():
    violations = TreeSitterGuard.scan_content(
        "# needle-ignore-all\ndef f(x):\n    return eval(x)\n", "src/a.py"
    )
    assert violations == []


def test_alias_suppression_crosses_layers():
    """needle-ignore de SECURITY_EVAL (Camada 1) suprime DANGEROUS_EVAL (Camada 2)."""
    violations = TreeSitterGuard.scan_content(
        "function f(x){\n  eval(x); // needle-ignore: SECURITY_EVAL\n}\n", "src/a.js"
    )
    assert violations == []
    py = TreeSitterGuard.scan_content(
        "def f(x):\n    return eval(x)  # needle-ignore: SECURITY_EVAL\n", "src/a.py"
    )
    assert py == []


def test_suppression_does_not_hide_other_rules():
    violations = TreeSitterGuard.scan_content(
        "function f(x){\n  eval(x); // needle-ignore: SQL_INJECTION\n}\n", "src/a.js"
    )
    assert _rules(violations) == ["DANGEROUS_EVAL"]


# ──────────────────────────────────────────────────────────────────────────────
# Fallback lexical / gramática ausente
# ──────────────────────────────────────────────────────────────────────────────

def _force_missing_c_sharp(monkeypatch) -> None:
    """Torna a gramática de C# indisponível de forma determinística.

    O ``tree_sitter_language_pack`` pode manter a gramática de C# em cache no
    disco da máquina; aqui patchamos o seam ``get_parser`` para devolver ``None``
    apenas para ``c_sharp``, sem depender do cache real do host e sem tocar nas
    demais linguagens.
    """
    original_get_parser = TreeSitterGuard.get_parser

    @classmethod
    def _no_c_sharp(cls, language):
        if language == "c_sharp":
            return None
        return original_get_parser(language)

    monkeypatch.setattr(TreeSitterGuard, "get_parser", _no_c_sharp)


def test_c_sharp_grammar_absent_falls_back_to_lexical(monkeypatch):
    _force_missing_c_sharp(monkeypatch)
    assert TreeSitterGuard.is_language_available("c_sharp") is False
    assert TreeSitterGuard.language_for_file("src/a.cs") == "c_sharp"
    violations = TreeSitterGuard.scan_content(
        "class A { void F(string x){ var y = eval(x); } }\n", "src/a.cs"
    )
    assert "DANGEROUS_EVAL" in _rules(violations)


def test_unknown_extension_uses_lexical_scan():
    violations = TreeSitterGuard.scan_content("var y = eval(x);\n", "src/a.zzz")
    assert "DANGEROUS_EVAL" in _rules(violations)


def test_empty_content_returns_empty():
    assert TreeSitterGuard.scan_content("", "src/a.py") == []
    assert TreeSitterGuard.scan_content("   \n\t", "src/a.js") == []


def test_scan_duplicate_rule_same_line_is_deduplicated():
    violations = TreeSitterGuard.scan_content(
        "function f(x){ eval(x); eval(x); }\n", "src/a.js"
    )
    keys = [(v.rule_id, v.line_number) for v in violations]
    assert len(keys) == len(set(keys)), f"violações duplicadas: {keys}"


def test_scan_file_missing_and_oversized(monkeypatch, tmp_path: Path):
    import agent_quality_guard.memory.tree_sitter_guard as guard_module

    assert TreeSitterGuard.scan_file(tmp_path / "nao_existe.py") == []

    big = tmp_path / "big.py"
    big.write_text("x = 1\n", encoding="utf-8")
    # Seam determinístico: rebaixa o teto de bytes para exercitar o guard real
    # de arquivo gigante sem materializar um arquivo de 2 MB.
    monkeypatch.setattr(guard_module, "MAX_FILE_BYTES", 1)
    assert TreeSitterGuard.scan_file(big) == []


# ──────────────────────────────────────────────────────────────────────────────
# Varredura de workspace + relatório
# ──────────────────────────────────────────────────────────────────────────────

def _build_workspace(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "tests").mkdir(parents=True)
    (root / "node_modules").mkdir(parents=True)
    (root / "src" / "a.py").write_text(
        "import unittest.mock\ndef f(x):\n    return eval(x)\n", encoding="utf-8"
    )
    (root / "src" / "b.js").write_text("async function f(){\n  fetch('/x');\n}\n", encoding="utf-8")
    (root / "tests" / "test_a.py").write_text(
        "from unittest.mock import MagicMock\n", encoding="utf-8"
    )
    (root / "node_modules" / "c.js").write_text("eval(x);\n", encoding="utf-8")


def test_scan_workspace_report_structure_and_exemptions(tmp_path: Path):
    _build_workspace(tmp_path)
    report = TreeSitterGuard.scan_workspace_report(str(tmp_path), audit_mode="full")

    assert set(report) >= {"blocking", "warnings", "total_files", "engine", "languages"}
    # node_modules é podado; tests/ existe mas não gera PROD_MOCK
    assert report["total_files"] == 3
    blocking_rules = {v["rule_id"] for v in report["blocking"]}
    assert "PROD_MOCK" in blocking_rules
    assert "DANGEROUS_EVAL" in blocking_rules
    assert {v["file_path"] for v in report["blocking"]} == {"src/a.py"}
    assert [v["rule_id"] for v in report["warnings"]] == ["FLOATING_PROMISE"]
    assert "javascript" in report["languages"] and "python" in report["languages"]
    assert report["engine"] == "tree_sitter"


def test_scan_workspace_report_scope_limits_files(tmp_path: Path):
    _build_workspace(tmp_path)
    scoped = TreeSitterGuard.scan_workspace_report(str(tmp_path), audit_mode="full", scope="src")
    assert scoped["total_files"] == 2
    assert {v["file_path"] for v in scoped["blocking"]} == {"src/a.py"}


def test_scan_workspace_report_engine_fallback_when_no_grammar(monkeypatch, tmp_path: Path):
    _force_missing_c_sharp(monkeypatch)
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.cs").write_text(
        "class A { void F(string x){ var y = eval(x); } }\n", encoding="utf-8"
    )
    report = TreeSitterGuard.scan_workspace_report(str(tmp_path), audit_mode="full")
    assert report["engine"] == "tree_sitter_lexical_fallback"
    assert any(v["rule_id"] == "DANGEROUS_EVAL" for v in report["blocking"])


def test_scan_workspace_report_missing_workspace_is_empty():
    report = TreeSitterGuard.scan_workspace_report("/caminho/que/nao/existe-xyz", audit_mode="full")
    assert report == {
        "blocking": [], "warnings": [], "total_files": 0,
        "engine": "tree_sitter", "languages": [],
    }


def test_scan_workspace_list_matches_report(tmp_path: Path):
    _build_workspace(tmp_path)
    listed = TreeSitterGuard.scan_workspace(str(tmp_path), audit_mode="full")
    report = TreeSitterGuard.scan_workspace_report(str(tmp_path), audit_mode="full")
    assert len(listed) == len(report["blocking"]) + len(report["warnings"])
    assert set(_rules(listed)) == {v["rule_id"] for v in report["blocking"] + report["warnings"]}


def test_scan_workspace_delta_falls_back_to_all_files_without_git(tmp_path: Path):
    """Contrato do docstring: em falha do diff, devolve TODOS os arquivos.

    Sem repositório Git o ``get_workspace_diff_files`` devolve lista vazia sem
    levantar exceção; hoje ``audit_mode='delta'`` retorna [] e a Camada 2 é
    silenciosamente ignorada (a Camada 1, em ``MultiStackRunner._filter_delta_files``,
    cai para todos os arquivos no mesmo cenário).
    """
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.js").write_text("function f(x){ eval(x); }\n", encoding="utf-8")

    full = _rules(TreeSitterGuard.scan_workspace(str(tmp_path), audit_mode="full"))
    delta = _rules(TreeSitterGuard.scan_workspace(str(tmp_path), audit_mode="delta"))
    assert "DANGEROUS_EVAL" in full
    assert "DANGEROUS_EVAL" in delta, (
        "delta em workspace sem Git ignorou todos os arquivos (sem fallback): "
        f"full={full} delta={delta}"
    )


# ──────────────────────────────────────────────────────────────────────────────
# Ausência de silent catch no runner multi-stack (regressão do escape amplo)
# ──────────────────────────────────────────────────────────────────────────────

def test_multi_stack_runner_has_no_silent_catch():
    """``multi_stack_runner.py`` não pode conter catch/except silencioso.

    Regressão: ``_tree_sitter_relpath`` engolia qualquer exceção com
    ``except Exception: pass``. Escaneamos o arquivo REAL com o próprio guard
    (sem mocks, conteúdo lido do disco) e exigimos zero ``SILENT_CATCH``.
    """
    source_path = (
        Path(__file__).resolve().parents[1]
        / "agent_quality_guard"
        / "orchestrator"
        / "multi_stack_runner.py"
    )
    assert source_path.is_file(), f"arquivo ausente: {source_path}"
    text = source_path.read_text(encoding="utf-8")
    violations = TreeSitterGuard.scan_content(
        text, "agent_quality_guard/orchestrator/multi_stack_runner.py"
    )
    silent = [v for v in violations if v.rule_id == "SILENT_CATCH"]
    assert silent == [], [(v.line_number, v.message) for v in silent]


def test_silent_catch_detector_flags_the_original_escape():
    """Sensibilidade: o detector continua pegando o padrão original aspirado.

    Sem esta âncora, o teste acima poderia passar por vacuidade caso a regra
    ``SILENT_CATCH`` deixasse de reconhecer ``except Exception: pass``.
    """
    violations = TreeSitterGuard.scan_content(
        "def f():\n    try:\n        go()\n    except Exception:\n        pass\n",
        "src/a.py",
    )
    assert "SILENT_CATCH" in _rules(violations)


# ──────────────────────────────────────────────────────────────────────────────
# Determinismo, velocidade e ausência de GPU/Laya
# ──────────────────────────────────────────────────────────────────────────────

def test_scan_is_deterministic_repeated_runs():
    content = (
        "import unittest.mock\n"
        "async function f(){\n"
        "  fetch('/x');\n"
        "}\n"
    )
    first = [(v.rule_id, v.line_number) for v in TreeSitterGuard.scan_content(content, "src/a.js")]
    for _ in range(5):
        assert [(v.rule_id, v.line_number) for v in TreeSitterGuard.scan_content(content, "src/a.js")] == first


def test_scan_is_subsecond_fast():
    content = "async function f(){\n  fetch('/x');\n}\n" * 50
    started = time.monotonic()
    for _ in range(20):
        TreeSitterGuard.scan_content(content, "src/a.js")
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, f"20 varreduras levaram {elapsed:.3f}s (contrato < 1s)"


def test_module_has_no_gpu_or_ml_inference_dependency():
    source = Path(__file__).resolve().parents[1].joinpath("agent_quality_guard", "memory", "tree_sitter_guard.py")
    text = source.read_text(encoding="utf-8").lower()
    for forbidden in ("torch", "cuda", "llama_cpp", "transformers", "noul", "laya"):
        assert forbidden not in text, f"Camada 2 não pode referenciar {forbidden} (sem GPU/ML)"
