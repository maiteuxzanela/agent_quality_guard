"""Testes de integração para openjev_score_and_diagnose — Screener Needle 3 + Noul (8 categorias).

Fluxo sem choice (set/2026):
  1. Guard AST (fail-fast determinístico) bloqueia mocks, testes tautológicos,
     exec/eval dinâmico, credenciais hardcoded etc. → reprovação imediata.
  2. Screener Needle 3 levanta suspeitas estruturais das 8 categorias no diff.
  3. JEV Noul arbitra cada suspeita (P >= 0.78 = confirmada).
  4. Decisão:
     - defeito confirmado pelo Noul => reprova com diagnosis;
     - zero suspeitas estruturais => aprova (nota < corte vira ressalva
       approved_with_caveat=true);
     - suspeita + nota >= corte => aprova;
     - suspeita + nota < corte => REPROVA com diagnosis (investigar no disco,
       mesmo com Noul negativo).
  5. Caminho mono (workspace sem símbolos AST no diff): nota >= corte aprova;
     nota < corte reprova com diagnosis=None e mensagem do caminho mono.

Todos os testes exercitam o engine Laya real (sem mock), conforme AGENTS.md §5.2.
O modelo é carregado uma única vez por sessão via singleton do mcp_server.

Estratégia de threshold:
  - threshold=1.0  → qualquer score real (1.0–5.0) passa → testa caminho de aprovação
  - threshold=5.0  → nenhum score real atinge            → testa reprovação/ressalva

Anti-gaming: usa repositório git real criado com subprocess para produzir
git_state autêntico — sem patch nem simulação do estado do repositório.
"""

import subprocess
import tempfile
import unittest
from pathlib import Path

import pytest

from src.core.schemas import (
    DEFECT_DESCRIPTIONS,
    DEFECT_MEMBERS,
    DEFECT_OPTIONS,
)
import src.mcp_server as _mcp_module
from src.mcp_server import _score_history, openjev_score_and_diagnose


# ─── Engine real: singleton carregado uma vez por sessão ─────────────────────

def _ensure_engine_loaded():
    """Garante que o engine Laya real está no singleton do mcp_server.
    Sem mock — carrega pesos reais da GPU/CPU (convaiinnovations/laya).
    """
    if _mcp_module._engine_instance is None:
        from src.decision.engine import OpenJevEngine
        engine = OpenJevEngine(
            model_id_or_path="convaiinnovations/laya",
            allow_cpu_fallback=True,
        )
        engine.load_model()
        _mcp_module._engine_instance = engine
    return _mcp_module._engine_instance


# ─── Helper: repositório git real ────────────────────────────────────────────

def _make_git_repo() -> str:
    """Cria repositório git real com commit inicial em tmpdir (caminho mono)."""
    tmp = tempfile.mkdtemp(prefix="jev_integ_")
    subprocess.run(["git", "init", tmp], check=True, capture_output=True)
    subprocess.run(["git", "-C", tmp, "config", "user.email", "test@jev.local"],
                   check=True, capture_output=True)
    subprocess.run(["git", "-C", tmp, "config", "user.name", "JEV Test"],
                   check=True, capture_output=True)
    (Path(tmp) / "README.md").write_text("# JEV integration test repo\n")
    subprocess.run(["git", "-C", tmp, "add", "."], check=True, capture_output=True)
    subprocess.run(["git", "-C", tmp, "commit", "-m", "init"], check=True, capture_output=True)
    return tmp


def _make_repo_with_code(source: str, filename: str = "entrega.py") -> str:
    """Repositório git real com um arquivo .py untracked (caminho AST do Screener)."""
    ws = _make_git_repo()
    (Path(ws) / filename).write_text(source, encoding="utf-8")
    return ws


# Código limpo: nenhum marcador do Screener e nenhuma violação do Guard AST
CLEAN_CODE = '''def somar(a, b):
    """Retorna a soma de dois números inteiros."""
    resultado = a + b
    return resultado
'''

# Código com suspeita estrutural garantida: E/S fora de bloco protegido
SUSPECT_CODE = '''def carregar_configuracao(caminho):
    """Abre o arquivo de configuração informado."""
    arquivo = open(caminho)
    conteudo = arquivo.read()
    return conteudo
'''

CLEAN_PREMISE = (
    "Pedido do Prompt: implementar soma(a, b). | Resultado Executado: função "
    "somar(a, b) implementada; teste assert somar(1, 2) == 3 aprovado no disco."
)
SUSPECT_PREMISE = (
    "Pedido do Prompt: entregar módulo de configuração. | Resultado Executado: "
    "função carregar_configuracao escrita no disco; fechamento de arquivo ainda "
    "pendente de refatoração."
)


# ─── Integração: Score + Screener + Noul com engine Laya real ────────────────

class TestScoreAndDiagnoseIntegration(unittest.IsolatedAsyncioTestCase):
    """Fluxo completo contra o engine Laya real — aprovação, ressalva e reprovação."""

    @classmethod
    def setUpClass(cls):
        _ensure_engine_loaded()
        cls.ws = _make_git_repo()

    def setUp(self):
        _score_history.clear()

    def tearDown(self):
        _score_history.clear()

    async def test_caminho_aprovado_sem_diagnostico(self):
        """Caminho mono com threshold=1.0: qualquer score real passa; diagnosis None."""
        result = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: implementar add(a, b). | Resultado Executado: função add(a, b): return a + b. Teste: assert add(1, 2) == 3. Cobertura 100%.",
            workspace=self.ws,
            threshold=1.0,
        )

        self.assertTrue(result["passed"])
        self.assertIsNone(result["diagnosis"])
        self.assertGreaterEqual(result["score"], 1.0)
        self.assertLessEqual(result["score"], 5.0)
        self.assertIn("score_latency_ms", result)
        self.assertIn("normalized_score", result)
        self.assertIn("breakdown", result)
        self.assertEqual(result["attempts"], 0)
        self.assertFalse(result["emergency_exit_unlocked"])
        self.assertFalse(result["approved_with_caveat"])

        # Contrato Plano 2: margem de incerteza e origem da rubrica sempre presentes
        self.assertIn("score_margin", result)
        self.assertIn("borderline", result)
        self.assertIsNotNone(result["score_margin"])
        self.assertIsNotNone(result["borderline"])
        self.assertEqual(result["task_type"], "codigo_producao")
        self.assertEqual(result["rubric_source"], "catalog:codigo_producao")

    async def test_caminho_mono_reprovado_sem_diagnostico(self):
        """Caminho mono com threshold=5.0: reprova com diagnosis=None e mensagem mono."""
        result = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: entregar módulo. | Resultado Executado: código entregue sem testes, sem tratamento de exceções, sem documentação.",
            workspace=self.ws,
            threshold=5.0,
        )

        self.assertFalse(result["passed"])
        self.assertIsNone(result["diagnosis"], "Caminho mono não tem suspeita do Needle 3 para diagnosticar")
        self.assertIn("caminho mono", result.get("message", ""))
        self.assertEqual(result["attempts"], 1)
        self.assertFalse(result["approved_with_caveat"])
        self.assertFalse(result["approved_as_borderline"])
        self.assertEqual(result["rubric_source"], "catalog:codigo_producao")

    async def test_suspeita_zerada_aprova_com_ressalva(self):
        """Needle 3 sem NENHUMA suspeita + nota < corte => aprovação com ressalva."""
        ws = _make_repo_with_code(CLEAN_CODE)
        result = await openjev_score_and_diagnose(
            premise=CLEAN_PREMISE,
            workspace=ws,
            threshold=5.0,
        )

        self.assertTrue(result["passed"])
        self.assertIsNone(result["diagnosis"])
        self.assertFalse(result["emergency_exit_unlocked"])
        if result["score"] < 5.0:
            self.assertTrue(
                result["approved_with_caveat"],
                f"nota {result['score']} < 5.0 sem suspeita deveria aprovar com ressalva",
            )
            self.assertIn("ressalva", result.get("message", ""))
        else:
            self.assertFalse(result["approved_with_caveat"])

    async def test_suspeita_com_score_contra_arbitragem_noul(self):
        """Suspeita + nota >= corte: Noul confirma => reprova; Noul refuta => aprova.

        A reprovação por defeito confirmado (P >= 0.78) vence a nota alta — o
        resultado depende da arbitragem real do Noul, sem mock.
        """
        ws = _make_repo_with_code(SUSPECT_CODE)
        result = await openjev_score_and_diagnose(
            premise=SUSPECT_PREMISE,
            workspace=ws,
            threshold=1.0,
        )

        if result["passed"]:
            # Noul refutou a hipótese (P < 0.78) e score_ok venceu a suspeita
            self.assertIsNone(result["diagnosis"])
            self.assertFalse(result["approved_with_caveat"])
        else:
            # Noul confirmou o defeito: reprova mesmo com nota >= corte
            self.assertIsNotNone(result["diagnosis"])
            self.assertEqual(result["diagnosis"]["selected"], "robustez_excecoes_recursos")
            self.assertGreaterEqual(result["diagnosis"]["confidence"], 0.78)
            self.assertIn("confirmado pelo Noul", result.get("message", ""))
            self.assertFalse(result["approved_with_caveat"])

    async def test_suspeita_com_score_baixo_reprova_para_investigacao(self):
        """Suspeita estrutural + nota < corte => REPROVA com diagnosis da categoria."""
        ws = _make_repo_with_code(SUSPECT_CODE)
        result = await openjev_score_and_diagnose(
            premise=SUSPECT_PREMISE,
            workspace=ws,
            threshold=5.0,
        )

        self.assertFalse(result["passed"])
        self.assertIsNotNone(result["diagnosis"], "suspeita + nota baixa exige diagnosis para investigar")

        diag = result["diagnosis"]

        # Categoria válida da taxonomia de 8 opções
        self.assertIn(diag["selected"], DEFECT_OPTIONS)
        self.assertIn(diag["selected"], DEFECT_DESCRIPTIONS)
        self.assertEqual(diag["description"], DEFECT_DESCRIPTIONS[diag["selected"]])
        self.assertTrue(len(diag["description"]) > 10)

        # Distribuição cobre a categoria vencedora (sem choice, sem 8 entradas)
        self.assertIn(diag["selected"], diag["probabilities"])
        self.assertGreaterEqual(len(diag["probabilities"]), 1)
        for value in diag["probabilities"].values():
            self.assertGreaterEqual(value, 0.0)
            self.assertLessEqual(value, 1.0)

        # Folhas candidatas da categoria para auto-correção
        self.assertEqual(diag["candidate_defects"], DEFECT_MEMBERS.get(diag["selected"], []))

        # Confiança no intervalo válido [0, 1]
        self.assertGreaterEqual(diag["confidence"], 0.0)
        self.assertLessEqual(diag["confidence"], 1.0)
        self.assertIn("diagnosis_latency_ms", diag)

        # Mensagem aponta o símbolo suspeito para investigação
        message = result.get("message", "")
        self.assertTrue(
            "investigar" in message or "confirmado pelo Noul" in message,
            f"mensagem deveria instruir investigação/confirmado: {message}",
        )
        self.assertIn("carregar_configuracao", message)

        # Contrato completo do ScoreDiagnoseResponse
        for campo in ("score", "normalized_score", "threshold", "breakdown",
                      "score_latency_ms", "attempts", "max_attempts", "emergency_exit_unlocked"):
            self.assertIn(campo, result)
        self.assertEqual(result["max_attempts"], 5)
        self.assertGreaterEqual(result["attempts"], 1)
        self.assertGreaterEqual(result["failed_stages"], 1)
        self.assertGreaterEqual(result["total_stages"], 1)
        self.assertIn("approved_as_borderline", result)
        self.assertFalse(result["approved_as_borderline"])
        self.assertFalse(result["approved_with_caveat"])
        self.assertIsInstance(result["score"], float)
        self.assertIsInstance(result["breakdown"], dict)


# ─── Integração: Anti-Gaming com git real ────────────────────────────────────

class TestAntiGamingIntegration(unittest.IsolatedAsyncioTestCase):
    """Testa lógica de anti-gaming com repositório git real e git_state autêntico."""

    @classmethod
    def setUpClass(cls):
        _ensure_engine_loaded()

    def setUp(self):
        _score_history.clear()
        self.ws = _make_git_repo()

    def tearDown(self):
        _score_history.clear()

    async def test_bloqueia_sem_novo_commit(self):
        """Após reprovação, nova chamada sem commit deve ser recusada."""
        from src.mcp_server import _get_workspace_git_signature
        git_state_real = _get_workspace_git_signature(self.ws)

        _score_history[self.ws] = {
            "passed": False,
            "attempts": 2,
            "git_state": git_state_real,
            "last_score": 2.50,
        }

        result = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: reavaliar entrega. | Resultado Executado: tentativa sem alterar o código.",
            workspace=self.ws,
        )

        self.assertIn("error", result)
        self.assertIn("AVALIAÇÃO RECUSADA", result["error"])
        self.assertFalse(result["emergency_exit_unlocked"])

    async def test_desbloqueia_apos_novo_commit(self):
        """Após commit real, git_state muda e avaliação deve ser aceita."""
        _score_history[self.ws] = {
            "passed": False,
            "attempts": 2,
            "git_state": "commit_antigo_sha:clean",
            "last_score": 2.50,
        }

        # Novo commit real altera o git_state
        (Path(self.ws) / "fix.py").write_text("def fix(): pass\n")
        subprocess.run(["git", "-C", self.ws, "add", "."], check=True, capture_output=True)
        subprocess.run(["git", "-C", self.ws, "commit", "-m", "fix: correcao real"],
                       check=True, capture_output=True)

        result = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: corrigir defeito. | Resultado Executado: código com nova correção commitada.",
            workspace=self.ws,
            threshold=1.0,
        )

        self.assertNotIn("error", result)
        self.assertIn("passed", result)
        self.assertTrue(result["passed"])

    async def test_circuit_breaker_apos_5_tentativas(self):
        """Saída de emergência deve ser ativada após MAX_ATTEMPTS=5 tentativas sem mudança."""
        from src.mcp_server import MAX_ATTEMPTS, _get_workspace_git_signature
        git_state_real = _get_workspace_git_signature(self.ws)

        _score_history[self.ws] = {
            "passed": False,
            "attempts": MAX_ATTEMPTS,
            "git_state": git_state_real,
            "last_score": 2.50,
        }

        result = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: refatorar módulo. | Resultado Executado: quinta tentativa sem refatoração real.",
            workspace=self.ws,
        )

        self.assertTrue(result.get("emergency_exit_unlocked"))
        self.assertIn("SAÍDA DE EMERGÊNCIA", result.get("message", ""))


# ─── Guard AST: reprovação fail-fast determinística (sem inferência) ─────────

class TestGuardAstFailFast(unittest.IsolatedAsyncioTestCase):
    """Violações determinísticas do Guard AST devem reprovar com diagnosis da categoria."""

    @classmethod
    def setUpClass(cls):
        _ensure_engine_loaded()

    def setUp(self):
        _score_history.clear()

    def tearDown(self):
        _score_history.clear()

    async def test_guard_bloqueia_teste_tautologico(self):
        ws = _make_repo_with_code(
            'def test_soma():\n    """Teste tautológico."""\n    assert True\n',
            filename="test_entrega.py",
        )
        result = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: entregar suíte de testes. | Resultado Executado: apenas assert True sem exercitar código.",
            workspace=ws,
            threshold=5.0,
        )

        self.assertFalse(result["passed"])
        self.assertTrue(result.get("ast_blocked"))
        self.assertIsNotNone(result["diagnosis"])
        self.assertEqual(result["diagnosis"]["selected"], "qualidade_de_testes")
        self.assertEqual(result["diagnosis"]["confidence"], 1.0)

    async def test_guard_bloqueia_exec_dinamico(self):
        ws = _make_repo_with_code(
            'def executar_query(sql):\n    """Executa query recebida."""\n    exec(sql)\n'
        )
        result = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: entregar busca. | Resultado Executado: query executada dinamicamente sem parametrização.",
            workspace=ws,
            threshold=5.0,
        )

        self.assertFalse(result["passed"])
        self.assertTrue(result.get("ast_blocked"))
        self.assertIsNotNone(result["diagnosis"])
        self.assertEqual(result["diagnosis"]["selected"], "seguranca")

    async def test_guard_bloqueia_credencial_hardcoded(self):
        ws = _make_repo_with_code(
            'def conectar():\n    """Retorna a chave de acesso da API."""\n'
            '    API_KEY = "sk-prod-abc123xyz"\n    return API_KEY\n'
        )
        result = await openjev_score_and_diagnose(
            premise="Pedido do Prompt: entregar cliente de API. | Resultado Executado: chave de API gravada no código-fonte.",
            workspace=ws,
            threshold=5.0,
        )

        self.assertFalse(result["passed"])
        self.assertTrue(result.get("ast_blocked"))
        self.assertIsNotNone(result["diagnosis"])
        self.assertEqual(result["diagnosis"]["selected"], "seguranca")


# ─── Integridade da Taxonomia (8 categorias, sem I/O, sem engine) ────────────

class TestTaxonomiaIntegridade(unittest.TestCase):
    """Verifica que a taxonomia flat de 8 categorias está corretamente definida.
    Sem I/O, sem rede, sem GPU — apenas lógica pura sobre os dicionários.
    """

    def test_total_de_opcoes_e_8(self):
        self.assertEqual(len(DEFECT_OPTIONS), 8)

    def test_todas_opcoes_tem_descricao(self):
        for defect in DEFECT_OPTIONS:
            self.assertIn(
                defect,
                DEFECT_DESCRIPTIONS,
                f"Defeito '{defect}' sem descrição em DEFECT_DESCRIPTIONS.",
            )
            self.assertTrue(
                len(DEFECT_DESCRIPTIONS[defect]) > 10,
                f"Descrição de '{defect}' está vazia ou muito curta.",
            )

    def test_sem_duplicatas_na_lista(self):
        self.assertEqual(len(DEFECT_OPTIONS), len(set(DEFECT_OPTIONS)),
                         "DEFECT_OPTIONS contém entradas duplicadas.")

    def test_categorias_principais_presentes(self):
        """Categorias essenciais cobrindo os domínios do pedido."""
        essenciais = [
            "logica_ou_regra",
            "robustez_excecoes_recursos",
            "seguranca",
            "qualidade_de_testes",
            "contrato_ambiente",
            "performance_escopo_estilo",
            "requisito_incompleto_ou_ausente",
            "documentacao_configuracao",
        ]
        for opt in essenciais:
            self.assertIn(opt, DEFECT_OPTIONS, f"Categoria '{opt}' ausente de DEFECT_OPTIONS.")

    def test_opcoes_nulas_removidas(self):
        """As 2 saídas nulas saíram do universo de decisão (Screener + Noul)."""
        for opt in ("sem_defeito_identificavel", "evidencia_insuficiente"):
            self.assertNotIn(opt, DEFECT_OPTIONS, f"Opção nula '{opt}' ainda presente.")
            self.assertNotIn(opt, DEFECT_DESCRIPTIONS)
            self.assertNotIn(opt, DEFECT_MEMBERS)

    def test_ordem_das_8_opcoes_estavel(self):
        """Ordem fixa da taxonomia (posições importam para orçamento de tokens)."""
        self.assertEqual(DEFECT_OPTIONS, [
            "logica_ou_regra",
            "robustez_excecoes_recursos",
            "seguranca",
            "qualidade_de_testes",
            "contrato_ambiente",
            "performance_escopo_estilo",
            "requisito_incompleto_ou_ausente",
            "documentacao_configuracao",
        ])

    def test_instructions_sem_nulas_e_curta(self):
        """Instrução do choice de auditoria não cita mais as nulas e segue curta."""
        from src.core.schemas import DIAG_CHOICE_INSTRUCTIONS
        self.assertNotIn("sem_defeito_identificavel", DIAG_CHOICE_INSTRUCTIONS)
        self.assertNotIn("evidencia_insuficiente", DIAG_CHOICE_INSTRUCTIONS)
        self.assertLess(len(DIAG_CHOICE_INSTRUCTIONS), 200)

    def test_members_cobrem_folhas_sem_colisao(self):
        """DEFECT_MEMBERS cobre as folhas de cada categoria sem sobreposição."""
        from src.core.schemas import DEFECT_MEMBERS
        self.assertEqual(set(DEFECT_MEMBERS), set(DEFECT_OPTIONS))
        all_leaves = []
        for cat, leaves in DEFECT_MEMBERS.items():
            all_leaves.extend(leaves)
        self.assertEqual(len(all_leaves), len(set(all_leaves)),
                         "Folha duplicada entre categorias de DEFECT_MEMBERS.")
        self.assertIn("funcionalidade_do_pedido_ausente", DEFECT_MEMBERS["requisito_incompleto_ou_ausente"])

    def test_tool_catalog_reflete_screener_noul(self):
        from src.core.tools import get_tool_definition
        tool = get_tool_definition("openjev_score_and_diagnose")
        self.assertEqual(tool["function"]["name"], "openjev_score_and_diagnose")
        params = tool["function"]["parameters"]["properties"]
        self.assertIn("premise", params)
        self.assertIn("threshold", params)
        self.assertIn("workspace", params)
        # Descrição reflete o contrato novo: Screener + Noul, 8 categorias, sem choice
        desc = tool["function"]["description"]
        self.assertIn("8", desc)
        self.assertIn("Screener", desc)
        self.assertIn("approved_with_caveat", desc)
        self.assertNotIn("choice flat", desc)
        self.assertNotIn("L1", desc)
        self.assertNotIn("L2", desc)

    def test_constantes_null_choice_removidas(self):
        """As constantes do carve-out de nulas saíram do mcp_server."""
        self.assertFalse(hasattr(_mcp_module, "NULL_DEFECT_OPTIONS"))
        self.assertFalse(hasattr(_mcp_module, "NULL_CHOICE_MIN_CONFIDENCE"))

    def test_score_gate_shared_max_attempts(self):
        """MAX_ATTEMPTS vem de score_gate (fonte única para MCP e FastAPI)."""
        from src.core.score_gate import MAX_ATTEMPTS as gate_attempts
        from src.mcp_server import MAX_ATTEMPTS as mcp_attempts
        self.assertEqual(gate_attempts, 5)
        self.assertEqual(mcp_attempts, gate_attempts)

    def test_score_gate_validate_premise_patterns(self):
        from src.core.score_gate import validate_score_premise
        self.assertIsNone(validate_score_premise("Pedido: x | Resultado: y"))
        self.assertIsNotNone(validate_score_premise("só prosa sem padrão"))
        self.assertIsNotNone(validate_score_premise("Pedido: " + "x" * 1600 + " | Resultado: y"))
        self.assertIsNotNone(validate_score_premise(""))


# ─── Contrato Pydantic dos schemas (sem engine, sem I/O) ─────────────────────

class TestPydanticSchemaIntegration(unittest.TestCase):
    """Verifica que os schemas Pydantic instanciam, validam e serializam corretamente.
    Sem mock e sem dicionários que bypassam validação.
    """

    def test_diagnosis_result_instancia_e_serializa(self):
        from src.core.schemas import DiagnosisResult
        probs = {d: round(1 / len(DEFECT_OPTIONS), 4) for d in DEFECT_OPTIONS}
        diag = DiagnosisResult(
            selected="qualidade_de_testes",
            confidence=0.74,
            probabilities=probs,
            description=DEFECT_DESCRIPTIONS["qualidade_de_testes"],
            diagnosis_latency_ms=42.5,
        )
        dumped = diag.model_dump()
        self.assertEqual(dumped["selected"], "qualidade_de_testes")
        self.assertAlmostEqual(dumped["confidence"], 0.74)
        self.assertEqual(len(dumped["probabilities"]), len(DEFECT_OPTIONS))
        self.assertTrue(len(dumped["description"]) > 10)

    def test_diagnosis_result_todas_categorias_serializam(self):
        """Todas as 8 categorias são válidas no schema."""
        from src.core.schemas import DiagnosisResult
        for opt in DEFECT_OPTIONS:
            diag = DiagnosisResult(
                selected=opt,
                confidence=0.80,
                probabilities={opt: 0.80},
                description=DEFECT_DESCRIPTIONS[opt],
                diagnosis_latency_ms=10.0,
            )
            dumped = diag.model_dump()
            self.assertEqual(dumped["selected"], opt)

    def test_score_diagnose_response_aprovado_serializa(self):
        from src.core.schemas import ScoreDiagnoseResponse
        resp = ScoreDiagnoseResponse(
            score=4.10,
            normalized_score=0.775,
            passed=True,
            threshold=3.40,
            breakdown={"good quality": 0.6, "exceptional": 0.4},
            score_latency_ms=120.0,
            diagnosis=None,
            attempts=0,
            max_attempts=5,
            emergency_exit_unlocked=False,
        )
        dumped = resp.model_dump()
        self.assertTrue(dumped["passed"])
        self.assertIsNone(dumped["diagnosis"])
        self.assertEqual(dumped["attempts"], 0)
        self.assertFalse(dumped["approved_as_borderline"])
        self.assertFalse(dumped["approved_with_caveat"])

    def test_score_diagnose_response_reprovado_com_diagnosis(self):
        from src.core.schemas import DiagnosisResult, ScoreDiagnoseResponse
        diag = DiagnosisResult(
            selected="requisito_incompleto_ou_ausente",
            confidence=0.65,
            probabilities={"requisito_incompleto_ou_ausente": 0.65},
            description=DEFECT_DESCRIPTIONS["requisito_incompleto_ou_ausente"],
            candidate_defects=["funcionalidade_do_pedido_ausente"],
            diagnosis_latency_ms=33.1,
        )
        resp = ScoreDiagnoseResponse(
            score=2.80,
            normalized_score=0.45,
            passed=False,
            threshold=3.40,
            breakdown={"critical flaws": 0.3, "poor quality": 0.3, "acceptable": 0.4},
            score_latency_ms=98.0,
            diagnosis=diag,
            attempts=2,
            max_attempts=5,
            emergency_exit_unlocked=False,
        )
        dumped = resp.model_dump()
        self.assertFalse(dumped["passed"])
        self.assertEqual(dumped["diagnosis"]["selected"], "requisito_incompleto_ou_ausente")
        self.assertEqual(dumped["diagnosis"]["candidate_defects"], ["funcionalidade_do_pedido_ausente"])
        self.assertEqual(dumped["attempts"], 2)

    def test_diagnosis_result_rejeita_confidence_invalida(self):
        """DiagnosisResult deve lançar ValidationError para confidence > 1.0."""
        from pydantic import ValidationError
        from src.core.schemas import DiagnosisResult
        with self.assertRaises(ValidationError):
            DiagnosisResult(
                selected="erro_off_by_one",
                confidence=1.5,  # viola ge=0.0, le=1.0
                probabilities={},
                description="Test",
                diagnosis_latency_ms=10.0,
            )


# ─── Testes parametrizados: reprovação com defeito conhecido da taxonomia ────

@pytest.mark.asyncio
@pytest.mark.parametrize("source,filename,premise,descricao,esperado", [
    (
        'def test_soma():\n    """Teste tautológico."""\n    assert True\n',
        "test_entrega.py",
        "Pedido do Prompt: entregar suíte. | Resultado Executado: código sem nenhum arquivo de teste real. Zero coverage.",
        "ausencia_de_testes",
        "qualidade_de_testes",
    ),
    (
        'def executar_query(sql):\n    """Executa query recebida."""\n    exec(sql)\n',
        "entrega.py",
        "Pedido do Prompt: entregar busca. | Resultado Executado: função executa entrada do usuário via exec sem sanitização.",
        "injecao_dinamica",
        "seguranca",
    ),
    (
        'def conectar():\n    """Retorna a chave de acesso."""\n    API_KEY = "sk-prod-abc123xyz"\n    return API_KEY\n',
        "entrega.py",
        "Pedido do Prompt: entregar cliente. | Resultado Executado: API key hardcoded: API_KEY = 'sk-prod-abc123xyz' no código-fonte.",
        "credencial_hardcoded",
        "seguranca",
    ),
])
async def test_reprovado_retorna_categoria_valida(source, filename, premise, descricao, esperado):
    """Reprovação fail-fast deve devolver sempre uma categoria válida das 8 opções."""
    _ensure_engine_loaded()
    _score_history.clear()
    ws = _make_repo_with_code(source, filename=filename)

    result = await openjev_score_and_diagnose(
        premise=premise,
        workspace=ws,
        threshold=5.0,  # força reprovação
    )

    assert not result["passed"], f"[{descricao}] Esperava reprovação com threshold=5.0"
    assert result["diagnosis"] is not None, f"[{descricao}] diagnosis não deve ser None"

    diag = result["diagnosis"]

    assert diag["selected"] in DEFECT_OPTIONS, (
        f"[{descricao}] '{diag['selected']}' não é uma opção válida das 8 opções flat"
    )
    assert diag["selected"] == esperado, (
        f"[{descricao}] esperado='{esperado}' obtido='{diag['selected']}'"
    )
    assert diag["selected"] in DEFECT_DESCRIPTIONS, (
        f"[{descricao}] '{diag['selected']}' sem descrição em DEFECT_DESCRIPTIONS"
    )
    assert 0.0 <= diag["confidence"] <= 1.0
    assert diag["selected"] in diag["probabilities"], (
        f"[{descricao}] distribuição deve conter a categoria selecionada"
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
