"""Guarda estática da política anti-mock (AGENTS.md §5.2).

Proíbe `unittest.mock`, `MagicMock`, `AsyncMock`, `@patch` e `from mock import`
no código de produção (`src/`) e na LÓGICA dos testes (`tests/`), depois de
remover docstrings e strings de fixture — o scrubber do audit_mocks.py e as
premissas negativas do golden set citam esses padrões propositalmente dentro
de strings, o que é permitido.
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).parent.parent
SRC_DIR = ROOT / "src"
TESTS_DIR = ROOT / "tests"

STRING_LITERAL = re.compile(r"""("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""")
TRIPLE_QUOTED = re.compile(
    r'"""(?:[^"]|"(?!""))*"""|\'\'\'(?:[^\']|\'(?!\'))*\'\'\'', re.DOTALL
)

FORBIDDEN_PATTERNS = [
    re.compile(r"\bunittest\.mock\b"),
    re.compile(r"\bMagicMock\b"),
    re.compile(r"\bAsyncMock\b"),
    re.compile(r"\bfrom\s+mock\s+import\b"),
    re.compile(r"^\s*@patch\s*\("),
]


def _code_only(path: Path) -> list:
    """Retorna (lineno, linha) sem docstrings nem strings literais."""
    source = TRIPLE_QUOTED.sub('""', path.read_text(encoding="utf-8"))
    out = []
    for lineno, line in enumerate(source.splitlines(), 1):
        code = STRING_LITERAL.sub('""', line)
        if code.strip() and not code.strip().startswith("#"):
            out.append((lineno, code))
    return out


def _find_violations(directory: Path):
    violations = []
    for path in sorted(directory.rglob("*.py")):
        for lineno, code in _code_only(path):
            for pat in FORBIDDEN_PATTERNS:
                if pat.search(code):
                    rel = path.relative_to(ROOT)
                    violations.append(f"{rel}:{lineno}: {code.strip()}")
    return violations


class TestNoMocksInSrc(unittest.TestCase):

    def test_src_zero_mocks(self):
        """Produção não pode conter nenhum mock/stub de framework."""
        violations = _find_violations(SRC_DIR)
        self.assertEqual(
            violations, [],
            "Mock detectado em src/ (§5.2 proíbe mocks em produção):\n"
            + "\n".join(violations),
        )

    def test_tests_logic_zero_mocks(self):
        """Lógica dos testes não pode usar mock — apenas strings de fixture."""
        violations = _find_violations(TESTS_DIR)
        self.assertEqual(
            violations, [],
            "Mock usado na lógica de teste (§5.2 exige dependências reais):\n"
            + "\n".join(violations),
        )

    def test_scripts_and_benchmarks_zero_mocks(self):
        """§5.2 é repo-wide: scripts/ e benchmarks/ também ficam sob o guard."""
        for sub in ("scripts", "benchmarks"):
            directory = ROOT / sub
            if not directory.is_dir():
                continue
            violations = _find_violations(directory)
            self.assertEqual(
                violations, [],
                f"Mock detectado em {sub}/ (§5.2):\n" + "\n".join(violations),
            )


if __name__ == "__main__":
    unittest.main(verbosity=2)
