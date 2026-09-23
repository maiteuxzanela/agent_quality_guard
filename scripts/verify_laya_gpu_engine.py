"""
Script de Verificação Ponta a Ponta: OpenJevEngine com Backend Laya na GPU GTX 1050 Ti.
"""

import asyncio
import sys
import time
from pathlib import Path

# Adiciona raiz do projeto
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.decision.engine import OpenJevEngine
from src.core.schemas import ChoiceRequest, NoulRequest, RUBRIC_CATALOG, ScoreRequest


async def main():
    print("=" * 65)
    print("VERIFICAÇÃO: OpenJevEngine com Backend Laya na GPU GTX 1050 Ti")
    print("=" * 65)

    import torch
    if not torch.cuda.is_available():
        print("[ERRO FATAL] CUDA não disponível nesta execução.")
        sys.exit(1)

    free_start, total_start = torch.cuda.mem_get_info()
    print(f"\n[GPU Hardware] {torch.cuda.get_device_name(0)}")
    print(f"  VRAM Livre Inicial: {free_start / 1024**3:.2f} GB / {total_start / 1024**3:.2f} GB")

    # 1. Instanciação padrão da Engine (deve ser Laya por padrão)
    print("\n[1] Instanciando OpenJevEngine padrão...")
    engine = OpenJevEngine()
    print(f"  model_id_or_path : {engine.model_id_or_path}")
    print(f"  target_device    : {engine.target_device}")
    print(f"  is_laya          : {engine.is_laya}")
    assert engine.is_laya, "Engine padrão DEVE ser is_laya=True"

    # 2. Carga do Modelo na GPU
    print("\n[2] Executando engine.load_model()...")
    t0 = time.perf_counter()
    engine.load_model()
    load_time = time.perf_counter() - t0
    print(f"  [OK] Modelo carregado em {load_time:.2f}s | is_ready={engine.is_ready}")

    alloc = torch.cuda.memory_allocated(0) / 1024**3
    resv = torch.cuda.memory_reserved(0) / 1024**3
    print(f"  VRAM Alocada  : {alloc:.3f} GB")
    print(f"  VRAM Reservada: {resv:.3f} GB")

    # 3. Teste de Choice (Roteamento Categórico)
    print("\n[3] Testando engine.evaluate_choice()...")
    req_c = ChoiceRequest(
        premise="Precisamos corrigir um crash no módulo de autenticação da API REST que retorna 500 Internal Server Error ao criar novos usuários via POST /auth/register.",
        options=["backend", "frontend", "security", "devops", "documentation"],
    )
    t0 = time.perf_counter()
    res_c = await engine.evaluate_choice(req_c)
    lat_c = (time.perf_counter() - t0) * 1000
    print(f"  selected       : {res_c.selected!r}")
    print(f"  top_confidence : {res_c.top_confidence}")
    print(f"  probabilities  : {res_c.probabilities}")
    print(f"  latency_ms     : {lat_c:.1f}ms (engine: {res_c.latency_ms:.1f}ms)")
    assert res_c.selected == "backend", f"Esperado 'backend', recebido {res_c.selected!r}"

    # 4. Teste de Noul (Guardrail de Segurança)
    print("\n[4] Testando engine.evaluate_noul()...")
    req_n = NoulRequest(
        premise="O sistema detectou uma tentativa de injeção SQL na query: SELECT * FROM users WHERE id=1 OR 1=1; DROP TABLE users;",
        hypothesis="Esta proposição indica um risco de segurança que exige contenção",
        threshold=0.78,
    )
    t0 = time.perf_counter()
    res_n = await engine.evaluate_noul(req_n)
    lat_n = (time.perf_counter() - t0) * 1000
    print(f"  probability    : {res_n.probability}")
    print(f"  passed         : {res_n.passed}")
    print(f"  threshold      : {res_n.threshold}")
    print(f"  latency_ms     : {lat_n:.1f}ms (engine: {res_n.latency_ms:.1f}ms)")

    # 5. Teste de Score (Avaliação de Rubrica)
    print("\n[5] Testando engine.evaluate_score()...")
    req_s = ScoreRequest(
        premise=(
            "Pedido do Prompt: Implementar o Circuit Breaker e Trava Anti-Premise Gaming no MCP Server. "
            "Resultado Executado: Criada trava física via assinatura Git em src/mcp_server.py, desenvolvida suite "
            "tests/test_circuit_breaker.py com 5 testes unitários, integrado ao tests/verify_all.py, "
            "totalizando 23 testes automatizados com 100% de aprovação."
        ),
        rubric=RUBRIC_CATALOG["entrega"],
        threshold=3.40,
    )
    t0 = time.perf_counter()
    res_s = await engine.evaluate_score(req_s)
    lat_s = (time.perf_counter() - t0) * 1000
    print(f"  score          : {res_s.score} / 5.0")
    print(f"  passed         : {res_s.passed}")
    print(f"  breakdown      : {res_s.breakdown}")
    print(f"  latency_ms     : {lat_s:.1f}ms (engine: {res_s.latency_ms:.1f}ms)")

    peak = torch.cuda.max_memory_allocated(0) / 1024**3
    print(f"\n[VRAM Pico Total]: {peak:.3f} GB (limite seguro: 3.00 GB na GTX 1050 Ti)")
    print("\n[STATUS] TODAS AS 3 PRIMITIVAS EXECUTADAS COM SUCESSO NA GPU COM LAYA!")


if __name__ == "__main__":
    asyncio.run(main())
