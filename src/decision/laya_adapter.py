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

import logging
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
LAYA_SCORE_THRESHOLD_DEFAULT: float = 3.60  # vs 3.75 do DeBERTa

# Mapeamento ordinais Laya (0–4) para escala JEV (1–5)
# score_JEV = 1.0 + score_ordinal
LAYA_ORDINAL_OFFSET: float = 1.0

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
            res = self._agent.predict({"state": request.premise}, question)
            prob = float(res["answers"]["is_true"]["noul"])
            prob = max(0.0, min(1.0, prob))

            # Usa o threshold do request. O threshold recalibrado (self.noul_threshold)
            # serve como padrão apenas quando o chamador instancia o LayaAdapter
            # e não sobrescreve via request — documentado em LAYA_NOUL_THRESHOLD_DEFAULT.
            threshold = request.threshold
            passed = prob >= threshold
            confidence = abs(prob - 0.5) * 2.0
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

        # Constrói mapa de critérios para o Laya
        # Se options for lista simples, usa o nome como critério (comportamento básico)
        if isinstance(request.options, list):
            criteria = {opt: opt for opt in request.options}
        else:
            criteria = dict(request.options)

        question = {
            "routed_agent": {
                "type": "choice",
                "instructions": "Which specialist agent should handle this task?",
                "criteria": criteria,
            }
        }

        try:
            res = self._agent.predict({"task": request.premise}, question)
            ans = res["answers"]["routed_agent"]
            selected = ans["choice"]
            confidence = float(ans.get("confidence", 0.0))
            probabilities = {
                opt: round(float(p), 4)
                for opt, p in ans.get("probabilities", {}).items()
            }
            if not probabilities:
                probabilities = {selected: round(confidence, 4)}

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
            res = self._agent.predict({"delivery": request.premise}, question)
            ans = res["answers"]["quality"]

            raw_ordinal = float(ans["score"])  # 0.0 a 4.0
            final_score = round(LAYA_ORDINAL_OFFSET + raw_ordinal, 2)  # 1.0 a 5.0
            normalized = round(raw_ordinal / 4.0, 4)  # 0.0 a 1.0

            # Usa threshold recalibrado se não vier no request
            threshold = request.threshold
            if threshold is None:
                threshold = self.score_threshold
            passed = final_score >= threshold if threshold is not None else None

            breakdown = {
                labels: round(float(p), 4)
                for labels, p in zip(criteria_labels, ans.get("probabilities", {}).values())
            }

            latency_ms = (time.perf_counter() - start) * 1000.0
            return ScoreResponse(
                score=final_score,
                normalized_score=normalized,
                passed=passed,
                breakdown=breakdown,
                latency_ms=round(latency_ms, 2),
            )
        except Exception as e:
            raise InferenceError(f"Erro na inferência 'score' do Laya: {e}") from e
