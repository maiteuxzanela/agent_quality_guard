"""TICKET-HARNESS-41 — testes determinísticos do extrator multilang Needle 3.

Sem mocks (anti-mock fail-fast). O gate Tree-Sitter é lazy por-chamada
(``_ts_extract_enabled``), desligado por padrão devido ao use-after-free no
binding C (tree-sitter 0.26.0 + language-pack 1.22.1). Os testes de rota
Tree-Sitter usam apenas arquivos sintéticos mínimos (<60 linhas), que foram
stressados 30/30 sem crash; arquivos reais/mundanos NÃO são percorridos pelo
gate ligado neste arquivo.
"""

import time
from pathlib import Path

import pytest

from src.core.schemas import Needle3Query
from src.memory.local_needle import (
    LocalNeedleEngine,
    _ts_extract_enabled,
)


# --------------------------------------------------------------------------- #
# Gate lazy (por-chamada, não import-time)
# --------------------------------------------------------------------------- #
class TestTsExtractGate:
    def test_gate_off_by_default(self, monkeypatch):
        monkeypatch.delenv("NEEDLE_TS_EXTRACT", raising=False)
        assert _ts_extract_enabled() is False

    def test_gate_reads_env_per_call_not_import_time(self, monkeypatch):
        # Antes do set: off; depois do set: on; depois do reset: off de novo.
        monkeypatch.delenv("NEEDLE_TS_EXTRACT", raising=False)
        assert _ts_extract_enabled() is False
        monkeypatch.setenv("NEEDLE_TS_EXTRACT", "1")
        assert _ts_extract_enabled() is True
        monkeypatch.setenv("NEEDLE_TS_EXTRACT", "0")
        assert _ts_extract_enabled() is False


# --------------------------------------------------------------------------- #
# Paridade Python: dispatch == stdlib ast (nunca toca Tree-Sitter em .py)
# --------------------------------------------------------------------------- #
class TestPythonDispatchParity:
    PY_SRC = '''\
from dataclasses import dataclass


@dataclass
class Config:
    name: str
    retries: int = 3


def load_config(path: str) -> Config:
    return Config(name=path)


async def main():
    cfg = load_config("x")
    print(cfg.name)
'''

    def test_dispatch_equals_stdlib_ast_on_valid_python(self, tmp_path):
        f = tmp_path / "mod.py"
        f.write_text(self.PY_SRC, encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))

        via_dispatch = engine._extract_blocks_for_file(f)
        via_stdlib = engine._extract_ast_blocks(f)

        assert via_dispatch == via_stdlib
        names = {b["name"] for b in via_dispatch}
        assert names == {"Config", "load_config", "main"}

    def test_dispatch_class_relation_has_bases(self, tmp_path):
        f = tmp_path / "oop.py"
        f.write_text(
            "class Base:\n    pass\n\n\nclass Child(Base):\n    pass\n",
            encoding="utf-8",
        )
        engine = LocalNeedleEngine(str(tmp_path))
        blocks = engine._extract_blocks_for_file(f)
        child = next(b for b in blocks if b["name"] == "Child")
        assert any("type:class" in r and "bases:Base" in r for r in child["relations"])

    def test_invalid_python_falls_back_gracefully_without_exception(self, tmp_path):
        f = tmp_path / "broken.py"
        f.write_text("def broken(:\n    pass\n  ???\n", encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        blocks = engine._extract_blocks_for_file(f)  # não pode levantar
        assert isinstance(blocks, list)


# --------------------------------------------------------------------------- #
# Fallbacks por família com gate DESLIGADO (caminho de produção default)
# --------------------------------------------------------------------------- #
class TestFamilyFallbacksGateOff:
    def test_js_family_uses_regex_extractor(self, tmp_path, monkeypatch):
        monkeypatch.delenv("NEEDLE_TS_EXTRACT", raising=False)
        f = tmp_path / "comp.js"
        f.write_text(
            "function alpha() {\n  return 1;\n}\n\n"
            "const beta = () => 2;\n\nclass Gamma {}\n",
            encoding="utf-8",
        )
        engine = LocalNeedleEngine(str(tmp_path))
        blocks = engine._extract_blocks_for_file(f)
        names = {b["name"] for b in blocks}
        assert {"alpha", "beta", "Gamma"} <= names
        # regex path marca relation name: (não type: do Tree-Sitter)
        assert any(r.startswith("name:") for b in blocks for r in b["relations"])

    @pytest.mark.parametrize("ext,content", [
        ("go", "package main\n\ntype Shape interface {\n\tArea() float64\n}\n\ntype Rect struct {\n\tW float64\n}\n\nfunc (r Rect) Area() float64 { return r.W }\n"),
        ("rs", "pub struct User {\n    pub id: u32,\n}\n\nimpl User {\n    pub fn new() -> Self { User { id: 0 } }\n}\n"),
        ("dart", "class Point {\n  final double x;\n  Point(this.x);\n}\n\nvoid main() {}\n"),
        ("kt", "class Engine\n\ninterface Clickable\n"),
        ("java", "public class Item {\n    private int id;\n}\n"),
        ("cs", "public class Account {\n    public int Id { get; set; }\n}\n"),
    ])
    def test_non_js_families_fall_back_to_lexical_when_gate_off(
        self, tmp_path, monkeypatch, ext, content
    ):
        monkeypatch.delenv("NEEDLE_TS_EXTRACT", raising=False)
        f = tmp_path / f"sample.{ext}"
        f.write_text(content, encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        blocks = engine._extract_blocks_for_file(f)
        assert blocks, f"fallback léxico devolveu vazio para .{ext}"
        # com gate off, NENHUMA relação type: (Tree-Sitter) — só file: léxico
        for b in blocks:
            assert all(r.startswith("file:") for r in b["relations"]), b["relations"]

    def test_unknown_extension_uses_lexical(self, tmp_path):
        f = tmp_path / "notes.xyz"
        f.write_text("\n".join(f"line {i}" for i in range(120)), encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        blocks = engine._extract_blocks_for_file(f)
        assert blocks
        assert all(r.startswith("file:") for b in blocks for r in b["relations"])

    def test_lexical_chunks_are_50_lines(self, tmp_path):
        f = tmp_path / "big.go"
        f.write_text("\n".join(f"// l{i}" for i in range(120)), encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        blocks = engine._extract_blocks_for_file(f)
        assert len(blocks) == 3  # 120 linhas -> 50 + 50 + 20
        assert blocks[0]["start_line"] == 1
        assert blocks[0]["end_line"] == 50


# --------------------------------------------------------------------------- #
# Descoberta de candidatos: extensões novas + vendor/third_party ignorados
# --------------------------------------------------------------------------- #
class TestCandidateDiscovery:
    def test_discovers_new_multilang_extensions(self, tmp_path):
        for name in (
            "a.go", "b.rs", "c.dart", "d.kt", "e.java", "f.cs",
            "g.pyi", "h.mjs", "i.cjs", "j.mts", "k.cts",
        ):
            (tmp_path / name).write_text("// x\n", encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        found = {p.name for p in engine._find_candidate_files()}
        assert {"a.go", "b.rs", "c.dart", "d.kt", "e.java", "f.cs",
                "g.pyi", "h.mjs", "i.cjs", "j.mts", "k.cts"} <= found

    def test_skips_vendor_and_third_party(self, tmp_path):
        (tmp_path / "vendor").mkdir()
        (tmp_path / "vendor" / "lib.go").write_text("package v\n", encoding="utf-8")
        (tmp_path / "third_party").mkdir()
        (tmp_path / "third_party" / "dep.rs").write_text("fn x() {}\n", encoding="utf-8")
        (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        found = {p.name for p in engine._find_candidate_files()}
        assert "app.py" in found
        assert "lib.go" not in found
        assert "dep.rs" not in found


# --------------------------------------------------------------------------- #
# Contratos e2e: orçamento max_tokens e latência (<50ms quente)
# --------------------------------------------------------------------------- #
class TestBudgetAndLatency:
    def _fill_workspace(self, tmp_path: Path, n_classes: int = 40) -> None:
        parts = []
        for i in range(n_classes):
            parts.append(
                f"class Widget{i}:\n"
                f"    def render_{i}(self):\n"
                f"        return 'widget {i} padding payload'\n"
            )
        (tmp_path / "widgets.py").write_text("\n\n".join(parts), encoding="utf-8")

    async def test_max_tokens_budget_never_exceeded(self, tmp_path):
        self._fill_workspace(tmp_path)
        engine = LocalNeedleEngine(str(tmp_path))
        q = Needle3Query(query="widget render", scope=None, max_tokens=200)
        resp = await engine.search(q)
        assert resp.total_tokens <= 200
        assert len(resp.snippets) >= 1

    def test_single_file_extraction_under_50ms_warm(self, tmp_path):
        self._fill_workspace(tmp_path, n_classes=60)
        f = tmp_path / "widgets.py"
        engine = LocalNeedleEngine(str(tmp_path))
        engine._extract_blocks_for_file(f)  # aquecimento
        t0 = time.perf_counter()
        blocks = engine._extract_blocks_for_file(f)
        elapsed_ms = (time.perf_counter() - t0) * 1000.0
        assert len(blocks) == 60
        assert elapsed_ms < 50.0, f"extração quente levou {elapsed_ms:.2f}ms"


# --------------------------------------------------------------------------- #
# Rota Tree-Sitter (gate LIGADO) — apenas sintéticos mínimos, stressados 30/30
# --------------------------------------------------------------------------- #
class TestTreeSitterRouteGateOn:
    @pytest.fixture(autouse=True)
    def _gate_on(self, monkeypatch):
        monkeypatch.setenv("NEEDLE_TS_EXTRACT", "1")

    def _extract(self, tmp_path, name, content):
        f = tmp_path / name
        f.write_text(content, encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        return engine._extract_ts_blocks(f)

    def test_go_struct_interface_function_method(self, tmp_path):
        blocks = self._extract(
            tmp_path, "m.go",
            "package main\n\n"
            "type Shape interface {\n\tArea() float64\n}\n\n"
            "type Rect struct {\n\tW float64\n}\n\n"
            "func (r Rect) Area() float64 { return r.W }\n\n"
            "func NewRect(w float64) Rect { return Rect{W: w} }\n",
        )
        # interface + struct + method + function nomeados
        names = {b["name"] for b in blocks}
        assert {"Shape", "Rect", "NewRect"} <= names
        assert any("type:interface" in r for b in blocks for r in b["relations"])
        assert any("type:struct" in r for b in blocks for r in b["relations"])
        assert any("type:function" in r for b in blocks for r in b["relations"])

    def test_rust_struct_and_impl_method(self, tmp_path):
        blocks = self._extract(
            tmp_path, "lib.rs",
            "pub struct User {\n    pub id: u32,\n}\n\n"
            "impl User {\n    pub fn new() -> Self { User { id: 0 } }\n}\n",
        )
        names = {b["name"] for b in blocks}
        assert "User" in names
        assert any("type:struct" in r for b in blocks for r in b["relations"])
        # método de impl: promovido a method
        assert any("type:method" in r and "new" in r for b in blocks for r in b["relations"])

    def test_dart_class_and_top_level_function(self, tmp_path):
        blocks = self._extract(
            tmp_path, "app.dart",
            "class Point {\n  final double x;\n  Point(this.x);\n}\n\n"
            "int add(int a, int b) {\n  return a + b;\n}\n",
        )
        names = {b["name"] for b in blocks}
        assert {"Point", "add"} <= names
        assert any("type:class" in r for b in blocks for r in b["relations"])
        assert any("type:function" in r for b in blocks for r in b["relations"])

    def test_ts_interface_class_and_arrow_function(self, tmp_path):
        blocks = self._extract(
            tmp_path, "ui.ts",
            "interface Shape {\n  area(): number;\n}\n\n"
            "class Circle implements Shape {\n  area(): number { return 1; }\n}\n\n"
            "const areaOf = (r: number): number => 3.14 * r * r;\n",
        )
        names = {b["name"] for b in blocks}
        assert {"Shape", "Circle"} <= names
        assert any("type:interface" in r for b in blocks for r in b["relations"])
        # arrow function nomeada promovida a function
        assert "areaOf" in names

    def test_java_and_kotlin_and_csharp_classes(self, tmp_path):
        blocks = self._extract(
            tmp_path, "Item.java",
            "public class Item {\n    private int id;\n    public int getId() { return id; }\n}\n",
        )
        assert any(b["name"] == "Item" for b in blocks)

        blocks_kt = self._extract(
            tmp_path, "Engine.kt",
            "class Engine {\n    fun start() = true\n}\n",
        )
        assert any(b["name"] == "Engine" for b in blocks_kt)

        blocks_cs = self._extract(
            tmp_path, "Account.cs",
            "public class Account {\n    public int Id { get; set; }\n}\n",
        )
        assert any(b["name"] == "Account" for b in blocks_cs)

    def test_invalid_source_returns_empty_not_raise(self, tmp_path):
        blocks = self._extract(tmp_path, "bad.go", "func ((( broken syntax\n")
        assert blocks == []

    def test_python_parity_via_tree_sitter_route(self, tmp_path):
        # Com gate ligado, .py AINDA usa stdlib ast (nunca Tree-Sitter).
        f = tmp_path / "mod.py"
        f.write_text("def alpha():\n    return 1\n", encoding="utf-8")
        engine = LocalNeedleEngine(str(tmp_path))
        via_dispatch = engine._extract_blocks_for_file(f)
        via_stdlib = engine._extract_ast_blocks(f)
        assert via_dispatch == via_stdlib
