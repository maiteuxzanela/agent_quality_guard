"""Módulo Central da Engine Open Jev.
Implementa a execução local das três primitivas discriminativas:
- noul: Avaliação de predicado booleano / guardrail
- choice: Roteamento de subagentes especialistas
- score: Avaliação de conformidade e notas ordinais (1 a 5)

POLÍTICA ESTRITA: Sem simulação ou mock. Se o modelo ou ambiente não estiver pronto,
lança exceções explícitas (OpenJevNotReadyError, HardwareUnavailableError, InferenceError).
"""

import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from ..core.schemas import (
    ChoiceRequest,
    ChoiceResponse,
    HardwareUnavailableError,
    InferenceError,
    NoulRequest,
    NoulResponse,
    OpenJevError,
    OpenJevNotReadyError,
    ScoreRequest,
    ScoreResponse,
)
from .hardware import (
    clean_gpu_memory,
    get_hardware_profile,
    sanitize_torch_dtype,
)

logger = logging.getLogger("jev.engine")


class OpenJevEngine:
    """Wrapper discriminativo não-gerativo para o motor Open Jev.
    Por padrão utiliza o modelo Laya (convaiinnovations/laya) rodando localmente
    na GPU GTX 1050 Ti com fallback seguro para CPU e calibração RLCD.
    Quando detecta um servidor IPC ativo (hospedado pelo processo primário com GPU),
    conecta-se via Unix Domain Socket, economizando ~2.3 GB de RAM física e acelerando inferências.
    """

    def __init__(
        self,
        model_id_or_path: str = "convaiinnovations/laya",
        device: Optional[str] = None,
        max_length: int = 1024,
        allow_cpu_fallback: bool = True,
    ):
        self.model_id_or_path = model_id_or_path
        self.max_length = max_length
        self.allow_cpu_fallback = allow_cpu_fallback
        self.profile = get_hardware_profile()

        # Detecção de serviço IPC (processo secundário compartilhando GPU)
        from .ipc_service import is_ipc_available
        self.use_ipc = False
        if (device is None or "ipc" in device) and is_ipc_available():
            logger.info("Detectado servidor IPC ativo na GPU. Conectando via Unix Domain Socket...")
            self.use_ipc = True
            self.target_device = "ipc:gpu"
            self.is_ready = True
            self.is_laya = True
            self.laya_adapter = None
            self.model = None
            self.tokenizer = None
            self._native_jev = None
            self._entailment_idx = 2
            return

        # Determina o dispositivo alvo
        if device is not None:
            self.target_device = device
        else:
            self.target_device = self.profile.recommended_device

        # Backend Laya (padrão) vs Legado
        self.is_laya = "laya" in model_id_or_path.lower()
        self.laya_adapter: Any = None
        if self.is_laya:
            from .laya_adapter import LayaAdapter
            self.laya_adapter = LayaAdapter(
                checkpoint=os.getenv("JEV_LAYA_CHECKPOINT", "english"),
                device=self.target_device,
                allow_cpu_fallback=allow_cpu_fallback,
            )

        self.model: Any = None
        self.tokenizer: Any = None
        self._native_jev: Any = None
        self.is_ready = False
        self._entailment_idx: int = 2  # Padrão NLI (0: contradiction, 1: neutral, 2: entailment)

    def load_model(self) -> None:
        """Carrega o modelo e tokenizador na memória com verificações de hardware.
        Lança exceção imediata caso PyTorch, Transformers ou os pesos não estejam disponíveis.
        """
        if getattr(self, "use_ipc", False):
            logger.info("OpenJevEngine operando via IPC. Carregamento local de pesos suprimido.")
            self.is_ready = True
            return

        try:
            import torch
            from transformers import AutoModelForSequenceClassification, AutoTokenizer
        except ImportError as e:
            self.is_ready = False
            raise HardwareUnavailableError(
                f"Dependências essenciais ausentes para carregar o Open Jev: {e}. "
                "Instale torch e transformers no ambiente."
            ) from e

        logger.info(f"Iniciando carregamento do modelo Open Jev: {self.model_id_or_path}")
        logger.info(f"Dispositivo alvo: {self.target_device} | Hardware: {self.profile.device_name}")

        # Se for o modelo Laya, delega diretamente para o LayaAdapter
        if self.is_laya and self.laya_adapter is not None:
            try:
                self.laya_adapter.load_model()
                self.is_ready = self.laya_adapter.is_ready
                self.target_device = self.laya_adapter._device or self.target_device
                logger.info(f"Engine Laya inicializada com sucesso no dispositivo {self.target_device}!")
                return
            except Exception as e:
                self.is_ready = False
                clean_gpu_memory()
                raise OpenJevNotReadyError(f"Falha ao inicializar o motor Laya: {e}") from e

        try:
            # Trava estrita contra bfloat16 na GTX 1050 Ti
            if self.target_device.startswith("cuda"):
                torch_dtype = sanitize_torch_dtype(torch.float16)
            else:
                torch_dtype = torch.float32

            # Tenta carregar primeiro como OpenJev nativo (com head customizada de decisão tipada)
            snapshot_dir = self.model_id_or_path
            if not os.path.exists(snapshot_dir) and "open-jev" in self.model_id_or_path.lower():
                try:
                    from huggingface_hub import snapshot_download
                    snapshot_dir = snapshot_download(self.model_id_or_path)
                except Exception:
                    snapshot_dir = self.model_id_or_path

            if os.path.exists(os.path.join(snapshot_dir, "head.safetensors")) and os.path.exists(os.path.join(snapshot_dir, "open_jev_config.json")):
                logger.info("Detectada arquitetura Open Jev nativa (head.safetensors). Carregando motor nativo...")
                import sys
                if snapshot_dir not in sys.path:
                    sys.path.insert(0, snapshot_dir)
                from typed_decisions.open_jev import OpenJev
                try:
                    self._native_jev = OpenJev.from_pretrained(snapshot_dir, device=self.target_device)
                except (torch.cuda.OutOfMemoryError, RuntimeError) as err:
                    clean_gpu_memory()
                    if self.allow_cpu_fallback:
                        logger.warning(
                            f"Falha ao carregar na GPU ({err}). "
                            "Ativando fallback seguro para CPU."
                        )
                        self.target_device = "cpu"
                        self._native_jev = OpenJev.from_pretrained(snapshot_dir, device="cpu")
                    else:
                        raise InferenceError(f"Memória ou dispositivo GPU indisponível: {err}") from err

                self.model = self._native_jev.model
                self.tokenizer = self._native_jev.tok
                self.is_ready = True
                self.is_native_jev = True
                logger.info(f"Engine nativa Open Jev inicializada com sucesso no dispositivo {self.target_device}!")
                return

            self.is_native_jev = False
            # Carregamento padrão SequenceClassification
            self.tokenizer = AutoTokenizer.from_pretrained(
                self.model_id_or_path,
                use_fast=True,
            )

            try:
                self.model = AutoModelForSequenceClassification.from_pretrained(
                    self.model_id_or_path,
                    torch_dtype=torch_dtype,
                    low_cpu_mem_usage=True,
                )
                self.model.to(self.target_device)
            except torch.cuda.OutOfMemoryError as oom_err:
                clean_gpu_memory()
                if self.allow_cpu_fallback:
                    logger.warning(
                        "CUDA Out of Memory detectado durante carregamento na GPU. "
                        "Ativando fallback seguro para CPU em float32."
                    )
                    self.target_device = "cpu"
                    self.model = AutoModelForSequenceClassification.from_pretrained(
                        self.model_id_or_path,
                        torch_dtype=torch.float32,
                        low_cpu_mem_usage=True,
                    )
                    self.model.to("cpu")
                else:
                    raise InferenceError(f"Memória insuficiente na GPU (4GB VRAM): {oom_err}") from oom_err

            self.model.eval()

            # Identificar índice de entailment do modelo
            if hasattr(self.model.config, "label2id") and self.model.config.label2id:
                label_map = {k.lower(): v for k, v in self.model.config.label2id.items()}
                if "entailment" in label_map:
                    self._entailment_idx = label_map["entailment"]
                elif "true" in label_map:
                    self._entailment_idx = label_map["true"]
                elif "positive" in label_map:
                    self._entailment_idx = label_map["positive"]

            self.is_ready = True
            logger.info("Modelo Open Jev carregado com sucesso e pronto para inferência.")

        except Exception as err:
            self.is_ready = False
            clean_gpu_memory()
            raise OpenJevNotReadyError(
                f"Falha ao carregar a engine Open Jev ({self.model_id_or_path}): {err}"
            ) from err

    def _ensure_ready(self) -> None:
        """Validação interna de disponibilidade. Sem mocks."""
        if getattr(self, "use_ipc", False):
            from .ipc_service import is_ipc_available
            if not is_ipc_available():
                raise OpenJevNotReadyError(
                    "Servidor IPC do Open Jev não está mais respondendo."
                )
            return
        if self.is_laya:
            if not self.is_ready or self.laya_adapter is None or not self.laya_adapter.is_ready:
                raise OpenJevNotReadyError(
                    "A Engine Open Jev (Laya) não está pronta ou o modelo não foi carregado. "
                    "Execute load_model() antes de invocar noul, choice ou score."
                )
            return
        if not self.is_ready or self.model is None or self.tokenizer is None:
            raise OpenJevNotReadyError(
                "A Engine Open Jev não está pronta ou o modelo não foi carregado. "
                "Execute load_model() antes de invocar noul, choice ou score."
            )

    async def evaluate_noul(self, request: NoulRequest) -> NoulResponse:
        """Avalia uma proposição lógica/booleana (Noul) em passagem única."""
        self._ensure_ready()
        if getattr(self, "use_ipc", False):
            from .ipc_service import call_ipc_async
            data = await call_ipc_async("noul", request.model_dump())
            return NoulResponse(**data)

        if self.is_laya:
            return await self.laya_adapter.evaluate_noul(request)

        start_time = time.perf_counter()

        if getattr(self, "is_native_jev", False):
            try:
                res = self._native_jev.decide(
                    request.premise,
                    [{"type": "noul", "instructions": request.hypothesis}],
                )
                prob = float(res[0]["noul"])
                prob = max(0.0, min(1.0, prob))
                confidence = abs(prob - 0.5) * 2.0
                passed = prob >= request.threshold
                latency_ms = (time.perf_counter() - start_time) * 1000.0
                return NoulResponse(
                    passed=passed,
                    probability=round(prob, 4),
                    confidence=round(confidence, 4),
                    threshold=request.threshold,
                    latency_ms=round(latency_ms, 2),
                )
            except Exception as e:
                raise InferenceError(f"Falha na inferência nativa 'noul': {e}") from e

        try:
            import torch
            import torch.nn.functional as F

            # Prepara entrada [premise, hypothesis]
            inputs = self.tokenizer(
                request.premise,
                request.hypothesis,
                truncation=True,
                max_length=self.max_length,
                return_tensors="pt",
            )
            inputs = {k: v.to(self.target_device) for k, v in inputs.items()}

            with torch.no_grad():
                outputs = self.model(**inputs)
                logits = outputs.logits

                # Cálculo da probabilidade
                num_classes = logits.shape[-1]
                if num_classes == 1:
                    prob = float(torch.sigmoid(logits[0, 0]).item())
                elif num_classes == 2:
                    probs = F.softmax(logits, dim=-1)
                    prob = float(probs[0, 1].item())
                else:
                    # 3 classes NLI (Contradiction, Neutral, Entailment)
                    probs = F.softmax(logits, dim=-1)
                    entail_prob = float(probs[0, self._entailment_idx].item())
                    # Normalização considerando entailment vs contradição
                    contra_idx = 0 if self._entailment_idx != 0 else 1
                    contra_prob = float(probs[0, contra_idx].item())
                    denom = entail_prob + contra_prob
                    prob = (entail_prob / denom) if denom > 1e-6 else entail_prob

            prob = max(0.0, min(1.0, prob))
            confidence = abs(prob - 0.5) * 2.0
            passed = prob >= request.threshold
            latency_ms = (time.perf_counter() - start_time) * 1000.0

            return NoulResponse(
                passed=passed,
                probability=round(prob, 4),
                confidence=round(confidence, 4),
                threshold=request.threshold,
                latency_ms=round(latency_ms, 2),
            )

        except torch.cuda.OutOfMemoryError as oom_err:
            clean_gpu_memory()
            raise InferenceError(
                f"CUDA Out of Memory na 1050 Ti durante execução de 'noul': {oom_err}. "
                "Reduza o tamanho da premissa ou utilize o fallback para CPU."
            ) from oom_err
        except Exception as e:
            if isinstance(e, OpenJevError):
                raise
            raise InferenceError(f"Erro inesperado durante a inferência 'noul': {e}") from e

    async def evaluate_choice(self, request: ChoiceRequest) -> ChoiceResponse:
        """Roteia para a melhor opção dentre as alternativas fornecidas (Choice)."""
        self._ensure_ready()
        if getattr(self, "use_ipc", False):
            from .ipc_service import call_ipc_async
            data = await call_ipc_async("choice", request.model_dump())
            return ChoiceResponse(**data)

        if self.is_laya:
            return await self.laya_adapter.evaluate_choice(request)

        start_time = time.perf_counter()

        if getattr(self, "is_native_jev", False):
            try:
                res = self._native_jev.decide(
                    request.premise,
                    [{
                        "type": "choice",
                        "instructions": "Select the most suitable category or specialist agent for this task:",
                        "options": request.options,
                    }],
                )
                selected_opt = res[0]["choice"]
                prob_dict = {k: round(float(v), 4) for k, v in res[0]["probabilities"].items()}
                top_conf = round(float(res[0]["confidence"]), 4)
                latency_ms = (time.perf_counter() - start_time) * 1000.0
                return ChoiceResponse(
                    selected=selected_opt,
                    probabilities=prob_dict,
                    top_confidence=top_conf,
                    latency_ms=round(latency_ms, 2),
                )
            except Exception as e:
                raise InferenceError(f"Falha na inferência nativa 'choice': {e}") from e

        try:
            import torch
            import torch.nn.functional as F

            # Constrói hipóteses estruturadas para cada opção
            hypotheses = [
                f"The most suitable category or specialist agent for this task is: {opt}."
                for opt in request.options
            ]
            premises = [request.premise] * len(hypotheses)

            inputs = self.tokenizer(
                premises,
                hypotheses,
                truncation=True,
                padding=True,
                max_length=self.max_length,
                return_tensors="pt",
            )
            inputs = {k: v.to(self.target_device) for k, v in inputs.items()}

            with torch.no_grad():
                outputs = self.model(**inputs)
                logits = outputs.logits

                num_classes = logits.shape[-1]
                if num_classes == 1:
                    raw_scores = logits[:, 0]
                elif num_classes == 2:
                    raw_scores = logits[:, 1]
                else:
                    raw_scores = logits[:, self._entailment_idx]

                # Aplica temperatura sobre as opções
                scaled_scores = raw_scores / max(request.temperature, 1e-4)
                probs = F.softmax(scaled_scores, dim=-1).cpu().tolist()

            prob_dict: Dict[str, float] = {}
            for opt, p in zip(request.options, probs):
                prob_dict[opt] = round(float(p), 4)

            selected_opt = max(prob_dict, key=prob_dict.get)
            top_confidence = prob_dict[selected_opt]
            latency_ms = (time.perf_counter() - start_time) * 1000.0

            return ChoiceResponse(
                selected=selected_opt,
                probabilities=prob_dict,
                top_confidence=top_confidence,
                latency_ms=round(latency_ms, 2),
            )

        except torch.cuda.OutOfMemoryError as oom_err:
            clean_gpu_memory()
            raise InferenceError(
                f"CUDA Out of Memory na 1050 Ti durante execução de 'choice': {oom_err}"
            ) from oom_err
        except Exception as e:
            if isinstance(e, OpenJevError):
                raise
            raise InferenceError(f"Erro inesperado durante a inferência 'choice': {e}") from e

    async def evaluate_score(self, request: ScoreRequest) -> ScoreResponse:
        """Avalia a conformidade de um artefato contra uma rubrica de qualidade (Score)."""
        self._ensure_ready()
        if getattr(self, "use_ipc", False):
            from .ipc_service import call_ipc_async
            data = await call_ipc_async("score", request.model_dump())
            return ScoreResponse(**data)

        if self.is_laya:
            return await self.laya_adapter.evaluate_score(request)

        start_time = time.perf_counter()

        if getattr(self, "is_native_jev", False):
            try:
                # Usa opções ordinais calibradas no vocabulário nativo do Jev
                options = ["critical flaws", "poor quality", "acceptable", "good quality", "exceptional"]
                res = self._native_jev.decide(
                    request.premise,
                    [{
                        "type": "score",
                        "instructions": f"Rate code/artifact quality against rubric: {request.rubric}",
                        "options": options,
                    }],
                )
                raw_score = float(res[0]["score"])  # de 0.0 a 4.0
                normalized = raw_score / (len(options) - 1.0)
                final_score = round(request.min_score + normalized * (request.max_score - request.min_score), 2)
                passed = (final_score >= request.threshold) if request.threshold is not None else None
                breakdown = {k: round(float(v), 4) for k, v in res[0]["probabilities"].items()}
                latency_ms = (time.perf_counter() - start_time) * 1000.0
                return ScoreResponse(
                    score=final_score,
                    normalized_score=round(normalized, 4),
                    passed=passed,
                    breakdown=breakdown,
                    latency_ms=round(latency_ms, 2),
                )
            except Exception as e:
                raise InferenceError(f"Falha na inferência nativa 'score': {e}") from e

        try:
            import torch
            import torch.nn.functional as F

            # Âncoras discretas de qualidade em escala de 5 níveis
            anchors = [
                (1, "The deliverable fails basic requirements and contains critical flaws or errors."),
                (2, "The deliverable partially meets requirements but has significant quality issues."),
                (3, "The deliverable is acceptable and meets core requirements with minor imperfections."),
                (4, "The deliverable is high quality, robust, well-structured and follows best practices."),
                (5, "The deliverable is exceptional, optimal, comprehensive and strictly exceeds the rubric."),
            ]

            hypotheses = [
                f"Based on rubric '{request.rubric}': {desc}"
                for _, desc in anchors
            ]
            premises = [request.premise] * len(hypotheses)

            inputs = self.tokenizer(
                premises,
                hypotheses,
                truncation=True,
                padding=True,
                max_length=self.max_length,
                return_tensors="pt",
            )
            inputs = {k: v.to(self.target_device) for k, v in inputs.items()}

            with torch.no_grad():
                outputs = self.model(**inputs)
                logits = outputs.logits

                num_classes = logits.shape[-1]
                if num_classes == 1:
                    raw_scores = logits[:, 0]
                elif num_classes == 2:
                    raw_scores = logits[:, 1]
                else:
                    raw_scores = logits[:, self._entailment_idx]

                anchor_weights = F.softmax(raw_scores, dim=-1).cpu().tolist()

            # Interpolação ponderada das notas de 1 a 5
            interpolated_raw_score = sum(level * w for (level, _), w in zip(anchors, anchor_weights))

            # Mapeia proporcionalmente para o intervalo [min_score, max_score]
            normalized = (interpolated_raw_score - 1.0) / 4.0  # de 0.0 a 1.0
            final_score = request.min_score + normalized * (request.max_score - request.min_score)
            final_score = round(final_score, 2)

            passed = None
            if request.threshold is not None:
                passed = final_score >= request.threshold

            breakdown = {
                f"level_{level}": round(float(w), 4)
                for (level, _), w in zip(anchors, anchor_weights)
            }

            latency_ms = (time.perf_counter() - start_time) * 1000.0

            return ScoreResponse(
                score=final_score,
                normalized_score=round(normalized, 4),
                passed=passed,
                breakdown=breakdown,
                latency_ms=round(latency_ms, 2),
            )

        except torch.cuda.OutOfMemoryError as oom_err:
            clean_gpu_memory()
            raise InferenceError(
                f"CUDA Out of Memory na 1050 Ti durante execução de 'score': {oom_err}"
            ) from oom_err
        except Exception as e:
            if isinstance(e, OpenJevError):
                raise
            raise InferenceError(f"Erro inesperado durante a inferência 'score': {e}") from e
