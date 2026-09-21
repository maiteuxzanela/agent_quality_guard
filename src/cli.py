#!/usr/bin/env python3
"""CLI Executável Unificado para a Arquitetura Tri-Camada (Projeto JEV).
Disponibiliza as primitivas Needle 3, Choice, Noul e Score diretamente no terminal.
"""

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

# Adiciona a raiz do projeto JEV ao sys.path
SCRIPT_DIR = Path(__file__).parent.parent.resolve()
sys.path.insert(0, str(SCRIPT_DIR))

from src.core.schemas import (
    ChoiceRequest,
    Needle3Query,
    NoulRequest,
    ScoreRequest,
)
from src.decision.engine import OpenJevEngine
from src.memory.local_needle import LocalNeedleEngine
from src.orchestrator.evaluator import DeliveryEvaluator
from src.orchestrator.guardrail import GuardrailInterceptor
from src.orchestrator.router import SpecialistRouter


def get_engine(model_path="convaiinnovations/laya"):
    engine = OpenJevEngine(model_id_or_path=model_path, allow_cpu_fallback=True)
    engine.load_model()
    return engine


async def cmd_needle(args):
    workspace = args.workspace or os.getcwd()
    needle = LocalNeedleEngine(workspace_root=workspace)
    scope = [s.strip() for s in args.scope.split(",")] if args.scope else None

    query = Needle3Query(
        query=args.query,
        scope=scope,
        max_tokens=args.max_tokens,
        relational_depth=args.depth,
        preserve_relational_integrity=not args.no_integrity,
    )

    t0 = time.perf_counter()
    resp = await needle.search(query)
    lat_ms = (time.perf_counter() - t0) * 1000.0

    if args.json:
        print(resp.model_dump_json(indent=2))
        return

    print("=" * 70)
    print(f"🌵 CACTUS NEEDLE 3 (Local On-Device) — {lat_ms:.2f} ms")
    print(f"Consulta: '{args.query}' | Escopo: {scope or 'Global'} | Limite: {args.max_tokens} tokens")
    print(f"Total Recuperado: {len(resp.snippets)} trecho(s) ({resp.total_tokens} tokens)")
    print("=" * 70)

    for i, s in enumerate(resp.snippets, 1):
        rel_str = f" [{', '.join(s.relations)}]" if s.relations else ""
        print(f"\n--- Trecho #{i}: {s.file_path}:{s.start_line}-{s.end_line} (Relevância: {s.relevance_score:.2f}){rel_str} ---")
        print(s.content)
    print("\n" + "=" * 70)


async def cmd_choice(args):
    engine = get_engine(args.model)
    options = [o.strip() for o in args.options.split(",")] if isinstance(args.options, str) else args.options

    req = ChoiceRequest(
        premise=args.premise,
        options=options,
        temperature=args.temperature,
    )

    resp = await engine.evaluate_choice(req)

    if args.json:
        print(resp.model_dump_json(indent=2))
        return

    print("=" * 70)
    print(f"🎯 OPEN JEV: CHOICE ROUTER — {resp.latency_ms:.2f} ms")
    print(f"Premissa: {args.premise[:100]}...")
    print(f"🏆 Subagente Eleito: @{resp.selected} (Confiança: {resp.top_confidence * 100:.1f}%)")
    print("-" * 70)
    print("Distribuição de Probabilidades:")
    sorted_probs = sorted(resp.probabilities.items(), key=lambda x: x[1], reverse=True)
    for opt, p in sorted_probs:
        bar = "█" * int(p * 30)
        marker = " 👈" if opt == resp.selected else ""
        print(f"  {opt:<20} | {p * 100:5.1f}% | {bar}{marker}")
    print("=" * 70)


async def cmd_noul(args):
    engine = get_engine(args.model)
    req = NoulRequest(
        premise=args.premise,
        hypothesis=args.hypothesis,
        threshold=args.threshold,
    )

    resp = await engine.evaluate_noul(req)

    if args.json:
        print(resp.model_dump_json(indent=2))
        sys.exit(0 if resp.passed else 1)

    status_tag = "✅ APROVADO" if resp.passed else "🚨 BLOQUEADO PELO GUARDRAIL"
    print("=" * 70)
    print(f"🛡️ OPEN JEV: NOUL GUARDRAIL — {resp.latency_ms:.2f} ms")
    print(f"Status: {status_tag}")
    print(f"Probabilidade P(válido): {resp.probability * 100:.2f}% (Corte: >= {resp.threshold * 100:.1f}%)")
    print(f"Certeza/Confiança: {resp.confidence * 100:.1f}%")
    print(f"Hipótese avaliada: '{args.hypothesis}'")
    print("=" * 70)

    if not resp.passed:
        sys.exit(1)


async def cmd_score(args):
    premise_content = args.premise
    if args.file:
        file_path = Path(args.file)
        if not file_path.exists():
            print(f"Erro: Arquivo '{args.file}' não encontrado.", file=sys.stderr)
            sys.exit(2)
        premise_content = file_path.read_text(encoding="utf-8", errors="replace")

    if not premise_content:
        print("Erro: Premissa ou arquivo deve ser fornecido.", file=sys.stderr)
        sys.exit(2)

    engine = get_engine(args.model)
    req = ScoreRequest(
        premise=premise_content,
        rubric=args.rubric,
        min_score=args.min_score,
        max_score=args.max_score,
        threshold=args.threshold,
    )

    resp = await engine.evaluate_score(req)

    if args.json:
        print(resp.model_dump_json(indent=2))
        sys.exit(0 if resp.passed else 1)

    status_tag = "✅ APROVADO" if resp.passed else "⚠️ REPROVADO (Requer Refatoração)"
    print("=" * 70)
    print(f"📊 OPEN JEV: SCORE EVALUATION — {resp.latency_ms:.2f} ms")
    print(f"Veredito: {status_tag}")
    print(f"Nota Obtida: {resp.score:.2f} / {args.max_score:.1f} (Corte Mínimo: >= {args.threshold:.1f})")
    print(f"Nota Normalizada: {resp.normalized_score * 100:.1f}%")
    print(f"Rubrica de Avaliação: '{args.rubric}'")
    print("=" * 70)

    if resp.passed is False:
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        prog="jev",
        description="CLI do Ecossistema JEV (Cactus Needle 3 + Open Jev Local na GTX 1050 Ti)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Subcomando needle
    p_needle = subparsers.add_parser("needle", help="Busca cirúrgica relacional com Needle 3")
    p_needle.add_argument("query", help="Consulta semântica ou nome de função/símbolo")
    p_needle.add_argument("--scope", "-s", help="Caminhos separados por vírgula (ex: site/app.js,site/admin.js)")
    p_needle.add_argument("--max-tokens", "-m", type=int, default=1000, help="Limite máximo de tokens")
    p_needle.add_argument("--depth", "-d", type=int, default=2, help="Profundidade relacional de nós AST")
    p_needle.add_argument("--workspace", "-w", help="Diretório raiz do workspace")
    p_needle.add_argument("--no-integrity", action="store_true", help="Desativa integridade estrita de blocos")
    p_needle.add_argument("--json", action="store_true", help="Saída em formato JSON estruturado")

    # Subcomando choice
    p_choice = subparsers.add_parser("choice", help="Roteamento de subagente especialista via choice")
    p_choice.add_argument("premise", help="Descrição detalhada da tarefa ou demanda")
    p_choice.add_argument(
        "--options",
        "-o",
        default="alex_cto,ariel_brand,lumi_ux,sam_payments,cris_merchant,morgan_security,noa_growth,robin_qa",
        help="Opções de subagentes separadas por vírgula",
    )
    p_choice.add_argument("--temperature", "-t", type=float, default=1.0, help="Temperatura do softmax")
    p_choice.add_argument("--model", default="convaiinnovations/laya", help="Modelo Open Jev (default: Laya)")
    p_choice.add_argument("--json", action="store_true", help="Saída em formato JSON estruturado")

    # Subcomando noul
    p_noul = subparsers.add_parser("noul", help="Guardrail pré-ação crítica via noul")
    p_noul.add_argument("premise", help="Ação, comando ou modificação proposta")
    p_noul.add_argument("--hypothesis", "-y", required=True, help="Condição de segurança a validar")
    p_noul.add_argument("--threshold", "-t", type=float, default=0.85, help="Limiar de aprovação (default: 0.85)")
    p_noul.add_argument("--model", default="convaiinnovations/laya", help="Modelo Open Jev (default: Laya)")
    p_noul.add_argument("--json", action="store_true", help="Saída em formato JSON estruturado")

    # Subcomando score
    p_score = subparsers.add_parser("score", help="Avaliação ordinal e nota de conformidade via score")
    p_score.add_argument("premise", nargs="?", default="", help="Conteúdo do artefato ou diff")
    p_score.add_argument("--file", "-f", help="Caminho do arquivo para avaliar")
    p_score.add_argument("--rubric", "-r", required=True, help="Critérios de qualidade e rubrica")
    p_score.add_argument("--min-score", type=float, default=1.0, help="Nota mínima (default: 1.0)")
    p_score.add_argument("--max-score", type=float, default=5.0, help="Nota máxima (default: 5.0)")
    p_score.add_argument("--threshold", "-t", type=float, default=4.0, help="Nota de corte para aprovação (default: 4.0)")
    p_score.add_argument("--model", default="convaiinnovations/laya", help="Modelo Open Jev (default: Laya)")
    p_score.add_argument("--json", action="store_true", help="Saída em formato JSON estruturado")

    args = parser.parse_args()

    if args.command == "needle":
        asyncio.run(cmd_needle(args))
    elif args.command == "choice":
        asyncio.run(cmd_choice(args))
    elif args.command == "noul":
        asyncio.run(cmd_noul(args))
    elif args.command == "score":
        asyncio.run(cmd_score(args))


if __name__ == "__main__":
    main()
