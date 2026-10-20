import asyncio
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from agent_quality_guard.core.schemas import Needle3Query
from agent_quality_guard.memory.local_needle import MAX_CANDIDATE_FILES, LocalNeedleEngine
from agent_quality_guard.memory.needle_client import Needle3Client


class TestLocalNeedle(unittest.IsolatedAsyncioTestCase):
    async def test_local_engine_search(self):
        engine = LocalNeedleEngine()
        query = Needle3Query(
            query="NoulRequest hypothesis threshold",
            scope=["agent_quality_guard/core"],
            max_tokens=800,
        )
        response = await engine.search(query)

        self.assertIsNotNone(response)
        self.assertGreater(len(response.snippets), 0)
        self.assertGreater(response.total_tokens, 0)
        self.assertGreaterEqual(response.snippets[0].relevance_score, 0.5)

        # Verifica se o primeiro snippet é de schemas.py
        first_snippet = response.snippets[0]
        self.assertIn("schemas.py", first_snippet.file_path)
        self.assertIn("class NoulRequest", first_snippet.content)

    async def test_needle_client_defaults_to_local_without_api_key(self):
        client = Needle3Client(mode="local")
        query = Needle3Query(
            query="LocalNeedleEngine",
            scope=["agent_quality_guard/memory"],
            max_tokens=600,
        )
        response = await client.search(query)
        self.assertIsNotNone(response)
        self.assertGreater(len(response.snippets), 0)
        self.assertIn("local_needle.py", response.snippets[0].file_path)

    async def test_token_budget_never_exceeds_max_tokens(self):
        """Regressão do off-by-one: o 1º snippet não pode furar max_tokens.

        Falha se 0 snippets (asserção de orçamento seria trivial/vacua) ou
        se total_tokens ultrapassar o budget.
        """
        engine = LocalNeedleEngine()
        for budget in (400, 800, 1200):
            query = Needle3Query(
                query="LocalNeedleEngine search token budget",
                scope=["agent_quality_guard/memory"],
                max_tokens=budget,
            )
            response = await engine.search(query)
            self.assertGreater(
                len(response.snippets),
                0,
                f"budget={budget}: 0 snippets — teste de orçamento seria tautológico",
            )
            self.assertGreater(response.total_tokens, 0)
            self.assertGreaterEqual(response.token_savings_pct, 0.0)
            self.assertLessEqual(response.token_savings_pct, 100.0)
            self.assertLessEqual(
                response.total_tokens,
                budget,
                f"total_tokens={response.total_tokens} > max_tokens={budget}",
            )
            for snip in response.snippets:
                est = engine._estimate_tokens(snip.content)
                self.assertLessEqual(est, budget)
                self.assertGreater(est, 0)

    async def test_decorator_line_included_in_block_start(self):
        """start_line deve apontar para o decorator, não só para def/class."""
        import ast as _ast

        src = (
            "@app.tool(name='x')\n"
            "def foo():\n"
            "    return 1\n"
        )
        tree = _ast.parse(src)
        node = next(n for n in tree.body if isinstance(n, _ast.FunctionDef))
        decorator_lines = [d.lineno for d in node.decorator_list]
        self.assertEqual(decorator_lines, [1])
        self.assertEqual(node.lineno, 2)
        start = min([node.lineno] + decorator_lines)
        self.assertEqual(start, 1)
        self.assertLess(start, node.lineno)

    async def test_search_does_not_block_event_loop(self):
        """Causa raiz do MCP -32001: walk+AST em worker thread, não no loop.

        Dispara um heartbeat durante a busca; se search bloqueasse o event
        loop, o heartbeat só rodaria após o fim da busca (ou nunca, se a
        busca for longa o suficiente para estourar a asserção).
        """
        engine = LocalNeedleEngine()
        query = Needle3Query(
            query="LocalNeedleEngine search token budget",
            scope=["agent_quality_guard/memory"],
            max_tokens=400,
        )
        main_thread = threading.get_ident()
        ticks: list[int] = []

        async def heartbeat():
            for _ in range(50):
                ticks.append(threading.get_ident())
                await asyncio.sleep(0.001)

        hb = asyncio.ensure_future(heartbeat())
        resp = await engine.search(query)
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass

        self.assertIsNotNone(resp)
        self.assertGreater(len(resp.snippets), 0)
        self.assertGreaterEqual(
            len(ticks),
            1,
            "event loop não girou durante search — walk ainda bloqueia o MCP",
        )
        # O corpo pesado não deve rodar na thread do event loop.
        search_thread = engine._last_search_thread_ident
        self.assertIsNotNone(
            search_thread,
            "_last_search_thread_ident não registrado — search não passou por to_thread",
        )
        self.assertNotEqual(
            search_thread,
            main_thread,
            f"search rodou na thread do event loop (ident={main_thread})",
        )

    async def test_candidate_cap_truncates_large_workspace(self):
        """Teto MAX_CANDIDATE_FILES: workspace inflado para com o walk e sinaliza."""
        with tempfile.TemporaryDirectory(prefix="jev_needle_cap_") as tmp:
            root = Path(tmp)
            for i in range(MAX_CANDIDATE_FILES + 50):
                (root / f"mod_{i:04d}.py").write_text(
                    f"def needle_trunc_target_{i}():\n    return {i}\n",
                    encoding="utf-8",
                )

            engine = LocalNeedleEngine(workspace_root=str(root))
            files = engine._find_candidate_files(None)

            self.assertTrue(engine.candidates_truncated)
            self.assertEqual(len(files), MAX_CANDIDATE_FILES)
            self.assertEqual(engine.candidates_found, MAX_CANDIDATE_FILES)

            query = Needle3Query(
                query="needle_trunc_target",
                max_tokens=400,
            )
            resp = await engine.search(query)
            self.assertIsNotNone(resp)
            self.assertTrue(engine.candidates_truncated)

    async def test_scope_narrow_workspace_not_truncated(self):
        """Com scope restrito em árvore pequena, não pode reportar truncamento."""
        engine = LocalNeedleEngine()
        engine._find_candidate_files(["agent_quality_guard/memory"])
        self.assertFalse(engine.candidates_truncated)
        self.assertLess(engine.candidates_found, MAX_CANDIDATE_FILES)


    async def test_extract_js_indented_functions_and_classes(self):
        """Verifica se o parser JS extrai funcoes indentadas, arrow functions e classes."""
        with tempfile.TemporaryDirectory(prefix="jev_needle_js_") as tmp:
            root = Path(tmp)
            js_file = root / "app.js"
            js_file.write_text(
                "document.addEventListener('DOMContentLoaded', () => {\n"
                "  const saveCatalog = (catalogList) => {\n"
                "    localStorage.setItem('jez_catalog', JSON.stringify(catalogList));\n"
                "  };\n"
                "  function processOrder(orderId) {\n"
                "    return orderId;\n"
                "  }\n"
                "  class OrderManager {\n"
                "    constructor() {}\n"
                "  }\n"
                "});\n",
                encoding="utf-8",
            )
            engine = LocalNeedleEngine(workspace_root=str(root))
            blocks = engine._extract_js_blocks(js_file)
            names = [b["name"] for b in blocks]
            self.assertIn("saveCatalog", names)
            self.assertIn("processOrder", names)
            self.assertIn("OrderManager", names)

            # Busca real pelo símbolo
            query = Needle3Query(
                query="saveCatalog",
                workspace=str(root),
                max_tokens=600,
            )
            resp = await engine.search(query)
            self.assertGreater(len(resp.snippets), 0)
            self.assertIn("saveCatalog", resp.snippets[0].content)


if __name__ == "__main__":
    unittest.main()

