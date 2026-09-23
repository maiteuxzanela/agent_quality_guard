"""Golden set de regressão e calibração de notas do Open JEV.

Mede e vigia a JUSTIÇA e a PRECISÃO da nota devolvida pelo Laya (engine real, sem mock,
conforme AGENTS.md §5.2).

Evolução Arquitetural (set/2026):
Adaptado para refletir o pipeline real de inspeção de diffs do Git e particionamento em
etapas cirúrgicas (DiffInspector), avaliando premissas que contêm:
  1. Pedido do Prompt (requisitos e comandos)
  2. Resultado Executado (relatório de testes, asserções, tipagem)
  3. Código Alterado / Diffs Reais (formatado por blocos cirúrgicos)

Critérios de Aceite:
  - 4 positivos  → score >= 3.40 ou aprovação via Borderline Mass Gate (p_healthy >= 0.85, p_flawed <= 0.12)
  - 4 negativos  → score < 3.40 e rejeição estrita (código com mock, sql injection, bare except, assert True)
  - 2 pares de ordem        → score(bom) > score(ruim)
  - 2 pares de estabilidade → |delta| <= 0.65 entre variações de extensão
"""

import os
import unittest

from src.core.schemas import RUBRIC_CATALOG, ScoreRequest
from src.decision.laya_adapter import evaluate_borderline_mass_gate
import src.mcp_server as _mcp_module

STANDARD_RUBRIC = RUBRIC_CATALOG["entrega"]
PROD_RUBRIC = RUBRIC_CATALOG["codigo_producao"]

THRESHOLD = 3.40

# ─── Casos Positivos com Diffs Reais Estruturadas (Esperado: Aprovado) ────────

POSITIVE_CASES = [
    (
        "add_com_testes_completos",
        """Pedido do Prompt: implementar funcao add(a, b) de soma inteira com testes unitários. | Resultado Executado: criado src/add.py e tests/test_add.py; pytest 5/5 passed; mypy tipagem estrita sem erros.

Código alterado (src/add.py - Bloco 1/1):
--- [Arquivo: src/add.py | Bloco 1/1] ---
+def add(a: int, b: int) -> int:
+    \"\"\"Retorna a soma de dois inteiros.\"\"\"
+    return a + b

Código alterado (tests/test_add.py - Bloco 1/1):
--- [Arquivo: tests/test_add.py | Bloco 1/1] ---
+from src.add import add
+
+def test_add():
+    assert add(2, 3) == 5
+    assert add(-1, 1) == 0
+    assert add(0, 0) == 0
""",
        STANDARD_RUBRIC,
    ),
    (
        "bugfix_slice_com_regressao",
        """Pedido do Prompt: corrigir bug off-by-one que descartava o ultimo elemento da lista. | Resultado Executado: corrigido range em src/slice_helper.py; teste de regressao tests/test_slice.py passando; pytest 13/13 passed.

Código alterado (src/slice_helper.py - Bloco 1/1):
--- [Arquivo: src/slice_helper.py | Bloco 1/1] ---
@@ -10,3 +10,3 @@
-    for i in range(len(items) - 1):
+    for i in range(len(items)):
         yield items[i]

Código alterado (tests/test_slice.py - Bloco 1/1):
--- [Arquivo: tests/test_slice.py | Bloco 1/1] ---
+def test_slice_includes_last_item():
+    assert list(slice_items([10, 20, 30])) == [10, 20, 30]
""",
        STANDARD_RUBRIC,
    ),
    (
        "refatoracao_validador_com_testes",
        """Pedido do Prompt: extrair validacao duplicada para funcao comum _validate_payload. | Resultado Executado: criado validador em src/validators.py; chamadas migradas nos handlers; pytest 16/16 passed sem mocks.

Código alterado (src/validators.py - Bloco 1/1):
--- [Arquivo: src/validators.py | Bloco 1/1] ---
+def validate_payload(data: dict) -> bool:
+    if not data or "id" not in data:
+        raise ValueError("Payload invalido")
+    return True

Código alterado (tests/test_validators.py - Bloco 1/1):
--- [Arquivo: tests/test_validators.py | Bloco 1/1] ---
+def test_validate_payload():
+    assert validate_payload({"id": 1}) is True
""",
        STANDARD_RUBRIC,
    ),
    (
        "documentacao_e_tipagem_estrita",
        """Pedido do Prompt: documentar modulo de pagamentos e adicionar tipagem estrita. | Resultado Executado: interfaces tipadas em src/payments.py; docstrings adicionadas; mypy sem erros; 4 testes de contrato passando.

Código alterado (src/payments.py - Bloco 1/1):
--- [Arquivo: src/payments.py | Bloco 1/1] ---
+from typing import Optional
+
+class PaymentProcessor:
+    \"\"\"Processador de transacoes financeiras com validacao estrita de montante.\"\"\"
+    def process(self, amount_cents: int, currency: str = "BRL") -> Optional[str]:
+        if amount_cents <= 0:
+            raise ValueError("Valor deve ser positivo")
+        return "txn_123"
""",
        STANDARD_RUBRIC,
    ),
]

# ─── Casos Negativos com Diffs Reais Contendo Defeitos (Esperado: Reprovado) ──

NEGATIVE_CASES = [
    (
        "mock_em_producao",
        """Pedido do Prompt: entregar modulo de pagamento integrado para producao. | Resultado Executado: src/pagamento.py utiliza MagicMock em producao com retorno sintetico hardcoded; sem testes reais.

Código alterado (src/pagamento.py - Bloco 1/1):
--- [Arquivo: src/pagamento.py | Bloco 1/1] ---
+from unittest.mock import MagicMock
+
+def cobrar_cartao(cartao_id: str, valor: float) -> dict:
+    mock = MagicMock()
+    mock.status = "pago"
+    return {"status": "pago", "code": 42}
""",
        PROD_RUBRIC,
    ),
    (
        "testes_tautologicos_assert_true",
        """Pedido do Prompt: entregar suite de testes para cobrir funcionalidade de autenticacao. | Resultado Executado: tests/test_auth.py contem apenas assercoes tautologicas assert True sem importar codigo de producao.

Código alterado (tests/test_auth.py - Bloco 1/1):
--- [Arquivo: tests/test_auth.py | Bloco 1/1] ---
+def test_auth():
+    assert True
+    assert 1 == 1
+    assert "a" in "abc"
""",
        STANDARD_RUBRIC,
    ),
    (
        "excecoes_engolidas_sem_tratamento",
        """Pedido do Prompt: entregar cliente de banco com tratamento de erros de conexao. | Resultado Executado: src/db.py usa bare except: pass engolindo qualquer falha de I/O em silencio sem log.

Código alterado (src/db.py - Bloco 1/1):
--- [Arquivo: src/db.py | Bloco 1/1] ---
+def execute_query(sql: str):
+    try:
+        conn = open_connection()
+        return conn.run(sql)
+    except:
+        pass
""",
        PROD_RUBRIC,
    ),
    (
        "injecao_sql_sem_sanitizacao",
        """Pedido do Prompt: entregar busca de usuarios por ID no banco de dados. | Resultado Executado: src/users.py monta query SQL via f-string sem parametros preparados, expondo vulnerabilidade critica de SQL Injection.

Código alterado (src/users.py - Bloco 1/1):
--- [Arquivo: src/users.py | Bloco 1/1] ---
+def get_user_by_id(user_input: str, db):
+    query = f"SELECT * FROM users WHERE id = {user_input}"
+    return db.execute(query)
""",
        PROD_RUBRIC,
    ),
]

# ─── Pares de Ordem Monotônica (score(bom) > score(ruim)) ───────────────────

ORDER_PAIRS = [
    (
        "add_com_teste_vs_mock",
        """Pedido do Prompt: implementar add(a, b). | Resultado Executado: criado src/add.py com soma e tests/test_add.py com assercoes passando; pytest 5/5.

Código alterado (src/add.py - Bloco 1/1):
--- [Arquivo: src/add.py | Bloco 1/1] ---
+def add(a: int, b: int) -> int: return a + b
""",
        """Pedido do Prompt: implementar add(a, b). | Resultado Executado: src/add.py retorna 42 fixo com MagicMock em producao; testes apenas assert True.

Código alterado (src/add.py - Bloco 1/1):
--- [Arquivo: src/add.py | Bloco 1/1] ---
+from unittest.mock import MagicMock
+def add(a, b): return 42
""",
        STANDARD_RUBRIC,
    ),
    (
        "sanitizado_vs_injecao_sql",
        """Pedido do Prompt: busca de usuarios por ID. | Resultado Executado: src/users.py usa query parametrizada segura com placeholder '?'; testes de contrato passando.

Código alterado (src/users.py - Bloco 1/1):
--- [Arquivo: src/users.py | Bloco 1/1] ---
+def get_user(uid: int, db): return db.execute("SELECT * FROM users WHERE id = ?", (uid,))
""",
        """Pedido do Prompt: busca de usuarios por ID. | Resultado Executado: src/users.py concatena input direto na query f-string; sem sanitizacao.

Código alterado (src/users.py - Bloco 1/1):
--- [Arquivo: src/users.py | Bloco 1/1] ---
+def get_user(uid, db): return db.execute(f"SELECT * FROM users WHERE id = {uid}")
""",
        PROD_RUBRIC,
    ),
]

# ─── Pares de Estabilidade (|delta| <= 0.65) ────────────────────────────────

STABILITY_PAIRS = [
    (
        "add_resumo_curto_vs_longo",
        """Pedido do Prompt: implementar add(a, b). | Resultado Executado: src/add.py com soma inteira; tests/test_add.py passando; pytest 3/3.

Código alterado (src/add.py - Bloco 1/1):
--- [Arquivo: src/add.py | Bloco 1/1] ---
+def add(a: int, b: int) -> int:
+    return a + b
""",
        """Pedido do Prompt: implementar funcao add(a, b) de soma inteira com tipagem estrita e testes. | Resultado Executado: criado arquivo src/add.py contendo def add(a: int, b: int) -> int: return a + b; criado tests/test_add.py com assercoes de comutatividade e valores nulos e negativos; pytest reports 3 passed, 0 failed; nenhum mock utilizado; tipagem validada via mypy sem erros.

Código alterado (src/add.py - Bloco 1/1):
--- [Arquivo: src/add.py | Bloco 1/1] ---
+def add(a: int, b: int) -> int:
+    return a + b
""",
        STANDARD_RUBRIC,
    ),
]


def _ensure_engine_loaded():
    if _mcp_module._engine_instance is None:
        from src.decision.engine import OpenJevEngine
        engine = OpenJevEngine(
            model_id_or_path="convaiinnovations/laya",
            allow_cpu_fallback=True,
        )
        engine.load_model()
        _mcp_module._engine_instance = engine
    return _mcp_module._engine_instance


async def _score_resp(premise: str, rubric: str = STANDARD_RUBRIC):
    engine = _ensure_engine_loaded()
    resp = await engine.evaluate_score(ScoreRequest(
        premise=premise,
        rubric=rubric,
        min_score=1.0,
        max_score=5.0,
        threshold=THRESHOLD,
    ))
    if resp.score_margin is None or resp.borderline is None:
        raise AssertionError("ScoreResponse sem score_margin/borderline — regressão no adapter")
    return resp


async def _score(premise: str, rubric: str = STANDARD_RUBRIC) -> float:
    resp = await _score_resp(premise, rubric)
    return resp.score


class TestGoldenSetPositivos(unittest.IsolatedAsyncioTestCase):
    """Entregas exemplares com diffs e testes reais devem atingir pontuação de corte."""

    async def test_positivos_ganham_nota_minima(self):
        for name, premise, rubric in POSITIVE_CASES:
            with self.subTest(case=name):
                resp = await _score_resp(premise, rubric)
                mass_ok, p_healthy, p_flawed = evaluate_borderline_mass_gate(resp.breakdown)
                passed = (resp.score >= THRESHOLD) or (resp.borderline and mass_ok)
                self.assertTrue(
                    passed,
                    f"FALSO NEGATIVO em '{name}': score={resp.score:.2f} < {THRESHOLD} (mass_gate={mass_ok}, p_healthy={p_healthy:.3f}, p_flawed={p_flawed:.3f})",
                )


class TestGoldenSetNegativos(unittest.IsolatedAsyncioTestCase):
    """Código inequivocamente ruim (mocks, falhas de segurança) deve ser reprovado (< 3.40)."""

    async def test_negativos_reprovam(self):
        for name, premise, rubric in NEGATIVE_CASES:
            with self.subTest(case=name):
                resp = await _score_resp(premise, rubric)
                mass_ok, p_healthy, p_flawed = evaluate_borderline_mass_gate(resp.breakdown)
                passed = (resp.score >= THRESHOLD) or (resp.borderline and mass_ok)
                self.assertFalse(
                    passed,
                    f"FALSO POSITIVO em '{name}': score={resp.score:.2f} >= {THRESHOLD} (mass_gate={mass_ok}, p_flawed={p_flawed:.3f})",
                )


class TestGoldenSetOrdem(unittest.IsolatedAsyncioTestCase):
    """Monotonicidade básica: código correto e testado > código com falha estrutural."""

    async def test_ordem_pares(self):
        for name, good, bad, rubric in ORDER_PAIRS:
            with self.subTest(case=name):
                score_good = await _score(good, rubric)
                score_bad = await _score(bad, rubric)
                self.assertGreater(
                    score_good, score_bad,
                    f"NÃO MONOTÔNICO em '{name}': bom={score_good:.2f} <= ruim={score_bad:.2f}",
                )


class TestGoldenSetEstabilidade(unittest.IsolatedAsyncioTestCase):
    """Sensibilidade à extensão da premissa da MESMA entrega deve ser controlada (|delta| <= 0.65)."""

    async def test_estabilidade_template(self):
        for name, short, long, rubric in STABILITY_PAIRS:
            with self.subTest(case=name):
                self.assertLessEqual(len(short), 1500, f"'{name}' curto excede teto 1500 chars")
                self.assertLessEqual(len(long), 1500, f"'{name}' longo excede teto 1500 chars")
                score_short = await _score(short, rubric)
                score_long = await _score(long, rubric)
                delta = abs(score_short - score_long)
                self.assertLessEqual(
                    delta, 0.65,
                    f"INSTÁVEL em '{name}': delta={delta:.2f} (curto={score_short:.2f}, longo={score_long:.2f}) > 0.65",
                )


if __name__ == "__main__":
    unittest.main(verbosity=2)
