"""Servidor FastAPI assíncrono para a Camada de Decisão & Roteamento (Open Jev).
Expõe endpoints para noul, choice, score e monitoramento de saúde da GPU/VRAM.
"""

from contextlib import asynccontextmanager
import logging
from typing import Any, Dict, Optional

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import JSONResponse

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
from .engine import OpenJevEngine
from .hardware import get_hardware_profile

logger = logging.getLogger("jev.server")


def create_app(
    engine: Optional[OpenJevEngine] = None,
    auto_load_model: bool = False,
    model_id: str = "convaiinnovations/laya",
    max_length: int = 1024,
) -> FastAPI:
    """Cria e configura a instância da aplicação FastAPI com backend Laya."""

    # Cria engine se não fornecida
    if engine is None:
        _engine = OpenJevEngine(model_id_or_path=model_id, max_length=max_length)
    else:
        _engine = engine

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Startup
        if auto_load_model:
            try:
                _engine.load_model()
            except Exception as e:
                logger.error(f"Falha na carga automática do modelo Open Jev no startup: {e}")
        yield
        # Shutdown
        _engine.model = None
        _engine.tokenizer = None
        _engine.is_ready = False

    app = FastAPI(
        title="Open Jev Decision API",
        description="Microserviço de Decisão e Roteamento Estruturado Ultra-Rápido (Primitivas Noul, Choice, Score)",
        version="0.1.0",
        lifespan=lifespan,
    )

    # Armazena referência da engine no state
    app.state.engine = _engine

    # ==========================================================================
    # Exception Handlers (Fail-Fast: Sem Mocks)
    # ==========================================================================

    @app.exception_handler(OpenJevNotReadyError)
    async def not_ready_handler(request, exc: OpenJevNotReadyError):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": "OpenJevNotReadyError",
                "message": str(exc),
                "detail": "O modelo discriminativo Open Jev ainda não está carregado em memória. Execute /v1/decision/load ou configure auto_load_model=True.",
            }
        )

    @app.exception_handler(HardwareUnavailableError)
    async def hardware_unavailable_handler(request, exc: HardwareUnavailableError):
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": "HardwareUnavailableError",
                "message": str(exc),
                "detail": "Recursos de hardware (GPU Pascal / VRAM / PyTorch) não atendem aos pré-requisitos para execução.",
            }
        )

    @app.exception_handler(InferenceError)
    async def inference_error_handler(request, exc: InferenceError):
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={
                "error": "InferenceError",
                "message": str(exc),
                "detail": "Falha matemática ou estouro de memória (OOM) durante o forward pass na GPU/CPU.",
            }
        )

    # ==========================================================================
    # Endpoints de Decisão
    # ==========================================================================

    @app.post(
        "/v1/decision/noul",
        response_model=NoulResponse,
        summary="Avaliação de Condição Booleana / Guardrail",
    )
    async def evaluate_noul(request: NoulRequest) -> NoulResponse:
        """Avalia se a premissa satisfaz a hipótese booleana, retornando probabilidade calibrada."""
        return await app.state.engine.evaluate_noul(request)

    @app.post(
        "/v1/decision/choice",
        response_model=ChoiceResponse,
        summary="Roteamento Categórico de Especialistas",
    )
    async def evaluate_choice(request: ChoiceRequest) -> ChoiceResponse:
        """Determina a categoria/subagente ideal para a tarefa com distribuição de probabilidade."""
        return await app.state.engine.evaluate_choice(request)

    @app.post(
        "/v1/decision/score",
        response_model=ScoreResponse,
        summary="Avaliação Ordinal e Nota de Rubrica",
    )
    async def evaluate_score(
        request: ScoreRequest,
        task_type: str = "entrega",
        workspace: str = "",
    ) -> ScoreResponse:
        """Gate de entrega portado do MCP: premissa Pedido|Resultado, teto 1500,
        rubrica só do catálogo fechado e anti-premise gaming + circuit breaker.

        `request.rubric` livre é ignorado (rubric gaming). Resposta é ScoreResponse
        (sem diagnosis flat — o choice de defeito vive só no MCP).
        """
        from src.core.score_gate import (
            MAX_ATTEMPTS,
            get_workspace_git_signature,
            history_get,
            history_increment_attempts,
            history_set,
            resolve_task_rubric,
            validate_score_premise,
        )

        premise_error = validate_score_premise(request.premise)
        if premise_error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
                if hasattr(status, "HTTP_422_UNPROCESSABLE_CONTENT")
                else status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=premise_error,
            )
        resolved_rubric, rubric_error = resolve_task_rubric(task_type)
        if rubric_error:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT
                if hasattr(status, "HTTP_422_UNPROCESSABLE_CONTENT")
                else status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=rubric_error,
            )
        request.rubric = resolved_rubric

        target_ws = workspace.strip() if workspace and workspace.strip() else "."
        git_state = get_workspace_git_signature(target_ws)
        hist = history_get(target_ws)
        if hist and not hist.get("passed", True) and git_state and git_state == hist.get("git_state", ""):
            attempts = hist.get("attempts", 1)
            if attempts >= MAX_ATTEMPTS:
                raise HTTPException(
                    status_code=status.HTTP_409_CONFLICT,
                    detail={
                        "error": "emergency_exit_unlocked",
                        "attempts": attempts,
                        "max_attempts": MAX_ATTEMPTS,
                        "message": "Saída de emergência desbloqueada — emitir Laudo de Inviabilidade Técnica.",
                    },
                )
            history_increment_attempts(target_ws)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail={
                    "error": "no_change_since_last_rejection",
                    "attempts": attempts,
                    "max_attempts": MAX_ATTEMPTS,
                    "message": "Nenhuma alteração no disco desde a última reprovação.",
                },
            )

        resp = await app.state.engine.evaluate_score(request)

        # Borderline Mass Gate (paridade com MCP): aprovação matemática se a distribuição é saudável
        if not resp.passed and getattr(resp, "borderline", False) and getattr(resp, "breakdown", None):
            from src.decision.laya_adapter import evaluate_borderline_mass_gate
            if evaluate_borderline_mass_gate(resp.breakdown):
                resp.passed = True
                resp.approved_as_borderline = True

        if resp.passed:
            history_set(target_ws, {
                "passed": True,
                "attempts": 0,
                "git_state": git_state,
                "last_score": round(resp.score, 2),
            })
        else:
            prev = hist.get("attempts", 0) if hist else 0
            history_set(target_ws, {
                "passed": False,
                "attempts": prev + 1,
                "git_state": git_state,
                "last_score": round(resp.score, 2),
            })
        return resp

    @app.post(
        "/v1/decision/load",
        summary="Carregar Modelo na Memória",
    )
    async def load_model() -> Dict[str, Any]:
        """Dispara o carregamento do modelo na GPU/CPU."""
        app.state.engine.load_model()
        return {
            "status": "ready",
            "model_id": app.state.engine.model_id_or_path,
            "device": app.state.engine.target_device,
        }

    @app.get(
        "/v1/decision/health",
        summary="Status do Serviço e Telemetria de Hardware",
    )
    async def health_check() -> Dict[str, Any]:
        """Informa a prontidão da engine e estatísticas de hardware da GTX 1050 Ti."""
        profile = get_hardware_profile()
        return {
            "status": "healthy" if app.state.engine.is_ready else "not_ready",
            "is_ready": app.state.engine.is_ready,
            "model_id": app.state.engine.model_id_or_path,
            "active_device": getattr(app.state.engine, "target_device", "unassigned"),
            "hardware": {
                "device_name": profile.device_name,
                "has_cuda": profile.has_cuda,
                "compute_capability": profile.compute_capability,
                "is_pascal": profile.is_pascal_or_older,
                "total_vram_mb": profile.total_vram_mb,
                "free_vram_mb": profile.free_vram_mb,
                "bfloat16_supported": profile.supports_bfloat16,
                "recommended_dtype": profile.recommended_dtype,
            }
        }

    return app


# Instância padrão para execução via Uvicorn CLI
app = create_app()
