"""Módulo de inspeção cirúrgica de diffs reais do Git e particionamento em etapas (stages).

Extrai alterações reais do repositório (staged, unstaged e untracked) e realiza
o particionamento respeitando blocos lógicos de código (hunks, funções, classes e
linhas completas), evitando estourar o orçamento de contexto do modelo Laya (512 tok)
e preservando a coerência sintática da avaliação.
"""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# Teto seguro de caracteres por etapa (1200 chars calibrado para o Golden Set; configurável via JEV_MAX_STAGE_CHARS)
DEFAULT_MAX_STAGE_CHARS = int(os.getenv("JEV_MAX_STAGE_CHARS", "1200"))

# Extensões e caminhos ignorados na inspeção de diffs de código
IGNORE_EXTENSIONS = {
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".lock", ".bin", ".wasm", ".pdf",
}

IGNORE_DIR_PARTS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
}


@dataclass
class DiffFile:
    """Representa um arquivo alterado ou novo no workspace."""
    path: str
    status: str  # 'modified', 'added', 'untracked', 'deleted'
    raw_diff: str
    is_binary: bool = False


@dataclass
class DiffStage:
    """Etapa cirúrgica de diff para avaliação no Laya."""
    stage_id: str
    file_path: str
    part_index: int
    total_parts: int
    content: str
    content_hash: str
    lines_count: int


def is_ignored_path(rel_path: str) -> bool:
    """Verifica se o caminho relativo pertence a diretórios ou extensões ignoradas."""
    p = Path(rel_path)
    if p.suffix.lower() in IGNORE_EXTENSIONS:
        return True
    return any(part in IGNORE_DIR_PARTS for part in p.parts)


def get_workspace_diff_files(workspace_dir: str) -> List[DiffFile]:
    """Extrai todas as alterações do repositório Git (staged, unstaged e untracked).

    Retorna lista de DiffFile ordenada por caminho.
    """
    root = Path(workspace_dir).resolve()
    if not root.is_dir():
        return []

    # 1. Captura diff de arquivos modificados / staged em relação a HEAD
    res_diff = subprocess.run(
        ["git", "diff", "HEAD", "--no-color", "--no-ext-diff"],
        cwd=str(root),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
    )
    diff_text = res_diff.stdout if res_diff.returncode == 0 else ""

    diff_files_map: dict[str, str] = {}
    if diff_text.strip():
        # Divide por arquivo: delimitador "diff --git a/path b/path"
        raw_chunks = re.split(r"(?m)^diff --git a/.* b/(.*)$", diff_text)
        # raw_chunks[0] é o preâmbulo (ou vazio); depois alternam (path, conteúdo do diff)
        i = 1
        while i < len(raw_chunks) - 1:
            file_path = raw_chunks[i].strip()
            patch_content = raw_chunks[i + 1]
            if not is_ignored_path(file_path):
                diff_files_map[file_path] = patch_content.strip()
            i += 2

    # 2. Captura arquivos untracked (arquivos 100% novos que ainda não foram comitados)
    res_untracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=str(root),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
    )
    untracked_paths = (
        [ln.strip() for ln in res_untracked.stdout.splitlines() if ln.strip()]
        if res_untracked.returncode == 0
        else []
    )

    diff_files: List[DiffFile] = []

    # Processa arquivos modificados
    for f_path, patch in diff_files_map.items():
        diff_files.append(
            DiffFile(
                path=f_path,
                status="modified",
                raw_diff=patch,
                is_binary="Binary files" in patch,
            )
        )

    # Processa arquivos novos untracked
    for u_path in untracked_paths:
        if is_ignored_path(u_path):
            continue
        full_p = root / u_path
        if not full_p.is_file():
            continue
        try:
            content = full_p.read_text(encoding="utf-8", errors="replace")
            # Converte em diff sintética de adição (+ linha)
            synth_patch = "".join(f"+{ln}\n" for ln in content.splitlines())
            diff_files.append(
                DiffFile(
                    path=u_path,
                    status="untracked",
                    raw_diff=synth_patch.strip(),
                    is_binary=False,
                )
            )
        except Exception:
            continue

    # Ordena por caminho relativo
    diff_files.sort(key=lambda x: x.path)
    return diff_files


def _clean_diff_line(line: str) -> str:
    """Remove o prefixo de diff (+, -, espaço) para analisar a sintaxe."""
    if line.startswith(("+", "-", " ")):
        return line[1:].strip()
    return line.strip()


def _is_block_start(line: str) -> bool:
    """Verifica se a linha inicia uma nova definição ou bloco de código."""
    clean = _clean_diff_line(line)
    return bool(
        re.match(
            r"^(def\s+|async\s+def\s+|class\s+|function\s+|export\s+|type\s+|interface\s+|struct\s+|fn\s+|pub\s+|@\w+)",
            clean,
        )
    )


def _is_block_end(line: str) -> bool:
    """Verifica se a linha encerra um bloco de código de forma natural."""
    clean = _clean_diff_line(line)
    if not clean:
        return True  # Linha vazia ou em branco é excelente ponto de corte
    if clean in ("}", "};", "end", "fi", "done", "esac"):
        return True
    if re.match(r"^(return(\s+.*)?|raise(\s+.*)?|break|continue|pass)$", clean):
        return True
    return False


def _split_hunk_at_block_boundary(hunk_text: str, max_chars: int) -> List[str]:
    """Divide um hunk grande respeitando estritamente fechamentos de bloco e linhas.

    Garante que:
    1. NUNCA quebra no meio de uma linha ou no meio de palavras.
    2. Prioriza quebras após fechamento de bloco de código (ex.: return, pass, })
       ou antes do início de nova função/classe/definição quando acumulado >= 50% de max_chars.
    3. Respeita o teto de max_chars cortando com segurança na última linha completa.
    """
    lines = hunk_text.splitlines(keepends=True)
    chunks: List[str] = []
    current_lines: List[str] = []
    current_len = 0
    min_split_threshold = int(max_chars * 0.5)

    idx = 0
    total_lines = len(lines)

    while idx < total_lines:
        line = lines[idx]
        line_len = len(line)
        next_line = lines[idx + 1] if idx + 1 < total_lines else ""

        # Verifica se estamos na zona de corte oportunista (>= 50% de max_chars)
        in_split_zone = current_len >= min_split_threshold

        # Ponto de corte 1: Próxima linha inicia um novo bloco (def, class, etc.)
        if in_split_zone and next_line and _is_block_start(next_line):
            current_lines.append(line)
            chunks.append("".join(current_lines).strip())
            current_lines = []
            current_len = 0
            idx += 1
            continue

        # Ponto de corte 2: Linha atual encerra bloco (return, pass, }) e próxima é vazia ou novo bloco
        if in_split_zone and _is_block_end(line) and (not _clean_diff_line(next_line) or _is_block_start(next_line)):
            current_lines.append(line)
            chunks.append("".join(current_lines).strip())
            current_lines = []
            current_len = 0
            idx += 1
            continue

        # Ponto de corte 3: Teto rígido de max_chars atingido
        if current_len + line_len > max_chars and current_lines:
            chunks.append("".join(current_lines).strip())
            current_lines = [line]
            current_len = line_len
            idx += 1
            continue

        current_lines.append(line)
        current_len += line_len
        idx += 1

    if current_lines:
        chunks.append("".join(current_lines).strip())

    return [c for c in chunks if c.strip()]


def partition_file_into_stages(
    diff_file: DiffFile,
    max_chars: int = DEFAULT_MAX_STAGE_CHARS,
) -> List[DiffStage]:
    """Particiona o diff de um arquivo em etapas lógicas e sintaticamente íntegras.

    - Se o diff couber em max_chars: retorna 1 etapa única.
    - Se for maior: quebra preferencialmente por hunks ('@@ ... @@').
    - Se um hunk ainda for maior que max_chars: quebra por linhas/blocos lógicos.
    """
    raw = diff_file.raw_diff.strip()
    if not raw:
        return []

    # Se cabe inteiro em uma etapa
    if len(raw) <= max_chars:
        raw_chunks = [raw]
    else:
        # Tenta separar por hunks do git
        # Procura linhas começando com @@
        hunk_split = re.split(r"(?m)(?=^@@\s+-\d+.*@@)", raw)
        hunk_split = [h.strip() for h in hunk_split if h.strip()]

        raw_chunks = []
        for hunk in hunk_split:
            if len(hunk) <= max_chars:
                raw_chunks.append(hunk)
            else:
                # Hunk muito grande: divide em fronteiras de blocos/linhas
                sub_chunks = _split_hunk_at_block_boundary(hunk, max_chars)
                raw_chunks.extend(sub_chunks)

    if not raw_chunks:
        raw_chunks = [raw[:max_chars]]

    total_parts = len(raw_chunks)
    stages: List[DiffStage] = []

    for idx, chunk_content in enumerate(raw_chunks, start=1):
        # Header contextual amigável para a atenção do Laya
        header = f"--- [Arquivo: {diff_file.path} | Bloco {idx}/{total_parts}] ---"
        stage_text = f"{header}\n{chunk_content}"
        c_hash = hashlib.sha256(stage_text.encode("utf-8")).hexdigest()
        s_id = f"{diff_file.path}:b{idx}_{c_hash[:8]}"

        stages.append(
            DiffStage(
                stage_id=s_id,
                file_path=diff_file.path,
                part_index=idx,
                total_parts=total_parts,
                content=stage_text,
                content_hash=c_hash,
                lines_count=len(chunk_content.splitlines()),
            )
        )

    return stages


def extract_workspace_stages(
    workspace_dir: str,
    max_stage_chars: int = DEFAULT_MAX_STAGE_CHARS,
) -> List[DiffStage]:
    """Extrai e particiona todas as etapas de diff do workspace."""
    diff_files = get_workspace_diff_files(workspace_dir)
    all_stages: List[DiffStage] = []
    for df in diff_files:
        if df.is_binary:
            continue
        file_stages = partition_file_into_stages(df, max_chars=max_stage_chars)
        all_stages.extend(file_stages)
    return all_stages


def format_stage_premise(user_premise: str, stage: DiffStage) -> str:
    """Monta a premissa de avaliação cirúrgica para o Laya.

    Preserva a intenção do usuário (requisitos) e injeta o código real da etapa.
    """
    # Extrai a seção do pedido do prompt se presente
    pedido_match = re.search(r"(?i)pedido\s*(?:do\s*prompt)?\s*:\s*(.*?)(?=\s*\|\s*resultado|$)", user_premise, re.DOTALL)
    pedido_text = pedido_match.group(1).strip() if pedido_match else user_premise[:250].strip()

    # Monta a premissa direcionada
    return (
        f"Pedido: {pedido_text}\n\n"
        f"Código alterado ({stage.file_path} - Bloco {stage.part_index}/{stage.total_parts}):\n"
        f"{stage.content}"
    )
