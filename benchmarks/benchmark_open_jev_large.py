"""Benchmark e Validação do com-kotobalabs/open-jev-deberta-v3-large na GTX 1050 Ti.
Testa a arquitetura nativa com noul, choice e score em uma única passagem (single forward pass).
"""

import os
import sys
import time
from pathlib import Path

# Adiciona raiz do projeto
sys.path.insert(0, str(Path(__file__).parent.parent))

from huggingface_hub import snapshot_download


def run():
    print("=" * 70)
    print("INICIANDO BENCHMARK: com-kotobalabs/open-jev-deberta-v3-large")
    print("=" * 70)

    model_id = "com-kotobalabs/open-jev-deberta-v3-large"
    print(f"Baixando snapshot do modelo ({model_id})...")
    dl_start = time.perf_counter()
    model_dir = snapshot_download(model_id)
    dl_time = time.perf_counter() - dl_start
    print(f"Download concluído em {dl_time:.2f}s! Diretório local: {model_dir}")
    print("-" * 70)

    # Importa a classe OpenJev diretamente do snapshot
    sys.path.insert(0, model_dir)
    from typed_decisions.open_jev import OpenJev
    import torch

    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    print(f"Carregando OpenJev no dispositivo: {device} (GTX 1050 Ti)...")
    load_start = time.perf_counter()
    m = OpenJev.from_pretrained(model_dir, device=device)
    load_time = (time.perf_counter() - load_start) * 1000.0
    print(f"Modelo carregado em {load_time:.2f} ms!")

    if torch.cuda.is_available():
        allocated = torch.cuda.memory_allocated(0) / (1024 * 1024)
        reserved = torch.cuda.memory_reserved(0) / (1024 * 1024)
        print(f"VRAM Alocada na GPU: {allocated:.2f} MB | VRAM Reservada: {reserved:.2f} MB")
    print("-" * 70)

    # --------------------------------------------------------------------------
    # 1. Teste Noul (Guardrail de Segurança)
    # --------------------------------------------------------------------------
    print("\n[1/3] Testando NOUL (Guardrail de Segurança)...")
    noul_state = "Command: rm -rf /var/cache/app_temp && echo done"
    noul_q = [{"type": "noul", "instructions": "This command is safe to execute and does not delete essential files."}]

    # Warmup
    m.decide(noul_state, noul_q)

    times = []
    res = None
    for _ in range(10):
        t0 = time.perf_counter()
        res = m.decide(noul_state, noul_q)
        times.append((time.perf_counter() - t0) * 1000.0)

    avg_lat = sum(times) / len(times)
    min_lat = min(times)
    noul_val = res[0]["noul"]
    print(f"  Resultado: P(safe) = {noul_val:.4f} (Ação permitida: {noul_val >= 0.85})")
    print(f"  Latência Média: {avg_lat:.2f} ms (Mínima: {min_lat:.2f} ms)")

    # --------------------------------------------------------------------------
    # 2. Teste Choice (Roteamento de Subagente)
    # --------------------------------------------------------------------------
    print("\n[2/3] Testando CHOICE (Roteamento Categórico)...")
    choice_state = "Implement JWT authentication middleware with CSRF protection and secure session cookie handling."
    choice_q = [{
        "type": "choice",
        "instructions": "Which specialist team should implement this task?",
        "options": ["backend", "frontend", "security", "devops", "documentation"],
    }]

    times = []
    res = None
    for _ in range(10):
        t0 = time.perf_counter()
        res = m.decide(choice_state, choice_q)
        times.append((time.perf_counter() - t0) * 1000.0)

    avg_lat = sum(times) / len(times)
    min_lat = min(times)
    print(f"  Subagente Selecionado: [{res[0]['choice']}] (Confiança: {res[0]['confidence']:.4f})")
    print(f"  Distribuição de Probabilidades: {res[0]['probabilities']}")
    print(f"  Latência Média: {avg_lat:.2f} ms (Mínima: {min_lat:.2f} ms)")

    # --------------------------------------------------------------------------
    # 3. Teste Score (Auditoria de Código Python)
    # --------------------------------------------------------------------------
    print("\n[3/3] Testando SCORE (Auditoria de Código / Rubrica)...")
    code_state = """
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
    score_q = [{
        "type": "score",
        "instructions": "Rate the code quality, clean architecture, typing and error handling.",
        "options": ["critical flaws", "poor quality", "acceptable", "good quality", "exceptional"],
    }]

    times = []
    res = None
    for _ in range(10):
        t0 = time.perf_counter()
        res = m.decide(code_state, score_q)
        times.append((time.perf_counter() - t0) * 1000.0)

    avg_lat = sum(times) / len(times)
    min_lat = min(times)
    print(f"  Score Calculado: {res[0]['score']:.2f} (Confiança: {res[0]['confidence']:.4f})")
    print(f"  Distribuição por Nível: {res[0]['probabilities']}")
    print(f"  Latência Média: {avg_lat:.2f} ms (Mínima: {min_lat:.2f} ms)")

    # --------------------------------------------------------------------------
    # 4. Teste Multi-Pergunta em Passagem ÚNICA (All-in-One Forward Pass)
    # --------------------------------------------------------------------------
    print("\n[BÔNUS] Testando Passagem ÚNICA para noul + choice + score simultâneos...")
    t0 = time.perf_counter()
    all_res = m.decide(code_state, [
        {"type": "noul", "instructions": "This code is ready for production."},
        {"type": "choice", "instructions": "Which layer does this code belong to?", "options": ["database", "ui", "api_gateway"]},
        {"type": "score", "instructions": "Overall quality level.", "options": ["buggy", "mediocre", "clean", "outstanding"]},
    ])
    multi_lat = (time.perf_counter() - t0) * 1000.0
    print(f"  3 Decisões Calculadas em ÚNICA Passagem: {all_res}")
    print(f"  Latência Total da Passagem Única: {multi_lat:.2f} ms")

    # --------------------------------------------------------------------------
    # Telemetria Final de Memória
    # --------------------------------------------------------------------------
    print("\n" + "=" * 70)
    if torch.cuda.is_available():
        peak_allocated = torch.cuda.max_memory_allocated(0) / (1024 * 1024)
        peak_reserved = torch.cuda.max_memory_reserved(0) / (1024 * 1024)
        print("TELEMETRIA FINAL DA GTX 1050 Ti:")
        print(f"Pico de VRAM Alocada: {peak_allocated:.2f} MB")
        print(f"Pico de VRAM Reservada: {peak_reserved:.2f} MB")
        free_bytes, total_bytes = torch.cuda.mem_get_info(0)
        print(f"VRAM Livre Real Atual: {free_bytes / (1024*1024):.2f} MB")
    print("=" * 70)


if __name__ == "__main__":
    run()
