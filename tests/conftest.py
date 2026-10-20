"""Isolamento do estado do circuit breaker durante a suíte (anti-wipe).

Sem `JEV_STATE_DIR`, `history_clear()`/`history_set()` dos testes operavam em
`/tmp/openjev_state` — o mesmo diretório que guarda o registro de homologação
e o contador de 5 tentativas do workspace real. Uma execução da suíte apagou
o registro `APPROVED` em 29/09/2026 (achado de auditoria: "histórico global
apagável").

Definido na importação do conftest (e não em fixture autouse) para cobrir
também os testes `unittest.TestCase`, que não recebem fixtures por assinatura.
Qualquer gravação/limpeza da suíte passa a acontecer neste diretório exclusivo
da sessão de teste.
"""

import importlib.util
import os
import sys
import tempfile
from pathlib import Path
from typing import Iterable, List, Tuple

_TEST_STATE_DIR = Path(tempfile.mkdtemp(prefix="jev_test_state_"))
os.environ["JEV_STATE_DIR"] = str(_TEST_STATE_DIR)
os.environ["HF_HUB_OFFLINE"] = "1"
os.environ["TRANSFORMERS_OFFLINE"] = "1"

# Isolamento do executor externo: com `JEV_USE_HERMES` unset (default "true"),
# `SubagentRunner.spawn()` invoca o binário real do Hermes (agente LLM) dentro
# do workspace do teste. Isso tornava a suíte lenta (~6-7 min) e não-determinística,
# pois o Hermes podia reescrever o arquivo violador antes da varredura AST.
# Os testes que exercitam o Hermes chamam `_execute_via_hermes()` diretamente
# com binários de prova, portanto não dependem deste branch automático.
os.environ["JEV_USE_HERMES"] = "false"

# TICKET-SGA-06 — Resiliência de ambiente: rodar a suíte fora do `.venv` do
# projeto (ou sem as dependências opcionais) gera falhas de coleta confusas.
# O diagnóstico abaixo é informativo e NUNCA falha a sessão: o job de CI
# "sem tree-sitter" precisa continuar exercitando o caminho de fallback.
_DIAGNOSED_MODULES: Tuple[Tuple[str, bool], ...] = (
    ("tree_sitter", True),
    ("tree_sitter_language_pack", True),
    ("pytest_asyncio", False),
)


def diagnose_test_environment(
    modules: Iterable[Tuple[str, bool]] = _DIAGNOSED_MODULES,
) -> List[str]:
    """Reporta módulos ausentes e como alinhar o interpretador ao projeto.

    ``modules`` é uma sequência de ``(nome_do_módulo, opcional)``. A função usa
    ``importlib.util.find_spec`` real (sem mocks) e devolve a lista de módulos
    ausentes; quando há ausências, imprime um bloco amigável em ``stderr``.

    Exemplo:
        >>> diagnose_test_environment([("sys", False), ("modulo_inexistente_xyz", True)])
        ['modulo_inexistente_xyz']
    """
    missing: List[Tuple[str, bool]] = []
    for name, optional in modules:
        try:
            present = importlib.util.find_spec(name) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            present = False
        if not present:
            missing.append((name, optional))

    if not missing:
        return []

    inside_venv = sys.prefix != getattr(sys, "base_prefix", sys.prefix)
    lines = [
        "",
        "[search-guard] Diagnóstico de ambiente da suíte de testes:",
        f"  Interpretador em uso: {sys.executable} (venv detectado: "
        f"{'sim' if inside_venv else 'não'})",
        "  Módulos ausentes:",
    ]
    for name, optional in missing:
        kind = "opcional — lanes dependentes usam fallback determinístico" if optional else "obrigatório"
        lines.append(f"    - {name} ({kind})")
    lines.extend(
        [
            "  Para alinhar o ambiente ao projeto (recomendado):",
            '    python -m venv .venv && .venv/bin/pip install -r requirements.txt \\',
            '        && .venv/bin/pip install ".[dev]" && .venv/bin/pytest tests/',
            "",
        ]
    )
    print("\n".join(lines), file=sys.stderr)
    return [name for name, _optional in missing]


diagnose_test_environment()

