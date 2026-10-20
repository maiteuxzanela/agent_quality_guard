#!/usr/bin/env python3
"""CLI Executável do Projeto JEV.

Expõe a busca cirúrgica (Needle 3) diretamente no terminal.

TICKET-HARNESS-47: as primitivas probabilísticas do motor legado de decisão e
a avaliação de score dependente dele foram desativadas. O CLI não importa
``src.decision.engine``, não carrega pesos de modelo e não aloca VRAM.
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

from agent_quality_guard.core.schemas import Needle3Query
from agent_quality_guard.memory.local_needle import LocalNeedleEngine


async def cmd_needle(args):
    """Executa a busca cirúrgica local (Needle 3) e imprime o contexto."""
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
        print(f"\n--- Trecho #{i}: {s.file_path}:{s.start_line}-{s.end_line} "
              f"(Relevância: {s.relevance_score:.2f}){rel_str} ---")
        print(s.content)
    print("\n" + "=" * 70)


def build_parser() -> argparse.ArgumentParser:
    """Constrói o parser do CLI (sem executar parse_args).

    Exposto para que os testes de regressão inspecionem o parser REAL.
    """
    parser = argparse.ArgumentParser(
        prog="jev",
        description="CLI do Projeto JEV (Cactus Needle 3 — busca cirúrgica local)",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    p_needle = subparsers.add_parser("needle", help="Busca cirúrgica relacional com Needle 3")
    p_needle.add_argument("query", help="Consulta semântica ou nome de função/símbolo")
    p_needle.add_argument(
        "--scope", "-s",
        help="Caminhos separados por vírgula (ex: site/app.js,site/admin.js)",
    )
    p_needle.add_argument("--max-tokens", "-m", type=int, default=1000, help="Limite máximo de tokens")
    p_needle.add_argument("--depth", "-d", type=int, default=2, help="Profundidade relacional de nós AST")
    p_needle.add_argument("--workspace", "-w", help="Diretório raiz do workspace")
    p_needle.add_argument("--no-integrity", action="store_true", help="Desativa integridade estrita de blocos")
    p_needle.add_argument("--json", action="store_true", help="Saída em formato JSON estruturado")

    return parser


def main():
    parser = build_parser()
    args = parser.parse_args()

    if args.command == "needle":
        asyncio.run(cmd_needle(args))


if __name__ == "__main__":
    main()
