"""Módulo de cache em memória para ASTs Python compartilhadas entre Guard e Screener.

Evita parses duplicados da mesma árvore sintática durante as etapas do pipeline
Needle (Layer 1 NeedleASTGuard, Layer 2 NeedleDefectScreener e ASTDiffExtractor).

TICKET-SGA-04 — Performance & Guardrails de I/O:
* ``FileIndexCache``: cache LRU thread-safe por ``(path, st_mtime_ns, st_size)``
  para o SymbolIndex (blocos AST + token sets) do Needle 3 — reutiliza índice de
  arquivos inalterados entre buscas consecutivas e invalida de forma transparente
  quando mtime ou size mudam.
* Teto individual de arquivo (``MAX_INDEX_FILE_BYTES`` = 2 MB) para indexação de
  símbolos / extração de AST / preview, evitando picos de memória.
"""

from __future__ import annotations

import ast
import hashlib
import logging
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, Optional, Set, Tuple, Union

logger = logging.getLogger(__name__)

_MAX_CACHE_ENTRIES = 512
_AST_CACHE: OrderedDict[str, Optional[ast.AST]] = OrderedDict()
_CACHE_LOCK = threading.Lock()

# ──────────────────────────────────────────────────────────────────────────────
# TICKET-SGA-04 — Guardrail de I/O: teto de tamanho individual de arquivo.
# Arquivos maiores que isso (bundles, dumps, binários disfarçados) nunca entram
# na indexação de símbolos nem na extração de AST/preview.
# ──────────────────────────────────────────────────────────────────────────────
MAX_INDEX_FILE_BYTES = 2 * 1024 * 1024  # 2 MB

# Chave de invalidação por estatística de arquivo: caminho absoluto + mtime em
# nanosegundos + tamanho. Qualquer mudança de mtime OU de size troca a chave.
FileStatKey = Tuple[str, int, int]

# Aviso único por caminho para não inundar o log em buscas repetidas.
_OVERSIZE_WARNED: Set[str] = set()
_OVERSIZE_WARNED_CAP = 4096


def _warn_oversize_once(resolved_path: str, size: int, purpose: str) -> None:
    """Registra UMA vez (por caminho) o descarte de arquivo acima do teto."""
    with _CACHE_LOCK:
        if resolved_path in _OVERSIZE_WARNED:
            return
        if len(_OVERSIZE_WARNED) >= _OVERSIZE_WARNED_CAP:
            _OVERSIZE_WARNED.clear()
        _OVERSIZE_WARNED.add(resolved_path)
    logger.warning(
        "Guardrail de I/O: arquivo de %.1f KB excede o teto de %d bytes (%s) e foi "
        "ignorado — reduza o arquivo ou remova-o do escopo para indexá-lo.",
        size / 1024.0,
        MAX_INDEX_FILE_BYTES,
        purpose,
    )


def file_size_allowed(file_path: Union[str, Path], purpose: str = "indexacao") -> bool:
    """Verifica se o arquivo respeita o teto de indexação (2 MB).

    Retorna ``False`` para arquivo inexistente/inacessível (falha é registrada em
    debug, nunca propaga) e para arquivo acima de ``MAX_INDEX_FILE_BYTES`` (com
    aviso único por caminho).

    Exemplo:
        >>> from pathlib import Path
        >>> import tempfile, os
        >>> p = Path(tempfile.mkdtemp()) / "tiny.py"
        >>> p.write_text("x = 1")
        >>> file_size_allowed(p)
        True
    """
    try:
        st = Path(file_path).stat()
    except OSError as stat_err:
        logger.debug("Guardrail de I/O: stat falhou em %s: %s", file_path, stat_err)
        return False
    if st.st_size > MAX_INDEX_FILE_BYTES:
        _warn_oversize_once(str(Path(file_path).resolve()), st.st_size, purpose)
        return False
    return True


def file_stat_key(file_path: Union[str, Path]) -> Optional[FileStatKey]:
    """Monta a chave de cache ``(caminho, st_mtime_ns, st_size)`` de um arquivo.

    Retorna ``None`` quando o arquivo não existe, não é estatível ou excede o
    teto de indexação (guardrail de I/O — nesses casos nada deve ser indexado).

    Exemplo:
        >>> import tempfile
        >>> from pathlib import Path
        >>> p = Path(tempfile.mkdtemp()) / "a.py"
        >>> p.write_text("def f():\\n    return 1\\n")
        >>> key = file_stat_key(p)
        >>> key is not None and key[1] >= 0 and key[2] > 0
        True
    """
    path = Path(file_path)
    if not file_size_allowed(path, purpose="chave de cache"):
        return None
    try:
        st = path.stat()
    except OSError as stat_err:
        logger.debug("Guardrail de I/O: stat falhou em %s: %s", path, stat_err)
        return None
    return (str(path.resolve()), st.st_mtime_ns, st.st_size)


class FileIndexCache:
    """LRU thread-safe por chave de estatística de arquivo, com orçamento de bytes.

    Diferente do cache de AST por hash de conteúdo, esta estrutura indexa por
    ``(path, mtime_ns, size)``: a invalidação é transparente — qualquer edição do
    arquivo produz outra chave e a entrada antiga é descartada pelo LRU.
    """

    def __init__(self, max_entries: int = 2048, max_total_bytes: int = 64 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_total_bytes = max_total_bytes
        self._entries: "OrderedDict[FileStatKey, Tuple[Any, int]]" = OrderedDict()
        self._lock = threading.Lock()
        self._hits = 0
        self._misses = 0
        self._evictions = 0
        self._total_bytes = 0

    def get(self, key: FileStatKey) -> Optional[Any]:
        """Consulta o cache; retorna ``None`` em miss (thread-safe)."""
        with self._lock:
            item = self._entries.get(key)
            if item is None:
                self._misses += 1
                return None
            self._entries.move_to_end(key)
            self._hits += 1
            return item[0]

    def set(self, key: FileStatKey, value: Any, size_bytes: int = 0) -> None:
        """Armazena o valor associado à chave, respeitando entradas e bytes.

        ``size_bytes`` é uma estimativa do custo de memória do valor; ao exceder
        o orçamento, as entradas mais antigas são evictadas (LRU).
        """
        size_bytes = max(0, int(size_bytes))
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._total_bytes -= old[1]
            self._entries[key] = (value, size_bytes)
            self._total_bytes += size_bytes
            while self._entries and (
                len(self._entries) > self.max_entries
                or self._total_bytes > self.max_total_bytes
            ):
                _evicted_key, (_evicted_value, evicted_size) = self._entries.popitem(
                    last=False
                )
                self._total_bytes -= evicted_size
                self._evictions += 1

    def clear(self) -> None:
        """Descarta todas as entradas e zera contadores."""
        with self._lock:
            self._entries.clear()
            self._total_bytes = 0
            self._hits = 0
            self._misses = 0
            self._evictions = 0

    def stats(self) -> Dict[str, int]:
        """Retorna contadores observáveis (entries/hits/misses/evictions/bytes)."""
        with self._lock:
            return {
                "entries": len(self._entries),
                "hits": self._hits,
                "misses": self._misses,
                "evictions": self._evictions,
                "bytes": self._total_bytes,
            }


# Instância global compartilhada entre instâncias de motor (o MCP cria um motor
# novo por requisição — o cache precisa sobreviver ao objeto do engine).
_FILE_INDEX_CACHE = FileIndexCache()


def get_file_index(key: FileStatKey) -> Optional[Any]:
    """Consulta o cache global do SymbolIndex por chave mtime/size."""
    return _FILE_INDEX_CACHE.get(key)


def set_file_index(key: FileStatKey, value: Any, size_bytes: int = 0) -> None:
    """Armazena valor no cache global do SymbolIndex por chave mtime/size."""
    _FILE_INDEX_CACHE.set(key, value, size_bytes=size_bytes)


def clear_file_index_cache() -> None:
    """Limpa o cache global do SymbolIndex (usado em testes e reloads)."""
    _FILE_INDEX_CACHE.clear()
    with _CACHE_LOCK:
        _OVERSIZE_WARNED.clear()


def get_file_cache_stats() -> Dict[str, int]:
    """Retorna contadores do cache global (hits/misses/entries/bytes)."""
    return _FILE_INDEX_CACHE.stats()


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
