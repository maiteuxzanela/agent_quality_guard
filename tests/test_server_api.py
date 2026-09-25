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
