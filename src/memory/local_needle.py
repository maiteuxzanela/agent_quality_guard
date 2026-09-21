"""Engine Local do Needle 3 (Cactus Needle On-Device).
Executa busca semântica, léxica e relacional 100% localmente no sistema de arquivos,
preservando integridade de nós AST, chamadores e restrição estrita de tokens.
"""

import ast
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..core.schemas import Needle3Query, Needle3Response, Needle3Snippet

logger = logging.getLogger("jev.needle.local")


class LocalNeedleEngine:
    """Motor de recuperação cirúrgica on-device / local para o ecossistema JEV.
    Dispensa dependência de APIs externas na nuvem, garantindo soberania e baixa latência.
    """

    def __init__(self, workspace_root: Optional[str] = None):
        self.workspace_root = Path(workspace_root or os.getcwd()).resolve()

    def tokenize(self, text: str) -> List[str]:
        """Tokenização simples para cálculo léxico/BM25."""
        return re.findall(r"\w+", text.lower())

    def _estimate_tokens(self, text: str) -> int:
        """Estimativa conservadora de tokens (aprox. 1 token a cada 3.8 caracteres)."""
        return max(1, math.ceil(len(text) / 3.8))

    def _extract_ast_blocks(self, file_path: Path) -> List[Dict[str, Any]]:
        """Extrai blocos de código com metadados relacionais de AST (Python)."""
        blocks = []
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            tree = ast.parse(content, filename=str(file_path))

            for node in ast.iter_child_nodes(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    start = node.lineno
                    end = getattr(node, "end_lineno", start + len(node.body))
                    block_code = "\n".join(lines[start - 1 : end])

                    relations = []
                    if isinstance(node, ast.ClassDef):
                        bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
                        relations.append(f"type:class | name:{node.name} | bases:{','.join(bases)}")
                    else:
                        relations.append(f"type:function | name:{node.name}")

                    # Detecta chamadas internas para enriquecer o grafo relacional
                    callees = set()
                    for sub in ast.walk(node):
                        if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name):
                            callees.add(sub.func.id)
                    if callees:
                        relations.append(f"calls:{','.join(list(callees)[:5])}")

                    blocks.append({
                        "file_path": str(file_path.relative_to(self.workspace_root)),
                        "start_line": start,
                        "end_line": end,
                        "content": block_code,
                        "relations": relations,
                        "name": getattr(node, "name", "block"),
                    })

            # Se não houver nós de nível superior identificados, indexa em bloco contínuo
            if not blocks and content.strip():
                blocks.append({
                    "file_path": str(file_path.relative_to(self.workspace_root)),
                    "start_line": 1,
                    "end_line": len(lines),
                    "content": content[:3000],
                    "relations": ["type:raw_file"],
                    "name": file_path.name,
                })

        except Exception as e:
            logger.debug(f"Não foi possível extrair AST completo de {file_path}: {e}")
        return blocks

    def _extract_js_blocks(self, file_path: Path) -> List[Dict[str, Any]]:
        """Extrai componentes, hooks e funções de arquivos JavaScript/TypeScript."""
        blocks = []
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            rel_path = str(file_path.relative_to(self.workspace_root))

            # Detecta declarações de funções, componentes React e hooks
            pattern = re.compile(
                r"^(?:export\s+)?(?:async\s+)?(?:function\s+([A-Za-z0-9_]+)|const\s+([A-Za-z0-9_]+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>)",
                re.MULTILINE,
            )

            matches = list(pattern.finditer(content))
            if not matches:
                # Se não houver declarações explícitas, quebra em blocos de 60 linhas
                chunk_size = 60
                for i in range(0, len(lines), chunk_size):
                    chunk_lines = lines[i : i + chunk_size]
                    blocks.append({
                        "file_path": rel_path,
                        "start_line": i + 1,
                        "end_line": min(i + chunk_size, len(lines)),
                        "content": "\n".join(chunk_lines),
                        "relations": [f"file:{file_path.name}"],
                        "name": file_path.name,
                    })
                return blocks

            for idx, match in enumerate(matches):
                func_name = match.group(1) or match.group(2) or "anonymous"
                start_pos = match.start()
                start_line = content[:start_pos].count("\n") + 1

                if idx + 1 < len(matches):
                    end_pos = matches[idx + 1].start()
                    end_line = content[:end_pos].count("\n")
                else:
                    end_line = len(lines)

                block_lines = lines[start_line - 1 : end_line]
                block_content = "\n".join(block_lines[:80])  # limita preview do bloco

                relations = [f"name:{func_name}"]
                if "useEffect" in block_content:
                    relations.append("hook:useEffect")
                if "useState" in block_content:
                    relations.append("hook:useState")
                if "fetch" in block_content or "api" in block_content.lower():
                    relations.append("has_api_call")

                blocks.append({
                    "file_path": rel_path,
                    "start_line": start_line,
                    "end_line": end_line,
                    "content": block_content,
                    "relations": relations,
                    "name": func_name,
                })

        except Exception as e:
            logger.debug(f"Falha ao extrair blocos JS de {file_path}: {e}")
        return blocks

    def _find_candidate_files(self, scope: Optional[List[str]] = None) -> List[Path]:
        """Localiza arquivos relevantes no escopo do projeto."""
        candidate_paths = []
        target_dirs = [self.workspace_root / p for p in scope] if scope else [self.workspace_root]

        ignored_dirs = {".git", ".venv", "venv", "__pycache__", "node_modules", ".pytest_cache", ".expo", "coverage", "android"}
        allowed_extensions = (
            ".py", ".js", ".jsx", ".ts", ".tsx", ".rules",
            ".md", ".json", ".yaml", ".yml", ".toml",
        )

        for tdir in target_dirs:
            if not tdir.exists():
                continue
            if tdir.is_file():
                candidate_paths.append(tdir)
                continue
            for root, dirs, files in os.walk(tdir):
                dirs[:] = [d for d in dirs if d not in ignored_dirs]
                for f in files:
                    if f.endswith(allowed_extensions):
                        candidate_paths.append(Path(root) / f)

        return candidate_paths

    async def search(self, query: Needle3Query) -> Needle3Response:
        """Executa busca relacional cirúrgica local com controle de token window."""
        start_time = time.perf_counter()

        query_tokens = set(self.tokenize(query.query))
        files = self._find_candidate_files(query.scope)

        all_blocks: List[Dict[str, Any]] = []
        for f in files:
            if f.suffix == ".py":
                all_blocks.extend(self._extract_ast_blocks(f))
            elif f.suffix in (".js", ".jsx", ".ts", ".tsx"):
                all_blocks.extend(self._extract_js_blocks(f))
            else:
                try:
                    content = f.read_text(encoding="utf-8", errors="replace")
                    lines = content.splitlines()
                    rel_path = str(f.relative_to(self.workspace_root))
                    # Quebra arquivos não-python em chunks lógicos
                    chunk_size = 50
                    for i in range(0, len(lines), chunk_size):
                        chunk_lines = lines[i : i + chunk_size]
                        all_blocks.append({
                            "file_path": rel_path,
                            "start_line": i + 1,
                            "end_line": min(i + chunk_size, len(lines)),
                            "content": "\n".join(chunk_lines),
                            "relations": [f"file:{f.name}"],
                            "name": f.name,
                        })
                except Exception:
                    pass

        # Pontua relevância dos blocos
        scored_blocks: List[Tuple[float, Dict[str, Any]]] = []
        for blk in all_blocks:
            text = f"{blk['name']} {' '.join(blk['relations'])} {blk['content']}"
            blk_tokens = self.tokenize(text)
            if not blk_tokens:
                continue

            # Interseção e ponderação de termos
            matches = query_tokens.intersection(set(blk_tokens))
            if not matches:
                continue

            score = sum(1.0 for m in matches) / (len(query_tokens) + 0.1)
            # Bônus se o nome do bloco/função/classe coincidir
            for q in query_tokens:
                if q in blk["name"].lower():
                    score += 0.5

            score = min(1.0, round(score, 4))
            scored_blocks.append((score, blk))

        # Ordena por relevância decrescente
        scored_blocks.sort(key=lambda x: x[0], reverse=True)

        selected_snippets: List[Needle3Snippet] = []
        current_tokens = 0

        for score, blk in scored_blocks:
            est_tokens = self._estimate_tokens(blk["content"])
            if current_tokens + est_tokens > query.max_tokens and selected_snippets:
                break

            selected_snippets.append(
                Needle3Snippet(
                    file_path=blk["file_path"],
                    start_line=blk["start_line"],
                    end_line=blk["end_line"],
                    content=blk["content"],
                    relevance_score=score,
                    relations=blk["relations"],
                )
            )
            current_tokens += est_tokens

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        return Needle3Response(
            snippets=selected_snippets,
            total_tokens=current_tokens,
            query_latency_ms=round(latency_ms, 2),
        )
