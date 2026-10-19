"""QMD Search: Motor de Busca Híbrido Cirúrgico (Ripgrep + Needle 3 + RRF).

Combina busca léxica de altíssima velocidade (Grep/Ripgrep) com recuperação semântica
e relacional de AST (Needle 3) através de Reciprocal Rank Fusion (RRF) em CPU.

Refinamentos de Produção:
1. Fallback Assimétrico: degradação suave quando uma das fontes não encontra resultados.
2. Fusão Macro-Micro: une o ranqueamento de arquivos (macro) aos blocos AST e linhas exatas (micro).
3. Atualização em Tempo Real: o Grep ao vivo captura edições recentes imediatamente.
"""

import asyncio
import logging
import math
import os
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..core.schemas import Needle3Query, Needle3Snippet
from .local_needle import LocalNeedleEngine, substantive_terms

logger = logging.getLogger("jev.qmd")

# Preview literal é fragmento de contexto, nunca dump de arquivo: precisa
# caber no mesmo teto de caracteres do chunker adaptativo do Needle.
MAX_LITERAL_PREVIEW_CHARS = 1800

# Arquivos de código executável são o alvo usual da busca cirúrgica; docs e
# dados continuam indexados, mas não devem vencer o código que implementa o
# comportamento consultado.
SOURCE_CODE_EXTENSIONS = frozenset(
    {
        ".py", ".pyi", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts",
        ".cts", ".rs", ".go", ".dart", ".kt", ".kts", ".java", ".cs", ".rb",
        ".php", ".swift", ".c", ".h", ".cc", ".cpp", ".hpp", ".sh", ".bash",
    }
)
SOURCE_CODE_BOOST = 1.25
DOC_SCOPE_MARKERS = frozenset({"docs", "doc", "personas"})
DOC_SCOPE_EXTENSIONS = frozenset({".md", ".json"})

# Denylist de bundles e artefatos derivados. Eles duplicam a semântica do
# código-fonte com conteúdo minificado e poluem o recall léxico.
IGNORED_GLOBS = (
    "!**/.git/**",
    "!**/node_modules/**",
    "!**/__pycache__/**",
    "!**/.venv/**",
    "!**/venv/**",
    "!**/dist/**",
    "!**/build/**",
    "!**/target/**",
    "!**/.cache/**",
    "!**/panel/*.js",
    "!**/*.bundle.js",
    "!**/*.min.js",
    "!**/*.min.mjs",
    "!**/*.map",
    "!**/.turbo/**",
    "!**/.parcel-cache/**",
    "!**/.agent-teams/**",
    "!**/screenshots/**",
)

GREP_EXCLUDED_DIRS = (
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    "target", ".cache", ".parcel-cache", ".agent-teams", "screenshots",
)

# Arquivos densos repetem um identificador muitas vezes; truncar em 5 esconde
# a evidência conjuntiva (cobertura de vários termos distintos da query).
DEFAULT_MAX_MATCHES_PER_FILE = 20


def _escape_literal(text: str) -> str:
    """Escapa metacaracteres de regex sem escapar espaços.

    O ``re.escape`` também escapa o espaço, o que o motor Rust do ripgrep
    rejeita como sequência de escape inválida.
    """
    return re.sub(r"([\\.^$|?*+()\[\]{}])", r"\\\1", text)


def _scope_is_documentation(scope: Optional[List[str]]) -> bool:
    """Retorna True quando o chamador escopou explicitamente docs/dados."""
    if not scope:
        return False
    for item in scope:
        lowered = str(item).lower()
        if Path(lowered).suffix in DOC_SCOPE_EXTENSIONS:
            return True
        if {part for part in Path(lowered).parts} & DOC_SCOPE_MARKERS:
            return True
    return False


def _type_multiplier(file_path: str, documentation_scope: bool) -> float:
    """Multiplicador tipológico do RRF para um arquivo."""
    if documentation_scope:
        return 1.0
    if Path(file_path).suffix.lower() in SOURCE_CODE_EXTENSIONS:
        return SOURCE_CODE_BOOST
    return 1.0

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
        # Teto rígido de caracteres: preview literal nunca vira dump de arquivo.
        content_preview = content_preview[:MAX_LITERAL_PREVIEW_CHARS]
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

    def _resolve_scope(
        self, scope: Optional[List[str]]
    ) -> Tuple[List[str], List[str]]:
        """Resolve ``scope`` em alvos absolutos existentes + filtros basename.

        Entradas relativas são resolvidas contra ``self.workspace_root``,
        comportando-se de forma consistente com o motor de AST. Entradas
        *path-like* inexistentes retornam ``([], [])`` — a rota léxica devolve
        nada em vez de ampliar silenciosamente para o workspace inteiro.

        Compatibilidade: um basename puro sem separador de caminho (ex.:
        ``scope="alpha.py"``) continua sendo um filtro de nome de arquivo,
        aplicado como glob relativo ao workspace (nunca varredura de disco).
        """
        if not scope:
            return [str(self.workspace_root)], []
        targets: List[str] = []
        globs: List[str] = []
        for item in scope:
            raw = item.strip()
            if not raw:
                continue
            candidate = Path(raw).expanduser()
            if not candidate.is_absolute():
                candidate = self.workspace_root / candidate
            if candidate.exists():
                targets.append(str(candidate.resolve()))
                continue
            is_basename = "/" not in raw and "\\" not in raw
            if is_basename and Path(raw).suffix:
                globs.append(f"*{raw}*")
                continue
            # Escopo path-like inexistente: nada a buscar.
            return [], []
        if not targets and globs:
            targets.append(str(self.workspace_root))
        return targets, globs

    def _build_pattern(self, query: str) -> Tuple[str, List[str]]:
        """Constrói o regex OR único do ripgrep e os termos substantivos.

        Stopwords bilíngues são removidas para que o pattern seja guiado pelos
        termos substantivos da pergunta. Fallback seguro: se a query inteira for
        stopwords, os tokens originais são preservados e o pattern nunca fica
        vazio.
        """
        clean_q = query.strip()
        raw_words = [w for w in re.findall(r"[\w_]+", clean_q) if len(w) >= 2]
        terms = substantive_terms(raw_words, min_length=2)

        if not terms:
            return _escape_literal(clean_q), terms
        escaped_terms = [_escape_literal(term) for term in terms]
        if len(terms) == 1 and len(raw_words) == 1:
            return _escape_literal(clean_q), terms
        if len(terms) == 1:
            return escaped_terms[0], terms
        return "|".join([_escape_literal(clean_q)] + escaped_terms[:6]), terms

    def _run_ripgrep(
        self,
        query: str,
        scope: Optional[List[str]] = None,
        max_results_per_file: int = DEFAULT_MAX_MATCHES_PER_FILE,
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

        pattern, terms = self._build_pattern(query)
        targets, scope_globs = self._resolve_scope(scope)
        if not targets:
            return results_by_file

        cmd = [self.rg_binary or "grep", "-n", "-i"]
        if self.rg_binary:
            cmd.extend([
                "--no-messages",
                # -H: alvo explícito de arquivo único também prefixa o path,
                # preservando o contrato path:line:text do parser.
                "-H",
                "-e", pattern,
                "--max-count", str(max_results_per_file),
            ])
            for glob in IGNORED_GLOBS:
                cmd.extend(["--glob", glob])
            for glob in scope_globs:
                cmd.extend(["-g", glob])
            cmd.append("--")
            cmd.extend(targets)
        else:
            # Fallback para grep padrão se rg não estiver presente
            cmd.extend(["-E", "-r", "-I"])
            for directory in GREP_EXCLUDED_DIRS:
                cmd.extend(["--exclude-dir", directory])
            for glob in scope_globs:
                cmd.extend(["--include", glob])
            cmd.extend(["-e", pattern])
            cmd.extend(targets)

        all_texts: Dict[str, List[str]] = {}
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
                entry = results_by_file.setdefault(
                    rel_p,
                    {
                        "file": rel_p,
                        "match_count": 0,
                        "matches": [],
                        "distinct_terms": 0,
                        "term_coverage": 0.0,
                        "matched_terms": [],
                    },
                )
                entry["match_count"] += 1
                all_texts.setdefault(rel_p, []).append(matched_text)
                if len(entry["matches"]) < max_results_per_file:
                    entry["matches"].append({
                        "line": line_no,
                        "text": matched_text,
                    })

        except Exception as e:
            logger.debug(f"Erro ao executar busca léxica: {e}")

        # Cobertura conjuntiva: quantos termos DISTINTOS da query o arquivo
        # realmente contém. Um arquivo que repete um único keyword quente
        # perde para outro que cobre os dois termos da pergunta.
        for rel_p, entry in results_by_file.items():
            haystack = "\n".join(all_texts.get(rel_p, [])).lower()
            matched = [term for term in terms if term in haystack]
            entry["matched_terms"] = matched
            entry["distinct_terms"] = len(matched)
            entry["term_coverage"] = round(len(matched) / max(1, len(terms)), 4)

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
        # Grep: ordem conjuntiva — arquivos que cobrem mais termos DISTINTOS da
        # query vencem os que apenas repetem um keyword quente muitas vezes.
        query_terms = substantive_terms(re.findall(r"[\w_]+", query), min_length=2)
        multi_term_query = len(query_terms) > 1

        def lexical_sort_key(file_path: str) -> Tuple[int, int, float]:
            entry = grep_results[file_path]
            return (
                int(entry.get("distinct_terms", 1)),
                int(entry.get("match_count", 0)),
                float(entry.get("term_coverage", 0.0)),
            )

        sorted_grep_files = sorted(
            grep_results.keys(),
            key=lexical_sort_key,
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

        # 4. Fusão RRF (Reciprocal Rank Fusion) com multiplicador tipológico.
        # Código-fonte recebe +25% sobre docs/dados; a neutralidade de escopo
        # preserva 1.0x quando o chamador apontou explicitamente para docs.
        all_candidate_files = set(grep_rank.keys()) | set(needle_rank.keys())
        documentation_scope = _scope_is_documentation(scope)
        rrf_scores: Dict[str, float] = {}

        for f in all_candidate_files:
            score = 0.0
            if f in grep_rank:
                lexical_weight = weight_grep
                entry = grep_results.get(f, {})
                # Match isolado de termo único é evidência fraca quando a
                # query contém múltiplos termos distintos.
                if multi_term_query and int(entry.get("distinct_terms", 1)) <= 1:
                    lexical_weight *= 0.5
                score += lexical_weight / (k + grep_rank[f])
            if f in needle_rank:
                score += weight_needle / (k + needle_rank[f])
            score *= _type_multiplier(f, documentation_scope)
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
