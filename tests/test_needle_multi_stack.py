"""Testes unitários para as quatro melhorias estruturais do Needle MultiStack e AST Guard:
1. Varredura por Delta / Git Diff (audit_mode="delta" vs "full")
2. Anotações de Exceção e Supressão (needle-ignore / needle-ignore-next-line)
3. AST Semântica Real em JavaScript/TypeScript (XSS_INNER_HTML, sanitizadores, etc.)
4. Diagnósticos Acionáveis com Sugestões Canônicas (suggestion preenchida)
"""

import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from src.memory.needle_ast_guard import NeedleASTGuard, ASTViolation
from src.orchestrator.multi_stack_runner import (
    MultiStackRunner,
    MultiStackViolation,
    RULE_SUGGESTIONS,
    _is_line_suppressed,
)


class TestNeedleMultiStack(unittest.TestCase):
    """Garante a integridade do pipeline multi-stack e das 4 melhorias de Alex."""

    def test_line_suppression_inline_and_next_line(self):
        """Testa supressão inline, na linha anterior e isolamento."""
        lines = [
            "const a = 1; // needle-ignore: XSS_INNER_HTML",
            "const b = 2;",
            "// needle-ignore-next-line: HARDCODED_SECRET",
            "const c = 3;",
            "# needle-ignore: SQL_INJECTION",
            "const d = 4;",
        ]
        # Linha 1 suprimida inline
        self.assertTrue(_is_line_suppressed("XSS_INNER_HTML", 1, lines))
        # Linha 2 NÃO suprimida por inline da linha 1
        self.assertFalse(_is_line_suppressed("XSS_INNER_HTML", 2, lines))
        # Linha 4 suprimida por needle-ignore-next-line na linha 3
        self.assertTrue(_is_line_suppressed("HARDCODED_SECRET", 4, lines))
        # Linha 6 suprimida por comentário isolado needle-ignore na linha 5
        self.assertTrue(_is_line_suppressed("SQL_INJECTION", 6, lines))

    def test_python_needle_ignore_suppression(self):
        """Testa se NeedleASTGuard respeita # needle-ignore e anotação ALL."""
        code_suppressed = (
            "def query_user(uid, db):\n"
            "    # needle-ignore: SQL_INJECTION\n"
            "    return db.execute(f\"SELECT * FROM users WHERE id = {uid}\")\n"
        )
        violations = NeedleASTGuard.scan_content(code_suppressed, "src/dao.py")
        self.assertEqual(len(violations), 0)

        code_inline = (
            "def query_user(uid, db):\n"
            "    return db.execute(f\"SELECT * FROM users WHERE id = {uid}\")  # needle-ignore: SQL_INJECTION\n"
        )
        violations_inline = NeedleASTGuard.scan_content(code_inline, "src/dao.py")
        self.assertEqual(len(violations_inline), 0)

        code_unsuppressed = (
            "def query_user(uid, db):\n"
            "    return db.execute(f\"SELECT * FROM users WHERE id = {uid}\")\n"
        )
        violations_real = NeedleASTGuard.scan_content(code_unsuppressed, "src/dao.py")
        self.assertEqual(len(violations_real), 1)
        self.assertIsNotNone(violations_real[0].suggestion)
        self.assertIn("placeholders", violations_real[0].suggestion)

    def test_semantic_js_ast_sanitization(self):
        """Verifica se AST semântica distingue innerHTML com escapeHtml de innerHTML vulnerável."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = Path(tmp_dir)
            site_dir = ws / "site" / "js"
            site_dir.mkdir(parents=True, exist_ok=True)

            # Arquivo com innerHTML seguro (usando escapeHtml)
            safe_js = (
                "function renderSafe(data) {\n"
                "  const el = document.getElementById('card');\n"
                "  el.innerHTML = `<h3>${escapeHtml(data.title)}</h3>`;\n"
                "}\n"
            )
            (site_dir / "safe.js").write_text(safe_js, encoding="utf-8")

            # Arquivo com innerHTML inseguro
            unsafe_js = (
                "function renderUnsafe(data) {\n"
                "  const el = document.getElementById('card');\n"
                "  el.innerHTML = `<h3>${data.rawUserInput}</h3>`;\n"
                "}\n"
            )
            (site_dir / "unsafe.js").write_text(unsafe_js, encoding="utf-8")

            res = MultiStackRunner.scan_js_ast(ws, "WEB_JS_FIREBASE", audit_mode="full")
            blocking = res.get("blocking", [])
            
            # Deve detectar apenas o unsafe.js
            blocking_files = [Path(b["file_path"]).name for b in blocking if b["rule_id"] == "XSS_INNER_HTML"]
            self.assertIn("unsafe.js", blocking_files)
            self.assertNotIn("safe.js", blocking_files)

    def test_js_ast_suppression_and_suggestions(self):
        """Verifica se supressão via // needle-ignore funciona em JS e sugestões são preenchidas."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = Path(tmp_dir)
            site_dir = ws / "site" / "js"
            site_dir.mkdir(parents=True, exist_ok=True)

            code = (
                "// needle-ignore: HARDCODED_SECRET\n"
                "const apiKey = 'AIzaSy123456789012345678901234567890123';\n"
                "function runDynamic() {\n"
                "  eval('1 + 1');\n"
                "}\n"
            )
            (site_dir / "test.js").write_text(code, encoding="utf-8")

            res = MultiStackRunner.scan_js_ast(ws, "WEB_JS_FIREBASE", audit_mode="full")
            blocking = res.get("blocking", [])
            rule_ids = [b["rule_id"] for b in blocking]

            # HARDCODED_SECRET foi suprimido
            self.assertNotIn("HARDCODED_SECRET", rule_ids)
            # eval não foi suprimido e deve conter sugestão
            self.assertIn("SECURITY_EVAL", rule_ids)
            eval_violation = next(b for b in blocking if b["rule_id"] == "SECURITY_EVAL")
            self.assertIsNotNone(eval_violation.get("suggestion"))
            self.assertIn("JSON.parse", eval_violation["suggestion"])

    def test_audit_mode_delta_vs_full_git(self):
        """Verifica se audit_mode='delta' restringe a varredura aos arquivos modificados no Git."""
        import subprocess
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = Path(tmp_dir)
            # Inicializa repo git
            subprocess.run(["git", "init"], cwd=str(ws), capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=str(ws), capture_output=True, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=str(ws), capture_output=True, check=True)

            site_dir = ws / "site" / "js"
            site_dir.mkdir(parents=True, exist_ok=True)

            # Arquivo legado com dívida técnica já comitado no histórico
            legacy_file = site_dir / "legacy_config.js"
            legacy_file.write_text("const oldKey = 'AIzaSy111111111111111111111111111111111';\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=str(ws), capture_output=True, check=True)
            subprocess.run(["git", "commit", "-m", "legacy"], cwd=str(ws), capture_output=True, check=True)

            # Novo arquivo da tarefa atual
            new_file = site_dir / "app.js"
            new_file.write_text("console.log('novo arquivo');\n", encoding="utf-8")

            # Em modo delta: apenas app.js deve ser varrido, legacy_config.js não deve entrar na varredura
            res_delta = MultiStackRunner.scan_js_ast(ws, "WEB_JS_FIREBASE", audit_mode="delta")
            blocking_delta = res_delta.get("blocking", [])
            delta_files = [b["file_path"] for b in blocking_delta]
            self.assertFalse(any("legacy_config.js" in f for f in delta_files))

            # Em modo full: legacy_config.js deve ser capturado
            res_full = MultiStackRunner.scan_js_ast(ws, "WEB_JS_FIREBASE", audit_mode="full")
            blocking_full = res_full.get("blocking", [])
            full_files = [b["file_path"] for b in blocking_full]
            self.assertTrue(any("legacy_config.js" in f for f in full_files))

    def test_error_handling_and_exceptions(self):
        """Verifica se tratamento de exceções e asserções de erro operam com segurança."""
        with self.assertRaises(TypeError):
            NeedleASTGuard.scan_content(None)  # type: ignore

    def test_file_level_suppressions_python_and_js(self):
        """Verifica diretivas globais de cabeçalho (# needle-ignore-file / // needle-ignore-all)."""
        # Python com needle-ignore-file
        py_code = (
            "# needle-ignore-file: SQL_INJECTION\n"
            "def query_all(db, val):\n"
            "    db.execute(f'SELECT * FROM tbl WHERE id = {val}')\n"
            "    db.execute(f'DELETE FROM tbl WHERE id = {val}')\n"
        )
        violations = NeedleASTGuard.scan_content(py_code, "src/service.py")
        self.assertEqual(len(violations), 0)

        # Python com needle-ignore-all
        py_all = (
            "# needle-ignore-all\n"
            "def query_all(db, val):\n"
            "    db.execute(f'SELECT * FROM tbl WHERE id = {val}')\n"
        )
        self.assertEqual(len(NeedleASTGuard.scan_content(py_all, "src/service.py")), 0)

        # JS com needle-ignore-file
        with tempfile.TemporaryDirectory() as tmp_dir:
            ws = Path(tmp_dir)
            js_dir = ws / "site" / "js"
            js_dir.mkdir(parents=True, exist_ok=True)
            js_file = js_dir / "eval_util.js"
            js_file.write_text(
                "// needle-ignore-file: SECURITY_EVAL\n"
                "function run(code) {\n"
                "  eval(code);\n"
                "}\n",
                encoding="utf-8"
            )
            res = MultiStackRunner.scan_js_ast(ws, "WEB_JS_FIREBASE", audit_mode="full")
            self.assertEqual(len(res.get("blocking", [])), 0)

    def test_ast_cache_functionality(self):
        """Verifica o cache thread-safe em memória de ASTs."""
        from src.memory.ast_cache import get_cached_ast, clear_ast_cache, get_ast_cache_size
        clear_ast_cache()
        self.assertEqual(get_ast_cache_size(), 0)

        code = "def foo(): return 42\n"
        tree1 = get_cached_ast(code, "foo.py")
        self.assertIsNotNone(tree1)
        self.assertEqual(get_ast_cache_size(), 1)

        tree2 = get_cached_ast(code, "/absolute/path/to/other.py")
        self.assertIs(tree1, tree2)
        self.assertEqual(get_ast_cache_size(), 1)

        # Código com erro de sintaxe retorna None
        invalid_tree = get_cached_ast("def syntax error (", "invalid.py")
        self.assertIsNone(invalid_tree)

        clear_ast_cache()
        self.assertEqual(get_ast_cache_size(), 0)

    def test_canonical_path_normalization(self):
        """Verifica normalização de caminhos no diff_inspector."""
        from src.core.diff_inspector import normalize_workspace_relpath
        ws = "/home/user/project"
        self.assertEqual(normalize_workspace_relpath("/home/user/project/src/a.py", ws), "src/a.py")
        self.assertEqual(normalize_workspace_relpath("./src/a.py", ws), "src/a.py")
        self.assertEqual(normalize_workspace_relpath("src\\a.py", ws), "src/a.py")
        self.assertEqual(normalize_workspace_relpath("src/a.py", ws), "src/a.py")

    def test_suite_budget_timeout(self):
        """Verifica orçamento de tempo limite em PytestChunkRunner."""
        from src.orchestrator.pytest_chunk_runner import PytestChunkRunner
        # Com orçamento já expirado (max_total_timeout=0), deve abortar de forma controlada
        res = PytestChunkRunner.run_all_chunks(workspace_path=".", max_total_timeout=0, audit_mode="delta")
        self.assertFalse(res["pytest_success"])
        self.assertIn("excedido", str(res["chunks"]))


if __name__ == "__main__":
    unittest.main()
