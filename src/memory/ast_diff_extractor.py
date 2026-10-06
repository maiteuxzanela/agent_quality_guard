"""Extrator Cirúrgico de Símbolos AST a partir do Git Diff.

Mapeia alterações físicas do git diff para os nós sintáticos correspondentes
(funções, métodos, classes ou blocos de módulo) via árvore sintática Python (AST),
eliminando o fatiamento arbitrário por linhas e garantindo escopo completo para avaliação.
"""

import ast
import logging
import os
import re
import subprocess
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from .ast_cache import get_cached_ast

logger = logging.getLogger("openjev.ast_diff_extractor")

# NOTA (set/2026): "tests"/"test" NÃO entram neste ignore — alinhado ao
# IGNORE_DIR_PARTS do diff_inspector. A rubrica codigo_producao avalia
# "cobertura de testes reais": excluir o código de teste dos símbolos AST
# escondia essa evidência do score, do Screener e da telemetria de stages.
IGNORE_DIR_PARTS = {
    ".git", "__pycache__", ".venv", "venv", "node_modules",
    ".pytest_cache", ".mypy_cache", ".ruff_cache", "dist", "build",
    ".agents", "scripts", "bin", "docs",
}

IGNORE_EXTENSIONS = {
    ".pyc", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".svg",
    ".zip", ".tar", ".gz", ".lock", ".bin", ".wasm", ".pdf",
    ".md", ".markdown", ".txt", ".rst", ".json", ".yaml", ".yml", ".toml",
    ".sh", ".bash",
}


@dataclass
class AstSymbolSlice:
    """Representa um símbolo sintático completo extraído a partir de alterações no Git."""
    file_path: str
    symbol_name: str
    symbol_type: str  # 'function', 'async_function', 'class', 'module_block'
    start_line: int
    end_line: int
    content: str
    imports_header: str = ""
    changed_lines: Set[int] = field(default_factory=set)
    docstring: Optional[str] = None
    tokens_estimate: int = 0

    @property
    def full_context(self) -> str:
        """Retorna o código do símbolo precedido pelos imports pertinentes do arquivo."""
        if self.imports_header:
            return f"{self.imports_header}\n\n{self.content}"
        return self.content


def is_ignored_path(rel_path: str) -> bool:
    """Verifica se o caminho pertence a diretórios ou extensões ignoradas."""
    p = Path(rel_path)
    if p.suffix.lower() in IGNORE_EXTENSIONS:
        return True
    return any(part in IGNORE_DIR_PARTS for part in p.parts)


def get_git_modified_line_ranges(workspace_dir: str) -> Dict[str, Set[int]]:
    """Extrai os números exatos de linhas adicionadas/modificadas no git (staged, unstaged e untracked).

    Retorna um dicionário {caminho_relativo: conjunto_de_linhas_modificadas}.
    """
    ws = Path(workspace_dir).resolve()
    if not ws.is_dir():
        return {}

    modified_lines_map: Dict[str, Set[int]] = {}

    # 1. Staged e Unstaged diffs unificados
    try:
        cmd = ["git", "diff", "HEAD", "--unified=0"]
        res = subprocess.run(cmd, cwd=str(ws), capture_output=True, text=True, timeout=10)
        if res.returncode == 0 and res.stdout:
            current_file: Optional[str] = None
            for line in res.stdout.splitlines():
                if line.startswith("+++ b/"):
                    current_file = line[6:].strip()
                    continue

                if not (line.startswith("@@ ") and current_file):
                    continue

                if is_ignored_path(current_file):
                    continue

                m = re.search(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@", line)
                if not m:
                    continue

                start_line = int(m.group(1))
                count = int(m.group(2)) if m.group(2) else 1
                if current_file not in modified_lines_map:
                    modified_lines_map[current_file] = set()
                modified_lines_map[current_file].update(range(start_line, start_line + count))
    except Exception as e:
        logger.warning(f"Erro ao extrair diff de linhas modificadas no git: {e}")

    # 2. Untracked files (arquivos novos que não existem no HEAD)
    try:
        cmd_untracked = ["git", "ls-files", "--others", "--exclude-standard"]
        res_untracked = subprocess.run(cmd_untracked, cwd=str(ws), capture_output=True, text=True, timeout=10)
        if res_untracked.returncode == 0 and res_untracked.stdout:
            for rel_file in res_untracked.stdout.splitlines():
                rel_file = rel_file.strip()
                if not rel_file or is_ignored_path(rel_file):
                    continue
                file_full = ws / rel_file
                if file_full.is_file() and file_full.suffix == ".py":
                    try:
                        total_lines = len(file_full.read_text(encoding="utf-8", errors="replace").splitlines())
                        modified_lines_map[rel_file] = set(range(1, total_lines + 1))
                    except Exception as err:
                        logger.debug("Falha ao ler arquivo untracked %s: %s", rel_file, err)
    except Exception as e:
        logger.warning(f"Erro ao inspecionar arquivos untracked no git: {e}")

    return modified_lines_map


def extract_file_imports(tree: ast.AST, lines: List[str]) -> str:
    """Extrai os imports do nível de módulo para dar contexto sintático aos símbolos."""
    import_lines: List[str] = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            start = getattr(node, "lineno", 1) - 1
            end = getattr(node, "end_lineno", start + 1)
            import_lines.extend(lines[start:end])
    return "\n".join(import_lines).strip()


def _extract_class_symbol_slices(
    node: ast.ClassDef,
    rel_path: str,
    lines: List[str],
    changed_lines: Set[int],
    imports_header: str,
    start: int,
    end: int,
    node_lines: Set[int],
    intersect: Set[int],
) -> Tuple[List[AstSymbolSlice], Set[int]]:
    """Extrai cirurgicamente os métodos ou escopo de classe que interceptam o git diff."""
    slices: List[AstSymbolSlice] = []
    covered: Set[int] = set()
    methods_found = False

    for child in node.body:
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            c_start = getattr(child, "lineno", start)
            if hasattr(child, "decorator_list") and child.decorator_list:
                c_start = child.decorator_list[0].lineno
            c_end = getattr(child, "end_lineno", c_start)
            c_lines = set(range(c_start, c_end + 1))
            c_intersect = c_lines.intersection(changed_lines)
            if c_intersect:
                methods_found = True
                covered.update(c_lines)
                c_type = "async_method" if isinstance(child, ast.AsyncFunctionDef) else "method"
                c_text = "\n".join(lines[c_start - 1 : c_end])
                c_doc = ast.get_docstring(child)
                slices.append(
                    AstSymbolSlice(
                        file_path=rel_path,
                        symbol_name=f"{node.name}.{child.name}",
                        symbol_type=c_type,
                        start_line=c_start,
                        end_line=c_end,
                        content=c_text,
                        imports_header=imports_header,
                        changed_lines=c_intersect,
                        docstring=c_doc,
                        tokens_estimate=max(1, len(c_text) // 4),
                    )
                )

    if not methods_found:
        covered.update(node_lines)
        symbol_text = "\n".join(lines[start - 1 : end])
        docstring = ast.get_docstring(node)
        slices.append(
            AstSymbolSlice(
                file_path=rel_path,
                symbol_name=node.name,
                symbol_type="class",
                start_line=start,
                end_line=end,
                content=symbol_text,
                imports_header=imports_header,
                changed_lines=intersect,
                docstring=docstring,
                tokens_estimate=max(1, len(symbol_text) // 4),
            )
        )
        return slices, covered

    class_level_diff = intersect - covered
    if not class_level_diff:
        return slices, covered

    sorted_diff = sorted(class_level_diff)
    cb_start = sorted_diff[0]
    cb_end = sorted_diff[-1]
    covered.update(class_level_diff)
    raw_text = "\n".join(lines[cb_start - 1 : cb_end])
    dedented = textwrap.dedent(raw_text).strip()
    is_class_decl = dedented.startswith("class ") or dedented.startswith("@")
    class_doc = ast.get_docstring(node)
    doc_prefix = f'    """{class_doc}"""\n' if class_doc else ""
    candidate = dedented if is_class_decl else f"class {node.name}:\n{doc_prefix}" + textwrap.indent(dedented, "    ")

    try:
        ast.parse(candidate)
        valid_content = candidate
    except SyntaxError:
        valid_content = "\n".join(lines[start - 1 : end])
        cb_start = start
        cb_end = end

    slices.append(
        AstSymbolSlice(
            file_path=rel_path,
            symbol_name=f"{node.name}.class_scope",
            symbol_type="class_body",
            start_line=cb_start,
            end_line=cb_end,
            content=valid_content,
            imports_header=imports_header,
            changed_lines=class_level_diff,
            tokens_estimate=max(1, len(valid_content) // 4),
        )
    )

    return slices, covered


def _extract_assignment_target_name(node: ast.AST) -> str:
    """Extrai o identificador da variável ou constante de módulo de forma plana."""
    if isinstance(node, ast.Assign) and node.targets:
        first = node.targets[0]
        if isinstance(first, ast.Name):
            return first.id
    elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
        return node.target.id
    return "variavel_modulo"


def extract_ast_symbols_from_file(
    rel_path: str,
    full_path: Path,
    changed_lines: Set[int],
) -> List[AstSymbolSlice]:
    """Analisa a AST do arquivo e retorna os símbolos cujas linhas interceptam o git diff.

    Exemplo:
        >>> from pathlib import Path
        >>> symbols = extract_ast_symbols_from_file("foo.py", Path("tests/test_schemas.py"), {1, 2})
        >>> isinstance(symbols, list)
        True
    """
    if not full_path.is_file() or full_path.suffix != ".py":
        return []

    try:
        source_code = full_path.read_text(encoding="utf-8", errors="replace")
        lines = source_code.splitlines()
        tree = get_cached_ast(source_code, filename=str(full_path))
        if tree is None:
            return []
    except Exception as e:
        logger.debug(f"Falha ao parsear AST de {rel_path}: {e}")
        return []

    imports_header = extract_file_imports(tree, lines)
    slices: List[AstSymbolSlice] = []
    covered_lines: Set[int] = set()

    for node in tree.body:
        start = getattr(node, "lineno", 1)
        if hasattr(node, "decorator_list") and node.decorator_list:
            start = node.decorator_list[0].lineno
        end = getattr(node, "end_lineno", start)
        node_lines = set(range(start, end + 1))
        intersect = node_lines.intersection(changed_lines)

        if not intersect:
            continue

        if isinstance(node, ast.ClassDef):
            class_slices, class_covered = _extract_class_symbol_slices(
                node=node,
                rel_path=rel_path,
                lines=lines,
                changed_lines=changed_lines,
                imports_header=imports_header,
                start=start,
                end=end,
                node_lines=node_lines,
                intersect=intersect,
            )
            slices.extend(class_slices)
            covered_lines.update(class_covered)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            covered_lines.update(node_lines)
            stype = "async_function" if isinstance(node, ast.AsyncFunctionDef) else "function"
            symbol_text = "\n".join(lines[start - 1 : end])
            docstring = ast.get_docstring(node)
            est_tokens = max(1, len(symbol_text) // 4)

            slices.append(
                AstSymbolSlice(
                    file_path=rel_path,
                    symbol_name=node.name,
                    symbol_type=stype,
                    start_line=start,
                    end_line=end,
                    content=symbol_text,
                    imports_header=imports_header,
                    changed_lines=intersect,
                    docstring=docstring,
                    tokens_estimate=est_tokens,
                )
            )
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            # Extrai declarações completas de variáveis ou constantes de módulo
            target_name = _extract_assignment_target_name(node)

            symbol_text = "\n".join(lines[start - 1 : end])
            slices.append(
                AstSymbolSlice(
                    file_path=rel_path,
                    symbol_name=target_name,
                    symbol_type="module_assignment",
                    start_line=start,
                    end_line=end,
                    content=symbol_text,
                    imports_header=imports_header,
                    changed_lines=intersect,
                    tokens_estimate=max(1, len(symbol_text) // 4),
                )
            )

        elif isinstance(node, (ast.Expr, ast.With, ast.AsyncWith, ast.Try)):
            covered_lines.update(node_lines)
            symbol_text = "\n".join(lines[start - 1 : end])
            if symbol_text.strip():
                slices.append(
                    AstSymbolSlice(
                        file_path=rel_path,
                        symbol_name=f"modulo_stmt_L{start}",
                        symbol_type="module_statement",
                        start_line=start,
                        end_line=end,
                        content=symbol_text,
                        imports_header=imports_header,
                        changed_lines=intersect,
                        tokens_estimate=max(1, len(symbol_text) // 4),
                    )
                )

    return slices


def extract_workspace_ast_symbols(workspace_dir: str) -> List[AstSymbolSlice]:
    """Varre 100% das alterações do workspace e retorna todos os símbolos AST modificados."""
    ws = Path(workspace_dir).resolve()
    if not ws.is_dir():
        return []

    modified_lines_map = get_git_modified_line_ranges(str(ws))
    all_slices: List[AstSymbolSlice] = []

    for rel_path, changed_lines in sorted(modified_lines_map.items()):
        if is_ignored_path(rel_path):
            continue
        full_path = ws / rel_path
        file_slices = extract_ast_symbols_from_file(rel_path, full_path, changed_lines)
        all_slices.extend(file_slices)

    logger.info(
        f"Extraídos {len(all_slices)} símbolos AST a partir das alterações do workspace {ws.name}"
    )
    return all_slices
