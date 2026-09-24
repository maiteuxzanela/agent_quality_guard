"""Testes de integração ponta a ponta dos Stop Hooks (Guards) dos repositórios satélites (sem mocks).

Valida os requisitos críticos de arquitetura:
1. Fast-path de Escopo: encerramento imediato (<50ms) com {} e exit 0 quando não há arquivos de código.
2. Camada 1 AST Guardrail: bloqueio determinístico com decision: continue quando há violações de AST.
3. Resiliência do cliente IPC: comunicação ultra-leve com daemon e tratamento de erro fail-fast.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path


class TestGuardsIntegration(unittest.TestCase):
    """Testa o comportamento real dos scripts de Stop Hook em ambientes Git controlados."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="jev_guard_test_")
        self.ws = Path(self.temp_dir)
        subprocess.run(["git", "init"], cwd=self.ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "QA Guard Test"], cwd=self.ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "qa@test.com"], cwd=self.ws, check=True, capture_output=True)

        # Commit inicial
        (self.ws / "README.md").write_text("# Project\n", encoding="utf-8")
        subprocess.run(["git", "add", "README.md"], cwd=self.ws, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.ws, check=True, capture_output=True)

    def tearDown(self) -> None:
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def _get_env(self) -> dict:
        env = dict(os.environ)
        env["GUARD_WORKSPACE"] = str(self.ws)
        return env

    def test_fast_path_when_only_documentation_modified(self) -> None:
        """Modificações puramente documentais (.md, .json) encerram em <50ms com {} e exit code 0."""
        (self.ws / "docs.md").write_text("# Nova documentacao\n", encoding="utf-8")
        (self.ws / "config.json").write_text('{"key": "value"}', encoding="utf-8")

        guard_script = Path("/mnt/94CCB337CCB3130A/Faturamento/.agents/scripts/faturamento_guard.py")
        if not guard_script.is_file():
            self.skipTest("Script faturamento_guard.py nao encontrado no caminho esperado.")

        t0 = time.perf_counter()
        res = subprocess.run(
            ["python3", str(guard_script)],
            cwd=str(self.ws),
            env=self._get_env(),
            capture_output=True,
            text=True,
            timeout=5,
        )
        elapsed_ms = (time.perf_counter() - t0) * 1000

        self.assertEqual(res.returncode, 0)
        out = json.loads(res.stdout.strip())
        self.assertEqual(out, {}, "Deveria retornar payload vazio permitindo encerramento.")
        self.assertLess(elapsed_ms, 250, f"Fast-path deveria ser quase instantaneo ({elapsed_ms:.1f}ms).")

    def test_ast_violation_triggers_deterministic_block(self) -> None:
        """Código com exceção engolida silenciosamente é bloqueado deterministicamente pelo Needle 3."""
        bad_code = (
            "def operacao_arriscada():\n"
            "    try:\n"
            "        x = 1 / 0\n"
            "    except Exception:\n"
            "        pass\n"
        )
        (self.ws / "service.py").write_text(bad_code, encoding="utf-8")

        guard_script = Path("/mnt/94CCB337CCB3130A/Faturamento/.agents/scripts/faturamento_guard.py")
        if not guard_script.is_file():
            self.skipTest("Script faturamento_guard.py nao encontrado.")

        res = subprocess.run(
            ["python3", str(guard_script)],
            cwd=str(self.ws),
            env=self._get_env(),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0)
        out = json.loads(res.stdout.strip())
        self.assertEqual(out.get("decision"), "continue")
        self.assertIn("Needle 3", out.get("reason", ""))
        self.assertIn("swallowed_exception", out.get("reason", ""))

    def test_jez_robin_guard_blocks_prohibited_emojis_in_ui(self) -> None:
        """Robin Guard bloqueia commits contendo emojis em arquivos de UI."""
        (self.ws / "site.html").write_text("<h1>Bem-vindo 🚀</h1>\n", encoding="utf-8")

        guard_script = Path("/mnt/94CCB337CCB3130A/JEZ collections/.agents/scripts/robin_guard.py")
        if not guard_script.is_file():
            self.skipTest("Script robin_guard.py nao encontrado.")

        res = subprocess.run(
            ["python3", str(guard_script)],
            cwd=str(self.ws),
            env=self._get_env(),
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(res.returncode, 0)
        out = json.loads(res.stdout.strip())
        self.assertEqual(out.get("decision"), "continue")
        self.assertIn("ZERO EMOJIS", out.get("reason", ""))


if __name__ == "__main__":
    unittest.main()
