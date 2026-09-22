#!/usr/bin/env python3
"""MCP Server para a Arquitetura Tri-Camada (Projeto JEV).
Disponibiliza Needle 3, Choice, Noul e Score via protocolo padrão MCP (stdio).
"""

import os
import sys
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
    ChoiceRequest,
    DEFECT_DESCRIPTIONS,
    DEFECT_OPTIONS,
    DiagnosisResult,
    Needle3Query,
    NoulRequest,
    ScoreRequest,
    ScoreDiagnoseResponse,
)
from src.decision.engine import OpenJevEngine
from src.memory.local_needle import LocalNeedleEngine

app = MCPServer("open-jev-server")

_engine_instance: Optional[OpenJevEngine] = None


def get_engine() -> OpenJevEngine:
    global _engine_instance
    if _engine_instance is None:
        _engine_instance = OpenJevEngine(
            model_id_or_path="convaiinnovations/laya",
            allow_cpu_fallback=True,
        )
        _engine_instance.load_model()
    return _engine_instance


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

    return {
        "status": "success",
        "latency_ms": round(lat_ms, 2),
        "total_tokens": resp.total_tokens,
        "token_savings_pct": getattr(resp, "token_savings_pct", 85.0),
        "snippets_count": len(resp.snippets),
        "snippets": snippets_data,
    }


@app.tool(
    name="openjev_choice",
    description="Roteamento deterministico de subagente especialista usando Laya (RLCD Classifier) local na GPU GTX 1050 Ti.",
)
async def openjev_choice(
    premise: str,
    options: Optional[List[str]] = None,
    temperature: float = 1.0,
) -> dict:
    """Calcula distribuicao de probabilidades entre subagentes ou caminhos."""
    if not options:
        # Detecta automaticamente o time pelo contexto da premissa ou diretório
        premise_lower = premise.lower()
        cwd_lower = os.getcwd().lower()
        if "panela" in premise_lower or "panela" in cwd_lower or "react" in premise_lower or "mobile" in premise_lower:
            options = [
                "zelia_cto",
                "erica_product_manager",
                "ana_mobile_dev",
                "bia_data_finops",
                "carla_ui_ux",
                "diana_qa_tests",
                "iara_ios_engineer",
                "cris_web_admin",
                "helena_supervisora_aceite",
            ]
        else:
            options = [
                "alex_cto",
                "ariel_brand",
                "lumi_ux",
                "sam_payments",
                "cris_merchant",
                "morgan_security",
                "noa_growth",
                "robin_qa",
            ]

    engine = get_engine()
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
    engine = get_engine()
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


DEFAULT_SCORE_RUBRIC = (
    "Avalie a resposta técnica de 1.0 a 5.0 considerando rigor analítico, "
    "clareza didática, fidelidade ao código real do repositório e atendimento aos comandos do usuário."
)

_score_history: Dict[str, dict] = {}


def _get_workspace_git_signature(workspace_dir: str) -> str:
    """Calcula a assinatura do estado do workspace no Git para impedir reavaliações sem refatoração real."""
    try:
        import subprocess
        res_head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        head = res_head.stdout.strip() if res_head.returncode == 0 else "no_head"
        res_status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=5,
        )
        status = res_status.stdout.strip() if res_status.returncode == 0 else ""
        return f"{head}:{status}"
    except Exception:
        pass
    return ""


@app.tool(
    name="openjev_score",
    description="Avaliacao ordinal de qualidade de codigo de 1.0 a 5.0 usando Laya com protecao anti-premise gaming e saida de emergencia.",
)
async def openjev_score(
    premise: str,
    workspace: str = "",
    rubric: str = DEFAULT_SCORE_RUBRIC,
    min_score: float = 1.0,
    max_score: float = 5.0,
    threshold: float = 3.60,
) -> dict:
    """Avalia conformidade qualitativa em escala continua com travamento anti-gaming e saida de emergencia."""
    target_workspace = workspace.strip() if workspace and workspace.strip() else os.getcwd()
    current_git_state = _get_workspace_git_signature(target_workspace)

    # Verificação Anti-Premise Gaming (Trava de Refatoração no Workspace)
    hist = _score_history.get(target_workspace)
    if hist and not hist.get("passed", True):
        prev_state = hist.get("git_state", "")
        attempts = hist.get("attempts", 1)

        # Se o workspace não sofreu nenhuma alteração de arquivo desde a reprovação
        if current_git_state == prev_state and current_git_state != "":
            if attempts >= 10:
                # Circuito Aberto: Saída de Emergência Desbloqueada
                return {
                    "score": hist.get("last_score", 0.0),
                    "passed": False,
                    "threshold": threshold,
                    "attempts": attempts,
                    "max_attempts": 10,
                    "emergency_exit_unlocked": True,
                    "message": (
                        "🚨 [SAÍDA DE EMERGÊNCIA DESBLOQUEADA]: O limite de 10 tentativas consecutivas de "
                        "refatoração sem aprovação foi atingido. O agente está autorizado a interromper o loop "
                        "e responder ao usuário com um Laudo de Inviabilidade Técnica fundamentado, "
                        "explicando a causa-raiz da impossibilidade e sugerindo alternativas viáveis."
                    ),
                }
            else:
                hist["attempts"] = attempts + 1
                return {
                    "error": (
                        f"🚨 AVALIAÇÃO RECUSADA (Tentativa {attempts}/10): Nenhuma alteração real de código ou "
                        "teste foi detectada no repositório desde a última reprovação. É proibido tentar passar no JEV "
                        "apenas reescrevendo ou adjetivando a premissa. Refatore o código ou testes no disco antes de reavaliar. "
                        "(Caso o pedido seja comprovadamente impossível, atinja 10 tentativas fundamentadas para desbloquear a Saída de Emergência)."
                    ),
                    "attempts": attempts,
                    "max_attempts": 10,
                    "emergency_exit_unlocked": False,
                }

    engine = get_engine()
    req = ScoreRequest(
        premise=premise,
        rubric=rubric,
        min_score=min_score,
        max_score=max_score,
        threshold=threshold,
    )
    resp = await engine.evaluate_score(req)

    # Atualiza histórico de tentativas do workspace
    if resp.passed:
        _score_history[target_workspace] = {
            "passed": True,
            "attempts": 0,
            "git_state": current_git_state,
            "last_score": round(resp.score, 2),
        }
    else:
        prev_attempts = hist.get("attempts", 0) if hist else 0
        _score_history[target_workspace] = {
            "passed": False,
            "attempts": prev_attempts + 1,
            "git_state": current_git_state,
            "last_score": round(resp.score, 2),
        }

    attempts_count = _score_history[target_workspace]["attempts"]
    return {
        "score": round(resp.score, 2),
        "max_score": max_score,
        "normalized_score": round(resp.normalized_score, 4),
        "passed": resp.passed,
        "threshold": threshold,
        "attempts": attempts_count,
        "max_attempts": 10,
        "emergency_exit_unlocked": attempts_count >= 10,
        "breakdown": resp.breakdown,
        "latency_ms": round(resp.latency_ms, 2),
    }



@app.tool(
    name="openjev_score_and_diagnose",
    description=(
        "Avaliacao de qualidade com diagnostico estruturado de defeito. "
        "Se score >= threshold: retorna aprovacao simples (diagnosis=null). "
        "Se score < threshold: executa um choice unico sobre 26 possiveis defeitos flat e retorna "
        "o defeito identificado com confianca, distribuicao de probabilidades e descricao legivel "
        "para auto-correcao precisa pelo agente."
    ),
)
async def openjev_score_and_diagnose(
    premise: str,
    workspace: str = "",
    rubric: str = DEFAULT_SCORE_RUBRIC,
    min_score: float = 1.0,
    max_score: float = 5.0,
    threshold: float = 3.60,
) -> dict:
    """Executa Score + Diagnóstico via choice único flat sobre 26 defeitos possíveis."""
    # DEFECT_OPTIONS e DEFECT_DESCRIPTIONS importados no topo do módulo via src.core.schemas

    target_workspace = workspace.strip() if workspace and workspace.strip() else os.getcwd()
    current_git_state = _get_workspace_git_signature(target_workspace)

    # ── Anti-Premise Gaming ──────────────────────────────────────────────────
    hist = _score_history.get(target_workspace)
    if hist and not hist.get("passed", True):
        prev_state = hist.get("git_state", "")
        attempts = hist.get("attempts", 1)

        if current_git_state == prev_state and current_git_state != "":
            if attempts >= 10:
                return {
                    "score": hist.get("last_score", 0.0),
                    "passed": False,
                    "threshold": threshold,
                    "attempts": attempts,
                    "max_attempts": 10,
                    "emergency_exit_unlocked": True,
                    "diagnosis": None,
                    "message": (
                        "🚨 [SAÍDA DE EMERGÊNCIA DESBLOQUEADA]: Limite de 10 tentativas atingido. "
                        "O agente está autorizado a emitir Laudo de Inviabilidade Técnica."
                    ),
                }
            else:
                hist["attempts"] = attempts + 1
                return {
                    "error": (
                        f"🚨 AVALIAÇÃO RECUSADA (Tentativa {attempts}/10): Nenhuma alteração detectada "
                        "no repositório desde a última reprovação. Refatore o código antes de reavaliar."
                    ),
                    "attempts": attempts,
                    "max_attempts": 10,
                    "emergency_exit_unlocked": False,
                }

    engine = get_engine()

    # ── ETAPA 1: Score ───────────────────────────────────────────────────────
    score_req = ScoreRequest(
        premise=premise,
        rubric=rubric,
        min_score=min_score,
        max_score=max_score,
        threshold=threshold,
    )
    score_resp = await engine.evaluate_score(score_req)

    # Atualiza histórico
    if score_resp.passed:
        _score_history[target_workspace] = {
            "passed": True,
            "attempts": 0,
            "git_state": current_git_state,
            "last_score": round(score_resp.score, 2),
        }
        return ScoreDiagnoseResponse(
            score=round(score_resp.score, 2),
            max_score=max_score,
            normalized_score=round(score_resp.normalized_score, 4),
            passed=True,
            threshold=threshold,
            breakdown=score_resp.breakdown,
            score_latency_ms=round(score_resp.latency_ms, 2),
            diagnosis=None,
            attempts=0,
            max_attempts=10,
            emergency_exit_unlocked=False,
        ).model_dump()

    # Score reprovado — atualiza tentativas
    prev_attempts = hist.get("attempts", 0) if hist else 0
    new_attempts = prev_attempts + 1
    _score_history[target_workspace] = {
        "passed": False,
        "attempts": new_attempts,
        "git_state": current_git_state,
        "last_score": round(score_resp.score, 2),
    }

    # ── ETAPA 2: Choice único flat — 26 defeitos ──────────────────────────────
    breakdown_str = ", ".join(
        f"{k}: {v:.2f}" for k, v in score_resp.breakdown.items() if v > 0.05
    )
    diag_premise = (
        f"Código avaliado com score {score_resp.score:.2f}/{max_score} "
        f"(reprovado, corte={threshold}). "
        f"Distribuição: {breakdown_str}. "
        f"Contexto do artefato: {premise[:500]}"
    )

    t_diag_start = time.perf_counter()
    choice_req = ChoiceRequest(premise=diag_premise, options=DEFECT_OPTIONS)
    choice_resp = await engine.evaluate_choice(choice_req)
    diag_latency_ms = (time.perf_counter() - t_diag_start) * 1000.0

    diagnosis = DiagnosisResult(
        selected=choice_resp.selected,
        confidence=round(choice_resp.top_confidence, 4),
        probabilities={k: round(v, 4) for k, v in choice_resp.probabilities.items()},
        description=DEFECT_DESCRIPTIONS.get(choice_resp.selected, ""),
        diagnosis_latency_ms=round(diag_latency_ms, 2),
    )

    return ScoreDiagnoseResponse(
        score=round(score_resp.score, 2),
        max_score=max_score,
        normalized_score=round(score_resp.normalized_score, 4),
        passed=False,
        threshold=threshold,
        breakdown=score_resp.breakdown,
        score_latency_ms=round(score_resp.latency_ms, 2),
        diagnosis=diagnosis,
        attempts=new_attempts,
        max_attempts=10,
        emergency_exit_unlocked=new_attempts >= 10,
    ).model_dump()


if __name__ == "__main__":
    app.run(transport="stdio")
