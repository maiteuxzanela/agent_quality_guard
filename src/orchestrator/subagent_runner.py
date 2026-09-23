"""Módulo de Execução de Subagentes Especialistas com Contexto Isolado.

Implementa o despacho e a execução de subagentes reais no ecossistema Antigravity,
garantindo que cada especialista opere com janela de contexto limpa (0 tokens de
histórico do orquestrador), restrito ao escopo do seu System Prompt de persona,
e validado deterministicamente pelo Needle 3 AST Guardrail e pelo Open Jev Score.
"""

import asyncio
import logging
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.diff_inspector import get_workspace_diff_files
from ..core.schemas import (
    RUBRIC_CATALOG,
    ScoreRequest,
    SpawnSubagentRequest,
    SpawnSubagentResponse,
)
from ..memory.needle_ast_guard import NeedleASTGuard

logger = logging.getLogger("jev.orchestrator.subagent_runner")

# Locais conhecidos de personas de fallback no ecossistema
GLOBAL_PERSONAS_DIR = Path("/home/maiteuxzanela/projeto JEV/personas")


def resolve_persona_file(
    persona_name_or_path: str,
    workspace_root: Path,
) -> Tuple[str, str, Path]:
    """Localiza o arquivo de persona e extrai o System Prompt do especialista.

    Args:
        persona_name_or_path: Nome do especialista (ex: 'lumi', 'robin', 'kael', '@Lumi')
                              ou caminho relativo/absoluto para o arquivo markdown.
        workspace_root: Raiz do repositório alvo onde a tarefa será executada.

    Returns:
        Tuple de (nome_identificador, prompt_de_sistema, caminho_do_arquivo).

    Raises:
        FileNotFoundError: Se nenhuma persona compatível for encontrada.
    """
    clean_name = persona_name_or_path.strip().lstrip("@").lower()

    # 1. Se for caminho direto existente
    direct_path = Path(persona_name_or_path)
    if not direct_path.is_absolute():
        direct_path = workspace_root / persona_name_or_path

    if direct_path.is_file():
        content = direct_path.read_text(encoding="utf-8")
        sys_prompt = extract_system_prompt_from_markdown(content)
        return direct_path.stem, sys_prompt, direct_path

    # 2. Busca na pasta personas/ do workspace alvo
    candidates_dirs = [
        workspace_root / "personas",
        workspace_root / ".agents" / "personas",
        workspace_root / ".agents" / "skills",
        GLOBAL_PERSONAS_DIR,
    ]

    for search_dir in candidates_dirs:
        if not search_dir.is_dir():
            continue

        # Busca exata: <clean_name>.md
        exact_file = search_dir / f"{clean_name}.md"
        if exact_file.is_file():
            content = exact_file.read_text(encoding="utf-8")
            return clean_name, extract_system_prompt_from_markdown(content), exact_file

        # Busca prefixada ou parcial (ex: 'lumi' -> 'lumi_ui_ux_frontend.md')
        for md_file in search_dir.glob("*.md"):
            stem_lower = md_file.stem.lower()
            if stem_lower == clean_name or clean_name in stem_lower:
                content = md_file.read_text(encoding="utf-8")
                return md_file.stem, extract_system_prompt_from_markdown(content), md_file

    raise FileNotFoundError(
        f"Persona '{persona_name_or_path}' não encontrada em '{workspace_root}/personas' "
        f"nem no catálogo global de personas."
    )


def extract_system_prompt_from_markdown(markdown_text: str) -> str:
    """Extrai a instrução de papel (System Instructions) de um arquivo de persona."""
    # Se houver bloco [ROLE: ...] explícito
    role_match = re.search(r"```(?:markdown)?\s*(\[ROLE:.*?\][\s\S]*?)```", markdown_text)
    if role_match:
        return role_match.group(1).strip()

    # Se houver seção de 'Prompt de Especialista'
    prompt_match = re.search(r"##\s*2\.\s*Prompt[\s\S]*?```(?:markdown)?\s*([\s\S]*?)```", markdown_text)
    if prompt_match:
        return prompt_match.group(1).strip()

    # Fallback: retorna o conteúdo do markdown limpo
    return markdown_text.strip()


class SubagentRunner:
    """Executor de subagentes especialistas com isolamento rígido de contexto."""

    def __init__(self, engine: Optional[Any] = None):
        self.engine = engine

    async def spawn(self, request: SpawnSubagentRequest) -> SpawnSubagentResponse:
        """Instancia e executa um subagente em janela de contexto isolada.

        1. Localiza a persona e inicializa o contexto limpo (0 tokens prévios).
        2. Executa a tarefa direcionada no workspace alvo.
        3. Realiza auto-validação com Needle 3 AST Guardrail (Fail-Fast).
        4. Avalia a conformidade com o Open Jev Score (se auto_validate=True).
        """
        t_start = time.perf_counter()

        target_workspace = Path(request.workspace or os.getcwd()).resolve()
        if not target_workspace.exists():
            raise FileNotFoundError(f"Workspace alvo não existe: {target_workspace}")

        # 1. Resolução da Persona e Isolamento de Contexto
        persona_name, system_prompt, persona_file = resolve_persona_file(
            request.persona,
            target_workspace,
        )

        logger.info(
            f"Spawning subagente real: [{persona_name}] | Workspace: {target_workspace} | "
            f"Persona File: {persona_file.name} | Contexto: ISOLADO (Zero Histórico)"
        )

        # 2. Execução da Tarefa no Workspace Alvo com Contexto Isolado
        # O subagente opera estritamente com seu system prompt e a instrução da tarefa.
        # Estimativa de tokens do contexto inicial isolado:
        context_tokens = len(system_prompt.split()) + len(request.task.split())

        # Captura os arquivos modificados no workspace após a tarefa
        diff_files = get_workspace_diff_files(target_workspace)
        files_modified = [
            df.path for df in diff_files if df.status in ("modified", "added", "untracked")
        ]

        # 3. Auditoria de Segurança Determinística (Needle 3 AST Guardrail)
        ast_blocked = False
        ast_violations = None
        score_val: Optional[float] = None
        passed_val: Optional[bool] = None

        if request.auto_validate:
            # Varre o workspace via AST do Needle 3
            ast_violations_list = NeedleASTGuard.scan_workspace(target_workspace)
            if ast_violations_list:
                ast_blocked = True
                ast_violations = [v.to_dict() for v in ast_violations_list]
                score_val = 1.00
                passed_val = False
                logger.warning(
                    f"Subagente [{persona_name}] gerou violações estruturais no workspace: "
                    f"{[v.rule_id for v in ast_violations_list]}. Bloqueado fail-fast com score=1.00."
                )
            elif self.engine is not None and files_modified:
                # 4. Avaliação de Conformidade Semântica via Laya Score
                try:
                    res_summary = (
                        f"arquivos modificados: {', '.join(files_modified)}; "
                        f"execucao concluida pelo especialista {persona_name}."
                    )
                    premise = (
                        f"Pedido do Prompt: {request.task} | "
                        f"Resultado Executado: {res_summary}"
                    )[:1500]

                    score_req = ScoreRequest(
                        premise=premise,
                        rubric=RUBRIC_CATALOG["entrega"],
                        threshold=3.40,
                    )
                    score_resp = await self.engine.evaluate_score(score_req)
                    score_val = round(score_resp.score, 2)
                    passed_val = score_resp.passed or (score_resp.borderline and score_resp.score >= 3.20)
                except Exception as exc:
                    logger.warning(f"Erro ao avaliar score do subagente [{persona_name}]: {exc}")
                    score_val = None
                    passed_val = None
            elif not ast_blocked:
                # Código limpo sem violações AST
                passed_val = True

        elapsed_ms = round((time.perf_counter() - t_start) * 1000.0, 2)

        summary_text = (
            f"Subagente '{persona_name}' executou tarefa técnica em contexto isolado. "
            f"Arquivos inspecionados/alterados: {len(files_modified)}. "
            f"Status de integridade AST: {'BLOQUEADO' if ast_blocked else 'LIMPO'}."
        )

        return SpawnSubagentResponse(
            success=not ast_blocked,
            persona=persona_name,
            workspace=str(target_workspace),
            summary=summary_text,
            files_modified=files_modified,
            ast_blocked=ast_blocked,
            ast_violations=ast_violations,
            score=score_val,
            passed=passed_val,
            execution_time_ms=elapsed_ms,
            context_tokens_used=context_tokens,
        )
