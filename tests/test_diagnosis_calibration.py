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
from tests.test_score_diagnose import _make_git_repo, _make_repo_with_code


# ─── Golden: código de defeito conhecido → categoria esperada ────────────────

GOLDEN_CASES = [
    (
        "e_s_desprotegida",
        'def carregar_configuracao(caminho):\n'
        '    """Abre o arquivo de configuração.\n'
        "\n"
        "    Exemplo:\n"
        '        carregar_configuracao("app.ini")\n'
        '    """\n'
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

# Premissa standalone usada só no caminho mono (workspace limpo, sem símbolos):
# a nota é medida em tempo de execução e o threshold da banda é derivado dela,
# tornando o carve-out imune a drift de calibração do Laya.
BORDERLINE_PREMISE = (
    "Pedido do Prompt: corrigir bug off-by-one que descartava o último elemento da fatia. | "
    "Resultado Executado: alterado range(n) para range(n + 1) em src/slice_helper.py; "
    "teste de regressão tests/test_slice.py valida fatia completa com sucesso (out == expected) "
    "e último elemento out[-1] == xs[-1]; pytest 4/4 passed; suíte total 13/13 passed; "
    "docstring do helper atualizada."
)


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

    def test_diagnosis_result_confidence_invalida_levanta_validationerror(self):
        """Caminho de erro real do contrato: confidence fora de [0, 1] é rejeitada."""
        from pydantic import ValidationError
        from src.core.schemas import DiagnosisResult

        with self.assertRaises(ValidationError):
            DiagnosisResult(
                selected="seguranca",
                confidence=1.5,  # viola ge=0.0, le=1.0
                description="confiança fora do intervalo",
                diagnosis_latency_ms=1.0,
            )

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
        """Carve-out do caminho mono em 3 fases (imune a drift da nota):

        1. Mede a nota mono da premissa (workspace git limpo, sem símbolos).
        2. threshold = nota + 0.10 => nota dentro da banda de indiferença
           [3.20, 3.40) => aprovação com ressalva (approved_with_caveat),
           borderline=True e telemetria approved_as_borderline; diagnosis=None.
        3. threshold = nota + 0.50 => nota abaixo da banda => reprovado com
           diagnosis=None e mensagem do caminho mono.
        """
        # Fase 1: sondagem da nota mono
        probe = await openjev_score_and_diagnose(
            premise=BORDERLINE_PREMISE,
            workspace=_make_git_repo(),
            threshold=1.0,
        )
        self.assertNotIn("error", probe, f"sondagem mono falhou: {probe.get('error')}")
        mono_score = probe["score"]
        self.assertGreaterEqual(mono_score, 1.0)

        # Fase 2: banda de indiferença derivada da nota medida
        result = await openjev_score_and_diagnose(
            premise=BORDERLINE_PREMISE,
            workspace=_make_git_repo(),
            threshold=round(mono_score + 0.10, 2),
        )
        self.assertNotIn("error", result, f"avaliação da banda falhou: {result.get('error')}")
        self.assertTrue(
            result["borderline"],
            f"esperava borderline=True dentro da banda (score={result['score']}, "
            f"margin={result['score_margin']})",
        )
        self.assertTrue(
            result["passed"],
            f"nota na banda sem suspeita deve aprovar com ressalva (score={result['score']})",
        )
        self.assertTrue(
            result.get("approved_with_caveat"),
            "aprovação na banda DEVE carregar approved_with_caveat",
        )
        self.assertIn("approved_as_borderline", result)
        self.assertIsNone(result.get("diagnosis"))
        self.assertEqual(result["attempts"], 0)

        # Fase 3: abaixo da banda => reprovado, mono sem diagnosis
        below = await openjev_score_and_diagnose(
            premise=BORDERLINE_PREMISE,
            workspace=_make_git_repo(),
            threshold=round(mono_score + 0.50, 2),
        )
        self.assertNotIn("error", below, f"avaliação abaixo da banda falhou: {below.get('error')}")
        self.assertFalse(below["passed"])
        self.assertIsNone(
            below.get("diagnosis"),
            "caminho mono não tem suspeita do Needle 3 para diagnosticar",
        )
        self.assertIn("caminho mono", below.get("message", ""))
        self.assertGreaterEqual(below["attempts"], 1)

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
