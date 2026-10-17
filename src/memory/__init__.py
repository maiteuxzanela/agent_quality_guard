"""Camada de Memória & Busca Cirúrgica (Needle 3).
Suporta operação local (LocalNeedleEngine) ou remota via Cactus Compute API.
"""

from .local_needle import LocalNeedleEngine
from .needle_client import Needle3Client
from .needle_ast_guard import NeedleASTGuard, ASTViolation
from .needle_defect_screener import NeedleASTScreener, screen_symbol_defects, screen_workspace_symbols

try:  # Camada 2 é opcional: uma gramática/dependência ausente nunca derruba o pacote
    from .tree_sitter_guard import TreeSitterGuard, TreeSitterViolation
    _TREE_SITTER_EXPORTS = True
except Exception:  # pragma: no cover - fallback gracioso
    TreeSitterGuard = None  # type: ignore[assignment]
    TreeSitterViolation = None  # type: ignore[assignment]
    _TREE_SITTER_EXPORTS = False

__all__ = [
    "Needle3Client",
    "LocalNeedleEngine",
    "NeedleASTGuard",
    "ASTViolation",
    "NeedleASTScreener",
    "screen_symbol_defects",
    "screen_workspace_symbols",
    "TreeSitterGuard",
    "TreeSitterViolation",
]
