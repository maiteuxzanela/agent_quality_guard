"""Testes reais do motor de busca híbrido QMD (Ripgrep + Needle 3 + RRF).

Validação estrita SEM MOCKS utilizando sistema de arquivos e subprocessos reais.
"""

import asyncio
import os
import tempfile
from pathlib import Path

import pytest

from agent_quality_guard.memory.qmd_search import QmdHybridSearchEngine, find_rg_binary
from agent_quality_guard.mcp_server import qmd_search


@pytest.fixture
def sample_workspace():
    """Cria workspace real temporário com múltiplos arquivos para teste de busca híbrida."""
    with tempfile.TemporaryDirectory(prefix="test_qmd_ws_") as tmp_dir:
        ws = Path(tmp_dir)

        # Arquivo 1: Módulo Python com funções e regras
        py_file1 = ws / "billing_service.py"
        py_file1.write_text(
            "import os\n\n"
            "def calculate_total_billing(patient_id: str, days: int) -> float:\n"
            "    # RN_FAT_009: regra de valoracao de diarias de UTI\n"
            "    daily_rate = 1500.0\n"
            "    multiplier = 1.0 if days <= 2 else 0.5\n"
            "    return days * daily_rate * multiplier\n\n"
            "def validate_eligibility(card_number: str) -> bool:\n"
            "    return len(card_number) == 16\n",
            encoding="utf-8",
        )

        # Arquivo 2: Arquivo de texto/config com literais sem AST
        cfg_file = ws / "settings.conf"
        cfg_file.write_text(
            "ENVIRONMENT=production\n"
            "SECRET_OPME_HASH=ANVISA_10332340023_STENT_KEY\n"
            "MAX_CONNECTIONS=100\n",
            encoding="utf-8",
        )

        yield ws


def test_find_rg_binary_available():
    """Verifica se o binário do ripgrep ou fallback foi localizado."""
    rg = find_rg_binary()
    assert rg is not None
    assert os.path.exists(rg)


@pytest.mark.asyncio
async def test_qmd_search_hybrid_intersection(sample_workspace: Path):
    """Testa busca híbrida onde Grep e Needle 3 convergem no mesmo arquivo e nó AST."""
    engine = QmdHybridSearchEngine(workspace_root=str(sample_workspace))
    res = await engine.search(
        query="calculate_total_billing RN_FAT_009",
        max_tokens=800,
    )

    assert res["status"] == "success"
    assert res["snippets_count"] >= 1
    assert res["files_found"] >= 1

    first_snip = res["snippets"][0]
    assert first_snip["file"] == "billing_service.py"
    assert first_snip["source"] == "hybrid"
    assert first_snip["rrf_score"] > 0.0
    assert any("type:function" in r for r in first_snip["relations"])
    assert any("RN_FAT_009" in m["text"] for m in first_snip["exact_matches"])


@pytest.mark.asyncio
async def test_qmd_search_fallback_grep_literal(sample_workspace: Path):
    """Refinamento 1: Fallback assimétrico para literais não indexados em AST Python."""
    engine = QmdHybridSearchEngine(workspace_root=str(sample_workspace))
    res = await engine.search(
        query="ANVISA_10332340023_STENT_KEY",
        max_tokens=800,
    )

    assert res["status"] == "success"
    assert res["snippets_count"] >= 1
    snip = res["snippets"][0]
    assert snip["file"] == "settings.conf"
    assert snip["source"] == "grep_literal"
    assert any("SECRET_OPME_HASH" in m["text"] for m in snip["exact_matches"])


@pytest.mark.asyncio
async def test_qmd_search_mcp_tool(sample_workspace: Path):
    """Testa invocação da tool MCP qmd_search diretamente."""
    res = await qmd_search(
        query="validate_eligibility",
        workspace=str(sample_workspace),
        max_tokens=600,
    )

    assert isinstance(res, dict)
    assert res["status"] == "success"
    assert res["snippets_count"] >= 1
    assert res["snippets"][0]["file"] == "billing_service.py"
    assert "validate_eligibility" in res["snippets"][0]["content"]


def test_qmd_search_estimate_tokens_type_error():
    """Valida tratamento de exceção com pytest.raises para tipo inválido em _estimate_tokens."""
    engine = QmdHybridSearchEngine()
    with pytest.raises(TypeError):
        engine._estimate_tokens(None)  # type: ignore
