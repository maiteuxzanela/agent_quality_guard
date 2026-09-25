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
import threading
import time
import warnings
from pathlib import Path
from typing import Any, Dict, List, Optional

# Silencia avisos
os.environ["PYTHONWARNINGS"] = "ignore"
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
    ScoreDiagnoseResponse,
    SpawnSubagentRequest,
    SpawnSubagentResponse,
)
from src.core.diff_inspector import extract_workspace_stages, format_stage_premise
from src.memory.ast_diff_extractor import extract_workspace_ast_symbols, AstSymbolSlice
from src.memory.needle_defect_screener import screen_workspace_symbols, DefectSuspicion, DEFECT_NOUL_HYPOTHESES
from src.decision.engine import OpenJevEngine
from src.decision.laya_adapter import evaluate_borderline_mass_gate
from src.memory.local_needle import LocalNeedleEngine
from src.memory.needle_ast_guard import NeedleASTGuard
from src.orchestrator.subagent_runner import SubagentRunner

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
    name="needle3_search",
    description="Busca cirurgica relacional local no codigo usando AST e grafo de dependencias (Needle 3). Economiza ate 90% de tokens.",
)
async def needle3_search(
    query: str,
    workspace: str = "",
    scope: str = "",
    max_tokens: int = 1000,
    depth: int = 2,
) -> dict:
    """Executa busca relacional com Cactus Needle 3."""
    target_workspace = workspace.strip() if workspace and workspace.strip() else os.getcwd()
    needle = LocalNeedleEngine(workspace_root=target_workspace)
    scope_list = [s.strip() for s in scope.split(",") if s.strip()] if scope else None

    q = Needle3Query(
        query=query,
        scope=scope_list,
        max_tokens=max_tokens,
        relational_depth=depth,
        preserve_relational_integrity=True,
    )

    t0 = time.perf_counter()
    resp = await needle.search(q)
    lat_ms = (time.perf_counter() - t0) * 1000.0

    snippets_data = []
    for s in resp.snippets:
        snippets_data.append({
            "file": s.file_path,
            "lines": f"{s.start_line}-{s.end_line}",
            "relevance": round(s.relevance_score, 3),
            "relations": s.relations,
            "content": s.content,
        })

    result = {
        "status": "success",
        "latency_ms": round(lat_ms, 2),
        "total_tokens": resp.total_tokens,
        "token_savings_pct": resp.token_savings_pct,
        "snippets_count": len(resp.snippets),
        "snippets": snippets_data,
    }
    if needle.candidates_truncated:
        result["candidates_truncated"] = True
        result["candidates_found"] = needle.candidates_found
        result["warning"] = (
            f"Teto de candidatos atingido ({needle.candidates_found}). "
            "Restringa `scope` para busca cirúrgica neste workspace."
        )
    return result


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


@app.tool(
    name="openjev_spawn_subagent",
    description=(
        "Instancia e executa um subagente especialista em janela de contexto isolada "
        "(zero historico do orquestrador), utilizando o system prompt da persona "
        "e validando deterministicamente a entrega via Needle 3 AST Guardrail e Open Jev Score."
    ),
)
async def openjev_spawn_subagent(
    persona: str,
    task: str,
    workspace: str = "",
    auto_validate: bool = True,
    timeout: Optional[float] = None,
) -> dict:
    """Executa um subagente especialista em contexto isolado com auto-validacao AST + Score.

    Args:
        persona: Identificador da persona especialista (ex: 'lumi', 'kael', 'juliana').
        task: Descricao cirurgica e detalhada da tarefa tecnica a ser executada.
        workspace: Diretorio absoluto do repositorio alvo (vazio assume o workspace corrente).
        auto_validate: Se True, executa auditoria fail-fast do Needle AST e do Laya Score.
        timeout: Limite de tempo em segundos. Se None ou <= 0, o timeout fica completamente desativado.
    """
    if not persona.strip():
        raise ValueError("openjev_spawn_subagent exige o parametro 'persona' preenchido.")
    if not task.strip():
        raise ValueError("openjev_spawn_subagent exige o parametro 'task' com a descricao da tarefa.")

    engine = await _get_engine_async() if auto_validate else None
    runner = SubagentRunner(engine=engine)
    req = SpawnSubagentRequest(
        persona=persona,
        task=task,
        workspace=workspace,
        auto_validate=auto_validate,
        timeout=timeout,
    )
    resp = await runner.spawn(req)
    return resp.model_dump()



DEFAULT_SCORE_RUBRIC = RUBRIC_CATALOG["codigo_producao"]

# Teto rígido da premissa de score (anti-ruído: prosa longa distorce a nota).
from src.core.score_gate import (  # noqa: E402
    MAX_ATTEMPTS as _SCORE_GATE_MAX_ATTEMPTS,
    SCORE_PREMISE_MAX_LEN,
    get_workspace_git_signature as _score_gate_git_signature,
    history_clear as _history_clear,
    history_get as _history_get,
    history_increment_attempts as _history_increment_attempts,
    history_set as _history_set,
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


def _build_failure_diagnosis(
    confirmed_defects: List[Dict[str, Any]],
    refuted_suspicions: List[Dict[str, Any]],
    delivery_score: float,
    threshold: float,
    latency_ms: float,
) -> tuple:
    """Monta ``(diagnosis, message)`` da reprovação: defeito confirmado pelo Noul
    ou suspeita estrutural que exige investigação no disco."""
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




# NOTA: a tool `openjev_score` foi expurgada (set/2026) — todo o fluxo de
# score (validação de premissa, catálogo de rubricas, anti-premise gaming,
# circuit breaker e rubric_source) vive exclusivamente em
# `openjev_score_and_diagnose`, logo abaixo.

# Circuit breaker (MAX_ATTEMPTS) vem de score_gate — fonte única.

# NOTA (set/2026): o choice de diagnóstico flat foi removido do fluxo de score.
# O diagnóstico vem do Needle 3 Screener (8 categorias técnicas) confirmado/
# arbitrado pelo JEV Noul (P >= 0.78); as categorias nulas
# (`sem_defeito_identificavel`, `evidencia_insuficiente`) deixaram de existir
# no universo de decisão — ver DEFECT_OPTIONS em src/core/schemas.py.


@app.tool(
    name="openjev_score_and_diagnose",
    description=(
        "Avaliacao de qualidade com diagnostico estruturado de defeito, sem choice. "
        "Pipeline: Needle 3 AST Guardrail (fail-fast) -> Screener estrutural sobre "
        "8 categorias reais -> JEV Noul (P >= 0.78) em cada suspeita -> Score Laya. "
        "Se o Screener nao aponta NENHUMA suspeita: passed=true; score < threshold "
        "vira passed=true com approved_with_caveat=true (divulgar score_margin). "
        "Se o Screener aponta suspeitas: defeito confirmado pelo Noul => passed=false "
        "com a categoria, arquivo, linhas e candidate_defects; todas refutadas mas "
        "score < threshold => passed=false e diagnosis aponta a suspeita de maior "
        "probabilidade para INVESTIGACAO (mesmo com Noul negativo). "
        "Circuito breaker apos 5 tentativas."
    ),
)
async def openjev_score_and_diagnose(
    premise: str,
    workspace: str = "",
    task_type: str = "codigo_producao",
    rubric: str = "",
    min_score: float = 1.0,
    max_score: float = 5.0,
    threshold: float = 3.40,
    reset_history: bool = False,
) -> dict:
    """Score + diagnóstico via Screener Needle 3 (8 categorias) + Noul — sem choice."""
    premise_error = _validate_score_premise(premise)
    if premise_error:
        return {"error": premise_error, "premise_rejected": True, "diagnosis": None}
    resolved_rubric, rubric_error = _resolve_task_rubric(task_type)
    if rubric_error:
        return {"error": rubric_error, "task_type_rejected": True, "diagnosis": None}
    task_type_key = "codigo_producao"

    target_workspace = workspace.strip() if workspace and workspace.strip() else os.getcwd()

    if reset_history:
        _history_clear()
        logger.info("Histórico e cache de etapas limpos via flag reset_history=True.")

    current_git_state = _get_workspace_git_signature(target_workspace)

    # ── 1. CIRCUIT BREAKER UNIVERSAL + 2. ANTI-PREMISE GAMING ────────────────
    # Lógica plana extraída para _evaluation_gate (evita cadeia de if de
    # nível 3 e predicado denso dentro da tool).
    hist = _history_get(target_workspace)
    gate_response = _evaluation_gate(
        target_workspace, hist, current_git_state, threshold
    )
    if gate_response is not None:
        return gate_response

    # ── Camada 1: Needle 3 AST Guardrail (Fail-Fast Determinístico) ───────────
    ast_violations = NeedleASTGuard.scan_workspace(target_workspace)
    if ast_violations:
        # Reprovação Imediata Fail-Fast — não gasta inferência GPU/CPU
        top_violation = ast_violations[0]
        violation_details = "; ".join(
            f"[{v.rule_id}] {v.file_path}:{v.line_number} — {v.message}"
            for v in ast_violations
        )
        logger.warning(f"🚨 [Needle 3 AST Guardrail] Violação determinística detectada: {violation_details}")

        attempts = 1
        if hist and not hist.get("passed", True):
            attempts = hist.get("attempts", 1) + 1

        _history_set(target_workspace, {
            "passed": False,
            "attempts": attempts,
            "git_state": current_git_state,
            "last_score": 1.0,
        })

        diag_res = DiagnosisResult(
            selected=top_violation.category,
            confidence=1.0,
            probabilities={top_violation.category: 1.0},
            description=f"🚨 [Needle 3 AST Block]: {top_violation.message} ({top_violation.file_path}:{top_violation.line_number})",
            candidate_defects=list(DEFECT_MEMBERS.get(top_violation.category, [])),
            diagnosis_latency_ms=0.0,
        )

        return ScoreDiagnoseResponse(
            score=1.0,
            max_score=max_score,
            normalized_score=0.0,
            passed=False,
            threshold=threshold,
            breakdown={"critical flaws": 1.0, "poor quality": 0.0, "acceptable": 0.0, "good quality": 0.0, "exceptional": 0.0},
            score_latency_ms=0.0,
            score_margin=-1.0,
            borderline=False,
            task_type=task_type_key,
            rubric_source=f"catalog:{task_type_key}",
            diagnosis=diag_res,
            approved_as_borderline=False,
            stages=[],
            total_stages=0,
            passed_stages=0,
            failed_stages=len(ast_violations),
            attempts=attempts,
            max_attempts=MAX_ATTEMPTS,
            emergency_exit_unlocked=(attempts >= MAX_ATTEMPTS),
            ast_blocked=True,
            ast_violations=[v.to_dict() for v in ast_violations],
        ).model_dump()

    engine = await _get_engine_async()

    # ── Camada 2: Extração de Símbolos AST e Arbitragem Noul Universal ────────
    # NOTA: sem importlib.reload — recarregar recria as classes (ex.:
    # AstSymbolSlice) e quebra isinstance nos módulos já importados no mesmo
    # processo (poluição de identidade entre lotes de teste / hot-reload).
    import src.memory.ast_diff_extractor as _ade
    _extract_symbols = _ade.extract_workspace_ast_symbols

    import src.memory.needle_defect_screener as _nds
    _screen_symbols = _nds.screen_workspace_symbols

    ast_symbols = _extract_symbols(target_workspace)

    if ast_symbols:
        suspicions_by_symbol = _screen_symbols(ast_symbols)
        t_start_noul = time.perf_counter()
        confirmed_defects: List[Dict[str, Any]] = []
        refuted_suspicions: List[Dict[str, Any]] = []

        # Indexa os símbolos por chave para busca O(1) imediata e laço plano linear
        symbols_by_key = {f"{s.file_path}::{s.symbol_name}": s for s in ast_symbols}
        all_screened_items = [
            (symbols_by_key[k], susp)
            for k, susps in suspicions_by_symbol.items()
            if k in symbols_by_key
            for susp in susps
        ]

        # Se o Needle 3 detectou indícios estruturais, submete cada hipótese ao JEV Noul em laço plano
        for symbol, susp in all_screened_items:
            noul_premise = (
                f"Código do símbolo {symbol.file_path}::{symbol.symbol_name} "
                f"(linhas {symbol.start_line}-{symbol.end_line}):\n"
                f"{symbol.full_context[:1200]}"
            )
            noul_req = NoulRequest(
                premise=noul_premise,
                hypothesis=susp.noul_hypothesis,
                threshold=0.78,
            )
            noul_resp = await engine.evaluate_noul(noul_req)
            if noul_resp.passed and noul_resp.probability >= 0.78:
                confirmed_defects.append({
                    "category": susp.category,
                    "symbol_name": symbol.symbol_name,
                    "file_path": symbol.file_path,
                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,
                    "probability": round(noul_resp.probability, 4),
                    "evidence": susp.trigger_evidence,
                    "description": DEFECT_DESCRIPTIONS.get(susp.category, ""),
                    "candidate_defects": list(DEFECT_MEMBERS.get(susp.category, [])),
                })
            else:
                refuted_suspicions.append({
                    "category": susp.category,
                    "symbol_name": symbol.symbol_name,
                    "file_path": symbol.file_path,
                    "start_line": symbol.start_line,
                    "end_line": symbol.end_line,
                    "probability": round(noul_resp.probability, 4),
                    "evidence": susp.trigger_evidence,
                })

        # Monta a premissa factual enriquecida com o código real dos símbolos alterados
        code_snippets = []
        for s in ast_symbols[:3]:
            code_snippets.append(f"Arquivo {s.file_path} ({s.symbol_type} {s.symbol_name}):\n{s.content[:350]}")
        code_context = "\n---\n".join(code_snippets)

        combined_premise = premise
        if code_context and "\nSímbolos alterados:\n" not in premise:
            combined_premise = f"{premise}\n\nSímbolos alterados:\n{code_context}"
        if len(combined_premise) > 1480:
            combined_premise = combined_premise[:1480]

        # Avaliação de Score base para a premissa de entrega
        score_req = ScoreRequest(
            premise=combined_premise,
            rubric=resolved_rubric,
            min_score=min_score,
            max_score=max_score,
            threshold=threshold,
        )
        score_resp = await engine.evaluate_score(score_req)
        delivery_score = round(score_resp.score, 2)
        score_borderline = bool(score_resp.borderline)

        mass_ok, p_healthy, p_flawed = evaluate_borderline_mass_gate(
            score_resp.breakdown,
            healthy_min=BORDERLINE_HEALTHY_MIN_MASS,
            flawed_max=BORDERLINE_FLAWED_MAX_MASS,
        )

        # Monta stages sintéticos por símbolo para auditoria com suporte a cache
        symbols_map = {f"{s.file_path}::{s.symbol_name}": s for s in ast_symbols}
        evaluated_stages = []
        for s in ast_symbols[:20]:  # Limite de telemetria
            st_id = f"{s.file_path}::{s.symbol_name}"
            s_hash = hashlib.sha256(s.content.encode("utf-8")).hexdigest()
            cached_entry = _stages_cache_get(target_workspace, st_id)
            is_cached = False
            if cached_entry and cached_entry.get("hash") == s_hash and cached_entry.get("passed", False):
                is_cached = True

            st_passed = is_cached or (not any(d["file_path"] == s.file_path and d["symbol_name"] == s.symbol_name for d in confirmed_defects))
            evaluated_stages.append(
                DiffStageResult(
                    stage_id=st_id,
                    file_path=s.file_path,
                    part_index=1,
                    total_parts=1,
                    score=cached_entry.get("score", delivery_score) if is_cached else delivery_score,
                    passed=st_passed,
                    borderline=score_borderline,
                    cached=is_cached,
                    diagnosis=None,
                )
            )

        # Se todas as etapas já foram aprovadas anteriormente e o workspace permanece idêntico
        all_stages_cached = bool(evaluated_stages and all(st.cached for st in evaluated_stages))
        if all_stages_cached and hist and hist.get("passed", False):
            delivery_score = max(delivery_score, float(hist.get("last_score", threshold)))

        # ── Decisão final (sem choice) ───────────────────────────────────────
        # 1. Defeito confirmado pelo Noul (P >= 0.78) => reprova.
        # 2. Needle 3 sem NENHUMA suspeita estrutural => aprova; nota < corte
        #    vira aprovação com ressalva (approved_with_caveat).
        # 3. Needle 3 apontou qualquer uma das 8 categorias e a nota ficou
        #    abaixo do corte => REPROVA e manda investigar — mesmo que o Noul
        #    tenha refutado a hipótese (P < 0.78): suspeita estrutural +
        #    nota baixa exige apuração humana no disco.
        zero_suspicions = not all_screened_items
        score_ok = delivery_score >= threshold

        if confirmed_defects:
            passed_delivery = False
        elif zero_suspicions or score_ok or all_stages_cached:
            passed_delivery = True
        else:
            passed_delivery = False

        is_caveat = passed_delivery and (not score_ok) and zero_suspicions

        # Na reprovação por suspeita não-resolvida, marca as etapas afetadas
        if not passed_delivery and not confirmed_defects:
            _flag_suspect_failed_stages(evaluated_stages, refuted_suspicions)

        failed_keys = {st.stage_id for st in evaluated_stages if not st.passed}
        failed_keys |= {f"{d['file_path']}::{d['symbol_name']}" for d in confirmed_defects}
        total_syms = len(ast_symbols)
        failed_syms = len(failed_keys)
        passed_syms = total_syms - failed_syms

        if passed_delivery:
            caveat_msg = (
                f"Entrega aprovada com ressalva: o Needle 3 não apontou nenhuma das 8 categorias de "
                f"defeito no diff; score contínuo ({delivery_score:.2f}) abaixo do corte de {threshold:.2f} "
                "aceito por não haver suspeita estrutural a investigar."
            ) if is_caveat else None

            # Persiste estágios aprovados no cache do workspace
            _cache_passed_stages(evaluated_stages, symbols_map, target_workspace)

            _history_set(target_workspace, {
                "passed": True,
                "attempts": 0,
                "git_state": current_git_state,
                "last_score": delivery_score,
            })
            return ScoreDiagnoseResponse(
                score=delivery_score,
                max_score=max_score,
                normalized_score=round((delivery_score - min_score) / (max_score - min_score), 4),
                passed=True,
                threshold=threshold,
                breakdown=score_resp.breakdown,
                score_latency_ms=round((time.perf_counter() - t_start_noul) * 1000.0, 2),
                score_margin=round(delivery_score - threshold, 4),
                borderline=score_borderline,
                task_type=task_type_key,
                rubric_source=f"catalog:{task_type_key}",
                diagnosis=None,
                approved_as_borderline=is_caveat and bool(mass_ok),
                approved_with_caveat=is_caveat,
                stages=evaluated_stages,
                total_stages=total_syms,
                passed_stages=passed_syms,
                failed_stages=failed_syms,
                attempts=0,
                max_attempts=MAX_ATTEMPTS,
                emergency_exit_unlocked=False,
                message=caveat_msg,
            ).model_dump()
        else:
            # Reprovado: defeito confirmado pelo Noul OU suspeita estrutural
            # não-resolvida com score abaixo do corte. Montagem extraída para
            # _build_failure_diagnosis (profundidade de condicional <= 2).
            global_diag, diag_msg = _build_failure_diagnosis(
                confirmed_defects,
                refuted_suspicions,
                delivery_score,
                threshold,
                round((time.perf_counter() - t_start_noul) * 1000.0, 2),
            )

            prev_attempts = hist.get("attempts", 0) if hist else 0
            new_attempts = prev_attempts + 1
            _history_set(target_workspace, {
                "passed": False,
                "attempts": new_attempts,
                "git_state": current_git_state,
                "last_score": delivery_score,
            })
            return ScoreDiagnoseResponse(
                score=delivery_score,
                max_score=max_score,
                normalized_score=round((delivery_score - min_score) / (max_score - min_score), 4),
                passed=False,
                threshold=threshold,
                breakdown=score_resp.breakdown,
                score_latency_ms=round((time.perf_counter() - t_start_noul) * 1000.0, 2),
                score_margin=round(delivery_score - threshold, 4),
                borderline=score_borderline,
                task_type=task_type_key,
                rubric_source=f"catalog:{task_type_key}",
                diagnosis=global_diag,
                approved_as_borderline=False,
                approved_with_caveat=False,
                stages=evaluated_stages,
                total_stages=total_syms,
                passed_stages=passed_syms,
                failed_stages=failed_syms,
                attempts=new_attempts,
                max_attempts=MAX_ATTEMPTS,
                emergency_exit_unlocked=new_attempts >= MAX_ATTEMPTS,
                message=(
                    f"🚨 [SAÍDA DE EMERGÊNCIA DESBLOQUEADA] (Tentativa {new_attempts}/{MAX_ATTEMPTS}): "
                    f"Limite de {MAX_ATTEMPTS} tentativas consecutivas atingido. "
                    "O agente está autorizado a emitir Laudo de Inviabilidade Técnica."
                ) if new_attempts >= MAX_ATTEMPTS else diag_msg,
            ).model_dump()

    # ── CAMINHO FALLBACK MONO-ETAPA (quando repositório limpo ou sem diffs de código) ─
    score_req = ScoreRequest(
        premise=premise,
        rubric=resolved_rubric,
        min_score=min_score,
        max_score=max_score,
        threshold=threshold,
    )
    score_resp = await engine.evaluate_score(score_req)

    # Etapa sintética para preenchimento de contrato
    mono_stage = DiffStageResult(
        stage_id="mono_stage_premise",
        file_path="workspace",
        part_index=1,
        total_parts=1,
        score=round(score_resp.score, 2),
        passed=score_resp.passed,
        borderline=bool(score_resp.borderline),
        cached=False,
        diagnosis=None,
    )

    if score_resp.passed:
        _history_set(target_workspace, {
            "passed": True,
            "attempts": 0,
            "git_state": current_git_state,
            "last_score": round(score_resp.score, 2),
        })
        return ScoreDiagnoseResponse(
            score=round(score_resp.score, 2),
            max_score=max_score,
            normalized_score=round(score_resp.normalized_score, 4),
            passed=True,
            threshold=threshold,
            breakdown=score_resp.breakdown,
            score_latency_ms=round(score_resp.latency_ms, 2),
            score_margin=score_resp.score_margin,
            borderline=score_resp.borderline,
            task_type=task_type_key,
            rubric_source=f"catalog:{task_type_key}",
            diagnosis=None,
            approved_as_borderline=False,
            stages=[mono_stage],
            total_stages=1,
            passed_stages=1,
            failed_stages=0,
            attempts=0,
            max_attempts=MAX_ATTEMPTS,
            emergency_exit_unlocked=False,
        ).model_dump()

    # Borderline Mass Gate
    if score_resp.borderline:
        mass_approved, p_healthy, p_flawed = evaluate_borderline_mass_gate(
            score_resp.breakdown,
            healthy_min=BORDERLINE_HEALTHY_MIN_MASS,
            flawed_max=BORDERLINE_FLAWED_MAX_MASS,
        )
        if mass_approved:
            _history_set(target_workspace, {
                "passed": True,
                "attempts": 0,
                "git_state": current_git_state,
                "last_score": round(score_resp.score, 2),
            })
            mono_stage.passed = True
            return ScoreDiagnoseResponse(
                score=round(score_resp.score, 2),
                max_score=max_score,
                normalized_score=round(score_resp.normalized_score, 4),
                passed=True,
                threshold=threshold,
                breakdown=score_resp.breakdown,
                score_latency_ms=round(score_resp.latency_ms, 2),
                score_margin=score_resp.score_margin,
                borderline=score_resp.borderline,
                task_type=task_type_key,
                rubric_source=f"catalog:{task_type_key}",
                diagnosis=None,
                approved_as_borderline=True,
                stages=[mono_stage],
                total_stages=1,
                passed_stages=1,
                failed_stages=0,
                attempts=0,
                max_attempts=MAX_ATTEMPTS,
                emergency_exit_unlocked=False,
            ).model_dump()

    prev_attempts = hist.get("attempts", 0) if hist else 0
    new_attempts = prev_attempts + 1
    _history_set(target_workspace, {
        "passed": False,
        "attempts": new_attempts,
        "git_state": current_git_state,
        "last_score": round(score_resp.score, 2),
    })

    return ScoreDiagnoseResponse(
        score=round(score_resp.score, 2),
        max_score=max_score,
        normalized_score=round(score_resp.normalized_score, 4),
        passed=False,
        threshold=threshold,
        breakdown=score_resp.breakdown,
        score_latency_ms=round(score_resp.latency_ms, 2),
        score_margin=score_resp.score_margin,
        borderline=score_resp.borderline,
        task_type=task_type_key,
        rubric_source=f"catalog:{task_type_key}",
        diagnosis=None,
        approved_as_borderline=False,
        stages=[mono_stage],
        total_stages=1,
        passed_stages=0,
        failed_stages=1,
        attempts=new_attempts,
        max_attempts=MAX_ATTEMPTS,
        emergency_exit_unlocked=new_attempts >= MAX_ATTEMPTS,
        message=(
            f"🚨 [SAÍDA DE EMERGÊNCIA DESBLOQUEADA] (Tentativa {new_attempts}/{MAX_ATTEMPTS}): "
            f"Limite de {MAX_ATTEMPTS} tentativas consecutivas atingido. "
            "O agente está autorizado a emitir Laudo de Inviabilidade Técnica."
        ) if new_attempts >= MAX_ATTEMPTS else (
            f"Score insuficiente ({round(score_resp.score, 2)}/{threshold:.2f}) no caminho mono "
            "(workspace sem símbolos AST no diff para triagem estrutural): não há suspeita do "
            "Needle 3 para diagnosticar — refatore o artefato no disco e reavalie."
        ),
    ).model_dump()


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
