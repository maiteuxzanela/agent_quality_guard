"""Engine Local do Needle 3 (Cactus Needle On-Device).
Executa busca semântica, léxica e relacional 100% localmente no sistema de arquivos,
preservando integridade de nós AST, chamadores e restrição estrita de tokens.

O corpo pesado (walk + leitura + AST) roda em worker thread via `asyncio.to_thread`
para não bloquear o event loop do servidor MCP (cause of MCP -32001 timeouts).
"""

import ast
import asyncio
import logging
import math
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..core.schemas import Needle3Query, Needle3Response, Needle3Snippet
from .tree_sitter_guard import TreeSitterGuard

logger = logging.getLogger("jev.needle.local")

# Teto de arquivos candidatos por busca (defesa contra workspace de dezenas de GB).
# Ao atingir o teto, o walk para e a resposta indica truncamento — o chamador
# deve restringir `scope` em vez de varrer a árvore inteira.
MAX_CANDIDATE_FILES = 800

# TICKET-HARNESS-64 — Chunking adaptativo: um bloco lexical nunca excede
# MAX_LEXICAL_CHUNK_LINES linhas *nem* MAX_LEXICAL_CHUNK_CHARS caracteres.
# O teto de caracteres protege o orçamento de tokens contra payloads densos
# (JS minificado, JSON compacto em uma única linha) que chegariam como um
# bloco gigante e estourariam a janela cirúrgica.
MAX_LEXICAL_CHUNK_LINES = 50
MAX_LEXICAL_CHUNK_CHARS = 1800

# TICKET-HARNESS-64 — Stopwords bilíngues (PT-BR / EN) compartilhadas entre
# as duas rotas (léxica em qmd_search e simbólica aqui). Perguntas naturais
# carregam ruído gramatical ("como", "onde", "how", "does") que dilui o
# pattern léxico e desperdiça tokens. qmd_search importa este conjunto.
STOPWORDS_PT = frozenset(
    {
        "como", "onde", "qual", "quais", "quando", "quem", "para", "com",
        "por", "sem", "dos", "das", "nos", "nas", "num", "numa", "que",
        "uma", "uns", "umas", "este", "esta", "está", "isso", "isto",
        "aqui", "pelo", "pela", "pelos", "pelas", "mais", "menos", "muito",
        "sobre", "entre", "depois", "antes", "funciona", "fazer", "estao",
        "estão", "ser", "foi", "sao", "são", "tem", "nao", "não", "sim",
        "ele", "ela", "eles", "elas", "seu", "sua", "seus", "suas", "meu",
        "minha", "ao", "aos", "as", "os", "um", "de", "do", "da", "em",
        "no", "na", "se", "ou", "e", "o", "a",
    }
)

STOPWORDS_EN = frozenset(
    {
        "what", "where", "when", "which", "who", "whom", "whose", "why",
        "how", "does", "did", "done", "with", "from", "into", "onto",
        "upon", "about", "above", "below", "under", "over", "between",
        "through", "during", "before", "after", "this", "that", "these",
        "those", "have", "has", "had", "been", "being", "were", "was",
        "are", "the", "and", "for", "nor", "but", "yet", "can", "will",
        "just", "should", "would", "could", "now", "not", "only", "very",
        "some", "any", "all", "both", "each", "few", "more", "most",
        "other", "such", "than", "too", "same", "work", "works", "working",
        "use", "used", "using", "get", "gets", "got", "make", "makes",
        "made", "is", "in", "on", "at", "to", "of", "by", "as", "or",
        "if", "it", "be", "do", "an", "a",
    }
)

STOPWORDS = STOPWORDS_PT | STOPWORDS_EN

# Fronteiras camelCase / PascalCase e runs de dígitos:
# ``evaluateFailoverCooldown`` -> evaluate, Failover, Cooldown.
_CAMEL_PART_RE = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|\d+")


def substantive_terms(tokens: Any, min_length: int = 2) -> List[str]:
    """Retorna os termos substantivos com fallback seguro.

    Regra de fallback (TICKET-HARNESS-64): quando TODOS os tokens são
    stopwords (ex.: busca literal por ``"from to"``), os tokens originais são
    preservados para que o pattern nunca fique vazio. Tokens com menos de
    ``min_length`` caracteres e duplicatas são descartados.

    Exemplo:
        >>> substantive_terms(["Como", "funciona", "Needle"])
        ['needle']
    """
    raw: List[str] = []
    seen: Set[str] = set()
    for token in tokens:
        lowered = str(token).lower()
        if len(lowered) < min_length or lowered in seen:
            continue
        seen.add(lowered)
        raw.append(lowered)
    kept = [token for token in raw if token not in STOPWORDS]
    return kept or raw


# TICKET-HARNESS-41 — extração universal multi-linguagem via Tree-Sitter.
# Nomes canônicos do tree_sitter_language_pack (ex.: "csharp", não "c_sharp").
# Este mapa é EXCLUSIVO do extrator: o TreeSitterGuard mantém seu próprio
# LANGUAGE_BY_LEGACY (".cs" -> "c_sharp", indisponível) sem alteração.
NEEDLE_LANGUAGE_BY_EXTENSION: Dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".dart": "dart",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".java": "java",
    ".cs": "csharp",
}

# Extensões elegíveis para o caminho Tree-Sitter (fallback por família abaixo).
TS_EXTRACT_EXTENSIONS = frozenset(NEEDLE_LANGUAGE_BY_EXTENSION)
# Fallbacks graciosos quando o caminho Tree-Sitter falha/retorna vazio:
# Python -> stdlib ast; JS/TS -> regex legada; demais -> chunks léxicos.
PY_FALLBACK_EXTENSIONS = frozenset({".py", ".pyi"})
JS_FALLBACK_EXTENSIONS = frozenset({".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts"})
# Nomes de idioma do pack para a família JS/TS (enriquecimento hook/API).
_JS_FAMILY_LANGUAGES = frozenset({"javascript", "typescript", "tsx"})

# TICKET-HARNESS-64 — Código-fonte é o alvo primário da busca cirúrgica;
# docs/dados nunca podem consumir o teto de candidatos antes da implementação.
SOURCE_EXTENSIONS = frozenset(NEEDLE_LANGUAGE_BY_EXTENSION)
DOC_DATA_EXTENSIONS = frozenset({".rules", ".md", ".json", ".yaml", ".yml", ".toml"})
ALLOWED_EXTENSIONS = SOURCE_EXTENSIONS | DOC_DATA_EXTENSIONS

# Denylist de bundles e artefatos derivados (duplicam semântica com conteúdo
# minificado). ``panel/*.js`` são bundles de UI gerados.
IGNORED_ARTIFACT_SUFFIXES = (".min.js", ".min.mjs", ".bundle.js", ".map")


def is_ignored_artifact(file_path: Path, rel_path: str) -> bool:
    """Descarta bundles/artefatos derivados de forma lexical e determinística.

    Exemplo:
        >>> is_ignored_artifact(Path("app.min.js"), "app.min.js")
        True
    """
    name = file_path.name.lower()
    if name.endswith(IGNORED_ARTIFACT_SUFFIXES):
        return True
    parts = [part.lower() for part in Path(rel_path).parts]
    return "panel" in parts[:-1] and name.endswith(".js")

# Classificação de nós Tree-Sitter -> tipo relacional do Needle 3.
_TS_KIND_BY_NODE_TYPE: Dict[str, str] = {
    # Python (paridade de nível superior com o caminho stdlib ast)
    "class_definition": "class",
    "function_definition": "function",
    # JavaScript / TypeScript / TSX
    "class_declaration": "class",
    "abstract_class_declaration": "class",
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "method_definition": "method",
    "interface_declaration": "interface",
    # Go
    "method_declaration": "method",
    # Rust
    "struct_item": "struct",
    "trait_item": "interface",
    "function_item": "function",
    # Dart
    "function_signature": "function",
    "method_signature": "method",
    # Java
    "constructor_declaration": "method",
    "enum_declaration": "class",
    # Kotlin
    "object_declaration": "class",
    # C#
    "struct_declaration": "struct",
}

# Contenção de tipo (class/interface/struct/impl/trait) para promover
# funções descendentes a `type:method` no walk profundo.
_TS_CONTAINER_KINDS = frozenset({"class", "interface", "struct", "impl"})

# Teto de blocos/nós por arquivo (defesa de latência em arquivos patológicos).
MAX_TS_BLOCKS_PER_FILE = 300
MAX_TS_VISITED_NODES = 50_000

# TICKET-HARNESS-41 — gate do caminho Tree-Sitter (LEITURA LAZY, por-chamada).
# ⚠️ DESLIGADO POR PADRÃO: o binding C instalado (tree-sitter 0.26.0 +
# tree_sitter_language_pack 1.22.1) apresenta use-after-free intermitente
# (segfault não-catchable) ao fazer walk profundo de arquivos REAIS — medido
# 13/15 crashes em src/memory/js_ast_guard.js e ~50% em src/core/schemas.py,
# tanto via `.children` quanto via TreeCursor, independente de GC/parser.
# Ative com NEEDLE_TS_EXTRACT=1 quando uma versão corrigida do binding for
# instalada. A flag é lida POR CHAMADA (_ts_extract_enabled), nunca em
# import-time, para permitir testes A/B e paridade FastAPI/MCP.


def _ts_extract_enabled() -> bool:
    """Gate lazy do caminho Tree-Sitter (lê a env a cada chamada, não no import)."""
    return os.environ.get("NEEDLE_TS_EXTRACT", "0") == "1"


class LocalNeedleEngine:
    """Motor de recuperação cirúrgica on-device / local para o ecossistema JEV.
    Dispensa dependência de APIs externas na nuvem, garantindo soberania e baixa latência.
    """

    def __init__(self, workspace_root: Optional[str] = None):
        self.workspace_root = Path(workspace_root or os.getcwd()).resolve()
        self.candidates_truncated: bool = False
        self.candidates_found: int = 0
        # Registrado por _search_blocking: ident da thread que executou o walk
        # (deve ser != thread do event loop — ver test_search_does_not_block_event_loop).
        self._last_search_thread_ident: Optional[int] = None

    def tokenize(self, text: str) -> List[str]:
        """Tokeniza em palavras completas + sub-partes camel/snake.

        Além do token completo em lowercase, emite as sub-partes extraídas de
        transições camelCase / PascalCase e de separadores snake_case, para que
        ``evaluateFailoverCooldown`` produza
        ``['evaluatefailovercooldown', 'evaluate', 'failover', 'cooldown']``.
        A ordem é estável e duplicatas são removidas.

        Exemplo:
            >>> LocalNeedleEngine().tokenize("evaluateFailoverCooldown")
            ['evaluatefailovercooldown', 'evaluate', 'failover', 'cooldown']
        """
        tokens: List[str] = []
        seen: Set[str] = set()

        def emit(token: str) -> None:
            lowered = token.lower()
            if not lowered or lowered in seen:
                return
            seen.add(lowered)
            tokens.append(lowered)

        for word in re.findall(r"\w+", text):
            emit(word)
            # Snake_case / kebab-like separators (``\w`` preserva underscore).
            for part in re.split(r"_+", word):
                if not part:
                    continue
                pieces = _CAMEL_PART_RE.findall(part)
                if not pieces:
                    continue
                emit(part)
                for piece in pieces:
                    emit(piece)
        return tokens

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
                    # Inclui decorators: node.lineno aponta para o 'def'/'class',
                    # não para o primeiro decorator (off-by-one no start_line).
                    decorator_lines = [d.lineno for d in getattr(node, "decorator_list", [])]
                    start = min([node.lineno] + decorator_lines) if decorator_lines else node.lineno
                    end = getattr(node, "end_lineno", None)
                    if end is None:
                        body_ends = [
                            getattr(child, "end_lineno", None) or getattr(child, "lineno", start)
                            for child in ast.walk(node)
                        ]
                        end = max(body_ends) if body_ends else start
                    # Fatia inclusiva: lines é 0-based, end é 1-based (end_lineno).
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

            # Detecta declarações de funções, componentes React, classes e hooks (incluindo indentados)
            pattern = re.compile(
                r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s+([A-Za-z0-9_]+)|(?:const|let|var)\s+([A-Za-z0-9_]+)\s*=\s*(?:async\s*)?(?:function|\([^)]*\)|[A-Za-z0-9_]+|\(\))\s*=>?|class\s+([A-Za-z0-9_]+))",
                re.MULTILINE,
            )

            matches = list(pattern.finditer(content))
            if not matches:
                # Sem declarações explícitas: chunking adaptativo (linhas e chars).
                return self._chunk_lexical_blocks(file_path, lines)

            for idx, match in enumerate(matches):
                func_name = match.group(1) or match.group(2) or match.group(3) or "anonymous"
                start_pos = match.start()
                start_line = content[:start_pos].count("\n") + 1

                if idx + 1 < len(matches):
                    end_pos = matches[idx + 1].start()
                    # end_line 1-based inclusivo da última linha do bloco atual
                    end_line = max(start_line, content[:end_pos].count("\n"))
                else:
                    end_line = len(lines)

                block_lines = lines[start_line - 1 : end_line]
                # Preview com teto duplo (80 linhas / 1800 chars): nenhum
                # snippet pode furar o orçamento cirúrgico de tokens.
                preview_lines: List[str] = []
                preview_chars = 0
                for preview_line in block_lines:
                    projected = preview_chars + (1 if preview_lines else 0) + len(preview_line)
                    if preview_lines and (
                        len(preview_lines) >= 80
                        or projected > MAX_LEXICAL_CHUNK_CHARS
                    ):
                        break
                    preview_lines.append(preview_line)
                    preview_chars = projected
                block_content = "\n".join(preview_lines)[:MAX_LEXICAL_CHUNK_CHARS]
                # Metadado end_line coerente com o conteúdo efetivamente retornado
                reported_end = min(end_line, start_line + len(preview_lines) - 1)
                if reported_end < start_line:
                    reported_end = start_line

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
                    "end_line": reported_end,
                    "content": block_content,
                    "relations": relations,
                    "name": func_name,
                })

        except Exception as e:
            logger.debug(f"Falha ao extrair blocos JS de {file_path}: {e}")
        return blocks

    # ------------------------------------------------------------------
    # TICKET-HARNESS-41 — extração universal Tree-Sitter (Cactus Needle 3)
    # ------------------------------------------------------------------

    def _ts_language_for(self, file_path: Path) -> Optional[str]:
        """Resolve o nome canônico do grammar pack para a extensão do arquivo."""
        return NEEDLE_LANGUAGE_BY_EXTENSION.get(file_path.suffix.lower())

    def _ts_node_name(self, node: Any) -> Optional[str]:
        """Extrai o nome de um nó de declaração (campo `name` ou fallback por tipo)."""
        name_node = node.child_by_field_name("name")
        if name_node is not None and name_node.text:
            text = name_node.text.decode("utf-8", errors="replace").strip()
            if text:
                return text
        # Dart: method_signature/factory envolvem function_signature com o nome.
        for child in node.children:
            if child.type in (
                "function_signature",
                "constructor_signature",
                "factory_constructor_signature",
                "getter_signature",
                "setter_signature",
            ):
                nested = self._ts_node_name(child)
                if nested:
                    return nested
        # Fallback Kotlin/C-family: primeiro identificador direto nomeado.
        for child in node.children:
            if child.type in (
                "type_identifier",
                "simple_identifier",
                "field_identifier",
                "identifier",
            ) and child.is_named:
                text = child.text.decode("utf-8", errors="replace").strip()
                if text:
                    return text
        return None

    def _ts_block_kind(self, node: Any, in_type_container: bool, language: str) -> Optional[str]:
        """Classifica um nó como bloco Needle 3 (class/function/interface/struct/method)."""
        node_type = node.type
        # JS/TS: const f = () => ... — só o declarador com valor função vira bloco.
        if node_type == "variable_declarator":
            value = node.child_by_field_name("value")
            if value is not None and value.type in (
                "arrow_function",
                "function_expression",
                "generator_function",
            ):
                return "method" if in_type_container else "function"
            return None
        # Go: type X struct {...} / type X interface {...} — o type_spec decide.
        if node_type == "type_spec":
            type_node = node.child_by_field_name("type")
            if type_node is not None:
                if type_node.type == "struct_type":
                    return "struct"
                if type_node.type == "interface_type":
                    return "interface"
            return None
        kind = _TS_KIND_BY_NODE_TYPE.get(node_type)
        if kind is None:
            return None
        # Função dentro de escopo de tipo vira método (paridade Go/Rust/Dart/Java/Kotlin/C#).
        if kind == "function" and in_type_container:
            return "method"
        # Kotlin: interface parseia como class_declaration; o keyword decide.
        if node_type == "class_declaration" and language == "kotlin":
            first = node.children[0] if node.children else None
            if first is not None and not first.is_named and first.text == b"interface":
                return "interface"
        return kind

    def _ts_class_bases(self, node: Any, language: str) -> List[str]:
        """Bases/superclasse de um nó de classe (paridade com ast.Name do caminho stdlib)."""
        # Python: campo `superclasses` (argument_list); só identifiers diretos
        # (KeywordArgument fica aninhado — parity exata com `isinstance(b, ast.Name)`).
        sup = node.child_by_field_name("superclasses")
        if sup is not None:
            return [
                c.text.decode("utf-8", errors="replace")
                for c in sup.children
                if c.type == "identifier" and c.is_named
            ]
        # JS/TS: campo `superclass`.
        sup = node.child_by_field_name("superclass")
        if sup is not None and sup.text:
            text = sup.text.decode("utf-8", errors="replace").strip()
            return [text] if text else []
        # Go: embedded fields diretos em struct/interface.
        if language == "go":
            type_node = node.child_by_field_name("type")
            if type_node is not None and type_node.type in ("struct_type", "interface_type"):
                return [
                    c.text.decode("utf-8", errors="replace")
                    for c in type_node.children
                    if c.type == "type_identifier" and c.is_named
                ]
        return []

    def _ts_callees(self, node: Any) -> List[str]:
        """Callees identifier-only sob o nó (paridade com ast.Call+ast.Name, ordenado)."""
        callees: Set[str] = set()
        stack = [node]
        while stack:
            current = stack.pop()
            if current.type in ("call", "call_expression", "method_invocation"):
                fn = current.child_by_field_name("function") or current.child_by_field_name("name")
                if fn is not None and fn.type in ("identifier", "type_identifier") and fn.text:
                    callees.add(fn.text.decode("utf-8", errors="replace"))
            stack.extend(current.children)
        return sorted(callees)

    def _ts_build_block(
        self,
        node: Any,
        span_node: Any,
        kind: str,
        file_path: Path,
        rel_path: str,
        lines: List[str],
        language: str,
        max_content_lines: Optional[int],
    ) -> Optional[Dict[str, Any]]:
        """Monta o dict de bloco no MESMO schema do caminho stdlib (sem mutar contrato)."""
        name = self._ts_node_name(node)
        if not name:
            return None
        # Dart: assinatura sem corpo — o corpo é o sibling `function_body`.
        end_point = node.end_point
        if language == "dart":
            sibling = node.next_sibling
            if sibling is not None and sibling.type.endswith("function_body") and sibling.end_point:
                end_point = sibling.end_point
        start_line = span_node.start_point.row + 1
        # end_point é 1 byte após o último byte: col>0 => última linha = row+1.
        end_line = end_point.row + (1 if end_point.column > 0 else 0)
        if end_line < start_line:
            end_line = start_line
        block_lines = lines[start_line - 1 : end_line]
        if max_content_lines is not None and len(block_lines) > max_content_lines:
            block_lines = block_lines[:max_content_lines]
            end_line = start_line + len(block_lines) - 1
        content = "\n".join(block_lines)
        if not content.strip():
            return None

        relations: List[str] = []
        if kind == "class":
            bases = self._ts_class_bases(node, language)
            relations.append(f"type:class | name:{name} | bases:{','.join(bases)}")
        elif kind == "function":
            relations.append(f"type:function | name:{name}")
        else:
            relations.append(f"type:{kind} | name:{name}")

        callees = self._ts_callees(node)
        if callees:
            relations.append(f"calls:{','.join(callees[:5])}")

        # Enriquecimento de hooks/API preservado do caminho regex JS legado.
        if language in _JS_FAMILY_LANGUAGES:
            if "useEffect" in content:
                relations.append("hook:useEffect")
            if "useState" in content:
                relations.append("hook:useState")
            if "fetch" in content or "api" in content.lower():
                relations.append("has_api_call")

        return {
            "file_path": rel_path,
            "start_line": start_line,
            "end_line": end_line,
            "content": content,
            "relations": relations,
            "name": name,
        }

    def _extract_ts_blocks(self, file_path: Path) -> List[Dict[str, Any]]:
        """Extração universal multi-linguagem via Tree-Sitter (Cactus Needle 3).

        Suporta Python, JS/JSX/TS/TSX, Go, Rust, Dart, Java, Kotlin e C# com os
        mesmos identificadores relacionais do caminho stdlib (`type:class`,
        `type:function`, `bases:`, `calls:`). Python mantém paridade estrita de
        nível superior (sem métodos avulsos) com `_extract_ast_blocks`.
        Retorna [] em qualquer falha (gramática ausente, parse vazio, I/O) para
        que `_extract_blocks_for_file` degrade graciosamente sem exceção.
        """
        try:
            language = self._ts_language_for(file_path)
            if language is None:
                return []
            parser = TreeSitterGuard.get_parser(language)
            if parser is None:
                return []
            raw = file_path.read_bytes()
            tree = parser.parse(raw)
            if tree is None or tree.root_node is None:
                return []
            lines = raw.decode("utf-8", errors="replace").splitlines()
            rel_path = str(file_path.relative_to(self.workspace_root))
            blocks: List[Dict[str, Any]] = []

            if language == "python":
                # Paridade com ast.iter_child_nodes(tree): só nível de módulo,
                # com unwrap de decorated_definition para incluir decorators.
                for node in tree.root_node.children:
                    target = node
                    span = node
                    if node.type == "decorated_definition":
                        target = next(
                            (
                                c
                                for c in node.children
                                if c.type in ("class_definition", "function_definition")
                            ),
                            None,
                        )
                        if target is None:
                            continue
                    elif node.type not in ("class_definition", "function_definition"):
                        continue
                    # Literal inline (sem variável `kind`): evita que mypy
                    # herde "str" desta branch e reprove a reatribuição
                    # `str | None` do walk profundo (linha ~521).
                    blk = self._ts_build_block(
                        target,
                        span,
                        "class" if target.type == "class_definition" else "function",
                        file_path,
                        rel_path,
                        lines,
                        language,
                        None,
                    )
                    if blk is not None:
                        blocks.append(blk)
                return blocks

            # Walk profundo (demais linguagens): métodos/arrow-functions aninhados
            # são blocos próprios; containers de tipo promovem função -> method.
            max_content_lines = 120
            seen: Set[Tuple[str, int]] = set()
            stack: List[Tuple[Any, bool]] = [(tree.root_node, False)]
            visited = 0
            while stack:
                node, in_type = stack.pop()
                visited += 1
                if visited > MAX_TS_VISITED_NODES:
                    logger.debug(
                        "Needle3: teto de nós Tree-Sitter atingido em %s — walk truncado.",
                        rel_path,
                    )
                    break
                container = in_type
                kind = self._ts_block_kind(node, in_type, language)
                if kind is not None:
                    blk = self._ts_build_block(
                        node,
                        node,
                        kind,
                        file_path,
                        rel_path,
                        lines,
                        language,
                        max_content_lines,
                    )
                    if blk is not None and (blk["name"], blk["start_line"]) not in seen:
                        seen.add((blk["name"], blk["start_line"]))
                        blocks.append(blk)
                        if len(blocks) >= MAX_TS_BLOCKS_PER_FILE:
                            break
                    if kind in _TS_CONTAINER_KINDS:
                        container = True
                    elif kind in ("function", "method"):
                        # Função aninhada dentro de função volta a ser função.
                        container = False
                if node.type == "impl_item":
                    # impl Rust não vira bloco (evita duplicar o struct), só escopo.
                    container = True
                for child in reversed(node.children):
                    stack.append((child, container))
            return blocks
        except Exception as e:
            logger.debug(f"Tree-Sitter indisponível para {file_path}: {e}")
            return []

    def _lexical_blocks(self, file_path: Path) -> List[Dict[str, Any]]:
        """Fallback léxico: quebra arquivos sem AST em chunks adaptativos."""
        blocks: List[Dict[str, Any]] = []
        try:
            content = file_path.read_text(encoding="utf-8", errors="replace")
            lines = content.splitlines()
            blocks = self._chunk_lexical_blocks(file_path, lines)
        except Exception as e:
            logger.debug(f"Falha no fallback léxico de {file_path}: {e}")
        return blocks

    def _chunk_lexical_blocks(
        self, file_path: Path, lines: List[str]
    ) -> List[Dict[str, Any]]:
        """Fatia ``lines`` respeitando o teto duplo de linhas e caracteres.

        Um bloco é fechado quando a próxima linha ultrapassaria
        ``MAX_LEXICAL_CHUNK_LINES`` linhas ou ``MAX_LEXICAL_CHUNK_CHARS``
        caracteres. Uma linha isolada maior que o teto de caracteres (JS
        minificado / JSON compacto) é fatiada em janelas de no máximo
        ``MAX_LEXICAL_CHUNK_CHARS``, preservando o número da linha original.
        """
        blocks: List[Dict[str, Any]] = []
        try:
            rel_path = str(file_path.relative_to(self.workspace_root))
        except ValueError:
            rel_path = str(file_path)
        chunk: List[str] = []
        chunk_start = 1
        chunk_chars = 0

        def flush(end_line: int) -> None:
            nonlocal chunk, chunk_chars
            if not chunk:
                return
            blocks.append({
                "file_path": rel_path,
                "start_line": chunk_start,
                "end_line": max(chunk_start, end_line),
                "content": "\n".join(chunk),
                "relations": [f"file:{file_path.name}"],
                "name": file_path.name,
            })
            chunk = []
            chunk_chars = 0

        for index, line in enumerate(lines, start=1):
            if len(line) > MAX_LEXICAL_CHUNK_CHARS:
                # Payload denso em uma única linha: fecha o bloco pendente e
                # emite janelas de caracteres da mesma linha física.
                flush(index - 1)
                for offset in range(0, len(line), MAX_LEXICAL_CHUNK_CHARS):
                    blocks.append({
                        "file_path": rel_path,
                        "start_line": index,
                        "end_line": index,
                        "content": line[offset : offset + MAX_LEXICAL_CHUNK_CHARS],
                        "relations": [f"file:{file_path.name}"],
                        "name": file_path.name,
                    })
                continue

            projected_chars = chunk_chars + (1 if chunk else 0) + len(line)
            if chunk and (
                len(chunk) + 1 > MAX_LEXICAL_CHUNK_LINES
                or projected_chars > MAX_LEXICAL_CHUNK_CHARS
            ):
                flush(index - 1)
                projected_chars = len(line)

            if not chunk:
                chunk_start = index
            chunk.append(line)
            chunk_chars = projected_chars

        flush(len(lines))
        return blocks

    def _extract_blocks_for_file(self, file_path: Path) -> List[Dict[str, Any]]:
        """Despacha a extração com fallback gracioso por família (sem exceção).

        Prioridade por família:
        - Python (.py/.pyi): stdlib `ast` (memory-safe, primeira classe). NUNCA
          Tree-Sitter — ver _ts_extract_enabled() (bug de memória no binding C).
        - JS/TS/TSX: regex legada `_extract_js_blocks` (memory-safe) enquanto o
          gate TS estiver desligado; Tree-Sitter quando NEEDLE_TS_EXTRACT=1.
        - Go/Rust/Dart/Java/Kotlin/C#: Tree-Sitter quando o gate está ligado;
          chunks léxicos quando desligado (sem alternativa stdlib).
        - Fallback final: chunks léxicos de 50 linhas.
        """
        suffix = file_path.suffix.lower()
        # Python: sempre stdlib ast (ver _ts_extract_enabled() — bug no binding C).
        if suffix in PY_FALLBACK_EXTENSIONS:
            return self._extract_ast_blocks(file_path)
        if _ts_extract_enabled() and suffix in TS_EXTRACT_EXTENSIONS:
            blocks = self._extract_ts_blocks(file_path)
            if blocks:
                return blocks
        if suffix in JS_FALLBACK_EXTENSIONS:
            return self._extract_js_blocks(file_path)
        return self._lexical_blocks(file_path)

    def _find_candidate_files(self, scope: Optional[List[str]] = None) -> List[Path]:
        """Localiza arquivos relevantes no escopo do projeto (teto duplo de fila).

        Código-fonte e docs/dados são coletados em filas separadas: a fila
        primária (código) consome até ``MAX_CANDIDATE_FILES`` antes que
        qualquer documento/JSON entre no corte. Artefatos derivados
        (bundles, source maps, ``panel/*.js``) são descartados.
        """
        primary: List[Path] = []
        secondary: List[Path] = []
        self.candidates_truncated = False

        if scope:
            target_dirs: List[Path] = []
            for item in scope:
                candidate = Path(item).expanduser()
                if not candidate.is_absolute():
                    candidate = self.workspace_root / candidate
                target_dirs.append(candidate)
        else:
            target_dirs = [self.workspace_root]

        ignored_dirs = {
            ".git", ".venv", "venv", "__pycache__", "node_modules",
            ".pytest_cache", ".expo", "coverage", "android",
            "dist", "build", ".next", "out", "target", ".cache",
            ".mypy_cache", ".ruff_cache", ".tox", ".turbo",
            "vendor", "third_party", ".parcel-cache", ".agent-teams",
            "screenshots",
        }

        def relative(candidate: Path) -> str:
            try:
                return str(candidate.relative_to(self.workspace_root))
            except ValueError:
                return str(candidate)

        def collect(candidate: Path) -> bool:
            """Enfileira o candidato; retorna False quando o teto primário fecha."""
            if is_ignored_artifact(candidate, relative(candidate)):
                return True
            bucket = primary if candidate.suffix.lower() in SOURCE_EXTENSIONS else secondary
            if len(bucket) < MAX_CANDIDATE_FILES:
                bucket.append(candidate)
                return True
            self.candidates_truncated = True
            return bucket is not primary

        for tdir in target_dirs:
            if not tdir.exists():
                continue
            if tdir.is_file():
                collect(tdir)
                continue
            for root, dirs, files in os.walk(tdir):
                dirs[:] = sorted(d for d in dirs if d not in ignored_dirs)
                for f in sorted(files):
                    if not f.endswith(tuple(ALLOWED_EXTENSIONS)):
                        continue
                    if not collect(Path(root) / f):
                        # Quota de código esgotada: docs/dados não podem mais
                        # entrar no corte, então o walk pode parar.
                        logger.warning(
                            "Needle3: teto de %d candidatos atingido em %s — "
                            "restringa `scope` para busca cirúrgica.",
                            MAX_CANDIDATE_FILES,
                            tdir,
                        )
                        selected = primary[:MAX_CANDIDATE_FILES]
                        self.candidates_found = len(selected)
                        return selected

        selected = primary[:MAX_CANDIDATE_FILES]
        remaining = MAX_CANDIDATE_FILES - len(selected)
        if remaining > 0:
            selected.extend(secondary[:remaining])
        elif secondary:
            self.candidates_truncated = True

        self.candidates_found = len(selected)
        return selected

    async def search(self, query: Needle3Query) -> Needle3Response:
        """Executa a busca fora do event loop (worker thread do asyncio)."""
        return await asyncio.to_thread(self._search_blocking, query)

    def _search_blocking(self, query: Needle3Query) -> Needle3Response:
        """Corpo síncrono da busca relacional (roda em thread, não no event loop)."""
        start_time = time.perf_counter()
        self._last_search_thread_ident = threading.get_ident()

        # Stopwords bilíngues com fallback seguro (nunca query vazia).
        raw_query_tokens = self.tokenize(query.query)
        substantive = [t for t in raw_query_tokens if t not in STOPWORDS]
        query_tokens = set(substantive or raw_query_tokens)

        files = self._find_candidate_files(query.scope)

        all_blocks: List[Dict[str, Any]] = []
        for f in files:
            all_blocks.extend(self._extract_blocks_for_file(f))

        block_token_sets: List[Set[str]] = []
        for blk in all_blocks:
            text = f"{blk['name']} {' '.join(blk['relations'])} {blk['content']}"
            block_token_sets.append(set(self.tokenize(text)))

        # IDF intra-busca sobre o universo dos blocos candidatos:
        #   idf(t) = ln(1 + (N - df(t) + 0.5) / (df(t) + 0.5))
        # Termos onipresentes (``self``, ``return``) perdem peso; identificadores
        # raros dominam o score. N = len(all_blocks) (blocos candidatos da busca).
        total_blocks = len(all_blocks)
        idf_by_token: Dict[str, float] = {}
        for token in query_tokens:
            df = sum(1 for block_tokens in block_token_sets if token in block_tokens)
            idf_by_token[token] = math.log(
                1.0 + (total_blocks - df + 0.5) / (df + 0.5)
            )
        total_idf = sum(idf_by_token.values()) or 1.0

        # Pontua relevância dos blocos
        scored_blocks: List[Tuple[float, Dict[str, Any]]] = []
        for blk, blk_tokens in zip(all_blocks, block_token_sets):
            if not blk_tokens:
                continue

            matches = query_tokens.intersection(blk_tokens)
            if not matches:
                continue

            matched_idf = sum(idf_by_token[token] for token in matches)
            score = matched_idf / total_idf
            # Bônus se o nome do bloco/função/classe coincidir (inclui as
            # sub-partes camelCase/snake_case emitidas por tokenize).
            name_tokens = set(self.tokenize(blk["name"]))
            name_matches = query_tokens.intersection(name_tokens)
            if name_matches:
                score += 0.5 * (len(name_matches) / max(1, len(query_tokens)))

            score = min(1.0, round(score, 4))
            scored_blocks.append((score, blk))

        # Ordena por relevância decrescente. Desempate determinístico: o bloco
        # cujo NOME (função/classe) cobre mais termos da query vence — evita
        # que um fallback raw_file com muito texto empate com o símbolo real.
        def _name_hits(block: Dict[str, Any]) -> int:
            return len(query_tokens.intersection(set(self.tokenize(block["name"]))))

        scored_blocks.sort(key=lambda x: (x[0], _name_hits(x[1])), reverse=True)

        selected_snippets: List[Needle3Snippet] = []
        current_tokens = 0
        # Orçamento rígido: o primeiro snippet NÃO pode furar max_tokens.
        # Antes: `... > max_tokens and selected_snippets` deixava o 1º bloco
        # passar sempre (ex.: 2781 tok com max_tokens=800).
        for score, blk in scored_blocks:
            content = blk["content"]
            est_tokens = self._estimate_tokens(content)
            remaining = query.max_tokens - current_tokens

            if est_tokens > remaining:
                if not selected_snippets and remaining >= 40:
                    max_chars = max(40, int(remaining * 3.8))
                    content = content[:max_chars]
                    est_tokens = self._estimate_tokens(content)
                    if est_tokens > remaining:
                        content = content[: max(40, int(remaining * 3.8))]
                        est_tokens = self._estimate_tokens(content)
                else:
                    break

            if est_tokens > remaining:
                break

            selected_snippets.append(
                Needle3Snippet(
                    file_path=blk["file_path"],
                    start_line=blk["start_line"],
                    end_line=blk["end_line"],
                    content=content,
                    relevance_score=score,
                    relations=blk["relations"],
                )
            )
            current_tokens += est_tokens

            if current_tokens >= query.max_tokens:
                break

        latency_ms = (time.perf_counter() - start_time) * 1000.0

        if self.candidates_truncated:
            logger.warning(
                "Needle3: resultados truncados (%d candidatos, teto=%d).",
                self.candidates_found,
                MAX_CANDIDATE_FILES,
            )

        # Economia real: tokens selecionados vs. todos os blocos candidatos indexados.
        total_available_tokens = sum(
            self._estimate_tokens(b["content"]) for b in all_blocks
        )
        if total_available_tokens > 0:
            token_savings_pct = round(
                max(0.0, min(100.0, (1.0 - current_tokens / total_available_tokens) * 100.0)),
                2,
            )
        else:
            token_savings_pct = 0.0

        return Needle3Response(
            snippets=selected_snippets,
            total_tokens=current_tokens,
            query_latency_ms=round(latency_ms, 2),
            token_savings_pct=token_savings_pct,
        )
