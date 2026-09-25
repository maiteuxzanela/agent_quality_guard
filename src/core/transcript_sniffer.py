"""Módulo de Transcript Sniffing para Governança e Interceptação Factual de Prompts.

Captura cirurgicamente a intenção real do usuário a partir dos logs de sessão da IDE
(~/.gemini/antigravity-ide/brain/<conv_id>/.system_generated/logs/transcript.jsonl),
eliminando premissas cegas nos Stop Hooks (motivação original: falsos
diagnósticos nulos como 'evidencia_insuficiente' — categoria aposentada em set/2026).
"""

import json
import logging
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("openjev.sniffer")

SCORE_PREMISE_MAX_LEN = 1500
SAFE_PREMISE_CEILING = 1400


def _transcript_path_from_conversation(
    conv_id: str, base_brain_dir: Optional[Path] = None
) -> Optional[Path]:
    """Caminho canônico de transcript para um conversation_id no brain (se existir)."""
    brain = base_brain_dir or (Path.home() / ".gemini" / "antigravity-ide" / "brain")
    candidate = brain / conv_id / ".system_generated" / "logs" / "transcript.jsonl"
    return candidate if candidate.is_file() else None


def _resolve_transcript_from_payload(
    payload: Optional[Dict[str, Any]],
    base_brain_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Passo 1 da resolução: transcript_path direto ou conversation_id no payload.

    Retorno antecipado por passo curto (profundidade de condicional <= 2).
    """
    if not payload:
        return None
    direct_path = payload.get("transcript_path")
    if direct_path and Path(direct_path).resolve().is_file():
        return Path(direct_path).resolve()
    conv_id = payload.get("conversation_id")
    if not conv_id:
        return None
    return _transcript_path_from_conversation(str(conv_id), base_brain_dir)


def find_active_transcript_path(
    payload: Optional[Dict[str, Any]] = None,
    base_brain_dir: Optional[Path] = None,
) -> Optional[Path]:
    """Localiza o arquivo transcript.jsonl da sessão ativa.

    Prioridade de resolução:
    1. Payload do hook: chave 'transcript_path' ou 'conversation_id'
    2. Variáveis de ambiente: ANTIGRAVITY_TRANSCRIPT_PATH ou GEMINI_TRANSCRIPT_PATH
    3. Varredura por st_mtime no diretório brain (~/.gemini/antigravity-ide/brain/)
    """
    # 1. Payload direto
    from_payload = _resolve_transcript_from_payload(payload, base_brain_dir)
    if from_payload:
        return from_payload

    # 2. Variável de ambiente explícita
    env_transcript = os.getenv("ANTIGRAVITY_TRANSCRIPT_PATH", "") or os.getenv("GEMINI_TRANSCRIPT_PATH", "")
    if env_transcript:
        p = Path(env_transcript).resolve()
        if p.is_file():
            return p

    env_conv_id = os.getenv("ANTIGRAVITY_CONVERSATION_ID", "") or os.getenv("CONVERSATION_ID", "")
    if env_conv_id:
        from_env = _transcript_path_from_conversation(env_conv_id, base_brain_dir)
        if from_env:
            return from_env

    # 3. Varredura no brain por mtime mais recente
    brain = base_brain_dir or (Path.home() / ".gemini" / "antigravity-ide" / "brain")
    if brain.is_dir():
        transcripts = list(brain.glob("*/.system_generated/logs/transcript.jsonl"))
        if transcripts:
            # Retorna o arquivo modificado mais recentemente
            return max(transcripts, key=lambda p: p.stat().st_mtime)

    return None


def _parse_user_request_from_lines(lines: List[str]) -> str:
    """Extrai a última mensagem de usuário de uma lista de linhas em complexidade O(N) linear."""
    for raw_line in reversed(lines):
        line = raw_line.strip()
        if not line or not line.startswith("{"):
            continue
        try:
            data = json.loads(line)
        except Exception:
            continue

        if not (isinstance(data, dict) and data.get("type") == "USER_INPUT"):
            continue

        content = data.get("content", "")
        if not content or not isinstance(content, str):
            continue

        match = re.search(r"<USER_REQUEST>(.*?)</USER_REQUEST>", content, re.DOTALL)
        if match and match.group(1).strip():
            return match.group(1).strip()

        clean_content = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", content, flags=re.DOTALL)
        clean_content = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", clean_content, flags=re.DOTALL)
        fallback_req = clean_content.strip()
        if fallback_req:
            return fallback_req

    return ""


def extract_latest_user_request(
    transcript_path: Optional[Path] = None,
    payload: Optional[Dict[str, Any]] = None,
    max_bytes: int = 4 * 1024 * 1024,
    initial_chunk: int = 256 * 1024,
) -> str:
    """Extrai cirurgicamente a última mensagem de USER_INPUT em tempo < 2ms e complexidade O(N)."""
    target_path = transcript_path or find_active_transcript_path(payload=payload)
    if not target_path or not target_path.is_file():
        return ""

    try:
        with open(target_path, "rb") as f:
            f.seek(0, os.SEEK_END)
            file_size = f.tell()
            if file_size == 0:
                return ""

            # 1. Lê o bloco final de 256KB (suficiente para a grande maioria das sessões)
            read_size = min(file_size, initial_chunk)
            f.seek(file_size - read_size)
            raw_chunk = f.read(read_size)
            lines = raw_chunk.decode("utf-8", errors="replace").splitlines()
            user_req = _parse_user_request_from_lines(lines)
            if user_req:
                return user_req

            # 2. Se não localizado no bloco inicial, expande a leitura linear até max_bytes
            if file_size > read_size:
                full_read_size = min(file_size, max_bytes)
                f.seek(file_size - full_read_size)
                larger_chunk = f.read(full_read_size)
                larger_lines = larger_chunk.decode("utf-8", errors="replace").splitlines()
                return _parse_user_request_from_lines(larger_lines)

    except (OSError, IOError) as io_err:
        logger.warning(f"Erro de E/S ao ler transcript em {target_path}: {io_err}")
    except Exception as e:
        logger.warning(f"Erro inesperado ao extrair último comando do transcript: {e}", exc_info=True)

    return ""


def get_git_changes_summary(workspace_dir: Path) -> str:
    """Retorna resumo conciso e factual dos arquivos modificados no git."""
    try:
        ws = Path(workspace_dir).resolve()
        if not ws.is_dir():
            return "Workspace não é um diretório válido"

        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(ws),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if res.returncode != 0:
            return "Status de git indisponível"

        lines = [line.strip() for line in res.stdout.splitlines() if line.strip()]
        if not lines:
            return "Nenhuma alteração pendente no repositório"

        modified_files: List[str] = []
        for line in lines:
            parts = line.split(maxsplit=1)
            if len(parts) == 2:
                status, path = parts
                modified_files.append(f"{path} ({status})")
            else:
                modified_files.append(line)

        # Se houver muitos arquivos, trunca de forma limpa
        if len(modified_files) > 10:
            summary = ", ".join(modified_files[:10]) + f" (+{len(modified_files) - 10} outros)"
        else:
            summary = ", ".join(modified_files)

        return f"Arquivos alterados no git: {summary}"
    except Exception as e:
        logger.warning(f"Erro ao inspecionar git em {workspace_dir}: {e}")
        return f"Erro ao inspecionar git: {e}"


def build_factual_premise(
    workspace: str,
    prompt_override: Optional[str] = None,
    payload: Optional[Dict[str, Any]] = None,
    max_total_len: int = SAFE_PREMISE_CEILING,
) -> str:
    """Compõe a premissa estritamente factual no padrão exigido pelo Open JEV Score:

    'Pedido do Prompt: <intenção real do usuário> | Resultado Executado: <arquivos e status do git>'

    Garante que o texto respeite o teto de 1500 caracteres (padrão safe: 1400)
    e não contenha adjetivos ou auto-avaliações subjetivas (anti premise-gaming).
    """
    try:
        ws_path = Path(workspace).resolve() if workspace else Path.cwd().resolve()
        safe_ceiling = min(SCORE_PREMISE_MAX_LEN, max(300, int(max_total_len)))

        # 1. Obtém o comando do usuário
        user_prompt = ""
        if prompt_override and str(prompt_override).strip():
            user_prompt = str(prompt_override).strip()
        else:
            user_prompt = extract_latest_user_request(payload=payload)

        if not user_prompt:
            user_prompt = f"Atendimento aos requisitos técnicos e tarefas de engenharia em {ws_path.name}"

        # 2. Obtém o resumo dos arquivos git
        git_summary = get_git_changes_summary(ws_path)

        # 3. Formatação preliminar e cálculo de limites
        prefix_pedido = "Pedido do Prompt: "
        sep = " | Resultado Executado: "

        fixed_len = len(prefix_pedido) + len(sep)
        available_chars = max(200, safe_ceiling - fixed_len)

        # Aloca proporção ~65% para o pedido e ~35% para o resultado
        prompt_budget = int(available_chars * 0.65)
        git_budget = available_chars - prompt_budget

        # Sanitização: substitui quebras de linha múltiplas por espaços simples
        clean_prompt = " ".join(user_prompt.split())
        clean_git = " ".join(git_summary.split())

        if len(clean_prompt) > prompt_budget:
            clean_prompt = clean_prompt[: prompt_budget - 3].rstrip() + "..."

        if len(clean_git) > git_budget:
            clean_git = clean_git[: git_budget - 3].rstrip() + "..."

        premise = f"{prefix_pedido}{clean_prompt}{sep}{clean_git}"

        # Corte rígido de segurança final
        if len(premise) > SCORE_PREMISE_MAX_LEN:
            premise = premise[:SCORE_PREMISE_MAX_LEN - 3].rstrip() + "..."

        return premise
    except Exception as e:
        logger.error(f"Erro ao construir premissa factual: {e}", exc_info=True)
        return (
            f"Pedido do Prompt: Atendimento aos requisitos técnicos | "
            f"Resultado Executado: Verificação física de arquivos e integridade de testes no repositório."
        )
