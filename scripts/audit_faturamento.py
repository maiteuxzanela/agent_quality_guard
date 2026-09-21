"""Auditoria Arquitetural e de Governança de IA do Projeto Faturamento.
Utiliza o Pipeline Tri-Camada (Needle 3 Local + Open Jev na GTX 1050 Ti + LLM)
para avaliar código, estrutura, governança de IA e segurança da informação.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Adiciona raiz do projeto JEV
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.core.schemas import ChoiceRequest, Needle3Query, NoulRequest, ScoreRequest
from src.decision.engine import OpenJevEngine
from src.memory.local_needle import LocalNeedleEngine

FATURAMENTO_DIR = "/mnt/94CCB337CCB3130A/Faturamento"


async def run_faturamento_audit():
    print("=" * 80)
    print("INICIANDO AUDITORIA ARQUITETURAL E GOVERNANÇA DE IA: FATURAMENTO")
    print(f"Diretório Alvo: {FATURAMENTO_DIR}")
    print("=" * 80)

    start_total = time.perf_counter()

    # 1. Inicializa motores locais
    print("\n[1/4] Inicializando Motores Locais...")
    needle = LocalNeedleEngine(workspace_root=FATURAMENTO_DIR)
    engine = OpenJevEngine(
        model_id_or_path="com-kotobalabs/open-jev-deberta-v3-large",
        device="cuda:0",
    )
    engine.load_model()
    print("  Motores prontos (Open Jev na GTX 1050 Ti).")
    print("-" * 80)

    # 2. Definição das Dimensões de Avaliação
    dimensions = [
        {
            "id": "ai_governance_and_sync",
            "name": "Governança de IA, Personas e Sincronização Dupla (AGENTS.md / personas / kanban)",
            "query": "AGENTS Cecília Beatriz Camila Daniela Juliana Leticia kanban dashboard_kanban sincronização mandatoria",
            "scope": ["AGENTS.md", "personas", "kanban.md"],
            "risk_hypothesis": "The AI instructions rely on manual dual-synchronization between a 46KB markdown kanban and a 118KB HTML dashboard, creating maintenance friction, prompt bloat and state drift.",
            "rubric": "Decoupled single source of truth, automated dashboard generation, relative file paths, clear role boundaries without manual file duplication.",
            "options": ["ai_prompt_architect", "operations_lead", "documentation_engineer", "fullstack_architect"],
        },
        {
            "id": "security_and_credentials",
            "name": "Segurança, Chaves de API e Gestão de Segredos (functions/index.js)",
            "query": "SENHA_MESTRA_CONSULTORIO SENHA_TESTES_TEMPORARIA GEMINI_API_KEY apiKey 123456 firestore.rules",
            "scope": ["bot_faturamento/functions/index.js", "bot_faturamento/firestore.rules"],
            "risk_hypothesis": "The backend source code contains hardcoded production and test passwords ('123456', '26061217'), token salt, and an exposed Google Gemini API key.",
            "rubric": "Zero hardcoded secrets, mandatory Secret Manager / environment variables, cryptographically secure dynamic tokens, restricted firestore rules.",
            "options": ["security_auditor", "backend_engineer", "devops_engineer", "compliance_officer"],
        },
        {
            "id": "codebase_duplication_and_monoliths",
            "name": "Arquitetura Frontend, Duplicação de Pastas e Monólito (auditoria-guias vs public)",
            "query": "auditoria-guias app.js bot_faturamento public index.html style.css",
            "scope": ["auditoria-guias", "bot_faturamento/public"],
            "risk_hypothesis": "The project maintains identical unbundled duplicate folders between auditoria-guias and bot_faturamento/public, and an 80KB monolithic app.js script exceeding 2,000 lines.",
            "rubric": "Single canonical source directory, automated deployment pipeline, modular ES components, clean separation of concerns.",
            "options": ["frontend_architect", "devops_engineer", "qa_lead", "system_architect"],
        },
        {
            "id": "core_engines_and_compliance",
            "name": "Motores Especializados e Compliance TISS/LGPD (extrator_guias.py / validador_tiss.py)",
            "query": "extrator_guias.py validador_tiss.py pdftotext tesseract validarConformidadeRegrasTUSS LGPD",
            "scope": ["extrator_local/extrator_guias.py", "validador_tiss/validador_tiss.py", "bot_faturamento/functions/index.js"],
            "risk_hypothesis": "The local extraction and validation engines suffer from fragile heuristics or dependency bottlenecks that could cause parsing failures.",
            "rubric": "Robust offline parsing, strict XSD schema validation, deterministic rule linter, privacy-by-design LGPD masking.",
            "options": ["backend_engineer", "compliance_officer", "data_engineer", "tiss_specialist"],
        },
    ]

    results = []

    # 3. Execução das Dimensões com Needle 3 e Open Jev
    print("\n[2/4] Executando Varredura e Decisões...")
    for idx, dim in enumerate(dimensions, 1):
        print(f"\n[{idx}/4] Avaliando: {dim['name']}")

        # Needle 3
        t_needle = time.perf_counter()
        needle_resp = await needle.search(Needle3Query(query=dim["query"], scope=dim["scope"], max_tokens=1200))
        lat_needle = (time.perf_counter() - t_needle) * 1000.0
        print(f"  [Needle 3] Recuperados {len(needle_resp.snippets)} trechos ({needle_resp.total_tokens} tokens) em {lat_needle:.2f} ms")

        snippets_txt = "\n---\n".join([f"File: {s.file_path} (L{s.start_line}-L{s.end_line}):\n{s.content[:600]}" for s in needle_resp.snippets[:3]])

        # Choice
        choice_resp = await engine.evaluate_choice(ChoiceRequest(
            premise=f"Project: Faturamento BPO\nDimension: {dim['name']}\nContext:\n{snippets_txt[:800]}",
            options=dim["options"],
        ))
        print(f"  [Choice] Especialista Indicado: [{choice_resp.selected}] ({choice_resp.top_confidence * 100:.1f}%)")

        # Noul (Risco)
        noul_resp = await engine.evaluate_noul(NoulRequest(
            premise=f"Code Context:\n{snippets_txt[:1000]}",
            hypothesis=dim["risk_hypothesis"],
            threshold=0.50,
        ))
        risk_tag = "RISCO CONFIRMADO" if noul_resp.passed else "BAIXO RISCO"
        print(f"  [Noul] Diagnóstico: {risk_tag} (P={noul_resp.probability:.4f})")

        # Score (Conformidade)
        score_resp = await engine.evaluate_score(ScoreRequest(
            premise=f"Dimension: {dim['name']}\nContext:\n{snippets_txt[:1000]}",
            rubric=dim["rubric"],
        ))
        print(f"  [Score] Nota de Conformidade: {score_resp.score:.2f} / 5.00")
        print(f"          Distribuição: {score_resp.breakdown}")

        results.append({
            "dimension": dim["id"],
            "name": dim["name"],
            "needle_tokens": needle_resp.total_tokens,
            "needle_latency_ms": lat_needle,
            "choice_selected": choice_resp.selected,
            "choice_confidence": round(choice_resp.top_confidence, 4),
            "choice_latency_ms": round(choice_resp.latency_ms, 2),
            "noul_passed": noul_resp.passed,
            "noul_probability": round(noul_resp.probability, 4),
            "noul_latency_ms": round(noul_resp.latency_ms, 2),
            "score": round(score_resp.score, 2),
            "score_breakdown": {k: round(v, 4) for k, v in score_resp.breakdown.items()},
            "score_latency_ms": round(score_resp.latency_ms, 2),
        })

    total_time = (time.perf_counter() - start_total) * 1000.0

    output_data = {
        "project": FATURAMENTO_DIR,
        "total_time_ms": total_time,
        "dimensions": results,
    }

    out_file = Path("/home/maiteuxzanela/projeto JEV/faturamento_audit_results.json")
    with open(out_file, "w", encoding="utf-8") as f:
        json.dump(output_data, f, indent=2)

    print("\n" + "=" * 80)
    print(f"AUDITORIA CONCLUÍDA EM {total_time:.2f} ms ({total_time/1000:.2f} s)")
    print(f"Resultados gravados em: {out_file}")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(run_faturamento_audit())
