"""Suíte determinística do diagnóstico de GPU em `subagent_runner.py`.

Escopo (TICKET-AQG-01): garantir que o relatório de hardware de @Kael não
depende mais da camada removida `src/decision/` (Laya/RLCD) e que os três
caminhos de execução são reais e determinísticos:

1. CUDA disponível  -> Compute Capability lida de `torch.cuda.get_device_properties`,
   bfloat16 bloqueado para arquitetura < sm_80 (Pascal GP107 = 6.1) e
   recomendação `float16`/`cuda`.
2. CUDA indisponível -> fallback `float32`/`cpu` (provado escondendo a GPU do
   processo via `CUDA_VISIBLE_DEVICES=""`, controle real do driver).
3. PyTorch ausente    -> fallback `float32`/`cpu` via `except ImportError`
   (provado em interpretador real sem `torch` no `sys.path`).

Nenhum mock, stub ou monkeypatch: os fallbacks são exercitados mudando o
AMBIENTE real do processo (visibilidade CUDA) e o CONJUNTO real de pacotes
disponíveis (farm de symlinks sem torch), sempre importando o módulo de
produção de verdade.
"""

from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_FILE = REPO_ROOT / "src" / "orchestrator" / "subagent_runner.py"

# Prefixos de pacotes que compõem a stack CUDA/Torch e devem ficar FORA da
# farm de symlinks para que `import torch` falhe de verdade com ImportError.
_TORCH_PACKAGE_PREFIXES = (
    "torch",
    "torchgen",
    "torchvision",
    "torchaudio",
    "functorch",
    "triton",
    "nvidia",
    "cupy",
)


def _parse_source() -> Tuple[str, ast.Module]:
    source = SRC_FILE.read_text(encoding="utf-8")
    return source, ast.parse(source)


def _diagnose_function() -> Tuple[ast.FunctionDef, str]:
    source, tree = _parse_source()
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_diagnose_gpu_hardware":
            segment = ast.get_source_segment(source, node)
            assert segment, "Falha ao extrair o corpo real de _diagnose_gpu_hardware"
            return node, segment
    pytest.fail("_diagnose_gpu_hardware ausente em src/orchestrator/subagent_runner.py")


def _run_report_subprocess(
    interpreter: str,
    extra_args: List[str],
    env: Dict[str, str],
) -> str:
    """Executa o módulo real em subprocesso e devolve o relatório de hardware."""
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
        "import importlib.util\n"
        "from src.orchestrator.subagent_runner import SubagentRunner\n"
        "report = SubagentRunner()._diagnose_gpu_hardware()\n"
        "payload = {\n"
        "    'report': report,\n"
        "    'torch_spec': importlib.util.find_spec('torch') is not None,\n"
        "}\n"
        "print('__REPORT__' + json.dumps(payload))\n"
    )
    proc = subprocess.run(
        [interpreter, *extra_args, "-c", script],
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert proc.returncode == 0, (
        f"Subprocesso de diagnóstico falhou (rc={proc.returncode}).\n"
        f"stderr: {proc.stderr[-2000:]}"
    )
    marker = "__REPORT__"
    payload_line = next(
        (line for line in proc.stdout.splitlines() if line.startswith(marker)),
        None,
    )
    assert payload_line is not None, f"Relatório não emitido. stdout: {proc.stdout[-2000:]}"
    payload: Dict[str, Any] = json.loads(payload_line[len(marker):])
    return payload["report"]


# ---------------------------------------------------------------------------
# 1. Ausência estrutural da camada removida src/decision/
# ---------------------------------------------------------------------------
def test_dead_decision_layer_fully_removed() -> None:
    source, tree = _parse_source()

    imported_modules: List[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    offending = [mod for mod in imported_modules if "decision" in mod.split(".")]
    assert offending == [], f"subagent_runner.py ainda importa a camada morta: {offending}"
    assert "decision.hardware" not in source
    assert "get_hardware_profile" not in source
    assert not (REPO_ROOT / "src" / "decision").exists(), (
        "A pasta src/decision/ foi removida e não pode reaparecer"
    )


def test_diagnostics_use_torch_native_api_as_single_source_of_truth() -> None:
    _node, body = _diagnose_function()

    # A única fonte de hardware é o PyTorch nativo.
    assert "torch.cuda.is_available()" in body
    assert "torch.cuda.get_device_properties(0)" in body
    assert "torch.cuda.get_device_name(0)" in body

    # Limiar canônico: bfloat16 nativo só a partir de Ampere (sm_80).
    # Pascal GP107 (sm_61) DEVE ser bloqueado e coagido para float16/float32.
    assert "supports_bfloat16 = major >= 8" in body

    # O fallback de dependência ausente precisa ser especificamente ImportError.
    assert "except ImportError:" in body
    assert "`float32`" in body
    assert "`cpu`" in body


# ---------------------------------------------------------------------------
# 2. Caminho CUDA real (hardware físico) — consistente com o ambiente
# ---------------------------------------------------------------------------
def test_live_report_matches_real_hardware() -> None:
    torch = pytest.importorskip("torch")
    from src.orchestrator.subagent_runner import SubagentRunner

    report = SubagentRunner()._diagnose_gpu_hardware()

    assert report.startswith("### Relatório Técnico de Hardware — @Kael (ML & Pascal)")
    assert report.count("- **Precisão Recomendada:**") == 1
    assert report.count("- **Dispositivo Recomendado:**") == 1

    if torch.cuda.is_available():
        props = torch.cuda.get_device_properties(0)
        major, minor = int(props.major), int(props.minor)
        assert f"- **Compute Capability:** {major}.{minor}" in report
        assert f"- **Dispositivo PyTorch:** {torch.cuda.get_device_name(0)}" in report
        assert "- **Precisão Recomendada:** `float16`" in report
        assert "- **Dispositivo Recomendado:** `cuda`" in report
        assert "- **Memória Alocada pelo PyTorch:**" in report
        assert "- **Memória Reservada pelo PyTorch:**" in report
        # Invariante Pascal/Ampere, derivada do hardware REAL (não da constante).
        if major < 8:
            assert "**Suporte a bfloat16:** NÃO SUPORTADO" in report
        else:
            assert "**Suporte a bfloat16:** NATIVO" in report
    else:
        assert "- **PyTorch CUDA:** Indisponível no processo." in report
        assert "- **Precisão Recomendada:** `float32`" in report
        assert "- **Dispositivo Recomendado:** `cpu`" in report


# ---------------------------------------------------------------------------
# 3. Fallback CPU real: GPU escondida do processo pelo driver
# ---------------------------------------------------------------------------
def test_cpu_fallback_when_cuda_is_hidden_from_process() -> None:
    pytest.importorskip("torch")

    env = dict(os.environ)
    env["CUDA_VISIBLE_DEVICES"] = ""
    report = _run_report_subprocess(sys.executable, [], env)

    assert "- **PyTorch CUDA:** Indisponível no processo." in report
    assert "- **Precisão Recomendada:** `float32`" in report
    assert "- **Dispositivo Recomendado:** `cpu`" in report
    # Nenhum vestígio do caminho GPU no fallback.
    assert "CUDA: Ativo" not in report
    assert "Compute Capability" not in report
    assert "Suporte a bfloat16" not in report
    assert "`float16`" not in report
    assert "`cuda`" not in report


# ---------------------------------------------------------------------------
# 4. Fallback ImportError real: interpretador sem torch instalado
# ---------------------------------------------------------------------------
def test_import_error_fallback_without_torch_installed() -> None:
    pytest.importorskip("torch")  # baseline: o interpretador atual TEM torch

    site_packages = Path(sysconfig.get_paths()["purelib"])
    assert site_packages.is_dir(), f"site-packages não encontrado: {site_packages}"

    with tempfile.TemporaryDirectory(prefix="jev_no_torch_") as farm:
        for entry in site_packages.iterdir():
            if entry.name.lower().startswith(_TORCH_PACKAGE_PREFIXES):
                continue
            os.symlink(entry, Path(farm) / entry.name)

        script = (
            "import json, sys\n"
            f"sys.path.insert(0, {farm!r})\n"
            f"sys.path.insert(0, {str(REPO_ROOT)!r})\n"
            "import importlib.util\n"
            "assert importlib.util.find_spec('torch') is None\n"
            "from src.orchestrator.subagent_runner import SubagentRunner\n"
            "report = SubagentRunner()._diagnose_gpu_hardware()\n"
            "print('__REPORT__' + json.dumps({'report': report, 'torch_spec': False}))\n"
        )
        # `-S` desliga o site-packages real; a farm (sem torch) vira a única origem
        # de dependências. É um ambiente REAL sem a dependência opcional, não um stub.
        proc = subprocess.run(
            [sys.executable, "-S", "-c", script],
            cwd=str(REPO_ROOT),
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=180,
        )

    assert proc.returncode == 0, (
        f"Subprocesso sem torch falhou (rc={proc.returncode}).\n"
        f"stderr: {proc.stderr[-2000:]}"
    )
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("__REPORT__")),
        None,
    )
    assert line is not None, f"Relatório não emitido. stdout: {proc.stdout[-2000:]}"
    report = json.loads(line[len("__REPORT__"):])["report"]

    # Mensagem exclusiva do ramo `except ImportError`.
    assert "- **PyTorch:** Indisponível no processo (fallback CPU em float32)." in report
    assert "- **Precisão Recomendada:** `float32`" in report
    assert "- **Dispositivo Recomendado:** `cpu`" in report
    assert "CUDA: Ativo" not in report
    assert "Compute Capability" not in report
    assert "Suporte a bfloat16" not in report
