"""Testes unitários determinísticos do Needle 3 AST Guardrail (Zero Mocks)."""

import unittest
from pathlib import Path
from src.memory.needle_ast_guard import NeedleASTGuard, ASTViolation


class TestNeedleASTGuard(unittest.TestCase):
    """Garante precisão analítica 100% determinística sem uso de mocks."""

    def test_detect_sql_injection_fstring(self):
        code = (
            "def get_user(uid, db):\n"
            "    query = f\"SELECT id, name FROM users WHERE id = {uid}\"\n"
            "    return db.execute(query).fetchall()\n"
        )
        violations = NeedleASTGuard.scan_content(code, "src/users.py")
        self.assertTrue(len(violations) >= 1)
        v = violations[0]
        self.assertEqual(v.rule_id, "sql_injection")
        self.assertEqual(v.category, "seguranca")
        self.assertIn("SQL Injection", v.message)

    def test_detect_sql_injection_direct_call(self):
        code = (
            "def find_records(user_input, conn):\n"
            "    return conn.execute(f\"SELECT * FROM data WHERE key = {user_input}\")\n"
        )
        violations = NeedleASTGuard.scan_content(code, "src/search.py")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].category, "seguranca")

    def test_detect_sql_injection_concatenation(self):
        code = (
            "def run_sql(param, conn):\n"
            "    conn.execute(\"SELECT * FROM items WHERE cat = \" + param)\n"
        )
        violations = NeedleASTGuard.scan_content(code, "src/items.py")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule_id, "sql_injection")

    def test_safe_parameterized_query_passes(self):
        code = (
            "def get_user_safe(uid: int, db):\n"
            "    query = \"SELECT id, name FROM users WHERE id = ?\"\n"
            "    return db.execute(query, (uid,)).fetchall()\n"
        )
        violations = NeedleASTGuard.scan_content(code, "src/users.py")
        self.assertEqual(len(violations), 0)

    def test_detect_mock_in_production_import(self):
        code = (
            "from unittest.mock import MagicMock\n"
            "def cobrar(cartao, valor):\n"
            "    mock = MagicMock()\n"
            "    return {'status': 'pago'}\n"
        )
        violations = NeedleASTGuard.scan_content(code, "src/payment.py")
        self.assertTrue(any(v.rule_id == "mock_in_production" for v in violations))
        v = [v for v in violations if v.rule_id == "mock_in_production"][0]
        self.assertEqual(v.category, "mock_stub_em_producao")

    def test_mock_in_test_file_is_allowed(self):
        code = (
            "from unittest.mock import MagicMock\n"
            "def test_pagamento():\n"
            "    mock = MagicMock()\n"
            "    assert mock is not None\n"
        )
        violations = NeedleASTGuard.scan_content(code, "tests/test_payment.py")
        self.assertEqual(len(violations), 0)

    def test_detect_swallowed_exception(self):
        code = (
            "def execute_db_call():\n"
            "    try:\n"
            "        connect()\n"
            "    except:\n"
            "        pass\n"
        )
        violations = NeedleASTGuard.scan_content(code, "src/db.py")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule_id, "swallowed_exception")
        self.assertEqual(violations[0].category, "robustez_excecoes_recursos")

    def test_proper_exception_handling_passes(self):
        code = (
            "import logging\n"
            "def execute_db_call():\n"
            "    try:\n"
            "        connect()\n"
            "    except Exception as e:\n"
            "        logging.error(f'Falha ao conectar: {e}')\n"
            "        raise\n"
        )
        violations = NeedleASTGuard.scan_content(code, "src/db.py")
        self.assertEqual(len(violations), 0)

    def test_detect_tautological_test(self):
        code = (
            "def test_sistema():\n"
            "    assert True\n"
            "    assert 1 == 1\n"
            "    assert 'a' in 'abc'\n"
        )
        violations = NeedleASTGuard.scan_content(code, "tests/test_dummy.py")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule_id, "tautological_test")
        self.assertEqual(violations[0].category, "qualidade_de_testes")

    def test_real_test_passes(self):
        code = (
            "from src.calc import add\n"
            "def test_real_add():\n"
            "    result = add(2, 3)\n"
            "    assert result == 5\n"
        )
        violations = NeedleASTGuard.scan_content(code, "tests/test_calc.py")
        self.assertEqual(len(violations), 0)

    def test_detect_hardcoded_secrets(self):
        code = (
            "def setup_client():\n"
            "    API_KEY = 'sk-proj-1234567890abcdef1234567890'\n"
            "    PASSWORD = '123456'\n"
            "    return API_KEY\n"
        )
        violations = NeedleASTGuard.scan_content(code, "src/client.py")
        self.assertTrue(len(violations) >= 1)
        self.assertEqual(violations[0].rule_id, "hardcoded_secret")
        self.assertEqual(violations[0].category, "seguranca")
        # Garante que code_snippet está presente no objeto e no to_dict()
        v_dict = violations[0].to_dict()
        self.assertIn("code_snippet", v_dict)
        self.assertIn("rule_id", v_dict)
        self.assertIn("category", v_dict)
        self.assertIn("file_path", v_dict)
        self.assertIn("line_number", v_dict)
        self.assertIn("message", v_dict)

    def test_screener_suppressions(self):
        """Valida supressões inteligentes do NeedleASTScreener (pyi, dunder, @property, @abstractmethod)."""
        from src.memory.needle_defect_screener import NeedleASTScreener, AstSymbolSlice

        # Stub .pyi file
        stub_slice = AstSymbolSlice(
            file_path="src/types.pyi",
            symbol_name="Interface",
            symbol_type="class",
            start_line=1,
            end_line=5,
            content="class Interface:\n    def run(self) -> None: ...\n",
        )
        self.assertEqual(len(NeedleASTScreener.screen_symbol(stub_slice)), 0)

        # Decorators @property and @abstractmethod
        property_slice = AstSymbolSlice(
            file_path="src/models.py",
            symbol_name="value",
            symbol_type="method",
            start_line=1,
            end_line=5,
            content="@property\ndef value(self):\n    return self._val\n",
        )
        self.assertEqual(len(NeedleASTScreener.screen_symbol(property_slice)), 0)

    def test_detect_command_injection(self):
        code = (
            "import subprocess\n"
            "def run_user_cmd(cmd_input):\n"
            "    return subprocess.run(f'echo {cmd_input}', shell=True, capture_output=True)\n"
        )
        violations = NeedleASTGuard.scan_content(code, "src/terminal.py")
        self.assertTrue(len(violations) >= 1)
        self.assertEqual(violations[0].rule_id, "command_injection")
        self.assertEqual(violations[0].category, "seguranca")

    def test_detect_insecure_deserialization(self):
        code = (
            "import pickle\n"
            "def load_payload(raw_bytes):\n"
            "    return pickle.loads(raw_bytes)\n"
        )
        violations = NeedleASTGuard.scan_content(code, "src/serializer.py")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule_id, "insecure_deserialization")
        self.assertEqual(violations[0].category, "seguranca")

    def test_detect_dangerous_eval(self):
        code = (
            "def compute_calc(user_expr):\n"
            "    return eval(user_expr)\n"
        )
        violations = NeedleASTGuard.scan_content(code, "src/calculator.py")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule_id, "dangerous_eval_exec")
        self.assertEqual(violations[0].category, "seguranca")

    def test_detect_insecure_ssl_verify(self):
        code = (
            "import requests\n"
            "def fetch_data(url):\n"
            "    return requests.get(url, verify=False)\n"
        )
        violations = NeedleASTGuard.scan_content(code, "src/network.py")
        self.assertEqual(len(violations), 1)
        self.assertEqual(violations[0].rule_id, "insecure_ssl_verify")
        self.assertEqual(violations[0].category, "seguranca")


    def test_scan_content_fails_fast_on_non_string_source(self):
        """Caminho de erro real: fonte não-string estoura em vez de silenciar a varredura."""
        with self.assertRaises((TypeError, AttributeError)):
            NeedleASTGuard.scan_content(123, "src/bad.py")


class TestNeedleASTGuardMCPIntegration(unittest.IsolatedAsyncioTestCase):
    """Garante que a verificação de entrega no MCP Server reprova fail-fast em violação AST."""

    async def test_mcp_fails_fast_on_sqli_with_zero_inference(self):
        import subprocess
        import tempfile
        from src.mcp_server import needle_verify_delivery

        with tempfile.TemporaryDirectory() as tmp_dir:
            # Inicializa repo Git real
            subprocess.run(["git", "init"], cwd=tmp_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_dir, check=True)
            subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_dir, check=True)

            # Cria commit inicial
            readme = Path(tmp_dir) / "README.md"
            readme.write_text("# Project", encoding="utf-8")
            subprocess.run(["git", "add", "README.md"], cwd=tmp_dir, check=True)
            subprocess.run(["git", "commit", "-m", "initial"], cwd=tmp_dir, check=True)

            # Cria arquivo com vulnerabilidade de SQL Injection
            src_dir = Path(tmp_dir) / "src"
            src_dir.mkdir(parents=True, exist_ok=True)
            vuln_file = src_dir / "users.py"
            vuln_file.write_text(
                "def find_user(uid, db):\n"
                "    query = f\"SELECT * FROM users WHERE id = {uid}\"\n"
                "    return db.execute(query).fetchall()\n",
                encoding="utf-8",
            )

            # Suíte real mínima: a Etapa 0 exige testes coletáveis (workspace sem
            # testes é REJECTED antes da Camada 1 — ver test_workspace_without_tests_is_rejected)
            tests_dir = Path(tmp_dir) / "tests"
            tests_dir.mkdir(parents=True, exist_ok=True)
            (tests_dir / "test_users.py").write_text(
                "def test_users_smoke():\n    assert True\n",
                encoding="utf-8",
            )

            # Executa avaliação no workspace real
            resp = await needle_verify_delivery(
                workspace=tmp_dir,
                attempt_number=1,
            )

            # Asserções determinísticas de fail-fast
            self.assertFalse(resp["passed"])
            self.assertEqual(resp["status"], "REJECTED")
            self.assertTrue(len(resp["blocking_ast_violations"]) >= 1)
            self.assertEqual(resp["blocking_ast_violations"][0]["rule_id"], "sql_injection")


if __name__ == "__main__":
    unittest.main()

