"""Módulo de contratos de dados, schemas e definições de ferramentas."""

from .schemas import (
    NoulRequest,
    NoulResponse,
    ChoiceRequest,
    ChoiceResponse,
    ScoreRequest,
    ScoreResponse,
    Needle3Query,
    Needle3Snippet,
    Needle3Response,
    OpenJevError,
    OpenJevNotReadyError,
    HardwareUnavailableError,
    InferenceError,
)
from .tools import TOOL_DEFINITIONS, get_tool_definition

__all__ = [
    "NoulRequest",
    "NoulResponse",
    "ChoiceRequest",
    "ChoiceResponse",
    "ScoreRequest",
    "ScoreResponse",
    "Needle3Query",
    "Needle3Snippet",
    "Needle3Response",
    "OpenJevError",
    "OpenJevNotReadyError",
    "HardwareUnavailableError",
    "InferenceError",
    "TOOL_DEFINITIONS",
    "get_tool_definition",
]
