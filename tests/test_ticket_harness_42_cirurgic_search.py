"""TICKET-HARNESS-42 — canonical `cirurgic_search` + legacy `qmd_search` alias.

Deterministic, no mocks: real temporary workspaces, real ripgrep/grep
subprocesses and the real MCP server registration.

The test functions are intentionally synchronous and drive the coroutines with
``asyncio.run`` so the suite stays green in environments without
``pytest-asyncio`` installed.
"""

from __future__ import annotations

import asyncio
import inspect
import tempfile
from pathlib import Path

import pytest

from agent_quality_guard.mcp_server import app, cirurgic_search, qmd_search

SAMPLE_FILES = {
    "billing_service.py": (
        "def calculate_total_billing(patient_id: str, days: int) -> float:\n"
        "    # RN_FAT_009: ICU daily rate rule\n"
        "    daily_rate = 1500.0\n"
        "    multiplier = 1.0 if days <= 2 else 0.5\n"
        "    return days * daily_rate * multiplier\n"
        "\n"
        "def validate_eligibility(card_number: str) -> bool:\n"
        "    return len(card_number) == 16\n"
    ),
    "settings.conf": (
        "ENVIRONMENT=production\n"
        "SECRET_OPME_HASH=ANVISA_10332340023_STENT_KEY\n"
        "MAX_CONNECTIONS=100\n"
    ),
    "pkg_a/alpha.py": "def alpha_index_handler() -> str:\n    return 'alpha'\n",
    "pkg_b/beta.py": "def beta_index_handler() -> str:\n    return 'beta'\n",
}


@pytest.fixture()
def workspace() -> Path:
    """Real on-disk workspace with disjoint packages for scope assertions."""
    with tempfile.TemporaryDirectory(prefix="jev_ticket42_") as tmp:
        root = Path(tmp)
        for relative, content in SAMPLE_FILES.items():
            target = root / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        yield root


def _strip_volatile(payload: dict) -> dict:
    """Remove the only legitimately time-dependent field."""
    stable = dict(payload)
    stable.pop("latency_ms", None)
    return stable


def test_canonical_and_alias_are_the_same_coroutine():
    assert cirurgic_search is qmd_search
    assert inspect.iscoroutinefunction(cirurgic_search)
    assert inspect.iscoroutinefunction(qmd_search)
    assert cirurgic_search.__name__ == "_cirurgic_search_core"


def test_app_renamed_to_cirurgic_search_mcp():
    assert app.name == "cirurgic-search-mcp"


def test_both_tools_registered_with_identical_contracts():
    tools = {tool.name: tool for tool in asyncio.run(app.list_tools())}
    assert "cirurgic_search" in tools
    assert "qmd_search" in tools

    canonical = tools["cirurgic_search"]
    legacy = tools["qmd_search"]

    # Same input/output contract: no drift between the canonical name and alias.
    assert canonical.input_schema == legacy.input_schema
    assert canonical.output_schema == legacy.output_schema
    assert canonical.input_schema["required"] == ["query"]
    assert canonical.input_schema["properties"]["max_tokens"]["default"] == 1200
    assert canonical.input_schema["properties"]["depth"]["default"] == 2
    assert canonical.input_schema["properties"]["workspace"]["default"] == ""
    assert canonical.input_schema["properties"]["scope"]["default"] == ""

    # Descriptions differ only to document the migration path.
    assert "cirurgic-search-mcp" in canonical.description
    assert "Nome canonico atual" in canonical.description
    assert "Alias legado" in legacy.description
    assert "MESMA implementacao" in legacy.description


def test_real_workspace_payload_parity(workspace: Path):
    canonical = asyncio.run(
        cirurgic_search(
            query="calculate_total_billing RN_FAT_009",
            workspace=str(workspace),
            max_tokens=800,
            depth=2,
        )
    )
    legacy = asyncio.run(
        qmd_search(
            query="calculate_total_billing RN_FAT_009",
            workspace=str(workspace),
            max_tokens=800,
            depth=2,
        )
    )

    assert canonical["status"] == "success"
    assert legacy["status"] == "success"
    assert _strip_volatile(canonical) == _strip_volatile(legacy)

    first = canonical["snippets"][0]
    assert first["file"] == "billing_service.py"
    assert first["source"] == "hybrid"
    assert first["rrf_score"] > 0.0
    assert any("type:function" in relation for relation in first["relations"])
    assert any("RN_FAT_009" in match["text"] for match in first["exact_matches"])
    assert "return days * daily_rate * multiplier" in first["content"]


def test_legacy_alias_still_finds_non_ast_literals(workspace: Path):
    result = asyncio.run(
        qmd_search(
            query="ANVISA_10332340023_STENT_KEY",
            workspace=str(workspace),
            max_tokens=800,
        )
    )
    assert result["status"] == "success"
    assert result["snippets_count"] >= 1
    assert result["snippets"][0]["file"] == "settings.conf"
    assert result["snippets"][0]["source"] == "grep_literal"
    assert any(
        "SECRET_OPME_HASH" in match["text"]
        for match in result["snippets"][0]["exact_matches"]
    )


def test_both_names_honour_scope_identically(workspace: Path):
    canonical = asyncio.run(
        cirurgic_search(query="index_handler", workspace=str(workspace), scope="alpha.py")
    )
    legacy = asyncio.run(
        qmd_search(query="index_handler", workspace=str(workspace), scope="alpha.py")
    )

    assert _strip_volatile(canonical) == _strip_volatile(legacy)
    assert canonical["snippets_count"] >= 1
    assert [snippet["file"] for snippet in canonical["snippets"]] == [
        "pkg_a/alpha.py"
    ]
    assert all(
        not snippet["file"].startswith("pkg_b")
        for snippet in canonical["snippets"]
    )


def test_unknown_scope_returns_empty_payload_without_widening(workspace: Path):
    result = asyncio.run(
        cirurgic_search(
            query="calculate_total_billing",
            workspace=str(workspace),
            scope="does_not_exist",
        )
    )
    assert result["status"] == "success"
    assert result["snippets_count"] == 0
    assert result["total_tokens"] == 0
    assert result["files_found"] == 0


def test_other_canonical_tools_were_not_displaced():
    """TICKET-HARNESS-47: superfície ativa = busca cirúrgica + quality guard (com aliases)."""
    names = {tool.name for tool in asyncio.run(app.list_tools())}
    assert {
        "cirurgic_search",
        "qmd_search",
        "agent_quality_guard",
        "needle_verify_delivery",
    }.issubset(names)
    assert names.isdisjoint({"openjev_choice", "openjev_noul", "openjev_consulta"})
