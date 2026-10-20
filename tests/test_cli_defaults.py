"""Testes de regressão do CLI após a desativação do motor Laya (TICKET-HARNESS-47).

O CLI deixou de expor `choice`, `noul` e `score` (dependentes de
`OpenJevEngine`/`convaiinnovations/laya`). Permanece apenas a busca cirúrgica
`needle`, sem importar torch nem o motor de decisão.
"""

import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parse_real(argv):
    """Parseia argv contra o parser REAL do CLI."""
    from agent_quality_guard.cli import build_parser

    return build_parser().parse_args(argv)


def test_cli_module_importable_without_engine():
    """src.cli deve importar sem carregar o motor Laya/PyTorch.

    Roda em subprocesso para não sofrer contaminação de sys.modules por outros
    testes da suíte que legitimamente importam o motor de decisão.
    """
    code = (
        "import sys; sys.path.insert(0, {root!r}); import agent_quality_guard.cli as cli; "
        "assert hasattr(cli, 'main'); "
        "assert hasattr(cli, 'build_parser'); "
        "assert not hasattr(cli, 'get_engine'); "
        "assert 'agent_quality_guard.decision.engine' not in sys.modules; "
        "assert 'torch' not in sys.modules; "
        "print('ok')"
    ).format(root=str(_PROJECT_ROOT))
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(_PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_needle_subcommand_defaults():
    args = _parse_real(["needle", "agent_quality_guard"])
    assert args.command == "needle"
    assert args.max_tokens == 1000
    assert args.depth == 2
    assert args.no_integrity is False
    assert args.json is False


@pytest.mark.parametrize(
    "argv",
    [
        ["choice", "x", "-o", "a,b"],
        ["noul", "x", "-y", "h"],
        ["score", "x"],
    ],
)
def test_retired_laya_subcommands_are_rejected(argv):
    """Subcomandos Laya aposentados devem falhar no parse (SystemExit)."""
    with pytest.raises(SystemExit):
        _parse_real(argv)
