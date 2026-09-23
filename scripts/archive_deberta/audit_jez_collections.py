# HISTORICAL — era DeBERTa (modelo deberta, noul 0.50/3.5, score 3.75).
# NAO USAR em pipeline Laya. Ver scripts/audit_mocks.py (0.78/3.40).
# Arquivado na auditoria P2 (2026).
"""Auditoria Arquitetural e de Governança de IA do Projeto JEZ Collections.
Utiliza o Pipeline Tri-Camada (Needle 3 Local + Open Jev na GTX 1050 Ti + LLM)
para avaliar código, estrutura e qualidade das instruções que direcionam as IAs.
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

JEZ_DIR = "/mnt/94CCB337CCB3130A/JEZ collections"


async def run_jez_audit():
    print("=" * 80)
    print("INICIANDO AUDITORIA ARQUITETURAL E GOVERNANÇA DE IA: JEZ COLLECTIONS")
    print(f"Diretório Alvo: {JEZ_DIR}")
    print("=" * 80)

    start_total = time.perf_counter()

    # 1. Inicializa motores locais
    print("\n[1/4] Inicializando Motores Locais...")
    needle = LocalNeedleEngine(workspace_root=JEZ_DIR)
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
            "id": "ai_prompts_and_governance",
            "name": "Governança de IA, Personas e Instruções (AGENTS.md / personas/)",
            "query": "AGENTS Alex Ariel Lumi Morgan Sam Noa Robin persona prompt role",
            "scope": ["AGENTS.md", "personas"],
            "risk_hypothesis": "The AI instructions suffer from duplicate prompt bloat, broken absolute path links, and lack deterministic operational guardrails.",
            "rubric": "Concise modular prompts, relative documentation links, executable decision rules, clear delegation boundaries.",
            "options": ["ai_prompt_architect", "fullstack_cto", "tech_lead", "documentation_engineer"],
        },
        {
            "id": "security_and_firestore",
            "name": "Segurança de Dados, LGPD e Regras do Firestore (firestore.rules)",
            "query": "firestore.rules allow read write if true orders products apiKey",
            "scope": ["firestore.rules", "site/firebase-config.js", "site/firebase-service.js"],
            "risk_hypothesis": "The database rules grant unauthenticated write and delete access to public clients, causing severe security vulnerability and data breach risk.",
            "rubric": "Role-based access control (RBAC), strict Firestore write validation, no public deletion of orders, full data privacy.",
            "options": ["security_auditor", "backend_engineer", "cloud_architect", "compliance_officer"],
        },
        {
            "id": "frontend_monoliths",
            "name": "Arquitetura Frontend e Monólitos Vanilla JS (admin.js 93KB / app.js 62KB)",
            "query": "admin.js app.js addEventListener querySelector innerHTML state monolithic",
            "scope": ["site/admin.js", "site/app.js", "site/index.html"],
            "risk_hypothesis": "The frontend consists of massive unbundled monolithic scripts exceeding 1,500 lines, causing maintainability collapse and race conditions.",
            "rubric": "Modular component architecture (ES modules or modern framework), separated state management, small testable units (<250 lines).",
            "options": ["frontend_architect", "ui_refactor_lead", "performance_engineer", "qa_lead"],
        },
        {
            "id": "css_design_system",
            "name": "Design System, Identidade Visual e CSS (styles.css 51KB / admin.css 42KB)",
            "query": "styles.css admin.css :root color-primary crochê pesponto typography",
            "scope": ["site/styles.css", "site/admin.css"],
            "risk_hypothesis": "The stylesheets are bloated monolithic CSS files with duplicated declarations, lack of component scoping, and hardcoded styling overrides.",
            "rubric": "Organized design token architecture, utility/component CSS modules, consistent responsive design without arbitrary overrides.",
            "options": ["ui_ux_designer", "design_system_lead", "frontend_architect", "brand_director"],
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
            premise=f"Project: JEZ Collections\nDimension: {dim['name']}\nContext:\n{snippets_txt[:800]}",
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
        print(f"  [Noul] Diagnóstico de Risco: {risk_tag} (P={noul_resp.probability:.4f})")

        # Score (Conformidade)
        score_resp = await engine.evaluate_score(ScoreRequest(
            premise=snippets_txt[:1200],
            rubric=dim["rubric"],
            min_score=1.0,
            max_score=5.0,
            threshold=3.5,
        ))
        print(f"  [Score] Nota de Conformidade: {score_resp.score:.2f} / 5.00")
        print(f"          Distribuição: {score_resp.breakdown}")

        results.append({
            "dimension": dim["id"],
            "name": dim["name"],
            "needle_tokens": needle_resp.total_tokens,
            "needle_latency_ms": lat_needle,
            "choice_selected": choice_resp.selected,
            "choice_confidence": choice_resp.top_confidence,
            "choice_latency_ms": choice_resp.latency_ms,
            "noul_passed": noul_resp.passed,
            "noul_probability": noul_resp.probability,
            "noul_latency_ms": noul_resp.latency_ms,
            "score": score_resp.score,
            "score_breakdown": score_resp.breakdown,
            "score_latency_ms": score_resp.latency_ms,
        })

    total_time = (time.perf_counter() - start_total) * 1000.0

    # 4. Grava resultados em JSON
    out_file = Path("/home/maiteuxzanela/projeto JEV/jez_audit_results.json")
    out_file.write_text(json.dumps({
        "total_time_ms": total_time,
        "dimensions": results,
    }, indent=2), encoding="utf-8")

    print("\n" + "=" * 80)
    print(f"AUDITORIA CONCLUÍDA EM {total_time:.2f} ms ({total_time / 1000.0:.2f} s)")
    print(f"Resultados gravados em: {out_file}")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(run_jez_audit())
