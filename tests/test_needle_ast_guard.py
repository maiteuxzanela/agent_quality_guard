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


class TestNeedleASTGuardMCPIntegration(unittest.IsolatedAsyncioTestCase):
    """Garante que o openjev_score_and_diagnose no MCP Server reprova fail-fast."""

    async def test_mcp_fails_fast_on_sqli_with_zero_inference(self):
        import subprocess
        import tempfile
        from src.mcp_server import openjev_score_and_diagnose

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

            # Executa avaliação no workspace real
            resp = await openjev_score_and_diagnose(
                premise="Pedido do Prompt: busca de usuarios. | Resultado Executado: criado src/users.py com busca.",
                workspace=tmp_dir,
                task_type="entrega",
                threshold=3.40,
            )

            # Asserções determinísticas de fail-fast
            self.assertEqual(resp["score"], 1.0)
            self.assertFalse(resp["passed"])
            self.assertTrue(resp["ast_blocked"])
            self.assertIsNotNone(resp["diagnosis"])
            self.assertEqual(resp["diagnosis"]["selected"], "seguranca")
            self.assertEqual(resp["diagnosis"]["confidence"], 1.0)
            self.assertTrue(len(resp["ast_violations"]) >= 1)
            self.assertEqual(resp["ast_violations"][0]["rule_id"], "sql_injection")


if __name__ == "__main__":
    unittest.main()

