"""TICKET-HARNESS-47 — descontinuação do Laya/RLCD no MCP do Projeto JEV.

Suíte determinística (sem mocks) que comprova, contra o código real:

1. o boot do módulo do servidor MCP não importa PyTorch/Transformers/Laya nem
   mapeia bibliotecas CUDA/NVIDIA no processo (< 200 ms de custo próprio);
2. a superfície MCP contém apenas as ferramentas determinísticas canônicas
   (``cirurgic_search``/``agent_quality_guard``) e seus aliases legados;
3. ``openjev_noul``/``openjev_choice``/``openjev_consulta`` não existem mais
   (chamada real → ``ToolError: Unknown tool``);
4. as ferramentas canônicas são funcionais de ponta a ponta e o alias devolve
   o mesmo contrato da ferramenta canônica;
5. o processo importado não aparece como cliente CUDA em ``nvidia-smi``.

Os testes de subprocesso usam o interpretador atual (``sys.executable``), de
modo que a suíte roda dentro do virtualenv do projeto sem depender de PATH.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import pytest

PROJECT_DIR = Path(__file__).resolve().parent.parent
MCP_SERVER_PATH = PROJECT_DIR / "src" / "mcp_server.py"

CANONICAL_TOOLS = {
    "cirurgic_search",
    "qmd_search",
    "agent_quality_guard",
    "needle_verify_delivery",
}
FORBIDDEN_MODULE_PREFIXES = (
    "torch",
    "transformers",
    "laya",
    "pynvml",
    "cupy",
    "src.decision",
)


def _run_python(code: str, timeout: int = 180) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_DIR),
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _forbidden_modules(module_names: List[str]) -> List[str]:
    bad = []
    for name in module_names:
        if any(
            name == prefix or name.startswith(prefix + ".")
            for prefix in FORBIDDEN_MODULE_PREFIXES
        ):
            bad.append(name)
    return bad


# ---------------------------------------------------------------------------
# 1. Boot: sem ML/Laya, sem VRAM, custo próprio < 200 ms
# ---------------------------------------------------------------------------
BOOT_PROBE = r"""
import json
import sys
import time

t0 = time.perf_counter()
import mcp.server.mcpserver  # framework MCP (baseline, não é código do JEV)
t1 = time.perf_counter()
import src.mcp_server as mcp_server
t2 = time.perf_counter()

import asyncio
tool_names = [tool.name for tool in asyncio.run(mcp_server.app.list_tools())]

maps = ""
try:
    maps = open("/proc/self/maps", encoding="utf-8", errors="ignore").read()
except OSError:
    pass

print(json.dumps({
    "baseline_ms": (t1 - t0) * 1000.0,
    "jev_ms": (t2 - t1) * 1000.0,
    "tools": tool_names,
    "modules": sorted(sys.modules),
    "cuda_mapped": ("libcuda.so" in maps) or ("libnvidia" in maps),
}, ensure_ascii=False))
"""


def test_boot_does_not_import_ml_and_stays_under_200ms():
    """Três amostras reais: nenhuma importa ML e o custo do JEV fica < 200 ms."""
    samples: List[Dict[str, Any]] = []
    for _ in range(3):
        proc = _run_python(BOOT_PROBE, timeout=180)
        assert proc.returncode == 0, proc.stderr[-2000:]
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
        samples.append(payload)

        bad = _forbidden_modules(payload["modules"])
        assert bad == [], f"Módulos proibidos importados no boot: {bad}"
        assert payload["cuda_mapped"] is False, (
            "O processo mapeou libcuda/libnvidia no boot do MCP (VRAM/driver "
            "carregados indevidamente)."
        )

    timings = [sample["jev_ms"] for sample in samples]
    assert all(ms < 200.0 for ms in timings), (
        "Boot próprio do MCP acima de 200 ms: " + json.dumps(timings)
    )
    # Sanidade: o framework MCP é o custo dominante; o código do JEV não é.
    assert all(sample["jev_ms"] < sample["baseline_ms"] for sample in samples)
    assert samples[-1]["tools"] and set(samples[-1]["tools"]) == CANONICAL_TOOLS


def test_no_gpu_compute_process_after_import():
    """O processo que importa o MCP não aparece como cliente CUDA."""
    code = (
        "import sys, time\n"
        "import src.mcp_server\n"
        "print('READY', flush=True)\n"
        "time.sleep(2.5)\n"
    )
    child = subprocess.Popen(
        [sys.executable, "-c", code],
        cwd=str(PROJECT_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        ready = child.stdout.readline().strip() if child.stdout else ""
        assert ready == "READY", (
            "O processo de prova não chegou ao estado READY: "
            + (child.stderr.read()[-1000:] if child.stderr else "")
        )

        maps_path = Path("/proc") / str(child.pid) / "maps"
        if maps_path.exists():
            maps = maps_path.read_text(errors="ignore")
            assert "libcuda.so" not in maps
            assert "libnvidia" not in maps

        nvidia_smi = shutil.which("nvidia-smi")
        if nvidia_smi:
            query = subprocess.run(
                [
                    nvidia_smi,
                    "--query-compute-apps=pid,used_memory",
                    "--format=csv,noheader",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert query.returncode == 0, query.stderr[-1000:]
            compute_pids = {
                line.split(",")[0].strip()
                for line in query.stdout.splitlines()
                if line.strip()
            }
            assert str(child.pid) not in compute_pids, (
                "nvidia-smi listou o processo do MCP como cliente CUDA: "
                + query.stdout
            )
    finally:
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait(timeout=10)


# ---------------------------------------------------------------------------
# 2. Superfície de ferramentas: canônicas presentes, primitivas ML ausentes
# ---------------------------------------------------------------------------
def _list_tool_names() -> List[str]:
    import src.mcp_server as mcp_server

    return [tool.name for tool in asyncio.run(mcp_server.app.list_tools())]


def test_tool_surface_is_exactly_the_deterministic_set():
    names = _list_tool_names()
    assert set(names) == CANONICAL_TOOLS
    assert len(names) == len(set(names)), f"Ferramentas duplicadas: {names}"

    forbidden = {
        name
        for name in names
        if name.startswith("openjev_")
        or name in {"openjev_noul", "openjev_choice", "openjev_consulta"}
    }
    assert forbidden == set(), f"Ferramentas ML/Laya ainda expostas: {forbidden}"


@pytest.mark.parametrize(
    "legacy_tool",
    ["openjev_noul", "openjev_choice", "openjev_consulta"],
)
def test_retired_ml_tools_are_unknown(legacy_tool: str):
    from mcp.server.mcpserver.exceptions import ToolError

    import src.mcp_server as mcp_server

    with pytest.raises(ToolError, match="Unknown tool"):
        asyncio.run(mcp_server.app.call_tool(legacy_tool, {}))


def test_source_has_no_ml_imports():
    tree = ast.parse(MCP_SERVER_PATH.read_text(encoding="utf-8"))
    imported: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)

    bad = [
        name
        for name in imported
        if name == "torch"
        or name.startswith("torch.")
        or name.split(".")[0] in {"transformers", "laya", "pynvml", "cupy"}
        or name.startswith("src.decision")
    ]
    assert bad == [], f"Imports ML/Laya no mcp_server.py: {bad}"


def test_get_engine_tombstone_fails_fast():
    import src.mcp_server as mcp_server

    with pytest.raises(RuntimeError, match="TICKET-HARNESS-47"):
        mcp_server.get_engine()


# ---------------------------------------------------------------------------
# 3. Funcionalidade real das ferramentas canônicas
# ---------------------------------------------------------------------------
def _search_payload(result: Any) -> Dict[str, Any]:
    assert result.is_error is False
    assert result.content, "Tool não retornou conteúdo"
    return json.loads(result.content[0].text)


def test_cirurgic_search_is_functional_and_alias_has_same_contract(tmp_path: Path):
    import src.mcp_server as mcp_server

    workspace = tmp_path / "ws"
    workspace.mkdir()
    (workspace / "sample.py").write_text(
        "def soma_total(a, b):\n    return a + b\n", encoding="utf-8"
    )

    async def call(name: str) -> Dict[str, Any]:
        result = await mcp_server.app.call_tool(
            name,
            {
                "query": "soma_total",
                "workspace": str(workspace),
                "max_tokens": 400,
            },
        )
        return _search_payload(result)

    canonical = asyncio.run(call("cirurgic_search"))
    alias = asyncio.run(call("qmd_search"))

    for payload in (canonical, alias):
        assert payload["status"] == "success"
        assert payload["files_found"] == 1
        assert payload["snippets_count"] >= 1
        assert payload["snippets"][0]["file"] == "sample.py"
        assert "soma_total" in payload["snippets"][0]["content"]

    # Alias legado: MESMO contrato (latência é telemetria volátil).
    assert canonical["snippets"] == alias["snippets"]
    assert canonical["files_found"] == alias["files_found"]
    assert canonical["total_tokens"] == alias["total_tokens"]


def test_agent_quality_guard_is_functional_without_laya(monkeypatch, tmp_path: Path):
    import src.mcp_server as mcp_server

    # Estado do circuit breaker isolado por teste (anti-wipe do histórico real).
    monkeypatch.setenv("JEV_STATE_DIR", str(tmp_path / "state"))
    workspace = tmp_path / "guard_ws"
    workspace.mkdir()

    def call() -> Dict[str, Any]:
        result = asyncio.run(
            mcp_server.app.call_tool("agent_quality_guard", {"workspace": str(workspace)})
        )
        assert result.is_error is False
        return json.loads(result.content[0].text)

    payload = call()
    assert payload["tool_name"] == "agent_quality_guard"
    assert payload["status"] in {"APPROVED", "REJECTED", "EMERGENCY_RELEASE"}
    # Buckets arbitrados pelo Laya/Noul foram aposentados e ficam vazios.
    assert payload["confirmed_defects"] == []
    assert payload["dismissed_warnings"] == []

    # O parâmetro `engine` legado é aceito e IGNORADO (retrocompatibilidade).
    with_engine = asyncio.run(
        mcp_server.app.call_tool(
            "agent_quality_guard",
            {"workspace": str(workspace), "engine": "legacy-laya-ignored"},
        )
    )
    assert with_engine.is_error is False
    assert json.loads(with_engine.content[0].text)["status"] == payload["status"]
