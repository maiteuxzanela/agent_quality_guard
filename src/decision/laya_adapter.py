"""Adaptador Laya para o motor Open Jev.

Encapsula a API nativa do Laya (choice/noul/score) mapeando para os
esquemas internos do JEV, com suporte a thresholds recalibrados e
fallback automático para CPU em caso de indisponibilidade de GPU.
O modelo DeBERTa-v3 foi removido do pipeline em set/2026 — o Laya
é agora o único backend discriminativo suportado.

Hardware alvo: GTX 1050 Ti (4 GB VRAM, Pascal sm_61).
  - Flash Attention 2: INCOMPATÍVEL (requer Ampere sm_80+).
  - Atenção padrão (sdpa): COMPATÍVEL.
  - VRAM necessária para laya english: ~1.57 GB (isolado).
  - VRAM necessária para laya-multilingual: ~1.0 GB.

Baseado em benchmark: benchmarks/laya_vs_deberta_cpu_stage1.json
                      benchmarks/laya_gpu_stage2.json
"""

import asyncio
import logging
import math
import threading
import time
from typing import Any, Dict, Optional

from ..core.schemas import (
    ChoiceRequest,
    ChoiceResponse,
    HardwareUnavailableError,
    InferenceError,
    NoulRequest,
    NoulResponse,
    OpenJevNotReadyError,
    ScoreRequest,
    ScoreResponse,
)

logger = logging.getLogger("jev.laya_adapter")

# ─── Thresholds recalibrados para o Laya ────────────────────────────────────
# O Laya é calibrado com RLCD (proper scoring rules) e produz probabilidades
# mais conservadoras que o DeBERTa-v3 NLI. Deltas medidos empiricamente:
#   noul:  DeBERTa=0.954, Laya=0.827 → Δ=-0.127
#   score: DeBERTa=3.68,  Laya=3.15  → Δ=-0.53

LAYA_NOUL_THRESHOLD_DEFAULT: float = 0.78   # vs 0.85 do DeBERTa
LAYA_SCORE_THRESHOLD_DEFAULT: float = 3.40  # vs 3.75 do DeBERTa

# Mapeamento ordinais Laya (0–4) para escala JEV (1–5)
# score_JEV = 1.0 + score_ordinal
LAYA_ORDINAL_OFFSET: float = 1.0


def map_score_ordinal(raw_ordinal: float, threshold: Optional[float]) -> tuple:
    """Decisão pura de Score: ordinal Laya (0–4) → (score_JEV, normalizado, passed).

    Único ponto de mapeamento da escala (regressão: 2.15 → 3.15, não 9.61).
    `threshold=None` → `passed=None`. Função pura, sem I/O.
    """
    final_score = round(LAYA_ORDINAL_OFFSET + raw_ordinal, 2)
    normalized = round(raw_ordinal / 4.0, 4)
    passed = final_score >= threshold if threshold is not None else None
    return final_score, normalized, passed


def decide_noul(probability: float, threshold: float) -> tuple:
    """Decisão pura de Noul: (probabilidade aparada, passed, confidence).

    Aparna em [0.0, 1.0], `passed = prob >= threshold` (limiar inclusivo) e
    `confidence = |prob - 0.5| * 2.0`. Função pura, sem I/O.
    """
    prob = max(0.0, min(1.0, probability))
    passed = prob >= threshold
    confidence = abs(prob - 0.5) * 2.0
    return prob, passed, confidence


def compute_score_uncertainty(
    probabilities_by_ordinal: Dict[int, float],
    threshold: float,
    borderline_band: float = 0.35,
) -> tuple:
    """Incerteza pura do Score a partir da distribuição por bucket ordinal.

    Bucket k (0–4) equivale a nota JEV 1+k (ex.: k=2 → 3.0). Retorna
    (p_at_or_above, p_below, score_margin, borderline) onde
    score_margin = p_at_or_above - p_below em [-1.0, 1.0] e borderline
    marca a fronteira quando ambos os lados retêm massa ≥ borderline_band.
    Função pura, sem I/O.
    """
    p_at = 0.0
    p_below = 0.0
    for ordinal, prob in probabilities_by_ordinal.items():
        if (1.0 + float(ordinal)) >= threshold:
            p_at += float(prob)
        else:
            p_below += float(prob)
    total = p_at + p_below
    if total > 0:
        p_at /= total
        p_below /= total
    score_margin = round(p_at - p_below, 4)
    borderline = p_below >= borderline_band and p_at >= borderline_band
    return round(p_at, 4), round(p_below, 4), score_margin, borderline


def evaluate_borderline_mass_gate(
    breakdown: Dict[str, float],
    healthy_min: float = 0.85,
    flawed_max: float = 0.12,
) -> tuple:
    """Critério Probabilístico de Aprovação na Fronteira (Borderline Mass Gate).

    Calcula a massa saudável (acceptable + good quality + exceptional) e a massa de falhas
    (critical flaws + poor quality). Se p_healthy >= healthy_min e p_flawed <= flawed_max,
    a entrega não tem defeitos relevantes e deve ser aprovada na fronteira.
    Retorna (approved, p_healthy, p_flawed). Função pura, sem I/O.
    """
    p_healthy = (
        float(breakdown.get("acceptable", 0.0))
        + float(breakdown.get("good quality", 0.0))
        + float(breakdown.get("exceptional", 0.0))
    )
    p_flawed = (
        float(breakdown.get("critical flaws", 0.0))
        + float(breakdown.get("poor quality", 0.0))
    )
    p_healthy = round(p_healthy, 4)
    p_flawed = round(p_flawed, 4)
    approved = p_healthy >= healthy_min and p_flawed <= flawed_max
    return approved, p_healthy, p_flawed


def finalize_choice(
    probabilities: Dict[str, float],
    temperature: float,
    fallback_selected: str = "",
    fallback_confidence: float = 0.0,
) -> tuple:
    """Decisão pura de Choice: temperatura + argmax sobre a distribuição.

    Se `probabilities` for vazia, usa `fallback_selected`/`fallback_confidence`
    (resposta bruta do agente). Retorna (selected, confidence, distribuição
    arredondada). Função pura, sem I/O.
    """
    probs = dict(probabilities)
    if not probs:
        probs = {fallback_selected: round(float(fallback_confidence), 4)}
    probs = LayaAdapter.apply_temperature(probs, temperature)
    probs = {k: round(v, 4) for k, v in probs.items()}
    selected = max(probs, key=probs.get)
    return selected, probs[selected], probs

# Checkpoints disponíveis por custo de VRAM
LAYA_CHECKPOINTS = {
    "english": {
        "repo": "convaiinnovations/laya",
        "subfolder": None,
        "vram_gb": 1.57,
        "params": "421M",
        "languages": ["en"],
    },
    "multilingual": {
        "repo": "convaiinnovations/laya",
        "subfolder": "multilingual",
        "vram_gb": 1.0,
        "params": "322M",
        "languages": ["100+"],
    },
    "typed-decisions": {
        "repo": "convaiinnovations/laya",
        "subfolder": "typed-decisions",
        "vram_gb": 1.57,
        "params": "421M",
        "languages": ["en"],
    },
}


class LayaAdapter:
    """Adaptador que expõe as primitivas JEV via API nativa do Laya.

    Uso preferencial quando o motor DeBERTa-v3 não está carregado
    (coexistência na GTX 1050 Ti 4GB é inviável).

    Exemplo:
        adapter = LayaAdapter(checkpoint="multilingual", device="cuda")
        adapter.load()
        resp = await adapter.evaluate_choice(request)
    """

    @staticmethod
    def apply_temperature(probabilities: Dict[str, float], temperature: float) -> Dict[str, float]:
        """Escala probabilidades com temperatura (T→0 confirma, T>1 achata).

        O parâmetro `temperature` da API ChoiceRequest não pode ser ignorado
        no caminho Laya — era stub silencioso (aceito e descartado).
        """
        if not probabilities:
            return probabilities
        t = max(float(temperature), 1e-4)
        if abs(t - 1.0) < 1e-9:
            return probabilities
        logs = {k: math.log(max(p, 1e-12)) for k, p in probabilities.items()}
        scaled = {k: v / t for k, v in logs.items()}
        m = max(scaled.values())
        exps = {k: math.exp(v - m) for k, v in scaled.items()}
        total = sum(exps.values())
        if total <= 0.0:
            return probabilities
        return {k: v / total for k, v in exps.items()}

    def __init__(
        self,
        checkpoint: str = "english",
        device: Optional[str] = None,
        noul_threshold_override: Optional[float] = None,
        score_threshold_override: Optional[float] = None,
        allow_cpu_fallback: bool = True,
    ):
        """
        Args:
            checkpoint: Um de 'english', 'multilingual', 'typed-decisions'.
            device: 'cuda', 'cpu', ou None (auto-detecta).
            noul_threshold_override: Sobrescreve o threshold de noul calibrado.
            score_threshold_override: Sobrescreve o threshold de score calibrado.
            allow_cpu_fallback: Se True, cai para CPU em caso de OOM na GPU.
        """
        if checkpoint not in LAYA_CHECKPOINTS:
            raise ValueError(
                f"Checkpoint inválido: {checkpoint!r}. "
                f"Opções: {list(LAYA_CHECKPOINTS.keys())}"
            )

        self._checkpoint_key = checkpoint
        self._checkpoint_cfg = LAYA_CHECKPOINTS[checkpoint]
        self._device = device
        self._allow_cpu_fallback = allow_cpu_fallback
        self._agent: Any = None
        self.is_ready = False
        # Serializa inferência: o agente Laya não é reentrante e o predict
        # roda em worker thread (asyncio.to_thread) para não travar o event loop.
        self._predict_lock = threading.Lock()

        # Thresholds recalibrados
        self.noul_threshold = noul_threshold_override or LAYA_NOUL_THRESHOLD_DEFAULT
        self.score_threshold = score_threshold_override or LAYA_SCORE_THRESHOLD_DEFAULT

        logger.info(
            f"LayaAdapter inicializado | checkpoint={checkpoint} | "
            f"noul_threshold={self.noul_threshold} | "
            f"score_threshold={self.score_threshold}"
        )

    def load(self) -> None:
        """Carrega o checkpoint Laya em memória."""
        try:
            from laya import Agent  # type: ignore[import]
        except ImportError as e:
            raise HardwareUnavailableError(
                f"Pacote 'laya' não instalado. Execute: pip install laya. Erro: {e}"
            ) from e

        cfg = self._checkpoint_cfg
        target_device = self._device or "cuda"

        logger.info(
            f"Carregando Laya '{self._checkpoint_key}' "
            f"({cfg['params']} params, ~{cfg['vram_gb']:.2f} GB VRAM) "
            f"no dispositivo '{target_device}'..."
        )

        try:
            self._agent = Agent(
                cfg["repo"],
                device=target_device,
                subfolder=cfg["subfolder"],
            )
            self.is_ready = True
            logger.info(f"Laya '{self._checkpoint_key}' carregado com sucesso em {target_device}.")
        except Exception as gpu_err:
            if self._allow_cpu_fallback:
                logger.warning(
                    f"Falha ao carregar Laya na GPU ({gpu_err}). "
                    "Ativando fallback para CPU."
                )
                try:
                    self._agent = Agent(
                        cfg["repo"],
                        device="cpu",
                        subfolder=cfg["subfolder"],
                    )
                    self.is_ready = True
                    logger.info("Laya carregado em CPU (fallback).")
                except Exception as cpu_err:
                    raise OpenJevNotReadyError(
                        f"Falha ao carregar Laya (GPU e CPU): {cpu_err}"
                    ) from cpu_err
            else:
                raise InferenceError(
                    f"Falha ao carregar Laya na GPU e CPU fallback desabilitado: {gpu_err}"
                ) from gpu_err

    def load_model(self) -> None:
        """Alias para load() mantendo compatibilidade de interface com OpenJevEngine."""
        self.load()

    def _ensure_ready(self) -> None:
        if not self.is_ready or self._agent is None:
            raise OpenJevNotReadyError(
                "LayaAdapter não está pronto. Execute load() antes de chamar as primitivas."
            )

    async def evaluate_noul(self, request: NoulRequest) -> NoulResponse:
        """Avalia proposição booleana via primitiva noul nativa do Laya."""
        self._ensure_ready()
        start = time.perf_counter()

        question = {
            "is_true": {
                "type": "noul",
                "instructions": request.hypothesis,
            }
        }

        try:
            def _predict() -> Any:
                with self._predict_lock:
                    return self._agent.predict({"state": request.premise}, question)

            res = await asyncio.to_thread(_predict)
            raw = float(res["answers"]["is_true"]["noul"])
            prob, passed, confidence = decide_noul(raw, request.threshold)

            # Usa o threshold do request. O threshold recalibrado (self.noul_threshold)
            # serve como padrão apenas quando o chamador instancia o LayaAdapter
            # e não sobrescreve via request — documentado em LAYA_NOUL_THRESHOLD_DEFAULT.
            threshold = request.threshold
            latency_ms = (time.perf_counter() - start) * 1000.0

            return NoulResponse(
                passed=passed,
                probability=round(prob, 4),
                confidence=round(confidence, 4),
                threshold=threshold,
                latency_ms=round(latency_ms, 2),
            )
        except Exception as e:
            raise InferenceError(f"Erro na inferência 'noul' do Laya: {e}") from e

    async def evaluate_choice(self, request: ChoiceRequest) -> ChoiceResponse:
        """Roteia para o melhor agente via primitiva choice nativa do Laya.

        NOTA: O Laya exige critérios descritivos por opção para acurácia ideal.
        Critérios genéricos (ex: 'specialist in X') degradam a confiança.
        """
        self._ensure_ready()
        start = time.perf_counter()

        # Critérios descritivos: usa o mapa do request quando presente
        # (o diagnóstico serve DEFECT_DESCRIPTIONS); senão, o nome da opção.
        if request.criteria:
            criteria = {
                opt: request.criteria.get(opt, opt) for opt in request.options
            }
        elif isinstance(request.options, list):
            criteria = {opt: opt for opt in request.options}
        else:
            criteria = dict(request.options)

        instructions = (
            request.instructions
            or "Which specialist agent should handle this task?"
        )

        question = {
            "routed_agent": {
                "type": "choice",
                "instructions": instructions,
                "criteria": criteria,
            }
        }

        try:
            def _predict() -> Any:
                with self._predict_lock:
                    return self._agent.predict({"task": request.premise}, question)

            res = await asyncio.to_thread(_predict)
            ans = res["answers"]["routed_agent"]
            raw_confidence = float(ans.get("confidence", 0.0))
            raw_choice = str(ans.get("choice", ""))
            probabilities = {
                opt: round(float(p), 4)
                for opt, p in ans.get("probabilities", {}).items()
            }

            # Temperatura do request + argmax: função pura finalize_choice
            selected, confidence, probabilities = finalize_choice(
                probabilities,
                request.temperature,
                fallback_selected=raw_choice,
                fallback_confidence=raw_confidence,
            )

            latency_ms = (time.perf_counter() - start) * 1000.0
            return ChoiceResponse(
                selected=selected,
                probabilities=probabilities,
                top_confidence=round(confidence, 4),
                latency_ms=round(latency_ms, 2),
            )
        except Exception as e:
            raise InferenceError(f"Erro na inferência 'choice' do Laya: {e}") from e

    async def evaluate_score(self, request: ScoreRequest) -> ScoreResponse:
        """Avalia conformidade via primitiva score nativa do Laya.

        Mapeamento de escala:
            Laya retorna ordinal 0–4 (índice dos critérios).
            score_JEV = 1.0 + score_ordinal → escala 1–5.
        """
        self._ensure_ready()
        start = time.perf_counter()

        criteria_labels = [
            "critical flaws",
            "poor quality",
            "acceptable",
            "good quality",
            "exceptional",
        ]

        question = {
            "quality": {
                "type": "score",
                "instructions": request.rubric,
                "criteria": criteria_labels,
            }
        }

        try:
            def _predict() -> Any:
                with self._predict_lock:
                    return self._agent.predict({"delivery": request.premise}, question)

            res = await asyncio.to_thread(_predict)
            ans = res["answers"]["quality"]

            raw_ordinal = float(ans["score"])  # 0.0 a 4.0

            # Usa threshold recalibrado se não vier no request
            threshold = request.threshold
            if threshold is None:
                threshold = self.score_threshold
            # Decisão pura: mapeamento 0–4 → 1–5 + normalização + passed
            final_score, normalized, passed = map_score_ordinal(raw_ordinal, threshold)

            probs_list = [float(p) for p in ans.get("probabilities", {}).values()]
            breakdown = {
                label: round(p, 4)
                for label, p in zip(criteria_labels, probs_list)
            }
            p_at, p_below, score_margin, borderline = compute_score_uncertainty(
                dict(enumerate(probs_list)), threshold
            )

            latency_ms = (time.perf_counter() - start) * 1000.0
            agent = getattr(self, "_agent", None)
            device_eff = str(getattr(agent, "device", self._device or "")) or None
            dtype_eff = str(getattr(agent, "dtype", "") or "") or None
            return ScoreResponse(
                score=final_score,
                normalized_score=normalized,
                passed=passed,
                breakdown=breakdown,
                latency_ms=round(latency_ms, 2),
                score_margin=score_margin,
                borderline=borderline,
                device=device_eff,
                dtype=dtype_eff,
            )
        except Exception as e:
            raise InferenceError(f"Erro na inferência 'score' do Laya: {e}") from e
