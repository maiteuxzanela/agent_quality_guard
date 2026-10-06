"""Módulo de inspeção cirúrgica de diffs reais do Git e particionamento em etapas (stages).

Extrai alterações reais do repositório (staged, unstaged e untracked) e realiza
o particionamento respeitando blocos lógicos de código (hunks, funções, classes e
linhas completas), evitando estourar o orçamento de contexto do modelo Laya (512 tok)
e preservando a coerência sintática da avaliação.
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("diff_inspector")

# Teto seguro de caracteres por etapa (4000 chars calibrado para janela de 1024 tok do Laya; configurável via JEV_MAX_STAGE_CHARS)
DEFAULT_MAX_STAGE_CHARS = int(os.getenv("JEV_MAX_STAGE_CHARS", "4000"))

# Extensões e caminhos ignorados na inspeção de diffs de código de produção
IGNORE_EXTENSIONS = {
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".lock", ".bin", ".wasm", ".pdf",
    ".md", ".markdown", ".txt", ".rst", ".json", ".yaml", ".yml", ".toml",
    ".sh", ".bash",
}

IGNORE_DIR_PARTS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
    ".agents", "scripts", "bin", "docs",
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


def normalize_workspace_relpath(path: str | Path, workspace_root: str | Path) -> str:
    """Normaliza qualquer caminho para caminho relativo POSIX canônico ao workspace.

    Exemplo:
        >>> normalize_workspace_relpath("/repo/src/mod.py", "/repo")
        'src/mod.py'
        >>> normalize_workspace_relpath("./src/mod.py", "/repo")
        'src/mod.py'
    """
    p = Path(path)
    w = Path(workspace_root).resolve()
    try:
        if p.is_absolute():
            return str(p.resolve().relative_to(w)).replace("\\", "/")
        resolved = (w / p).resolve()
        if resolved.is_relative_to(w):
            return str(resolved.relative_to(w)).replace("\\", "/")
        clean = str(p).replace("\\", "/")
        return clean[2:] if clean.startswith("./") else clean
    except Exception:
        clean = str(p).replace("\\", "/")
        return clean[2:] if clean.startswith("./") else clean


def is_ignored_path(rel_path: str) -> bool:
    """Verifica se o caminho relativo pertence a diretórios ou extensões ignoradas."""
    p = Path(rel_path)
    if p.suffix.lower() in IGNORE_EXTENSIONS:
        return True
    return any(part in IGNORE_DIR_PARTS for part in p.parts)


def _commit_fallback_files(root: Path) -> List[DiffFile]:
    """Fallback HEAD~1..HEAD quando a árvore de trabalho está 100% limpa.

    Mantém o mesmo parsing por hunk do diff do commit, com profundidade de
    condicional <= 2 (retornos antecipados e laço plano).
    """
    res_commit = subprocess.run(
        ["git", "diff", "HEAD~1", "HEAD", "--no-color", "--no-ext-diff", "-U12"],
        cwd=str(root),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
    )
    if res_commit.returncode != 0 or not res_commit.stdout.strip():
        res_commit = subprocess.run(
            ["git", "diff", "HEAD~1", "HEAD", "--no-color", "--no-ext-diff"],
            cwd=str(root),
            capture_output=True,
            text=True,
            errors="replace",
            timeout=10,
        )
    if res_commit.returncode != 0 or not res_commit.stdout.strip():
        return []
    out: List[DiffFile] = []
    matches = list(re.finditer(r"(?m)^diff --git a/(?:.+?) b/(.+?)$", res_commit.stdout))
    for idx, match in enumerate(matches):
        file_path = match.group(1).strip()
        if file_path.startswith('"') and file_path.endswith('"'):
            file_path = file_path[1:-1]
        if is_ignored_path(file_path):
            continue
        start_idx = match.end()
        end_idx = matches[idx + 1].start() if idx + 1 < len(matches) else len(res_commit.stdout)
        patch_content = res_commit.stdout[start_idx:end_idx].strip()
        out.append(
            DiffFile(
                path=file_path,
                status="committed",
                raw_diff=patch_content,
                is_binary="Binary files" in patch_content,
            )
        )
    return out


def get_workspace_diff_files(workspace_dir: str) -> List[DiffFile]:
    """Extrai todas as alterações do repositório Git (staged, unstaged e untracked).

    Retorna lista de DiffFile ordenada por caminho.
    """
    root = Path(workspace_dir).resolve()
    if not root.is_dir():
        return []

    # 1. Captura diff de arquivos modificados / staged em relação a HEAD com contexto cirúrgico (-U12)
    res_diff = subprocess.run(
        ["git", "diff", "HEAD", "--no-color", "--no-ext-diff", "-U12"],
        cwd=str(root),
        capture_output=True,
        text=True,
        errors="replace",
        timeout=10,
    )
    if res_diff.returncode != 0 or not res_diff.stdout.strip():
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
        # Divide por arquivo usando regex iterativa robusta contra cabeçalhos git diff
        matches = list(re.finditer(r"(?m)^diff --git a/(?:.+?) b/(.+?)$", diff_text))
        for idx, match in enumerate(matches):
            file_path = match.group(1).strip()
            if file_path.startswith('"') and file_path.endswith('"'):
                file_path = file_path[1:-1]
            start_idx = match.end()
            end_idx = matches[idx + 1].start() if idx + 1 < len(matches) else len(diff_text)
            patch_content = diff_text[start_idx:end_idx].strip()
            if not is_ignored_path(file_path):
                diff_files_map[file_path] = patch_content

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
            # Converte em diff sintética de adição (+ linha) sem duplicação de quebra
            synth_patch = "".join(f"+{ln}\n" for ln in content.splitlines())
            diff_files.append(
                DiffFile(
                    path=u_path,
                    status="untracked",
                    raw_diff=synth_patch.strip(),
                    is_binary=False,
                )
            )
        except Exception as err:
            logger.debug(f"Aviso ao ler arquivo untracked {u_path}: {err}")
            continue

    # 3. Fallback para commit recente: se a arvore de trabalho estiver 100% limpa,
    # inspeciona cirurgicamente as alteracoes reais do ultimo commit (HEAD~1..HEAD),
    # impedindo a queda cega no caminho mono-etapa textual.
    if not diff_files:
        diff_files.extend(_commit_fallback_files(root))

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
    min_split_threshold = int(max_chars * 0.85) if max_chars >= 1000 else int(max_chars * 0.50)

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

    Estratégia de Consolidação e Empacotamento Ganancioso:
    1. Se o diff total do arquivo couber dentro de `max_chars`, retorna uma etapa única
       consolidando todos os hunks juntos para máxima coerência semântica.
    2. Se o diff total exceder `max_chars`:
       a) Decompõe o arquivo em unidades atômicas (hunks do Git ou subdivisões de blocos
          sintáticos que respeitam `max_chars`).
       b) Realiza empacotamento ganancioso (greedy packing) acumulando o maior número
          possível de unidades contíguas por bloco, minimizando o total de etapas geradas
          e preservando a visão coesa do arquivo.
    """
    raw_content = diff_file.raw_diff.strip()
    if not raw_content:
        return []

    # Limiar elástico de tolerância suave para produção (15% sobre max_chars quando max_chars >= 1000)
    soft_max_chars = int(max_chars * 1.15) if max_chars >= 1000 else max_chars
    min_orphan_chars = max(1, min(400, int(max_chars * 0.15)))
    max_absorb_unit = max(10, min(800, int(max_chars * 0.20)))

    # Caso 1: Todo o diff do arquivo cabe dentro do orçamento de caracteres elástico da etapa
    if len(raw_content) <= soft_max_chars:
        raw_chunks: List[str] = [raw_content]
    else:
        # Caso 2: O arquivo excede max_chars e requer particionamento estruturado
        hunk_split = re.split(r"(?m)(?=^@@\s+-\d+.*@@)", raw_content)
        hunk_split = [h.strip() for h in hunk_split if h.strip()]
        if not hunk_split:
            hunk_split = [raw_content]
        elif len(hunk_split) > 1 and not hunk_split[0].startswith("@@"):
            # O preâmbulo do Git (index, ---, +++) não é código executável isolado:
            # anexa-o ao cabeçalho do primeiro hunk real com código
            preamble = hunk_split.pop(0)
            hunk_split[0] = f"{preamble}\n\n{hunk_split[0]}"

        # Decompõe em unidades atômicas estritamente menores ou iguais a soft_max_chars
        atomic_units: List[str] = []
        for hunk in hunk_split:
            if len(hunk) <= soft_max_chars:
                atomic_units.append(hunk)
            else:
                sub_chunks = _split_hunk_at_block_boundary(hunk, max_chars)
                # Previne que o último sub-chunk seja um fragmento anão/órfão
                if len(sub_chunks) > 1 and len(sub_chunks[-1]) < min_orphan_chars:
                    sub_chunks[-2] = f"{sub_chunks[-2]}\n\n{sub_chunks[-1]}"
                    sub_chunks.pop()
                atomic_units.extend(sub_chunks)

        if not atomic_units:
            atomic_units = [raw_content[:max_chars]]

        # Empacotamento ganancioso de unidades atômicas contíguas com absorção elástica
        raw_chunks = []
        current_chunk_parts: List[str] = []
        current_chunk_len = 0

        for unit in atomic_units:
            unit_len = len(unit)
            separator_len = 2 if current_chunk_parts else 0  # "\n\n"
            # Absorve se couber em max_chars OU se for unidade pequena que cabe em soft_max_chars
            if current_chunk_parts and (current_chunk_len + separator_len + unit_len > max_chars):
                if (current_chunk_len + separator_len + unit_len <= soft_max_chars) and (unit_len <= max_absorb_unit):
                    current_chunk_parts.append(unit)
                    current_chunk_len += separator_len + unit_len
                else:
                    raw_chunks.append("\n\n".join(current_chunk_parts))
                    current_chunk_parts = [unit]
                    current_chunk_len = unit_len
            else:
                current_chunk_parts.append(unit)
                current_chunk_len += separator_len + unit_len

        if current_chunk_parts:
            # Se a sobra final for minúscula e já existir bloco anterior, funde para evitar fragmentos órfãos
            if raw_chunks and current_chunk_len < min_orphan_chars:
                raw_chunks[-1] = f"{raw_chunks[-1]}\n\n" + "\n\n".join(current_chunk_parts)
            else:
                raw_chunks.append("\n\n".join(current_chunk_parts))

    if not raw_chunks:
        raw_chunks = [raw_content[:max_chars]]

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

    Preserva a intenção do usuário (requisitos) e o resultado executado factual,
    injetando o código real da etapa dentro do teto seguro de 1500 caracteres
    sem duplicações ou desperdício de tokens.
    """
    pedido_match = re.search(
        r"(?i)pedido\s*(?:do\s*prompt)?\s*:\s*(.*?)(?=\s*\|\s*resultado|$)",
        user_premise,
        re.DOTALL,
    )
    pedido_text = pedido_match.group(1).strip() if pedido_match else user_premise[:250].strip()

    resultado_match = re.search(
        r"(?i)\|\s*resultado\s*(?:executado)?\s*:\s*(.*?)$",
        user_premise,
        re.DOTALL,
    )
    resultado_text = resultado_match.group(1).strip() if resultado_match else ""
    if resultado_text:
        # Preserva o resumo factual (testes, status) fornecido pelo agente ou sniffer
        resultado_summary = resultado_text.splitlines()[0][:200].strip()
        prefix = (
            f"Pedido: {pedido_text} | "
            f"Resultado: {resultado_summary}\n"
            f"Diff em {stage.file_path} (bloco {stage.part_index}/{stage.total_parts}):\n"
        )
    else:
        prefix = (
            f"Pedido: {pedido_text} | "
            f"Resultado: Alteração em {stage.file_path} (bloco {stage.part_index}/{stage.total_parts}):\n"
        )

    # Teto seguro de 1480 caracteres (margem de 20 chars para 1500)
    available_chars = max(300, 1480 - len(prefix))
    stage_snippet = stage.content[:available_chars]

    return f"{prefix}{stage_snippet}"

