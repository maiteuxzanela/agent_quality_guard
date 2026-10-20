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

import os
import tempfile
from pathlib import Path

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
