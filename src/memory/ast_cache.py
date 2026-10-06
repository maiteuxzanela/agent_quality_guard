"""Módulo de cache em memória para ASTs Python compartilhadas entre Guard e Screener.

Evita parses duplicados da mesma árvore sintática durante as etapas do pipeline
Needle (Layer 1 NeedleASTGuard, Layer 2 NeedleDefectScreener e ASTDiffExtractor).
"""

from __future__ import annotations

import ast
import hashlib
import logging
import threading
from collections import OrderedDict
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

_MAX_CACHE_ENTRIES = 512
_AST_CACHE: OrderedDict[str, Optional[ast.AST]] = OrderedDict()
_CACHE_LOCK = threading.Lock()


def get_cached_ast(source: str, filename: str = "<unknown>") -> Optional[ast.AST]:
    """Obtém ou compila e armazena em cache a AST do código Python fornecido.

    Retorna None se houver erro de sintaxe (SyntaxError).
    Operação thread-safe com limite de _MAX_CACHE_ENTRIES.
    A chave é estritamente o hash SHA-256 do conteúdo, garantindo compartilhamento
    universal de AST entre caminhos relativos, absolutos e anônimos.

    Exemplo:
        >>> tree1 = get_cached_ast("x = 1", "test.py")
        >>> tree2 = get_cached_ast("x = 1", "/full/path/test.py")
        >>> tree1 is tree2
        True
    """
    if not source.strip():
        return None

    # Chave invariante baseada no hash do conteúdo
    content_hash = hashlib.sha256(source.encode("utf-8", errors="replace")).hexdigest()

    with _CACHE_LOCK:
        if content_hash in _AST_CACHE:
            _AST_CACHE.move_to_end(content_hash)
            return _AST_CACHE[content_hash]

    # Parsing fora do lock para não bloquear leituras concorrentes
    try:
        tree: Optional[ast.AST] = ast.parse(source, filename=filename)
    except SyntaxError as e:
        logger.debug(f"SyntaxError ao analisar AST de {filename}: {e}")
        tree = None
    except Exception as e:
        logger.debug(f"Erro inesperado ao analisar AST de {filename}: {e}")
        tree = None

    with _CACHE_LOCK:
        _AST_CACHE[content_hash] = tree
        _AST_CACHE.move_to_end(content_hash)
        if len(_AST_CACHE) > _MAX_CACHE_ENTRIES:
            _AST_CACHE.popitem(last=False)

    return tree


def clear_ast_cache() -> None:
    """Limpa todo o cache de ASTs em memória.

    Exemplo:
        >>> clear_ast_cache()
        >>> get_ast_cache_size()
        0
    """
    with _CACHE_LOCK:
        _AST_CACHE.clear()


def get_ast_cache_size() -> int:
    """Retorna o número de entradas ativas no cache.

    Exemplo:
        >>> isinstance(get_ast_cache_size(), int)
        True
    """
    with _CACHE_LOCK:
        return len(_AST_CACHE)
