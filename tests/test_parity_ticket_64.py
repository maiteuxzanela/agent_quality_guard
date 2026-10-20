"""TICKET-HARNESS-64 — Paridade de qualidade da busca cirúrgica.

Suíte determinística REAL (sem mocks) cobrindo as cinco frentes:

A. Stopwords bilíngues PT/EN + fallback seguro e tokenização camel/snake.
B. Cobertura conjuntiva no Ripgrep (distinct_terms/term_coverage), penalidade
   0.5x e IDF intra-busca no LocalNeedleEngine.
C. Boost tipológico de 1.25x para código-fonte no RRF (neutralizado em escopo
   documental).
D. Denylist de bundles/artefatos e chunking adaptativo de 1800 caracteres.
E. Resolução estrita de ``scope`` relativo ao workspace.

Todas as buscas exercitam subprocessos reais do ripgrep, sistema de arquivos
real e SQLite/AST reais — nenhum duplo de teste.
"""

from pathlib import Path

import pytest

from agent_quality_guard.memory.local_needle import (
    MAX_LEXICAL_CHUNK_CHARS,
    MAX_LEXICAL_CHUNK_LINES,
    STOPWORDS,
    STOPWORDS_EN,
    STOPWORDS_PT,
    LocalNeedleEngine,
    is_ignored_artifact,
    substantive_terms,
)
from agent_quality_guard.core.schemas import Needle3Query
from agent_quality_guard.memory.qmd_search import (
    DEFAULT_MAX_MATCHES_PER_FILE,
    GREP_EXCLUDED_DIRS,
    IGNORED_GLOBS,
    MAX_LITERAL_PREVIEW_CHARS,
    QmdHybridSearchEngine,
    SOURCE_CODE_BOOST,
    _escape_literal,
    _scope_is_documentation,
    _type_multiplier,
    find_rg_binary,
)


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# FRENTE A — Stopwords bilíngues + fallback seguro + tokenização
# ---------------------------------------------------------------------------


def test_stopwords_catalog_bilingual_shared():
    """PT-BR e EN convivem no mesmo conjunto consumido pelas duas rotas."""
    assert {"como", "onde", "qual", "para", "que", "uma", "não"} <= STOPWORDS_PT
    assert {"what", "where", "how", "does", "the", "with", "from"} <= STOPWORDS_EN
    assert STOPWORDS == STOPWORDS_PT | STOPWORDS_EN
    assert "needle" not in STOPWORDS
    assert "workspace" not in STOPWORDS


def test_substantive_terms_filters_bilingual_stopwords():
    """Ruído gramatical PT/EN sai; termos técnicos permanecem, em ordem estável."""
    assert substantive_terms(["Como", "funciona", "o", "Needle", "WHERE", "needle"]) == [
        "needle"
    ]
    assert substantive_terms(["The", "workspace", "with", "search"]) == [
        "workspace",
        "search",
    ]
    # min_length continua descartando tokens de 1 caractere.
    assert substantive_terms(["a", "de", "x", "ne"] ) == ["ne"]


def test_substantive_terms_fallback_preserves_all_stopword_query():
    """Query só de stopwords nunca vira lista vazia (pattern não fica vazio)."""
    assert substantive_terms(["from", "to"]) == ["from", "to"]
    assert substantive_terms(["Como", "funciona"]) == ["como", "funciona"]
    assert substantive_terms(["the", "and"]) == ["the", "and"]


def test_build_pattern_strips_stopwords_and_has_safe_fallback(tmp_path: Path):
    """O pattern do ripgrep é guiado pelos substantivos; fallback preserva literal."""
    engine = QmdHybridSearchEngine(workspace_root=str(tmp_path))

    pattern, terms = engine._build_pattern("Como funciona evaluateFailoverCooldown")
    assert terms == ["evaluatefailovercooldown"]
    assert pattern == "evaluatefailovercooldown"
    assert "como" not in pattern.lower()
    assert "funciona" not in pattern.lower()

    # Query inteira de stopwords: pattern nunca vazio e sem escape inválido de espaço.
    fallback_pattern, fallback_terms = engine._build_pattern("from to")
    assert fallback_terms == ["from", "to"]
    assert fallback_pattern
    assert "\\ " not in fallback_pattern

    # ``re.escape`` escaparia o espaço; o motor Rust do rg rejeitaria "\ ".
    assert _escape_literal("from to") == "from to"
    assert _escape_literal("a.b(c)") == "a\\.b\\(c\\)"


def test_tokenize_emits_whole_and_camel_snake_parts(tmp_path: Path):
    """camelCase/PascalCase/snake_case geram sub-partes pesquisáveis."""
    engine = LocalNeedleEngine(workspace_root=str(tmp_path))
    assert engine.tokenize("evaluateFailoverCooldown") == [
        "evaluatefailovercooldown",
        "evaluate",
        "failover",
        "cooldown",
    ]
    assert engine.tokenize("MAX_LEXICAL_CHUNK_LINES") == [
        "max_lexical_chunk_lines",
        "max",
        "lexical",
        "chunk",
        "lines",
    ]
    assert engine.tokenize("plain_word") == ["plain_word", "plain", "word"]
    # Duplicatas case-insensitive são removidas (ordem estável).
    assert engine.tokenize("Token TOKEN") == ["token"]


# ---------------------------------------------------------------------------
# FRENTE B — Cobertura conjuntiva, penalidade 0.5x e IDF intra-busca
# ---------------------------------------------------------------------------


def test_ripgrep_conjunctive_coverage_and_tiebreak(tmp_path: Path):
    """Arquivo que cobre os DOIS termos vence o que repete um keyword quente."""
    _write(tmp_path / "hot.conf", "alpha\n" * 12)
    _write(tmp_path / "both.conf", "alpha beta\n")
    _write(tmp_path / "solo.conf", "beta\nbeta\n")

    engine = QmdHybridSearchEngine(workspace_root=str(tmp_path))
    entries = engine._run_ripgrep("alpha beta")

    assert entries["both.conf"]["distinct_terms"] == 2
    assert entries["both.conf"]["term_coverage"] == 1.0
    assert entries["both.conf"]["matched_terms"] == ["alpha", "beta"]

    assert entries["hot.conf"]["distinct_terms"] == 1
    assert entries["hot.conf"]["term_coverage"] == 0.5
    assert entries["hot.conf"]["matched_terms"] == ["alpha"]


@pytest.mark.asyncio
async def test_multi_term_query_penalizes_single_term_evidence(tmp_path: Path):
    """Peso léxico 0.5x quando a query tem múltiplos termos e o arquivo só cobre 1."""
    _write(tmp_path / "hot.conf", "alpha\n" * 12)
    _write(tmp_path / "both.conf", "alpha beta\n")
    _write(tmp_path / "solo.conf", "beta\nbeta\n")

    engine = QmdHybridSearchEngine(workspace_root=str(tmp_path))
    res = await engine.search("alpha beta", max_tokens=800)

    files = [s["file"] for s in res["snippets"]]
    assert files == ["both.conf", "hot.conf", "solo.conf"]

    scores = {s["file"]: s["rrf_score"] for s in res["snippets"]}
    # both cobre 2 termos (rank 1, peso cheio); hot cobre 1 (rank 2, peso 0.5x).
    # O rrf_score publicado é arredondado a 5 casas (abs=1e-5 cobre o arredondamento).
    assert scores["both.conf"] == pytest.approx(1.0 / (60 + 1), abs=1e-5)
    assert scores["hot.conf"] == pytest.approx(0.5 / (60 + 2), abs=1e-5)
    assert scores["both.conf"] / scores["hot.conf"] == pytest.approx(
        (1.0 / 61) / (0.5 / 62), rel=1e-3
    )


def test_ripgrep_match_cap_per_file(tmp_path: Path):
    """Arquivo denso é truncado no teto por arquivo, preservando contagem real."""
    _write(tmp_path / "dense.conf", "alpha\n" * 30)
    engine = QmdHybridSearchEngine(workspace_root=str(tmp_path))

    assert DEFAULT_MAX_MATCHES_PER_FILE == 20
    default_entries = engine._run_ripgrep("alpha")
    assert default_entries["dense.conf"]["match_count"] == 20
    assert len(default_entries["dense.conf"]["matches"]) == 20

    narrow = engine._run_ripgrep("alpha", max_results_per_file=5)
    assert narrow["dense.conf"]["match_count"] == 5
    assert len(narrow["dense.conf"]["matches"]) == 5


@pytest.mark.asyncio
async def test_idf_intra_search_weights_rare_terms(tmp_path: Path):
    """Termo raro domina o score; termo ubíquo perde peso mesmo casando."""
    common_functions = "\n\n".join(
        f"def fn_{i}():\n    return common_token + {i}" for i in range(8)
    )
    _write(tmp_path / "common.py", common_functions + "\n")
    _write(
        tmp_path / "rare.py",
        "def fn_rare():\n    return common_token + rare_token\n",
    )

    engine = LocalNeedleEngine(workspace_root=str(tmp_path))
    response = await engine.search(
        Needle3Query(query="common_token rare_token", max_tokens=4000)
    )

    assert len(response.snippets) == 9
    top = response.snippets[0]
    assert top.file_path == "rare.py"
    assert top.relevance_score == 1.0

    common_scores = [
        s.relevance_score for s in response.snippets if s.file_path == "common.py"
    ]
    assert len(common_scores) == 8
    # Sem IDF, um bloco que casa 3/5 tokens marcaria 0.6; com IDF fica < 0.1.
    assert all(0.0 < score < 0.10 for score in common_scores)


# ---------------------------------------------------------------------------
# FRENTE C — Boost tipológico 1.25x no RRF
# ---------------------------------------------------------------------------


def test_type_multiplier_constants_and_doc_scope():
    """Código-fonte 1.25x; docs/dados 1.0x; escopo documental neutraliza o boost."""
    assert SOURCE_CODE_BOOST == 1.25
    assert _type_multiplier("impl.py", False) == pytest.approx(1.25)
    assert _type_multiplier("src/app.ts", False) == pytest.approx(1.25)
    assert _type_multiplier("docs/guide.md", False) == 1.0
    assert _type_multiplier("data.json", False) == 1.0
    assert _type_multiplier("impl.py", True) == 1.0

    assert _scope_is_documentation(None) is False
    assert _scope_is_documentation([]) is False
    assert _scope_is_documentation(["src/memory"]) is False
    assert _scope_is_documentation(["docs"]) is True
    assert _scope_is_documentation(["src/personas"]) is True
    assert _scope_is_documentation(["README.md"]) is True
    assert _scope_is_documentation(["data.json"]) is True


@pytest.mark.asyncio
async def test_code_type_boost_applied_in_rrf(tmp_path: Path):
    """Mesmo conteúdo/rank: .py recebe exatamente 1.25x do score de .md."""
    content = "def target_symbol():\n    return 'target_symbol'\n"
    code_ws = tmp_path / "code_ws"
    doc_ws = tmp_path / "doc_ws"
    _write(code_ws / "impl.py", content)
    _write(doc_ws / "notes.md", "# target_symbol\n\ntarget_symbol docs\n")

    code = await QmdHybridSearchEngine(workspace_root=str(code_ws)).search(
        "target_symbol", max_tokens=800
    )
    doc = await QmdHybridSearchEngine(workspace_root=str(doc_ws)).search(
        "target_symbol", max_tokens=800
    )

    assert code["snippets"] and doc["snippets"]
    code_score = code["snippets"][0]["rrf_score"]
    doc_score = doc["snippets"][0]["rrf_score"]
    assert code_score / doc_score == pytest.approx(1.25, rel=1e-3)


# ---------------------------------------------------------------------------
# FRENTE D — Denylist de bundles/artefatos + teto adaptativo de 1800
# ---------------------------------------------------------------------------


def test_is_ignored_artifact_denylist():
    """Bundles, source maps e panel/*.js nunca entram na busca cirúrgica."""
    assert is_ignored_artifact(Path("app.min.js"), "app.min.js") is True
    assert is_ignored_artifact(Path("src/app.bundle.js"), "src/app.bundle.js") is True
    assert is_ignored_artifact(Path("x.min.mjs"), "x.min.mjs") is True
    assert is_ignored_artifact(Path("x.js.map"), "x.js.map") is True
    assert is_ignored_artifact(Path("panel/app.js"), "panel/app.js") is True
    assert is_ignored_artifact(Path("src/panel/app.js"), "src/panel/app.js") is True

    assert is_ignored_artifact(Path("app.js"), "app.js") is False
    assert is_ignored_artifact(Path("panel/app.ts"), "panel/app.ts") is False
    assert is_ignored_artifact(Path("panelx/app.js"), "panelx/app.js") is False

    assert "!**/panel/*.js" in IGNORED_GLOBS
    assert "!**/*.min.js" in IGNORED_GLOBS
    assert "!**/*.bundle.js" in IGNORED_GLOBS
    assert "!**/*.min.mjs" in IGNORED_GLOBS
    assert "dist" in GREP_EXCLUDED_DIRS


@pytest.mark.asyncio
async def test_bundles_and_excluded_dirs_never_returned(tmp_path: Path):
    """Integração real (rg + Needle): só o fonte legítimo aparece."""
    if find_rg_binary() is None:
        pytest.skip("ripgrep ausente: denylist por glob não é aplicável ao grep puro")

    marker = "unique_bundle_marker"
    _write(
        tmp_path / "src" / "real.py",
        f"def {marker}():\n    return '{marker}'\n",
    )
    _write(tmp_path / "panel" / "app.js", f"var {marker} = 1;\n")
    _write(tmp_path / "bundle.min.js", f"var {marker}=1;\n")
    _write(tmp_path / "dist" / "copy.py", f"def {marker}(): ...\n")
    _write(tmp_path / "screenshots" / "shot.py", f"def {marker}(): ...\n")
    _write(tmp_path / "node_modules" / "x" / "index.js", f"var {marker}=1;\n")

    engine = QmdHybridSearchEngine(workspace_root=str(tmp_path))
    res = await engine.search(marker, max_tokens=2000)

    assert [s["file"] for s in res["snippets"]] == ["src/real.py"]
    assert res["files_found"] == 1


def test_chunk_lexical_blocks_caps_chars_and_lines(tmp_path: Path):
    """Nenhum bloco lexical excede 50 linhas nem 1800 caracteres."""
    # Contrato explícito (valores literais, NÃO os símbolos importados: ler o
    # próprio símbolo tornaria o assert tautológico sob mutação do teto).
    assert MAX_LEXICAL_CHUNK_CHARS == 1800
    assert MAX_LEXICAL_CHUNK_LINES == 50
    engine = LocalNeedleEngine(workspace_root=str(tmp_path))

    long_lines = [f"line_{i:03d} " + "x" * 80 for i in range(120)]
    char_blocks = engine._chunk_lexical_blocks(tmp_path / "long.txt", long_lines)
    assert len(char_blocks) >= 3
    for block in char_blocks:
        assert len(block["content"]) <= 1800
        assert block["end_line"] - block["start_line"] + 1 <= 50
    # Conteúdo íntegro e contíguo (nenhuma linha perdida na segmentação).
    assert "\n".join(b["content"] for b in char_blocks) == "\n".join(long_lines)

    short_lines = ["x"] * 120
    line_blocks = engine._chunk_lexical_blocks(tmp_path / "short.txt", short_lines)
    assert [b["end_line"] - b["start_line"] + 1 for b in line_blocks] == [50, 50, 20]
    assert all(len(b["content"]) <= 1800 for b in line_blocks)


def test_chunk_dense_single_line_is_sliced(tmp_path: Path):
    """Linha única gigante (minificado/JSON) é fatiada em janelas de 1800."""
    engine = LocalNeedleEngine(workspace_root=str(tmp_path))
    dense = "z" * (1800 * 2 + 137)

    blocks = engine._chunk_lexical_blocks(tmp_path / "dense.json", [dense])

    assert [len(b["content"]) for b in blocks] == [1800, 1800, 137]
    assert all(b["start_line"] == 1 and b["end_line"] == 1 for b in blocks)
    assert "".join(b["content"] for b in blocks) == dense


def test_lexical_blocks_and_js_preview_respect_char_cap(tmp_path: Path):
    """_lexical_blocks e preview JS nunca devolvem bloco acima do teto."""
    engine = LocalNeedleEngine(workspace_root=str(tmp_path))

    txt = _write(tmp_path / "notes.txt", "\n".join(["payload " * 20] * 100))
    lexical = engine._lexical_blocks(txt)
    assert lexical
    assert all(len(b["content"]) <= 1800 for b in lexical)

    big_function_body = "\n".join(f"  const v{i} = 'p' + {i};" for i in range(100))
    js = _write(
        tmp_path / "huge.js",
        "function hugeBlock() {\n" + big_function_body + "\n}\n",
    )
    js_blocks = engine._extract_js_blocks(js)
    assert js_blocks
    huge = next(b for b in js_blocks if b["name"] == "hugeBlock")
    assert len(huge["content"]) <= 1800
    assert huge["content"].count("\n") <= 79


def test_grep_literal_preview_truncated_to_1800(tmp_path: Path):
    """Preview literal de fallback também respeita o teto de 1800."""
    engine = QmdHybridSearchEngine(workspace_root=str(tmp_path))
    long_lines = ["hit " + "q" * 500 for _ in range(20)]
    target = _write(tmp_path / "huge.conf", "\n".join(long_lines))

    snippet = engine._collect_grep_fallback_snippet(
        "huge.conf",
        target,
        0.5,
        [{"line": 10, "text": "hit"}],
    )

    assert snippet is not None
    assert MAX_LITERAL_PREVIEW_CHARS == 1800
    assert len(snippet["content"]) == MAX_LITERAL_PREVIEW_CHARS


# ---------------------------------------------------------------------------
# FRENTE E — Resolução estrita de scope relativo ao workspace
# ---------------------------------------------------------------------------


def test_resolve_scope_relative_strict(tmp_path: Path):
    """Relativo -> workspace; path-like inexistente -> vazio; basename -> glob."""
    _write(tmp_path / "sub" / "alpha.py", "def alpha(): ...\n")
    _write(tmp_path / "alpha.py", "def alpha(): ...\n")
    engine = QmdHybridSearchEngine(workspace_root=str(tmp_path))
    root = engine.workspace_root

    targets, globs = engine._resolve_scope(None)
    assert targets == [str(root)]
    assert globs == []

    targets, globs = engine._resolve_scope(["sub"])
    assert targets == [str((root / "sub").resolve())]
    assert globs == []

    # Basename puro que existe: alvo exato do arquivo.
    targets, globs = engine._resolve_scope(["alpha.py"])
    assert targets == [str((root / "alpha.py").resolve())]
    assert globs == []

    # Basename puro inexistente: compatibilidade via filtro de nome (nunca walk).
    targets, globs = engine._resolve_scope(["beta.py"])
    assert targets == [str(root)]
    assert globs == ["*beta.py*"]

    # Escopo path-like inexistente NUNCA amplia silenciosamente para o workspace.
    assert engine._resolve_scope(["nope/missing.py"]) == ([], [])


@pytest.mark.asyncio
async def test_search_scope_relative_and_missing_are_strict(tmp_path: Path):
    """Integração: scope relativo restringe; escopo inexistente devolve vazio."""
    body = "def unique_scope_marker():\n    return 'unique_scope_marker'\n"
    _write(tmp_path / "sub" / "alpha.py", body)
    _write(tmp_path / "alpha.py", body)
    engine = QmdHybridSearchEngine(workspace_root=str(tmp_path))

    scoped = await engine.search("unique_scope_marker", scope=["sub"], max_tokens=1500)
    assert {s["file"] for s in scoped["snippets"]} == {"sub/alpha.py"}

    basename = await engine.search(
        "unique_scope_marker", scope=["alpha.py"], max_tokens=1500
    )
    assert {s["file"] for s in basename["snippets"]} == {"alpha.py"}

    missing = await engine.search(
        "unique_scope_marker", scope=["nope/missing.py"], max_tokens=1500
    )
    assert missing["status"] == "success"
    assert missing["files_found"] == 0
    assert missing["snippets_count"] == 0
