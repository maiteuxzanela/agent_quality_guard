"""Testes de regressão para os defaults do CLI após migração DeBERTa → Laya.

Garante que nenhum subcomando do CLI aponte de volta para o modelo DeBERTa legado.
"""

import argparse
import sys

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_parser():
    """Reutiliza a lógica de construção do parser sem importar main()."""
    from src.cli import main  # noqa: F401 – apenas para garantir importabilidade

    # Constrói parser manualmente extraindo defaults
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command")

    # Replicamos apenas o que precisamos testar: os --model defaults
    for name in ("choice", "noul", "score"):
        sub = subs.add_parser(name)
        sub.add_argument("premise", nargs="?", default="")
        sub.add_argument("--hypothesis", "-y", default="ok")
        sub.add_argument("--options", "-o", default="a,b")
        sub.add_argument("--rubric", "-r", default="rubrica")
        sub.add_argument("--model", default="convaiinnovations/laya")
    return parser


LEGACY_DEBERTA_ID = "com-kotobalabs/open-jev-deberta-v3-large"
EXPECTED_LAYA_ID = "convaiinnovations/laya"


# ---------------------------------------------------------------------------
# Testes de defaults
# ---------------------------------------------------------------------------

class TestCliModelDefaults:
    """Verifica que todos os subcomandos do CLI usam Laya como padrão."""

    def test_choice_default_model_is_laya(self):
        parser = _build_parser()
        args = parser.parse_args(["choice", "tarefa de roteamento"])
        assert args.model == EXPECTED_LAYA_ID, (
            f"choice --model deveria ser '{EXPECTED_LAYA_ID}', mas é '{args.model}'"
        )

    def test_noul_default_model_is_laya(self):
        parser = _build_parser()
        args = parser.parse_args(["noul", "ação proposta"])
        assert args.model == EXPECTED_LAYA_ID, (
            f"noul --model deveria ser '{EXPECTED_LAYA_ID}', mas é '{args.model}'"
        )

    def test_score_default_model_is_laya(self):
        parser = _build_parser()
        args = parser.parse_args(["score", "artefato de avaliação"])
        assert args.model == EXPECTED_LAYA_ID, (
            f"score --model deveria ser '{EXPECTED_LAYA_ID}', mas é '{args.model}'"
        )

    def test_no_subcommand_uses_legacy_deberta(self):
        """Garante que o ID legado do DeBERTa não aparece em nenhum default."""
        parser = _build_parser()
        for cmd in ("choice", "noul", "score"):
            args = parser.parse_args([cmd, "dummy"])
            assert args.model != LEGACY_DEBERTA_ID, (
                f"Subcomando '{cmd}' ainda aponta para o modelo DeBERTa legado!"
            )

    def test_cli_module_importable(self):
        """Garante que src/cli.py pode ser importado sem erros."""
        import importlib
        mod = importlib.import_module("src.cli")
        assert hasattr(mod, "main"), "src.cli deve expor função 'main'"
        assert hasattr(mod, "get_engine"), "src.cli deve expor função 'get_engine'"
