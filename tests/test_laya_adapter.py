"""Testes do LayaAdapter: mapeamento de escala ordinal (0-4 → 1-5), thresholds recalibrados
e comportamento de fallback.

SEM mocks (AGENTS.md §5.2): a lógica de decisão (mapeamento de escala, corte de
noul, temperatura+argmax de choice) vive em funções PURAS de produção —
`map_score_ordinal`, `decide_noul`, `finalize_choice` — testadas diretamente com
dados de entrada fixos (matemática pura, sem I/O). O contrato ponta-a-ponta com o
modelo é coberto por `TestLayaAdapterIntegracao`, que usa o Laya REAL do disco.

Caminho de falha de load: `test_load_falha_device_invalido_sem_cpu_fallback`
exercita o caminho REAL — device "cuda:9" inválido derruba a GPU e
`allow_cpu_fallback=False` impede o retry em CPU → `InferenceError`
(laya instalado; sem patch.dict).
"""

import pytest

from src.core.schemas import ChoiceRequest, NoulRequest, ScoreRequest
from src.decision.laya_adapter import (
    LAYA_NOUL_THRESHOLD_DEFAULT,
    LAYA_ORDINAL_OFFSET,
    LAYA_SCORE_THRESHOLD_DEFAULT,
    LayaAdapter,
    decide_noul,
    finalize_choice,
    map_score_ordinal,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_adapter(checkpoint="english", device="cpu") -> LayaAdapter:
    return LayaAdapter(checkpoint=checkpoint, device=device, allow_cpu_fallback=True)


# ─── Testes de Thresholds Recalibrados ───────────────────────────────────────

class TestLayaThresholds:
    """Verifica que os thresholds padrão do Laya estão recalibrados corretamente."""

    def test_noul_threshold_recalibrado(self):
        """Threshold noul deve ser 0.78 (vs 0.85 do DeBERTa)."""
        assert LAYA_NOUL_THRESHOLD_DEFAULT == 0.78, (
            f"Esperado 0.78, obtido {LAYA_NOUL_THRESHOLD_DEFAULT}. "
            "Delta empírico DeBERTa(0.954) - Laya(0.827) = 0.127."
        )

    def test_score_threshold_recalibrado(self):
        """Threshold score deve ser 3.40 (vs 3.75 do DeBERTa)."""
        assert LAYA_SCORE_THRESHOLD_DEFAULT == 3.40, (
            f"Esperado 3.40, obtido {LAYA_SCORE_THRESHOLD_DEFAULT}. "
            "Delta empírico DeBERTa(3.68) - Laya(3.15) = 0.53."
        )

    def test_laya_ordinal_offset(self):
        """Offset de mapeamento ordinal Laya(0-4) → JEV(1-5) deve ser 1.0."""
        assert LAYA_ORDINAL_OFFSET == 1.0


class TestTemperatureNaoEStub:
    """temperature da API ChoiceRequest deve surtir efeito no caminho Laya."""

    def test_temperature_unitaria_identidade(self):
        probs = {"a": 0.7, "b": 0.3}
        out = LayaAdapter.apply_temperature(probs, 1.0)
        assert out == probs

    def test_temperature_baixa_achata_distribuicao(self):
        probs = {"a": 0.9, "b": 0.1}
        out = LayaAdapter.apply_temperature(probs, 5.0)
        assert abs(sum(out.values()) - 1.0) < 1e-6
        assert out["b"] > probs["b"]
        assert out["a"] < probs["a"]

    def test_temperature_alta_concentra_no_topo(self):
        probs = {"a": 0.6, "b": 0.4}
        out = LayaAdapter.apply_temperature(probs, 0.1)
        assert abs(sum(out.values()) - 1.0) < 1e-6
        assert out["a"] > probs["a"]

    def test_temperatura_zero_negativa_usa_piso_seguro(self):
        probs = {"a": 0.5, "b": 0.5}
        out = LayaAdapter.apply_temperature(probs, 0.0)
        assert abs(sum(out.values()) - 1.0) < 1e-6
        out2 = LayaAdapter.apply_temperature(probs, -1.0)
        assert abs(sum(out2.values()) - 1.0) < 1e-6


# ─── Testes de Mapeamento de Escala (função pura) ────────────────────────────

class TestScoreMapeamento:
    """`map_score_ordinal`: ordinal Laya 0–4 → escala JEV 1–5 + corte."""

    def test_score_ordinal_0_mapeia_para_1(self):
        """Laya ordinal 0.0 (critical flaws) → JEV 1.0, reprovado em 3.40."""
        score, normalized, passed = map_score_ordinal(0.0, 3.40)
        assert score == 1.0, f"Esperado 1.0, obtido {score}"
        assert normalized == 0.0
        assert passed is False

    def test_score_ordinal_4_mapeia_para_5(self):
        """Laya ordinal 4.0 (exceptional) → JEV 5.0, aprovado em 3.40."""
        score, normalized, passed = map_score_ordinal(4.0, 3.40)
        assert score == 5.0, f"Esperado 5.0, obtido {score}"
        assert normalized == 1.0
        assert passed is True

    def test_score_ordinal_2_15_mapeia_corretamente(self):
        """Regressão: ordinal 2.15 → JEV 3.15 (NUNCA 1+2.15*4=9.61)."""
        score, normalized, passed = map_score_ordinal(2.15, 3.40)
        assert score == 3.15, f"Esperado 3.15, obtido {score}"
        assert normalized == round(2.15 / 4.0, 4)
        assert passed is False

    def test_score_exatamente_no_threshold_passa(self):
        """Corte inclusivo (>=): ordinal 2.40 → 3.40 == threshold → aprovado."""
        score, _, passed = map_score_ordinal(2.40, 3.40)
        assert score == 3.40
        assert passed is True

    def test_score_threshold_none_passed_none(self):
        """threshold=None → passed=None (contrato ScoreRequest.threshold opcional)."""
        score, normalized, passed = map_score_ordinal(3.0, None)
        assert score == 4.0
        assert normalized == 0.75
        assert passed is None


# ─── Testes de Noul (função pura) ────────────────────────────────────────────

class TestNoulDecisao:
    """`decide_noul`: aparada, corte inclusivo e confidence."""

    def test_noul_aprovado_threshold_recalibrado(self):
        """prob=0.827 deve passar com threshold recalibrado 0.78."""
        prob, passed, confidence = decide_noul(0.827, LAYA_NOUL_THRESHOLD_DEFAULT)
        assert passed is True, "prob=0.827 deve passar threshold=0.78"
        assert prob == 0.827
        assert confidence == pytest.approx(abs(0.827 - 0.5) * 2.0)

    def test_noul_reprovado_threshold_original_deberta(self):
        """prob=0.827 NÃO deve passar com threshold original DeBERTa=0.85."""
        _, passed, _ = decide_noul(0.827, 0.85)
        assert passed is False, "prob=0.827 NÃO deve passar threshold=0.85"

    def test_noul_corte_inclusivo_no_threshold(self):
        """prob == threshold → aprovado (>= inclusivo)."""
        _, passed, _ = decide_noul(0.78, 0.78)
        assert passed is True

    def test_noul_apara_fora_do_intervalo(self):
        """Probabilidade fora de [0,1] é aparada antes do corte."""
        prob_up, passed_up, _ = decide_noul(1.5, 0.9)
        assert prob_up == 1.0
        assert passed_up is True

        prob_down, passed_down, conf_down = decide_noul(-0.2, 0.5)
        assert prob_down == 0.0
        assert passed_down is False
        assert conf_down == 1.0  # |0.0 - 0.5| * 2 — certeza máxima na rejeição


# ─── Testes de Choice (função pura) ──────────────────────────────────────────

class TestChoiceDecisao:
    """`finalize_choice`: temperatura + argmax + cobertura da distribuição."""

    def test_choice_seleciona_argmax(self):
        """Maior probabilidade vence com temperature=1.0 (identidade)."""
        selected, confidence, probs = finalize_choice(
            {"backend": 0.7, "frontend": 0.2, "security": 0.1}, 1.0
        )
        assert selected == "backend"
        assert confidence == pytest.approx(0.7)
        assert probs == {"backend": 0.7, "frontend": 0.2, "security": 0.1}

    def test_choice_retorna_todas_probabilidades(self):
        """A distribuição final preserva TODAS as opções e soma ~1."""
        options = ["backend", "security"]
        raw = {opt: (0.85 if opt == "security" else 0.15) for opt in options}
        selected, confidence, probs = finalize_choice(raw, 1.0)
        assert set(probs) == set(options), (
            f"Esperado cobertura completa {set(options)}, obtido {set(probs)}"
        )
        assert selected == "security"
        assert confidence == pytest.approx(0.85)
        assert abs(sum(probs.values()) - 1.0) < 1e-3
        assert all(v > 0.0 for v in probs.values())

    def test_choice_vazio_usa_fallback_do_agente(self):
        """Distribuição vazia cai no fallback (choice/confidence brutos do agente)."""
        selected, confidence, probs = finalize_choice(
            {}, 1.0, fallback_selected="docs", fallback_confidence=0.6
        )
        assert selected == "docs"
        assert confidence == pytest.approx(0.6)
        assert probs == {"docs": 0.6}

    def test_choice_temperature_achata_mas_mantem_argmax(self):
        """T=5.0 achata a distribuição; o líder permanece o selecionado."""
        raw = {"a": 0.9, "b": 0.1}
        selected, _, probs = finalize_choice(raw, 5.0)
        assert selected == "a"
        assert probs["b"] > 0.1
        assert abs(sum(probs.values()) - 1.0) < 1e-3


# ─── Testes de Carga ─────────────────────────────────────────────────────────

class TestLayaLoad:
    """Verifica comportamento de carga e fallback."""

    def test_load_falha_device_invalido_sem_cpu_fallback(self):
        """Caminho REAL de falha de load: device cuda inválido + fallback desabilitado.

        laya está instalado — não há como forçar ImportError sem patch.dict
        (banido por AGENTS.md §5.2). O caminho documentado de falha é
        `load()` com `allow_cpu_fallback=False`: device "cuda:9" derruba a
        GPU ("Invalid device id") e o retry em CPU é suprimido →
        `InferenceError`.
        """
        from src.core.schemas import InferenceError

        adapter = LayaAdapter(
            checkpoint="english", device="invalid_device_xyz", allow_cpu_fallback=False
        )
        with pytest.raises(InferenceError):
            adapter.load()
        assert adapter.is_ready is False

    def test_checkpoint_invalido_recai_para_padrao(self):
        """Checkpoint desconhecido recai para o checkpoint padrão documentado (sem exceção)."""
        adapter = LayaAdapter(checkpoint="inexistente")
        assert adapter.is_ready is False
        assert adapter._checkpoint_key in ("typed-decisions", "english")

    def test_ensure_ready_sem_load(self):
        """Chamar _ensure_ready sem load deve lançar OpenJevNotReadyError."""
        from src.core.schemas import OpenJevNotReadyError

        adapter = _make_adapter()
        with pytest.raises(OpenJevNotReadyError):
            adapter._ensure_ready()


# ─── Integração Real com Laya ────────────────────────────────────────────────

@pytest.mark.skip(
    reason=(
        "TICKET-HARNESS-47: Laya RLCD desativado. Integração real com pesos do "
        "modelo aposentada junto com o caminho de inferência ML/GPU."
    )
)
class TestLayaAdapterIntegracao:
    """Ponta-a-ponta contra o modelo Laya real carregado do disco (sem agente sintético).

    Complementa os testes das funções puras acima: valida o contrato completo
    evaluate_* → response com o modelo de verdade.
    """

    @classmethod
    def setup_class(cls):
        """Carrega o LayaAdapter real uma vez para toda a classe."""
        cls.adapter = LayaAdapter(checkpoint="english", device="cpu", allow_cpu_fallback=True)
        cls.adapter.load()

    @pytest.mark.asyncio
    async def test_score_real_retorna_float_entre_1_e_5(self):
        """Score real deve estar em [1.0, 5.0] e `passed` coerente com o threshold."""
        threshold = LAYA_SCORE_THRESHOLD_DEFAULT
        req = ScoreRequest(
            premise="Função que retorna a soma de dois inteiros com teste de cobertura.",
            rubric="Avalie a qualidade técnica de 1 a 5.",
            threshold=threshold,
        )
        resp = await self.adapter.evaluate_score(req)
        assert 1.0 <= resp.score <= 5.0
        assert resp.passed == (resp.score >= threshold), (
            f"passed={resp.passed} inconsistente com score={resp.score} "
            f"e threshold={threshold}"
        )
        assert len(resp.breakdown) == 5
        assert abs(sum(resp.breakdown.values()) - 1.0) < 0.05

    @pytest.mark.asyncio
    async def test_noul_real_retorna_bool_com_probabilidade(self):
        """Noul real: passed coerente com probabilidade >= threshold, prob em [0,1]."""
        threshold = LAYA_NOUL_THRESHOLD_DEFAULT
        req = NoulRequest(
            premise="Operação de leitura em arquivo de log sem alterações no disco.",
            hypothesis="A operação é somente de leitura e não altera o estado do disco.",
            threshold=threshold,
        )
        resp = await self.adapter.evaluate_noul(req)
        assert isinstance(resp.passed, bool)
        assert 0.0 <= resp.probability <= 1.0
        assert resp.passed == (resp.probability >= threshold), (
            f"passed={resp.passed} inconsistente com "
            f"probability={resp.probability} e threshold={threshold}"
        )
        assert resp.confidence == pytest.approx(
            abs(resp.probability - 0.5) * 2.0, abs=1e-3
        )

    @pytest.mark.asyncio
    async def test_choice_real_seleciona_opcao_da_lista(self):
        """Choice real: selected ∈ options, cobertura total e distribuição normalizada."""
        req = ChoiceRequest(
            premise="Banco de dados sem índice em coluna de busca frequente.",
            options=["gargalo_de_performance_ou_complexidade", "credenciais_hardcoded",
                     "falta_cobertura_caminhos_de_erro"],
        )
        resp = await self.adapter.evaluate_choice(req)
        assert resp.selected in req.options
        assert resp.selected == max(resp.probabilities, key=resp.probabilities.get), (
            "selected deve ser o argmax da distribuição retornada"
        )
        assert 0.0 <= resp.top_confidence <= 1.0
        assert set(resp.probabilities) == set(req.options)
        assert abs(sum(resp.probabilities.values()) - 1.0) < 0.01


class TestBorderlineMassGatePure:
    """Testes unitários puros da lógica de fronteira (Borderline Mass Gate)."""

    def test_mass_gate_aprovado_alta_massa_saudavel(self):
        from src.decision.laya_adapter import evaluate_borderline_mass_gate
        breakdown = {
            "critical flaws": 0.02,
            "poor quality": 0.06,
            "acceptable": 0.35,
            "good quality": 0.45,
            "exceptional": 0.12,
        }
        approved, p_healthy, p_flawed = evaluate_borderline_mass_gate(breakdown)
        assert approved is True
        assert p_healthy == 0.92
        assert p_flawed == 0.08

    def test_mass_gate_reprovado_falhas_excessivas(self):
        from src.decision.laya_adapter import evaluate_borderline_mass_gate
        breakdown = {
            "critical flaws": 0.10,
            "poor quality": 0.08,
            "acceptable": 0.35,
            "good quality": 0.37,
            "exceptional": 0.10,
        }
        approved, p_healthy, p_flawed = evaluate_borderline_mass_gate(breakdown)
        assert approved is False
        assert p_flawed == 0.18
        assert p_healthy == 0.82

    def test_mass_gate_reprovado_massa_saudavel_insuficiente(self):
        from src.decision.laya_adapter import evaluate_borderline_mass_gate
        breakdown = {
            "critical flaws": 0.05,
            "poor quality": 0.11,
            "acceptable": 0.40,
            "good quality": 0.40,
            "exceptional": 0.04,
        }
        approved, p_healthy, p_flawed = evaluate_borderline_mass_gate(breakdown)
        assert approved is False
        assert p_healthy == 0.84
        assert p_flawed == 0.16

