"""Benchmark de Performance do Open Jev Compacto na NVIDIA GeForce GTX 1050 Ti.
Mede latência em milissegundos, consumo de VRAM e calibração das primitivas noul, choice e score.
"""

import asyncio
import os
import sys
import time
from pathlib import Path

# Adiciona raiz do projeto
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.schemas import ChoiceRequest, NoulRequest, ScoreRequest
from src.decision.engine import OpenJevEngine
from src.decision.hardware import get_hardware_profile


async def run_benchmark(model_id: str = "cross-encoder/nli-deberta-v3-small"):
    print("=" * 70)
    print(f"INICIANDO BENCHMARK: {model_id}")
    print("=" * 70)

    profile = get_hardware_profile()
    print(f"Dispositivo detectado: {profile.device_name}")
    print(f"CUDA Ativo: {profile.has_cuda} | Compute Capability: {profile.compute_capability}")
    print(f"VRAM Total: {profile.total_vram_mb} MB | VRAM Livre: {profile.free_vram_mb} MB")
    print(f"Suporte a bfloat16: {profile.supports_bfloat16} (Trava ativa: {not profile.supports_bfloat16})")
    print(f"Precisão recomendada: {profile.recommended_dtype}")
    print("-" * 70)

    # Inicializa Engine
    print("Carregando pesos e tokenizador...")
    load_start = time.perf_counter()
    engine = OpenJevEngine(
        model_id_or_path=model_id,
        device="cuda:0" if profile.has_cuda else "cpu",
        max_length=1024,
    )
    engine.load_model()
    load_time = (time.perf_counter() - load_start) * 1000.0
    print(f"Tempo de carga do modelo: {load_time:.2f} ms")

    import torch
    if torch.cuda.is_available():
        allocated = torch.cuda.memory_allocated(0) / (1024 * 1024)
        reserved = torch.cuda.memory_reserved(0) / (1024 * 1024)
        print(f"VRAM Alocada pós-carga: {allocated:.2f} MB | VRAM Reservada: {reserved:.2f} MB")
    print("-" * 70)

    # --------------------------------------------------------------------------
    # 1. Benchmark: Primitiva Noul (Guardrail)
    # --------------------------------------------------------------------------
    print("\n[1/3] Testando Primitiva NOUL (Guardrail de Segurança)...")
    noul_req = NoulRequest(
        premise="Command: rm -rf /var/cache/app_temp && echo done",
        hypothesis="This command is safe to execute and does not destroy essential system files.",
        threshold=0.85,
    )

    # Warmup
    await engine.evaluate_noul(noul_req)

    noul_times = []
    noul_res = None
    for _ in range(10):
        t0 = time.perf_counter()
        noul_res = await engine.evaluate_noul(noul_req)
        noul_times.append((time.perf_counter() - t0) * 1000.0)

    avg_noul = sum(noul_times) / len(noul_times)
    min_noul = min(noul_times)
    print(f"  Resultado: Passed={noul_res.passed} | Probabilidade={noul_res.probability:.4f} | Confiança={noul_res.confidence:.4f}")
    print(f"  Latência Média: {avg_noul:.2f} ms (Mínima: {min_noul:.2f} ms)")

    # --------------------------------------------------------------------------
    # 2. Benchmark: Primitiva Choice (Roteamento Categórico de Especialistas)
    # --------------------------------------------------------------------------
    print("\n[2/3] Testando Primitiva CHOICE (Roteamento de Subagente)...")
    choice_req = ChoiceRequest(
        premise="Implement JWT authentication middleware with CSRF protection and secure session cookie handling.",
        options=["backend", "frontend", "security", "devops", "documentation"],
        temperature=1.0,
    )

    # Warmup
    await engine.evaluate_choice(choice_req)

    choice_times = []
    choice_res = None
    for _ in range(10):
        t0 = time.perf_counter()
        choice_res = await engine.evaluate_choice(choice_req)
        choice_times.append((time.perf_counter() - t0) * 1000.0)

    avg_choice = sum(choice_times) / len(choice_times)
    min_choice = min(choice_times)
    print(f"  Subagente Selecionado: [{choice_res.selected}] (Confiança: {choice_res.top_confidence:.4f})")
    print(f"  Distribuição de Probabilidades: {choice_res.probabilities}")
    print(f"  Latência Média: {avg_choice:.2f} ms (Mínima: {min_choice:.2f} ms)")

    # --------------------------------------------------------------------------
    # 3. Benchmark: Primitiva Score (Avaliação de Conformidade de Código)
    # --------------------------------------------------------------------------
    print("\n[3/3] Testando Primitiva SCORE (Auditoria de Código / Rubrica)...")
    code_sample = """
    async def get_user_profile(user_id: int) -> UserProfile:
        \"\"\"Recupera o perfil do usuário pelo identificador primário.\"\"\"
        if user_id <= 0:
            raise ValueError("user_id deve ser um inteiro positivo.")
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow("SELECT id, name, email FROM users WHERE id = $1", user_id)
            if not row:
                raise NotFoundError(f"Usuário {user_id} não encontrado.")
            return UserProfile(**dict(row))
    """
    score_req = ScoreRequest(
        premise=code_sample,
        rubric="Clean code, explicit error handling, parameterized SQL, robust typing, and docstrings.",
        min_score=1.0,
        max_score=5.0,
        threshold=4.0,
    )

    # Warmup
    await engine.evaluate_score(score_req)

    score_times = []
    score_res = None
    for _ in range(10):
        t0 = time.perf_counter()
        score_res = await engine.evaluate_score(score_req)
        score_times.append((time.perf_counter() - t0) * 1000.0)

    avg_score = sum(score_times) / len(score_times)
    min_score = min(score_times)
    print(f"  Nota Calculada: {score_res.score:.2f} / 5.00 (Passed: {score_res.passed})")
    print(f"  Detalhamento por Nível: {score_res.breakdown}")
    print(f"  Latência Média: {avg_score:.2f} ms (Mínima: {min_score:.2f} ms)")

    # --------------------------------------------------------------------------
    # Telemetria Final de Memória
    # --------------------------------------------------------------------------
    print("\n" + "=" * 70)
    if torch.cuda.is_available():
        peak_allocated = torch.cuda.max_memory_allocated(0) / (1024 * 1024)
        peak_reserved = torch.cuda.max_memory_reserved(0) / (1024 * 1024)
        print(f"TELEMETRIA FINAL DA GTX 1050 Ti:")
        print(f"Pico de VRAM Alocada: {peak_allocated:.2f} MB")
        print(f"Pico de VRAM Reservada: {peak_reserved:.2f} MB")
        print(f"Margem de VRAM Livre Restante: {profile.total_vram_mb - peak_reserved:.2f} MB")
    print("=" * 70)


if __name__ == "__main__":
    model = sys.argv[1] if len(sys.argv) > 1 else "cross-encoder/nli-deberta-v3-small"
    asyncio.run(run_benchmark(model))
