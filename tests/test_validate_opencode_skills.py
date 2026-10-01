"""Testes reais do validador de skills globais (sem mocks)."""

from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "validate_opencode_skills.py"


class TestValidateOpencodeSkills(unittest.TestCase):
    def test_script_exists(self) -> None:
        self.assertTrue(SCRIPT.is_file())

    def test_validator_passes_on_disk(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        self.assertEqual(
            proc.returncode,
            0,
            msg=f"stdout={proc.stdout!r} stderr={proc.stderr!r}",
        )
        self.assertIn("global", proc.stdout)
        self.assertIn("7 intra", proc.stdout)

    def test_frontmatter_parser_accepts_folded_description(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location("vok", SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        fm = mod.parse_frontmatter(
            "---\nname: demo-skill\ndescription: >-\n  line one\n  line two\n---\nbody\n"
        )
        self.assertEqual(fm["name"], "demo-skill")
        self.assertIn("line one", fm["description"])

    def test_frontmatter_parser_rejects_missing_frontmatter(self) -> None:
        """Caminho de erro real: YAML sem frontmatter levanta ValueError."""
        import importlib.util

        spec = importlib.util.spec_from_file_location("vok", SCRIPT)
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with self.assertRaises(ValueError):
            mod.parse_frontmatter("texto qualquer sem frontmatter")


if __name__ == "__main__":
    unittest.main()
