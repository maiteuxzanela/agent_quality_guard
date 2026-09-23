"""Testes do score_gate: defaults pinados, lock de histórico e validação de premissa."""

import threading
import unittest

from src.core.score_gate import (
    MAX_ATTEMPTS,
    SCORE_PREMISE_MAX_LEN,
    history_clear,
    history_get,
    history_increment_attempts,
    history_set,
    resolve_task_rubric,
    validate_score_premise,
)
from src.core.schemas import NoulRequest, ScoreRequest


class TestScoreGateDefaults(unittest.TestCase):
    def test_score_request_default_threshold_340(self):
        self.assertEqual(ScoreRequest(premise="Pedido: a | Resultado: b").threshold, 3.40)

    def test_noul_request_default_threshold_078(self):
        self.assertEqual(NoulRequest(premise="a", hypothesis="b").threshold, 0.78)

    def test_max_attempts_is_five(self):
        self.assertEqual(MAX_ATTEMPTS, 5)

    def test_premise_max_len_1500(self):
        self.assertEqual(SCORE_PREMISE_MAX_LEN, 1500)


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


if __name__ == "__main__":
    unittest.main()
