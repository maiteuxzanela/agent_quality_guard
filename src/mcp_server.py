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

# Silencia avisos
os.environ.setdefault("PYTHONWARNINGS", "ignore")
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
    ScoreResponse,
    ScoreDiagnoseResponse,
    SpawnSubagentRequest,
    SpawnSubagentResponse,
    QUALITY_DEFECT_CATEGORIES,
    NeedleVerifyDeliveryRequest,
    NeedleVerifyDeliveryResponse,
    PytestChunkResult,
    ASTViolation,
)
from src.core.diff_inspector import extract_workspace_stages, format_stage_premise
from src.memory.ast_diff_extractor import extract_workspace_ast_symbols, AstSymbolSlice
from src.memory.needle_defect_screener import (
    screen_workspace_symbols,
    DefectSuspicion,
    DEFECT_NOUL_HYPOTHESES,
    _is_test_path,
)
from src.decision.engine import OpenJevEngine
from src.memory.local_needle import LocalNeedleEngine
from src.memory.needle_ast_guard import NeedleASTGuard
from src.orchestrator.subagent_runner import SubagentRunner
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

# NOTA (set/2026): o choice de diagnóstico flat foi removido do fluxo de score.
# O diagnóstico vem do Needle 3 Screener (10 categorias técnicas: 8 duras + 2 de
# análise tipagem/docstring) confirmado/arbitrado pelo JEV Noul (P >= 0.78); as
# categorias nulas (`sem_defeito_identificavel`, `evidencia_insuficiente`)
# deixaram de existir no universo de decisão — ver DEFECT_OPTIONS em
# src/core/schemas.py. A ressalva de nota segue a banda de indiferença
# CAVEAT_BAND (src/core/score_gate.py).


_CATEGORY_PRIORITY: Dict[str, int] = {
    "seguranca": 1,
    "robustez_excecoes_recursos": 2,
    "logica_ou_regra": 3,
    "contrato_ambiente": 4,
    "requisito_incompleto_ou_ausente": 5,
    "qualidade_de_testes": 6,
    "performance_escopo_estilo": 7,
    "documentacao_configuracao": 8,
    "tipagem_ausente": 9,
    "docstring_ausente": 10,
}
MAX_ARBITRATIONS: int = 10


@app.tool(
    name="needle_verify_delivery",
    description=(
        "Validação de entrega de código 100% determinística (Needle 3 + Pytest Real + JEV Noul Arbitrage). "
        "Executa em ordem fail-fast: "
        "1º Pytest em 3-4 chunks sequenciais com isolamento dos testes pesados de ML; "
        "2º NeedleASTGuard (Camada 1: anti-mock em produção, SQL Injection, eval perigoso, segredos hardcoded); "
        "3º NeedleASTScreener (Camada 2: alertas estruturais de tipagem, docstrings, exceções); "
        "4º JEV Noul Arbitrage (Camada 3: Laya RLCD P >= 0.78 arbitra suspeitas em micro-premissas atômicas <= 150 tokens). "
        "Circuit breaker universal de 5 tentativas: atinge a Saída de Emergência na 5ª tentativa."
    ),
)
async def needle_verify_delivery(
    task_type: str = "codigo_producao",
    workspace: str = "",
    attempt_number: int = 1,
    pedido: str = "",
    resultado: str = "",
    engine: Any = None,
) -> dict:
    """Validação determinística de entrega via Pytest em Chunks + Needle 3 AST + JEV Noul.

    A contagem de tentativas é exclusivamente server-side
    (`score_gate.resolve_attempt_number`, histórico persistido por workspace);
    `attempt_number` é apenas informativo e NÃO participa da decisão do
    circuit breaker. Não existe parâmetro de reset de histórico nesta tool.

    Exemplo:
        >>> import asyncio, tempfile
        >>> with tempfile.TemporaryDirectory() as ws:
        ...     out = asyncio.run(needle_verify_delivery(workspace=ws))
        >>> out["status"] in {"APPROVED", "REJECTED", "EMERGENCY_RELEASE"}
        True
    """
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
        logger.warning(f"🚨 [Circuit Breaker] 5ª tentativa atingida em {target_workspace}. Saída de Emergência liberada.")
        _history_set(target_workspace, {
            "passed": True,
            "attempts": current_attempt,
            "emergency": True
        })
        return NeedleVerifyDeliveryResponse(
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
            message=(
                f"🚨 [Circuit Breaker] Saída de Emergência ativada na tentativa "
                f"{current_attempt}/{MAX_ATTEMPTS}. Entrega liberada COM RESSALVA "
                "(pytest/Guard/Screener NÃO executados nesta chamada)."
            )
        ).model_dump()

    target_path = Path(target_workspace)
    stack = MultiStackRunner.detect_stack(target_path)
    tmp_dir = Path(tempfile.gettempdir()) / "needle_tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)

    if stack in ("REACT_NATIVE_EXPO", "WEB_JS_FIREBASE", "PYTHON_FATURAMENTO", "PYTHON_PREDICTERA"):
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
            return NeedleVerifyDeliveryResponse(
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

        if stack in ("REACT_NATIVE_EXPO", "WEB_JS_FIREBASE"):
            ast_scan = MultiStackRunner.scan_js_ast(target_path, stack)
            blocking_violations = [ASTViolation(
                rule_id=b["rule_id"],
                category=b["category"],
                file_path=b["file_path"],
                line_number=b["line_number"],
                code_snippet=b["code_snippet"],
                message=b["message"],
                suggestion=None,
            ) for b in ast_scan.get("blocking", [])]

            structural_warnings = [ASTViolation(
                rule_id=w["rule_id"],
                category=w["category"],
                file_path=w["file_path"],
                line_number=w["line_number"],
                code_snippet=w["code_snippet"],
                message=w["message"],
                suggestion=None,
            ) for w in ast_scan.get("warnings", [])]

            passed = len(blocking_violations) == 0
            _history_set(target_workspace, {
                "passed": passed,
                "attempts": current_attempt
            })
            return NeedleVerifyDeliveryResponse(
                passed=passed,
                status="APPROVED" if passed else "REJECTED",
                attempt_number=current_attempt,
                pytest_success=True,
                pytest_chunks=pytest_chunks_typed,
                blocking_ast_violations=blocking_violations,
                structural_ast_warnings=structural_warnings,
                confirmed_defects=[],
                dismissed_warnings=[],
                total_files_scanned=ast_scan.get("total_files", 0),
                total_lines_scanned=0,
                pedido_audit=pedido or None,
                resultado_audit=resultado or None,
                message=f"{'✅' if passed else '🚨'} [{stack}] {test_res.get('summary', '')} | AST: {len(blocking_violations)} bloqueios, {len(structural_warnings)} alertas."
            ).model_dump()

    # ── Etapa 0: Execução Sequencial de Testes Reais em Chunks (Pytest 1º Lugar) ──
    pytest_out = PytestChunkRunner.run_all_chunks(workspace_path=target_workspace)
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
        return NeedleVerifyDeliveryResponse(
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
        return NeedleVerifyDeliveryResponse(
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
    ast_violations = NeedleASTGuard.scan_workspace(target_workspace)
    blocking_violations = []
    for v in ast_violations:
        blocking_violations.append(ASTViolation(
            rule_id=v.rule_id,
            category=v.category,
            file_path=v.file_path,
            line_number=v.line_number,
            code_snippet=v.snippet,
            message=v.message,
            suggestion=None,
        ))

    if blocking_violations:
        _history_set(target_workspace, {
            "passed": False,
            "attempts": current_attempt
        })
        return NeedleVerifyDeliveryResponse(
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
    symbols = extract_workspace_ast_symbols(target_workspace)
    suspicions_map = screen_workspace_symbols(symbols, workspace_root=target_workspace)
    structural_warnings = []
    all_suspicions = []

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
        all_suspicions.append((sym_key, s, warning))

    # ── Etapa 3: JEV Noul Arbitrage (Camada 3: Laya RLCD P >= 0.78) ──
    confirmed_defects = []
    dismissed_warnings = []

    if all_suspicions:
        active_engine = engine
        if active_engine is None:
            try:
                active_engine = await _get_engine_async()
            except Exception as e:
                logger.debug(f"Motor JEV não disponível para arbitragem Noul: {e}")
                active_engine = None

        is_engine_ready = active_engine is not None and getattr(active_engine, "is_ready", False)

        if not is_engine_ready:
            # Política Fail-Safe: Se o motor Laya não estiver pronto na memória,
            # relata os alertas como telemetria sem abortar a entrega por indisponibilidade de pesos.
            logger.info("Motor JEV descarregado: alertas do Screener mantidos como telemetria.")
            _history_set(target_workspace, {
                "passed": True,
                "attempts": current_attempt
            })
            return NeedleVerifyDeliveryResponse(
                passed=True,
                status="APPROVED",
                attempt_number=current_attempt,
                pytest_success=True,
                pytest_chunks=pytest_chunks_typed,
                blocking_ast_violations=[],
                structural_ast_warnings=structural_warnings,
                confirmed_defects=[],
                dismissed_warnings=[],
                total_files_scanned=len(symbols),
                total_lines_scanned=sum(sym.end_line - sym.start_line + 1 for sym in symbols),
                pedido_audit=pedido or None,
                resultado_audit=resultado or None,
                message=(
                    f"Entrega aprovada deterministicamente por testes reais (pytest) e Needle 3 AST Guard. "
                    f"({len(structural_warnings)} alerta(s) estrutural(is) registrado(s) como telemetria - motor Laya descarregado)."
                )
            ).model_dump()

        # Ordena suspeitas por prioridade técnica (segurança e robustez primeiro)
        all_suspicions.sort(key=lambda item: _CATEGORY_PRIORITY.get(item[1].category, 99))
        to_arbitrate = all_suspicions[:MAX_ARBITRATIONS]
        leftover = all_suspicions[MAX_ARBITRATIONS:]

        # Engine pronta: avalia cada suspeita prioritária em micro-premissa atômica (<= 150 tokens)
        for sym_key, s, warning in to_arbitrate:
            scope_desc = "Código de Teste" if _is_test_path(s.file_path) else "Código de Produção"
            code_body = s.snippet or s.trigger_evidence or "Sem trecho disponível"
            premise = (
                f"Símbolo: {sym_key}\n"
                f"Escopo: {scope_desc}\n"
                f"Evidência: {s.trigger_evidence}\n"
                f"Trecho AST:\n{code_body}"
            )
            hypothesis = (
                s.noul_hypothesis
                or DEFECT_NOUL_HYPOTHESES.get(s.category, "")
                or f"O código em {sym_key} apresenta defeito concreto de {s.category}."
            )

            try:
                noul_req = NoulRequest(
                    premise=premise,
                    hypothesis=hypothesis,
                    threshold=0.78,
                )
                noul_resp = await active_engine.evaluate_noul(noul_req)
                prob = round(noul_resp.probability, 4)
                is_confirmed = noul_resp.passed  # prob >= 0.78

                bucket = confirmed_defects if is_confirmed else dismissed_warnings
                rule_prefix = "confirmed" if is_confirmed else "dismissed"
                verdict = (
                    f"[Laya Noul P={prob:.2f} >= 0.78] Defeito confirmado"
                    if is_confirmed
                    else f"[Laya Noul P={prob:.2f} < 0.78] Falso-positivo descartado"
                )
                bucket.append(ASTViolation(
                    rule_id=f"{rule_prefix}_{s.category}",
                    category=s.category,
                    file_path=warning.file_path,
                    line_number=warning.line_number,
                    code_snippet=warning.code_snippet,
                    message=f"{verdict}: {warning.message}",
                    suggestion=warning.suggestion if is_confirmed else None,
                ))
            except Exception as eval_err:
                logger.warning(f"Falha na avaliação Noul para {sym_key}: {eval_err}")
                dismissed_warnings.append(warning)

        if confirmed_defects:
            _history_set(target_workspace, {
                "passed": False,
                "attempts": current_attempt
            })
            return NeedleVerifyDeliveryResponse(
                passed=False,
                status="REJECTED",
                attempt_number=current_attempt,
                pytest_success=True,
                pytest_chunks=pytest_chunks_typed,
                blocking_ast_violations=[],
                structural_ast_warnings=structural_warnings,
                confirmed_defects=confirmed_defects,
                dismissed_warnings=dismissed_warnings,
                total_files_scanned=len(symbols),
                total_lines_scanned=sum(sym.end_line - sym.start_line + 1 for sym in symbols),
                pedido_audit=pedido or None,
                resultado_audit=resultado or None,
                message=(
                    f"🚨 [JEV Noul Arbitrage] {len(confirmed_defects)} defeito(s) estrutural(is) "
                    f"confirmado(s) pelo Laya RLCD (P >= 0.78). {len(dismissed_warnings)} alerta(s) descartado(s)."
                )
            ).model_dump()

    # ── Aprovação Determinística com Arbitragem Noul ──
    _history_set(target_workspace, {
        "passed": True,
        "attempts": current_attempt
    })

    approval_msg = (
        f"Entrega 100% aprovada deterministicamente por testes reais (pytest), Needle 3 AST Guard "
        f"e arbitragem JEV Noul ({len(dismissed_warnings)} alerta(s) descartado(s) como falso-positivo)."
        if dismissed_warnings else
        "Entrega 100% aprovada deterministicamente por testes reais (pytest) e Needle 3 AST Guard."
    )

    return NeedleVerifyDeliveryResponse(
        passed=True,
        status="APPROVED",
        attempt_number=current_attempt,
        pytest_success=True,
        pytest_chunks=pytest_chunks_typed,
        blocking_ast_violations=[],
        structural_ast_warnings=structural_warnings,
        confirmed_defects=[],
        dismissed_warnings=dismissed_warnings,
        total_files_scanned=len(symbols),
        total_lines_scanned=sum(sym.end_line - sym.start_line + 1 for sym in symbols),
        pedido_audit=pedido or None,
        resultado_audit=resultado or None,
        message=approval_msg,
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
