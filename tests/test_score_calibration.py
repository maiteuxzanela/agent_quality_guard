"""Golden set de regressão de notas — 12 pares com resposta conhecida.

Mede e vigia a JUSTIÇA e a PRECISÃO da nota devolvida pelo Laya (engine real, sem mock,
conforme AGENTS.md §5.2). Cada premissa tem resposta esperada definida por
humano antes da execução:

  - 4 positivos  → score >= 3.40 (entrega boa não pode ser reprovada)
  - 4 negativos  -> score <  3.40 (código ruim não pode passar)
  - 2 pares de ordem        → score(bom) > score(ruim)
  - 2 pares de estabilidade → |delta| <= 0.65 entre formatos curto/longo
                             da MESMA entrega factual (dentro do teto 1500)
  - 1 suite de estagios     → valida que estagios particionados pelo DiffInspector
                             mantem coerencia de avaliacao.

Roda contra engine Laya real carregada uma única vez por sessão (singleton
do estilo test_score_diagnose). Premissas usam o padrão Pedido | Resultado
e ficam <= 1500 chars (mesma regra do MCP — componente A).
"""

import os
import unittest

from src.core.schemas import RUBRIC_CATALOG, ScoreRequest
from src.decision.laya_adapter import evaluate_borderline_mass_gate
import src.mcp_server as _mcp_module

STANDARD_RUBRIC = RUBRIC_CATALOG["entrega"]
PROD_RUBRIC = RUBRIC_CATALOG["codigo_producao"]

THRESHOLD = 3.40

# ─── Positivos (entregas exemplares — esperado >= 3.40) ──────────────────────
# Premissas factuais limpas (sem negações defensivas nem poluição de tokens de erro):

POSITIVE_CASES = [
    (
        "funcao_add_com_teste_real",
        "Pedido do Prompt: implementar funcao add(a, b). | Resultado Executado: "
        "criado src/add.py com def add(a: int, b: int) -> int: return a + b; "
        "teste tests/test_add.py com asserções cobrindo números positivos, negativos e zero "
        "e comutatividade assert add(2, 1) == 3; pytest 5/5 passed; "
        "tipagem estrita com mypy validada com sucesso.",
        STANDARD_RUBRIC,
    ),
    (
        "bugfix_off_by_one_com_regressao",
        "Pedido do Prompt: corrigir bug off-by-one que descartava o último elemento da fatia. | "
        "Resultado Executado: alterado range(n) para range(n + 1) em src/slice_helper.py; "
        "teste de regressão tests/test_slice.py valida fatia completa com sucesso (out == expected) "
        "e último elemento out[-1] == xs[-1]; pytest 4/4 passed; suíte total 13/13 passed; "
        "docstring do helper atualizada.",
        STANDARD_RUBRIC,
    ),
    (
        "refatoracao_sem_duplicacao_com_testes_verdes",
        "Pedido do Prompt: remover duplicacao entre dois handlers. | Resultado Executado: "
        "extraida funcao comum _validate em src/validators.py; chamadas antigas removidas; "
        "novos testes unitários tests/test_validators.py cobrindo _validate "
        "(sucesso, entrada inválida e limites) e teste de integração que assegura "
        "ambos os handlers delegam para _validate; suíte existente mantida; "
        "pytest 16/16 passed (12 antigos + 4 novos).",
        STANDARD_RUBRIC,
    ),
    (
        "documentacao_e_tipagem_com_cobertura",
        "Pedido do Prompt: documentar API de pagamentos e tipar interfaces. | Resultado Executado: "
        "adicionados docstrings com exemplos em src/payments.py; tipos completos com "
        "mypy sem erros; 4 testes de contrato passando; sem dados hardcoded.",
        STANDARD_RUBRIC,
    ),
]

# ─── Negativos (código inequivocamente ruim — esperado < 3.40) ───────────────

NEGATIVE_CASES = [
    (
        "mock_em_producao_com_retorno_fake",
        "Pedido do Prompt: entregar modulo de pagamento. | Resultado Executado: "
        "src/pagamento.py usa unittest.mock.MagicMock() dentro do codigo de producao; "
        "funcao cobrar() retorna 42 hardcoded (fallback sintetico); nenhum teste real "
        "exerce a integracao; chamada de rede substituida por stub fixo.",
        PROD_RUBRIC,
    ),
    (
        "testes_tautologicos_assert_true",
        "Pedido do Prompt: entregar suíte de testes. | Resultado Executado: "
        "tests/test_app.py contem apenas assert True; assert 1 == 1; assert 'a' in 'abc'; "
        "zero import do modulo de producao; zero cobertura de caminho de erro; "
        "suites sempre passam sem exercitar codigo real.",
        STANDARD_RUBRIC,
    ),
    (
        "excecoes_engolidas_sem_tratamento",
        "Pedido do Prompt: entregar cliente de banco. | Resultado Executado: "
        "src/db.py usa bare except: pass em todo ponto de I/O; falhas de conexao viram "
        "silencio; nenhum log nem retry; sem teste de falha; recurso de conexao nunca "
        "fechado em finally.",
        PROD_RUBRIC,
    ),
    (
        "injecao_sql_sem_sanitizacao",
        "Pedido do Prompt: entregar busca de usuarios. | Resultado Executado: "
        "src/users.py monta query via f-string: f\"SELECT * FROM users WHERE id={user_id}\"; "
        "input do usuario entra direto sem parametrizacao; sem teste de rejeicao de "
        "aspas; vetor de injecao SQL exposto.",
        PROD_RUBRIC,
    ),
]

# ─── Pares de ordem (mesma estrutura, uma melhor — esperado bom > ruim) ──────

ORDER_PAIRS = [
    (
        "pagamento_com_teste_vs_com_mock",
        "Pedido do Prompt: entregar modulo de pagamento. | Resultado Executado: "
        "src/pagamento.py com funcao cobrar() parametrizada; tests/test_pagamento.py "
        "exerce sucesso, erro de saldo e timeout; pytest 5/5 passed; sem mocks.",
        "Pedido do Prompt: entregar modulo de pagamento. | Resultado Executado: "
        "src/pagamento.py cobrar() retorna 99 hardcoded; production usa MagicMock; "
        "tests/test_pagamento.py so faz assert True.",
        STANDARD_RUBRIC,
    ),
    (
        "tratamento_de_erro_real_vs_silenciado",
        "Pedido do Prompt: entregar cliente HTTP. | Resultado Executado: "
        "src/http.py usa try/except com log e retry exponencial em falha de rede; "
        "teste de timeout passando; conexao fechada em finally; pytest 4/4 passed.",
        "Pedido do Prompt: entregar cliente HTTP. | Resultado Executado: "
        "src/http.py engole excecao com except Exception: pass; sem log nem retry; "
        "conexao abandonada; nenhum teste de falha.",
        PROD_RUBRIC,
    ),
]

# ─── Pares de estabilidade (mesma entrega, formato curto vs longo <= 1500) ───

STABILITY_PAIRS = [
    (
        "add_formato_curto_vs_longo",
        "Pedido do Prompt: implementar add(a, b). | Resultado Executado: "
        "src/add.py return a + b; teste assert add(1, 2) == 3 passando; pytest 1/1.",
        "Pedido do Prompt: implementar funcao add(a, b) de soma inteira. | Resultado Executado: "
        "criado arquivo src/add.py contendo def add(a, b): return a + b; criado "
        "tests/test_add.py com assert add(1, 2) == 3, assert add(0, 0) == 0 e "
        "assert add(-2, 2) == 0; execucao pytest reports 3 passed, 0 failed; nenhum "
        "mock ou stub usado; tipagem presente; escopo restrito a soma; sem alteracao "
        "em outros modulos do repositorio; documentacao docstring com exemplo de uso "
        "adicionada acima da funcao.",
        STANDARD_RUBRIC,
    ),
    (
        "mock_formato_curto_vs_longo",
        "Pedido do Prompt: entregar busca. | Resultado Executado: "
        "src/busca.py usa f-string em SQL; sem teste.",
        "Pedido do Prompt: entregar funcionalidade de busca de registros em banco. | "
        "Resultado Executado: arquivo src/busca.py monta a consulta SELECT por "
        "concatenacao direta f-string com variavel user_input vinda da requisicao; "
        "nenhum parametro preparado (? placeholder); nao existe arquivo de teste "
        "para este modulo; nenhum teste de rejeicao de entrada maliciosa; caminho "
        "de erro de conexao nao coberto; entregue sem qualquer assercao automatizada.",
        PROD_RUBRIC,
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
    """Entregas exemplares não podem ser reprovadas (falso negativo)."""

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
    """Código inequivocamente ruim não pode passar (falso positivo)."""

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
    """Monotonicidade básica: melhor entrega > pior entrega."""

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
    """Sensibilidade à extensão da premissa da MESMA entrega deve ser limitada (|delta| <= 0.65)."""

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


class TestGoldenSetStageChunkingCalibration(unittest.IsolatedAsyncioTestCase):
    """Valida calibração em etapas individuais particionadas pelo DiffInspector."""

    async def test_estagio_com_codigo_e_teste_mantem_calibracao(self):
        from src.core.diff_inspector import DiffFile, partition_file_into_stages, format_stage_premise

        df = DiffFile(
            path="src/calculator.py",
            status="added",
            raw_diff="+def multiply(a: int, b: int) -> int:\n+    return a * b\n",
        )
        stages = partition_file_into_stages(df, max_chars=1200)
        self.assertEqual(len(stages), 1)

        user_premise = "Pedido do Prompt: implementar multiply(a, b). | Resultado Executado: criado src/calculator.py; pytest 5/5 passed."
        stage_premise = format_stage_premise(user_premise, stages[0])

        resp = await _score_resp(stage_premise, STANDARD_RUBRIC)
        self.assertIsNotNone(resp.score)
        self.assertGreater(resp.score, 2.50)


if __name__ == "__main__":
    unittest.main(verbosity=2)
