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
from typing import Dict, List, Optional

# Silencia avisos
os.environ["PYTHONWARNINGS"] = "ignore"
warnings.filterwarnings("ignore")

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
    DEFECT_OPTIONS,
    DIAG_CHOICE_INSTRUCTIONS,
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
from src.decision.engine import OpenJevEngine
from src.decision.laya_adapter import evaluate_borderline_mass_gate
from src.memory.local_needle import LocalNeedleEngine
from src.memory.needle_ast_guard import NeedleASTGuard
from src.orchestrator.subagent_runner import SubagentRunner

logger = logging.getLogger("jev.mcp")

app = MCPServer("open-jev-server")

_engine_instance: Optional[OpenJevEngine] = None
_engine_lock = threading.Lock()


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
    """Executa um subagente especialista em contexto isolado com auto-validacao AST + Score."""
    if not persona.strip():
        raise ValueError("openjev_spawn_subagent exige o parametro 'persona' preenchido.")
    if not task.strip():
        raise ValueError("openjev_spawn_subagent exige o parametro 'task' com a descricao da tarefa.")

    engine = await _get_engine_async()
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



DEFAULT_SCORE_RUBRIC = RUBRIC_CATALOG["entrega"]

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




# NOTA: a tool `openjev_score` foi expurgada (set/2026) — todo o fluxo de
# score (validação de premissa, catálogo de rubricas, anti-premise gaming,
# circuit breaker e rubric_source) vive exclusivamente em
# `openjev_score_and_diagnose`, logo abaixo.

# Circuit breaker (MAX_ATTEMPTS) vem de score_gate — fonte única.

# Categorias nulas do choice flat: o diagnóstico não aponta defeito concreto.
# Em borderline, estas categorias aprovam a entrega (carve-out §4.4) SOMENTE
# se a confiança do choice for >= NULL_CHOICE_MIN_CONFIDENCE (gate anti-FP:
# nula fraca não deve resgatar entrega reprovada, ex.: injecao_sql).
NULL_DEFECT_OPTIONS = frozenset(
    {
        "sem_defeito_identificavel",
        "evidencia_insuficiente",
    }
)

# Confiança mínima do choice para a nula valer como aprovação borderline.
# Alinhado a Router.min_confidence (0.40). FP medido (injecao → nula) teve ~0.216.
NULL_CHOICE_MIN_CONFIDENCE = 0.40


@app.tool(
    name="openjev_score_and_diagnose",
    description=(
        "Avaliacao de qualidade com diagnostico estruturado de defeito. "
        "Se score >= threshold: retorna aprovacao simples (diagnosis=null). "
        "Se score < threshold: executa um choice unico sobre 10 categorias de defeito flat "
        "(calibrado — listas >= 11 opções degradam o head do Laya) e retorna a categoria "
        "identificada com confianca, distribuicao, descricao legivel e candidate_defects "
        "(folhas candidatas) para auto-correcao. "
        "Se borderline=true, o choice AINDA roda: se selected for sem_defeito_identificavel "
        "ou evidencia_insuficiente E confidence >= 0.40, passed=true com "
        "approved_as_borderline=true (divulgar score_margin); nula fraca ou defeito "
        "concreto => passed=false e exige correcao real "
        "no disco. Circuit breaker apos 5 tentativas."
    ),
)
async def openjev_score_and_diagnose(
    premise: str,
    workspace: str = "",
    task_type: str = "entrega",
    rubric: str = "",
    min_score: float = 1.0,
    max_score: float = 5.0,
    threshold: float = 3.40,
) -> dict:
    """Executa Score + Diagnóstico via choice único flat sobre 10 categorias de defeito."""
    # DEFECT_OPTIONS e DEFECT_DESCRIPTIONS importados no topo do módulo via src.core.schemas
    premise_error = _validate_score_premise(premise)
    if premise_error:
        return {"error": premise_error, "premise_rejected": True, "diagnosis": None}
    resolved_rubric, rubric_error = _resolve_task_rubric(task_type)
    if rubric_error:
        return {"error": rubric_error, "task_type_rejected": True, "diagnosis": None}
    task_type_key = task_type.strip().lower()

    target_workspace = workspace.strip() if workspace and workspace.strip() else os.getcwd()
    current_git_state = _get_workspace_git_signature(target_workspace)

    # ── Anti-Premise Gaming (history sob lock: chamadas concorrentes) ────────
    hist = _history_get(target_workspace)
    if hist and not hist.get("passed", True):
        prev_state = hist.get("git_state", "")
        attempts = hist.get("attempts", 1)

        if current_git_state == prev_state and current_git_state != "":
            if attempts >= MAX_ATTEMPTS:
                return {
                    "score": hist.get("last_score", 0.0),
                    "passed": False,
                    "threshold": threshold,
                    "attempts": attempts,
                    "max_attempts": MAX_ATTEMPTS,
                    "emergency_exit_unlocked": True,
                    "diagnosis": None,
                    "message": (
                        f"🚨 [SAÍDA DE EMERGÊNCIA DESBLOQUEADA]: Limite de {MAX_ATTEMPTS} tentativas atingido. "
                        "O agente está autorizado a emitir Laudo de Inviabilidade Técnica."
                    ),
                }
            else:
                _history_increment_attempts(target_workspace)
                return {
                    "error": (
                        f"🚨 AVALIAÇÃO RECUSADA (Tentativa {attempts}/{MAX_ATTEMPTS}): Nenhuma alteração detectada "
                        "no repositório desde a última reprovação. Refatore o código antes de reavaliar."
                    ),
                    "attempts": attempts,
                    "max_attempts": MAX_ATTEMPTS,
                    "emergency_exit_unlocked": False,
                }

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

    # ── Avaliação Multi-Etapas via Diffs Reais do Git ─────────────────────────
    diff_stages = extract_workspace_stages(target_workspace)

    if diff_stages:
        evaluated_stages: List[DiffStageResult] = []
        for st in diff_stages:
            cached_data = _stages_cache_get(target_workspace, st.stage_id)
            if cached_data and cached_data.get("passed", False):
                # Cache Hit: etapa inalterada já aprovada em ciclo anterior
                evaluated_stages.append(
                    DiffStageResult(
                        stage_id=st.stage_id,
                        file_path=st.file_path,
                        part_index=st.part_index,
                        total_parts=st.total_parts,
                        score=cached_data["score"],
                        passed=True,
                        borderline=cached_data.get("borderline", False),
                        cached=True,
                        diagnosis=None,
                    )
                )
                continue

            # Avalia a etapa individualmente com o Laya Score
            st_premise = format_stage_premise(premise, st)
            st_score_req = ScoreRequest(
                premise=st_premise,
                rubric=resolved_rubric,
                min_score=min_score,
                max_score=max_score,
                threshold=threshold,
            )
            st_score_resp = await engine.evaluate_score(st_score_req)

            st_passed = st_score_resp.passed
            st_borderline = bool(st_score_resp.borderline)
            st_diag = None

            if not st_passed and st_borderline:
                mass_ok, _, _ = evaluate_borderline_mass_gate(
                    st_score_resp.breakdown,
                    healthy_min=BORDERLINE_HEALTHY_MIN_MASS,
                    flawed_max=BORDERLINE_FLAWED_MAX_MASS,
                )
                if mass_ok:
                    st_passed = True

            if not st_passed:
                # Dispara choice sobre as 10 categorias no diff da etapa
                st_diag_premise = (
                    f"Código alterado avaliado com score {st_score_resp.score:.2f}/{max_score} "
                    f"(corte={threshold}, borderline={st_borderline}). "
                    f"Arquivo: {st.file_path} (bloco {st.part_index}/{st.total_parts}).\n"
                    f"{st.content[:700]}"
                )
                t_st_diag = time.perf_counter()
                st_choice_req = ChoiceRequest(
                    premise=st_diag_premise,
                    options=DEFECT_OPTIONS,
                    instructions=DIAG_CHOICE_INSTRUCTIONS,
                    criteria=DEFECT_DESCRIPTIONS,
                )
                st_choice_resp = await engine.evaluate_choice(st_choice_req)
                st_diag = DiagnosisResult(
                    selected=st_choice_resp.selected,
                    confidence=round(st_choice_resp.top_confidence, 4),
                    probabilities={k: round(v, 4) for k, v in st_choice_resp.probabilities.items()},
                    description=DEFECT_DESCRIPTIONS.get(st_choice_resp.selected, ""),
                    candidate_defects=list(DEFECT_MEMBERS.get(st_choice_resp.selected, [])),
                    diagnosis_latency_ms=round((time.perf_counter() - t_st_diag) * 1000.0, 2),
                )
                if (
                    st_borderline
                    and st_diag.selected in NULL_DEFECT_OPTIONS
                    and st_diag.confidence >= NULL_CHOICE_MIN_CONFIDENCE
                ):
                    st_passed = True

            # Registra no cache de etapas aprovadas
            if st_passed:
                _stages_cache_set(
                    target_workspace,
                    st.stage_id,
                    {
                        "score": round(st_score_resp.score, 2),
                        "passed": True,
                        "borderline": st_borderline,
                    },
                )

            evaluated_stages.append(
                DiffStageResult(
                    stage_id=st.stage_id,
                    file_path=st.file_path,
                    part_index=st.part_index,
                    total_parts=st.total_parts,
                    score=round(st_score_resp.score, 2),
                    passed=st_passed,
                    borderline=st_borderline,
                    cached=False,
                    diagnosis=st_diag if not st_passed else None,
                )
            )

        all_passed = all(s.passed for s in evaluated_stages)
        passed_cnt = sum(1 for s in evaluated_stages if s.passed)
        failed_cnt = len(evaluated_stages) - passed_cnt
        avg_score = round(sum(s.score for s in evaluated_stages) / len(evaluated_stages), 2)
        any_borderline = any(s.borderline for s in evaluated_stages)
        first_failure = next((s for s in evaluated_stages if not s.passed), None)
        global_diag = first_failure.diagnosis if first_failure else None

        if all_passed:
            _history_set(target_workspace, {
                "passed": True,
                "attempts": 0,
                "git_state": current_git_state,
                "last_score": avg_score,
            })
            return ScoreDiagnoseResponse(
                score=avg_score,
                max_score=max_score,
                normalized_score=round((avg_score - min_score) / (max_score - min_score), 4),
                passed=True,
                threshold=threshold,
                breakdown={},
                score_latency_ms=0.0,
                score_margin=round(avg_score - threshold, 4),
                borderline=any_borderline,
                task_type=task_type_key,
                rubric_source=f"catalog:{task_type_key}",
                diagnosis=None,
                approved_as_borderline=any_borderline,
                stages=evaluated_stages,
                total_stages=len(evaluated_stages),
                passed_stages=passed_cnt,
                failed_stages=failed_cnt,
                attempts=0,
                max_attempts=MAX_ATTEMPTS,
                emergency_exit_unlocked=False,
            ).model_dump()
        else:
            prev_attempts = hist.get("attempts", 0) if hist else 0
            new_attempts = prev_attempts + 1
            _history_set(target_workspace, {
                "passed": False,
                "attempts": new_attempts,
                "git_state": current_git_state,
                "last_score": avg_score,
            })
            return ScoreDiagnoseResponse(
                score=avg_score,
                max_score=max_score,
                normalized_score=round((avg_score - min_score) / (max_score - min_score), 4),
                passed=False,
                threshold=threshold,
                breakdown={},
                score_latency_ms=0.0,
                score_margin=round(avg_score - threshold, 4),
                borderline=any_borderline,
                task_type=task_type_key,
                rubric_source=f"catalog:{task_type_key}",
                diagnosis=global_diag,
                approved_as_borderline=False,
                stages=evaluated_stages,
                total_stages=len(evaluated_stages),
                passed_stages=passed_cnt,
                failed_stages=failed_cnt,
                attempts=new_attempts,
                max_attempts=MAX_ATTEMPTS,
                emergency_exit_unlocked=new_attempts >= MAX_ATTEMPTS,
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

    # Choice de diagnóstico flat (10 categorias)
    diag_premise = (
        f"Código avaliado com score {score_resp.score:.2f}/{max_score} "
        f"(corte={threshold}, borderline={bool(score_resp.borderline)}). "
        f"Contexto do artefato: {premise[:500]}"
    )

    t_diag_start = time.perf_counter()
    choice_req = ChoiceRequest(
        premise=diag_premise,
        options=DEFECT_OPTIONS,
        instructions=DIAG_CHOICE_INSTRUCTIONS,
        criteria=DEFECT_DESCRIPTIONS,
    )
    choice_resp = await engine.evaluate_choice(choice_req)
    diag_latency_ms = (time.perf_counter() - t_diag_start) * 1000.0

    diagnosis = DiagnosisResult(
        selected=choice_resp.selected,
        confidence=round(choice_resp.top_confidence, 4),
        probabilities={k: round(v, 4) for k, v in choice_resp.probabilities.items()},
        description=DEFECT_DESCRIPTIONS.get(choice_resp.selected, ""),
        candidate_defects=list(DEFECT_MEMBERS.get(choice_resp.selected, [])),
        diagnosis_latency_ms=round(diag_latency_ms, 2),
    )
    mono_stage.diagnosis = diagnosis

    null_elected = diagnosis.selected in NULL_DEFECT_OPTIONS
    null_conf_ok = diagnosis.confidence >= NULL_CHOICE_MIN_CONFIDENCE
    if score_resp.borderline and null_elected and null_conf_ok:
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
            diagnosis=diagnosis,
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
        diagnosis=diagnosis,
        approved_as_borderline=False,
        stages=[mono_stage],
        total_stages=1,
        passed_stages=0,
        failed_stages=1,
        attempts=new_attempts,
        max_attempts=MAX_ATTEMPTS,
        emergency_exit_unlocked=new_attempts >= MAX_ATTEMPTS,
    ).model_dump()


if __name__ == "__main__":
    def _graceful_exit(signum, frame) -> None:
        logger.info("Sinal %s recebido no servidor MCP, encerrando de forma limpa...", signum)
        sys.exit(0)

    try:
        signal.signal(signal.SIGTERM, _graceful_exit)
        signal.signal(signal.SIGINT, _graceful_exit)
    except Exception:
        pass

    def _preload_engine() -> None:
        try:
            get_engine()
            logger.info("Motor JEV pré-carregado no startup do MCP.")
        except Exception as e:
            logger.warning("Pré-load do motor JEV falhou (retry no 1º uso): %s", e)

    # Pré-load em background: a 1ª tool call não paga o load de pesos na GPU/CPU.
    # O get_engine é thread-safe; se o preload ainda estiver rodando, o await
    # de to_thread espera o mesmo singleton sob o lock.
    threading.Thread(target=_preload_engine, daemon=True, name="jev-engine-preload").start()
    app.run(transport="stdio")
