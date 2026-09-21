"""Testes de integração da API FastAPI do Open Jev."""

import pytest
from starlette.testclient import TestClient

from src.decision.server import create_app


@pytest.fixture
def client():
    app = create_app(auto_load_model=False)
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
        "premise": "def add(a, b): return a + b",
        "rubric": "Code quality rubric",
    }
    response = client.post("/v1/decision/score", json=payload)
    assert response.status_code == 503
    data = response.json()
    assert data["error"] == "OpenJevNotReadyError"
