"""Golden set de DIAGNÓSTICO — 4 defeitos de resposta conhecidos + carve-out borderline.

Mede a PRECISÃO do choice flat (10 categorias) contra o motor Laya real, sem mock
(AGENTS.md §5.2). Baseline P0 (código anterior, 26 opções finas, criteria=nomes,
instructions de roteamento): **1/4** — os 3 misses colapsaram em
stub_ou_mock_em_producao (conf. 0.60–0.996).

Calibração medida (ago/2026):
  - ≥ 11 opções finas (flat 26/30, todas as variantes): 1/4 — head colapsa
    na opção-savor (bucket temperature choice:11+ fora de [0.5, 5],
    head_max_len=192 trunca cada opção a ~5 tokens);
  - ≤ 5 opções concentradas: 1/4 e 2/4 (abaixo da barra);
  - **10 categorias flat EN: 3/4** ← configuração vigente (barra ≥ 3/4).

Estratégia: threshold=5.0 força reprovação sem borderline (massa na faixa
'at' mínima → borderline=false → o choice roda). Carve-out borderline testado
à parte com threshold=3.5 sobre premissa ~3.4: o choice SEMPRE roda; se
selected ∈ {sem_defeito_identificavel, evidencia_insuficiente} → aprovação
borderline; senão → reprovação com diagnosis concreta.

Anti-gaming: repositório git real por execução via subprocess; histórico
limpo por teste; nenhuma dependência é substituída por stub.
"""

import unittest

import src.mcp_server as _mcp_module
from src.core.schemas import (
    DEFECT_DESCRIPTIONS,
    DEFECT_MEMBERS,
    DEFECT_OPTIONS,
)
from src.mcp_server import NULL_DEFECT_OPTIONS, _score_history, openjev_score_and_diagnose
from tests.test_score_calibration import NEGATIVE_CASES, POSITIVE_CASES


# ─── Rótulo humano: premissão → categoria esperada ───────────────────────────

EXPECTED_DEFECT = {
    "mock_em_producao_com_retorno_fake": "mock_stub_em_producao",
    "testes_tautologicos_assert_true": "qualidade_de_testes",
    "excecoes_engolidas_sem_tratamento": "robustez_excecoes_recursos",
    "injecao_sql_sem_sanitizacao": "seguranca",
}

# Premissa com nota ~3.4 (positivo bugfix) para forçar fail+borderline em th=3.5
BORDERLINE_PREMISE = dict(POSITIVE_CASES)["bugfix_off_by_one_com_regressao"]


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


class TestGoldenDiagnostico(unittest.IsolatedAsyncioTestCase):
    """4 defeitos conhecidos devem ser selecionados corretamente (>= 3/4)."""

    @classmethod
    def setUpClass(cls):
        from tests.test_score_diagnose import _make_git_repo
        _ensure_engine_loaded()
        cls._make_git_repo = staticmethod(_make_git_repo)

    def setUp(self):
        _score_history.clear()
        self.ws = self._make_git_repo()

    def tearDown(self):
        _score_history.clear()

    async def test_diagnostico_defeito_conhecido(self):
        hits = []
        misses = []
        for name, premise in NEGATIVE_CASES:
            _score_history.clear()
            expected = EXPECTED_DEFECT[name]
            result = await openjev_score_and_diagnose(
                premise=premise,
                workspace=self.ws,
                threshold=5.0,
            )
            self.assertFalse(result["passed"], f"{name}: deveria reprovar em th=5.0")
            self.assertIsNotNone(result.get("diagnosis"), f"{name}: diagnosis ausente")
            selected = result["diagnosis"]["selected"]
            if selected == expected:
                hits.append(name)
            else:
                misses.append(
                    f"{name}: esperado={expected} obtido={selected} "
                    f"(conf={result['diagnosis']['confidence']})"
                )

        report = f"PRECISAO DIAGNOSTICO: {len(hits)}/4 (baseline P0=1/4, flat-10 calibrado=3/4)"
        if misses:
            report += " | misses: " + "; ".join(misses)
        # Barra calibrada do flat-10: >= 3/4
        self.assertGreaterEqual(len(hits), 3, report)

    async def test_diagnostico_emissao_borderline_carve_out(self):
        """Reprovação na fronteira (th=3.5, nota ~3.4):

        Contrato Borderline Mass Gate: se a massa saudável >= 0.85 e falhas <= 0.12,
        aprova com approved_as_borderline=True sem depender de choice com viés léxico.
        Se reprovado por falha real, o choice roda e gera diagnosis com defeito concreto.
        """
        result = await openjev_score_and_diagnose(
            premise=BORDERLINE_PREMISE,
            workspace=self.ws,
            threshold=3.5,
        )
        self.assertTrue(
            result["borderline"],
            f"esperava borderline=True em th=3.5 (score={result['score']}, "
            f"margin={result['score_margin']})",
        )

        if result["approved_as_borderline"]:
            self.assertTrue(result["passed"])
            self.assertEqual(result["attempts"], 0)
            self.assertIsNone(result.get("diagnosis"))
        else:
            self.assertFalse(result["passed"])
            self.assertIsNotNone(result.get("diagnosis"))
            selected = result["diagnosis"]["selected"]
            self.assertIn(selected, DEFECT_OPTIONS)
            self.assertGreaterEqual(result["attempts"], 1)

        # Contrato do único tool de score: origem da rubrica sempre presente
        self.assertEqual(result["rubric_source"], "catalog:entrega")

    async def test_diagnostico_reprovado_nao_borderline_nao_suprimido(self):
        """Fora da fronteira (th=5.0): diagnóstico roda e não é suprimido."""
        name, premise = NEGATIVE_CASES[0]
        result = await openjev_score_and_diagnose(
            premise=premise,
            workspace=self.ws,
            threshold=5.0,
        )
        self.assertFalse(result["passed"])
        self.assertFalse(result["borderline"])
        self.assertIsNone(result["diagnosis_suppressed"])
        self.assertFalse(result["approved_as_borderline"])
        self.assertIsNotNone(result["diagnosis"])
        self.assertIn(result["diagnosis"]["selected"], DEFECT_OPTIONS)
        self.assertIn(result["diagnosis"]["selected"], DEFECT_DESCRIPTIONS)
        self.assertEqual(
            result["diagnosis"]["candidate_defects"],
            DEFECT_MEMBERS.get(result["diagnosis"]["selected"], []),
        )
        # Distribuição cobre as 10 opções
        self.assertEqual(
            set(result["diagnosis"]["probabilities"]),
            set(DEFECT_OPTIONS),
        )


if __name__ == "__main__":
    unittest.main()
