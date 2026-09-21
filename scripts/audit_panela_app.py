"""Pipeline de Auditoria Automatizada do Panela de Sobra (PanelaAndroid).
Executa o fluxo completo:
1. Needle 3: Recuperação cirúrgica relacional de módulos e trechos críticos.
2. Open Jev (GTX 1050 Ti): Classificação (choice), checagem de guardrail/risco (noul) e auditoria de nota (score).
3. Telemetria: Medição precisa de velocidade em milissegundos.
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

TARGET_APP_DIR = "/home/maiteuxzanela/PanelaBuild/PanelaAndroid"


async def run_pipeline_audit():
    print("=" * 80)
    print("INICIANDO AUDITORIA DO APP 'PANELA DE SOBRA' VIA PIPELINE JEV")
    print(f"Diretório Alvo: {TARGET_APP_DIR}")
    print("=" * 80)

    total_pipeline_start = time.perf_counter()

    # --------------------------------------------------------------------------
    # 1. Inicialização dos Motores Locais
    # --------------------------------------------------------------------------
    print("\n[FASE 0] Inicializando Motores Locais...")
    init_start = time.perf_counter()

    needle = LocalNeedleEngine(workspace_root=TARGET_APP_DIR)

    engine = OpenJevEngine(
        model_id_or_path="com-kotobalabs/open-jev-deberta-v3-large",
        device="cuda:0",
    )
    engine.load_model()
    init_time = (time.perf_counter() - init_start) * 1000.0
    print(f"  Motores prontos em: {init_time:.2f} ms (OpenJev rodando na GTX 1050 Ti)")
    print("-" * 80)

    # --------------------------------------------------------------------------
    # 2. Definição das Dimensões de Auditoria
    # --------------------------------------------------------------------------
    audit_dimensions = [
        {
            "id": "monolithic_screens",
            "name": "Telas Monolíticas (CriarScreen / CardapioScreen / PerfilScreen)",
            "query": "CriarScreen CardapioScreen PerfilScreen useState useEffect render",
            "scope": ["src/screens"],
            "risk_hypothesis": "This component has severe monolithic coupling, mixing business logic, API calls, and complex UI rendering.",
            "rubric": "Modular screen structure, small focused components, separate custom hooks for business logic.",
            "options": ["frontend_architect", "backend_specialist", "ui_ux_refactor", "security_auditor"],
        },
        {
            "id": "root_and_state",
            "name": "Ponto de Entrada e Estado Global (App.js e Contextos)",
            "query": "App AlertContext NavigationContainer useEffect global state",
            "scope": ["App.js", "src/contexts"],
            "risk_hypothesis": "The root application component acts as a god-object with excessive re-renders and tightly coupled state.",
            "rubric": "Clean application bootstrap, robust global state management, lightweight root component.",
            "options": ["frontend_architect", "state_management", "devops_build", "performance_engineer"],
        },
        {
            "id": "services_and_api",
            "name": "Camada de Serviços e Integração Externa (Services)",
            "query": "apiService authService receitaService fetch axios try catch error handling",
            "scope": ["src/services"],
            "risk_hypothesis": "The network service layer lacks centralized error handling, retry policies, or token refresh logic.",
            "rubric": "Resilient HTTP client, standardized error envelopes, token interceptors, and typed responses.",
            "options": ["backend_specialist", "security_auditor", "performance_engineer", "qa_testing"],
        },
        {
            "id": "security_and_rules",
            "name": "Segurança, Regras do Firestore e Chaves Keystore",
            "query": "firestore.rules storage.rules firebaseConfig apiKey PanelaBuild.jks",
            "scope": ["firestore.rules", "storage.rules", "src/firebaseConfig.js", "@paneladesobra__PanelaBuild.jks"],
            "risk_hypothesis": "The repository exposes sensitive release keystores, open database rules, or unconstrained storage access.",
            "rubric": "Strict Firestore security rules, secure credential storage, keystores excluded from version control.",
            "options": ["security_auditor", "devops_build", "backend_specialist", "compliance_officer"],
        },
        {
            "id": "build_and_performance",
            "name": "Configuração de Build, Dependências e Otimização Expo/Android",
            "query": "app.json eas.json package.json bundle dependencies",
            "scope": ["app.json", "eas.json", "package.json"],
            "risk_hypothesis": "The build configuration has bloat dependencies, outdated SDK versions, or missing asset optimizations.",
            "rubric": "Optimized dependency tree, production release profiling, proactive asset compression.",
            "options": ["devops_build", "performance_engineer", "qa_testing", "release_manager"],
        },
    ]

    results = []

    # --------------------------------------------------------------------------
    # 3. Execução das 5 Dimensões de Auditoria
    # --------------------------------------------------------------------------
    for idx, dim in enumerate(audit_dimensions, 1):
        print(f"\n[{idx}/5] Auditando Dimensão: {dim['name']}")

        # A) Recuperação Cirúrgica via Needle 3
        needle_start = time.perf_counter()
        needle_resp = await needle.search(
            Needle3Query(
                query=dim["query"],
                scope=dim["scope"],
                max_tokens=1200,
            )
        )
        needle_lat = (time.perf_counter() - needle_start) * 1000.0
        print(f"  [Needle 3] Recuperados {len(needle_resp.snippets)} trechos cirúrgicos ({needle_resp.total_tokens} tokens) em {needle_lat:.2f} ms")

        # Agrega o conteúdo cirúrgico para a decisão do Open Jev
        context_snippets = "\n---\n".join([f"File: {s.file_path} (lines {s.start_line}-{s.end_line})\n{s.content[:500]}" for s in needle_resp.snippets[:3]])
        if not context_snippets:
            context_snippets = f"Target Scope: {dim['scope']}"

        # B) Roteamento Especialista via Choice
        choice_req = ChoiceRequest(
            premise=f"Architecture Dimension: {dim['name']}\nAnalyzed Code Context:\n{context_snippets[:800]}",
            options=dim["options"],
        )
        choice_resp = await engine.evaluate_choice(choice_req)
        print(f"  [Choice] Subagente Atribuído: [{choice_resp.selected}] ({choice_resp.top_confidence * 100:.1f}%) | Latência: {choice_resp.latency_ms:.2f} ms")

        # C) Verificação de Risco via Noul
        noul_req = NoulRequest(
            premise=f"Dimension: {dim['name']}\nCode Evidence:\n{context_snippets[:1000]}",
            hypothesis=dim["risk_hypothesis"],
            threshold=0.50,
        )
        noul_resp = await engine.evaluate_noul(noul_req)
        has_risk = noul_resp.passed  # se True, a hipótese de risco se confirma
        risk_tag = "ALTO RISCO / CRÍTICO" if has_risk else "BAIXO RISCO"
        print(f"  [Noul] Hipótese de Risco: {risk_tag} (P={noul_resp.probability:.4f}) | Latência: {noul_resp.latency_ms:.2f} ms")

        # D) Auditoria de Qualidade via Score
        score_req = ScoreRequest(
            premise=context_snippets[:1200],
            rubric=dim["rubric"],
            min_score=1.0,
            max_score=5.0,
            threshold=3.5,
        )
        score_resp = await engine.evaluate_score(score_req)
        print(f"  [Score] Nota de Conformidade: {score_resp.score:.2f} / 5.00 | Latência: {score_resp.latency_ms:.2f} ms")
        print(f"          Distribuição: {score_resp.breakdown}")

        results.append({
            "dimension": dim["id"],
            "name": dim["name"],
            "snippets_count": len(needle_resp.snippets),
            "tokens": needle_resp.total_tokens,
            "needle_latency_ms": needle_lat,
            "choice_selected": choice_resp.selected,
            "choice_confidence": choice_resp.top_confidence,
            "choice_latency_ms": choice_resp.latency_ms,
            "noul_risk_confirmed": has_risk,
            "noul_probability": noul_resp.probability,
            "noul_latency_ms": noul_resp.latency_ms,
            "score": score_resp.score,
            "score_breakdown": score_resp.breakdown,
            "score_latency_ms": score_resp.latency_ms,
            "snippets": [
                {
                    "file": s.file_path,
                    "start": s.start_line,
                    "end": s.end_line,
                    "relations": s.relations,
                }
                for s in needle_resp.snippets[:3]
            ],
        })

    total_pipeline_time = (time.perf_counter() - total_pipeline_start) * 1000.0

    # --------------------------------------------------------------------------
    # 4. Relatório Consolidado de Desempenho e Latência
    # --------------------------------------------------------------------------
    print("\n" + "=" * 80)
    print("RELATÓRIO DE VELOCIDADE E AUDITORIA DO PIPELINE JEV")
    print("=" * 80)

    total_needle_time = sum(r["needle_latency_ms"] for r in results)
    total_choice_time = sum(r["choice_latency_ms"] for r in results)
    total_noul_time = sum(r["noul_latency_ms"] for r in results)
    total_score_time = sum(r["score_latency_ms"] for r in results)
    total_decision_time = total_choice_time + total_noul_time + total_score_time

    print(f"Tempo Total do Pipeline Completo (5 Dimensões): {total_pipeline_time:.2f} ms ({total_pipeline_time / 1000.0:.2f} s)")
    print(f"  - Tempo Total de Busca Needle 3: {total_needle_time:.2f} ms (média: {total_needle_time / len(results):.2f} ms/dimensão)")
    print(f"  - Tempo Total de Decisão Open Jev (GTX 1050 Ti): {total_decision_time:.2f} ms (média: {total_decision_time / len(results):.2f} ms/dimensão)")
    print(f"    * Choice (Roteamento): {total_choice_time:.2f} ms")
    print(f"    * Noul (Risco/Guardrail): {total_noul_time:.2f} ms")
    print(f"    * Score (Auditoria/Rubrica): {total_score_time:.2f} ms")

    # Salva relatório estruturado em JSON para síntese da LLM
    output_path = Path("/home/maiteuxzanela/projeto JEV/panela_audit_results.json")
    output_path.write_text(json.dumps({
        "total_time_ms": total_pipeline_time,
        "needle_time_ms": total_needle_time,
        "decision_time_ms": total_decision_time,
        "dimensions": results,
    }, indent=2), encoding="utf-8")
    print(f"\nResultados gravados em: {output_path}")


if __name__ == "__main__":
    asyncio.run(run_pipeline_audit())
