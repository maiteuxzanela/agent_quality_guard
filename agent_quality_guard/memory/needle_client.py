"""Cliente assíncrono para a Camada de Memória & Busca (Cactus Needle 3).
Responsável por extrair recortes cirúrgicos de código, documentação e logs
preservando integridade relacional sem estourar a janela de contexto.

POLÍTICA ESTRITA: Sem simulação ou dados fictícios. Se o serviço Cactus Compute
não estiver acessível, lança Needle3NotAvailableError.
"""

import ast
import logging
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import httpx
    HAS_HTTPX = True
except ImportError:
    HAS_HTTPX = False
    import requests

from ..core.schemas import (
    Needle3Error,
    Needle3NotAvailableError,
    Needle3Query,
    Needle3Response,
    Needle3Snippet,
)

from .local_needle import LocalNeedleEngine

logger = logging.getLogger("jev.needle")


class Needle3Client:
    """Cliente unificado para o Needle 3 com suporte a modo Local On-Device ou API Cloud."""

    def __init__(
        self,
        mode: str = "local",
        api_endpoint: str = "https://api.cactuscompute.com/v1",
        api_key: Optional[str] = None,
        timeout_seconds: float = 10.0,
        workspace_root: Optional[str] = None,
    ):
        self.mode = mode
        self.api_endpoint = api_endpoint.rstrip("/")
        self.api_key = api_key or os.getenv("CACTUS_COMPUTE_API_KEY")
        self.timeout_seconds = timeout_seconds
        self.local_engine = LocalNeedleEngine(workspace_root=workspace_root)

    async def search(self, query: Needle3Query) -> Needle3Response:
        """Executa a busca cirúrgica relacional. No modo 'local', roda 100% on-device."""
        if self.mode == "local" or not self.api_key:
            return await self.local_engine.search(query)

        import asyncio
        start_time = time.perf_counter()

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "query": query.query,
            "scope": query.scope,
            "max_tokens": query.max_tokens,
            "relational_depth": query.relational_depth,
            "preserve_relational_integrity": query.preserve_relational_integrity,
        }

        url = f"{self.api_endpoint}/needle3/search"

        try:
            if HAS_HTTPX:
                async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                    resp = await client.post(url, json=payload, headers=headers)
                    status_code = resp.status_code
                    resp_text = resp.text
                raw_data = resp.json() if status_code == 200 else None
                data: Dict[str, Any] = raw_data if isinstance(raw_data, dict) else {}
            else:
                def _do_post():
                    return requests.post(url, json=payload, headers=headers, timeout=self.timeout_seconds)
                resp = await asyncio.to_thread(_do_post)
                status_code = resp.status_code
                resp_text = resp.text
                raw_data = resp.json() if status_code == 200 else None
                data = raw_data if isinstance(raw_data, dict) else {}

            if status_code in (401, 403):
                raise Needle3NotAvailableError(
                    "Autenticação recusada pela API Cactus Compute. "
                    "Verifique a variável de ambiente CACTUS_COMPUTE_API_KEY."
                )
            if status_code >= 500:
                raise Needle3NotAvailableError(
                    f"Serviço Cactus Compute indisponível (HTTP {status_code}): {resp_text}"
                )
            if status_code != 200:
                raise Needle3Error(
                    f"Erro retornado pelo Cactus Needle 3 (HTTP {status_code}): {resp_text}"
                )

            snippets = [
                Needle3Snippet(
                    file_path=item["file_path"],
                    start_line=item["start_line"],
                    end_line=item["end_line"],
                    content=item["content"],
                    relevance_score=item["relevance_score"],
                    relations=item.get("relations", []),
                )
                for item in data.get("snippets", [])
            ]

            latency_ms = (time.perf_counter() - start_time) * 1000.0

            return Needle3Response(
                snippets=snippets,
                total_tokens=data.get("total_tokens", 0),
                query_latency_ms=round(latency_ms, 2),
            )

        except Exception as conn_err:
            if isinstance(conn_err, Needle3Error):
                raise
            err_name = conn_err.__class__.__name__
            if "Connect" in err_name or "Connection" in err_name or "Timeout" in err_name:
                raise Needle3NotAvailableError(
                    f"Não foi possível conectar ao endpoint Cactus Needle 3 ({self.api_endpoint}): {conn_err}."
                ) from conn_err
            raise Needle3Error(f"Falha na consulta ao Needle 3: {conn_err}") from conn_err

    def scan_local_ast_relations(self, file_path: str) -> List[Dict[str, Any]]:
        """Utilitário local complementar: analisa a árvore sintática (AST) de arquivos Python
        para extrair declarações, hierarquias de classes e chamadas de funções,
        alimentando o grafo relacional da memória.
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"Arquivo não encontrado para análise AST: {file_path}")

        source_code = path.read_text(encoding="utf-8")
        tree = ast.parse(source_code, filename=file_path)

        relations = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
                relations.append({
                    "type": "class_definition",
                    "name": node.name,
                    "line": node.lineno,
                    "bases": bases,
                })
            elif isinstance(node, ast.FunctionDef) or isinstance(node, ast.AsyncFunctionDef):
                relations.append({
                    "type": "function_definition",
                    "name": node.name,
                    "line": node.lineno,
                    "args": [a.arg for a in node.args.args],
                })

        return relations
