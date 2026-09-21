import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.schemas import Needle3Query
from src.memory.local_needle import LocalNeedleEngine
from src.memory.needle_client import Needle3Client


class TestLocalNeedle(unittest.IsolatedAsyncioTestCase):
    async def test_local_engine_search(self):
        engine = LocalNeedleEngine()
        query = Needle3Query(
            query="NoulRequest hypothesis threshold",
            scope=["src/core"],
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
            query="OpenJevEngine evaluate_noul",
            scope=["src/decision"],
            max_tokens=600,
        )
        response = await client.search(query)
        self.assertIsNotNone(response)
        self.assertGreater(len(response.snippets), 0)
        self.assertIn("engine.py", response.snippets[0].file_path)


if __name__ == "__main__":
    unittest.main()
