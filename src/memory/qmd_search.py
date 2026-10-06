"""QMD Search: Motor de Busca Híbrido Cirúrgico (Ripgrep + Needle 3 + RRF).

Combina busca léxica de altíssima velocidade (Grep/Ripgrep) com recuperação semântica
e relacional de AST (Needle 3) através de Reciprocal Rank Fusion (RRF) em CPU.

Refinamentos de Produção:
1. Fallback Assimétrico: degradação suave quando uma das fontes não encontra resultados.
2. Fusão Macro-Micro: une o ranqueamento de arquivos (macro) aos blocos AST e linhas exatas (micro).
3. Atualização em Tempo Real: o Grep ao vivo captura edições recentes imediatamente.
"""

import asyncio
import json
import logging
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..core.schemas import Needle3Query, Needle3Response, Needle3Snippet
from .local_needle import LocalNeedleEngine

logger = logging.getLogger("jev.qmd")

# Candidatos para o binário do ripgrep (rg)
KNOWN_RG_PATHS = [
    str(Path.home() / ".local/node/lib/node_modules/@deepseek-ai/dsh/node_modules/@vscode/ripgrep-linux-x64/bin/rg"),
    str(Path.home() / ".local/share/antigravity-ide/resources/app/node_modules/@vscode/ripgrep/bin/rg"),
]


def find_rg_binary() -> Optional[str]:
    """Localiza o binário do ripgrep disponível no sistema.

    Exemplo:
        >>> p = find_rg_binary()
        >>> p is None or isinstance(p, str)
        True
    """
    for p in KNOWN_RG_PATHS:
        if os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    system_rg = shutil.which("rg")
    if system_rg and os.access(system_rg, os.X_OK):
        return system_rg
    return None


def _parse_grep_line(line: str, workspace_root: Path) -> Optional[Tuple[str, int, str]]:
    """Extrai arquivo relativo, linha e texto de uma linha de saída do grep/ripgrep.

    Exemplo:
        >>> res = _parse_grep_line("src/main.py:10:def foo():", Path("."))
        >>> res is not None and res[1] == 10
        True
    """
    parts = line.split(":", 2)
    if len(parts) < 3:
        return None
    raw_path, line_no_str, matched_text = parts[0], parts[1], parts[2]
    try:
        line_no = int(line_no_str)
    except ValueError as parse_err:
        logger.debug("Linha invalida na saida do grep: %s", parse_err)
        return None
    try:
        abs_p = Path(raw_path).resolve()
        rel_p = str(abs_p.relative_to(workspace_root))
    except (ValueError, RuntimeError) as path_err:
        logger.debug("Falha ao resolver caminho relativo: %s", path_err)
        rel_p = raw_path
    return rel_p, line_no, matched_text.strip()


class QmdHybridSearchEngine:
    """Motor Híbrido de Busca Cirúrgica (Needle 3 + Ripgrep + RRF)."""

    def __init__(self, workspace_root: Optional[str] = None):
        self.workspace_root = Path(workspace_root or os.getcwd()).resolve()
        self.needle_engine = LocalNeedleEngine(workspace_root=str(self.workspace_root))
        self.rg_binary = find_rg_binary()

    def _estimate_tokens(self, text: str) -> int:
        """Estima a contagem de tokens a partir do comprimento do texto.

        Exemplo:
            >>> QmdHybridSearchEngine()._estimate_tokens("exemplo de codigo") > 0
            True
        """
        return max(1, math.ceil(len(text) / 3.8))

    def _collect_needle_snippets_for_file(
        self,
        file_path: str,
        file_rrf: float,
        is_hybrid: bool,
        grep_matches: List[Dict[str, Any]],
        needle_snips: List[Needle3Snippet],
    ) -> List[Dict[str, Any]]:
        """Empacota snippets AST Needle enriquecidos com matches exatos do grep.

        Exemplo:
            >>> QmdHybridSearchEngine()._collect_needle_snippets_for_file("a.py", 0.5, False, [], [])
            []
        """
        results: List[Dict[str, Any]] = []
        for snip in needle_snips:
            in_range_matches = [
                m for m in grep_matches
                if snip.start_line <= m["line"] <= snip.end_line
            ]
            results.append({
                "file": file_path,
                "lines": f"{snip.start_line}-{snip.end_line}",
                "rrf_score": file_rrf,
                "relations": snip.relations,
                "exact_matches": in_range_matches or list(grep_matches[:2]),
                "content": snip.content,
                "source": "hybrid" if is_hybrid else "needle_semantic",
                "tokens": self._estimate_tokens(snip.content),
            })
        return results

    def _collect_grep_fallback_snippet(
        self,
        file_path: str,
        abs_path: Path,
        file_rrf: float,
        grep_matches: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Cria snippet de fallback para literais capturados pelo grep sem nó AST.

        Exemplo:
            >>> QmdHybridSearchEngine()._collect_grep_fallback_snippet("a.txt", Path("a.txt"), 0.5, [])
            None
        """
        if not grep_matches:
            return None
        lines_preview: List[str] = []
        first_line = grep_matches[0]["line"]
        start_l = max(1, first_line - 3)
        end_l = first_line + 5
        try:
            if abs_path.is_file():
                all_lines = abs_path.read_text(encoding="utf-8", errors="replace").splitlines()
                lines_preview = all_lines[start_l - 1 : min(end_l, len(all_lines))]
        except Exception as read_err:
            logger.debug("Falha ao ler preview de %s: %s", abs_path, read_err)

        content_preview = "\n".join(lines_preview) or "\n".join(m["text"] for m in grep_matches)
        return {
            "file": file_path,
            "lines": f"{start_l}-{end_l}",
            "rrf_score": file_rrf,
            "relations": [f"literal_matches:{len(grep_matches)}"],
            "exact_matches": grep_matches,
            "content": content_preview,
            "source": "grep_literal",
            "tokens": self._estimate_tokens(content_preview),
        }

    def _append_file_snippets(
        self,
        final_snippets: List[Dict[str, Any]],
        file_items: List[Dict[str, Any]],
        max_tokens: int,
        total_tokens: int,
    ) -> Tuple[int, bool]:
        """Adiciona itens de um arquivo à lista final respeitando limite de tokens.

        Exemplo:
            >>> out = []
            >>> t, stop = QmdHybridSearchEngine()._append_file_snippets(out, [], 100, 0)
            >>> t == 0 and not stop
            True
        """
        curr_tokens = total_tokens
        for item in file_items:
            snip_tokens = item.pop("tokens", 0)
            if curr_tokens + snip_tokens > max_tokens and final_snippets:
                return curr_tokens, True
            final_snippets.append(item)
            curr_tokens += snip_tokens
        return curr_tokens, False

    def _run_ripgrep(
        self,
        query: str,
        scope: Optional[List[str]] = None,
        max_results_per_file: int = 5,
        max_files: int = 40,
    ) -> Dict[str, Dict[str, Any]]:
        """Executa busca léxica via ripgrep e agrupa resultados por arquivo.

        Exemplo:
            >>> res = QmdHybridSearchEngine()._run_ripgrep("needle")
            >>> isinstance(res, dict)
            True
        """
        results_by_file: Dict[str, Dict[str, Any]] = {}
        if not query.strip():
            return results_by_file

        clean_q = query.strip()
        words = [re.escape(w) for w in re.findall(r"[\w_]+", clean_q) if len(w) >= 3]
        if len(words) > 1:
            pattern = "|".join([re.escape(clean_q)] + words[:6])
        else:
            pattern = re.escape(clean_q)

        cmd = [
            self.rg_binary or "grep",
            "-n",
            "-i",
        ]

        if self.rg_binary:
            cmd.extend([
                "-e", pattern,
                "--max-count", str(max_results_per_file),
                "--glob", "!**/.git/**",
                "--glob", "!**/node_modules/**",
                "--glob", "!**/__pycache__/**",
                "--glob", "!**/.agent-teams/**",
                "--glob", "!**/.venv/**",
                "--glob", "!**/venv/**",
            ])
            if scope:
                for sc in scope:
                    cmd.extend(["-g", f"*{sc.strip()}*"])
            cmd.extend(["--", str(self.workspace_root)])
        else:
            # Fallback para grep padrão se rg não estiver presente
            cmd.extend(["-E", "-r", "-I", "--exclude-dir=.git", "--exclude-dir=node_modules", "-e", pattern, str(self.workspace_root)])

        try:
            res = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=5.0,
                check=False,
            )
            for line in res.stdout.splitlines():
                parsed = _parse_grep_line(line, self.workspace_root)
                if not parsed:
                    continue
                rel_p, line_no, matched_text = parsed
                if rel_p not in results_by_file and len(results_by_file) >= max_files:
                    break
                entry = results_by_file.setdefault(rel_p, {"file": rel_p, "match_count": 0, "matches": []})
                entry["match_count"] += 1
                if len(entry["matches"]) < max_results_per_file:
                    entry["matches"].append({
                        "line": line_no,
                        "text": matched_text,
                    })

        except Exception as e:
            logger.debug(f"Erro ao executar busca léxica: {e}")

        return results_by_file

    async def search(
        self,
        query: str,
        scope: Optional[List[str]] = None,
        max_tokens: int = 1200,
        depth: int = 2,
        k: float = 60.0,
        weight_grep: float = 1.0,
        weight_needle: float = 1.2,
    ) -> Dict[str, Any]:
        """Executa a busca híbrida unificada com fusão RRF em CPU.

        Exemplo:
            >>> import asyncio
            >>> res = asyncio.run(QmdHybridSearchEngine().search(query="needle_engine"))
            >>> res["status"]
            'success'
        """
        t0 = time.perf_counter()

        # 1. Executa busca léxica via Ripgrep em thread
        grep_future = asyncio.to_thread(self._run_ripgrep, query, scope)

        # 2. Executa busca semântica/AST via Needle 3
        needle_q = Needle3Query(
            query=query,
            scope=scope,
            max_tokens=max_tokens * 2,  # Captura candidatos amplos para rankear
            relational_depth=depth,
            preserve_relational_integrity=True,
        )
        needle_task = asyncio.create_task(self.needle_engine.search(needle_q))

        grep_results, needle_resp = await asyncio.gather(grep_future, needle_task)

        # 3. Ranqueamento de Arquivos
        # Grep: arquivos com mais matches recebem rank mais alto
        sorted_grep_files = sorted(
            grep_results.keys(),
            key=lambda f: grep_results[f]["match_count"],
            reverse=True,
        )
        grep_rank: Dict[str, int] = {f: idx + 1 for idx, f in enumerate(sorted_grep_files)}

        # Needle: agrupa snippets por arquivo e extrai maior score por arquivo
        needle_snippets_by_file: Dict[str, List[Needle3Snippet]] = {}
        for snip in needle_resp.snippets:
            needle_snippets_by_file.setdefault(snip.file_path, []).append(snip)

        sorted_needle_files = sorted(
            needle_snippets_by_file.keys(),
            key=lambda f: max(s.relevance_score for s in needle_snippets_by_file[f]),
            reverse=True,
        )
        needle_rank: Dict[str, int] = {f: idx + 1 for idx, f in enumerate(sorted_needle_files)}

        # 4. Fusão RRF (Reciprocal Rank Fusion)
        all_candidate_files = set(grep_rank.keys()) | set(needle_rank.keys())
        rrf_scores: Dict[str, float] = {}

        for f in all_candidate_files:
            score = 0.0
            if f in grep_rank:
                score += weight_grep / (k + grep_rank[f])
            if f in needle_rank:
                score += weight_needle / (k + needle_rank[f])
            rrf_scores[f] = score

        # Refinamento 1: Fallback Assimétrico
        ranked_files = sorted(
            all_candidate_files,
            key=lambda f: rrf_scores[f],
            reverse=True,
        )

        # 5. Refinamento 2: Fusão Macro-Micro e Empacotamento de Snippets
        final_snippets: List[Dict[str, Any]] = []
        total_tokens = 0
        raw_bytes_evaluated = 0

        for f in ranked_files:
            file_rrf = round(rrf_scores[f], 5)
            grep_matches = grep_results.get(f, {}).get("matches", [])
            needle_snips = needle_snippets_by_file.get(f, [])

            abs_path = self.workspace_root / f
            if abs_path.is_file():
                try:
                    raw_bytes_evaluated += abs_path.stat().st_size
                except Exception as stat_err:
                    logger.debug("Falha ao ler tamanho de %s: %s", abs_path, stat_err)

            if needle_snips:
                file_items = self._collect_needle_snippets_for_file(
                    file_path=f,
                    file_rrf=file_rrf,
                    is_hybrid=(f in grep_rank),
                    grep_matches=grep_matches,
                    needle_snips=needle_snips,
                )
            else:
                fallback_item = self._collect_grep_fallback_snippet(
                    file_path=f,
                    abs_path=abs_path,
                    file_rrf=file_rrf,
                    grep_matches=grep_matches,
                )
                file_items = [fallback_item] if fallback_item else []

            total_tokens, should_stop = self._append_file_snippets(
                final_snippets=final_snippets,
                file_items=file_items,
                max_tokens=max_tokens,
                total_tokens=total_tokens,
            )
            if should_stop or total_tokens >= max_tokens:
                break

        total_latency_ms = (time.perf_counter() - t0) * 1000.0
        est_raw_tokens = max(1, math.ceil(raw_bytes_evaluated / 3.8)) if raw_bytes_evaluated else total_tokens
        savings_pct = max(0.0, round((1.0 - (total_tokens / max(est_raw_tokens, total_tokens, 1))) * 100.0, 2))

        return {
            "status": "success",
            "latency_ms": round(total_latency_ms, 2),
            "total_tokens": total_tokens,
            "token_savings_pct": savings_pct,
            "snippets_count": len(final_snippets),
            "files_found": len(ranked_files),
            "snippets": final_snippets,
        }
