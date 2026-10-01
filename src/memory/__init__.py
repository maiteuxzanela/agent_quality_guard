"""Camada de Memória & Busca Cirúrgica (Needle 3).
Suporta operação local (LocalNeedleEngine) ou remota via Cactus Compute API.
"""

from .local_needle import LocalNeedleEngine
from .needle_client import Needle3Client
from .needle_ast_guard import NeedleASTGuard, ASTViolation
from .needle_defect_screener import NeedleASTScreener, screen_symbol_defects, screen_workspace_symbols

__all__ = [
    "Needle3Client",
    "LocalNeedleEngine",
    "NeedleASTGuard",
    "ASTViolation",
    "NeedleASTScreener",
    "screen_symbol_defects",
    "screen_workspace_symbols",
]
