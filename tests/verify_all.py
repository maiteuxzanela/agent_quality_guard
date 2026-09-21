"""Script de verificação completa utilizando a biblioteca padrão (unittest/asyncio).
Executa todos os testes de schemas, hardware, engine fail-fast, server API e orquestrador.
"""

import asyncio
import sys
import unittest
from pathlib import Path

# Adiciona a raiz do projeto ao sys.path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.schemas import (
    ChoiceRequest,
    ChoiceResponse,
    Needle3NotAvailableError,
    Needle3Query,
    Needle3Response,
    Needle3Snippet,
    NoulRequest,
    NoulResponse,
    OpenJevNotReadyError,
    ScoreRequest,
    ScoreResponse,
)
from src.core.tools import TOOL_DEFINITIONS, get_tool_definition
from src.decision.engine import OpenJevEngine
from src.decision.hardware import (
    build_max_memory_map,
    get_hardware_profile,
    sanitize_torch_dtype,
)
from src.decision.server import create_app
from src.memory.needle_client import Needle3Client
from src.orchestrator.evaluator import DeliveryEvaluator
from src.orchestrator.guardrail import GuardrailInterceptor
from src.orchestrator.router import SpecialistRouter
from tests.test_circuit_breaker import TestCircuitBreakerAndAntiGaming


class TestSchemas(unittest.TestCase):
    def test_noul_valid(self):
        req = NoulRequest(premise="Command rm", hypothesis="Safe", threshold=0.85)
        self.assertEqual(req.threshold, 0.85)
        res = NoulResponse(passed=True, probability=0.92, confidence=0.84, threshold=0.85, latency_ms=10.0)
        self.assertTrue(res.passed)

    def test_choice_validation(self):
        req = ChoiceRequest(premise="Fix SQL", options=["backend", "security"])
        self.assertEqual(len(req.options), 2)
        with self.assertRaises(Exception):
            ChoiceRequest(premise="Fix SQL", options=["backend", "backend"])

    def test_score_validation(self):
        req = ScoreRequest(premise="code", rubric="clean", min_score=1.0, max_score=5.0, threshold=4.0)
        self.assertEqual(req.threshold, 4.0)
        with self.assertRaises(Exception):
            ScoreRequest(premise="code", rubric="clean", min_score=5.0, max_score=1.0)


class TestHardware(unittest.TestCase):
    def test_bfloat16_blocked(self):
        sanitized = sanitize_torch_dtype("torch.bfloat16")
        self.assertNotIn("bfloat16", str(sanitized).lower())

    def test_hardware_profile(self):
        profile = get_hardware_profile()
        self.assertIsNotNone(profile)
        self.assertIsInstance(profile.has_cuda, bool)
        self.assertIn(profile.recommended_dtype, ("float16", "float32"))

    def test_memory_map(self):
        mem_map = build_max_memory_map(max_vram_mb=3000, cpu_ram_gb=12)
        self.assertEqual(mem_map[0], "3000MiB")
        self.assertEqual(mem_map["cpu"], "12GiB")


class TestToolsCatalog(unittest.TestCase):
    def test_all_tools_present(self):
        names = [t["function"]["name"] for t in TOOL_DEFINITIONS]
        self.assertIn("needle3_relational_search", names)
        self.assertIn("openjev_noul_guardrail", names)
        self.assertIn("openjev_choice_route", names)
        self.assertIn("openjev_score_evaluate", names)

    def test_get_tool(self):
        tool = get_tool_definition("openjev_noul_guardrail")
        self.assertEqual(tool["type"], "function")


import json

async def asgi_request(app, method: str, path: str, json_body=None):
    body_bytes = json.dumps(json_body).encode("utf-8") if json_body is not None else b""
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body_bytes)).encode("utf-8"))] if json_body is not None else []
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method.upper(),
        "path": path,
        "raw_path": path.encode("utf-8"),
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
        "scheme": "http",
    }
    status_code = 500
    response_body = []

    async def receive():
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    async def send(message):
        nonlocal status_code, response_body
        if message["type"] == "http.response.start":
            status_code = message["status"]
        elif message["type"] == "http.response.body":
            response_body.append(message.get("body", b""))

    await app(scope, receive, send)
    body_str = b"".join(response_body).decode("utf-8")
    return status_code, json.loads(body_str) if body_str else {}


class TestServerAPI(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.app = create_app(auto_load_model=False)

    async def test_health(self):
        status_code, data = await asgi_request(self.app, "GET", "/v1/decision/health")
        self.assertEqual(status_code, 200)
        self.assertEqual(data["status"], "not_ready")
        self.assertFalse(data["is_ready"])

    async def test_failfast_noul(self):
        status_code, data = await asgi_request(self.app, "POST", "/v1/decision/noul", json_body={"premise": "a", "hypothesis": "b"})
        self.assertEqual(status_code, 503)
        self.assertEqual(data["error"], "OpenJevNotReadyError")

    async def test_failfast_choice(self):
        status_code, data = await asgi_request(self.app, "POST", "/v1/decision/choice", json_body={"premise": "a", "options": ["x", "y"]})
        self.assertEqual(status_code, 503)
        self.assertEqual(data["error"], "OpenJevNotReadyError")

    async def test_failfast_score(self):
        status_code, data = await asgi_request(self.app, "POST", "/v1/decision/score", json_body={"premise": "a", "rubric": "b"})
        self.assertEqual(status_code, 503)
        self.assertEqual(data["error"], "OpenJevNotReadyError")


class TestAsyncComponents(unittest.IsolatedAsyncioTestCase):
    async def test_engine_unloaded_raises(self):
        engine = OpenJevEngine()
        with self.assertRaises(OpenJevNotReadyError):
            await engine.evaluate_noul(NoulRequest(premise="p", hypothesis="h"))
        with self.assertRaises(OpenJevNotReadyError):
            await engine.evaluate_choice(ChoiceRequest(premise="p", options=["a", "b"]))
        with self.assertRaises(OpenJevNotReadyError):
            await engine.evaluate_score(ScoreRequest(premise="p", rubric="r"))

    async def test_guardrail_interceptor(self):
        engine = OpenJevEngine()
        interceptor = GuardrailInterceptor(engine=engine)
        self.assertTrue(interceptor.is_action_critical("run_shell_command"))
        self.assertTrue(interceptor.is_action_critical("delete_file"))
        self.assertFalse(interceptor.is_action_critical("read_file"))
        with self.assertRaises(OpenJevNotReadyError):
            await interceptor.verify_action("run_shell_command", "rm -rf /")

    async def test_specialist_router(self):
        engine = OpenJevEngine()
        router = SpecialistRouter(engine=engine)
        with self.assertRaises(OpenJevNotReadyError):
            await router.route_task("Task description")

    async def test_delivery_evaluator(self):
        engine = OpenJevEngine()
        evaluator = DeliveryEvaluator(engine=engine)
        with self.assertRaises(OpenJevNotReadyError):
            await evaluator.evaluate_delivery("artifact diff")

    async def test_needle3_client_unreachable(self):
        client = Needle3Client(mode="cloud", api_key="test-key", api_endpoint="http://127.0.0.1:59999", timeout_seconds=0.5)
        with self.assertRaises(Needle3NotAvailableError):
            await client.search(Needle3Query(query="test"))

    def test_needle3_ast_scan(self):
        client = Needle3Client()
        relations = client.scan_local_ast_relations("src/core/schemas.py")
        self.assertTrue(len(relations) > 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
