"""Auditoria Estratégica e Análise de Caminhos Futuros do Projeto Predictera.
Utiliza o Pipeline Tri-Camada (Needle 3 Local + Open Jev na GTX 1050 Ti + LLM)
para decodificar as fases históricas, débitos técnicos e avaliar os caminhos futuros.
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

PREDICTERA_DIR = "/mnt/94CCB337CCB3130A/predictera"


async def run_predictera_analysis():
    print("=" * 80)
    print("INICIANDO AUDITORIA ARQUITETURAL E ESTRATÉGICA DO PROJETO PREDICTERA")
    print(f"Diretório Alvo: {PREDICTERA_DIR}")
    print("=" * 80)

    start_total = time.perf_counter()

    # 1. Inicializa motores locais
    print("\n[1/4] Inicializando Motores Locais...")
    needle = LocalNeedleEngine(workspace_root=PREDICTERA_DIR)
    engine = OpenJevEngine(
        model_id_or_path="com-kotobalabs/open-jev-deberta-v3-large",
        device="cuda:0",
    )
    engine.load_model()
    print("  Motores prontos (Open Jev ativo na GTX 1050 Ti).")
    print("-" * 80)

    # 2. Investigação Cirúrgica das Fases e Conflitos com Needle 3
    print("\n[2/4] Executando Varredura Cirúrgica com Needle 3...")

    queries = [
        {
            "id": "evolution_era",
            "name": "Era da Neuroevolução (Legado)",
            "query": "genoma alquimista janela de ouro holdout 104547",
            "scope": ["SPEC.md", "predictera_evolution.py"],
        },
        {
            "id": "causal_era",
            "name": "Era Causal Dixon-Coles (+EV Betano)",
            "query": "dixon coles poisson valor esperado ev betano cotações ataque defesa",
            "scope": ["novasorientacoes.md", "predictera_predict_causal.py"],
        },
        {
            "id": "manual_friction",
            "name": "Fricção Operacional e Dados Manuais",
            "query": "data_1600 previsoes json adicionar resultados rodada",
            "scope": ["novasorientacoes.md", "SPEC.md"],
        },
        {
            "id": "validation_and_bankroll",
            "name": "Validação e Gestão de Banca",
            "query": "banca livro caixa tranche ratchet sensibilidade",
            "scope": ["analise_sensibilidade.py", "banca_predictera.json", "novasorientacoes.md"],
        },
    ]

    retrieved_contexts = {}
    for q in queries:
        t0 = time.perf_counter()
        resp = await needle.search(Needle3Query(query=q["query"], scope=q["scope"], max_tokens=1000))
        lat = (time.perf_counter() - t0) * 1000.0
        retrieved_contexts[q["id"]] = resp
        print(f"  [Needle 3] {q['name']}: {len(resp.snippets)} trechos ({resp.total_tokens} tokens) em {lat:.2f} ms")

    print("-" * 80)

    # 3. Avaliação dos 4 Caminhos Futuros via Open Jev na GTX 1050 Ti
    print("\n[3/4] Avaliando Caminhos Estratégicos com Open Jev na GTX 1050 Ti...")

    strategic_paths = [
        {
            "id": "caminho_1_automacao_quant",
            "title": "Caminho 1: Pipeline Causal Automatizado (Full Quant + Scraping +EV)",
            "description": (
                "Automatizar 100% da ingestão de dados via API de futebol (eliminando edição manual de JSON), "
                "conectar raspagem automatizada de odds da Betano/Pinnacle, rodar Dixon-Coles automaticamente "
                "e despachar alertas de apostas +EV via Telegram/Discord com gestão de banca programática."
            ),
            "feasibility_hypothesis": "This path eliminates manual friction, has high positive expected value, and is immediately feasible with current hardware.",
            "rubric": "High engineering feasibility, direct operational ROI, low technical risk, complete elimination of manual error.",
            "roles": ["quant_developer", "data_engineer", "devops_automation", "financial_analyst"],
        },
        {
            "id": "caminho_2_xg_ml_ensemble",
            "title": "Caminho 2: Expansão com Machine Learning Tabular & xG (Expected Goals)",
            "description": (
                "Substituir apenas placares brutos por métricas avançadas de Expected Goals (xG), "
                "desfalques, fadiga de calendário e treinar um modelo de Gradient Boosting (LightGBM/CatBoost) "
                "como meta-aprendizado calibrando os resíduos do Dixon-Coles contra o mercado."
            ),
            "feasibility_hypothesis": "Enriching the model with xG and gradient boosting significantly improves edge over naive bookmaker Poisson models.",
            "rubric": "High predictive alpha, moderate data complexity, requires reliable xG data sources, solid statistical foundation.",
            "roles": ["data_scientist", "ml_engineer", "sports_analyst", "quant_researcher"],
        },
        {
            "id": "caminho_3_deep_learning_sequencial",
            "title": "Caminho 3: Deep Learning Sequencial (LSTMs / Transformers / Chronos)",
            "description": (
                "Tentar ressuscitar a abordagem sequencial da Era 1, mas utilizando redes neurais modernas "
                "(LSTMs temporais ou Foundation Models de séries temporais) para prever sequências temporais de partidas."
            ),
            "feasibility_hypothesis": "Deep learning models trained on raw sequential match sequences will consistently beat market odds in football betting.",
            "rubric": "High overfitting risk, low signal-to-noise ratio in football scores, heavy compute requirements, unproven betting edge.",
            "roles": ["deep_learning_researcher", "mlops_engineer", "gpu_specialist"],
        },
        {
            "id": "caminho_4_agente_autonomo_jev",
            "title": "Caminho 4: Sistema de Decisão Autônoma JEV-Predictera (Tri-Camada)",
            "description": (
                "Integrar o Predictera à nossa arquitetura tri-camada: Needle 3 recupera dados históricos e H2H, "
                "Open Jev atua como guardrail estrito de aposta (noul: EV >= 8% e liquidez OK) e sizing de banca (score: fração de Kelly), "
                "e a LLM sintetiza o relatório de fundamento da aposta para o apostador."
            ),
            "feasibility_hypothesis": "An agentic tri-layer orchestration provides explainability, strict risk guardrails, and automated execution without human bias.",
            "rubric": "Exceptional decision governance, zero emotional bias in betting, robust risk control, cutting-edge architecture.",
            "roles": ["ai_architect", "agentic_engineer", "risk_manager", "quant_developer"],
        },
    ]

    eval_results = []
    for path in strategic_paths:
        print(f"\n  Avaliando: {path['title']}")

        # Choice: perfil mais indicado
        choice_resp = await engine.evaluate_choice(ChoiceRequest(
            premise=f"Project: Predictera Quant\nProposal: {path['description']}",
            options=path["roles"],
        ))
        print(f"    [Choice] Perfil ideal: [{choice_resp.selected}] ({choice_resp.top_confidence * 100:.1f}%)")

        # Noul: viabilidade e aderência matemática
        noul_resp = await engine.evaluate_noul(NoulRequest(
            premise=f"Proposal: {path['description']}",
            hypothesis=path["feasibility_hypothesis"],
            threshold=0.50,
        ))
        print(f"    [Noul] Viabilidade Aprovada: {noul_resp.passed} (P={noul_resp.probability:.4f})")

        # Score: Nota estratégica de 1.0 a 5.0
        score_resp = await engine.evaluate_score(ScoreRequest(
            premise=f"Proposal: {path['description']}",
            rubric=path["rubric"],
            min_score=1.0,
            max_score=5.0,
            threshold=3.5,
        ))
        print(f"    [Score] Nota Estratégica: {score_resp.score:.2f} / 5.00")
        print(f"            Distribuição: {score_resp.breakdown}")

        eval_results.append({
            "id": path["id"],
            "title": path["title"],
            "selected_role": choice_resp.selected,
            "role_confidence": choice_resp.top_confidence,
            "feasibility_passed": noul_resp.passed,
            "feasibility_probability": noul_resp.probability,
            "strategic_score": score_resp.score,
            "breakdown": score_resp.breakdown,
        })

    total_time = (time.perf_counter() - start_total) * 1000.0

    # 4. Salvar resultados
    out_file = Path("/home/maiteuxzanela/projeto JEV/predictera_analysis_results.json")
    out_file.write_text(json.dumps({
        "total_time_ms": total_time,
        "evaluations": eval_results,
    }, indent=2), encoding="utf-8")

    print("\n" + "=" * 80)
    print(f"ANÁLISE ESTRATÉGICA CONCLUÍDA EM {total_time:.2f} ms ({total_time / 1000.0:.2f} s)")
    print(f"Resultados gravados em: {out_file}")
    print("=" * 80)


if __name__ == "__main__":
    asyncio.run(run_predictera_analysis())
