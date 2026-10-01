"""Testes de integração da API FastAPI do Open Jev."""

import pytest
from starlette.testclient import TestClient

from src.decision.engine import OpenJevEngine
from src.decision.server import create_app


@pytest.fixture
def client():
    # device explícito: sem auto-detecção IPC. Se o daemon openjev estiver
    # ativo no ambiente, OpenJevEngine() padrão ficaria is_ready=True via
    # socket e quebraria o cenário real "motor não carregado => 503".
    engine = OpenJevEngine(device="cpu")
    app = create_app(engine=engine, auto_load_model=False)
    return TestClient(app)


def test_health_check_endpoint(client):
    response = client.get("/v1/decision/health")
    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "not_ready"
    assert data["is_ready"] is False
    assert "hardware" in data
    assert "device_name" in data["hardware"]


def test_noul_endpoint_fails_fast_when_not_ready(client):
    payload = {
        "premise": "rm -rf /etc",
        "hypothesis": "Operation is safe",
        "threshold": 0.85,
    }
    response = client.post("/v1/decision/noul", json=payload)
    assert response.status_code == 503
    data = response.json()
    assert data["error"] == "OpenJevNotReadyError"


def test_choice_endpoint_fails_fast_when_not_ready(client):
    payload = {
        "premise": "Create landing page hero section",
        "options": ["backend", "frontend", "security"],
    }
    response = client.post("/v1/decision/choice", json=payload)
    assert response.status_code == 503
    data = response.json()
    assert data["error"] == "OpenJevNotReadyError"


def test_score_endpoint_fails_fast_when_not_ready(client):
    payload = {
        "premise": "Pedido: def add. | Resultado Executado: função implementada.",
        "rubric": "Code quality rubric",
    }
    response = client.post("/v1/decision/score", json=payload)
    assert response.status_code == 503
    data = response.json()
    assert data["error"] == "OpenJevNotReadyError"


def test_score_endpoint_rejects_bad_premise_before_engine(client):
    """Gate portado: premissa sem Pedido|Resultado → 422 antes do fail-fast 503."""
    payload = {"premise": "só prosa, sem padrão factual"}
    response = client.post("/v1/decision/score", json=payload)
    assert response.status_code == 422
    detail = response.json()["detail"]
    assert "PREMISSA RECUSADA" in detail or "Pedido" in detail


def test_score_endpoint_rejects_bad_task_type(client):
    payload = {
        "premise": "Pedido: x | Resultado: y",
        "rubric": "livre que seria ignorada",
    }
    response = client.post(
        "/v1/decision/score?task_type=inexistente", json=payload
    )
    assert response.status_code == 422
    assert "TASK_TYPE" in response.json()["detail"]


def test_needle_ui_endpoint(client):
    """Garante que as rotas da interface HTML do Needle 3 respondem 200 OK com HTML."""
    res_v1 = client.get("/v1/needle/ui")
    assert res_v1.status_code == 200
    assert "text/html" in res_v1.headers["content-type"]
    assert "Cactus Needle 3" in res_v1.text

    res_shortcut = client.get("/ui")
    assert res_shortcut.status_code == 200
    assert "text/html" in res_shortcut.headers["content-type"]
    assert "Cactus Needle 3" in res_shortcut.text


def test_needle_search_endpoint_success(client):
    """Testa a busca de AST do Needle 3 via endpoint HTTP FastAPI."""
    payload = {
        "query": "OpenJevEngine",
        "workspace_path": ".",
        "max_tokens": 500,
        "relational_depth": 2,
    }
    response = client.post("/v1/needle/search", json=payload)
    assert response.status_code == 200
    data = response.json()
    assert "snippets" in data
    assert "total_tokens" in data
    assert "query_latency_ms" in data
    assert "token_savings_pct" in data
    assert isinstance(data["snippets"], list)
    assert len(data["snippets"]) > 0


def test_needle_search_endpoint_invalid_workspace(client):
    """Garante 400 Bad Request se o repositório/workspace não existir."""
    payload = {
        "query": "test",
        "workspace_path": "/caminho/invalido/inexistente_12345",
    }
    response = client.post("/v1/needle/search", json=payload)
    assert response.status_code == 400
    assert "Caminho de repositório inválido ou inexistente" in response.json()["detail"]


@pytest.mark.asyncio
async def test_engine_not_ready_raises_for_server_503_mapping():
    """Caminho de erro real: a engine lança OpenJevNotReadyError e create_app mapeia para 503."""
    from src.core.schemas import NoulRequest, OpenJevNotReadyError

    engine = OpenJevEngine(device="cpu")
    req = NoulRequest(premise="rm -rf /", hypothesis="Ação segura", threshold=0.78)
    with pytest.raises(OpenJevNotReadyError):
        await engine.evaluate_noul(req)
