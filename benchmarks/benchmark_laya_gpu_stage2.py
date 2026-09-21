"""
Benchmark Estágio 2: Laya em GPU (GTX 1050 Ti - Pascal sm_61)
Teste isolado — sem carregar DeBERTa concorrentemente.
Usa attn_implementation="sdpa" (sem Flash Attention 2, incompatível com Pascal).
"""

import gc
import json
import os
import time

# Força garbage collection e libera cache CUDA antes de qualquer import pesado
os.environ["PYTORCH_CUDA_ALLOC_CONF"] = "expandable_segments:True"

import torch

def clear_gpu():
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.synchronize()

print("=" * 60)
print("STAGE 2: Laya GPU — GTX 1050 Ti (Pascal sm_61)")
print("=" * 60)

# ── Diagnóstico VRAM antes de carregar ──────────────────────────────────────
if torch.cuda.is_available():
    total = torch.cuda.get_device_properties(0).total_memory / 1024**3
    reserved = torch.cuda.memory_reserved(0) / 1024**3
    allocated = torch.cuda.memory_allocated(0) / 1024**3
    free_est = total - reserved
    print(f"\n[VRAM antes do load]")
    print(f"  Total    : {total:.2f} GB")
    print(f"  Reservado: {reserved:.2f} GB")
    print(f"  Alocado  : {allocated:.2f} GB")
    print(f"  Estimado livre: {free_est:.2f} GB")
else:
    print("[AVISO] CUDA não disponível — abortando Stage 2.")
    exit(1)

# ── Casos de teste (mesmos do Stage 1) ──────────────────────────────────────
SCORE_PREMISE = (
    "Pedido do Prompt: Implementar o Circuit Breaker e Trava Anti-Premise Gaming no MCP Server. "
    "Resultado Executado: Criada trava física via assinatura Git em src/mcp_server.py, desenvolvida suite "
    "tests/test_circuit_breaker.py com 5 testes unitários, integrado ao tests/verify_all.py, "
    "totalizando 23 testes automatizados com 100% de aprovação."
)
RUBRIC = "Avalie a resposta técnica de 1.0 a 5.0 considerando rigor analítico, clareza didática, fidelidade ao código real do repositório e atendimento aos comandos do usuário."

# ── Tenta carregar Laya na GPU ───────────────────────────────────────────────
print("\n[info] Tentando carregar Laya english na GPU com sdpa...")
clear_gpu()

import laya
from laya import Agent

agent = None
device_used = "cpu"
load_error = None

try:
    # Carrega apenas o checkpoint english (421M) — mais leve que o Router com preload=True
    agent = Agent("convaiinnovations/laya", device="cuda")
    device_used = "cuda"
    print("[ok] Laya carregado na GPU!")

    if torch.cuda.is_available():
        alloc = torch.cuda.memory_allocated(0) / 1024**3
        resv = torch.cuda.memory_reserved(0) / 1024**3
        print(f"  VRAM após load: alocado={alloc:.2f}GB, reservado={resv:.2f}GB")

except Exception as e:
    load_error = str(e)
    print(f"[aviso] Falha ao carregar na GPU: {e}")
    print("[info] Tentando laya-multilingual (322M, menor)...")
    clear_gpu()
    try:
        agent = Agent("convaiinnovations/laya", device="cuda", subfolder="multilingual")
        device_used = "cuda (multilingual)"
        print("[ok] laya-multilingual carregado na GPU!")
    except Exception as e2:
        print(f"[aviso] Falha também com multilingual: {e2}")
        print("[info] Fallback final para CPU pura (sem MCP concorrente — VRAM 0 alocada por este processo)")
        agent = Agent("convaiinnovations/laya", device="cpu")
        device_used = "cpu (isolated)"

print(f"\n[device] Dispositivo efetivo: {device_used}")

# ── Inferências ──────────────────────────────────────────────────────────────
results = {"device": device_used, "load_error": load_error}

questions_all = {
    "specialist": {
        "type": "choice",
        "instructions": "Which specialist agent should handle this task?",
        "criteria": {
            "backend": "API, server-side logic, database, REST, authentication bugs",
            "frontend": "UI, CSS, React, browser rendering issues",
            "security": "vulnerabilities, SQL injection, auth bypass, CVE",
            "devops": "CI/CD, Docker, Kubernetes, deployment pipeline",
            "documentation": "README, docstrings, API docs",
        },
    },
    "is_security_risk": {
        "type": "noul",
        "instructions": "This observation represents a critical security risk that must be blocked immediately",
    },
    "quality": {
        "type": "score",
        "instructions": RUBRIC,
        "criteria": ["critical flaws", "poor quality", "acceptable", "good quality", "exceptional"],
    },
}

# ── Choice ───────────────────────────────────────────────────────────────────
print("\n[choice] Rodando...")
clear_gpu()
t0 = time.perf_counter()
try:
    res_c = agent.predict(
        {"task": "Precisamos corrigir um crash no módulo de autenticação da API REST que retorna 500 Internal Server Error ao criar novos usuários via POST /auth/register."},
        {"specialist": questions_all["specialist"]},
    )
    lat_c = (time.perf_counter() - t0) * 1000
    selected = res_c["answers"]["specialist"]["choice"]
    conf = res_c["answers"]["specialist"]["confidence"]
    correct = selected == "backend"
    results["choice"] = {"selected": selected, "confidence": round(conf, 4), "latency_ms": round(lat_c, 1), "correct": correct}
    print(f"  selected={selected!r} | confidence={conf:.3f} | {lat_c:.0f}ms | correct={correct}")
except Exception as e:
    print(f"  ERRO: {e}")
    results["choice"] = {"error": str(e)}

# ── Noul ─────────────────────────────────────────────────────────────────────
print("\n[noul] Rodando...")
clear_gpu()
t0 = time.perf_counter()
try:
    res_n = agent.predict(
        {"observation": "O sistema detectou uma tentativa de injeção SQL na query: SELECT * FROM users WHERE id=1 OR 1=1; DROP TABLE users;"},
        {"is_security_risk": questions_all["is_security_risk"]},
    )
    lat_n = (time.perf_counter() - t0) * 1000
    prob_n = float(res_n["answers"]["is_security_risk"]["noul"])
    passed_n = prob_n >= 0.85
    results["noul"] = {"probability": round(prob_n, 4), "passed": passed_n, "latency_ms": round(lat_n, 1)}
    print(f"  prob={prob_n:.4f} | passed={passed_n} | {lat_n:.0f}ms")
except Exception as e:
    print(f"  ERRO: {e}")
    results["noul"] = {"error": str(e)}

# ── Score ─────────────────────────────────────────────────────────────────────
print("\n[score] Rodando...")
clear_gpu()
t0 = time.perf_counter()
try:
    res_s = agent.predict(
        {"delivery": SCORE_PREMISE},
        {"quality": questions_all["quality"]},
    )
    lat_s = (time.perf_counter() - t0) * 1000
    raw_ordinal = float(res_s["answers"]["quality"]["score"])  # 0–4
    score_5 = round(1.0 + raw_ordinal, 2)  # 1–5
    passed_s = score_5 >= 3.75
    breakdown = res_s["answers"]["quality"].get("probabilities", {})
    results["score"] = {
        "score_jev": score_5,
        "score_ordinal": round(raw_ordinal, 4),
        "passed": passed_s,
        "breakdown": {k: round(float(v), 4) for k, v in breakdown.items()},
        "latency_ms": round(lat_s, 1),
    }
    print(f"  score={score_5:.2f}/5.0 | passed={passed_s} | {lat_s:.0f}ms")
except Exception as e:
    print(f"  ERRO: {e}")
    results["score"] = {"error": str(e)}

# ── VRAM Final ────────────────────────────────────────────────────────────────
if torch.cuda.is_available() and device_used.startswith("cuda"):
    alloc = torch.cuda.memory_allocated(0) / 1024**3
    resv = torch.cuda.memory_reserved(0) / 1024**3
    peak = torch.cuda.max_memory_allocated(0) / 1024**3
    results["vram"] = {"allocated_gb": round(alloc, 3), "reserved_gb": round(resv, 3), "peak_gb": round(peak, 3)}
    print(f"\n[VRAM final] alocado={alloc:.3f}GB | reservado={resv:.3f}GB | pico={peak:.3f}GB")

# ── Salva resultado ───────────────────────────────────────────────────────────
out_path = "benchmarks/laya_gpu_stage2.json"
with open(out_path, "w") as f:
    json.dump(results, f, indent=2, default=str)

print(f"\n[ok] Resultados salvos em: {out_path}")
print("STAGE2_COMPLETE:OK")
