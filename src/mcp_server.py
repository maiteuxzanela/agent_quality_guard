#!/usr/bin/env python3
"""MCP Server para a Arquitetura Tri-Camada (Projeto JEV).
Disponibiliza Needle 3, Choice, Noul e Score via protocolo padrão MCP (stdio).
"""

import asyncio
import hashlib
import logging
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

# Silencia avisos e garante carregamento local rápido sem chamadas de rede no HuggingFace Hub
os.environ.setdefault("PYTHONWARNINGS", "ignore")
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
warnings.filterwarnings("ignore")

# Redireciona todos os logs para stderr (stdout exclusivo do protocolo MCP stdio)
logging.basicConfig(
    level=logging.INFO,
    stream=sys.stderr,
    format="%(asctime)s [%(levelname)s] [JEV MCP] %(message)s",
)

# Adiciona raiz do projeto JEV ao sys.path
PROJECT_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(PROJECT_DIR))

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from src.core.schemas import (
    BORDERLINE_FLAWED_MAX_MASS,
    BORDERLINE_HEALTHY_MIN_MASS,
    ChoiceRequest,
    ConsultaResponse,
    DEFECT_DESCRIPTIONS,
    DEFECT_MEMBERS,
    DiagnosisResult,
    DiffStageResult,
    Needle3Query,
    NoulRequest,
    RUBRIC_CATALOG,
    ScoreRequest,
    ScoreResponse,
    ScoreDiagnoseResponse,
    QUALITY_DEFECT_CATEGORIES,
    AgentQualityGuardResponse,
    LinterDiagnostic,
    MetaRuleViolation,
    ToolchainToolStatus,
    META_RULE_BLOCKING_CATEGORIES,
    PytestChunkResult,
    ASTViolation,
)
from src.core.diff_inspector import extract_workspace_stages, format_stage_premise
from src.memory.ast_diff_extractor import extract_workspace_ast_symbols, AstSymbolSlice
from src.memory.needle_defect_screener import (
    screen_workspace_symbols,
)
from src.decision.engine import OpenJevEngine
from src.memory.local_needle import LocalNeedleEngine
from src.memory.qmd_search import QmdHybridSearchEngine
from src.memory.needle_ast_guard import NeedleASTGuard
from src.orchestrator.pytest_chunk_runner import PytestChunkRunner
from src.orchestrator.multi_stack_runner import MultiStackRunner

logger = logging.getLogger("jev.mcp")

app = MCPServer("open-jev-server")

_engine_instance: Optional[OpenJevEngine] = None
_engine_lock = threading.Lock()


_ipc_server: Any = None
# Definido como True pelo daemon embutido (src/decision/daemon.py) antes de
# get_engine(): quem detém o socket IPC é o daemon — este processo não compete.
_ipc_disabled: bool = False


def _start_ipc_server_if_needed(engine: OpenJevEngine) -> None:
    """Inicia o servidor IPC em background thread caso este processo detenha a GPU."""
    global _ipc_server
    if _ipc_disabled or _ipc_server is not None:
        return
    # Não sobe IPC se estiver rodando em CPU fallback ou já for cliente IPC
    if getattr(engine, "use_ipc", False) or getattr(engine, "target_device", "") == "cpu":
        return
    try:
        from src.decision.ipc_service import OpenJevIPCServer
        _ipc_server = OpenJevIPCServer(engine=engine)

        def _run_ipc_loop():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                loop.run_until_complete(_ipc_server.start())
            except OSError as exc:
                # Outro servidor (ex.: daemon) já detém o socket — não competir.
                logger.warning("Servidor IPC não iniciado (socket de outro detentor): %s", exc)
                return
            loop.run_forever()

        t = threading.Thread(target=_run_ipc_loop, daemon=True, name="jev-ipc-service")
        t.start()
        logger.info("Servidor IPC Open Jev iniciado em /tmp/openjev_ipc.sock para compartilhamento de GPU.")
    except Exception as e:
        logger.warning("Falha ao iniciar servidor IPC: %s", e)


def get_engine() -> OpenJevEngine:
    """Singleton do motor; thread-safe (chamado via asyncio.to_thread)."""
    global _engine_instance
    if _engine_instance is not None:
        return _engine_instance
    with _engine_lock:
        if _engine_instance is None:
            engine = OpenJevEngine(
                model_id_or_path="convaiinnovations/laya",
                allow_cpu_fallback=True,
            )
            engine.load_model()
            _engine_instance = engine
            _start_ipc_server_if_needed(_engine_instance)
    return _engine_instance


async def _get_engine_async() -> OpenJevEngine:
    """Carrega/busca o motor fora do event loop (load pesado não trava o MCP)."""
    return await asyncio.to_thread(get_engine)


@app.tool(
    name="qmd_search",
    description=(
        "Busca hibrida cirurgica (Ripgrep + Needle 3 + RRF em CPU). "
        "Ferramenta primaria obrigatoria para explorar codigo, funcoes, regras de negocio e investigacao de bugs. "
        "Combina busca lexica exata com analise semantica/AST e ranqueamento Reciprocal Rank Fusion."
    ),
)
async def qmd_search(
    query: str,
    workspace: str = "",
    scope: str = "",
    max_tokens: int = 1200,
    depth: int = 2,
) -> dict:
    """Executa busca híbrida de código via QMD (Ripgrep + Needle 3 + RRF).

    Exemplo:
        >>> import asyncio
        >>> res = asyncio.run(qmd_search(query="needle_verify_delivery"))
        >>> isinstance(res, dict)
        True
    """
    target_workspace = workspace.strip() if workspace and workspace.strip() else os.getcwd()
    engine = QmdHybridSearchEngine(workspace_root=target_workspace)
    scope_list = [s.strip() for s in scope.split(",") if s.strip()] if scope else None

    return await engine.search(
        query=query,
        scope=scope_list,
        max_tokens=max_tokens,
        depth=depth,
    )


@app.tool(
    name="openjev_choice",
    description=(
        "Distribuicao probabilistica (Laya RLCD local) entre subagentes, ferramentas, skills, "
        "arquivos ou caminhos — nao roteamento deterministico. GTX 1050 Ti / CPU com lock de predict."
    ),
)
async def openjev_choice(
    premise: str,
    options: List[str],
    temperature: float = 1.0,
) -> dict:
    """Calcula distribuicao de probabilidades entre subagentes ou caminhos.

    `options` é obrigatório (assinatura compatível com ChoiceRequest, min_length=2).
    """
    if not options or len(options) < 2:
        raise ValueError(
            "openjev_choice exige 'options' com pelo menos 2 opções formais "
            "(compatível com ChoiceRequest.min_length=2). Sem rosters hardcoded."
        )
    if not (0.0 < temperature <= 5.0):
        raise ValueError("temperature deve estar em (0.0, 5.0] (limites de ChoiceRequest).")

    engine = await _get_engine_async()
    req = ChoiceRequest(
        premise=premise,
        options=options,
        temperature=temperature,
    )
    resp = await engine.evaluate_choice(req)

    return {
        "selected": resp.selected,
        "confidence": round(resp.top_confidence, 4),
        "probabilities": {k: round(v, 4) for k, v in resp.probabilities.items()},
        "latency_ms": round(resp.latency_ms, 2),
    }


@app.tool(
    name="openjev_noul",
    description="Guardrail de validacao pre-acao critica (seguranca, isolamento, transacoes) usando Laya RLCD local na GPU.",
)
async def openjev_noul(
    premise: str,
    hypothesis: str,
    threshold: float = 0.78,
) -> dict:
    """Verifica se a premissa implica a hipotese de seguranca com probabilidade >= threshold."""
    engine = await _get_engine_async()
    req = NoulRequest(
        premise=premise,
        hypothesis=hypothesis,
        threshold=threshold,
    )
    resp = await engine.evaluate_noul(req)

    return {
        "passed": resp.passed,
        "probability": round(resp.probability, 4),
        "threshold": resp.threshold,
        "confidence": round(resp.confidence, 4),
        "latency_ms": round(resp.latency_ms, 2),
    }


@app.tool(
    name="openjev_consulta",
    description=(
        "Consulta direta ('esse script funciona?', 'esse banco tem essa informação?') "
        "retornando indício textual SEM booleano de aprovação. Postura: aceitar como "
        "indício de algo a investigar; premise gaming proibido; indício desfavorável "
        "exige investigação e alerta à usuária."
    ),
)
async def openjev_consulta(premise: str, question: str) -> dict:
    """Retorna indício textual (probability/confidence) — sem campo `passed`.

    Usa a primitiva noul do motor por baixo, mas o contrato serializado é
    ConsultaResponse (extra='forbid'): jamais expõe booleano de aprovação
    que convide o agente a praticar premise gaming.
    """
    if not premise.strip() or not question.strip():
        raise ValueError("openjev_consulta exige 'premise' e 'question' não vazios.")

    engine = await _get_engine_async()
    req = NoulRequest(
        premise=premise,
        hypothesis=question,
        threshold=0.5,
    )
    resp = await engine.evaluate_noul(req)
    prob = resp.probability

    if prob >= 0.78:
        forca = "forte a favor"
    elif prob >= 0.5:
        forca = "fraco a favor"
    elif prob >= 0.22:
        forca = "fraco contra"
    else:
        forca = "forte contra"

    indicio = (
        f"Indício {forca} de que a resposta à pergunta é verdadeira "
        f"(P={prob:.4f}). Esta resposta é um INDÍCIO para investigação, não um "
        "veredito nem aprovação: indício desfavorável exige investigar, consertar "
        "o que precisar e alertar a usuária — nunca reformular a premissa para "
        "inverter o resultado (premise gaming proibido)."
    )

    return ConsultaResponse(
        indicio=indicio,
        probability=round(prob, 4),
        confidence=round(resp.confidence, 4),
        latency_ms=round(resp.latency_ms, 2),
    ).model_dump()


DEFAULT_SCORE_RUBRIC = RUBRIC_CATALOG["codigo_producao"]

# Teto rígido da premissa de score (anti-ruído: prosa longa distorce a nota).
from src.core.score_gate import (  # noqa: E402
    MAX_ATTEMPTS as _SCORE_GATE_MAX_ATTEMPTS,
    SCORE_PREMISE_MAX_LEN,
    CAVEAT_BAND as _CAVEAT_BAND,
    caveat_decision as _caveat_decision,
    get_workspace_git_signature as _score_gate_git_signature,
    history_get as _history_get,
    history_increment_attempts as _history_increment_attempts,
    history_set as _history_set,
    resolve_attempt_number as _resolve_attempt_number,
    resolve_task_rubric as _resolve_task_rubric,
    stages_cache_get as _stages_cache_get,
    stages_cache_set as _stages_cache_set,
    validate_score_premise as _validate_score_premise,
    _score_history,
)

MAX_ATTEMPTS = _SCORE_GATE_MAX_ATTEMPTS


def _get_workspace_git_signature(workspace_dir: str) -> str:
    """Assinatura do workspace — delega a score_gate (HEAD + diff + untracked)."""
    return _score_gate_git_signature(workspace_dir)


def _emergency_exit_response(
    threshold: float, attempts: int, last_score: float, reason: str
) -> dict:
    """Resposta-padrão da Saída de Emergência (breaker universal ou anti-gaming)."""
    return {
        "score": last_score,
        "passed": False,
        "threshold": threshold,
        "attempts": attempts,
        "max_attempts": MAX_ATTEMPTS,
        "emergency_exit_unlocked": True,
        "diagnosis": None,
        "message": (
            f"🚨 [SAÍDA DE EMERGÊNCIA DESBLOQUEADA] (Tentativa {attempts}/{MAX_ATTEMPTS}): "
            f"{reason} "
            "O agente está autorizado a emitir Laudo de Inviabilidade Técnica."
        ),
    }


def _no_change_response(attempts: int) -> dict:
    """Recusa anti-premise gaming: avaliação repetida sem mudança no disco."""
    return {
        "error": (
            f"🚨 AVALIAÇÃO RECUSADA (Tentativa {attempts}/{MAX_ATTEMPTS}): Nenhuma alteração detectada "
            "no repositório desde a última reprovação. Refatore o código no disco antes de reavaliar."
        ),
        "attempts": attempts,
        "max_attempts": MAX_ATTEMPTS,
        "emergency_exit_unlocked": False,
    }


def _evaluation_gate(
    target_workspace: str,
    hist: Optional[Dict[str, Any]],
    current_git_state: str,
    threshold: float,
) -> Optional[dict]:
    """Camada 0: circuit breaker universal + anti-premise gaming, sem aninhamento.

    Condições checadas em passos curtos com retorno antecipado (evita predicado
    denso e cadeia de if de nível 3). Devolve a resposta-padrão de bloqueio ou
    ``None`` quando a avaliação pode prosseguir.
    """
    if not hist or hist.get("passed", True):
        return None
    attempts = int(hist.get("attempts", 1))
    last_score = hist.get("last_score", 0.0)
    if attempts >= MAX_ATTEMPTS:
        logger.warning(
            f"🚨 [Circuit Breaker Universal]: Limite de {MAX_ATTEMPTS} tentativas atingido para {target_workspace}. "
            "Desbloqueando Saída de Emergência."
        )
        return _emergency_exit_response(
            threshold,
            attempts,
            last_score,
            f"Limite de {MAX_ATTEMPTS} tentativas consecutivas atingido.",
        )
    prev_state = hist.get("git_state", "")
    if current_git_state != prev_state or current_git_state == "":
        return None
    new_attempts = _history_increment_attempts(target_workspace)
    if new_attempts >= MAX_ATTEMPTS:
        return _emergency_exit_response(
            threshold,
            new_attempts,
            last_score,
            f"Limite de {MAX_ATTEMPTS} tentativas atingido sem alteração no repositório.",
        )
    return _no_change_response(new_attempts)


def _flag_suspect_failed_stages(
    stages: List[Any], refuted_suspicions: List[Dict[str, Any]]
) -> None:
    """Na reprovação por suspeita não-resolvida, marca as etapas afetadas."""
    suspect_keys = {
        f"{r['file_path']}::{r['symbol_name']}" for r in refuted_suspicions
    }
    for st in stages:
        if not st.cached and st.stage_id in suspect_keys:
            st.passed = False


def _cache_passed_stages(
    stages: List[Any], symbols_map: Dict[str, Any], target_workspace: str
) -> None:
    """Persiste estágios aprovados no cache do workspace."""
    for st in stages:
        if st.passed:
            sym_obj = symbols_map.get(st.stage_id)
            s_h = hashlib.sha256(sym_obj.content.encode("utf-8")).hexdigest() if sym_obj else ""
            _stages_cache_set(target_workspace, st.stage_id, {
                "score": st.score,
                "passed": True,
                "hash": s_h,
            })


def _is_test_symbol_path(file_path: str) -> bool:
    """True quando o símbolo vem de um diretório de testes (tests/, test/)."""
    return any(part in ("tests", "test") for part in file_path.split("/"))


def _truncate_at_line(text: str, limit: int) -> str:
    """Trunca `text` em no máximo `limit` caracteres, SEMPRE em fronteira de linha.

    O corte cego de caracteres (ex.: `text[:350]`) podia fatiar uma declaração no
    meio (ex.: `out: ` de `out: list[int] = []`), apresentando ao Laya código
    sintaticamente quebrado e derrubando a nota mesmo com entrega impecável.
    """
    if len(text) <= limit:
        return text
    window = text[:limit]
    cut = window.rfind("\n")
    if cut < 60:
        return window
    return window[:cut]


def _build_code_context(premise: str, symbols: List[Any], headroom: int = 1480) -> str:
    """Seleciona os snippets de código que enriquecem a premissa de score.

    - Ordem estável alternando símbolos de produção e de teste (round-robin),
      preenchendo o orçamento de caracteres em vez de um cap cego de 3 símbolos;
    - garante pelo menos UM snippet de teste quando o diff contém testes, para
      que o critério "cobertura de testes reais" da rubrica codigo_producao
      tenha evidência de código (não só prosa na premissa);
    - nunca excede o teto da premissa combinada (o score_gate recusa > 1500).
    """
    marker = "\n\nSímbolos alterados:\n"
    if "\nSímbolos alterados:\n" in premise:
        return ""
    budget = headroom - len(premise) - len(marker)
    if budget < 60:
        return ""
    src_syms = [s for s in symbols if not _is_test_symbol_path(s.file_path)]
    test_syms = [s for s in symbols if _is_test_symbol_path(s.file_path)]
    ordered: List[Any] = []
    for i in range(max(len(src_syms), len(test_syms))):
        if i < len(src_syms):
            ordered.append(src_syms[i])
        if i < len(test_syms):
            ordered.append(test_syms[i])
    parts: List[str] = []
    used = 0
    for s in ordered:
        # full_context inclui o cabeçalho de imports: sem ele, trechos de teste
        # exibem chamadas sem definição (ex.: add(...)) e leem como código quebrado.
        piece = (
            f"Arquivo {s.file_path} ({s.symbol_type} {s.symbol_name}):\n"
            f"{_truncate_at_line(s.full_context, 350)}"
        )
        cost = len(piece) + (5 if parts else 0)
        if used + cost > budget:
            break
        parts.append(piece)
        used += cost
    return "\n---\n".join(parts)


def _build_failure_diagnosis(
    confirmed_defects: List[Dict[str, Any]],
    refuted_suspicions: List[Dict[str, Any]],
    delivery_score: float,
    threshold: float,
    latency_ms: float,
    quality_confirmed: Optional[List[Dict[str, Any]]] = None,
) -> tuple:
    """Monta ``(diagnosis, message)`` da reprovação por prioridade:
    (1) defeito duro confirmado pelo Noul, (2) suspeita dura não-resolvida de
    maior probabilidade (investigar mesmo com Noul negativo), (3) categoria de
    análise confirmada (tier qualidade — só quando não há suspeita dura)."""
    if confirmed_defects:
        confirmed_defects.sort(key=lambda d: d["probability"], reverse=True)
        top_defect = confirmed_defects[0]
        diag = DiagnosisResult(
            selected=top_defect["category"],
            confidence=top_defect["probability"],
            probabilities={top_defect["category"]: top_defect["probability"]},
            description=top_defect["description"],
            candidate_defects=top_defect["candidate_defects"],
            diagnosis_latency_ms=latency_ms,
        )
        msg = (
            f"Defeito '{top_defect['category']}' confirmado pelo Noul (P={top_defect['probability']:.1%}) "
            f"em {top_defect['file_path']}::{top_defect['symbol_name']} (linhas {top_defect['start_line']}-{top_defect['end_line']}): "
            f"{top_defect['evidence']}"
        )
        return diag, msg
    if refuted_suspicions:
        top_susp = max(refuted_suspicions, key=lambda r: r["probability"])
        diag = DiagnosisResult(
            selected=top_susp["category"],
            confidence=top_susp["probability"],
            probabilities={top_susp["category"]: top_susp["probability"]},
            description=DEFECT_DESCRIPTIONS.get(top_susp["category"], ""),
            candidate_defects=list(DEFECT_MEMBERS.get(top_susp["category"], [])),
            diagnosis_latency_ms=latency_ms,
        )
        msg = (
            f"Score insuficiente ({delivery_score:.2f}/{threshold:.2f}) com suspeita estrutural "
            f"do Needle 3 em {top_susp['file_path']}::{top_susp['symbol_name']} "
            f"(linhas {top_susp['start_line']}-{top_susp['end_line']}, categoria "
            f"'{top_susp['category']}'): {top_susp['evidence']}. O Noul refutou a hipótese "
            f"(P={top_susp['probability']:.1%} < 0.78), mas a nota está abaixo do corte: "
            "investigar o símbolo no disco antes de reavaliar."
        )
        return diag, msg
    if quality_confirmed:
        quality_confirmed.sort(key=lambda d: d["probability"], reverse=True)
        top_quality = quality_confirmed[0]
        diag = DiagnosisResult(
            selected=top_quality["category"],
            confidence=top_quality["probability"],
            probabilities={top_quality["category"]: top_quality["probability"]},
            description=top_quality["description"],
            candidate_defects=top_quality["candidate_defects"],
            diagnosis_latency_ms=latency_ms,
        )
        msg = (
            f"Categoria de análise '{top_quality['category']}' confirmada pelo Noul "
            f"(P={top_quality['probability']:.1%}) em {top_quality['file_path']}::"
            f"{top_quality['symbol_name']} (linhas {top_quality['start_line']}-{top_quality['end_line']}): "
            f"{top_quality['evidence']} Score ({delivery_score:.2f}/{threshold:.2f}) abaixo do corte: "
            "investigar e refinar o símbolo no disco antes de reavaliar."
        )
        return diag, msg
    raise ValueError(
        "Nenhuma suspeita disponível para diagnosis: chamador deve barrar "
        "reprovação sem suspeita (caminho da banda de indiferença)."
    )


def _caveat_message(delivery_score: float, threshold: float, zero_suspicions: bool) -> Optional[str]:
    """Mensagem da aprovação com ressalva na banda de indiferença (None se não é ressalva)."""
    floor = threshold - _CAVEAT_BAND
    if delivery_score >= threshold:
        return None
    if zero_suspicions:
        return (
            f"Entrega aprovada com ressalva: score {delivery_score:.2f} caiu na banda de "
            f"indiferença [{floor:.2f}, {threshold:.2f}) abaixo do corte de {threshold:.2f} e o "
            "Needle 3 não apontou nenhuma suspeita estrutural a investigar."
        )
    return (
        f"Entrega aprovada com ressalva: score {delivery_score:.2f} caiu na banda de "
        f"indiferença [{floor:.2f}, {threshold:.2f}) e o workspace permanece idêntico ao "
        "aprovado anteriormente (cache de estágios)."
    )




# NOTA: a tool `openjev_score` foi expurgada (set/2026) — todo o fluxo de
# score (validação de premissa, catálogo de rubricas, anti-premise gaming,
# circuit breaker e rubric_source) vive exclusivamente em
# `openjev_score_and_diagnose`, logo abaixo.

# Circuit breaker (MAX_ATTEMPTS) vem de score_gate — fonte única.

# NOTA (out/2026, TICKET-HARNESS-43): o pipeline de homologação de entrega
# (`agent_quality_guard`) é 100% DETERMINÍSTICO — Laya/Noul foram removidos do
# caminho crítico. A antiga arbitragem probabilística (Etapa 3) operava sobre as
# 10 categorias do Screener via as constantes `_CATEGORY_PRIORITY` e
# `MAX_ARBITRATIONS`, agora descontinuadas. O Screener permanece apenas como
# telemetria estrutural; o bloqueio determinístico vem das Camadas 1 (toolchain
# nativa) e 2 (meta-regras Tree-Sitter). Ver DEFECT_OPTIONS em src/core/schemas.py.


# ──────────────────────────────────────────────────────────────────────────────
# Agent Quality Guard — pipeline 100% DETERMINÍSTICO de homologação de entrega.
# Camada 0: testes reais | Camada 1: Guard + toolchain nativa | Camada 2: meta-regras.
# Laya RLCD / JEV Noul NÃO participam do caminho crítico (sem pesos de ML/GPU).
# ──────────────────────────────────────────────────────────────────────────────

_QUALITY_GUARD_DESCRIPTION = (
    "Validação de entrega de código 100% determinística e SEM ML (Pytest Real + "
    "Camada 1 compiladores/linters/guard + Camada 2 meta-regras Tree-Sitter). "
    "Executa em ordem fail-fast: "
    "1º testes reais (pytest em 3-4 chunks sequenciais / runner multi-stack); "
    "2º NeedleASTGuard (anti-mock em produção, SQL Injection, eval perigoso, segredos hardcoded); "
    "3º bridge nativa de compiladores/linters do projeto (tsc, eslint/biome, ruff/mypy, clippy, go vet); "
    "4º meta-regras universais de AST via Tree-Sitter (floating promises, silent catch, leaks). "
    "Circuit breaker universal de 5 tentativas: atinge a Saída de Emergência na 5ª tentativa (EMERGENCY_RELEASE)."
)

_LEGACY_ALIAS_DESCRIPTION = (
    "Alias legado de agent_quality_guard (MESMA implementação e assinatura). "
    "Mantido para compatibilidade retroativa total com clientes antigos. "
    "Pipeline 100% determinístico, sem Laya/Noul no caminho crítico. "
    "Circuit breaker universal de 5 tentativas com Saída de Emergência (EMERGENCY_RELEASE)."
)

# Stacks cujo runner multi-stack executa a suíte real de testes.
_MULTISTACK_STACKS = (
    "REACT_NATIVE_EXPO",
    "WEB_JS_FIREBASE",
    "PYTHON_FATURAMENTO",
    "PYTHON_PREDICTERA",
    "NODE_TS_BUN",
)
# Subconjunto JS/TS que ainda usa o scan AST dedicado (scan_js_ast) como Camada 1.
_JS_MULTISTACK_STACKS = ("REACT_NATIVE_EXPO", "WEB_JS_FIREBASE", "NODE_TS_BUN")


def _coerce_violation(raw: Any, model: type) -> Optional[Any]:
    """Normaliza um item bruto (dict ou ASTViolation) para o modelo tipado alvo.

    Fail-soft: item com shape inesperado é ignorado com log de depuração — uma
    ferramenta externa nunca derruba o quality gate por formato de saída.
    """
    if isinstance(raw, ASTViolation):
        return raw
    if isinstance(raw, dict):
        try:
            return model(**raw)
        except Exception as exc:  # noqa: BLE001 — shape externo imprevisível
            logger.debug("Violação ignorada (shape inválido para %s): %s", model.__name__, exc)
            return None
    return None


def _coerce_violations(items: Any, model: type) -> List[Any]:
    """Converte iterável de itens brutos em lista de modelos tipados válidos."""
    coerced: List[Any] = []
    for raw in items or []:
        item = _coerce_violation(raw, model)
        if item is not None:
            coerced.append(item)
    return coerced


def _run_layer1_native_bridge(target_path: Path, stack: str, audit_mode: str) -> Dict[str, Any]:
    """Camada 1 — bridge nativa de compiladores/linters (determinística, fail-open).

    Delega a ``MultiStackRunner.run_native_bridge``. Se a bridge ainda não existir
    no runner ou falhar, devolve listas vazias SEM bloquear a entrega:
    indisponibilidade de ferramenta JAMAIS é violação (fail-open).
    """
    empty: Dict[str, Any] = {
        "available": False, "blocking": [], "warnings": [], "tools": [], "total_files": 0,
    }
    bridge = getattr(MultiStackRunner, "run_native_bridge", None)
    if not callable(bridge):
        logger.debug("Camada 1: MultiStackRunner.run_native_bridge indisponível (fail-open).")
        return empty
    try:
        res = bridge(target_path, stack, audit_mode=audit_mode) or {}
    except Exception as exc:  # noqa: BLE001 — fail-open por contrato
        logger.warning("Camada 1 (native bridge) falhou — fail-open sem bloqueio: %s", exc)
        return empty
    if not isinstance(res, dict):
        logger.warning("Camada 1 (native bridge) retornou %s — fail-open.", type(res).__name__)
        return empty
    return {
        "available": True,
        "blocking": res.get("blocking") or [],
        "warnings": res.get("warnings") or [],
        "tools": res.get("tools") or [],
        "total_files": int(res.get("total_files", 0) or 0),
    }


def _run_layer2_meta_rules(target_path: Path, audit_mode: str) -> Dict[str, Any]:
    """Camada 2 — meta-regras universais de AST via Tree-Sitter (fail-open).

    Prefere ``TreeSitterGuard.scan_workspace_report`` (agregado já classificado);
    recorre a ``scan_workspace`` (iterável) classificando por ``severity``/
    ``category``. Módulo ausente = fail-open (nenhum bloqueio).
    """
    empty: Dict[str, Any] = {"available": False, "blocking": [], "warnings": [], "total_files": 0}
    try:
        from src.memory.tree_sitter_guard import TreeSitterGuard  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001
        logger.debug("Camada 2: TreeSitterGuard indisponível (fail-open): %s", exc)
        return empty

    try:
        report = getattr(TreeSitterGuard, "scan_workspace_report", None)
        if callable(report):
            res = report(target_path, audit_mode=audit_mode) or {}
            if not isinstance(res, dict):
                return empty
            return {
                "available": True,
                "blocking": res.get("blocking") or [],
                "warnings": res.get("warnings") or [],
                "total_files": int(res.get("total_files", 0) or 0),
            }
        scan = getattr(TreeSitterGuard, "scan_workspace", None)
        if not callable(scan):
            return empty
        items = scan(target_path, audit_mode=audit_mode) or []
        blocking: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []
        for item in items:
            data = item.to_dict() if hasattr(item, "to_dict") else (item if isinstance(item, dict) else {})
            severity = str(data.get("severity", "warning")).lower()
            if severity == "blocking" or data.get("category") in META_RULE_BLOCKING_CATEGORIES:
                blocking.append(data)
            else:
                warnings.append(data)
        return {"available": True, "blocking": blocking, "warnings": warnings, "total_files": 0}
    except Exception as exc:  # noqa: BLE001 — fail-open por contrato
        logger.warning("Camada 2 (tree-sitter) falhou — fail-open sem bloqueio: %s", exc)
        return empty


async def _quality_guard_core(
    task_type: str = "codigo_producao",
    workspace: str = "",
    attempt_number: int = 1,
    pedido: str = "",
    resultado: str = "",
    engine: Any = None,
    audit_mode: str = "delta",
    ctx: Optional[Context] = None,
) -> dict:
    """Homologa uma entrega com pipeline 100% determinístico (sem ML/Laya).

    Camada 0: testes reais; Camada 1: NeedleASTGuard + bridge nativa de
    compiladores/linters; Camada 2: meta-regras universais de AST via Tree-Sitter.
    A contagem de tentativas é exclusivamente server-side
    (``score_gate.resolve_attempt_number``, histórico persistido por workspace);
    ``attempt_number`` é apenas informativo e NÃO participa da decisão do
    circuit breaker. Não existe parâmetro de reset de histórico.

    ``engine`` é aceito apenas por retrocompatibilidade de assinatura e é
    IGNORADO: a validação de entrega não usa Laya RLCD/Noul nem inferência na GPU.

    Exemplo:
        >>> import asyncio, tempfile
        >>> with tempfile.TemporaryDirectory() as ws:
        ...     out = asyncio.run(_quality_guard_core(workspace=ws))
        >>> out["status"] in {"APPROVED", "REJECTED", "EMERGENCY_RELEASE"}
        True
    """
    del engine  # retrocompat de assinatura: ML/Laya removido do caminho crítico
    target_workspace = workspace.strip() if workspace and workspace.strip() else os.getcwd()

    hist = _history_get(target_workspace)
    current_attempt = _resolve_attempt_number(hist)
    if attempt_number != current_attempt:
        logger.info(
            "attempt_number do chamador (%s) é informativo; tentativa real server-side = %s.",
            attempt_number,
            current_attempt,
        )

    # ── 1. CIRCUIT BREAKER UNIVERSAL (Limite de 5 Tentativas -> Saída de Emergência) ──
    if current_attempt >= MAX_ATTEMPTS:
        already_emergency = bool(hist and hist.get("emergency"))
        if already_emergency:
            logger.warning(
                f"🚨 [Circuit Breaker - STATUS TERMINAL] Saída de Emergência já liberada anteriormente "
                f"para {target_workspace}. Re-execução sem novo commit bloqueada; mantendo liberação com ressalva."
            )
            msg = (
                f"🚨 [Circuit Breaker - STATUS TERMINAL] Saída de Emergência ativada na tentativa "
                f"{current_attempt}/{MAX_ATTEMPTS}. Entrega liberada COM RESSALVA. "
                "Ciclo de tentativas ENCERRADO. NÃO re-execute needle_verify_delivery para este mesmo commit. "
                "Comite as alterações (git commit) para iniciar novo ciclo, ou proceda imediatamente "
                "para a auditoria do Revisor Crítico (subagent_adversarial) acompanhada do Laudo Técnico."
            )
        else:
            logger.warning(f"🚨 [Circuit Breaker] 5ª tentativa atingida em {target_workspace}. Saída de Emergência liberada.")
            msg = (
                f"🚨 [Circuit Breaker - STATUS TERMINAL] Saída de Emergência ativada na tentativa "
                f"{current_attempt}/{MAX_ATTEMPTS}. Entrega liberada COM RESSALVA "
                "(pytest/Guard/Screener NÃO executados nesta chamada). Ciclo de tentativas encerrado: "
                "proceda imediatamente com a auditoria do Revisor Crítico e Laudo Técnico de Inviabilidade."
            )

        _history_set(target_workspace, {
            "passed": True,
            "attempts": current_attempt,
            "emergency": True
        })
        return AgentQualityGuardResponse(
            passed=True,
            status="EMERGENCY_RELEASE",
            attempt_number=current_attempt,
            approved_with_caveat=True,
            pedido_audit=pedido or None,
            resultado_audit=resultado or None,
            pytest_success=False,
            pytest_chunks=[],
            blocking_ast_violations=[],
            structural_ast_warnings=[],
            confirmed_defects=[],
            dismissed_warnings=[],
            total_files_scanned=0,
            total_lines_scanned=0,
            message=msg
        ).model_dump()

    target_path = Path(target_workspace)
    stack = MultiStackRunner.detect_stack(target_path)
    tmp_dir = Path(tempfile.gettempdir()) / "needle_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    if stack in _MULTISTACK_STACKS:
        # Execução Multi-Stack especializada
        test_res = MultiStackRunner.run_tests(target_path, stack, tmp_dir)
        test_success = test_res.get("passed", False)
        pytest_chunks_typed = [PytestChunkResult(
            chunk_id=f"chunk_multistack_{stack.lower()}",
            passed=test_success,
            total_passed=test_res.get("total_passed", 0),
            total_failed=test_res.get("total_failed", 0),
            execution_time_seconds=test_res.get("duration", 0.0),
            failure_details=test_res.get("failure_details", []),
        )]

        if not test_success:
            _history_set(target_workspace, {
                "passed": False,
                "attempts": current_attempt
            })
            return AgentQualityGuardResponse(
                passed=False,
                status="REJECTED",
                attempt_number=current_attempt,
                pytest_success=False,
                pytest_chunks=pytest_chunks_typed,
                blocking_ast_violations=[],
                structural_ast_warnings=[],
                confirmed_defects=[],
                dismissed_warnings=[],
                total_files_scanned=0,
                total_lines_scanned=0,
                pedido_audit=pedido or None,
                resultado_audit=resultado or None,
                message=f"🚨 [{stack} Fail-Fast] Suíte de testes reais falhou: {test_res.get('summary', '')}."
            ).model_dump()

        if stack in _JS_MULTISTACK_STACKS:
            ast_scan = MultiStackRunner.scan_js_ast(target_path, stack, audit_mode=audit_mode)

            # Camadas determinísticas 1 (toolchain nativa) e 2 (meta-regras Tree-Sitter)
            layer1 = _run_layer1_native_bridge(target_path, stack, audit_mode)
            layer2 = _run_layer2_meta_rules(target_path, audit_mode)

            blocking_violations = _coerce_violations(ast_scan.get("blocking", []), ASTViolation)
            blocking_violations += _coerce_violations(layer1["blocking"], LinterDiagnostic)
            blocking_violations += _coerce_violations(layer2["blocking"], MetaRuleViolation)

            structural_warnings = _coerce_violations(ast_scan.get("warnings", []), ASTViolation)
            structural_warnings += _coerce_violations(layer1["warnings"], LinterDiagnostic)
            structural_warnings += _coerce_violations(layer2["warnings"], MetaRuleViolation)

            layer1_diags = (
                _coerce_violations(layer1["blocking"], LinterDiagnostic)
                + _coerce_violations(layer1["warnings"], LinterDiagnostic)
            )
            layer2_viols = (
                _coerce_violations(layer2["blocking"], MetaRuleViolation)
                + _coerce_violations(layer2["warnings"], MetaRuleViolation)
            )
            toolchain_status = _coerce_violations(layer1["tools"], ToolchainToolStatus)

            passed = len(blocking_violations) == 0
            _history_set(target_workspace, {
                "passed": passed,
                "attempts": current_attempt
            })
            return AgentQualityGuardResponse(
                passed=passed,
                status="APPROVED" if passed else "REJECTED",
                attempt_number=current_attempt,
                pytest_success=True,
                pytest_chunks=pytest_chunks_typed,
                blocking_ast_violations=blocking_violations,
                structural_ast_warnings=structural_warnings,
                confirmed_defects=[],
                dismissed_warnings=[],
                total_files_scanned=max(
                    ast_scan.get("total_files", 0),
                    layer1["total_files"],
                    layer2["total_files"],
                ),
                total_lines_scanned=0,
                pedido_audit=pedido or None,
                resultado_audit=resultado or None,
                layer1_toolchain_diagnostics=layer1_diags,
                toolchain_status=toolchain_status,
                layer2_meta_rule_violations=layer2_viols,
                message=(
                    f"{'✅' if passed else '🚨'} [{stack}] {test_res.get('summary', '')} | "
                    f"Camada 1: {len(blocking_violations)} bloqueio(s); "
                    f"Camada 2: {len(structural_warnings)} alerta(s)."
                )
            ).model_dump()

    # ── Etapa 0: Execução Sequencial de Testes Reais em Chunks (Pytest 1º Lugar) ──
    if ctx:
        try:
            await ctx.report_progress(1, 4)
        except Exception as ctx_err:
            logger.debug(f"Progress report etapa 0 falhou: {ctx_err}")

    try:
        import importlib
        import src.orchestrator.pytest_chunk_runner as pcr_mod
        pcr_mod = importlib.reload(pcr_mod)
        runner_cls = getattr(pcr_mod, "PytestChunkRunner", PytestChunkRunner)
    except Exception as reload_err:
        logger.debug(f"Recorrendo ao PytestChunkRunner em memória: {reload_err}")
        runner_cls = PytestChunkRunner

    pytest_out = runner_cls.run_all_chunks(
        workspace_path=target_workspace,
        audit_mode=audit_mode,
        timeout_per_chunk=60,
        max_total_timeout=120,
    )
    pytest_success = pytest_out.get("pytest_success", False)

    pytest_chunks_typed = []
    for c in pytest_out.get("chunks", []):
        pytest_chunks_typed.append(PytestChunkResult(
            chunk_id=c["chunk_id"],
            passed=c["passed"],
            total_passed=c["total_passed"],
            total_failed=c["total_failed"],
            execution_time_seconds=c["execution_time_seconds"],
            failure_details=c.get("failure_details", []),
        ))

    if not pytest_chunks_typed:
        _history_set(target_workspace, {
            "passed": False,
            "attempts": current_attempt
        })
        return AgentQualityGuardResponse(
            passed=False,
            status="REJECTED",
            attempt_number=current_attempt,
            pytest_success=False,
            pytest_chunks=[],
            blocking_ast_violations=[],
            structural_ast_warnings=[],
            confirmed_defects=[],
            dismissed_warnings=[],
            total_files_scanned=0,
            total_lines_scanned=0,
            pedido_audit=pedido or None,
            resultado_audit=resultado or None,
            message=(
                "🚨 [Pytest Fail-Fast: TESTES OBRIGATÓRIOS NÃO ENCONTRADOS]\n"
                f"Nenhum teste coletado em tests/ — workspace '{target_workspace}' sem "
                "suíte real não pode ser homologado (aprovação sem executar teste é proibida).\n"
                "👉 AÇÃO OBRIGATÓRIA DO AGENTE: Crie imediatamente o arquivo de teste 'tests/test_<funcionalidade>.py' "
                "com testes automatizados reais (sem mocks em src/) cobrindo os caminhos críticos e submeta novamente."
            )
        ).model_dump()

    if not pytest_success:
        _history_set(target_workspace, {
            "passed": False,
            "attempts": current_attempt
        })
        return AgentQualityGuardResponse(
            passed=False,
            status="REJECTED",
            attempt_number=current_attempt,
            pytest_success=False,
            pytest_chunks=pytest_chunks_typed,
            blocking_ast_violations=[],
            structural_ast_warnings=[],
            confirmed_defects=[],
            dismissed_warnings=[],
            total_files_scanned=0,
            total_lines_scanned=0,
            pedido_audit=pedido or None,
            resultado_audit=resultado or None,
            message="🚨 [Pytest Fail-Fast] Suíte de testes reais falhou em um ou mais chunks. Resolva as falhas de teste antes de prosseguir."
        ).model_dump()

    # ── Etapa 1: NeedleASTGuard (Camada 1: Bloqueios de Segurança/Integridade) ──
    if ctx:
        try:
            await ctx.report_progress(2, 4)
        except Exception as ctx_err:
            logger.debug(f"Progress report etapa 1 falhou: {ctx_err}")

    ast_violations = NeedleASTGuard.scan_workspace(target_workspace, audit_mode=audit_mode)
    blocking_violations = []
    for v in ast_violations:
        blocking_violations.append(ASTViolation(
            rule_id=v.rule_id,
            category=v.category,
            file_path=v.file_path,
            line_number=v.line_number,
            code_snippet=v.snippet,
            message=v.message,
            suggestion=v.suggestion,
        ))

    if blocking_violations:
        _history_set(target_workspace, {
            "passed": False,
            "attempts": current_attempt
        })
        return AgentQualityGuardResponse(
            passed=False,
            status="REJECTED",
            attempt_number=current_attempt,
            pytest_success=True,
            pytest_chunks=pytest_chunks_typed,
            blocking_ast_violations=blocking_violations,
            structural_ast_warnings=[],
            confirmed_defects=[],
            dismissed_warnings=[],
            total_files_scanned=len(ast_violations),
            total_lines_scanned=0,
            pedido_audit=pedido or None,
            resultado_audit=resultado or None,
            message=f"🚨 [Needle 3 AST Guardrail] {len(blocking_violations)} violação(ões) de segurança/integridade bloqueante(s) detectada(s)."
        ).model_dump()

    # ── Etapa 2: NeedleASTScreener (Camada 2: Alertas de Qualidade Estrutural) ──
    if ctx:
        try:
            await ctx.report_progress(3, 4)
        except Exception as ctx_err:
            logger.debug(f"Progress report etapa 2 falhou: {ctx_err}")

    symbols = extract_workspace_ast_symbols(target_workspace)
    suspicions_map = screen_workspace_symbols(symbols, workspace_root=target_workspace)
    structural_warnings = []

    suspicions_pairs = [
        (sym_key, s)
        for sym_key, suspicions_list in suspicions_map.items()
        for s in suspicions_list
    ]
    for sym_key, s in suspicions_pairs:
        file_path = sym_key.split("::")[0] if "::" in sym_key else target_workspace
        warning = ASTViolation(
            rule_id=f"screener_{s.category}",
            category=s.category,
            file_path=s.file_path or file_path,
            line_number=s.line_number,
            code_snippet=s.snippet or s.trigger_evidence or "",
            message=s.trigger_evidence or f"Alerta de qualidade estrutural: {s.category}",
            suggestion=f"Refatorar símbolo em {sym_key} para resolver alerta de {s.category}."
        )
        structural_warnings.append(warning)

    # ── Etapa 3: Camada 1 — bridge nativa de compiladores/linters (determinística) ──
    layer1 = _run_layer1_native_bridge(target_path, stack, audit_mode)
    layer1_blocking = _coerce_violations(layer1["blocking"], LinterDiagnostic)
    layer1_warnings = _coerce_violations(layer1["warnings"], LinterDiagnostic)
    toolchain_status = _coerce_violations(layer1["tools"], ToolchainToolStatus)

    # ── Etapa 4: Camada 2 — meta-regras universais de AST via Tree-Sitter (determinística) ──
    layer2 = _run_layer2_meta_rules(target_path, audit_mode)
    layer2_blocking = _coerce_violations(layer2["blocking"], MetaRuleViolation)
    layer2_warnings = _coerce_violations(layer2["warnings"], MetaRuleViolation)

    # Telemetria estrutural: Screener legado + alertas das Camadas 1/2 (não bloqueiam).
    structural_warnings = structural_warnings + layer1_warnings + layer2_warnings
    layer1_toolchain_diagnostics = layer1_blocking + layer1_warnings
    layer2_meta_rule_violations = layer2_blocking + layer2_warnings

    # Bloqueio determinístico combinado (NeedleASTGuard já barrou acima).
    combined_blocking = layer1_blocking + layer2_blocking
    if combined_blocking:
        _history_set(target_workspace, {
            "passed": False,
            "attempts": current_attempt
        })
        return AgentQualityGuardResponse(
            passed=False,
            status="REJECTED",
            attempt_number=current_attempt,
            pytest_success=True,
            pytest_chunks=pytest_chunks_typed,
            blocking_ast_violations=combined_blocking,
            structural_ast_warnings=structural_warnings,
            confirmed_defects=[],
            dismissed_warnings=[],
            total_files_scanned=max(len(symbols), layer1["total_files"], layer2["total_files"]),
            total_lines_scanned=sum(sym.end_line - sym.start_line + 1 for sym in symbols),
            pedido_audit=pedido or None,
            resultado_audit=resultado or None,
            layer1_toolchain_diagnostics=layer1_toolchain_diagnostics,
            toolchain_status=toolchain_status,
            layer2_meta_rule_violations=layer2_meta_rule_violations,
            message=(
                f"🚨 [Quality Guard Determinístico] {len(layer1_blocking)} erro(s) de toolchain "
                f"(Camada 1) e {len(layer2_blocking)} violação(ões) de meta-regra bloqueante(s) "
                f"(Camada 2). Alertas estruturais registrados: {len(structural_warnings)}."
            )
        ).model_dump()

    # ── Aprovação Determinística (sem arbitragem ML/Laya) ──
    if ctx:
        try:
            await ctx.report_progress(4, 4)
        except Exception as ctx_err:
            logger.debug(f"Progress report final falhou: {ctx_err}")

    _history_set(target_workspace, {
        "passed": True,
        "attempts": current_attempt
    })

    approval_msg = (
        "Entrega 100% aprovada deterministicamente por testes reais (pytest), Needle 3 AST Guard (Camada 1) "
        "e meta-regras universais de AST (Camada 2). "
        f"Toolchain nativa sem bloqueios; {len(structural_warnings)} alerta(s) estrutural(is) como telemetria."
    )

    return AgentQualityGuardResponse(
        passed=True,
        status="APPROVED",
        attempt_number=current_attempt,
        pytest_success=True,
        pytest_chunks=pytest_chunks_typed,
        blocking_ast_violations=[],
        structural_ast_warnings=structural_warnings,
        confirmed_defects=[],
        dismissed_warnings=[],
        total_files_scanned=max(len(symbols), layer1["total_files"], layer2["total_files"]),
        total_lines_scanned=sum(sym.end_line - sym.start_line + 1 for sym in symbols),
        pedido_audit=pedido or None,
        resultado_audit=resultado or None,
        layer1_toolchain_diagnostics=layer1_toolchain_diagnostics,
        toolchain_status=toolchain_status,
        layer2_meta_rule_violations=layer2_meta_rule_violations,
        message=approval_msg,
    ).model_dump()


# ──────────────────────────────────────────────────────────────────────────────
# Registro das tools: nome canônico `agent_quality_guard` + alias legado
# `needle_verify_delivery` (MESMA implementação, retrocompatibilidade total).
# ──────────────────────────────────────────────────────────────────────────────
agent_quality_guard = app.tool(
    name="agent_quality_guard", description=_QUALITY_GUARD_DESCRIPTION
)(_quality_guard_core)
needle_verify_delivery = app.tool(
    name="needle_verify_delivery", description=_LEGACY_ALIAS_DESCRIPTION
)(_quality_guard_core)


if __name__ == "__main__":
    import ctypes
    import os

    # ── Proteção contra processos órfãos (Linux PR_SET_PDEATHSIG) ───────────
    # Garante que o Kernel envie SIGTERM no microssegundo em que a IDE / Language Server pai morrer
    try:
        PR_SET_PDEATHSIG = 1
        libc = ctypes.CDLL("libc.so.6")
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGTERM)
    except Exception as e:
        logger.debug("prctl(PR_SET_PDEATHSIG) não suportado ou indisponível: %s", e)

    def _graceful_exit(signum, frame) -> None:
        logger.info("Sinal %s recebido no servidor MCP, encerrando de forma limpa...", signum)
        if os.path.exists("/tmp/openjev_ipc.sock"):
            try:
                os.unlink("/tmp/openjev_ipc.sock")
            except OSError as unlink_err:
                logger.debug("Não foi possível desvincular socket IPC no encerramento: %s", unlink_err)
        os._exit(0)

    try:
        signal.signal(signal.SIGTERM, _graceful_exit)
        signal.signal(signal.SIGINT, _graceful_exit)
        if hasattr(signal, "SIGHUP"):
            signal.signal(signal.SIGHUP, _graceful_exit)
    except Exception as e:
        logger.debug("Sinais de terminação não puderam ser registrados: %s", e)

    def _preload_engine() -> None:
        try:
            from src.decision.hardware import try_acquire_gpu_lock
            if not try_acquire_gpu_lock():
                logger.info(
                    "Instância secundária sem lock de GPU detectada. "
                    "Pré-carregamento em CPU suprimido para economizar ~2.3 GB de RAM física."
                )
                return
            get_engine()
            logger.info("Motor JEV pré-carregado no startup do MCP com GPU alocada.")
        except Exception as e:
            logger.warning("Pré-load do motor JEV falhou (retry no 1º uso): %s", e)

    # Pré-load em background: a 1ª tool call não paga o load de pesos na GPU/CPU.
    # O get_engine é thread-safe; se o preload ainda estiver rodando, o await
    # de to_thread espera o mesmo singleton sob o lock.
    threading.Thread(target=_preload_engine, daemon=True, name="jev-engine-preload").start()
    app.run(transport="stdio")
