"""Testes do score_gate: defaults pinados, lock de histórico e validação de premissa."""

import os
import subprocess
import tempfile
import threading
import unittest
from pathlib import Path

from agent_quality_guard.core.score_gate import (
    CAVEAT_BAND,
    MAX_ATTEMPTS,
    SCORE_PREMISE_MAX_LEN,
    caveat_decision,
    history_clear,
    history_get,
    history_increment_attempts,
    history_set,
    resolve_task_rubric,
    validate_score_premise,
)
from agent_quality_guard.core.schemas import NoulRequest, ScoreRequest


class TestCaveatBand(unittest.TestCase):
    """Banda de indiferença (indifference zone) — decisão da fundadora, set/2026.

    Faixa de 0.20 abaixo do corte: dentro dela a ressalva é permitida (com
    zero suspeitas, arbitrado pelo chamador); abaixo dela o corte é estrito.
    """

    def test_banda_e_020(self):
        self.assertEqual(CAVEAT_BAND, 0.20)

    def test_score_no_corte_passa(self):
        self.assertEqual(caveat_decision(3.40, 3.40), "pass")
        self.assertEqual(caveat_decision(4.85, 3.40), "pass")

    def test_score_na_banda_caveat(self):
        self.assertEqual(caveat_decision(3.39, 3.40), "caveat")
        self.assertEqual(caveat_decision(3.20, 3.40), "caveat")
        self.assertEqual(caveat_decision(3.21, 3.40), "caveat")

    def test_score_abaixo_da_banda_fail(self):
        self.assertEqual(caveat_decision(3.19, 3.40), "fail")
        self.assertEqual(caveat_decision(2.68, 3.40), "fail")

    def test_faixa_calculada(self):
        """Piso da banda: threshold - 0.20 (3.40 -> 3.20)."""
        self.assertAlmostEqual(3.40 - CAVEAT_BAND, 3.20)


class TestScoreGateDefaults(unittest.TestCase):
    def test_score_request_default_threshold_340(self):
        self.assertEqual(ScoreRequest(premise="Pedido: a | Resultado: b").threshold, 3.40)

    def test_noul_request_default_threshold_078(self):
        self.assertEqual(NoulRequest(premise="a", hypothesis="b").threshold, 0.78)

    def test_max_attempts_is_five(self):
        self.assertEqual(MAX_ATTEMPTS, 5)

    def test_premise_max_len_1500(self):
        self.assertEqual(SCORE_PREMISE_MAX_LEN, 1500)

    def test_score_request_escala_invertida_levanta_validationerror(self):
        """Caminho de erro real: min_score >= max_score é rejeitado pelo validator do schema."""
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            ScoreRequest(
                premise="Pedido: x | Resultado: y",
                min_score=5.0,
                max_score=1.0,
            )

    def test_score_request_threshold_fora_da_escala_levanta_validationerror(self):
        """Caminho de erro real: threshold fora de [min_score, max_score] é rejeitado."""
        from pydantic import ValidationError

        with self.assertRaises(ValidationError):
            ScoreRequest(
                premise="Pedido: x | Resultado: y",
                min_score=1.0,
                max_score=5.0,
                threshold=9.0,
            )


class TestValidatePremise(unittest.TestCase):
    def test_ok(self):
        self.assertIsNone(validate_score_premise("Pedido: x | Resultado: y"))

    def test_rejects_missing_pattern(self):
        self.assertIsNotNone(validate_score_premise("apenas texto livre"))

    def test_rejects_empty(self):
        self.assertIsNotNone(validate_score_premise("   "))

    def test_rejects_too_long(self):
        long_p = "Pedido: " + "x" * 1600 + " | Resultado: y"
        self.assertIsNotNone(validate_score_premise(long_p))


class TestResolveRubric(unittest.TestCase):
    def test_entrega_ok(self):
        rubric, err = resolve_task_rubric("entrega")
        self.assertIsNone(err)
        self.assertTrue(rubric)

    def test_unknown_rejected(self):
        rubric, err = resolve_task_rubric("qualquer_coisa")
        self.assertIsNone(rubric)
        self.assertIn("TASK_TYPE", err)


class TestHistoryLock(unittest.TestCase):
    def setUp(self):
        history_clear()

    def tearDown(self):
        history_clear()

    def test_concurrent_increments_do_not_lose_attempts(self):
        """P1.2: increments concorrentes no mesmo workspace não perdem contagem."""
        ws = "/tmp/jev_concurrent_ws"
        history_set(ws, {"passed": False, "attempts": 0, "git_state": "abc", "last_score": 2.0})
        n_threads = 20
        per_thread = 5
        barrier = threading.Barrier(n_threads)

        def worker():
            barrier.wait()
            for _ in range(per_thread):
                history_increment_attempts(ws)

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        hist = history_get(ws)
        self.assertEqual(hist["attempts"], n_threads * per_thread)

    def test_history_get_returns_copy(self):
        ws = "/tmp/jev_copy_ws"
        history_set(ws, {"passed": True, "attempts": 0, "git_state": "", "last_score": 4.0})
        got = history_get(ws)
        got["attempts"] = 99
        self.assertEqual(history_get(ws)["attempts"], 0)

    def test_history_and_stages_persist_when_ram_is_evicted(self):
        """Valida que o estado em disco restaura tentativas e estágios mesmo se a memória do processo for esvaziada."""
        from agent_quality_guard.core.score_gate import _score_history, _stages_cache, stages_cache_get, stages_cache_set
        ws = "/tmp/jev_persist_test_ws"
        history_set(ws, {"passed": False, "attempts": 3, "git_state": "hash123", "last_score": 3.10})
        stages_cache_set(ws, "stage_abc", {"score": 3.50, "passed": True})

        # Simula morte do processo / evaporação da memória RAM
        _score_history.clear()
        _stages_cache.clear()

        # O novo ciclo recupera fielmente do disco
        recovered_hist = history_get(ws)
        self.assertIsNotNone(recovered_hist)
        self.assertEqual(recovered_hist["attempts"], 3)
        self.assertEqual(recovered_hist["last_score"], 3.10)

        recovered_stage = stages_cache_get(ws, "stage_abc")
        self.assertIsNotNone(recovered_stage)
        self.assertEqual(recovered_stage["score"], 3.50)
        self.assertTrue(recovered_stage["passed"])

        # Incremento em novo ciclo preserva a sequência (3 -> 4)
        next_attempts = history_increment_attempts(ws)
        self.assertEqual(next_attempts, 4)


class TestWorkspaceGitHeadInvalidation(unittest.TestCase):
    """Garante que a mudança de commit Git (novo ciclo de entrega) reseta o histórico."""

    def setUp(self):
        history_clear()

    def tearDown(self):
        history_clear()

    def test_git_head_resets_history_on_new_commit(self):
        import subprocess
        import tempfile
        from agent_quality_guard.core.score_gate import get_workspace_head, resolve_attempt_number

        with tempfile.TemporaryDirectory() as tmp_dir:
            # 1. Workspace sem git -> get_workspace_head é None
            self.assertIsNone(get_workspace_head(tmp_dir))

            # 2. Inicializa repositório git e faz commit 1
            subprocess.run(["git", "init"], cwd=tmp_dir, capture_output=True, check=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=tmp_dir, check=True)
            subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tmp_dir, check=True)
            subprocess.run(["git", "commit", "--allow-empty", "-m", "commit 1"], cwd=tmp_dir, capture_output=True, check=True)

            head_1 = get_workspace_head(tmp_dir)
            self.assertIsNotNone(head_1)

            # 3. Registra 4 tentativas falhas no commit 1
            history_set(tmp_dir, {"passed": False, "attempts": 4})
            hist_1 = history_get(tmp_dir)
            self.assertIsNotNone(hist_1)
            self.assertEqual(hist_1["attempts"], 4)
            self.assertEqual(hist_1["git_head"], head_1)
            self.assertEqual(resolve_attempt_number(hist_1), 5)

            # 4. Faz commit 2 (novo ciclo de entrega após merge/conclusão)
            subprocess.run(["git", "commit", "--allow-empty", "-m", "commit 2"], cwd=tmp_dir, capture_output=True, check=True)
            head_2 = get_workspace_head(tmp_dir)
            self.assertNotEqual(head_1, head_2)

            # 5. history_get detecta commit novo e invalida automaticamente
            hist_2 = history_get(tmp_dir)
            self.assertIsNone(hist_2)
            self.assertEqual(resolve_attempt_number(hist_2), 1)

    def test_workspace_path_normalization_relative_vs_absolute(self):
        """Valida que caminhos relativos e absolutos convergem para o mesmo estado em memória e disco."""
        with tempfile.TemporaryDirectory() as tmp_dir:
            abs_path = str(Path(tmp_dir).resolve())
            history_clear(abs_path)
            history_set(abs_path, {"passed": False, "attempts": 2})

            orig_cwd = os.getcwd()
            try:
                os.chdir(abs_path)
                hist_rel = history_get(".")
                self.assertIsNotNone(hist_rel)
                self.assertEqual(hist_rel["attempts"], 2)

                inc_val = history_increment_attempts(".")
                self.assertEqual(inc_val, 3)

                hist_abs = history_get(abs_path)
                self.assertIsNotNone(hist_abs)
                self.assertEqual(hist_abs["attempts"], 3)
            finally:
                os.chdir(orig_cwd)
                history_clear(abs_path)


if __name__ == "__main__":
    unittest.main()
