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
import shutil
import subprocess
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


def classify_execution_level(
    task_description: str,
    target_files: Optional[List[str]] = None,
) -> int:
    """Classifica a demanda nos Três Níveis de Execução (AGENTS.md §3.1).

    - Nível 1: Dúvidas conceituais, documentação, markdown, .env ou status do git.
    - Nível 2: Implementação técnica, código de produção, testes, lógica ou APIs.
    - Nível 3: Demanda técnica cuja especialidade não existe no catálogo do repositório.
    """
    desc = task_description.lower().strip()
    files = [f.lower() for f in (target_files or [])]

    # Indicadores claros de código de produção (Nível 2)
    code_extensions = (".py", ".js", ".ts", ".jsx", ".tsx", ".css", ".html", ".sh")
    has_code_targets = any(f.endswith(code_extensions) for f in files)
    if has_code_targets:
        return 2

    # Perguntas conceituais, dúvidas ou inspeções sem arquivos alvo de código pertencem ao Nível 1
    conceptual_patterns = ("como funciona", "o que e", "explicar", "qual a diferenca", "por que", "tirar duvida", "inspecionar")
    if desc.startswith(conceptual_patterns) or (desc.endswith("?") and not any(act in desc for act in ["implementar", "criar", "alterar", "corrigir", "refatorar"])):
        return 1

    technical_keywords = [
        "implementar", "criar componente", "refatorar", "corrigir bug", "endpoint",
        "api", "backend", "frontend", "teste", "testes", "banco de dados", "firestore",
        "funcao", "classe", "modulo", "hook", "css", "checkout", "deploy", "migracao",
    ]
    is_technical_task = any(kw in desc for kw in technical_keywords)

    if is_technical_task:
        return 2

    return 1


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

        # Execução especializada: Se for Kael e a tarefa solicitar diagnóstico de hardware / GPU
        execution_report = ""
        clean_task_lower = request.task.lower()
        if "kael" in persona_name.lower() and any(
            k in clean_task_lower for k in ("placa de vídeo", "placa de video", "gpu", "vram", "hardware", "temperatura", "pascal", "1050")
        ):
            execution_report = self._diagnose_gpu_hardware()
        elif os.getenv("JEV_USE_HERMES", "true").lower() in ("true", "1", "yes"):
            hermes_bin = shutil.which("hermes") or os.path.expanduser("~/.local/bin/hermes")
            if os.path.isfile(hermes_bin) and os.access(hermes_bin, os.X_OK):
                hermes_out = await self._execute_via_hermes(
                    hermes_bin=hermes_bin,
                    persona_name=persona_name,
                    system_prompt=system_prompt,
                    task=request.task,
                    workspace=target_workspace,
                    timeout=request.timeout,
                )
                if hermes_out:
                    execution_report = hermes_out

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

        is_invocation_error = (
            "[Hermes]: Timeout de execução atingido" in execution_report
            or "[Hermes]: Falha na invocação" in execution_report
        )
        if is_invocation_error:
            passed_val = False

        if execution_report:
            summary_text = (
                f"{execution_report}\n\n"
                f"---\n"
                f"Status de integridade AST: {'BLOQUEADO' if ast_blocked else 'LIMPO'}. "
                f"Contexto isolado: {context_tokens} tokens."
            )
        else:
            summary_text = (
                f"Subagente '{persona_name}' executou tarefa técnica em contexto isolado. "
                f"Arquivos inspecionados/alterados: {len(files_modified)}. "
                f"Status de integridade AST: {'BLOQUEADO' if ast_blocked else 'LIMPO'}."
            )

        return SpawnSubagentResponse(
            success=not ast_blocked and not is_invocation_error,
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

    def _diagnose_gpu_hardware(self) -> str:
        """Executa rotina diagnóstica real da GPU Pascal GTX 1050 Ti."""
        lines = []
        lines.append("### Relatório Técnico de Hardware — @Kael (ML & Pascal)")

        # 1. nvidia-smi
        try:
            res = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=name,driver_version,temperature.gpu,utilization.gpu,memory.total,memory.used,memory.free,fan.speed",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                check=True,
            )
            parts = [p.strip() for p in res.stdout.strip().split(",")]
            if len(parts) >= 8:
                lines.append(f"- **Dispositivo Físico:** {parts[0]}")
                lines.append(f"- **Driver NVIDIA:** {parts[1]}")
                lines.append(f"- **Temperatura da GPU:** {parts[2]}°C")
                lines.append(f"- **Utilização da GPU:** {parts[3]}%")
                lines.append(f"- **Velocidade da Ventoinha (Fan):** {parts[7]}%")
                lines.append(f"- **VRAM Total:** {parts[4]} MiB")
                lines.append(f"- **VRAM Alocada:** {parts[5]} MiB ({(float(parts[5])/float(parts[4]))*100:.1f}%)")
                lines.append(f"- **VRAM Livre:** {parts[6]} MiB")
        except Exception as e:
            lines.append(f"- **nvidia-smi:** Erro ao consultar: {e}")

        # 2. PyTorch & Profiling
        try:
            from ..decision.hardware import get_hardware_profile
            prof = get_hardware_profile()
            lines.append(f"- **Compute Capability:** {prof.compute_capability[0]}.{prof.compute_capability[1]} (Pascal GP107)")
            lines.append(f"- **Suporte a bfloat16:** {'NATIVO' if prof.supports_bfloat16 else 'NÃO SUPORTADO (Bloqueio Ativo — Coerção para float16/float32)'}")
            lines.append(f"- **Precisão Recomendada:** `{prof.recommended_dtype}`")
            lines.append(f"- **Dispositivo Recomendado:** `{prof.recommended_device}`")
        except Exception as e:
            lines.append(f"- **Perfil de Hardware:** Erro ao carregar: {e}")

        try:
            import torch
            if torch.cuda.is_available():
                lines.append(f"- **PyTorch CUDA:** Ativo (CUDA {torch.version.cuda})")
                lines.append(f"- **Dispositivo PyTorch:** {torch.cuda.get_device_name(0)}")
                alloc = torch.cuda.memory_allocated(0) / (1024 * 1024)
                reserv = torch.cuda.memory_reserved(0) / (1024 * 1024)
                lines.append(f"- **Memória Alocada pelo PyTorch:** {alloc:.2f} MiB")
                lines.append(f"- **Memória Reservada pelo PyTorch:** {reserv:.2f} MiB")
            else:
                lines.append("- **PyTorch CUDA:** Indisponível no processo.")
        except Exception as e:
            lines.append(f"- **PyTorch:** Erro: {e}")

        return "\n".join(lines)

    async def _execute_via_hermes(
        self,
        hermes_bin: str,
        persona_name: str,
        system_prompt: str,
        task: str,
        workspace: Path,
        timeout: Optional[float] = None,
    ) -> str:
        """Executa a tarefa usando o Hermes Agent em contexto isolado.

        Se timeout for None ou <= 0, o timeout fica completamente desativado,
        permitindo que tarefas complexas executem até a conclusão.
        """
        prompt = (
            f"Você é o especialista [{persona_name}].\n"
            f"Diretrizes da Persona:\n{system_prompt}\n\n"
            f"MISSÃO TÉCNICA:\n{task}\n\n"
            "Execute a missão no workspace e forneça um resumo conciso e factual do que foi implementado."
        )

        cmd = [
            hermes_bin,
            "-z",
            prompt,
            "--in",
            str(workspace),
            "--yolo",
        ]

        # Verifica se há timeout definido explicitamente ou via variável de ambiente
        effective_timeout = timeout
        if effective_timeout is None and "JEV_SUBAGENT_TIMEOUT" in os.environ:
            try:
                env_t = float(os.environ["JEV_SUBAGENT_TIMEOUT"])
                effective_timeout = env_t if env_t > 0 else None
            except ValueError:
                pass

        timeout_desc = f"{effective_timeout}s" if effective_timeout and effective_timeout > 0 else "DESATIVADO"
        logger.info(f"Despachando tarefa para Hermes Agent: [{persona_name}] (timeout={timeout_desc})")

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            # Executa com timeout apenas se configurado e > 0
            if effective_timeout is not None and effective_timeout > 0:
                stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=effective_timeout)
            else:
                stdout, stderr = await proc.communicate()

            out_text = stdout.decode("utf-8", errors="replace").strip()
            err_text = stderr.decode("utf-8", errors="replace").strip()

            if proc.returncode != 0:
                logger.warning(f"Hermes finalizou com código {proc.returncode}: {err_text}")
                return f"[Hermes]: {out_text or err_text}"

            return out_text
        except asyncio.TimeoutError:
            try:
                proc.kill()
            except Exception:
                pass
            logger.warning(f"Hermes Agent atingiu timeout de {effective_timeout}s.")
            return f"[Hermes]: Timeout de execução atingido ({effective_timeout}s)."
        except Exception as e:
            logger.warning(f"Erro ao invocar Hermes Agent: {e}")
            return f"[Hermes]: Falha na invocação: {e}"
