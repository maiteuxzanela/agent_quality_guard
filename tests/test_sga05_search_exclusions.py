"""TICKET-SGA-05 — Exclusões configuráveis, profundidade explícita e heurística de tokens.

Determinístico, sem mocks: workspaces reais em disco, walkers reais do Needle,
subprocessos reais de ripgrep (com skip honesto quando o binário ausente) e a
superfície MCP real registrada.
"""

from __future__ import annotations

import ast
import asyncio
import inspect
import re
import tempfile
from pathlib import Path

import pytest

from agent_quality_guard.core.schemas import (
    CHARS_PER_TOKEN,
    Needle3Query,
    NeedleSearchHttpRequest,
)
from agent_quality_guard.memory import local_needle as local_needle_module
from agent_quality_guard.memory import qmd_search as qmd_search_module
from agent_quality_guard.memory.local_needle import (
    UNIVERSAL_EXCLUDED_DIRS,
    exclude_patterns_to_rg_globs,
    is_ignored_artifact,
    load_cirurgicignore,
    merge_exclude_patterns,
    normalize_ignore_pattern,
    path_is_excluded,
    LocalNeedleEngine,
)
from agent_quality_guard.memory.qmd_search import (
    GREP_EXCLUDED_DIRS,
    IGNORED_GLOBS,
    QmdHybridSearchEngine,
    find_rg_binary,
)
from agent_quality_guard.mcp_server import cirurgic_search, qmd_search


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ─────────────────────────────────────────────────────────────────────────────
# 1. Saneamento de heurísticas locais: .cirurgicignore + exclude_patterns
# ─────────────────────────────────────────────────────────────────────────────


def test_normalize_ignore_pattern_rules():
    """Subset .gitignore: comentários, brancos, ./ e / final; sem negação."""
    assert normalize_ignore_pattern("# comentario") is None
    assert normalize_ignore_pattern("   ") is None
    assert normalize_ignore_pattern("!negado") is None
    assert normalize_ignore_pattern("./screenshots/") == "screenshots/"
    assert normalize_ignore_pattern("/legacy") == "legacy"
    assert normalize_ignore_pattern("panel/*.js") == "panel/*.js"


def test_load_cirurgicignore_reads_only_when_present(tmp_path: Path):
    assert load_cirurgicignore(tmp_path) == []
    _write(
        tmp_path / ".cirurgicignore",
        "# bundles locais\n\nscreenshots\n\nlegacy/\npanel/*.js\nscreenshots\n",
    )
    assert load_cirurgicignore(tmp_path) == ["screenshots", "legacy/", "panel/*.js"]


def test_merge_exclude_patterns_dedupes_explicit_then_file(tmp_path: Path):
    _write(tmp_path / ".cirurgicignore", "screenshots\nlegacy/\n")
    merged = merge_exclude_patterns(["legacy/", "vendor"], tmp_path)
    assert merged == ["legacy/", "vendor", "screenshots"]


def test_path_is_excluded_semantics():
    # Nome puro casa em qualquer nível (arquivo OU diretório com esse nome exato).
    assert path_is_excluded("pkg/screenshots/shot.py", ["screenshots"]) is True
    assert path_is_excluded("screenshots", ["screenshots"]) is True
    # Um arquivo que apenas termina com o nome não casa (igual ao .gitignore).
    assert path_is_excluded("screenshots.py", ["screenshots"]) is False
    # Padrão com '/' é ancorado na raiz do workspace.
    assert path_is_excluded("panel/app.js", ["panel/*.js"]) is True
    assert path_is_excluded("x/panel/app.js", ["panel/*.js"]) is False
    # '/' final marca diretório: casa com o dir e todo o subtree.
    assert path_is_excluded("legacy/deep/mod.py", ["legacy/"]) is True
    assert path_is_excluded("notlegacy/mod.py", ["legacy/"]) is False
    assert path_is_excluded("src/main.py", []) is False


def test_exclude_patterns_to_rg_globs_only_pushes_bare_names():
    globs = exclude_patterns_to_rg_globs(["screenshots", "panel/*.js"])
    assert globs == ["!**/screenshots", "!**/screenshots/**"]


def test_universal_defaults_have_no_local_specific_rules():
    """As exclusões universais padrão nunca carregam regras de ambiente local."""
    for local_rule in (".agent-teams", "screenshots", "panel"):
        assert local_rule not in UNIVERSAL_EXCLUDED_DIRS
        assert local_rule not in GREP_EXCLUDED_DIRS
        assert not any(local_rule in glob for glob in IGNORED_GLOBS)
    for canonical in (
        ".git", "node_modules", "dist", "__pycache__", ".venv",
        ".pytest_cache", ".mypy_cache",
    ):
        assert canonical in UNIVERSAL_EXCLUDED_DIRS


def test_is_ignored_artifact_accepts_runtime_exclude_patterns():
    assert is_ignored_artifact(Path("panel/app.js"), "panel/app.js") is False
    assert (
        is_ignored_artifact(Path("panel/app.js"), "panel/app.js", ["panel/*.js"])
        is True
    )
    # Sufixos universais de artefato derivado continuam bloqueados sempre.
    assert is_ignored_artifact(Path("x.min.js"), "x.min.js", None) is True


# ─────────────────────────────────────────────────────────────────────────────
# 2. Rota AST (Needle local): exclude_patterns + .cirurgicignore + max_depth
# ─────────────────────────────────────────────────────────────────────────────


def test_needle_walk_applies_exclude_patterns_and_cirurgicignore(tmp_path: Path):
    _write(tmp_path / "src" / "keep.py", "def alpha_marker(): ...\n")
    _write(tmp_path / "screenshots" / "shot.py", "def alpha_marker(): ...\n")
    _write(tmp_path / "vendor" / "lib.py", "def alpha_marker(): ...\n")
    _write(tmp_path / ".cirurgicignore", "vendor\n")
    engine = LocalNeedleEngine(workspace_root=str(tmp_path))

    found = {
        str(p.relative_to(tmp_path))
        for p in engine._find_candidate_files(exclude_patterns=["screenshots"])
    }
    assert found == {"src/keep.py"}


def test_needle_walk_honours_max_depth(tmp_path: Path):
    _write(tmp_path / "top.py", "def depth_marker(): ...\n")
    _write(tmp_path / "pkg" / "mid.py", "def depth_marker(): ...\n")
    _write(tmp_path / "pkg" / "sub" / "deep.py", "def depth_marker(): ...\n")
    engine = LocalNeedleEngine(workspace_root=str(tmp_path))

    unlimited = {
        str(p.relative_to(tmp_path)) for p in engine._find_candidate_files()
    }
    assert unlimited == {"top.py", "pkg/mid.py", "pkg/sub/deep.py"}

    depth_one = {
        str(p.relative_to(tmp_path)) for p in engine._find_candidate_files(max_depth=1)
    }
    assert depth_one == {"top.py"}


def test_needle_search_wires_query_exclude_patterns_and_max_depth(tmp_path: Path):
    _write(tmp_path / "keep.py", "def wired_marker(): 'wired_marker'\n")
    _write(tmp_path / "legacy" / "old.py", "def wired_marker(): ...\n")
    _write(tmp_path / "pkg" / "deep" / "leaf.py", "def wired_marker(): ...\n")
    engine = LocalNeedleEngine(workspace_root=str(tmp_path))

    query = Needle3Query(
        query="wired_marker",
        max_tokens=2000,
        max_depth=2,
        exclude_patterns=["legacy"],
    )
    response = asyncio.run(engine.search(query))
    files = {s.file_path for s in response.snippets}
    assert "keep.py" in files
    assert not any(f.startswith("legacy") for f in files)
    assert not any("deep" in f for f in files)


# ─────────────────────────────────────────────────────────────────────────────
# 3. Rota léxica/híbrida (ripgrep) + profundidade
# ─────────────────────────────────────────────────────────────────────────────


def test_hybrid_search_applies_exclude_patterns_and_depth(tmp_path: Path):
    if find_rg_binary() is None:
        pytest.skip("ripgrep ausente: rota léxica com glob não é aplicável")

    marker = "sga05_hybrid_marker"
    _write(tmp_path / "src" / "keep.py", f"def {marker}():\n    return '{marker}'\n")
    _write(tmp_path / "legacy" / "old.py", f"def {marker}(): ...\n")
    _write(tmp_path / "pkg" / "deep" / "leaf.py", f"def {marker}(): ...\n")

    engine = QmdHybridSearchEngine(workspace_root=str(tmp_path))
    res = asyncio.run(
        engine.search(marker, max_tokens=2000, depth=2, exclude_patterns=["legacy"])
    )

    files = {s["file"] for s in res["snippets"]}
    assert "src/keep.py" in files
    assert not any(f.startswith("legacy") for f in files)
    assert not any("deep" in f for f in files)


def test_hybrid_search_depth_none_is_unlimited(tmp_path: Path):
    if find_rg_binary() is None:
        pytest.skip("ripgrep ausente: rota léxica com glob não é aplicável")

    marker = "sga05_unlimited_marker"
    _write(tmp_path / "pkg" / "deep" / "leaf.py", f"def {marker}(): ...\n")

    engine = QmdHybridSearchEngine(workspace_root=str(tmp_path))
    res = asyncio.run(engine.search(marker, max_tokens=2000))
    assert {s["file"] for s in res["snippets"]} == {"pkg/deep/leaf.py"}


# ─────────────────────────────────────────────────────────────────────────────
# 4. Schemas, CHARS_PER_TOKEN e superfície MCP (paridade + depreciação)
# ─────────────────────────────────────────────────────────────────────────────


def test_chars_per_token_constant_is_documented_heuristic():
    # Literal explícito (não o símbolo importado): o assert não pode virar
    # tautológico sob mutação da constante.
    assert CHARS_PER_TOKEN == 3.8
    assert QmdHybridSearchEngine()._estimate_tokens("a" * 38) == 10
    assert LocalNeedleEngine()._estimate_tokens("a" * 38) == 10


def test_needle3query_new_fields_defaults_and_coercion():
    plain = Needle3Query(query="x")
    assert plain.max_depth is None
    assert plain.exclude_patterns is None

    coerced = Needle3Query(query="x", exclude_patterns="screenshots/, *.min.js")
    assert coerced.exclude_patterns == ["screenshots/", "*.min.js"]

    http = NeedleSearchHttpRequest(query="x", exclude_patterns=["vendor"])
    assert http.exclude_patterns == ["vendor"]
    assert http.max_depth is None


def test_mcp_search_tools_share_contract_with_new_params():
    from agent_quality_guard.mcp_server import app

    assert inspect.iscoroutinefunction(cirurgic_search)
    assert cirurgic_search is qmd_search

    tools = {tool.name: tool for tool in asyncio.run(app.list_tools())}
    canonical = tools["cirurgic_search"]
    legacy = tools["qmd_search"]
    assert canonical.input_schema == legacy.input_schema
    props = canonical.input_schema["properties"]
    assert props["depth"].get("default") is None
    assert "exclude_patterns" in props
    assert "DEPRECADO" in legacy.description
    assert "Nome canonico atual" in canonical.description


def test_mcp_core_forwards_exclude_patterns_end_to_end(tmp_path: Path):
    if find_rg_binary() is None:
        pytest.skip("ripgrep ausente: rota léxica com glob não é aplicável")

    marker = "sga05_mcp_marker"
    _write(tmp_path / "keep.py", f"def {marker}(): ...\n")
    _write(tmp_path / "legacy" / "old.py", f"def {marker}(): ...\n")

    res = asyncio.run(
        cirurgic_search(
            query=marker,
            workspace=str(tmp_path),
            max_tokens=1500,
            exclude_patterns=["legacy"],
        )
    )
    assert res["status"] == "success"
    assert {s["file"] for s in res["snippets"]} == {"keep.py"}


def test_mcp_core_applies_cirurgicignore_end_to_end(tmp_path: Path):
    """.cirurgicignore da raiz é honrado pelo núcleo MCP SEM exclude_patterns."""
    if find_rg_binary() is None:
        pytest.skip("ripgrep ausente: rota léxica com glob não é aplicável")

    marker = "sga05_mcp_ignore_marker"
    _write(tmp_path / "keep.py", f"def {marker}(): ...\n")
    _write(tmp_path / "generated" / "out.py", f"def {marker}(): ...\n")
    _write(tmp_path / ".cirurgicignore", "# artefatos gerados localmente\n\ngenerated\n")

    res = asyncio.run(
        cirurgic_search(query=marker, workspace=str(tmp_path), max_tokens=1500)
    )
    assert res["status"] == "success"
    assert {s["file"] for s in res["snippets"]} == {"keep.py"}


def test_mcp_core_depth_backcompat_end_to_end(tmp_path: Path):
    """Retrocompat `depth`: None = ilimitado; enviado = corte real da árvore."""
    if find_rg_binary() is None:
        pytest.skip("ripgrep ausente: rota léxica com glob não é aplicável")

    marker = "sga05_mcp_depth_marker"
    _write(tmp_path / "keep.py", f"def {marker}(): ...\n")
    _write(tmp_path / "pkg" / "deep.py", f"def {marker}(): ...\n")

    unlimited = asyncio.run(
        cirurgic_search(query=marker, workspace=str(tmp_path), max_tokens=1500)
    )
    assert unlimited["status"] == "success"
    assert {s["file"] for s in unlimited["snippets"]} == {"keep.py", "pkg/deep.py"}

    cut = asyncio.run(
        cirurgic_search(query=marker, workspace=str(tmp_path), max_tokens=1500, depth=1)
    )
    assert cut["status"] == "success"
    assert {s["file"] for s in cut["snippets"]} == {"keep.py"}


# ─────────────────────────────────────────────────────────────────────────────
# 5. Wiring de `depth` (AST real) e conformidade heurística de docstrings
# ─────────────────────────────────────────────────────────────────────────────


def test_depth_wiring_overrides_relational_depth_and_max_depth():
    """AST do fonte real: `depth` vira max_depth E sobrepõe relational_depth.

    Tripwire de regressão determinístico (mesmo estilo do AST source-check do
    repositório): o wiring em ``QmdHybridSearchEngine.search`` precisa manter
    ``relational_depth=depth if depth is not None else 2`` e
    ``max_depth=depth`` (nas duas rotas: Needle walk e ripgrep). Se alguém
    remover o override ou fixar default diferente de 2, este teste falha.
    """
    source = (Path(qmd_search_module.__file__)).read_text(encoding="utf-8")
    tree = ast.parse(source)

    search_fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "search"
    )

    needle_query_call = next(
        n
        for n in ast.walk(search_fn)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == "Needle3Query")
            or (isinstance(n.func, ast.Attribute) and n.func.attr == "Needle3Query")
        )
    )
    keywords = {kw.arg: kw.value for kw in needle_query_call.keywords if kw.arg}

    # max_depth recebe o `depth` diretamente (corte de árvore de diretórios).
    max_depth_node = keywords["max_depth"]
    assert isinstance(max_depth_node, ast.Name) and max_depth_node.id == "depth"

    # relational_depth é o override condicional: depth enviado sobrepõe;
    # None mantém o padrão canônico 2 (retrocompat sem quebra).
    rel_node = keywords["relational_depth"]
    assert isinstance(rel_node, ast.IfExp), (
        "relational_depth precisa ser `depth if depth is not None else 2` — "
        f"obtido: {ast.dump(rel_node)[:120]}"
    )
    assert isinstance(rel_node.test, ast.Compare)
    assert isinstance(rel_node.test.left, ast.Name) and rel_node.test.left.id == "depth"
    assert isinstance(rel_node.test.ops[0], ast.IsNot)
    assert isinstance(rel_node.body, ast.Name) and rel_node.body.id == "depth"
    assert isinstance(rel_node.orelse, ast.Constant) and rel_node.orelse.value == 2

    # Rota léxica: `asyncio.to_thread(self._run_ripgrep, ..., max_depth=depth)`.
    rg_call = next(
        n
        for n in ast.walk(search_fn)
        if isinstance(n, ast.Call)
        and len(n.args) >= 1
        and isinstance(n.args[0], ast.Attribute)
        and n.args[0].attr == "_run_ripgrep"
    )
    rg_keywords = {kw.arg: kw.value for kw in rg_call.keywords if kw.arg}
    rg_depth = rg_keywords["max_depth"]
    assert isinstance(rg_depth, ast.Name) and rg_depth.id == "depth"


def test_relational_depth_schema_default_and_override_documented():
    """Schema canônico: default 2 e docstring declara a sobrescrita por `depth`."""
    from agent_quality_guard.core.schemas import Needle3Response

    default_query = Needle3Query(query="x")
    assert default_query.relational_depth == 2
    assert default_query.max_depth is None  # None = ilimitado (retrocompat)

    http = NeedleSearchHttpRequest(query="x")
    assert http.relational_depth == 2
    assert http.max_depth is None

    rel_field = Needle3Query.model_fields["relational_depth"]
    assert "depth" in (rel_field.description or "")
    assert "sobrepõe" in (rel_field.description or "")
    assert "ilimitada" in (Needle3Query.model_fields["max_depth"].description or "")

    resp_doc = inspect.getdoc(Needle3Response) or ""
    assert "CHARS_PER_TOKEN = 3.8" in resp_doc
    assert "criptográfica" in resp_doc


def test_heuristic_disclaimer_conforms_across_schemas_and_engines():
    """Toda descrição/docstring de estimativa declara heurística, não garantia."""
    from agent_quality_guard.core.schemas import Needle3Response

    def _assert_heuristic(label: str, text: str) -> None:
        lowered = text.lower()
        assert "heurístic" in lowered, f"{label}: marcador 'heurístic' ausente"
        assert "criptográfic" in lowered, (
            f"{label}: disclaimers 'não é garantia criptográfica' ausentes"
        )

    _assert_heuristic(
        "Needle3Query.max_tokens",
        Needle3Query.model_fields["max_tokens"].description or "",
    )
    _assert_heuristic(
        "NeedleSearchHttpRequest.max_tokens",
        NeedleSearchHttpRequest.model_fields["max_tokens"].description or "",
    )
    _assert_heuristic(
        "Needle3Response.__doc__", inspect.getdoc(Needle3Response) or ""
    )
    _assert_heuristic(
        "LocalNeedleEngine._estimate_tokens",
        inspect.getdoc(LocalNeedleEngine._estimate_tokens) or "",
    )
    _assert_heuristic(
        "QmdHybridSearchEngine._estimate_tokens",
        inspect.getdoc(QmdHybridSearchEngine._estimate_tokens) or "",
    )

    # Fonte única: os módulos de memória IMPORTAM CHARS_PER_TOKEN do schema —
    # nenhuma reimplementação local da constante.
    for module in (local_needle_module, qmd_search_module):
        module_tree = ast.parse(
            (Path(module.__file__)).read_text(encoding="utf-8")
        )
        for node in module_tree.body:
            targets: list = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id == "CHARS_PER_TOKEN":
                    raise AssertionError(
                        f"{module.__name__} redefine CHARS_PER_TOKEN — "
                        "a fonte única é agent_quality_guard.core.schemas"
                    )


def test_depth_zero_is_empty_parity_across_routes(tmp_path: Path):
    """Edge determinístico: depth=0 devolve vazio nas DUAS rotas (paridade)."""
    marker = "sga05_depth_zero_marker"
    _write(tmp_path / "top.py", f"def {marker}(): ...\n")
    _write(tmp_path / "pkg" / "mid.py", f"def {marker}(): ...\n")

    engine = QmdHybridSearchEngine(workspace_root=str(tmp_path))
    res = asyncio.run(engine.search(marker, max_tokens=1000, depth=0))
    assert res["status"] == "success"
    assert res["snippets"] == []

    needle = LocalNeedleEngine(workspace_root=str(tmp_path))
    needle_res = asyncio.run(
        needle.search(Needle3Query(query=marker, max_tokens=1000, max_depth=0))
    )
    assert needle_res.snippets == []
