"""TICKET-SGA-04 — Performance & Guardrails de I/O da busca cirúrgica.

Validação 100% determinística SEM MOCKS: sistema de arquivos real, subprocessos
reais (ripgrep) e threads reais cobrindo:

1. Cache em memória do SymbolIndex/blocos AST por ``(path, st_mtime_ns, st_size)``
   com invalidação transparente e operação thread-safe.
2. Buscas consecutivas ultrarrápidas (< 50 ms) em workspace estável.
3. Teto individual de arquivo (2 MB) para indexação/AST/preview com aviso.
4. Timeout global configurável por requisição (padrão 15 s) na busca híbrida.
5. Retrocompatibilidade da assinatura de ``cirurgic_search``/``qmd_search``.
"""

import inspect
import logging
import os
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import List

import pytest

from agent_quality_guard.core.tools import get_tool_definition
from agent_quality_guard.memory.ast_cache import (
    MAX_INDEX_FILE_BYTES,
    FileIndexCache,
    file_size_allowed,
    file_stat_key,
    get_file_cache_stats,
)
from agent_quality_guard.memory.local_needle import LocalNeedleEngine
from agent_quality_guard.memory.qmd_search import (
    DEFAULT_RIPGREP_TIMEOUT_S,
    DEFAULT_SEARCH_TIMEOUT_S,
    MAX_LITERAL_PREVIEW_CHARS,
    QmdHybridSearchEngine,
)

SAMPLE_PY = (
    "import os\n\n"
    "def calculate_total_billing(patient_id: str, days: int) -> float:\n"
    "    # RN_FAT_009: regra de valoracao de diarias de UTI\n"
    "    daily_rate = 1500.0\n"
    "    return days * daily_rate\n\n"
    "def validate_eligibility(card_number: str) -> bool:\n"
    "    return len(card_number) == 16\n"
)


@pytest.fixture
def stable_workspace():
    """Workspace real e estável (arquivos inalterados entre buscas)."""
    with tempfile.TemporaryDirectory(prefix="sga04_stable_") as tmp_dir:
        ws = Path(tmp_dir)
        (ws / "billing_service.py").write_text(SAMPLE_PY, encoding="utf-8")
        (ws / "notes.md").write_text("# RN_FAT_009 documentacao\n", encoding="utf-8")
        yield ws


# ──────────────────────────────────────────────────────────────────────────────
# 1. Chave de cache por mtime/size
# ──────────────────────────────────────────────────────────────────────────────


def test_file_stat_key_invalidates_on_mtime_or_size_change(tmp_path: Path):
    """A chave muda com mtime OU size — invalidação transparente do cache."""
    f = tmp_path / "mod.py"
    f.write_text("def alpha():\n    return 1\n", encoding="utf-8")
    key1 = file_stat_key(f)
    assert key1 is not None

    # Mudança SOMENTE de mtime (conteúdo idêntico, tamanho igual).
    st = f.stat()
    os.utime(f, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))
    key2 = file_stat_key(f)
    assert key2 is not None and key2 != key1
    assert key2[0] == key1[0] and key2[2] == key1[2]

    # Mudança de tamanho.
    f.write_text("def alpha():\n    return 1\n    # linha extra\n", encoding="utf-8")
    key3 = file_stat_key(f)
    assert key3 is not None and key3 != key2
    assert key3[2] > key2[2]


def test_file_stat_key_rejects_oversized_and_missing(tmp_path: Path):
    """Arquivo > 2 MB e arquivo inexistente nunca geram chave de indexação."""
    big = tmp_path / "huge.py"
    big.write_bytes(b"x = 1\n" * (MAX_INDEX_FILE_BYTES // 6 + 8))
    assert big.stat().st_size > MAX_INDEX_FILE_BYTES
    assert file_stat_key(big) is None
    assert file_size_allowed(big) is False

    assert file_stat_key(tmp_path / "nao_existe.py") is None
    assert file_size_allowed(tmp_path / "nao_existe.py") is False


# ──────────────────────────────────────────────────────────────────────────────
# 2. SymbolIndex cacheado (blocos AST + token sets) no LocalNeedleEngine
# ──────────────────────────────────────────────────────────────────────────────


def test_indexed_file_symbols_cached_and_defensive(tmp_path: Path):
    """Blocos + token sets são servidos do cache e não podem ser corrompidos."""
    f = tmp_path / "svc.py"
    f.write_text(SAMPLE_PY, encoding="utf-8")
    engine = LocalNeedleEngine(workspace_root=str(tmp_path))

    blocks1, tokens1 = engine._indexed_file_symbols(f)
    assert blocks1 and tokens1
    names1 = {b["name"] for b in blocks1}
    assert {"calculate_total_billing", "validate_eligibility"} <= names1

    # Reaproveitamento: mesmo índice sem novo parse/tokenização.
    blocks2, tokens2 = engine._indexed_file_symbols(f)
    assert [b["name"] for b in blocks1] == [b["name"] for b in blocks2]
    assert all(t1 == t2 for t1, t2 in zip(tokens1, tokens2))

    # Cópia defensiva: mutação do chamador não contamina o cache global.
    blocks1[0]["content"] = "CORROMPIDO"
    blocks1[0]["relations"].append("injected:1")
    blocks3, _ = engine._indexed_file_symbols(f)
    assert all(b["content"] != "CORROMPIDO" for b in blocks3)
    assert all("injected:1" not in b["relations"] for b in blocks3)

    # Cópia defensiva também no HIT: mutar o objeto devolvido pelo cache não
    # pode envenenar a entrada compartilhada entre buscas/threads.
    blocks_hit, _ = engine._indexed_file_symbols(f)
    blocks_hit[0]["content"] = "HIT_CORROMPIDO"
    blocks_hit[0]["relations"].append("hit-injected:1")
    blocks4, _ = engine._indexed_file_symbols(f)
    assert all(b["content"] != "HIT_CORROMPIDO" for b in blocks4)
    assert all("hit-injected:1" not in b["relations"] for b in blocks4)
    assert [b["name"] for b in blocks4] == [b["name"] for b in blocks1]


def test_indexed_file_symbols_reextracts_after_edit(tmp_path: Path):
    """Edição real do arquivo invalida o cache e o novo conteúdo é indexado."""
    f = tmp_path / "svc.py"
    f.write_text("def alpha_one():\n    return 1\n", encoding="utf-8")
    engine = LocalNeedleEngine(workspace_root=str(tmp_path))

    blocks1, _ = engine._indexed_file_symbols(f)
    assert {b["name"] for b in blocks1} == {"alpha_one"}

    f.write_text(
        "def beta_two():\n    return 2\n    # conteudo cresceu\n", encoding="utf-8"
    )
    blocks2, tokens2 = engine._indexed_file_symbols(f)
    names2 = {b["name"] for b in blocks2}
    assert "beta_two" in names2 and "alpha_one" not in names2
    assert any("beta_two" in tok for ts in tokens2 for tok in ts)


def test_file_index_cache_thread_safe_with_budgets():
    """8 threads × 300 operações: sem corrida, LRU respeita entradas e bytes."""
    cache = FileIndexCache(max_entries=64, max_total_bytes=100_000)
    errors: List[BaseException] = []

    def worker(worker_id: int) -> None:
        try:
            for j in range(300):
                key = (f"/tmp/sga04_w{worker_id % 8}.py", j, 1024 + j)
                cache.set(key, {"worker": worker_id, "j": j}, size_bytes=2_000)
                cache.get(key)
        except BaseException as exc:  # pragma: no cover - só em falha real
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(worker, range(8)))

    assert errors == []
    stats = cache.stats()
    assert stats["entries"] <= 64
    assert stats["bytes"] <= 100_000
    assert stats["hits"] + stats["misses"] >= 8 * 300
    assert stats["evictions"] > 0
    assert cache.get(("nunca", 0, 0)) is None


# ──────────────────────────────────────────────────────────────────────────────
# 3. Guardrail de tamanho de arquivo (2 MB) com aviso
# ──────────────────────────────────────────────────────────────────────────────


def test_oversized_file_is_skipped_with_warning(tmp_path: Path, caplog):
    """Arquivo > 2 MB: extração de AST/indexação retorna [] + log de aviso."""
    big = tmp_path / "generated_huge.py"
    line = "def generated_function():\n    return 1\n\n"
    big.write_bytes(line.encode("utf-8") * (MAX_INDEX_FILE_BYTES // len(line) + 4))
    assert big.stat().st_size > MAX_INDEX_FILE_BYTES

    engine = LocalNeedleEngine(workspace_root=str(tmp_path))
    with caplog.at_level(logging.WARNING):
        assert engine._extract_blocks_for_file(big) == []
        assert engine._indexed_file_symbols(big) == ([], [])

    warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
    assert any("excede o teto" in w for w in warnings), warnings


@pytest.mark.asyncio
async def test_search_workspace_with_oversized_file_still_succeeds(
    tmp_path: Path, caplog
):
    """Workspace com arquivo gigante: busca segue íntegra e o gigante não indexa."""
    big = tmp_path / "generated_huge.py"
    line = "def generated_function():\n    return 1\n\n"
    big.write_bytes(line.encode("utf-8") * (MAX_INDEX_FILE_BYTES // len(line) + 4))
    probe = tmp_path / "probe_module.py"
    probe.write_text(
        "def unique_probe_guardrail():\n"
        "    # oversized_probe_marker\n"
        "    return 'ok'\n",
        encoding="utf-8",
    )

    engine = QmdHybridSearchEngine(workspace_root=str(tmp_path))
    with caplog.at_level(logging.WARNING):
        res = await engine.search(
            query="unique_probe_guardrail oversized_probe_marker", max_tokens=600
        )

    assert res["status"] == "success"
    assert res["snippets_count"] >= 1
    assert all(s["file"] == "probe_module.py" for s in res["snippets"])
    assert any("excede o teto" in r.getMessage() for r in caplog.records)


# ──────────────────────────────────────────────────────────────────────────────
# 4. Performance: buscas consecutivas em workspace estável
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_consecutive_search_fast_on_stable_workspace(stable_workspace: Path):
    """2ª busca reaproveita o SymbolIndex: sem miss, < 50 ms, mesmo resultado."""
    engine = QmdHybridSearchEngine(workspace_root=str(stable_workspace))
    r1 = await engine.search(
        query="calculate_total_billing RN_FAT_009", max_tokens=800
    )
    assert r1["status"] == "success"
    assert r1["snippets_count"] >= 1

    before = get_file_cache_stats()
    expected_contents = [s["content"] for s in r1["snippets"]]
    wall_samples: List[float] = []
    internal_samples: List[float] = []
    for _ in range(3):
        wall_start = time.perf_counter()
        repeat = await engine.search(
            query="calculate_total_billing RN_FAT_009", max_tokens=800
        )
        wall_samples.append((time.perf_counter() - wall_start) * 1000.0)
        internal_samples.append(repeat["latency_ms"])
        assert repeat["status"] == "success"
        assert [s["content"] for s in repeat["snippets"]] == expected_contents
    after = get_file_cache_stats()

    # Reaproveitamento real: todos os arquivos estáveis vieram do cache (sem
    # nenhum miss novo). Assert determinístico e independente de hardware.
    assert after["hits"] >= before["hits"] + 3
    assert after["misses"] == before["misses"]
    # Contrato de performance: usa o MELHOR de 3 amostras para não punir um
    # único hiccup de scheduler do runner (o requisito é capacidade, não p99).
    assert min(internal_samples) < 50.0, internal_samples
    assert min(wall_samples) < 50.0, wall_samples
    # Resultado determinístico e idêntico entre buscas consecutivas.
    assert [s["content"] for s in repeat["snippets"]] == expected_contents


# ──────────────────────────────────────────────────────────────────────────────
# 5. Timeout global configurável por requisição
# ──────────────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_hybrid_search_global_timeout(stable_workspace: Path, caplog):
    """timeout=100µs aborta com status='timeout'; <=0 desativa; padrão é 15 s."""
    assert DEFAULT_SEARCH_TIMEOUT_S == 15.0
    engine = QmdHybridSearchEngine(workspace_root=str(stable_workspace))

    with caplog.at_level(logging.WARNING):
        res = await engine.search(
            query="calculate_total_billing RN_FAT_009", timeout=0.0001
        )
    assert res["status"] == "timeout"
    assert res["snippets"] == []
    assert res["snippets_count"] == 0
    assert res["total_tokens"] == 0
    assert res["timeout_s"] == 0.0001
    assert isinstance(res["latency_ms"], (int, float))
    assert any("timeout" in r.getMessage().lower() for r in caplog.records)

    # timeout <= 0 desativa a cota (semântica de SpawnSubagentRequest.timeout).
    res_off = await engine.search(
        query="calculate_total_billing", max_tokens=600, timeout=0
    )
    assert res_off["status"] == "success"

    # Cota explícita folgada permanece saudável.
    res_ok = await engine.search(
        query="calculate_total_billing", max_tokens=600, timeout=30.0
    )
    assert res_ok["status"] == "success"
    assert res_ok["snippets_count"] >= 1


@pytest.mark.asyncio
async def test_mcp_tool_exposes_timeout_and_keeps_alias_parity(stable_workspace: Path):
    """Catálogo MCP expõe `timeout` e cirurgic_search/qmd_search seguem paridade."""
    canonical = get_tool_definition("cirurgic_search")
    alias = get_tool_definition("qmd_search")
    assert canonical["function"]["parameters"] == alias["function"]["parameters"]
    props = canonical["function"]["parameters"]["properties"]
    assert props["timeout"]["default"] == 15.0
    assert canonical["function"]["parameters"]["required"] == ["query"]

    from agent_quality_guard.mcp_server import qmd_search

    res_timeout = await qmd_search(
        query="validate_eligibility",
        workspace=str(stable_workspace),
        max_tokens=600,
        timeout=0.0001,
    )
    assert res_timeout["status"] == "timeout"

    res_ok = await qmd_search(
        query="validate_eligibility",
        workspace=str(stable_workspace),
        max_tokens=600,
        timeout=30.0,
    )
    assert res_ok["status"] == "success"
    assert res_ok["snippets_count"] >= 1


def test_ripgrep_subprocess_timeout_degrades_gracefully(stable_workspace: Path):
    """Cota do subprocesso rg: expiração degrada para {} sem levantar exceção."""
    engine = QmdHybridSearchEngine(workspace_root=str(stable_workspace))
    degraded = engine._run_ripgrep("calculate_total_billing", timeout=0.00001)
    assert isinstance(degraded, dict)

    full = engine._run_ripgrep("calculate_total_billing")
    assert "billing_service.py" in full


# ──────────────────────────────────────────────────────────────────────────────
# 6. Retrocompatibilidade de assinaturas
# ──────────────────────────────────────────────────────────────────────────────


def test_search_signatures_backward_compatible():
    """Parâmetros novos são opcionais e vêm depois dos legados."""
    sig = inspect.signature(QmdHybridSearchEngine.search)
    params = list(sig.parameters)
    assert params[:6] == ["self", "query", "scope", "max_tokens", "depth", "k"]
    assert params[-1] == "timeout"
    assert sig.parameters["timeout"].default is None

    rg_sig = inspect.signature(QmdHybridSearchEngine._run_ripgrep)
    rg_params = list(rg_sig.parameters)
    assert rg_params[:4] == [
        "self",
        "query",
        "scope",
        "max_results_per_file",
    ]
    assert rg_sig.parameters["timeout"].default == DEFAULT_RIPGREP_TIMEOUT_S


@pytest.mark.asyncio
async def test_preview_bounded_read_and_oversize_guard(tmp_path: Path):
    """Preview lê só as linhas ao redor do match; >2 MB nunca é aberto."""
    engine = QmdHybridSearchEngine(workspace_root=str(tmp_path))

    small = tmp_path / "settings.conf"
    small.write_text(
        "\n".join(f"LINE_{i}=v{i}" for i in range(1, 51)) + "\n", encoding="utf-8"
    )
    item = engine._collect_grep_fallback_snippet(
        "settings.conf", small, 0.5, [{"line": 30, "text": "LINE_30=v30"}]
    )
    assert item is not None
    assert "LINE_30=v30" in item["content"]
    assert "LINE_1=v1" not in item["content"]  # nunca materializa o arquivo inteiro
    assert len(item["content"]) <= MAX_LITERAL_PREVIEW_CHARS

    big = tmp_path / "huge_dump.txt"
    big.write_bytes(b"MARKER_OVERSIZE_SGA04 " + b"a" * MAX_INDEX_FILE_BYTES)
    item_big = engine._collect_grep_fallback_snippet(
        "huge_dump.txt", big, 0.5, [{"line": 1, "text": "MARKER_OVERSIZE_SGA04"}]
    )
    assert item_big is not None
    assert "MARKER_OVERSIZE_SGA04" in item_big["content"]
    assert "aaaa" not in item_big["content"]  # corpo > 2 MB nunca entrou na memória
    assert len(item_big["content"]) <= MAX_LITERAL_PREVIEW_CHARS


@pytest.mark.asyncio
async def test_oversized_file_warning_is_once_per_path(tmp_path: Path, caplog):
    """Aviso de oversize é emitido uma única vez por caminho (sem inundar log)."""
    big = tmp_path / "spam_warn.py"
    big.write_bytes(b"y = 2\n" * (MAX_INDEX_FILE_BYTES // 6 + 8))
    engine = LocalNeedleEngine(workspace_root=str(tmp_path))

    with caplog.at_level(logging.WARNING):
        for _ in range(5):
            engine._indexed_file_symbols(big)

    oversize_warnings = [
        r for r in caplog.records if "excede o teto" in r.getMessage()
    ]
    assert len(oversize_warnings) == 1
