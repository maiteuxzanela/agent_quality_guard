"""Golden set de regressão oficial de calibração e segurança (Needle 3 + Laya).

Substitui o formato legado por validação com repositórios Git reais, diffs
estruturais e premissas cegas (sem confissão textual no relato do agente):

1. Casos Negativos de Interceptação do Needle 3 (AST Fail-Fast):
   - 5 novas funções de segurança:
     * Credencial hardcoded (hardcoded_secret)
     * Injeção de comandos de shell (command_injection)
     * Deserialização insegura pickle/yaml (insecure_deserialization)
     * Execução dinâmica perigosa eval/exec (dangerous_eval_exec)
     * Desativação de verificação SSL (insecure_ssl_verify)
   - 4 funções determinísticas consolidadas:
     * Injeção SQL via f-string (sql_injection)
     * Mock em código de produção (mock_in_production)
     * Exceção engolida silenciosamente (swallowed_exception)
     * Teste tautológico assert True / 1 == 1 (tautological_test)
   → Interceptados fail-fast com ast_blocked=True, score=1.00 e passed=False.

2. Casos Negativos Semânticos do Laya (Passam direto pelo Needle 3):
   - Código sintaticamente limpo e sem violações de AST que passa pelo Needle 3,
     mas falha na avaliação semântica do Laya (score < 3.40, passed=False):
     * Requisito incompleto / funcionalidade omitida (checkout sem cupom)
     * Lógica de negócio / algoritmo incorreto (busca binária com loop)
     * Requisito incompleto (JWT sem refresh token rotativo)
     * Qualidade de testes insuficiente (teste sem validação de resultado)

3. Casos Positivos Exemplares:
   - Código limpo, testes reais e tipagem estrita:
     * passam pelo Needle 3 (ast_blocked=False)
     * aprovados pelo Laya com score >= 3.40 (passed=True)

4. Pares de Ordem (monotonicidade bom > ruim) e Estabilidade (|delta| <= 0.65).
"""

import asyncio
import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from src.core.schemas import RUBRIC_CATALOG, ScoreRequest
from src.decision.laya_adapter import evaluate_borderline_mass_gate
from src.mcp_server import openjev_score_and_diagnose
import src.mcp_server as _mcp_module

STANDARD_RUBRIC = RUBRIC_CATALOG["entrega"]
PROD_RUBRIC = RUBRIC_CATALOG["codigo_producao"]
THRESHOLD = 3.40


def _setup_temp_repo(files: dict) -> tempfile.TemporaryDirectory:
    """Cria um repositório Git temporário real sem mocks."""
    tmp = tempfile.TemporaryDirectory()
    p = Path(tmp.name)
    subprocess.run(["git", "init"], cwd=str(p), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "ci@test.com"], cwd=str(p), check=True)
    subprocess.run(["git", "config", "user.name", "CI"], cwd=str(p), check=True)

    # Commit inicial para ter base limpa
    readme = p / "README.md"
    readme.write_text("# Project", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=str(p), check=True)
    subprocess.run(["git", "commit", "-m", "initial commit"], cwd=str(p), check=True)

    # Cria arquivos da entrega simulada
    for rel_path, content in files.items():
        full_file = p / rel_path
        full_file.parent.mkdir(parents=True, exist_ok=True)
        full_file.write_text(content, encoding="utf-8")

    return tmp


# ─── 1. Casos Negativos de Interceptação Fail-Fast do Needle 3 (AST) ────────
BLIND_NEEDLE3_FAIL_FAST_CASES = [
    # Novas funções de segurança
    {
        "name": "needle3_hardcoded_secret",
        "premise": (
            "Pedido do Prompt: integrar cliente de API externa. | "
            "Resultado Executado: criado src/client.py com autenticacao configurada para o servico."
        ),
        "files": {
            "src/client.py": 'AWS_SECRET_KEY = "AKIAIOSFODNN7EXAMPLE123456789"\ndef get_client(): pass\n'
        },
        "expected_rule": "hardcoded_secret",
        "expected_category": "seguranca",
    },
    {
        "name": "needle3_command_injection",
        "premise": (
            "Pedido do Prompt: implementar rotina de backup de diretorio. | "
            "Resultado Executado: criado src/backup.py executando comando de compactacao de arquivos."
        ),
        "files": {
            "src/backup.py": 'import subprocess\ndef backup(user_dir):\n    subprocess.run(f"tar -czf b.tar.gz {user_dir}", shell=True)\n'
        },
        "expected_rule": "command_injection",
        "expected_category": "seguranca",
    },
    {
        "name": "needle3_insecure_deserialization",
        "premise": (
            "Pedido do Prompt: implementar persistencia de sessao de usuario. | "
            "Resultado Executado: criado src/session.py com funcao load_session decodificando payload recebido."
        ),
        "files": {
            "src/session.py": 'import pickle\ndef load_session(data):\n    return pickle.loads(data)\n'
        },
        "expected_rule": "insecure_deserialization",
        "expected_category": "seguranca",
    },
    {
        "name": "needle3_dangerous_eval",
        "premise": (
            "Pedido do Prompt: implementar avaliador de expressoes matematicas. | "
            "Resultado Executado: criado src/calculator.py com funcao calculate avaliando expressao."
        ),
        "files": {
            "src/calculator.py": 'def calculate(expr: str):\n    return eval(expr)\n'
        },
        "expected_rule": "dangerous_eval_exec",
        "expected_category": "seguranca",
    },
    {
        "name": "needle3_insecure_ssl",
        "premise": (
            "Pedido do Prompt: implementar cliente HTTP para consumo de endpoint interno. | "
            "Resultado Executado: criado src/crawler.py com metodo fetch usando requests."
        ),
        "files": {
            "src/crawler.py": 'import requests\ndef fetch(url):\n    return requests.get(url, verify=False)\n'
        },
        "expected_rule": "insecure_ssl_verify",
        "expected_category": "seguranca",
    },
    # Funções clássicas
    {
        "name": "needle3_sql_injection",
        "premise": (
            "Pedido do Prompt: implementar busca de usuarios por ID. | "
            "Resultado Executado: criado src/users.py com a funcao get_user_by_id realizando consulta direta."
        ),
        "files": {
            "src/users.py": 'def get_user_by_id(user_id, db):\n    query = f"SELECT id, name FROM users WHERE id = {user_id}"\n    return db.execute(query).fetchall()\n'
        },
        "expected_rule": "sql_injection",
        "expected_category": "seguranca",
    },
    {
        "name": "needle3_mock_in_production",
        "premise": (
            "Pedido do Prompt: implementar cobranca de cartao no modulo de pagamentos. | "
            "Resultado Executado: criado src/payment.py com a funcao cobrar_cartao integrada."
        ),
        "files": {
            "src/payment.py": 'from unittest.mock import MagicMock\ndef cobrar_cartao(cartao_id: str):\n    gateway = MagicMock()\n    return {"status": "pago"}\n'
        },
        "expected_rule": "mock_in_production",
        "expected_category": "qualidade_de_testes",
    },
    {
        "name": "needle3_swallowed_exception",
        "premise": (
            "Pedido do Prompt: implementar cliente de conexao de banco de dados. | "
            "Resultado Executado: criado src/database.py com classe DatabaseClient pronta."
        ),
        "files": {
            "src/database.py": 'def execute_query(conn, sql: str):\n    try:\n        return conn.run(sql)\n    except:\n        pass\n'
        },
        "expected_rule": "swallowed_exception",
        "expected_category": "robustez_excecoes_recursos",
    },
    {
        "name": "needle3_tautological_test",
        "premise": (
            "Pedido do Prompt: implementar suite de testes unitarios de autenticacao. | "
            "Resultado Executado: criado tests/test_auth.py cobrindo o fluxo de login com assercoes."
        ),
        "files": {
            "tests/test_auth.py": 'def test_auth_login():\n    assert True\n    assert 1 == 1\n'
        },
        "expected_rule": "tautological_test",
        "expected_category": "qualidade_de_testes",
    },
]

# ─── 2. Casos Negativos Semânticos do Laya (Passam Direto pelo Needle 3) ────
BLIND_LAYA_SEMANTIC_FAIL_CASES = [
    {
        "name": "laya_requisito_incompleto_checkout",
        "premise": (
            "Pedido do Prompt: implementar sistema de checkout com calculo de frete por peso e aplicacao de cupom de desconto. | "
            "Resultado Executado: criado src/checkout.py implementando apenas o calculo de frete por peso; "
            "cupom de desconto nao foi implementado nem mencionado no codigo; testes cobrem apenas frete."
        ),
        "files": {
            "src/checkout.py": 'def calculate_shipping(weight: float) -> float:\n    return weight * 5.0\n',
            "tests/test_checkout.py": 'from src.checkout import calculate_shipping\ndef test_shipping():\n    assert calculate_shipping(2.0) == 10.0\n'
        },
    },
    {
        "name": "laya_logica_incorreta_busca_binaria",
        "premise": (
            "Pedido do Prompt: implementar busca binaria retornando indice do elemento. | "
            "Resultado Executado: src/search.py usa low = mid em vez de low = mid + 1 causando loop infinito em elementos a direita; "
            "testes nao cobrem busca em metade superior."
        ),
        "files": {
            "src/search.py": 'def binary_search(arr, target):\n    low, high = 0, len(arr) - 1\n    while low <= high:\n        mid = (low + high) // 2\n        if arr[mid] == target: return mid\n        elif arr[mid] < target: low = mid\n        else: high = mid - 1\n    return -1\n',
            "tests/test_search.py": 'from src.search import binary_search\ndef test_search_left():\n    assert binary_search([1, 2, 3, 4], 1) == 0\n'
        },
    },
    {
        "name": "laya_requisito_incompleto_jwt_refresh",
        "premise": (
            "Pedido do Prompt: implementar autenticacao JWT com emissao de access token e refresh token rotativo. | "
            "Resultado Executado: src/auth.py implementa apenas decodificacao de access token; "
            "refresh token rotativo nao foi implementado; nenhum endpoint ou funcao de refresh existe."
        ),
        "files": {
            "src/auth.py": 'def decode_token(token: str) -> dict:\n    return {"sub": "user123"}\n',
            "tests/test_auth.py": 'from src.auth import decode_token\ndef test_decode():\n    assert decode_token("xyz")["sub"] == "user123"\n'
        },
    },
    {
        "name": "laya_qualidade_testes_insuficiente",
        "premise": (
            "Pedido do Prompt: implementar calculo de juros compostos com testes unitarios cobrindo cenarios financeiros. | "
            "Resultado Executado: criado src/interest.py; tests/test_interest.py apenas chama a funcao sem validar o valor retornado; "
            "nenhuma assercao de valor financeiro; cobertura de casos de erro ausente."
        ),
        "files": {
            "src/interest.py": 'def compound_interest(p, r, t):\n    return p * ((1 + r) ** t)\n',
            "tests/test_interest.py": 'from src.interest import compound_interest\ndef test_run():\n    compound_interest(1000, 0.05, 2)\n'
        },
    },
]

# ─── 3. Casos Positivos Exemplares (Passam pelo Needle 3 e Aprovados no Laya) ─
BLIND_POSITIVE_CASES = [
    {
        "name": "pos_funcao_add_com_testes_reais",
        "premise": (
            "Pedido do Prompt: implementar funcao add(a, b) com tipagem estrita e testes unitarios. | "
            "Resultado Executado: criado src/math_ops.py e tests/test_math_ops.py; "
            "assercoes cobrindo numeros positivos, negativos e zero; pytest passed."
        ),
        "files": {
            "src/math_ops.py": 'def add(a: int, b: int) -> int:\n    """Retorna a soma de dois inteiros."""\n    return a + b\n',
            "tests/test_math_ops.py": 'from src.math_ops import add\ndef test_add_positive():\n    assert add(2, 3) == 5\n    assert add(-1, 1) == 0\n    assert add(0, 0) == 0\n'
        },
    },
]

# Casos Positivos de Calibração Direta da Engine
CALIBRATION_POSITIVE_CASES = [
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

POSITIVE_CASES = CALIBRATION_POSITIVE_CASES

NEGATIVE_CASES = [
    (
        "mock_em_producao_com_retorno_fake",
        "Pedido do Prompt: entregar modulo de pagamento. | Resultado Executado: "
        "src/pagamento.py usa unittest.mock.MagicMock() dentro do codigo de producao; "
        "funcao cobrar() retorna 42 hardcoded (fallback sintetico); nenhum teste real "
        "exerce a integracao; chamada de rede substituida por stub fixo.",
    ),
    (
        "testes_tautologicos_assert_true",
        "Pedido do Prompt: entregar suíte de testes. | Resultado Executado: "
        "tests/test_app.py contem apenas assert True; assert 1 == 1; assert 'a' in 'abc'; "
        "zero import do modulo de producao; zero cobertura de caminho de erro; "
        "suites sempre passam sem exercitar codigo real.",
    ),
    (
        "excecoes_engolidas_sem_tratamento",
        "Pedido do Prompt: entregar cliente de banco. | Resultado Executado: "
        "src/db.py usa bare except: pass em todo ponto de I/O; falhas de conexao viram "
        "silencio; nenhum log nem retry; sem teste de falha; recurso de conexao nunca "
        "fechado em finally.",
    ),
    (
        "injecao_sql_sem_sanitizacao",
        "Pedido do Prompt: entregar busca de usuarios. | Resultado Executado: "
        "src/users.py monta query via f-string: f\"SELECT * FROM users WHERE id={user_id}\"; "
        "input do usuario entra direto sem parametrizacao; sem teste de rejeicao de "
        "aspas; vetor de injecao SQL exposto.",
    ),
]

# ─── Pares de Ordem (Monotonicidade: bom > ruim) ────────────────────────────
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

# ─── Pares de Estabilidade (Tolerância ao formato curto vs longo <= 0.65) ───
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
    return resp


async def _score(premise: str, rubric: str = STANDARD_RUBRIC) -> float:
    resp = await _score_resp(premise, rubric)
    return resp.score


class TestGoldenSetNeedle3FailFast(unittest.IsolatedAsyncioTestCase):
    """Testa interceptação determinística do Needle 3 AST Guardrail."""

    async def test_casos_negativos_interceptados_pelo_needle3(self):
        for case in BLIND_NEEDLE3_FAIL_FAST_CASES:
            with self.subTest(case=case["name"]):
                with _setup_temp_repo(case["files"]) as tmpdir:
                    resp = await openjev_score_and_diagnose(
                        premise=case["premise"],
                        workspace=tmpdir,
                    )
                    self.assertTrue(
                        resp["ast_blocked"],
                        f"Needle 3 NÃO bloqueou {case['name']}",
                    )
                    self.assertEqual(
                        resp["score"], 1.0,
                        f"Score de bloqueio AST deve ser 1.00 em {case['name']}, obteve {resp['score']}",
                    )
                    self.assertFalse(resp["passed"])
                    self.assertIsNotNone(resp["diagnosis"])
                    detected_rules = [v["rule_id"] for v in resp.get("ast_violations") or []]
                    self.assertIn(
                        case["expected_rule"],
                        detected_rules,
                        f"Regra {case['expected_rule']} não detectada em {case['name']} (detectadas: {detected_rules})",
                    )


class TestGoldenSetLayaSemantico(unittest.IsolatedAsyncioTestCase):
    """Testa decisões semânticas do Laya em código que passa direto pelo Needle 3."""

    async def test_casos_negativos_semanticos_reprovam_no_laya(self):
        for case in BLIND_LAYA_SEMANTIC_FAIL_CASES:
            with self.subTest(case=case["name"]):
                with _setup_temp_repo(case["files"]) as tmpdir:
                    resp = await openjev_score_and_diagnose(
                        premise=case["premise"],
                        workspace=tmpdir,
                    )
                    self.assertFalse(
                        resp["ast_blocked"],
                        f"FALSO POSITIVO DO NEEDLE 3: bloqueou AST indevidamente em {case['name']}",
                    )
                    self.assertFalse(
                        resp["passed"],
                        f"FALSO POSITIVO DO LAYA: aprovou entrega defeituosa em {case['name']} com score {resp['score']:.2f}",
                    )
                    self.assertLess(
                        resp["score"], THRESHOLD,
                        f"Score deve ser < {THRESHOLD} em {case['name']}, obteve {resp['score']:.2f}",
                    )

    async def test_casos_positivos_aprovam_no_laya(self):
        for case in BLIND_POSITIVE_CASES:
            with self.subTest(case=case["name"]):
                with _setup_temp_repo(case["files"]) as tmpdir:
                    resp = await openjev_score_and_diagnose(
                        premise=case["premise"],
                        workspace=tmpdir,
                    )
                    self.assertFalse(
                        resp["ast_blocked"],
                        f"FALSO POSITIVO DO NEEDLE 3: bloqueou AST indevidamente em {case['name']}",
                    )
                    self.assertTrue(
                        resp["passed"],
                        f"FALSO NEGATIVO: reprovou entrega exemplar em {case['name']} com score {resp['score']:.2f}",
                    )


class TestGoldenSetPositivosCalibracao(unittest.IsolatedAsyncioTestCase):
    """Entregas exemplares avaliadas na engine do Laya não podem ser reprovadas (falso negativo)."""

    async def test_positivos_ganham_nota_minima(self):
        for name, premise, rubric in CALIBRATION_POSITIVE_CASES:
            with self.subTest(case=name):
                resp = await _score_resp(premise, rubric)
                mass_ok, p_healthy, p_flawed = evaluate_borderline_mass_gate(resp.breakdown)
                passed = (resp.score >= THRESHOLD) or (resp.borderline and mass_ok)
                self.assertTrue(
                    passed,
                    f"FALSO NEGATIVO em '{name}': score={resp.score:.2f} < {THRESHOLD} (mass_gate={mass_ok}, p_healthy={p_healthy:.3f}, p_flawed={p_flawed:.3f})",
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
