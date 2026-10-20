"""Testes do cliente Cactus Needle 3 e análise relacional de AST."""

import pytest

from agent_quality_guard.core.schemas import Needle3NotAvailableError, Needle3Query
from agent_quality_guard.memory.needle_client import Needle3Client


@pytest.mark.asyncio
async def test_needle3_client_fails_fast_on_unreachable_host():
    # Endereço local inexistente para verificar falha de conexão sem mock
    client = Needle3Client(mode="cloud", api_key="dummy_key", api_endpoint="http://127.0.0.1:59999", timeout_seconds=1.0)
    query = Needle3Query(query="Find authentication middleware")

    with pytest.raises(Needle3NotAvailableError) as exc_info:
        await client.search(query)

    assert "Não foi possível conectar" in str(exc_info.value) or "Timeout" in str(exc_info.value)


def test_scan_local_ast_relations():
    client = Needle3Client()
    # Analisa o próprio schema do projeto
    relations = client.scan_local_ast_relations("agent_quality_guard/core/schemas.py")
    assert len(relations) > 0
    names = [r["name"] for r in relations]
    assert "NoulRequest" in names
    assert "ChoiceResponse" in names
