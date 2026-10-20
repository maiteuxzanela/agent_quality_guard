"""Testes de regressão do CLI determinístico do Search Guard.

O CLI expõe apenas a busca cirúrgica `needle`, sem importar torch nem motores
neurais locais.
"""

import subprocess
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _parse_real(argv):
    """Parseia argv contra o parser REAL do CLI."""
    from agent_quality_guard.cli import build_parser

    return build_parser().parse_args(argv)


def test_cli_module_importable_without_engine():
    """agent_quality_guard.cli deve importar sem carregar PyTorch nem motor neural.

    Roda em subprocesso para não sofrer contaminação de sys.modules por outros
    testes da suíte.
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
