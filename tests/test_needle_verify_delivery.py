"""Suíte de testes de regressão determinísticos para a ferramenta MCP needle_verify_delivery.

Garante 100% de aprovação factual sem o uso de mocks em 3 cenários principais:
1. Workspace limpo com testes de unidade passando -> APPROVED (passed=True, pytest_success=True)
2. Workspace com violações de AST sob src/ (ex: import unittest.mock) -> REJECTED (passed=False)
3. 5ª tentativa de homologação -> Saída de Emergência EMERGENCY_RELEASE (passed=True)
"""

import os
import subprocess
import tempfile
from pathlib import Path
import pytest

from src.mcp_server import needle_verify_delivery


@pytest.mark.asyncio
async def test_needle_verify_delivery_clean_workspace_approved():
    """Teste 1: Invocação em workspace limpo com testes passando.
    
    Deve retornar passed: True, status: "APPROVED", pytest_success: True.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Inicializa repositório Git
        subprocess.run(["git", "init"], cwd=tmp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@jev.local"], cwd=tmp_dir, check=True)
        subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=tmp_dir, check=True)

        # Cria código de produção limpo
        src_dir = Path(tmp_dir) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "math_ops.py").write_text(
            "def multiply(a: int, b: int) -> int:\n"
            "    \"\"\"Multiplica dois inteiros.\"\"\"\n"
            "    return a * b\n",
            encoding="utf-8",
        )

        # Cria teste de unidade passando
        tests_dir = Path(tmp_dir) / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_math_ops.py").write_text(
            "from src.math_ops import multiply\n\n"
            "def test_multiply():\n"
            "    assert multiply(3, 4) == 12\n",
            encoding="utf-8",
        )

        # Adiciona e faz commit inicial
        subprocess.run(["git", "add", "."], cwd=tmp_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_dir, check=True)

        # Executa homologação determinística
        resp = await needle_verify_delivery(
            workspace=tmp_dir,
            attempt_number=1,
        )

        assert resp["passed"] is True
        assert resp["status"] == "APPROVED"
        assert resp["pytest_success"] is True
        assert resp["blocking_ast_violations"] == []


@pytest.mark.asyncio
async def test_needle_verify_delivery_ast_violation_rejected():
    """Teste 2: Invocação em workspace com violação determinística de AST.
    
    Import de unittest.mock em arquivo sob src/ deve retornar passed: False,
    status: "REJECTED", contendo blocking_ast_violations.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        # Inicializa repositório Git
        subprocess.run(["git", "init"], cwd=tmp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@jev.local"], cwd=tmp_dir, check=True)
        subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=tmp_dir, check=True)

        # Commit inicial para ter baseline
        readme = Path(tmp_dir) / "README.md"
        readme.write_text("# Test Project\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_dir, check=True)

        # Cria arquivo com import proibido sob src/
        src_dir = Path(tmp_dir) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "mock_service.py").write_text(
            "import unittest.mock\n\n"
            "def get_mock():\n"
            "    return unittest.mock.MagicMock()\n",
            encoding="utf-8",
        )

        # Cria teste passando
        tests_dir = Path(tmp_dir) / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_dummy.py").write_text(
            "def test_dummy():\n"
            "    assert True\n",
            encoding="utf-8",
        )

        # Executa homologação determinística
        resp = await needle_verify_delivery(
            workspace=tmp_dir,
            attempt_number=1,
            pedido="Integrar serviço externo",
            resultado="mock_service.py implementado com mock",
        )

        assert resp["passed"] is False
        assert resp["status"] == "REJECTED"
        assert resp["pedido_audit"] == "Integrar serviço externo"
        assert resp["resultado_audit"] == "mock_service.py implementado com mock"
        assert len(resp["blocking_ast_violations"]) >= 1
        violation_rules = [v["rule_id"] for v in resp["blocking_ast_violations"]]
        assert "mock_in_production" in violation_rules


@pytest.mark.asyncio
async def test_needle_verify_delivery_circuit_breaker_emergency_release():
    """Teste 3: 5ª tentativa real (contagem server-side) -> Saída de Emergência.

    O contador vem exclusivamente do histórico persistido por workspace: as 4
    primeiras chamadas reprovam (workspace sem suíte) e avançam a contagem.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        for expected_attempt in range(1, 5):
            resp = await needle_verify_delivery(workspace=tmp_dir)
            assert resp["passed"] is False
            assert resp["status"] == "REJECTED"
            assert resp["attempt_number"] == expected_attempt
            assert resp["approved_with_caveat"] is False

        resp = await needle_verify_delivery(workspace=tmp_dir)
        assert resp["passed"] is True
        assert resp["status"] == "EMERGENCY_RELEASE"
        assert resp["attempt_number"] == 5
        assert resp["pytest_success"] is False, (
            "Saída de Emergência não executa pytest e não pode reportá-lo como sucesso"
        )
        assert resp["pytest_chunks"] == []
        assert resp["approved_with_caveat"] is True


@pytest.mark.asyncio
async def test_client_cannot_forge_emergency_on_first_attempt():
    """BLOQUEANTE corrigido (auditoria 09/2026): attempt_number do chamador não destrava emergência.

    Antes da correção, `attempt_number=5` já na 1ª chamada devolvia
    EMERGENCY_RELEASE com pytest_success=True sem executar pytest, Guard nem
    Screener. A tentativa é resolvida server-side pelo histórico.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        resp = await needle_verify_delivery(workspace=tmp_dir, attempt_number=5)

        assert resp["status"] != "EMERGENCY_RELEASE"
        assert resp["attempt_number"] == 1
        assert resp["approved_with_caveat"] is False


@pytest.mark.asyncio
async def test_workspace_without_tests_is_rejected():
    """BLOQUEANTE corrigido: workspace sem suíte real não pode ser homologado.

    O runner devolvia `pytest_success=True, chunks=[]` para diretório sem
    `tests/` — aprovação sem executar um único teste.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        resp = await needle_verify_delivery(workspace=tmp_dir)

        assert resp["passed"] is False
        assert resp["status"] == "REJECTED"
        assert resp["pytest_success"] is False
        assert resp["pytest_chunks"] == []
        assert "Nenhum teste coletado" in resp["message"]


def test_history_clear_is_scoped_and_state_dir_is_isolated():
    """BLOQUEANTE corrigido: a suíte não pode apagar o estado do workspace real.

    `history_clear()` global limpava `/tmp/openjev_state` inteiro (registro de
    homologação + contador de tentativas de todos os workspaces). Com
    `JEV_STATE_DIR` (tests/conftest.py) a suíte opera em diretório próprio.
    """
    import json

    from src.core.score_gate import _get_state_path, history_clear, history_set

    real_state_dir = Path(tempfile.gettempdir()) / "openjev_state"
    sentinel = real_state_dir / "score_state_deadbeef.json"
    real_state_dir.mkdir(parents=True, exist_ok=True)
    sentinel.write_text(json.dumps({"history": {"passed": False, "attempts": 4}}), encoding="utf-8")

    try:
        ws = "/workspace/estado_isolado"
        history_set(ws, {"passed": False, "attempts": 3})

        state_path = _get_state_path(ws)
        assert str(state_path).startswith(os.environ["JEV_STATE_DIR"]), (
            f"estado do teste vazou para fora do diretório isolado: {state_path}"
        )

        history_clear()

        assert sentinel.exists(), (
            "history_clear() da suíte apagou estado de OUTRO workspace — "
            "o circuit breaker e o registro de homologação ficariam sem persistência"
        )
        assert not state_path.exists(), "reset do próprio estado do teste falhou"
    finally:
        if sentinel.exists():
            sentinel.unlink()


class DeterministicNoulEngine:
    """Implementação real e determinística da interface do motor para testes de arbitragem."""

    def __init__(self, probability: float = 0.90, is_ready: bool = True):
        self.probability = probability
        self.is_ready = is_ready

    async def evaluate_noul(self, req):
        from src.core.schemas import NoulResponse
        passed = self.probability >= req.threshold
        return NoulResponse(
            passed=passed,
            probability=self.probability,
            confidence=round(abs(self.probability - 0.5) * 2.0, 4),
            threshold=req.threshold,
            latency_ms=10.0,
        )


@pytest.mark.asyncio
async def test_needle_verify_delivery_noul_arbitrage_confirms_defect():
    """Teste 4: Camada 3 Noul confirma defeito estrutural (P >= 0.78).

    Deve rejeitar a entrega (status: REJECTED, passed: False) e listar o defeito em confirmed_defects.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        subprocess.run(["git", "init"], cwd=tmp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@jev.local"], cwd=tmp_dir, check=True)
        subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=tmp_dir, check=True)

        # Baseline inicial commitado
        (Path(tmp_dir) / "README.md").write_text("# Project\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_dir, check=True)

        # Código de produção em alteração ativa com alerta de tipagem/docstring
        src_dir = Path(tmp_dir) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "untyped.py").write_text(
            "def calculate_total(subtotal, tax):\n"
            "    return subtotal + tax\n",
            encoding="utf-8",
        )

        # Teste passando
        tests_dir = Path(tmp_dir) / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_untyped.py").write_text(
            "from src.untyped import calculate_total\n\n"
            "def test_calculate_total():\n"
            "    assert calculate_total(100, 10) == 110\n",
            encoding="utf-8",
        )

        engine = DeterministicNoulEngine(probability=0.88, is_ready=True)

        resp = await needle_verify_delivery(
            workspace=tmp_dir,
            attempt_number=1,
            engine=engine,
        )

        assert resp["passed"] is False
        assert resp["status"] == "REJECTED"
        assert len(resp["confirmed_defects"]) >= 1
        assert "Laya Noul P=0.88 >= 0.78" in resp["confirmed_defects"][0]["message"]
        assert resp["confirmed_defects"][0]["code_snippet"] != ""


@pytest.mark.asyncio
async def test_needle_verify_delivery_noul_arbitrage_dismisses_false_positive():
    """Teste 5: Camada 3 Noul descarta suspeita como falso-positivo (P < 0.78).

    Deve aprovar a entrega (status: APPROVED, passed: True) e listar o alerta em dismissed_warnings.
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        subprocess.run(["git", "init"], cwd=tmp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@jev.local"], cwd=tmp_dir, check=True)
        subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=tmp_dir, check=True)

        (Path(tmp_dir) / "README.md").write_text("# Project\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_dir, check=True)

        src_dir = Path(tmp_dir) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "untyped.py").write_text(
            "def calculate_total(subtotal, tax):\n"
            "    return subtotal + tax\n",
            encoding="utf-8",
        )

        tests_dir = Path(tmp_dir) / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_untyped.py").write_text(
            "from src.untyped import calculate_total\n\n"
            "def test_calculate_total():\n"
            "    assert calculate_total(100, 10) == 110\n",
            encoding="utf-8",
        )

        engine = DeterministicNoulEngine(probability=0.42, is_ready=True)

        resp = await needle_verify_delivery(
            workspace=tmp_dir,
            attempt_number=1,
            engine=engine,
        )

        assert resp["passed"] is True
        assert resp["status"] == "APPROVED"
        assert resp["confirmed_defects"] == []
        assert len(resp["dismissed_warnings"]) >= 1
        assert "Laya Noul P=0.42 < 0.78" in resp["dismissed_warnings"][0]["message"]
        assert "arbitragem JEV Noul" in resp["message"]


@pytest.mark.asyncio
async def test_needle_verify_delivery_engine_unloaded_telemetry_fallback():
    """Teste 6: Falback Fail-Safe quando o motor Laya não está pronto na memória.

    Deve aprovar a entrega mantendo os alertas apenas como telemetria (sem confirmed_defects).
    """
    with tempfile.TemporaryDirectory() as tmp_dir:
        subprocess.run(["git", "init"], cwd=tmp_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@jev.local"], cwd=tmp_dir, check=True)
        subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=tmp_dir, check=True)

        (Path(tmp_dir) / "README.md").write_text("# Project\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=tmp_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial commit"], cwd=tmp_dir, check=True)

        src_dir = Path(tmp_dir) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "untyped.py").write_text(
            "def calculate_total(subtotal, tax):\n"
            "    return subtotal + tax\n",
            encoding="utf-8",
        )

        tests_dir = Path(tmp_dir) / "tests"
        tests_dir.mkdir(parents=True, exist_ok=True)
        (tests_dir / "test_untyped.py").write_text(
            "from src.untyped import calculate_total\n\n"
            "def test_calculate_total():\n"
            "    assert calculate_total(100, 10) == 110\n",
            encoding="utf-8",
        )

        # Engine com is_ready = False
        engine = DeterministicNoulEngine(probability=0.99, is_ready=False)

        resp = await needle_verify_delivery(
            workspace=tmp_dir,
            attempt_number=1,
            engine=engine,
            pedido="Adicionar calculate_total com tipagem",
            resultado="calculate_total implementado em src/untyped.py com teste",
        )

        assert resp["passed"] is True
        assert resp["status"] == "APPROVED"
        assert resp["pedido_audit"] == "Adicionar calculate_total com tipagem"
        assert resp["resultado_audit"] == "calculate_total implementado em src/untyped.py com teste"
        assert resp["confirmed_defects"] == []
        assert resp["dismissed_warnings"] == []
        assert len(resp["structural_ast_warnings"]) >= 1
        assert "motor Laya descarregado" in resp["message"]


@pytest.mark.asyncio
async def test_needle_verify_delivery_multistack_chunk_id_and_audit():
    """Valida tipagem string de chunk_id e propagação de pedido_audit em multi-stack."""
    with tempfile.TemporaryDirectory() as tmp_dir:
        (Path(tmp_dir) / "package.json").write_text('{"name": "test-app"}', encoding="utf-8")
        (Path(tmp_dir) / "app.json").write_text('{"expo": {}}', encoding="utf-8")
        src_dir = Path(tmp_dir) / "src"
        src_dir.mkdir(parents=True, exist_ok=True)
        (src_dir / "index.js").write_text("console.log('app');\n", encoding="utf-8")

        resp = await needle_verify_delivery(
            workspace=tmp_dir,
            pedido="Refatorar componente mobile",
            resultado="index.js ajustado",
        )

        assert resp["status"] == "REJECTED"
        assert resp["pedido_audit"] == "Refatorar componente mobile"
        assert resp["resultado_audit"] == "index.js ajustado"
        assert len(resp["pytest_chunks"]) == 1
        assert isinstance(resp["pytest_chunks"][0]["chunk_id"], str)
        assert resp["pytest_chunks"][0]["chunk_id"].startswith("chunk_multistack_")


def test_needle_verify_delivery_request_rejects_attempt_above_five():
    """Caminho de erro real: attempt_number > 5 estoura no schema (circuit breaker)."""
    from pydantic import ValidationError

    from src.core.schemas import NeedleVerifyDeliveryRequest

    with pytest.raises(ValidationError):
        NeedleVerifyDeliveryRequest(attempt_number=6)
