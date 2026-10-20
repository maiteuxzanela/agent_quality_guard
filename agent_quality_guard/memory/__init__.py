"""Camada de Memória & Busca Cirúrgica (Needle 3).
Suporta operação local (LocalNeedleEngine) ou remota via Cactus Compute API.
"""

from typing import Any

from .local_needle import LocalNeedleEngine
from .needle_client import Needle3Client
from .needle_ast_guard import NeedleASTGuard, ASTViolation
from .needle_defect_screener import NeedleASTScreener, screen_symbol_defects, screen_workspace_symbols

TreeSitterGuard: Any
TreeSitterViolation: Any

try:  # Camada 2 é opcional: uma gramática/dependência ausente nunca derruba o pacote
    from .tree_sitter_guard import (
        TreeSitterGuard as _TreeSitterGuard,
        TreeSitterViolation as _TreeSitterViolation,
    )
    TreeSitterGuard = _TreeSitterGuard
    TreeSitterViolation = _TreeSitterViolation
    _TREE_SITTER_EXPORTS = True
except Exception:  # pragma: no cover - fallback gracioso
    TreeSitterGuard = None
    TreeSitterViolation = None
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
