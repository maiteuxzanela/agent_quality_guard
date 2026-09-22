"""Testes de integração para openjev_score_and_diagnose — Score + Choice flat (26 opções).

Todos os testes exercitam o engine Laya real (sem mock), conforme AGENTS.md §5.2.
O modelo é carregado uma única vez por sessão via singleton do mcp_server.

Estratégia de threshold:
  - threshold=1.0  → qualquer score real (1.0–5.0) passa → testa caminho de aprovação
  - threshold=5.0  → nenhum score real atinge            → testa caminho de reprovação + diagnóstico

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
    """Cria repositório git real com commit inicial em tmpdir."""
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


# ─── Integração: Score + Diagnóstico com engine Laya real ────────────────────

class TestScoreAndDiagnoseIntegration(unittest.IsolatedAsyncioTestCase):
    """Fluxo completo contra o engine Laya real — aprovação e reprovação."""

    @classmethod
    def setUpClass(cls):
        _ensure_engine_loaded()
        cls.ws = _make_git_repo()

    def setUp(self):
        _score_history.clear()

    def tearDown(self):
        _score_history.clear()

    async def test_caminho_aprovado_sem_diagnostico(self):
        """threshold=1.0 garante que qualquer score real passa; diagnosis deve ser None."""
        result = await openjev_score_and_diagnose(
            premise="Função add(a, b): return a + b. Teste: assert add(1, 2) == 3. Cobertura 100%.",
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

    async def test_caminho_reprovado_retorna_diagnostico_flat(self):
        """threshold=5.0 garante reprovação real; diagnosis deve conter um defeito válido."""
        result = await openjev_score_and_diagnose(
            premise="Código entregue sem testes, sem tratamento de exceções, sem documentação.",
            workspace=self.ws,
            threshold=5.0,
        )

        self.assertFalse(result["passed"])
        self.assertIsNotNone(result["diagnosis"])

        diag = result["diagnosis"]

        # Defeito selecionado deve ser uma das 26 opções válidas
        self.assertIn(diag["selected"], DEFECT_OPTIONS)

        # Descrição deve existir no dicionário centralizado
        self.assertIn(diag["selected"], DEFECT_DESCRIPTIONS)
        self.assertEqual(diag["description"], DEFECT_DESCRIPTIONS[diag["selected"]])
        self.assertTrue(len(diag["description"]) > 10)

        # Distribuição deve cobrir todas as 26 opções
        self.assertEqual(len(diag["probabilities"]), len(DEFECT_OPTIONS))
        for defect in DEFECT_OPTIONS:
            self.assertIn(defect, diag["probabilities"])

        # Confiança no intervalo válido [0, 1]
        self.assertGreaterEqual(diag["confidence"], 0.0)
        self.assertLessEqual(diag["confidence"], 1.0)
        self.assertIn("diagnosis_latency_ms", diag)

        # Contrato completo do ScoreDiagnoseResponse
        for campo in ("score", "normalized_score", "threshold", "breakdown",
                      "score_latency_ms", "attempts", "max_attempts", "emergency_exit_unlocked"):
            self.assertIn(campo, result)
        self.assertEqual(result["max_attempts"], 10)
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
            premise="Tentativa sem alterar o código.",
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
            premise="Código com nova correção commitada.",
            workspace=self.ws,
            threshold=1.0,
        )

        self.assertNotIn("error", result)
        self.assertIn("passed", result)
        self.assertTrue(result["passed"])

    async def test_circuit_breaker_apos_10_tentativas(self):
        """Saída de emergência deve ser ativada após 10 tentativas sem mudança."""
        from src.mcp_server import _get_workspace_git_signature
        git_state_real = _get_workspace_git_signature(self.ws)

        _score_history[self.ws] = {
            "passed": False,
            "attempts": 10,
            "git_state": git_state_real,
            "last_score": 2.50,
        }

        result = await openjev_score_and_diagnose(
            premise="Décima tentativa sem refatoração real.",
            workspace=self.ws,
        )

        self.assertTrue(result.get("emergency_exit_unlocked"))
        self.assertIn("SAÍDA DE EMERGÊNCIA", result.get("message", ""))


# ─── Integridade da Taxonomia Flat (sem I/O, sem engine) ─────────────────────

class TestTaxonomiaFlatIntegridade(unittest.TestCase):
    """Verifica que a taxonomia flat de 26 defeitos está corretamente definida.
    Sem I/O, sem rede, sem GPU — apenas lógica pura sobre os dicionários.
    """

    def test_total_de_opcoes_e_26(self):
        self.assertEqual(len(DEFECT_OPTIONS), 26)

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

    def test_novas_opcoes_presentes(self):
        """Verifica que as 6 novas opções foram incluídas."""
        novas = [
            "incompatibilidade_de_ambiente_ou_runtime",
            "gargalo_de_performance_ou_complexidade",
            "alucinacao_de_efeitos_colaterais_externos",
            "desvio_de_escopo_ou_overengineering",
            "inconsistencia_estilistica_ou_linting_estrito",
            "stub_ou_mock_em_producao",
        ]
        for opt in novas:
            self.assertIn(opt, DEFECT_OPTIONS, f"Nova opção '{opt}' ausente de DEFECT_OPTIONS.")

    def test_tool_catalog_reflete_choice_flat(self):
        from src.core.tools import get_tool_definition
        tool = get_tool_definition("openjev_score_and_diagnose")
        self.assertEqual(tool["function"]["name"], "openjev_score_and_diagnose")
        params = tool["function"]["parameters"]["properties"]
        self.assertIn("premise", params)
        self.assertIn("threshold", params)
        self.assertIn("workspace", params)
        # Garante que a descrição foi atualizada para choice flat
        desc = tool["function"]["description"]
        self.assertIn("26", desc)
        self.assertNotIn("L1", desc)
        self.assertNotIn("L2", desc)


# ─── Contrato Pydantic do novo DiagnosisResult (sem engine, sem I/O) ─────────

class TestPydanticSchemaIntegration(unittest.TestCase):
    """Verifica que os schemas Pydantic instanciam, validam e serializam corretamente.
    Sem mock e sem dicionários que bypassam validação.
    """

    def test_diagnosis_result_flat_instancia_e_serializa(self):
        from src.core.schemas import DiagnosisResult
        probs = {d: round(1 / len(DEFECT_OPTIONS), 4) for d in DEFECT_OPTIONS}
        diag = DiagnosisResult(
            selected="falta_cobertura_caminhos_de_erro",
            confidence=0.74,
            probabilities=probs,
            description=DEFECT_DESCRIPTIONS["falta_cobertura_caminhos_de_erro"],
            diagnosis_latency_ms=42.5,
        )
        dumped = diag.model_dump()
        self.assertEqual(dumped["selected"], "falta_cobertura_caminhos_de_erro")
        self.assertAlmostEqual(dumped["confidence"], 0.74)
        self.assertEqual(len(dumped["probabilities"]), len(DEFECT_OPTIONS))
        self.assertTrue(len(dumped["description"]) > 10)

    def test_diagnosis_result_nova_opcao_serializa(self):
        """Verifica que as 6 novas opções são válidas no schema."""
        from src.core.schemas import DiagnosisResult
        novas = [
            "incompatibilidade_de_ambiente_ou_runtime",
            "gargalo_de_performance_ou_complexidade",
            "alucinacao_de_efeitos_colaterais_externos",
            "desvio_de_escopo_ou_overengineering",
            "inconsistencia_estilistica_ou_linting_estrito",
            "stub_ou_mock_em_producao",
        ]
        for opt in novas:
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
            threshold=3.60,
            breakdown={"good quality": 0.6, "exceptional": 0.4},
            score_latency_ms=120.0,
            diagnosis=None,
            attempts=0,
            max_attempts=10,
            emergency_exit_unlocked=False,
        )
        dumped = resp.model_dump()
        self.assertTrue(dumped["passed"])
        self.assertIsNone(dumped["diagnosis"])
        self.assertEqual(dumped["attempts"], 0)

    def test_score_diagnose_response_reprovado_com_diagnosis(self):
        from src.core.schemas import DiagnosisResult, ScoreDiagnoseResponse
        diag = DiagnosisResult(
            selected="stub_ou_mock_em_producao",
            confidence=0.65,
            probabilities={"stub_ou_mock_em_producao": 0.65},
            description=DEFECT_DESCRIPTIONS["stub_ou_mock_em_producao"],
            diagnosis_latency_ms=33.1,
        )
        resp = ScoreDiagnoseResponse(
            score=2.80,
            normalized_score=0.45,
            passed=False,
            threshold=3.60,
            breakdown={"critical flaws": 0.3, "poor quality": 0.3, "acceptable": 0.4},
            score_latency_ms=98.0,
            diagnosis=diag,
            attempts=2,
            max_attempts=10,
            emergency_exit_unlocked=False,
        )
        dumped = resp.model_dump()
        self.assertFalse(dumped["passed"])
        self.assertEqual(dumped["diagnosis"]["selected"], "stub_ou_mock_em_producao")
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


# ─── Testes parametrizados: diagnóstico real contra premissas temáticas ───────

@pytest.mark.asyncio
@pytest.mark.parametrize("premise,descricao", [
    (
        "Código sem nenhum arquivo de teste. Nenhuma função de assert. Zero coverage.",
        "ausencia_de_testes",
    ),
    (
        "Função executa SQL via f-string com input do usuário sem sanitização.",
        "injecao_sql",
    ),
    (
        "API key hardcoded: API_KEY = 'sk-prod-abc123xyz' no código-fonte.",
        "credencial_hardcoded",
    ),
])
async def test_diagnostico_flat_retorna_opcao_valida(premise, descricao):
    """Verifica que o choice flat sempre retorna um defeito válido da taxonomia."""
    _ensure_engine_loaded()
    _score_history.clear()
    ws = _make_git_repo()

    result = await openjev_score_and_diagnose(
        premise=premise,
        workspace=ws,
        threshold=5.0,  # força reprovação
    )

    assert not result["passed"], f"[{descricao}] Esperava reprovação com threshold=5.0"
    assert result["diagnosis"] is not None, f"[{descricao}] diagnosis não deve ser None"

    diag = result["diagnosis"]

    assert diag["selected"] in DEFECT_OPTIONS, (
        f"[{descricao}] '{diag['selected']}' não é uma opção válida das 26 opções flat"
    )
    assert diag["selected"] in DEFECT_DESCRIPTIONS, (
        f"[{descricao}] '{diag['selected']}' sem descrição em DEFECT_DESCRIPTIONS"
    )
    assert 0.0 <= diag["confidence"] <= 1.0
    assert len(diag["probabilities"]) == len(DEFECT_OPTIONS), (
        f"[{descricao}] Distribuição deve ter {len(DEFECT_OPTIONS)} entradas, "
        f"encontrou {len(diag['probabilities'])}"
    )


if __name__ == "__main__":
    unittest.main(verbosity=2)
