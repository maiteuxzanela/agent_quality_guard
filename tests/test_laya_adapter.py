"""Testes do LayaAdapter: verifica mapeamento de escala, thresholds recalibrados
e comportamento de fallback sem depender do modelo real (mock lightweight).
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest

from src.core.schemas import ChoiceRequest, NoulRequest, ScoreRequest
from src.decision.laya_adapter import (
    LAYA_NOUL_THRESHOLD_DEFAULT,
    LAYA_ORDINAL_OFFSET,
    LAYA_SCORE_THRESHOLD_DEFAULT,
    LayaAdapter,
)


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_adapter(checkpoint="english", device="cpu") -> LayaAdapter:
    return LayaAdapter(checkpoint=checkpoint, device=device, allow_cpu_fallback=True)


def _mock_agent(choice_val="backend", noul_val=0.92, score_ordinal=2.5):
    """Cria um Agent mock que retorna valores fixos para as 3 primitivas."""
    agent = MagicMock()

    def _predict(state, questions):
        results = {}
        for key, q in questions.items():
            qtype = q.get("type")
            if qtype == "choice":
                results[key] = {
                    "choice": choice_val,
                    "confidence": 0.85,
                    "probabilities": {choice_val: 0.85},
                }
            elif qtype == "noul":
                results[key] = {"noul": noul_val}
            elif qtype == "score":
                results[key] = {
                    "score": score_ordinal,
                    "probabilities": {str(i): 0.2 for i in range(5)},
                }
        return {"answers": results}

    agent.predict.side_effect = _predict
    return agent


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
        """Threshold score deve ser 3.60 (vs 3.75 do DeBERTa)."""
        assert LAYA_SCORE_THRESHOLD_DEFAULT == 3.60, (
            f"Esperado 3.60, obtido {LAYA_SCORE_THRESHOLD_DEFAULT}. "
            "Delta empírico DeBERTa(3.68) - Laya(3.15) = 0.53."
        )

    def test_laya_ordinal_offset(self):
        """Offset de mapeamento ordinal Laya(0-4) → JEV(1-5) deve ser 1.0."""
        assert LAYA_ORDINAL_OFFSET == 1.0


# ─── Testes de Mapeamento de Escala ──────────────────────────────────────────

class TestScoreMapeamento:
    """Verifica o mapeamento ordinal Laya 0–4 → escala JEV 1–5."""

    @pytest.mark.asyncio
    async def test_score_ordinal_0_mapeia_para_1(self):
        """Laya score=0.0 (critical flaws) → JEV score=1.0"""
        adapter = _make_adapter()
        adapter._agent = _mock_agent(score_ordinal=0.0)
        adapter.is_ready = True

        req = ScoreRequest(
            premise="Entrega vazia.",
            rubric="Avalie a qualidade.",
            threshold=3.60,
        )
        resp = await adapter.evaluate_score(req)
        assert resp.score == 1.0, f"Esperado 1.0, obtido {resp.score}"
        assert resp.passed is False

    @pytest.mark.asyncio
    async def test_score_ordinal_4_mapeia_para_5(self):
        """Laya score=4.0 (exceptional) → JEV score=5.0"""
        adapter = _make_adapter()
        adapter._agent = _mock_agent(score_ordinal=4.0)
        adapter.is_ready = True

        req = ScoreRequest(
            premise="Entrega perfeita.",
            rubric="Avalie a qualidade.",
            threshold=3.60,
        )
        resp = await adapter.evaluate_score(req)
        assert resp.score == 5.0, f"Esperado 5.0, obtido {resp.score}"
        assert resp.passed is True

    @pytest.mark.asyncio
    async def test_score_ordinal_2_15_mapeia_corretamente(self):
        """Reproduz o bug original: Laya score=2.15 → JEV deve ser 3.15 (não 9.61)."""
        adapter = _make_adapter()
        adapter._agent = _mock_agent(score_ordinal=2.15)
        adapter.is_ready = True

        req = ScoreRequest(
            premise="Entrega aceitável.",
            rubric="Avalie a qualidade.",
            threshold=3.60,
        )
        resp = await adapter.evaluate_score(req)
        # score_JEV = 1.0 + 2.15 = 3.15 (NÃO 1.0 + 2.15 * 4 = 9.60)
        assert resp.score == 3.15, f"Esperado 3.15, obtido {resp.score}"


# ─── Testes de Noul ──────────────────────────────────────────────────────────

class TestNoulAdapter:
    """Verifica avaliação de proposições booleanas via Laya."""

    @pytest.mark.asyncio
    async def test_noul_aprovado_threshold_recalibrado(self):
        """prob=0.827 deve passar com threshold recalibrado 0.78."""
        adapter = _make_adapter()
        adapter._agent = _mock_agent(noul_val=0.827)
        adapter.is_ready = True

        req = NoulRequest(
            premise="SQL injection detectado.",
            hypothesis="Isso é um risco de segurança crítico.",
            threshold=LAYA_NOUL_THRESHOLD_DEFAULT,  # 0.78
        )
        resp = await adapter.evaluate_noul(req)
        assert resp.passed is True, f"prob=0.827 deve passar threshold=0.78"

    @pytest.mark.asyncio
    async def test_noul_reprovado_threshold_original_deberta(self):
        """prob=0.827 NÃO deve passar com threshold original DeBERTa=0.85."""
        adapter = _make_adapter()
        adapter._agent = _mock_agent(noul_val=0.827)
        adapter.is_ready = True

        req = NoulRequest(
            premise="SQL injection detectado.",
            hypothesis="Isso é um risco de segurança crítico.",
            threshold=0.85,  # threshold original DeBERTa
        )
        resp = await adapter.evaluate_noul(req)
        assert resp.passed is False, f"prob=0.827 NÃO deve passar threshold=0.85"


# ─── Testes de Choice ────────────────────────────────────────────────────────

class TestChoiceAdapter:
    """Verifica roteamento de agentes via primitiva choice do Laya."""

    @pytest.mark.asyncio
    async def test_choice_seleciona_corretamente(self):
        """Com critérios descritivos, choice deve retornar o agente correto."""
        adapter = _make_adapter()
        adapter._agent = _mock_agent(choice_val="backend")
        adapter.is_ready = True

        req = ChoiceRequest(
            premise="Crash na API de autenticação ao criar usuários.",
            options=["backend", "frontend", "security", "devops"],
        )
        resp = await adapter.evaluate_choice(req)
        assert resp.selected == "backend"
        assert resp.top_confidence > 0.0

    @pytest.mark.asyncio
    async def test_choice_retorna_todas_probabilidades(self):
        """ChoiceResponse deve conter probabilidades para o agente selecionado."""
        adapter = _make_adapter()
        adapter._agent = _mock_agent(choice_val="security")
        adapter.is_ready = True

        req = ChoiceRequest(
            premise="SQL injection detectado.",
            options=["backend", "security"],
        )
        resp = await adapter.evaluate_choice(req)
        assert resp.selected in resp.probabilities
        assert resp.probabilities[resp.selected] > 0.0


# ─── Testes de Carga ─────────────────────────────────────────────────────────

class TestLayaLoad:
    """Verifica comportamento de carga e fallback."""

    def test_load_falha_sem_laya_instalado(self):
        """Se laya não estiver instalado, deve lançar HardwareUnavailableError."""
        from src.core.schemas import HardwareUnavailableError

        adapter = _make_adapter()
        with patch.dict("sys.modules", {"laya": None}):
            with pytest.raises((HardwareUnavailableError, ImportError)):
                adapter.load()

    def test_checkpoint_invalido_lanca_value_error(self):
        """Checkpoint desconhecido deve lançar ValueError na inicialização."""
        with pytest.raises(ValueError, match="Checkpoint inválido"):
            LayaAdapter(checkpoint="inexistente")

    def test_ensure_ready_sem_load(self):
        """Chamar _ensure_ready sem load deve lançar OpenJevNotReadyError."""
        from src.core.schemas import OpenJevNotReadyError

        adapter = _make_adapter()
        with pytest.raises(OpenJevNotReadyError):
            adapter._ensure_ready()
