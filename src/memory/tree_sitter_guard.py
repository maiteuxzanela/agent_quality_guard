"""Needle 3 Tree-Sitter Guard — Camada 2 (Meta-Regras Universais de AST).

Analisa código real via Árvore Sintática Abstrata em múltiplas linguagens
(Python, TypeScript, JavaScript, TSX, Java, Go, Rust, Ruby, PHP, Kotlin,
Swift, C/C++, Bash) usando ``tree_sitter`` + ``tree_sitter_language_pack``.

Meta-regras universais implementadas:
  1. ``PROD_MOCK`` — import/invocação de frameworks de mock, spy/stub ou
     classes manuais de mock sob diretórios de produção (src/, lib/, app/...).
  2. ``FLOATING_PROMISE`` / ``ASYNC_RETURN_NO_AWAIT`` — promessa órfã
     (chamada assíncrona sem ``await``, ``.then``/``.catch``) e retorno de
     chamada assíncrona sem ``await`` dentro de ``try/catch``.
  3. ``RESOURCE_LEAK`` — ``addEventListener``/``setInterval``/``setTimeout``
     sem cleanup correspondente; ``open()`` Python fora de context manager.
  4. ``SILENT_CATCH`` — ``catch (e) {}`` vazio ou ``except Exception: pass``.
  5. ``SQL_INJECTION`` / ``DANGEROUS_EVAL`` / ``SECURITY_EXEC`` /
     ``COMMAND_INJECTION`` — concatenação/template em SQL, ``eval()``,
     ``new Function()`` e ``child_process.exec(input)`` com dado dinâmico.

Regras de governança:
  * Nunca lança exceção por gramática ausente: cai para varredura léxica.
  * Respeita ``needle-ignore``, ``needle-ignore-next-line``,
    ``needle-ignore-file`` e ``needle-ignore-all``.
  * Não usa mocks, rede ou GPU: 100% determinístico.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple, Union

logger = logging.getLogger("jev.needle.tree_sitter_guard")

try:  # pragma: no cover - depende do ambiente
    from tree_sitter import Node  # type: ignore

    _TREE_SITTER_AVAILABLE = True
except Exception as exc:  # pragma: no cover - fallback gracioso
    Node = Any  # type: ignore
    _TREE_SITTER_AVAILABLE = False
    logger.debug("tree_sitter indisponível: %s", exc)

try:  # pragma: no cover - depende do ambiente
    import tree_sitter_language_pack as _language_pack
except Exception as exc:  # pragma: no cover - fallback gracioso
    _language_pack = None  # type: ignore
    logger.debug("tree_sitter_language_pack indisponível: %s", exc)


SQL_KEYWORDS = re.compile(
    r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|DROP\s+TABLE|ALTER\s+TABLE|CREATE\s+TABLE)\b",
    re.IGNORECASE,
)

MOCK_MODULES = (
    "unittest.mock",
    "pytest",
    "faker",
    "responses",
    "httpretty",
    "freezegun",
    "jest",
    "@jest/globals",
    "vitest",
    "sinon",
    "@sinonjs/fake-timers",
    "mocha",
    "chai",
    "testdouble",
)

MOCK_SYMBOLS = {
    "MagicMock",
    "Mock",
    "AsyncMock",
    "NonCallableMock",
    "PropertyMock",
    "patch",
    "create_autospec",
}

MOCK_CALL_PATTERNS = (
    re.compile(r"^(?:jest|vi)\.(?:mock|fn|spyOn)$"),
    re.compile(r"^sinon\.(?:stub|spy|mock|fake|useFakeTimers)$"),
    re.compile(r"^td\.(?:function|object|replace|constructor)$"),
)

EVAL_NAMES = {"eval", "builtins.eval", "window.eval", "global.eval", "globalThis.eval"}
EXEC_NAMES = {"exec", "builtins.exec"}

LISTENER_ADD_METHODS = {"addEventListener", "on", "once", "subscribe"}
LISTENER_CLEANUP_METHODS = {
    "removeEventListener",
    "off",
    "unsubscribe",
    "removeListener",
    "removeAllListeners",
    "dispose",
    "destroy",
}
TIMER_ADD_METHODS = {"setInterval", "setTimeout", "requestAnimationFrame"}
TIMER_CLEANUP_METHODS = {"clearInterval", "clearTimeout", "cancelAnimationFrame"}

JS_CLIENT_OBJECTS = {
    "axios",
    "api",
    "client",
    "http",
    "https",
    "db",
    "prisma",
    "supabase",
    "firebase",
    "firestore",
    "storage",
    "request",
}
PROMISE_METHOD_RE = re.compile(
    r"^(?:get|post|put|patch|delete|query|execute|insert|update|upsert|save|send|"
    r"request|upload|download|read|write|find|fetch|login|logout|signup|authenticate|verify)(?:Async|AsyncAll)?$"
)

JS_FAMILY = {"javascript", "typescript", "tsx"}

IGNORED_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "env",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
    "coverage",
    ".expo",
    ".next",
    ".nuxt",
    "vendor",
    "site-packages",
}

NON_PRODUCTION_DIRS = {
    "tests",
    "test",
    "__tests__",
    "__mocks__",
    "mocks",
    "spec",
    "specs",
    "e2e",
    "examples",
    "example",
    "docs",
    "doc",
    "benchmarks",
    "benchmark",
    "fixtures",
    "scripts",
    "tools",
    "node_modules",
    "dist",
    "build",
    "coverage",
    "vendor",
    ".venv",
    "venv",
    "__pycache__",
}

TEST_DIR_PARTS = {
    "tests",
    "test",
    "__tests__",
    "__mocks__",
    "mocks",
    "spec",
    "specs",
    "e2e",
    "__snapshots__",
}

MAX_FILE_BYTES = 2_000_000

_PARSER_CACHE: Dict[str, Any] = {}
_UNAVAILABLE_LANGUAGES: Set[str] = set()
_PARSE_LOCK = threading.Lock()


@dataclass
class TreeSitterViolation:
    """Violação estrutural detectada por análise sintática multi-linguagem.

    Exemplo:
        >>> v = TreeSitterViolation("SQL_INJECTION", "security", "a.js", 1, "m")
        >>> v.to_dict()["rule_id"]
        'SQL_INJECTION'
    """

    rule_id: str
    category: str
    file_path: str
    line_number: int
    message: str
    code_snippet: str = ""
    snippet: str = ""
    suggestion: Optional[str] = None
    language: str = ""

    def __post_init__(self) -> None:
        if not self.code_snippet and self.snippet:
            self.code_snippet = self.snippet
        elif not self.snippet and self.code_snippet:
            self.snippet = self.code_snippet

    @property
    def severity(self) -> str:
        """Classifica a violação como bloqueante ou alerta (contrato do runner)."""
        return "blocking" if TreeSitterGuard.is_blocking_rule(self.rule_id) else "warning"

    def to_dict(self) -> Dict[str, Any]:
        """Serializa a violação preservando compatibilidade com ASTViolation.

        Exemplo:
            >>> TreeSitterViolation("R", "security", "a.py", 1, "m").to_dict()["line_number"]
            1
        """
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "code_snippet": self.code_snippet,
            "snippet": self.snippet,
            "message": self.message,
            "suggestion": self.suggestion,
            "language": self.language,
            "severity": self.severity,
        }


class TreeSitterGuard:
    """Guardião determinístico da Camada 2: meta-regras universais de AST."""

    LANGUAGE_BY_EXTENSION: Dict[str, str] = {
        ".py": "python",
        ".pyi": "python",
        ".js": "javascript",
        ".jsx": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".mts": "typescript",
        ".cts": "typescript",
        ".tsx": "tsx",
        ".java": "java",
        ".go": "go",
        ".rs": "rust",
        ".rb": "ruby",
        ".php": "php",
        ".kt": "kotlin",
        ".kts": "kotlin",
        ".swift": "swift",
        ".c": "c",
        ".h": "c",
        ".cc": "cpp",
        ".cpp": "cpp",
        ".cxx": "cpp",
        ".hpp": "cpp",
        ".hh": "cpp",
        ".sh": "bash",
        ".bash": "bash",
        ".cs": "c_sharp",
    }

    RULE_CATEGORIES: Dict[str, str] = {
        "PROD_MOCK": "anti_mock_producao",
        "FLOATING_PROMISE": "structural",
        "ASYNC_RETURN_NO_AWAIT": "structural",
        "RESOURCE_LEAK": "structural",
        "SILENT_CATCH": "structural",
        "SQL_INJECTION": "security",
        "DANGEROUS_EVAL": "security",
        "SECURITY_EXEC": "security",
        "COMMAND_INJECTION": "security",
    }

    BLOCKING_RULES = {
        "PROD_MOCK",
        "SQL_INJECTION",
        "DANGEROUS_EVAL",
        "SECURITY_EXEC",
        "COMMAND_INJECTION",
    }

    RULE_MESSAGES: Dict[str, str] = {
        "PROD_MOCK": "Mock, spy, stub ou framework de teste detectado em código de produção.",
        "FLOATING_PROMISE": "Chamada assíncrona sem await, .then() ou .catch(): promessa órfã (floating promise).",
        "ASYNC_RETURN_NO_AWAIT": "Retorno de chamada assíncrona sem await dentro de try/catch.",
        "RESOURCE_LEAK": "Recurso/listener registrado sem cleanup correspondente (vazamento de recursos).",
        "SILENT_CATCH": "Bloco de exceção vazio engolindo erros silenciosamente.",
        "SQL_INJECTION": "Concatenação/interpolação de dado dinâmico em query SQL (SQL Injection).",
        "DANGEROUS_EVAL": "Uso de eval()/exec() com execução dinâmica de código.",
        "SECURITY_EXEC": "Construção dinâmica de função (new Function) proibida.",
        "COMMAND_INJECTION": "Execução de comando de shell com argumento dinâmico (child_process).",
    }

    RULE_SUGGESTIONS: Dict[str, str] = {
        "PROD_MOCK": (
            "Remova mocks/spies/stubs do código de produção. Mocks pertencem a tests/, "
            "__tests__/, __mocks__/ ou *.test.*/*.spec.*."
        ),
        "FLOATING_PROMISE": (
            "Aguarde a promessa com await ou trate a rejeição com .catch()/.then(undefined, handler)."
        ),
        "ASYNC_RETURN_NO_AWAIT": (
            "Use 'return await chamada()' dentro do try/catch para que a rejeição seja capturada pelo bloco."
        ),
        "RESOURCE_LEAK": (
            "Pare o timer/listener no teardown (clearInterval/removeEventListener/unsubscribe) "
            "ou use context manager (with open(...))."
        ),
        "SILENT_CATCH": (
            "Registre o erro (logging.exception/console.error), re-lance com raise/throw ou trate explicitamente."
        ),
        "SQL_INJECTION": (
            "Utilize queries parametrizadas com placeholders (?, %s, :param) ou prepared statements; "
            "nunca interpole variáveis na string SQL."
        ),
        "DANGEROUS_EVAL": (
            "Remova eval()/exec(). Use ast.literal_eval(), JSON.parse() ou despacho por dicionário."
        ),
        "SECURITY_EXEC": "Substitua new Function() por funções declaradas ou parser formal.",
        "COMMAND_INJECTION": (
            "Passe argumentos como lista (execFile/spawn) e valide o input; nunca use shell com string dinâmica."
        ),
    }

    # Equivalências entre regras da Camada 2 (Tree-Sitter) e da Camada 1
    # (Node AST / léxico), usadas para que uma diretiva ``needle-ignore`` de
    # qualquer uma das camadas suprima a regra equivalente da outra.
    RULE_ALIASES: Dict[str, Set[str]] = {
        "DANGEROUS_EVAL": {"SECURITY_EVAL"},
        "SECURITY_EVAL": {"DANGEROUS_EVAL"},
        "PROD_MOCK": {"JS_PROD_MOCK"},
        "JS_PROD_MOCK": {"PROD_MOCK"},
        "SILENT_CATCH": {"EMPTY_CATCH"},
        "EMPTY_CATCH": {"SILENT_CATCH"},
        "SQL_INJECTION": {"SQL_INJECTION"},
        "SECURITY_EXEC": {"SECURITY_EXEC"},
        "COMMAND_INJECTION": {"COMMAND_INJECTION"},
    }

    # ── Infraestrutura de parsing ─────────────────────────────────────────

    @classmethod
    def is_blocking_rule(cls, rule_id: str) -> bool:
        """Indica se a regra bloqueia a entrega (segurança/anti-mock).

        Exemplo:
            >>> TreeSitterGuard.is_blocking_rule("SQL_INJECTION")
            True
        """
        return rule_id in cls.BLOCKING_RULES

    @classmethod
    def language_for_file(cls, file_path: Union[str, Path]) -> Optional[str]:
        """Resolve a gramática a partir da extensão do arquivo.

        Exemplo:
            >>> TreeSitterGuard.language_for_file("src/app.tsx")
            'tsx'
        """
        suffix = Path(str(file_path)).suffix.lower()
        language = cls.LANGUAGE_BY_EXTENSION.get(suffix)
        if language:
            return language
        if _language_pack is not None:
            try:
                detected = _language_pack.detect_language_from_path(str(file_path))
                return detected or None
            except Exception as exc:  # pragma: no cover - API opcional do pack
                logger.debug("Falha ao detectar linguagem por path (%s): %s", file_path, exc)
        return None

    @classmethod
    def supports_file(cls, file_path: Union[str, Path]) -> bool:
        """True quando a extensão do arquivo possui gramática mapeada.

        Exemplo:
            >>> TreeSitterGuard.supports_file("a.rb")
            True
        """
        return cls.language_for_file(file_path) is not None

    @classmethod
    def is_language_available(cls, language: str) -> bool:
        """True quando a gramática pode ser carregada pelo language pack.

        Exemplo:
            >>> isinstance(TreeSitterGuard.is_language_available("python"), bool)
            True
        """
        return cls.get_parser(language) is not None

    @classmethod
    def get_parser(cls, language: Optional[str]) -> Any:
        """Obtém (e cacheia) o parser da linguagem; None se a gramática faltar.

        Exemplo:
            >>> TreeSitterGuard.get_parser("__inexistente__") is None
            True
        """
        if not language or _language_pack is None or not _TREE_SITTER_AVAILABLE:
            return None
        if language in _PARSER_CACHE:
            return _PARSER_CACHE[language]
        if language in _UNAVAILABLE_LANGUAGES:
            return None
        try:
            parser = _language_pack.get_parser(language)
        except Exception as exc:
            logger.debug("Gramática indisponível para '%s': %s", language, exc)
            _UNAVAILABLE_LANGUAGES.add(language)
            _PARSER_CACHE[language] = None
            return None
        _PARSER_CACHE[language] = parser
        return parser

    @classmethod
    def available_languages(cls) -> List[str]:
        """Lista as linguagens mapeadas cujo parser carrega neste ambiente.

        Exemplo:
            >>> "python" in TreeSitterGuard.available_languages()
            True
        """
        loaded: List[str] = []
        for language in sorted(set(cls.LANGUAGE_BY_EXTENSION.values())):
            if cls.get_parser(language) is not None:
                loaded.append(language)
        return loaded

    @staticmethod
    def _node_text(node: Any, source: bytes) -> str:
        """Extrai o texto-fonte exato coberto pelo nó da AST."""
        if node is None:
            return ""
        try:
            return source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")
        except Exception:
            return ""

    @staticmethod
    def _walk(node: Any) -> Iterable[Any]:
        """Percorre a árvore sintática em profundidade sem recursão excessiva."""
        stack = [node]
        while stack:
            current = stack.pop()
            yield current
            stack.extend(reversed(getattr(current, "children", [])))

    @staticmethod
    def _children_without_comments(node: Any) -> List[Any]:
        """Filhos nomeados ignorando comentários (nós extra da gramática)."""
        return [
            child
            for child in getattr(node, "named_children", [])
            if child.type not in ("comment", "line_comment", "block_comment")
        ]

    @staticmethod
    def _has_token(node: Any, token: str) -> bool:
        """Verifica a presença de token anônimo (ex.: 'async') entre os filhos."""
        return any(getattr(child, "type", "") == token for child in getattr(node, "children", []))

    @classmethod
    def _callable_name(cls, call_node: Any, source: bytes) -> str:
        """Nome textual do alvo de uma chamada (identificador ou member access)."""
        function_node = call_node.child_by_field_name("function")
        if function_node is None:
            function_node = call_node.child_by_field_name("name")
        return cls._node_text(function_node, source).strip()

    @classmethod
    def _call_arguments(cls, call_node: Any) -> List[Any]:
        """Lista de nós de argumento de uma chamada (campo 'arguments')."""
        args = call_node.child_by_field_name("arguments")
        if args is None:
            return []
        return list(getattr(args, "named_children", []))

    @classmethod
    def _is_literal_string(cls, node: Any, source: bytes) -> bool:
        """True para string/template estático sem interpolação dinâmica."""
        if node is None:
            return True
        if node.type in ("string", "string_literal", "raw_string_literal", "char_literal"):
            return True
        if node.type == "template_string":
            text = cls._node_text(node, source)
            return "${" not in text
        return False

    @classmethod
    def _ancestor_types(cls, node: Any) -> List[str]:
        """Tipos dos ancestrais do nó (do pai até a raiz)."""
        types: List[str] = []
        parent = getattr(node, "parent", None)
        while parent is not None:
            types.append(parent.type)
            parent = getattr(parent, "parent", None)
        return types

    # ── Supressão (needle-ignore) ─────────────────────────────────────────

    @classmethod
    def _get_file_suppressed_rules(cls, lines: List[str]) -> Set[str]:
        """Extrai regras suprimidas globalmente via diretiva de cabeçalho.

        Exemplo:
            >>> "SQL_INJECTION" in TreeSitterGuard._get_file_suppressed_rules(
            ...     ["// needle-ignore-file: SQL_INJECTION"]
            ... )
            True
        """
        suppressed: Set[str] = set()
        file_pat = re.compile(
            r"(?://|/\*|#|--)\s*needle-ignore-file:\s*([A-Za-z0-9_,\s-]+)",
            re.IGNORECASE,
        )
        all_pat = re.compile(r"(?://|/\*|#|--)\s*needle-ignore-all\b", re.IGNORECASE)
        for line in lines[:20]:
            cleaned = line.strip()
            if all_pat.search(cleaned):
                suppressed.add("ALL")
                return suppressed
            match = file_pat.search(cleaned)
            if match:
                tokens = [tok.strip().upper() for tok in match.group(1).split(",") if tok.strip()]
                suppressed.update(tokens)
        return suppressed

    @staticmethod
    def _line_rule_tokens(line: str, directive: str) -> Optional[Set[str]]:
        """Tokens de regra de uma diretiva inline (needle-ignore[-next-line])."""
        pattern = re.compile(
            rf"(?://|/\*|#|--)\s*{directive}\s*:\s*([A-Za-z0-9_,\s-]+)",
            re.IGNORECASE,
        )
        match = pattern.search(line)
        if not match:
            return None
        return {token.strip().upper() for token in match.group(1).split(",") if token.strip()}

    @classmethod
    def _is_suppressed(
        cls,
        rule_id: str,
        line_number: int,
        lines: List[str],
        file_suppressed: Optional[Set[str]] = None,
    ) -> bool:
        """True se a regra foi suprimida no arquivo, na linha ou na linha anterior.

        Exemplo:
            >>> TreeSitterGuard._is_suppressed("PROD_MOCK", 1, ["# needle-ignore: PROD_MOCK"])
            True
        """
        target = rule_id.upper()
        targets = {target} | set(cls.RULE_ALIASES.get(target, set()))

        def _matches(tokens: Optional[Set[str]]) -> bool:
            if not tokens:
                return False
            return "ALL" in tokens or not targets.isdisjoint(tokens)

        if file_suppressed and ("ALL" in file_suppressed or not targets.isdisjoint(file_suppressed)):
            return True

        current_idx = line_number - 1
        if 0 <= current_idx < len(lines):
            if _matches(cls._line_rule_tokens(lines[current_idx], "needle-ignore")):
                return True

        previous_idx = line_number - 2
        if 0 <= previous_idx < len(lines):
            prev = lines[previous_idx]
            if _matches(cls._line_rule_tokens(prev, "needle-ignore-next-line")):
                return True
            stripped = prev.strip()
            if stripped.startswith(("//", "/*", "#", "*", "--")):
                if _matches(cls._line_rule_tokens(prev, "needle-ignore")):
                    return True

        return False

    # ── Classificação de caminhos ─────────────────────────────────────────

    @staticmethod
    def is_test_file(rel_path: str) -> bool:
        """True quando o caminho pertence a suíte de testes/mocks.

        Exemplo:
            >>> TreeSitterGuard.is_test_file("tests/test_pay.py")
            True
        """
        normalized = str(rel_path).replace("\\", "/")
        path = Path(normalized)
        parent_parts = {part.lower() for part in path.parts[:-1]}
        if parent_parts & TEST_DIR_PARTS:
            return True
        name = path.name.lower()
        return (
            name.startswith("test_")
            or name.endswith(("_test.py", "_test.go", "_test.rs", "_spec.rb"))
            or ".test." in name
            or ".spec." in name
            or name == "conftest.py"
        )

    @classmethod
    def is_production_path(cls, rel_path: str) -> bool:
        """True para código de produção (fora de tests/, examples/, scripts/...).

        Exemplo:
            >>> TreeSitterGuard.is_production_path("tests/test_a.py")
            False
        """
        if cls.is_test_file(rel_path):
            return False
        normalized = str(rel_path).replace("\\", "/")
        parent_parts = {part.lower() for part in Path(normalized).parts[:-1]}
        return not bool(parent_parts & NON_PRODUCTION_DIRS)

    # ── Emissão de violações ──────────────────────────────────────────────

    @classmethod
    def _emit(
        cls,
        violations: List[TreeSitterViolation],
        rule_id: str,
        line_number: int,
        lines: List[str],
        file_path: str,
        language: str,
        message: Optional[str] = None,
        suggestion: Optional[str] = None,
    ) -> None:
        """Cria e registra uma violação com snippet da linha afetada."""
        snippet = ""
        if 0 < line_number <= len(lines):
            snippet = lines[line_number - 1].strip()[:160]
        violations.append(
            TreeSitterViolation(
                rule_id=rule_id,
                category=cls.RULE_CATEGORIES.get(rule_id, "structural"),
                file_path=file_path,
                line_number=max(1, line_number),
                message=message or cls.RULE_MESSAGES.get(rule_id, rule_id),
                code_snippet=snippet,
                snippet=snippet,
                suggestion=suggestion or cls.RULE_SUGGESTIONS.get(rule_id),
                language=language,
            )
        )

    @classmethod
    def _finalize(
        cls,
        violations: List[TreeSitterViolation],
        lines: List[str],
        file_path: str,
        file_suppressed: Set[str],
    ) -> List[TreeSitterViolation]:
        """Aplica supressões, regra de path de produção e deduplicação."""
        result: List[TreeSitterViolation] = []
        seen: Set[Tuple[str, int]] = set()
        for violation in violations:
            if violation.rule_id == "PROD_MOCK" and not cls.is_production_path(file_path):
                continue
            if cls._is_suppressed(
                violation.rule_id, violation.line_number, lines, file_suppressed
            ):
                continue
            key = (violation.rule_id, violation.line_number)
            if key in seen:
                continue
            seen.add(key)
            result.append(violation)
        return sorted(result, key=lambda item: (item.line_number, item.rule_id))

    # ── API principal ─────────────────────────────────────────────────────

    @classmethod
    def scan_content(
        cls,
        content: str,
        file_path: str,
        language: Optional[str] = None,
    ) -> List[TreeSitterViolation]:
        """Analisa um conteúdo-fonte e devolve violações estruturais.

        Exemplo:
            >>> code = "function f(){ eval(input); }"
            >>> TreeSitterGuard.scan_content(code, "src/a.js")[0].rule_id
            'DANGEROUS_EVAL'
        """
        if not content or not content.strip():
            return []

        lines = content.splitlines()
        file_suppressed = cls._get_file_suppressed_rules(lines)
        resolved_language = language or cls.language_for_file(file_path) or ""
        violations: List[TreeSitterViolation] = []
        parsed = False

        if resolved_language:
            parser = cls.get_parser(resolved_language)
            if parser is not None:
                try:
                    source = content.encode("utf-8", errors="replace")
                    with _PARSE_LOCK:
                        tree = parser.parse(source)
                    if resolved_language == "python":
                        cls._scan_python(tree.root_node, source, lines, file_path, violations)
                    elif resolved_language in JS_FAMILY:
                        cls._scan_javascript(
                            tree.root_node, source, lines, file_path, violations
                        )
                    else:
                        cls._scan_generic(
                            tree.root_node,
                            source,
                            lines,
                            file_path,
                            resolved_language,
                            violations,
                        )
                    parsed = True
                except Exception as exc:
                    logger.debug(
                        "Falha de parsing tree-sitter em %s (%s): %s",
                        file_path,
                        resolved_language,
                        exc,
                    )
                    violations.clear()

        used_language = resolved_language or "text"
        if not parsed:
            cls._scan_lexical(content, lines, file_path, used_language, violations)

        return cls._finalize(violations, lines, file_path, file_suppressed)

    @classmethod
    def scan_file(
        cls,
        file_path: Union[str, Path],
        workspace: Optional[Union[str, Path]] = None,
        language: Optional[str] = None,
    ) -> List[TreeSitterViolation]:
        """Analisa um arquivo em disco, opcionalmente relativo a um workspace.

        Exemplo:
            >>> TreeSitterGuard.scan_file("inexistente.py") == []
            True
        """
        path = Path(file_path)
        if not path.is_file():
            return []
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                logger.warning("Arquivo ignorado por exceder %d bytes: %s", MAX_FILE_BYTES, path)
                return []
            content = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            logger.warning("Falha ao ler %s para análise tree-sitter: %s", path, exc)
            return []

        rel_path = cls._relative_path(path, workspace)
        return cls.scan_content(content, rel_path, language=language)

    @staticmethod
    def _relative_path(path: Path, workspace: Optional[Union[str, Path]]) -> str:
        """Normaliza o caminho do arquivo relativo ao workspace quando possível."""
        if workspace is None:
            return str(path)
        try:
            return str(path.resolve().relative_to(Path(workspace).resolve()))
        except Exception:
            return str(path)

    @classmethod
    def _iter_source_files(
        cls,
        root: Path,
        scope: Optional[Union[str, Sequence[str]]] = None,
    ) -> List[Path]:
        """Coleta arquivos-fonte suportados, podando diretórios ignorados."""
        bases: List[Path] = []
        if scope:
            scopes = [scope] if isinstance(scope, str) else list(scope)
            for item in scopes:
                for candidate in str(item).split(","):
                    candidate = candidate.strip()
                    if not candidate:
                        continue
                    base = (root / candidate).resolve()
                    if base.is_file():
                        bases.append(base)
                    elif base.is_dir():
                        bases.append(base)
            if not bases:
                return []
        else:
            bases = [root]

        collected: List[Path] = []
        seen: Set[Path] = set()
        for base in bases:
            if base.is_file():
                if base.suffix.lower() in cls.LANGUAGE_BY_EXTENSION and base not in seen:
                    seen.add(base)
                    collected.append(base)
                continue
            for dirpath, dirnames, filenames in os.walk(base):
                dirnames[:] = sorted(
                    directory
                    for directory in dirnames
                    if directory not in IGNORED_DIRS and not directory.startswith(".cache")
                )
                for filename in sorted(filenames):
                    path = Path(dirpath) / filename
                    if path.suffix.lower() not in cls.LANGUAGE_BY_EXTENSION:
                        continue
                    if path in seen:
                        continue
                    seen.add(path)
                    collected.append(path)
        return collected

    @classmethod
    def _filter_delta_files(cls, root: Path, files: List[Path]) -> List[Path]:
        """Mantém apenas arquivos alterados no Git.

        Em indisponibilidade do Git (exceção) ou workspace que não é repositório,
        devolve TODOS os arquivos (mesmo fallback da Camada 1). Num repo Git sem
        alterações, devolve lista vazia (delta legítimo).
        """
        try:
            from ..core.diff_inspector import (
                get_workspace_diff_files,
                normalize_workspace_relpath,
            )

            diff_files = get_workspace_diff_files(str(root))
        except Exception as exc:
            logger.debug("Diff indisponível para auditoria delta (%s): %s", root, exc)
            return files

        if not diff_files:
            # Distingue "sem Git" de "repo Git limpo": sem repositório (ou se o
            # Git não responde) devolvemos TODOS os arquivos — alinhado ao
            # fallback da Camada 1; num repo Git sem alterações, o delta é vazio
            # de forma legítima.
            if cls._is_git_workspace(root):
                return []
            logger.debug("Workspace sem Git: delta degrada para varredura completa (%s).", root)
            return files

        changed = {
            normalize_workspace_relpath(df.path, str(root))
            for df in diff_files
            if not getattr(df, "is_binary", False)
        }
        filtered: List[Path] = []
        for path in files:
            rel = normalize_workspace_relpath(path, root)
            if rel in changed or any(rel.endswith(item) or item.endswith(rel) for item in changed):
                filtered.append(path)
        return filtered

    @staticmethod
    def _is_git_workspace(root: Path) -> bool:
        """True quando ``root`` está dentro de uma árvore de trabalho Git válida.

        Exemplo:
            >>> TreeSitterGuard._is_git_workspace(Path("/tmp/nao-repo")) is False
            True
        """
        try:
            proc = subprocess.run(
                ["git", "rev-parse", "--is-inside-work-tree"],
                cwd=str(root),
                capture_output=True,
                text=True,
                errors="replace",
                timeout=5,
            )
        except Exception as exc:
            logger.debug("Git indisponível em %s: %s", root, exc)
            return False
        return proc.returncode == 0 and proc.stdout.strip() == "true"

    @classmethod
    def scan_workspace(
        cls,
        workspace: Union[str, Path],
        audit_mode: str = "delta",
        scope: Optional[Union[str, Sequence[str]]] = None,
    ) -> List[TreeSitterViolation]:
        """Varre um workspace multi-linguagem (modo delta ou full).

        Exemplo:
            >>> TreeSitterGuard.scan_workspace("/caminho/inexistente", audit_mode="full")
            []
        """
        root = Path(workspace).resolve()
        if not root.is_dir():
            return []

        files = cls._iter_source_files(root, scope=scope)
        if audit_mode == "delta":
            files = cls._filter_delta_files(root, files)

        violations: List[TreeSitterViolation] = []
        for path in files:
            violations.extend(cls.scan_file(path, workspace=root))
        return violations

    @classmethod
    def scan_workspace_report(
        cls,
        workspace: Union[str, Path],
        audit_mode: str = "delta",
        scope: Optional[Union[str, Sequence[str]]] = None,
    ) -> Dict[str, Any]:
        """Varre o workspace e devolve relatório agregado pronto para o runner.

        Exemplo:
            >>> report = TreeSitterGuard.scan_workspace_report("/tmp", audit_mode="full")
            >>> sorted(report.keys())[:2]
            ['blocking', 'engine']
        """
        root = Path(workspace).resolve()
        if not root.is_dir():
            return {
                "blocking": [],
                "warnings": [],
                "total_files": 0,
                "engine": "tree_sitter",
                "languages": [],
            }

        files = cls._iter_source_files(root, scope=scope)
        if audit_mode == "delta":
            files = cls._filter_delta_files(root, files)

        blocking: List[Dict[str, Any]] = []
        warnings: List[Dict[str, Any]] = []
        languages: Set[str] = set()
        parsed_files = 0
        fallback_files = 0

        for path in files:
            language = cls.language_for_file(path)
            if language:
                languages.add(language)
            parser = cls.get_parser(language) if language else None
            if parser is not None:
                parsed_files += 1
            else:
                fallback_files += 1
            for violation in cls.scan_file(path, workspace=root):
                payload = violation.to_dict()
                if violation.severity == "blocking":
                    blocking.append(payload)
                else:
                    warnings.append(payload)

        if parsed_files and fallback_files:
            engine = "tree_sitter_partial"
        elif fallback_files and not parsed_files:
            engine = "tree_sitter_lexical_fallback"
        else:
            engine = "tree_sitter"

        return {
            "blocking": blocking,
            "warnings": warnings,
            "total_files": len(files),
            "engine": engine,
            "languages": sorted(languages),
        }

    # ── Python: regras estruturais ────────────────────────────────────────

    @classmethod
    def _python_async_names(cls, root: Any, source: bytes) -> Set[str]:
        """Coleta nomes de corrotinas locais declaradas com ``async def``."""
        names: Set[str] = set()
        for node in cls._walk(root):
            if node.type != "function_definition" or not cls._has_token(node, "async"):
                continue
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                names.add(cls._node_text(name_node, source))
        return names

    @classmethod
    def _scan_python(
        cls,
        root: Any,
        source: bytes,
        lines: List[str],
        file_path: str,
        violations: List[TreeSitterViolation],
    ) -> None:
        """Aplica as meta-regras sobre a AST Python (tree-sitter)."""
        async_names = cls._python_async_names(root, source)

        for node in cls._walk(root):
            node_type = node.type
            line = node.start_point[0] + 1

            if node_type in ("import_statement", "import_from_statement"):
                text = cls._node_text(node, source)
                if re.search(
                    r"(?<![.\w])(?:unittest\.mock|mock|pytest|faker|responses|httpretty|freezegun)\b",
                    text,
                ):
                    cls._emit(
                        violations,
                        "PROD_MOCK",
                        line,
                        lines,
                        file_path,
                        "python",
                        message=f"Import de framework de mock/teste em produção: {text.strip()[:90]}",
                    )
                continue

            if node_type == "call":
                cls._check_python_call(
                    node, source, lines, file_path, async_names, violations
                )
                continue

            if node_type == "expression_statement":
                direct_call = next(
                    (child for child in node.named_children if child.type == "call"),
                    None,
                )
                if direct_call is not None:
                    name = cls._python_call_name(direct_call, source)
                    if name in async_names:
                        cls._emit(
                            violations,
                            "FLOATING_PROMISE",
                            direct_call.start_point[0] + 1,
                            lines,
                            file_path,
                            "python",
                            message=f"Corrotina '{name}()' chamada sem await (promessa órfã).",
                        )
                continue

            if node_type == "return_statement":
                children = cls._children_without_comments(node)
                returned = next(
                    (child for child in children if child.type != "return"), None
                )
                if returned is not None and returned.type == "call":
                    name = cls._python_call_name(returned, source)
                    ancestors = cls._ancestor_types(node)
                    if name in async_names and "try_statement" in ancestors:
                        cls._emit(
                            violations,
                            "ASYNC_RETURN_NO_AWAIT",
                            node.start_point[0] + 1,
                            lines,
                            file_path,
                            "python",
                            message=f"Retorno de corrotina '{name}()' sem await dentro de try.",
                        )
                continue

            if node_type == "except_clause":
                blocks = [child for child in node.named_children if child.type == "block"]
                if blocks:
                    statements = cls._children_without_comments(blocks[-1])
                    if len(statements) == 1:
                        stmt = statements[0]
                        stmt_text = cls._node_text(stmt, source).strip()
                        if stmt.type == "pass_statement" or stmt_text in {"...", "pass"}:
                            cls._emit(
                                violations,
                                "SILENT_CATCH",
                                line,
                                lines,
                                file_path,
                                "python",
                                message="except silencioso: bloco contém apenas pass/ellipsis.",
                            )
                continue

            if node_type == "string":
                text = cls._node_text(node, source)
                has_interpolation = any(
                    child.type == "interpolation" for child in node.named_children
                )
                if has_interpolation and SQL_KEYWORDS.search(text):
                    cls._emit(
                        violations,
                        "SQL_INJECTION",
                        line,
                        lines,
                        file_path,
                        "python",
                        message="f-string com SQL interpolado dinamicamente.",
                    )
                continue

            if node_type == "binary_operator":
                cls._check_python_sql_binary(node, source, lines, file_path, violations)
                continue

            if node_type in ("function_definition", "class_definition"):
                cls._check_manual_stub(node, source, lines, file_path, "python", violations)
                continue

    @classmethod
    def _python_call_name(cls, call_node: Any, source: bytes) -> str:
        """Nome do chamável Python (``func`` ou ``obj.method``)."""
        function_node = call_node.child_by_field_name("function")
        return cls._node_text(function_node, source).strip()

    @classmethod
    def _check_python_call(
        cls,
        node: Any,
        source: bytes,
        lines: List[str],
        file_path: str,
        async_names: Set[str],
        violations: List[TreeSitterViolation],
    ) -> None:
        """Detecta eval/exec, mocks, SQL por .format, open() sem with e shell."""
        name = cls._python_call_name(node, source)
        base = name.rsplit(".", 1)[-1]
        line = node.start_point[0] + 1

        parent = getattr(node, "parent", None)
        if (
            name in async_names
            and parent is not None
            and parent.type in ("block", "expression_statement", "module")
        ):
            cls._emit(
                violations,
                "FLOATING_PROMISE",
                line,
                lines,
                file_path,
                "python",
                message=f"Corrotina '{name}()' chamada sem await (promessa órfã).",
            )
        # Só a função global ``eval``/``exec`` (ou ``builtins.eval``/``exec``) é
        # perigosa. Chamada de MÉTODO em objeto/instância — como
        # ``self.model.eval()`` (modo de inferência de modelos de ML) ou
        # ``expr.eval()`` — não é eval dinâmico e não deve ser bloqueada.
        if name in EVAL_NAMES:
            cls._emit(violations, "DANGEROUS_EVAL", line, lines, file_path, "python")
            return
        if name in EXEC_NAMES:
            cls._emit(violations, "DANGEROUS_EVAL", line, lines, file_path, "python")
            return
        if base in MOCK_SYMBOLS or name.startswith(("mock.", "unittest.mock.")):
            cls._emit(
                violations,
                "PROD_MOCK",
                line,
                lines,
                file_path,
                "python",
                message=f"Invocação de mock/stub em produção: {name}()",
            )
            return
        if "format" in base and SQL_KEYWORDS.search(cls._node_text(node, source)):
            cls._emit(
                violations,
                "SQL_INJECTION",
                line,
                lines,
                file_path,
                "python",
                message="SQL montado via .format() com valores dinâmicos.",
            )
            return
        if name == "open":
            ancestors = set(cls._ancestor_types(node))
            if not ancestors & {"with_statement", "with_item", "with_clause"}:
                cls._emit(
                    violations,
                    "RESOURCE_LEAK",
                    line,
                    lines,
                    file_path,
                    "python",
                    message="open() fora de context manager: arquivo pode vazar.",
                )
            return
        if name in {"os.system", "os.popen"}:
            cls._emit(
                violations,
                "COMMAND_INJECTION",
                line,
                lines,
                file_path,
                "python",
                message=f"Execução de shell via {name}().",
            )
            return
        if name.startswith("subprocess.") and cls._has_shell_true(node, source):
            arguments = cls._call_arguments(node)
            first = arguments[0] if arguments else None
            if first is not None and first.type != "string":
                cls._emit(
                    violations,
                    "COMMAND_INJECTION",
                    line,
                    lines,
                    file_path,
                    "python",
                    message=f"{name} com shell=True e comando dinâmico.",
                )

    @classmethod
    def _has_shell_true(cls, call_node: Any, source: bytes) -> bool:
        """True se a chamada subprocess possui keyword argument shell=True.

        Exemplo:
            >>> call = type("C", (), {})()
            >>> TreeSitterGuard._has_shell_true(call, b"") is False
            True
        """
        args_node = call_node.child_by_field_name("arguments")
        candidates = (
            getattr(args_node, "children", [])
            if args_node is not None
            else getattr(call_node, "children", [])
        )
        for child in candidates:
            if child.type != "keyword_argument":
                continue
            name_node = child.child_by_field_name("name")
            value_node = child.child_by_field_name("value")
            if name_node is None or value_node is None:
                continue
            name_text = cls._node_text(name_node, source).strip()
            value_text = cls._node_text(value_node, source).strip()
            if name_text == "shell" and value_text in {"True", "true"}:
                return True
        return False

    @classmethod
    def _check_python_sql_binary(
        cls,
        node: Any,
        source: bytes,
        lines: List[str],
        file_path: str,
        violations: List[TreeSitterViolation],
    ) -> None:
        """Detecta ``"SELECT ... " + var`` e ``"SELECT %s" % var``."""
        operator_node = node.child_by_field_name("operator")
        operator = (
            cls._node_text(operator_node, source).strip()
            if operator_node is not None
            else ""
        )
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None:
            return
        left_text = cls._node_text(left, source)
        right_text = cls._node_text(right, source)
        left_is_string = left.type in ("string", "concatenated_string") and not any(
            child.type == "interpolation" for child in left.named_children
        )
        right_is_string = right.type in ("string", "concatenated_string")

        if operator == "+" and SQL_KEYWORDS.search(left_text) and not right_is_string:
            cls._emit(
                violations,
                "SQL_INJECTION",
                node.start_point[0] + 1,
                lines,
                file_path,
                "python",
                message="Concatenação de variável em query SQL.",
            )
        elif operator == "+" and SQL_KEYWORDS.search(right_text) and not left_is_string:
            cls._emit(
                violations,
                "SQL_INJECTION",
                node.start_point[0] + 1,
                lines,
                file_path,
                "python",
                message="Concatenação de variável em query SQL.",
            )
        elif operator == "%" and SQL_KEYWORDS.search(left_text) and not right_is_string:
            cls._emit(
                violations,
                "SQL_INJECTION",
                node.start_point[0] + 1,
                lines,
                file_path,
                "python",
                message="Interpolação de SQL via operador %.",
            )

    @classmethod
    def _check_manual_stub(
        cls,
        node: Any,
        source: bytes,
        lines: List[str],
        file_path: str,
        language: str,
        violations: List[TreeSitterViolation],
    ) -> None:
        """Detecta classes/funções manuais de mock/stub/fake em produção."""
        name_node = node.child_by_field_name("name")
        if name_node is None:
            return
        name = cls._node_text(name_node, source).strip()
        if not name or not cls.is_production_path(file_path):
            return
        is_class = node.type in ("class_definition", "class_declaration")
        looks_manual = (
            re.match(r"^(?:Mock|Fake|Stub|Dummy)[A-Z]", name) is not None
            or name.endswith("Stub")
            or (not is_class and re.match(r"^(?:fake|stub|dummy)_[a-z0-9_]+$", name) is not None)
        )
        if looks_manual:
            cls._emit(
                violations,
                "PROD_MOCK",
                node.start_point[0] + 1,
                lines,
                file_path,
                language,
                message=f"Stub/mock manual declarado em produção: '{name}'.",
            )

    # ── JavaScript / TypeScript: regras estruturais ───────────────────────

    @classmethod
    def _js_async_names(cls, root: Any, source: bytes) -> Set[str]:
        """Coleta nomes de funções/métodos declarados ``async`` no arquivo."""
        names: Set[str] = set()
        for node in cls._walk(root):
            if node.type in ("function_declaration", "method_definition", "function_expression"):
                if not cls._has_token(node, "async"):
                    continue
                name_node = node.child_by_field_name("name")
                if name_node is not None:
                    names.add(cls._node_text(name_node, source).strip())
            elif node.type == "variable_declarator":
                value = node.child_by_field_name("value")
                name_node = node.child_by_field_name("name")
                if (
                    value is not None
                    and value.type in ("arrow_function", "function_expression", "function")
                    and cls._has_token(value, "async")
                    and name_node is not None
                ):
                    names.add(cls._node_text(name_node, source).strip())
        return names

    @classmethod
    def _js_child_process_names(cls, root: Any, source: bytes) -> Set[str]:
        """Nomes importados de ``child_process`` / ``node:child_process``."""
        names: Set[str] = set()
        for node in cls._walk(root):
            if node.type != "import_statement":
                continue
            source_node = node.child_by_field_name("source")
            module_name = cls._node_text(source_node, source).strip("\"'`")
            if "child_process" not in module_name:
                continue
            for child in cls._walk(node):
                if child.type in ("import_specifier", "identifier") and child is not node:
                    if child.type == "import_specifier":
                        name_node = child.child_by_field_name("name")
                        if name_node is not None:
                            names.add(cls._node_text(name_node, source).strip())
                    else:
                        text = cls._node_text(child, source).strip()
                        if text not in {"import", "from", "child_process"}:
                            names.add(text)
        for node in cls._walk(root):
            if node.type != "call_expression":
                continue
            name = cls._callable_name(node, source)
            if name != "require":
                continue
            arguments = cls._call_arguments(node)
            if not arguments:
                continue
            module_name = cls._node_text(arguments[0], source).strip("\"'`")
            if "child_process" not in module_name:
                continue
            parent = getattr(node, "parent", None)
            if parent is not None and parent.type == "variable_declarator":
                var_name = parent.child_by_field_name("name")
                if var_name is not None:
                    names.add(cls._node_text(var_name, source).strip())
        return names

    @classmethod
    def _js_call_static_name(cls, call_node: Any, source: bytes) -> str:
        """Nome textual do chamável JS (identificador ou obj.método)."""
        return cls._callable_name(call_node, source)

    @classmethod
    def _js_callee_parts(cls, call_node: Any, source: bytes) -> Tuple[str, str]:
        """Devolve (objeto, propriedade) para chamadas de membro; ('', nome) caso contrário."""
        function_node = call_node.child_by_field_name("function")
        if function_node is None:
            return "", ""
        if function_node.type == "member_expression":
            object_node = function_node.child_by_field_name("object")
            property_node = function_node.child_by_field_name("property")
            return (
                cls._node_text(object_node, source).strip(),
                cls._node_text(property_node, source).strip(),
            )
        return "", cls._node_text(function_node, source).strip()

    @classmethod
    def _js_call_returns_promise(
        cls, call_node: Any, source: bytes, async_names: Set[str]
    ) -> bool:
        """Heurística determinística de chamada que devolve promessa."""
        call_name = cls._js_call_static_name(call_node, source)
        base = call_name.rsplit(".", 1)[-1]
        if call_name in async_names or base in async_names:
            return True
        if call_name in {"fetch", "axios", "superagent"} or base in {"fetch"}:
            return True
        if base.endswith(("Async", "AsyncAll")) and len(base) > 5:
            return True
        obj, prop = cls._js_callee_parts(call_node, source)
        if obj and prop and obj.split(".")[-1] in JS_CLIENT_OBJECTS:
            return PROMISE_METHOD_RE.match(prop) is not None
        return False

    @classmethod
    def _scan_javascript(
        cls,
        root: Any,
        source: bytes,
        lines: List[str],
        file_path: str,
        violations: List[TreeSitterViolation],
    ) -> None:
        """Aplica as meta-regras sobre AST JavaScript/TypeScript/TSX."""
        async_names = cls._js_async_names(root, source)
        child_process_names = cls._js_child_process_names(root, source)

        listener_adds: List[Tuple[str, int, str, str]] = []
        listener_cleanups: List[Tuple[str, str]] = []
        timer_adds: List[Tuple[str, int, str]] = []
        timer_cleanups: Set[str] = set()

        for node in cls._walk(root):
            node_type = node.type
            line = node.start_point[0] + 1

            if node_type in ("import_statement", "export_statement"):
                source_node = node.child_by_field_name("source")
                module_name = cls._node_text(source_node, source).strip("\"'`")
                if module_name and any(
                    module_name == mock or module_name.startswith(mock + "/")
                    for mock in MOCK_MODULES
                ):
                    cls._emit(
                        violations,
                        "PROD_MOCK",
                        line,
                        lines,
                        file_path,
                        "javascript",
                        message=f"Import de framework de mock/teste em produção: {module_name}",
                    )
                continue

            if node_type == "call_expression":
                call_name = cls._js_call_static_name(node, source)
                base = call_name.rsplit(".", 1)[-1]

                if any(pattern.match(call_name) for pattern in MOCK_CALL_PATTERNS):
                    cls._emit(
                        violations,
                        "PROD_MOCK",
                        line,
                        lines,
                        file_path,
                        "javascript",
                        message=f"Mock/spy/stub invocado em produção: {call_name}()",
                    )
                    continue

                if base == "eval" or call_name in EVAL_NAMES:
                    cls._emit(violations, "DANGEROUS_EVAL", line, lines, file_path, "javascript")
                    continue

                if base in ("exec", "execSync", "spawn", "spawnSync", "fork") and (
                    base in child_process_names
                    or cls._js_callee_parts(node, source)[0].split(".")[-1]
                    in {"child_process", "cp"}
                    or "child_process" in call_name
                ):
                    arguments = cls._call_arguments(node)
                    first = arguments[0] if arguments else None
                    if not cls._is_literal_string(first, source):
                        cls._emit(
                            violations,
                            "COMMAND_INJECTION",
                            line,
                            lines,
                            file_path,
                            "javascript",
                            message=f"{call_name}() com argumento dinâmico (command injection).",
                        )
                    continue

                if base in LISTENER_ADD_METHODS:
                    event = cls._first_string_argument(node, source)
                    listener_adds.append((base, line, event, call_name))
                elif base in LISTENER_CLEANUP_METHODS:
                    listener_cleanups.append((base, cls._first_string_argument(node, source)))
                elif base in TIMER_ADD_METHODS:
                    timer_adds.append((base, line, cls._first_string_argument(node, source)))
                elif base in TIMER_CLEANUP_METHODS:
                    timer_cleanups.add(base)
                continue

            if node_type == "new_expression":
                constructor = node.child_by_field_name("constructor")
                constructor_name = cls._node_text(constructor, source).strip()
                if constructor_name in {"Function", "FunctionConstructor"}:
                    cls._emit(
                        violations,
                        "SECURITY_EXEC",
                        line,
                        lines,
                        file_path,
                        "javascript",
                        message="Construção dinâmica de função via new Function().",
                    )
                continue

            if node_type == "expression_statement":
                expr = next(
                    (child for child in node.named_children if child is not None), None
                )
                cls._check_js_floating(expr, source, lines, file_path, async_names, violations)
                continue

            if node_type == "return_statement":
                cls._check_js_async_return(
                    node, source, lines, file_path, async_names, violations
                )
                continue

            if node_type == "catch_clause":
                body = node.child_by_field_name("body")
                if body is not None and cls._is_empty_block(body):
                    cls._emit(
                        violations,
                        "SILENT_CATCH",
                        line,
                        lines,
                        file_path,
                        "javascript",
                        message="Bloco catch vazio engolindo exceções.",
                    )
                continue

            if node_type == "template_string":
                text = cls._node_text(node, source)
                if "${" in text and SQL_KEYWORDS.search(text):
                    cls._emit(
                        violations,
                        "SQL_INJECTION",
                        line,
                        lines,
                        file_path,
                        "javascript",
                        message="Template string com SQL interpolado dinamicamente.",
                    )
                continue

            if node_type == "binary_expression":
                if cls._js_sql_binary(node, source):
                    cls._emit(
                        violations,
                        "SQL_INJECTION",
                        line,
                        lines,
                        file_path,
                        "javascript",
                        message="Concatenação de variável em query SQL.",
                    )
                continue

            if node_type in ("class_declaration", "function_declaration", "variable_declarator"):
                cls._check_manual_stub(node, source, lines, file_path, "javascript", violations)
                if node.type == "variable_declarator":
                    name_node = node.child_by_field_name("name")
                    name = cls._node_text(name_node, source).strip()
                    if name and re.match(r"^(?:mock|fake|stub|dummy)[A-Z_]", name):
                        cls._emit(
                            violations,
                            "PROD_MOCK",
                            line,
                            lines,
                            file_path,
                            "javascript",
                            message=f"Stub/mock manual declarado em produção: '{name}'.",
                        )

        cls._check_listener_leaks(
            listener_adds, listener_cleanups, lines, file_path, violations
        )
        cls._check_timer_leaks(timer_adds, timer_cleanups, lines, file_path, violations)

    @staticmethod
    def _first_string_argument(call_node: Any, source: bytes) -> str:
        """Primeiro argumento string literal de uma chamada, se existir."""
        arguments = call_node.child_by_field_name("arguments")
        if arguments is None:
            return ""
        for child in arguments.named_children:
            text = source[child.start_byte : child.end_byte].decode("utf-8", "replace")
            if child.type in ("string", "template_string") and "${" not in text:
                return text.strip("\"'`")
        return ""

    @staticmethod
    def _is_empty_block(block_node: Any) -> bool:
        """True quando um bloco de statements está vazio (ignorando comentários)."""
        named = [
            child
            for child in getattr(block_node, "named_children", [])
            if child.type not in ("comment", "line_comment", "block_comment")
        ]
        if not named:
            return True
        if len(named) == 1 and named[0].type == "empty_statement":
            return True
        return False

    @classmethod
    def _check_js_floating(
        cls,
        expr: Any,
        source: bytes,
        lines: List[str],
        file_path: str,
        async_names: Set[str],
        violations: List[TreeSitterViolation],
    ) -> None:
        """Detecta expression_statement cujo topo é chamada de promessa não tratada."""
        if expr is None or expr.type != "call_expression":
            return
        call_text = cls._node_text(expr, source)
        if re.search(r"\.(?:then|catch|finally)\s*\(", call_text):
            return
        if not cls._js_call_returns_promise(expr, source, async_names):
            return
        call_name = cls._js_call_static_name(expr, source)
        cls._emit(
            violations,
            "FLOATING_PROMISE",
            expr.start_point[0] + 1,
            lines,
            file_path,
            "javascript",
            message=f"Chamada assíncrona '{call_name}()' sem await/.catch (floating promise).",
        )

    @classmethod
    def _check_js_async_return(
        cls,
        node: Any,
        source: bytes,
        lines: List[str],
        file_path: str,
        async_names: Set[str],
        violations: List[TreeSitterViolation],
    ) -> None:
        """Detecta ``return promise()`` dentro de try sem await."""
        ancestors = cls._ancestor_types(node)
        if "try_statement" not in ancestors:
            return
        if "await_expression" in ancestors:
            return
        children = [
            child
            for child in node.named_children
            if child.type not in ("comment", "line_comment", "block_comment")
        ]
        if not children:
            return
        returned = children[0]
        if returned.type != "call_expression":
            return
        if not cls._js_call_returns_promise(returned, source, async_names):
            return
        call_name = cls._js_call_static_name(returned, source)
        cls._emit(
            violations,
            "ASYNC_RETURN_NO_AWAIT",
            node.start_point[0] + 1,
            lines,
            file_path,
            "javascript",
            message=f"return de '{call_name}()' sem await dentro de try/catch.",
        )

    @classmethod
    def _js_sql_binary(cls, node: Any, source: bytes) -> bool:
        """True quando um binário ``+`` concatena SQL com dado dinâmico."""
        text = cls._node_text(node, source)
        if "+" not in text or not SQL_KEYWORDS.search(text):
            return False
        left = node.child_by_field_name("left")
        right = node.child_by_field_name("right")
        if left is None or right is None:
            return False
        left_text = cls._node_text(left, source)
        right_text = cls._node_text(right, source)
        left_is_string = left.type in ("string", "template_string")
        right_is_string = right.type in ("string", "template_string")
        if SQL_KEYWORDS.search(left_text) and left_is_string and not right_is_string:
            return True
        if SQL_KEYWORDS.search(right_text) and right_is_string and not left_is_string:
            return True
        return False

    @classmethod
    def _check_listener_leaks(
        cls,
        adds: List[Tuple[str, int, str, str]],
        cleanups: List[Tuple[str, str]],
        lines: List[str],
        file_path: str,
        violations: List[TreeSitterViolation],
    ) -> None:
        """Sinaliza addEventListener/on sem removeEventListener/off correspondente."""
        if not adds:
            return
        cleanup_events = {event for _, event in cleanups if event}
        has_any_cleanup = bool(cleanups)
        for method, line, event, call_name in adds:
            cleaned = event in cleanup_events if event else has_any_cleanup
            if cleaned:
                continue
            detail = f"evento '{event}'" if event else "listener"
            cls._emit(
                violations,
                "RESOURCE_LEAK",
                line,
                lines,
                file_path,
                "javascript",
                message=(
                    f"{call_name}() registra {detail} sem cleanup correspondente "
                    f"({method.replace('addEventListener', 'removeEventListener')})."
                ),
            )

    @classmethod
    def _check_timer_leaks(
        cls,
        adds: List[Tuple[str, int, str]],
        cleanups: Set[str],
        lines: List[str],
        file_path: str,
        violations: List[TreeSitterViolation],
    ) -> None:
        """Sinaliza setInterval/setTimeout sem clearInterval/clearTimeout no arquivo."""
        if not adds:
            return
        for method, line, _handler in adds:
            expected = {
                "setInterval": "clearInterval",
                "setTimeout": "clearTimeout",
                "requestAnimationFrame": "cancelAnimationFrame",
            }.get(method, "clear")
            if expected in cleanups:
                continue
            cls._emit(
                violations,
                "RESOURCE_LEAK",
                line,
                lines,
                file_path,
                "javascript",
                message=f"{method}() sem {expected}() correspondente no arquivo.",
            )

    # ── Genérico (Java, Go, Rust, Ruby, PHP, Kotlin, Swift, C/C++, Bash) ──

    @classmethod
    def _scan_generic(
        cls,
        root: Any,
        source: bytes,
        lines: List[str],
        file_path: str,
        language: str,
        violations: List[TreeSitterViolation],
    ) -> None:
        """Meta-regras agnósticas de gramática para linguagens adicionais."""
        listener_adds: List[Tuple[str, int, str, str]] = []
        listener_cleanups: List[Tuple[str, str]] = []
        timer_adds: List[Tuple[str, int, str]] = []
        timer_cleanups: Set[str] = set()
        call_types = {
            "call_expression",
            "method_invocation",
            "call",
            "invocation_expression",
            "function_call_expression",
            "method_call",
        }

        for node in cls._walk(root):
            node_type = node.type
            line = node.start_point[0] + 1

            if node_type in call_types:
                text = cls._node_text(node, source)
                name_match = re.match(r"\s*([A-Za-z_$][\w$.:]*)", text)
                call_name = name_match.group(1) if name_match else ""
                base = call_name.rsplit(".", 1)[-1].rsplit(":", 1)[-1]

                # Restringe ao chamável global ``eval``/``exec`` (ou nomes
                # qualificados conhecidos). Chamadas de método em objeto
                # (``obj.eval()``) não são eval dinâmico.
                if call_name in EVAL_NAMES or call_name in EXEC_NAMES:
                    cls._emit(
                        violations,
                        "DANGEROUS_EVAL",
                        line,
                        lines,
                        file_path,
                        language,
                        message=f"Uso de {base}() detectado por AST.",
                    )
                elif base in LISTENER_ADD_METHODS:
                    listener_adds.append((base, line, "", call_name))
                elif base in LISTENER_CLEANUP_METHODS:
                    listener_cleanups.append((base, ""))
                elif base in TIMER_ADD_METHODS:
                    timer_adds.append((base, line, ""))
                elif base in TIMER_CLEANUP_METHODS:
                    timer_cleanups.add(base)
                continue

            if "catch" in node_type or "except" in node_type:
                block = node.child_by_field_name("body")
                if block is None:
                    block = next(
                        (
                            child
                            for child in node.named_children
                            if child.type in ("block", "statement_block", "compound_statement")
                        ),
                        None,
                    )
                if block is not None and cls._is_empty_block(block):
                    cls._emit(
                        violations,
                        "SILENT_CATCH",
                        line,
                        lines,
                        file_path,
                        language,
                        message="Bloco de exceção vazio (silent catch).",
                    )
                continue

            if node_type in ("class_declaration", "class_definition", "class"):
                cls._check_manual_stub(node, source, lines, file_path, language, violations)

        cls._check_listener_leaks(listener_adds, listener_cleanups, lines, file_path, violations)
        cls._check_timer_leaks(timer_adds, timer_cleanups, lines, file_path, violations)
        cls._scan_lexical_sql(lines, file_path, language, violations)

    # ── Fallback léxico (gramática ausente) ───────────────────────────────

    @classmethod
    def _scan_lexical(
        cls,
        content: str,
        lines: List[str],
        file_path: str,
        language: str,
        violations: List[TreeSitterViolation],
    ) -> None:
        """Varredura textual resiliente usada quando a gramática não está disponível."""
        file_has_listener_cleanup = bool(
            re.search(r"\b(?:removeEventListener|unsubscribe|\.off\()", content)
        )
        file_has_interval_cleanup = "clearInterval" in content
        file_has_timeout_cleanup = "clearTimeout" in content

        patterns: List[Tuple[re.Pattern[str], str, str]] = [
            (
                re.compile(
                    r"(?:\bfrom\s+|\bimport\s+)(?:unittest\.mock|mock\b|pytest\b|sinon\b|vitest\b|jest\b)"
                    r"|jest\.(?:mock|fn|spyOn)\s*\(|vi\.(?:fn|mock)\s*\(|sinon\.(?:stub|spy)\s*\(",
                    re.IGNORECASE,
                ),
                "PROD_MOCK",
                "Import/invocação de mock detectado em arquivo de produção.",
            ),
            (
                re.compile(r"(?<![\w.$])eval\s*\("),
                "DANGEROUS_EVAL",
                "Uso de eval() detectado.",
            ),
            (
                re.compile(r"\bnew\s+Function\s*\("),
                "SECURITY_EXEC",
                "Construção dinâmica de função via new Function().",
            ),
            (
                re.compile(
                    r"(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|DROP\s+TABLE)"
                    r"[^;\n]*(?:\$\{|%\s*\(|\"\s*\+|\'\s*\+|`\s*\+|\+\s*[A-Za-z_$])",
                    re.IGNORECASE,
                ),
                "SQL_INJECTION",
                "Interpolação/concatenação de dado dinâmico em SQL.",
            ),
            (
                re.compile(
                    r"catch\s*\([^)]*\)\s*\{\s*(?:(?://|/\*)[^\n]*\s*)*\}",
                    re.IGNORECASE,
                ),
                "SILENT_CATCH",
                "Bloco catch vazio (silent catch).",
            ),
            (
                re.compile(
                    r"child_process\.(?:exec|execSync|spawn|spawnSync)\s*\(\s*[A-Za-z_$]"
                ),
                "COMMAND_INJECTION",
                "child_process com argumento dinâmico.",
            ),
        ]

        for index, raw_line in enumerate(lines, 1):
            stripped = raw_line.strip()
            if not stripped:
                continue
            for pattern, rule_id, message in patterns:
                if pattern.search(raw_line):
                    cls._emit(
                        violations,
                        rule_id,
                        index,
                        lines,
                        file_path,
                        language,
                        message=message,
                    )
            if re.search(r"\baddEventListener\s*\(", raw_line) and not file_has_listener_cleanup:
                cls._emit(
                    violations,
                    "RESOURCE_LEAK",
                    index,
                    lines,
                    file_path,
                    language,
                    message="addEventListener sem removeEventListener no arquivo.",
                )
            if re.search(r"\bsetInterval\s*\(", raw_line) and not file_has_interval_cleanup:
                cls._emit(
                    violations,
                    "RESOURCE_LEAK",
                    index,
                    lines,
                    file_path,
                    language,
                    message="setInterval sem clearInterval no arquivo.",
                )
            if re.search(r"\bsetTimeout\s*\(", raw_line) and not file_has_timeout_cleanup:
                cls._emit(
                    violations,
                    "RESOURCE_LEAK",
                    index,
                    lines,
                    file_path,
                    language,
                    message="setTimeout sem clearTimeout no arquivo.",
                )

        cls._scan_lexical_sql(lines, file_path, language, violations)

    @classmethod
    def _scan_lexical_sql(
        cls,
        lines: List[str],
        file_path: str,
        language: str,
        violations: List[TreeSitterViolation],
    ) -> None:
        """Complemento léxico de SQL para gramáticas cujas regras AST não cobrem."""
        sql_pattern = re.compile(
            r"(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|DROP\s+TABLE)"
            r"[^;\n]*(?:\$\{|%\s*\(|\"\s*\+|\'\s*\+|`\s*\+)",
            re.IGNORECASE,
        )
        for index, raw_line in enumerate(lines, 1):
            if sql_pattern.search(raw_line):
                cls._emit(
                    violations,
                    "SQL_INJECTION",
                    index,
                    lines,
                    file_path,
                    language,
                    message="Interpolação/concatenação de dado dinâmico em SQL.",
                )


__all__ = [
    "TreeSitterGuard",
    "TreeSitterViolation",
    "SQL_KEYWORDS",
]
