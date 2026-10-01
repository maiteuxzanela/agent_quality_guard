"""Servidor FastAPI assíncrono para a Camada de Decisão & Roteamento (Open Jev).
Expõe endpoints para noul, choice, score e monitoramento de saúde da GPU/VRAM.
"""

from contextlib import asynccontextmanager
import logging
from pathlib import Path
from typing import Any, AsyncIterator, Dict, Optional

from fastapi import FastAPI, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse

from ..core.schemas import (
    ChoiceRequest,
    ChoiceResponse,
    HardwareUnavailableError,
    InferenceError,
    Needle3Query,
    Needle3Response,
    NeedleSearchHttpRequest,
    NoulRequest,
    NoulResponse,
    OpenJevError,
    OpenJevNotReadyError,
    ScoreRequest,
    ScoreResponse,
)
from ..memory.local_needle import LocalNeedleEngine
from .engine import OpenJevEngine
from .hardware import get_hardware_profile
from .needle_ui_html import NEEDLE_UI_HTML

logger = logging.getLogger("jev.server")


def _unprocessable(detail: Any) -> HTTPException:
    """422 canônico (evita repetir a checagem de compatibilidade do status code)."""
    if hasattr(status, "HTTP_422_UNPROCESSABLE_CONTENT"):
        code = status.HTTP_422_UNPROCESSABLE_CONTENT
    else:
        code = status.HTTP_422_UNPROCESSABLE_ENTITY
    return HTTPException(status_code=code, detail=detail)


def _block_if_unchanged(target_ws: str, git_state: str, hist: Optional[Dict]) -> None:
    """Anti-premise gaming: reprovação sem mudança no disco => 409 (+breaker).

    Pré-condições checadas em passos curtos (evita predicado encadeado denso):
    histórico existe, última avaliação reprovada e assinatura git idêntica.
    """
    if not hist or hist.get("passed", True):
        return
    if not git_state or git_state != hist.get("git_state", ""):
        return
    attempts = hist.get("attempts", 1)

    from src.core.score_gate import MAX_ATTEMPTS, history_increment_attempts

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


def _apply_caveat_band(resp: ScoreResponse, threshold: Optional[float]) -> None:
    """Paridade com MCP: banda de indiferença (CAVEAT_BAND) decide a ressalva.

    Nota em [threshold - CAVEAT_BAND, threshold) => passed=true com
    approved_with_caveat=true (o endpoint HTTP não tem Screener — mesmas
    condições do caminho mono do MCP). Abaixo da banda o corte é estrito.
    O Borderline Mass Gate vira apenas telemetria (approved_as_borderline).
    """
    if resp.passed or threshold is None:
        return
    from src.core.score_gate import caveat_decision

    if caveat_decision(resp.score, threshold) != "caveat":
        return
    resp.passed = True
    resp.approved_with_caveat = True
    if getattr(resp, "breakdown", None):
        from src.decision.laya_adapter import evaluate_borderline_mass_gate

        if evaluate_borderline_mass_gate(resp.breakdown)[0]:
            resp.approved_as_borderline = True


def create_app(
    engine: Optional[OpenJevEngine] = None,
    auto_load_model: bool = False,
    model_id: str = "convaiinnovations/laya",
    max_length: int = 1024,
) -> FastAPI:
    """Cria e configura a instância da aplicação FastAPI com backend Laya.

    Exemplo:
        app = create_app(auto_load_model=True)
        # ou com engine já carregada: app = create_app(engine=meu_engine)
    """

    # Cria engine se não fornecida
    if engine is None:
        _engine = OpenJevEngine(model_id_or_path=model_id, max_length=max_length)
    else:
        _engine = engine

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        """Carrega o modelo no startup (se configurado) e libera os recursos no shutdown."""
        # Startup
        if auto_load_model:
            try:
                _engine.load_model()
            except Exception as e:
                logger.error(
                    f"Falha na carga automática do modelo Open Jev no startup: {e}",
                    exc_info=True,
                )
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
    async def not_ready_handler(request: Request, exc: OpenJevNotReadyError) -> JSONResponse:
        """503 canônico quando o modelo ainda não está carregado na memória."""
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": "OpenJevNotReadyError",
                "message": str(exc),
                "detail": "O modelo discriminativo Open Jev ainda não está carregado em memória. Execute /v1/decision/load ou configure auto_load_model=True.",
            }
        )

    @app.exception_handler(HardwareUnavailableError)
    async def hardware_unavailable_handler(
        request: Request, exc: HardwareUnavailableError
    ) -> JSONResponse:
        """503 canônico quando GPU/VRAM/PyTorch não atendem aos pré-requisitos."""
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": "HardwareUnavailableError",
                "message": str(exc),
                "detail": "Recursos de hardware (GPU Pascal / VRAM / PyTorch) não atendem aos pré-requisitos para execução.",
            }
        )

    @app.exception_handler(InferenceError)
    async def inference_error_handler(request: Request, exc: InferenceError) -> JSONResponse:
        """500 canônico para falha matemática ou OOM durante o forward pass."""
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
        (sem diagnosis flat — o diagnóstico estruturado é exclusivo do MCP, via
        Screener Needle 3 + Noul; choice de defeito removido em set/2026).
        """
        from src.core.score_gate import (
            get_workspace_git_signature,
            history_get,
            history_set,
            resolve_task_rubric,
            validate_score_premise,
        )

        premise_error = validate_score_premise(request.premise)
        if premise_error:
            raise _unprocessable(premise_error)
        resolved_rubric, rubric_error = resolve_task_rubric(task_type)
        if rubric_error:
            raise _unprocessable(rubric_error)
        request.rubric = resolved_rubric

        target_ws = workspace.strip() if workspace and workspace.strip() else "."
        git_state = get_workspace_git_signature(target_ws)
        hist = history_get(target_ws)
        _block_if_unchanged(target_ws, git_state, hist)

        resp = await app.state.engine.evaluate_score(request)
        _apply_caveat_band(resp, request.threshold)

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

    # ==========================================================================
    # Endpoints de Busca de Memória (Needle 3 UI & Search)
    # ==========================================================================

    @app.post(
        "/v1/needle/search",
        response_model=Needle3Response,
        summary="Busca Cirúrgica de Código AST (Cactus Needle 3)",
    )
    async def needle_search(request: NeedleSearchHttpRequest) -> Needle3Response:
        """Executa busca relacional de AST e léxica no repositório selecionado."""
        target_ws = (request.workspace_path or ".").strip()
        ws_path = Path(target_ws).resolve()
        if not ws_path.exists() or not ws_path.is_dir():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Caminho de repositório inválido ou inexistente: '{target_ws}'",
            )

        needle_engine = LocalNeedleEngine(workspace_root=str(ws_path))
        query_obj = Needle3Query(
            query=request.query,
            scope=request.scope,
            max_tokens=request.max_tokens,
            relational_depth=request.relational_depth,
        )
        return await needle_engine.search(query_obj)

    @app.get(
        "/v1/needle/ui",
        response_class=HTMLResponse,
        summary="Interface Gráfica HTML para Consulta Needle 3",
    )
    async def needle_ui() -> HTMLResponse:
        """Retorna a interface web simples para consulta ao repositório em linguagem natural."""
        return HTMLResponse(content=NEEDLE_UI_HTML)

    @app.get(
        "/ui",
        response_class=HTMLResponse,
        include_in_schema=False,
    )
    async def needle_ui_shortcut() -> HTMLResponse:
        """Atalho direto para a interface gráfica."""
        return HTMLResponse(content=NEEDLE_UI_HTML)

    return app


# Instância padrão para execução via Uvicorn CLI
app = create_app()
