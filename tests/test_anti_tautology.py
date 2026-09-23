"""Guarda anti-tautologia da suíte (responde ao diagnóstico teste_tautologico).

Varre os arquivos de teste reais por padrões de asserção tautológica
(`assert True`, `assert 1 == 1`, `assertTrue(True)`, ...) depois de remover
strings de fixture, e exige zero ocorrências. Qualquer teste que "sempre
passa" por asserção trivial derruba esta suite.
"""

import re
import unittest
from pathlib import Path

TESTS_DIR = Path(__file__).parent

STRING_LITERAL = re.compile(r"""("(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')""")
TRIPLE_QUOTED = re.compile(r'"""(?:[^"]|"(?!""))*"""|\'\'\'(?:[^\']|\'(?!\'))*\'\'\'', re.DOTALL)

TAUTOLOGY_PATTERNS = [
    re.compile(r"assert\s+True\b"),
    re.compile(r"assert\s+False\b"),
    re.compile(r"assert\s+1\s*==\s*1\b"),
    re.compile(r"assert\s+0\s*==\s*0\b"),
    re.compile(r"assertTrue\s*\(\s*True\s*\)"),
    re.compile(r"assertIsNotNone\s*\(\s*None\s*\)"),
    re.compile(r"""assert\s+["'][^"']*["']\s*==\s*["']"""),
]


def _strip_strings(line: str) -> str:
    return STRING_LITERAL.sub('""', line)


class TestSuiteAntiTautologia(unittest.TestCase):

    def test_nenhum_assert_tautologico_na_suit(self):
        violacoes = []
        for path in sorted(TESTS_DIR.glob("test_*.py")):
            source = TRIPLE_QUOTED.sub('""', path.read_text(encoding="utf-8"))
            for lineno, line in enumerate(source.splitlines(), 1):
                code_line = _strip_strings(line).strip()
                if not code_line or code_line.startswith("#"):
                    continue
                for pat in TAUTOLOGY_PATTERNS:
                    if pat.search(code_line):
                        violacoes.append(f"{path.name}:{lineno}: {code_line}")
        self.assertEqual(
            violacoes, [],
            "Asserções tautológicas encontradas (teste que sempre passa):\n"
            + "\n".join(violacoes),
        )

    def test_golden_set_mantem_casos_negativos_reais(self):
        """Sem premissas negativas no golden set, os testes positivos virariam
        aprovação circular (sempre-verdadeiro). Cobertura mínima exigida."""
        from tests.test_score_calibration import (
            NEGATIVE_CASES,
            ORDER_PAIRS,
            POSITIVE_CASES,
            STABILITY_PAIRS,
        )
        self.assertGreaterEqual(len(POSITIVE_CASES), 4)
        self.assertGreaterEqual(len(NEGATIVE_CASES), 4)
        self.assertGreaterEqual(len(ORDER_PAIRS), 2)
        self.assertGreaterEqual(len(STABILITY_PAIRS), 2)
        nomes_pos = {name for name, _ in POSITIVE_CASES}
        nomes_neg = {name for name, _ in NEGATIVE_CASES}
        self.assertTrue(nomes_pos.isdisjoint(nomes_neg))


if __name__ == "__main__":
    unittest.main(verbosity=2)
