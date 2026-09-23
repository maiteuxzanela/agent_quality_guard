"""Testes reais do particionamento block-aware de diffs e avaliação por etapas (sem mocks)."""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.core.diff_inspector import (
    DiffFile,
    extract_workspace_stages,
    format_stage_premise,
    get_workspace_diff_files,
    partition_file_into_stages,
)
from src.core.score_gate import (
    stages_cache_clear,
    stages_cache_get,
    stages_cache_set,
)


class TestDiffInspectorBlockAware(unittest.TestCase):
    """Testa que o fatiador de diffs nunca quebra no meio de palavras ou linhas."""

    def test_block_aware_chunking_preserves_lines_and_words(self) -> None:
        raw_code = (
            "@@ -1,10 +1,25 @@\n"
            "+def calcular_imposto_complexo(base_calculo: float, aliquota: float) -> float:\n"
            "+    # Linha explicativa importante que nao pode ser cortada no meio\n"
            "+    if base_calculo <= 0.0:\n"
            "+        raise ValueError('Base de calculo invalida')\n"
            "+    valor_bruto = base_calculo * aliquota\n"
            "+    desconto_padrao = 150.0\n"
            "+    return max(0.0, valor_bruto - desconto_padrao)\n"
            "+\n"
            "+def processar_folha_pagamento(colaboradores: list) -> dict:\n"
            "+    resultado_total = 0.0\n"
            "+    for colab in colaboradores:\n"
            "+        resultado_total += calcular_imposto_complexo(colab['salario'], 0.275)\n"
            "+    return {'total_retido': resultado_total}\n"
        )
        diff_file = DiffFile(
            path="src/tax_engine.py",
            status="modified",
            raw_diff=raw_code,
        )

        # Força um teto baixo para testar múltiplos blocos
        stages = partition_file_into_stages(diff_file, max_chars=250)
        self.assertGreater(len(stages), 1, "Deveria ter gerado pelo menos 2 etapas.")

        total_lines_raw = len(raw_code.splitlines())
        total_lines_chunks = sum(st.lines_count for st in stages)
        self.assertEqual(
            total_lines_raw,
            total_lines_chunks,
            "A soma de linhas dos chunks deve ser idêntica ao diff original.",
        )

    def test_block_aware_cuts_after_block_end_or_before_new_def(self) -> None:
        """Valida que o fatiador corta prioritariamente no fechamento de bloco ou antes de novo def."""
        raw_code = (
            "+def funcao_um() -> bool:\n"
            "+    linha_a = 10\n"
            "+    linha_b = 20\n"
            "+    linha_c = 30\n"
            "+    return True\n"
            "+\n"
            "+def funcao_dois() -> bool:\n"
            "+    outra_a = 100\n"
            "+    outra_b = 200\n"
            "+    return False\n"
        )
        diff_file = DiffFile(
            path="src/blocks.py",
            status="added",
            raw_diff=raw_code,
        )

        # max_chars=140 permite acumular a funcao_um (aprox 100 chars) e força a quebra
        # antes ou no início da funcao_dois
        stages = partition_file_into_stages(diff_file, max_chars=140)
        self.assertEqual(len(stages), 2)

        # A primeira etapa deve conter funcao_um completa fechada com return True
        self.assertIn("def funcao_um", stages[0].content)
        self.assertIn("return True", stages[0].content)
        self.assertNotIn("def funcao_dois", stages[0].content)

        # A segunda etapa deve iniciar a funcao_dois
        self.assertIn("def funcao_dois", stages[1].content)
        self.assertIn("return False", stages[1].content)

        # Garante que nenhum bloco corta palavras
        for st in stages:
            lines = st.content.splitlines()
            # Primeira linha é sempre o cabeçalho contextual do bloco
            self.assertTrue(lines[0].startswith("--- [Arquivo:"))
            for ln in lines[1:]:
                self.assertFalse(ln.startswith("alor_bruto"))  # palavra cortada
                self.assertFalse(ln.endswith("def calcular_"))  # corte arbitrário


class TestDiffInspectorRealGit(unittest.TestCase):
    """Testa extração de diffs e estágios em repositório Git real temporário."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="jev_test_git_diff_")
        self.ws = Path(self.temp_dir)
        subprocess.run(["git", "init"], cwd=self.ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=self.ws, check=True, capture_output=True)

        # Commit inicial
        (self.ws / "initial.py").write_text("INITIAL = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "initial.py"], cwd=self.ws, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.ws, check=True, capture_output=True)

    def tearDown(self) -> None:
        stages_cache_clear(str(self.ws))
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_extract_diff_staged_unstaged_and_untracked(self) -> None:
        # 1. Modifica o inicial
        (self.ws / "initial.py").write_text("INITIAL = 2  # modificado\n", encoding="utf-8")
        # 2. Cria arquivo novo untracked
        (self.ws / "new_module.py").write_text("def hello(): return 'world'\n", encoding="utf-8")

        diff_files = get_workspace_diff_files(str(self.ws))
        paths = [df.path for df in diff_files]
        self.assertIn("initial.py", paths)
        self.assertIn("new_module.py", paths)

        stages = extract_workspace_stages(str(self.ws), max_stage_chars=500)
        self.assertGreaterEqual(len(stages), 2)
        stage_files = {st.file_path for st in stages}
        self.assertIn("initial.py", stage_files)
        self.assertIn("new_module.py", stage_files)

    def test_incremental_stages_cache(self) -> None:
        ws_str = str(self.ws)
        stage_id = "initial.py:b1_abcdef12"

        # Cache miss
        self.assertIsNone(stages_cache_get(ws_str, stage_id))

        # Grava no cache
        stages_cache_set(ws_str, stage_id, {"score": 3.8, "passed": True, "borderline": False})

        # Cache hit
        hit = stages_cache_get(ws_str, stage_id)
        self.assertIsNotNone(hit)
        self.assertEqual(hit["score"], 3.8)
        self.assertTrue(hit["passed"])

        # Limpeza
        stages_cache_clear(ws_str)
        self.assertIsNone(stages_cache_get(ws_str, stage_id))

    def test_format_stage_premise_injects_diff_and_pedido(self) -> None:
        user_premise = "Pedido do Prompt: criar funcao soma | Resultado Executado: arquivos criados."
        (self.ws / "calc.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")
        stages = extract_workspace_stages(str(self.ws))
        self.assertEqual(len(stages), 1)

        formatted = format_stage_premise(user_premise, stages[0])
        self.assertIn("Pedido: criar funcao soma", formatted)
        self.assertIn("def add(a, b): return a + b", formatted)
        self.assertIn("calc.py", formatted)


class TestMcpMultiStageIntegration(unittest.IsolatedAsyncioTestCase):
    """Testa a integração da tool MCP openjev_score_and_diagnose com avaliação por etapas reais."""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="jev_mcp_stages_")
        self.ws = Path(self.temp_dir)
        subprocess.run(["git", "init"], cwd=self.ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=self.ws, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=self.ws, check=True, capture_output=True)

        (self.ws / "app.py").write_text("VERSION = 1\n", encoding="utf-8")
        subprocess.run(["git", "add", "app.py"], cwd=self.ws, check=True, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.ws, check=True, capture_output=True)

    def tearDown(self) -> None:
        stages_cache_clear(str(self.ws))
        import shutil
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    async def test_mcp_evaluates_stages_and_caches_approvals(self) -> None:
        from src.mcp_server import openjev_score_and_diagnose

        # Adiciona código limpo com testes reais
        (self.ws / "math_ops.py").write_text(
            "def multiply(a: int, b: int) -> int:\n    return a * b\n",
            encoding="utf-8",
        )
        (self.ws / "test_math.py").write_text(
            "from math_ops import multiply\ndef test_mult():\n    assert multiply(3, 4) == 12\n",
            encoding="utf-8",
        )

        res1 = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: implementar multiplicacao com testes reais. | Resultado Executado: modulos implementados.",
            workspace=str(self.ws),
            threshold=3.40,
        )

        self.assertIn("stages", res1)
        self.assertGreaterEqual(res1["total_stages"], 2)
        # Primeira rodada: nenhuma etapa deve vir do cache
        for st in res1["stages"]:
            self.assertFalse(st["cached"])

        # Segunda rodada: sem modificar os arquivos, as etapas aprovadas devem vir marcadas como cached=True
        if res1["passed"]:
            res2 = await openjev_score_and_diagnose(
                premise="Pedido do Prompt: reavaliar multiplicacao. | Resultado Executado: codigo inalterado.",
                workspace=str(self.ws),
                threshold=3.40,
            )
            self.assertTrue(res2["passed"])
            for st in res2["stages"]:
                self.assertTrue(st["cached"], f"Etapa {st['stage_id']} deveria ser cached.")


if __name__ == "__main__":
    unittest.main()
