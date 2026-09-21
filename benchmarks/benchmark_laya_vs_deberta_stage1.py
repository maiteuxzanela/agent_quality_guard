"""
Benchmark Estágio 1: Laya (CPU) vs DeBERTa-v3 (CPU)
Compara latência, scores e distribuições de probabilidade para as 3 primitivas.
Executa exclusivamente em CPU para validação segura antes do teste GPU.
"""

import asyncio
import json
import time
import sys
from pathlib import Path

# Adiciona raiz do projeto ao path
sys.path.insert(0, str(Path(__file__).parent.parent))

# ─── Premissas e casos de teste ─────────────────────────────────────────────

BENCHMARK_CASES = {
    "choice": {
        "premise": "Precisamos corrigir um crash no módulo de autenticação da API REST que retorna 500 Internal Server Error ao criar novos usuários via POST /auth/register.",
        "options": ["backend", "frontend", "security", "devops", "documentation"],
        "expected": "backend",
    },
    "noul": {
        "premise": "O sistema detectou uma tentativa de injeção SQL na query: SELECT * FROM users WHERE id=1 OR 1=1; DROP TABLE users;",
        "hypothesis": "Esta operação representa um risco crítico de segurança que deve ser bloqueada imediatamente",
        "threshold": 0.85,
    },
    "score": {
        "premise": (
            "Pedido do Prompt: Implementar o Circuit Breaker e Trava Anti-Premise Gaming no MCP Server. "
            "Resultado Executado: Criada trava física via assinatura Git em src/mcp_server.py, desenvolvida suite "
            "tests/test_circuit_breaker.py com 5 testes unitários, integrado ao tests/verify_all.py, "
            "totalizando 23 testes automatizados com 100% de aprovação."
        ),
        "rubric": "Avalie a resposta técnica de 1.0 a 5.0 considerando rigor analítico, clareza didática, fidelidade ao código real do repositório e atendimento aos comandos do usuário.",
        "threshold": 3.75,
    },
}

STANDARD_RUBRIC = "Avalie a resposta técnica de 1.0 a 5.0 considerando rigor analítico, clareza didática, fidelidade ao código real do repositório e atendimento aos comandos do usuário."


# ─── Benchmark DeBERTa-v3 (engine atual) ────────────────────────────────────

async def benchmark_deberta():
    from src.decision.engine import OpenJevEngine
    from src.core.schemas import ChoiceRequest, NoulRequest, ScoreRequest

    print("\n" + "="*60)
    print("DeBERTa-v3 (CPU Fallback)")
    print("="*60)

    engine = OpenJevEngine(device="cpu", allow_cpu_fallback=True)
    engine.load_model()

    results = {}

    # Choice
    t0 = time.perf_counter()
    c = BENCHMARK_CASES["choice"]
    resp_c = await engine.evaluate_choice(ChoiceRequest(premise=c["premise"], options=c["options"]))
    lat_c = (time.perf_counter() - t0) * 1000
    results["choice"] = {
        "selected": resp_c.selected,
        "top_confidence": resp_c.top_confidence,
        "latency_ms": round(lat_c, 1),
        "correct": resp_c.selected == c["expected"],
    }
    print(f"[choice] selected={resp_c.selected!r} | confidence={resp_c.top_confidence:.3f} | {lat_c:.0f}ms | correct={resp_c.selected == c['expected']}")

    # Noul
    t0 = time.perf_counter()
    n = BENCHMARK_CASES["noul"]
    resp_n = await engine.evaluate_noul(NoulRequest(premise=n["premise"], hypothesis=n["hypothesis"], threshold=n["threshold"]))
    lat_n = (time.perf_counter() - t0) * 1000
    results["noul"] = {
        "passed": resp_n.passed,
        "probability": resp_n.probability,
        "latency_ms": round(lat_n, 1),
    }
    print(f"[noul]   passed={resp_n.passed} | prob={resp_n.probability:.4f} | {lat_n:.0f}ms")

    # Score
    t0 = time.perf_counter()
    s = BENCHMARK_CASES["score"]
    resp_s = await engine.evaluate_score(ScoreRequest(premise=s["premise"], rubric=s["rubric"], threshold=s["threshold"]))
    lat_s = (time.perf_counter() - t0) * 1000
    results["score"] = {
        "score": resp_s.score,
        "passed": resp_s.passed,
        "breakdown": resp_s.breakdown,
        "latency_ms": round(lat_s, 1),
    }
    print(f"[score]  score={resp_s.score:.2f} | passed={resp_s.passed} | {lat_s:.0f}ms")

    avg_lat = (lat_c + lat_n + lat_s) / 3
    print(f"\n[avg latency] {avg_lat:.0f}ms")
    return results


# ─── Benchmark Laya (CPU) ────────────────────────────────────────────────────

def benchmark_laya():
    import laya
    from laya import Router

    print("\n" + "="*60)
    print("Laya v0.3.5 (CPU — Router automático)")
    print("="*60)

    print("[info] Carregando checkpoints Laya (pode demorar na primeira vez)...")
    router = Router(preload=True)
    print("[info] Checkpoints carregados.")

    results = {}

    c = BENCHMARK_CASES["choice"]
    n = BENCHMARK_CASES["noul"]
    s = BENCHMARK_CASES["score"]

    # ── Choice ──────────────────────────────────────────────────────────────
    questions_choice = {
        "specialist": {
            "type": "choice",
            "instructions": "Which specialist agent should handle this task?",
            "criteria": {opt: f"specialist in {opt}" for opt in c["options"]},
        }
    }
    t0 = time.perf_counter()
    res_c = router.predict({"task": c["premise"]}, questions_choice)
    lat_c = (time.perf_counter() - t0) * 1000
    selected = res_c["answers"]["specialist"]["choice"]
    conf = res_c["answers"]["specialist"]["confidence"]
    results["choice"] = {
        "selected": selected,
        "top_confidence": round(conf, 4),
        "latency_ms": round(lat_c, 1),
        "correct": selected == c["expected"],
    }
    print(f"[choice] selected={selected!r} | confidence={conf:.3f} | {lat_c:.0f}ms | correct={selected == c['expected']}")

    # ── Noul ────────────────────────────────────────────────────────────────
    questions_noul = {
        "is_security_risk": {
            "type": "noul",
            "instructions": n["hypothesis"],
        }
    }
    t0 = time.perf_counter()
    res_n = router.predict({"observation": n["premise"]}, questions_noul)
    lat_n = (time.perf_counter() - t0) * 1000
    prob_n = float(res_n["answers"]["is_security_risk"]["noul"])
    passed_n = prob_n >= n["threshold"]
    results["noul"] = {
        "passed": passed_n,
        "probability": round(prob_n, 4),
        "latency_ms": round(lat_n, 1),
    }
    print(f"[noul]   passed={passed_n} | prob={prob_n:.4f} | {lat_n:.0f}ms")

    # ── Score ────────────────────────────────────────────────────────────────
    questions_score = {
        "quality": {
            "type": "score",
            "instructions": s["rubric"],
            "criteria": ["critical flaws", "poor quality", "acceptable", "good quality", "exceptional"],
        }
    }
    t0 = time.perf_counter()
    res_s = router.predict({"delivery": s["premise"]}, questions_score)
    lat_s = (time.perf_counter() - t0) * 1000
    # Laya score retorna valor ordinal 0–4 (índice dos critérios).
    # Remapear para escala JEV 1–5: final = 1.0 + (ordinal / 4.0) * 4.0 = 1.0 + ordinal
    raw_ordinal = float(res_s["answers"]["quality"]["score"])  # 0.0 a 4.0
    final_score_5 = round(1.0 + raw_ordinal, 2)               # 1.0 a 5.0
    passed_s = final_score_5 >= s["threshold"]
    breakdown = res_s["answers"]["quality"].get("probabilities", {})
    results["score"] = {
        "score": final_score_5,
        "score_ordinal_raw": round(raw_ordinal, 4),
        "passed": passed_s,
        "breakdown": {k: round(float(v), 4) for k, v in breakdown.items()},
        "latency_ms": round(lat_s, 1),
    }
    print(f"[score]  score={final_score_5:.2f} | passed={passed_s} | {lat_s:.0f}ms")

    avg_lat = (lat_c + lat_n + lat_s) / 3
    print(f"\n[avg latency] {avg_lat:.0f}ms")
    return results


# ─── Comparativo Final ───────────────────────────────────────────────────────

async def main():
    print("\n" + "#"*60)
    print("# BENCHMARK: Laya vs DeBERTa-v3")
    print("# Plataforma: CPU (Estágio 1)")
    print("#"*60)

    deberta_results = await benchmark_deberta()
    laya_results = benchmark_laya()

    print("\n" + "="*60)
    print("COMPARATIVO FINAL")
    print("="*60)

    primitives = ["choice", "noul", "score"]
    for p in primitives:
        d = deberta_results[p]
        l = laya_results[p]
        speedup = d["latency_ms"] / l["latency_ms"] if l["latency_ms"] > 0 else 0
        print(f"\n[{p.upper()}]")
        print(f"  DeBERTa-v3 : {d['latency_ms']:.0f}ms")
        print(f"  Laya       : {l['latency_ms']:.0f}ms  (speedup: {speedup:.1f}x)")
        if p == "choice":
            print(f"  DeBERTa selected={d['selected']!r} correct={d['correct']} | Laya selected={l['selected']!r} correct={l['correct']}")
        elif p == "noul":
            print(f"  DeBERTa prob={d['probability']:.4f} passed={d['passed']} | Laya prob={l['probability']:.4f} passed={l['passed']}")
        elif p == "score":
            print(f"  DeBERTa score={d['score']:.2f} passed={d['passed']} | Laya score={l['score']:.2f} passed={l['passed']}")

    # Resumo final legível por máquina
    summary = {"deberta": deberta_results, "laya": laya_results}
    out_path = "benchmarks/laya_vs_deberta_cpu_stage1.json"
    with open(out_path, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n[ok] Resultados salvos em: {out_path}")
    print("BENCHMARK_COMPLETE:OK")


if __name__ == "__main__":
    asyncio.run(main())
