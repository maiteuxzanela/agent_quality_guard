"""Camada de Memória & Busca Cirúrgica (Needle 3).
Suporta operação local (LocalNeedleEngine) ou remota via Cactus Compute API.
"""

from .local_needle import LocalNeedleEngine
from .needle_client import Needle3Client

__all__ = ["Needle3Client", "LocalNeedleEngine"]
