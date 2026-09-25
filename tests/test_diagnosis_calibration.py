"""Golden set de DIAGNÓSTICO — 4 defeitos de código conhecidos, sem choice.

Mede a PRECISÃO do caminho Screener Needle 3 + Noul contra o motor Laya real,
sem mock (AGENTS.md §5.2). Substitui o golden antigo do choice flat (10
categorias), que foi expurgado do fluxo de score em set/2026.

Contrato novo (mcp_server):
  1. Guard AST bloqueia deterministicamente mocks, testes tautológicos,
     exec/eval dinâmico e credenciais hardcoded → categoria garantida.
  2. Screener Needle 3 levanta suspeitas estruturais; Noul arbitra (P >= 0.78).
  3. Reprovação com nota < corte: diagnosis = defeito confirmado OU a suspeita
     estrutural de maior probabilidade (a ser INVESTIGADA mesmo com Noul negativo).
  4. Caminho mono (workspace sem símbolos AST): NÃO há diagnosis — nota abaixo
     do corte reprova com diagnosis=None e mensagem do caminho mono.

Cada caso do golden usa um repositório git real com código-fonte que dispara
UMA categoria estrutural conhecida — a categoria esperada é estável seja pelo
Guard (fail-fast), seja pelo Screener (suspeita única).

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
from src.mcp_server import _score_history, openjev_score_and_diagnose
from tests.test_score_calibration import POSITIVE_CASES
from tests.test_score_diagnose import _make_git_repo, _make_repo_with_code


# ─── Golden: código de defeito conhecido → categoria esperada ────────────────

GOLDEN_CASES = [
    (
        "e_s_desprotegida",
        'def carregar_configuracao(caminho):\n    """Abre o arquivo de configuração."""\n'
        '    arquivo = open(caminho)\n    return arquivo.read()\n',
        "entrega.py",
        "Pedido do Prompt: entregar módulo de configuração. | Resultado Executado: arquivo aberto sem proteção nem fechamento no disco.",
        "robustez_excecoes_recursos",
    ),
    (
        "exec_dinamico",
        'def executar_script(codigo):\n    """Executa o código recebido."""\n    exec(codigo)\n',
        "entrega.py",
        "Pedido do Prompt: entregar executor. | Resultado Executado: código arbitrário executado via exec() sem restrição.",
        "seguranca",
    ),
    (
        "loop_sem_parada",
        'def aguardar_evento(limite):\n    """Aguarda até o limite informado."""\n'
        '    atual = 0\n    while True:\n        atual += 1\n        if atual >= limite:\n            return atual\n',
        "entrega.py",
        "Pedido do Prompt: entregar loop de eventos. | Resultado Executado: laço while True implementado no disco.",
        "logica_ou_regra",
    ),
    (
        "teste_tautologico",
        'def test_soma():\n    """Teste tautológico."""\n    assert True\n',
        "test_entrega.py",
        "Pedido do Prompt: entregar suíte de testes. | Resultado Executado: apenas assert True sem exercitar o código.",
        "qualidade_de_testes",
    ),
]

# Premissa com nota ~3.4 (positivo bugfix) para forçar faixa borderline em th=3.5
BORDERLINE_PREMISE = {case[0]: case[1] for case in POSITIVE_CASES}["bugfix_off_by_one_com_regressao"]


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
    """4 defeitos conhecidos devem apontar a categoria estrutural esperada (4/4)."""

    @classmethod
    def setUpClass(cls):
        _ensure_engine_loaded()

    def setUp(self):
        _score_history.clear()

    def tearDown(self):
        _score_history.clear()

    async def test_diagnostico_defeito_conhecido(self):
        hits = []
        misses = []
        for name, source, filename, premise, expected in GOLDEN_CASES:
            _score_history.clear()
            ws = _make_repo_with_code(source, filename=filename)
            result = await openjev_score_and_diagnose(
                premise=premise,
                workspace=ws,
                threshold=5.0,
            )
            self.assertFalse(result["passed"], f"{name}: deveria reprovar em th=5.0")
            self.assertIsNotNone(result.get("diagnosis"), f"{name}: diagnosis ausente")
            selected = result["diagnosis"]["selected"]
            if selected == expected:
                hits.append(name)
            else:
                misses.append(f"{name}: esperado={expected} obtido={selected}")

        report = (
            f"PRECISAO DIAGNOSTICO: {len(hits)}/4 "
            "(Screener+Noul sem choice; golden antigo do flat-10: 3/4)"
        )
        if misses:
            report += " | misses: " + "; ".join(misses)
        self.assertGreaterEqual(len(hits), 4, report)

    async def test_diagnostico_emissao_borderline_carve_out(self):
        """Reprovação na fronteira (th=3.5, nota ~3.4) no caminho mono:

        Contrato Borderline Mass Gate: se a massa saudável >= 0.85 e falhas <= 0.12,
        aprova com approved_as_borderline=True. Sem símbolos AST no diff não há
        Screener — reprovado fora da massa sai com diagnosis=None (caminho mono).
        """
        ws = _make_git_repo()
        result = await openjev_score_and_diagnose(
            premise=BORDERLINE_PREMISE,
            workspace=ws,
            threshold=3.5,
        )
        self.assertTrue(
            result["borderline"],
            f"esperava borderline=True em th=3.5 (score={result['score']}, "
            f"margin={result['score_margin']})",
        )

        if result["passed"]:
            # Massa borderline aprovou (ou nota >= corte)
            self.assertIn("approved_as_borderline", result)
            self.assertIsNone(result.get("diagnosis"))
            self.assertEqual(result["attempts"], 0)
        else:
            self.assertFalse(result["passed"])
            self.assertIsNone(
                result.get("diagnosis"),
                "caminho mono não tem suspeita do Needle 3 para diagnosticar",
            )
            self.assertIn("caminho mono", result.get("message", ""))
            self.assertGreaterEqual(result["attempts"], 1)

        # Contrato do único tool de score: origem da rubrica sempre presente
        self.assertEqual(result["rubric_source"], "catalog:codigo_producao")

    async def test_diagnostico_reprovado_nao_borderline_nao_suprimido(self):
        """Fora da fronteira (th=5.0) com símbolo AST suspeito: diagnosis presente."""
        name, source, filename, premise, expected = GOLDEN_CASES[0]
        ws = _make_repo_with_code(source, filename=filename)
        result = await openjev_score_and_diagnose(
            premise=premise,
            workspace=ws,
            threshold=5.0,
        )
        self.assertFalse(result["passed"])
        self.assertFalse(result["borderline"])
        self.assertIsNone(result.get("diagnosis_suppressed"))
        self.assertFalse(result["approved_as_borderline"])
        self.assertFalse(result.get("approved_with_caveat", False))
        self.assertIsNotNone(result["diagnosis"])
        self.assertEqual(result["diagnosis"]["selected"], expected)
        self.assertIn(result["diagnosis"]["selected"], DEFECT_OPTIONS)
        self.assertIn(result["diagnosis"]["selected"], DEFECT_DESCRIPTIONS)
        self.assertEqual(
            result["diagnosis"]["candidate_defects"],
            DEFECT_MEMBERS.get(result["diagnosis"]["selected"], []),
        )
        # Sem choice: a distribuição cobre apenas a categoria apontada
        self.assertIn(result["diagnosis"]["selected"], result["diagnosis"]["probabilities"])
        self.assertGreaterEqual(len(result["diagnosis"]["probabilities"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
