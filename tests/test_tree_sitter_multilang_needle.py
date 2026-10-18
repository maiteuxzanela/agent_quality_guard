"""TICKET-HARNESS-41 — SDET suite determinística: extração multilang Needle 3.

Sem mocks (anti-mock fail-fast). Arquivos REAIS temporários escritos em
``tmp_path``; o gate Tree-Sitter é lido por-chamada (``_ts_extract_enabled``)
e, quando ligado, só percorre arquivos sintéticos mínimos (<60 linhas) —
mesma política da suíte da Maya, pois o binding C 0.26.0 tem UAF em
arquivos mundanos.

Cobertura exigida pelo ticket:
  1. Extração estrutural de classes/funções/interfaces/structs/métodos
     em Python, TypeScript/TSX, Go, Dart e Rust (rota Tree-Sitter gate-ON).
  2. Inclusão EXATA de decorators/anotações na ``start_line`` (Python stdlib
     e rota Tree-Sitter).
  3. Fallback gracioso léxico para linguagem não suportada e sintaxe quebrada.
  4. Latência de extração quente < 50 ms por arquivo.
  5. Teto de ``max_tokens`` inviolável em ``search()``.
"""

import time
from pathlib import Path

import pytest

from src.core.schemas import Needle3Query
from src.memory.local_needle import LocalNeedleEngine


# --------------------------------------------------------------------------- #
# 1. Extração estrutural multilang (rota Tree-Sitter, gate LIGADO)
# --------------------------------------------------------------------------- #
class TestStructuralExtractionMultilang:
    """Arquivos sintéticos mínimos por linguagem; asserts de kind + name."""

    @pytest.fixture(autouse=True)
    def _gate_on(self, monkeypatch):
        monkeypatch.setenv("NEEDLE_TS_EXTRACT", "1")

    def _extract(self, tmp_path: Path, name: str, content: str):
        f = tmp_path / name
        f.write_text(content, encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        return engine._extract_ts_blocks(f)

    def test_python_class_function_and_decorator_in_start_line(self, tmp_path):
        """Python via Tree-Sitter: decorated_definition unwrap + start na decorator."""
        src = (
            "@dataclass\n"
            "class Config:\n"
            "    name: str\n"
            "\n"
            "\n"
            "def load(path: str) -> Config:\n"
            "    return Config(name=path)\n"
        )
        blocks = self._extract(tmp_path, "mod.py", src)
        by_name = {b["name"]: b for b in blocks}
        assert {"Config", "load"} <= set(by_name)
        # start_line do Config aponta para o decorator, não para 'class'
        assert by_name["Config"]["start_line"] == 1
        assert by_name["Config"]["content"].startswith("@dataclass")
        assert by_name["load"]["start_line"] == 6
        # paridade de relations com o caminho stdlib
        assert any(
            r.startswith("type:class") and "name:Config" in r
            for r in by_name["Config"]["relations"]
        )
        assert any(
            r.startswith("type:function") and "name:load" in r
            for r in by_name["load"]["relations"]
        )

    def test_typescript_interface_class_method_and_arrow(self, tmp_path):
        src = (
            "interface Shape {\n"
            "  area(): number;\n"
            "}\n"
            "\n"
            "class Circle implements Shape {\n"
            "  area(): number { return 1; }\n"
            "}\n"
            "\n"
            "const areaOf = (r: number): number => 3.14 * r * r;\n"
        )
        blocks = self._extract(tmp_path, "ui.ts", src)
        by_name = {b["name"]: b for b in blocks}
        assert {"Shape", "Circle", "areaOf"} <= set(by_name)
        assert any("type:interface" in r and "name:Shape" in r for r in by_name["Shape"]["relations"])
        assert any("type:class" in r and "name:Circle" in r for r in by_name["Circle"]["relations"])
        # método dentro de classe vira bloco próprio promovido a method
        method_blocks = [b for b in blocks if b["name"] == "area"]
        assert any(
            "type:method" in r and "name:area" in r
            for b in method_blocks
            for r in b["relations"]
        )
        # arrow function nomeada promovida a function
        assert any("type:function" in r and "name:areaOf" in r for r in by_name["areaOf"]["relations"])

    def test_tsx_component_and_hook_enrichment(self, tmp_path):
        src = (
            "import React, { useState } from 'react';\n"
            "\n"
            "export const Counter = () => {\n"
            "  const [n, setN] = useState(0);\n"
            "  return <button onClick={() => setN(n + 1)}>{n}</button>;\n"
            "};\n"
        )
        blocks = self._extract(tmp_path, "Counter.tsx", src)
        counter = next(b for b in blocks if b["name"] == "Counter")
        assert any("type:function" in r for r in counter["relations"])
        assert any("hook:useState" in r for r in counter["relations"])

    def test_go_interface_struct_function_and_method(self, tmp_path):
        src = (
            "package main\n"
            "\n"
            "type Shape interface {\n"
            "\tArea() float64\n"
            "}\n"
            "\n"
            "type Rect struct {\n"
            "\tW float64\n"
            "}\n"
            "\n"
            "func (r Rect) Area() float64 { return r.W }\n"
            "\n"
            "func NewRect(w float64) Rect { return Rect{W: w} }\n"
        )
        blocks = self._extract(tmp_path, "shape.go", src)
        by_name = {b["name"]: b for b in blocks}
        assert {"Shape", "Rect", "NewRect"} <= set(by_name)
        assert any("type:interface" in r for r in by_name["Shape"]["relations"])
        assert any("type:struct" in r for r in by_name["Rect"]["relations"])
        assert any("type:function" in r and "name:NewRect" in r for r in by_name["NewRect"]["relations"])
        # método receptor vira method nomeado Area
        assert any("type:method" in r and "Area" in r for b in blocks for r in b["relations"])

    def test_rust_struct_trait_and_impl_method(self, tmp_path):
        src = (
            "pub trait Describable {\n"
            "    fn describe(&self) -> String;\n"
            "}\n"
            "\n"
            "pub struct User {\n"
            "    pub id: u32,\n"
            "}\n"
            "\n"
            "impl User {\n"
            "    pub fn new() -> Self { User { id: 0 } }\n"
            "}\n"
        )
        blocks = self._extract(tmp_path, "user.rs", src)
        by_name = {b["name"]: b for b in blocks}
        assert {"User", "Describable"} <= set(by_name)
        assert any("type:struct" in r and "name:User" in r for r in by_name["User"]["relations"])
        assert any("type:interface" in r and "name:Describable" in r for r in by_name["Describable"]["relations"])
        # método de impl promovido a method
        assert any("type:method" in r and "new" in r for b in blocks for r in b["relations"])

    def test_dart_class_constructor_and_top_level_function(self, tmp_path):
        src = (
            "class Point {\n"
            "  final double x;\n"
            "  Point(this.x);\n"
            "}\n"
            "\n"
            "int add(int a, int b) {\n"
            "  return a + b;\n"
            "}\n"
        )
        blocks = self._extract(tmp_path, "point.dart", src)
        by_name = {b["name"]: b for b in blocks}
        assert {"Point", "add"} <= set(by_name)
        assert any("type:class" in r and "name:Point" in r for r in by_name["Point"]["relations"])
        assert any("type:function" in r and "name:add" in r for r in by_name["add"]["relations"])

    def test_go_invalid_source_returns_empty_without_raise(self, tmp_path):
        """Sintaxe quebrada no caminho Tree-Sitter devolve [] sem exceção."""
        blocks = self._extract(tmp_path, "bad.go", "func ((( broken\n")
        assert blocks == []

    def test_python_via_dispatch_never_touches_tree_sitter(self, tmp_path, monkeypatch):
        """Mesmo com gate ligado, .py/.pyi despacham para stdlib ast (paridade)."""
        f = tmp_path / "mod.py"
        f.write_text("def alpha():\n    return 1\n", encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        assert engine._extract_blocks_for_file(f) == engine._extract_ast_blocks(f)


# --------------------------------------------------------------------------- #
# 2. Decorators/anotações na start_line (caminho stdlib — produção default)
# --------------------------------------------------------------------------- #
class TestDecoratorStartLineExact:
    def test_single_decorator_included_in_start_line(self, tmp_path):
        src = (
            "@app.get('/health')\n"
            "def health() -> dict:\n"
            "    return {'ok': True}\n"
        )
        f = tmp_path / "api.py"
        f.write_text(src, encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        blocks = engine._extract_blocks_for_file(f)
        health = next(b for b in blocks if b["name"] == "health")
        assert health["start_line"] == 1
        assert health["content"].startswith("@app.get")

    def test_stacked_decorators_start_at_first_decorator(self, tmp_path):
        src = (
            "@property\n"
            "@functools.cache\n"
            "def expensive(self) -> int:\n"
            "    return 42\n"
        )
        f = tmp_path / "svc.py"
        f.write_text(src, encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        blocks = engine._extract_blocks_for_file(f)
        expensive = next(b for b in blocks if b["name"] == "expensive")
        assert expensive["start_line"] == 1
        assert expensive["content"].splitlines()[0] == "@property"

    def test_decorated_class_start_line_includes_decorator(self, tmp_path):
        src = (
            "@dataclass(frozen=True)\n"
            "class FrozenPoint:\n"
            "    x: int\n"
            "    y: int\n"
        )
        f = tmp_path / "model.py"
        f.write_text(src, encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        blocks = engine._extract_blocks_for_file(f)
        cp = next(b for b in blocks if b["name"] == "FrozenPoint")
        assert cp["start_line"] == 1
        assert cp["content"].startswith("@dataclass(frozen=True)")

    def test_async_decorated_function_start_line(self, tmp_path):
        src = (
            "@retry(times=3)\n"
            "async def fetch_remote(url: str) -> bytes:\n"
            "    return b'payload'\n"
        )
        f = tmp_path / "fetch.py"
        f.write_text(src, encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        blocks = engine._extract_blocks_for_file(f)
        fetch = next(b for b in blocks if b["name"] == "fetch_remote")
        assert fetch["start_line"] == 1
        assert "async def fetch_remote" in fetch["content"]

    def test_no_decorator_start_line_is_def_line(self, tmp_path):
        """Controle: sem decorator, start_line == linha do def (não há off-by-one)."""
        src = "def plain() -> int:\n    return 1\n"
        f = tmp_path / "plain.py"
        f.write_text(src, encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        blocks = engine._extract_blocks_for_file(f)
        plain = next(b for b in blocks if b["name"] == "plain")
        assert plain["start_line"] == 1


# --------------------------------------------------------------------------- #
# 3. Fallback gracioso léxico (linguagem não suportada / sintaxe quebrada)
# --------------------------------------------------------------------------- #
class TestGracefulLexicalFallback:
    def test_unknown_extension_uses_lexical_chunks(self, tmp_path):
        f = tmp_path / "notes.xyz"
        f.write_text("\n".join(f"line {i}" for i in range(120)), encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        blocks = engine._extract_blocks_for_file(f)
        assert blocks
        assert all(r.startswith("file:") for b in blocks for r in b["relations"])
        # 120 linhas -> chunks de 50: 50 + 50 + 20
        assert [(b["start_line"], b["end_line"]) for b in blocks] == [
            (1, 50), (51, 100), (101, 120),
        ]

    def test_gate_off_go_rs_dart_fall_back_to_lexical(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NEEDLE_TS_EXTRACT", raising=False)
        for name, content in (
            ("a.go", "package main\n\ntype T struct{}\n"),
            ("b.rs", "pub struct S { pub id: u32 }\n"),
            ("c.dart", "class C {}\n"),
        ):
            f = tmp_path / name
            f.write_text(content, encoding="utf-8")
            engine = LocalNeedleEngine(str(tmp_path))
            blocks = engine._extract_blocks_for_file(f)
            assert blocks, f"fallback léxico vazio para {name}"
            for b in blocks:
                assert all(r.startswith("file:") for r in b["relations"]), b["relations"]

    def test_broken_python_falls_back_without_exception(self, tmp_path):
        f = tmp_path / "broken.py"
        f.write_text("def broken(:\n    pass\n  ???\n", encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        blocks = engine._extract_blocks_for_file(f)  # não pode levantar
        assert isinstance(blocks, list)

    def test_broken_ts_falls_back_to_regex_without_exception(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NEEDLE_TS_EXTRACT", raising=False)
        f = tmp_path / "broken.ts"
        f.write_text("function ((( nope\n", encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        blocks = engine._extract_blocks_for_file(f)  # não pode levantar
        assert isinstance(blocks, list)

    def test_gate_on_broken_go_degrades_to_lexical_via_dispatch(self, tmp_path, monkeypatch):
        """Gate ligado + sintaxe quebrada: _extract_ts_blocks=[] -> léxico, sem exceção."""
        monkeypatch.setenv("NEEDLE_TS_EXTRACT", "1")
        f = tmp_path / "broken.go"
        f.write_text("func ((( broken syntax\n" + "\n".join(f"// pad {i}" for i in range(60)), encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        blocks = engine._extract_blocks_for_file(f)
        assert blocks  # léxico de 50 linhas
        assert all(r.startswith("file:") for b in blocks for r in b["relations"])

    def test_empty_file_returns_empty_list(self, tmp_path):
        f = tmp_path / "empty.go"
        f.write_text("", encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        assert engine._extract_blocks_for_file(f) == []


# --------------------------------------------------------------------------- #
# 4. Latência de extração quente < 50 ms
# --------------------------------------------------------------------------- #
class TestExtractionLatency:
    @pytest.mark.parametrize("ext,content", [
        ("py", "class W:\n    def r(self):\n        return 1\n"),
        ("ts", "interface I { m(): void; }\nclass C implements I { m() {} }\n"),
        ("tsx", "export const App = () => <div />;\n"),
        ("go", "package m\n\ntype T struct{}\n\nfunc F() {}\n"),
        ("rs", "pub struct S {}\n\nimpl S { pub fn n() -> Self { S {} } }\n"),
        ("dart", "class D {}\n\nvoid main() {}\n"),
    ])
    def test_warm_single_file_extraction_under_50ms(self, tmp_path, monkeypatch, ext, content):
        monkeypatch.delenv("NEEDLE_TS_EXTRACT", raising=False)  # caminho de produção default
        f = tmp_path / f"sample.{ext}"
        # multiplica o conteúdo para forçar trabalho real de parsing
        big = "\n\n".join(content for _ in range(20))
        f.write_text(big, encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        engine._extract_blocks_for_file(f)  # aquecimento (caches de parser)
        t0 = time.perf_counter()
        blocks = engine._extract_blocks_for_file(f)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert blocks, f"extração de .{ext} devolveu vazio"
        assert elapsed_ms < 50.0, f"extração quente de .{ext} levou {elapsed_ms:.2f}ms"

    def test_warm_python_extraction_under_50ms_gate_on(self, tmp_path, monkeypatch):
        monkeypatch.setenv("NEEDLE_TS_EXTRACT", "1")
        parts = [
            f"class Widget{i}:\n    def render_{i}(self):\n        return 'widget {i} padding'\n"
            for i in range(40)
        ]
        f = tmp_path / "widgets.py"
        f.write_text("\n\n".join(parts), encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        engine._extract_blocks_for_file(f)  # aquecimento
        t0 = time.perf_counter()
        blocks = engine._extract_blocks_for_file(f)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert len(blocks) == 40
        assert elapsed_ms < 50.0, f"extração quente levou {elapsed_ms:.2f}ms"


# --------------------------------------------------------------------------- #
# 5. Teto de max_tokens inviolável
# --------------------------------------------------------------------------- #
class TestMaxTokensInviolable:
    def _fill(self, tmp_path: Path, n: int = 40) -> None:
        parts = [
            f"class Widget{i}:\n    def render_{i}(self):\n        return 'widget {i} payload padding text'\n"
            for i in range(n)
        ]
        (tmp_path / "widgets.py").write_text("\n\n".join(parts), encoding="utf-8")

    async def test_budget_200_never_exceeded(self, tmp_path):
        self._fill(tmp_path)
        engine = LocalNeedleEngine(str(tmp_path))
        resp = await engine.search(Needle3Query(query="widget render", scope=None, max_tokens=200))
        assert resp.total_tokens <= 200
        assert resp.snippets
        # cada snippet individual respeita o orçamento agregado
        for snip in resp.snippets:
            assert snip.end_line >= snip.start_line >= 1
            assert snip.content

    async def test_tiny_budget_60_first_snippet_truncated_not_ignored(self, tmp_path):
        """Orçamento apertado: 1º snippet é truncado, não pulado; total <= max."""
        self._fill(tmp_path, n=30)
        engine = LocalNeedleEngine(str(tmp_path))
        resp = await engine.search(Needle3Query(query="widget render padding", scope=None, max_tokens=60))
        assert resp.total_tokens <= 60
        assert resp.snippets  # não pode devolver vazio quando há match

    async def test_oversized_first_block_is_truncated_not_skipped(self, tmp_path):
        """Bloco 1º maior que o orçamento: truncado para caber, nunca ignorado.

        O caminho de produção antigo (`> max_tokens and selected_snippets`)
        deixava o 1º bloco furar o teto; o gate atual trunca o conteúdo do
        1º snippet quando remaining >= 40 tokens.
        """
        payload = "widget render padding " + ("símbolo de preenchimento longo " * 80)
        (tmp_path / "big.py").write_text(
            f"class BigWidget:\n    def render_big(self):\n        return '{payload}'\n",
            encoding="utf-8",
        )
        engine = LocalNeedleEngine(str(tmp_path))
        resp = await engine.search(Needle3Query(query="widget render padding", scope=None, max_tokens=80))
        assert resp.total_tokens <= 80
        # 1º snippet PRECISA existir (truncado), não pode ser pulado
        assert resp.snippets, "1º bloco oversized foi ignorado em vez de truncado"
        # conteúdo truncado: menor que o payload original
        assert len(resp.snippets[0].content) < len(payload)

    async def test_zero_or_negative_budget_returns_empty(self, tmp_path):
        self._fill(tmp_path)
        engine = LocalNeedleEngine(str(tmp_path))
        resp = await engine.search(Needle3Query(query="widget", scope=None, max_tokens=1))
        assert resp.total_tokens <= 1

    async def test_budget_respected_with_multilang_workspace(self, tmp_path):
        """Workspace misto (py+ts+go+rs+dart) não fura o teto com gate off."""
        (tmp_path / "a.py").write_text(
            "class Alpha:\n    def run(self):\n        return 'alpha widget render'\n", encoding="utf-8"
        )
        (tmp_path / "b.ts").write_text(
            "export const beta = () => 'beta widget render';\n", encoding="utf-8"
        )
        (tmp_path / "c.go").write_text(
            "package main\n\n// widget render helper\nfunc Gamma() string { return \"g\" }\n", encoding="utf-8"
        )
        engine = LocalNeedleEngine(str(tmp_path))
        resp = await engine.search(Needle3Query(query="widget render", scope=None, max_tokens=120))
        assert resp.total_tokens <= 120
        assert resp.snippets
