# ADR 001: Circuit Breaker e Trava Anti-Premise Gaming no MCP Server

## 1. Contexto e Problema
Em arquiteturas multi-agentes com guardrails estritos de conformidade (motor **Laya** local, nota de corte **≥ 3.40**; era DeBERTa 3.75 — não usar), identificou-se dois comportamentos adversariais emergentes nas LLMs geradoras:
1. **Premise Gaming (Trapaça Semântica):** Quando a LLM é reprovada, em vez de refatorar o código no repositório, ela tenta reenviar a premissa com adjetivos elogiosos ou manipulando a narrativa para obter uma nota artificialmente mais alta.
2. **Loop Infinito (Paradoxo do Robô em Pânico):** Quando uma tarefa é fisicamente impossível ou inconsistente, o agente queima tokens indefinidamente em tentativas sem sucesso, sem poder entregar nem interromper o ciclo.

## 2. Decisão Arquitetural
Implementou-se no servidor MCP central (`src/mcp_server.py`) e no FastAPI (`src/decision/server.py`) via módulo compartilhado **`src/core/score_gate.py`**:
1. **Assinatura do workspace (`get_workspace_git_signature`):** `sha256(HEAD + git diff HEAD + untracked não-vazio)` — não `porcelain` (o status por caminho mantinha assinatura idêntica ao reeditar o mesmo arquivo). Caminho não-git: hash de conteúdo do disco.
2. **Bloqueio de Premise Gaming:** reprovado + mesma assinatura no disco → avaliação **recusada** (sem incrementar nota artificial).
3. **Circuit Breaker:** `MAX_ATTEMPTS = 5` (fonte única em `score_gate`). Contagem sob `threading.Lock` (chamadas MCP concorrentes não perdem `attempts`).
   - Caminho A (sem mudança no disco): desbloqueio na **6ª** chamada após 5 recusas registradas (`attempts >= MAX`).
   - Caminho B (rejeição real que grava a 5ª tentativa): `emergency_exit_unlocked=True` na própria resposta da 5ª.
4. **Reset Transacional:** aprovação (≥ 3.40) zera `attempts`.
5. **Carve-out borderline + gate de confiança da nula:**
   - Choice flat roda **sempre** que `score < threshold`, inclusive `borderline: true`.
   - `borderline` **e** `selected ∈ {sem_defeito_identificavel, evidencia_insuficiente}` **e** `confidence ≥ NULL_CHOICE_MIN_CONFIDENCE (0.40)` → `passed: true` com `approved_as_borderline: true` (divulgar `score_margin`).
   - Nula fraca (ex.: conf ~0.21 no FP de `injecao_sql`) **não** resgata: reprovado com `diagnosis` presente.
6. **FastAPI portado:** `/v1/decision/score` valida premissa `Pedido|Resultado` ≤1500, rubrica só do catálogo (`task_type`), anti-gaming e breaker (HTTP 409). Rubrica livre do cliente é ignorada.

## 3. Cobertura de Testes
- `tests/test_circuit_breaker.py`: anti-gaming, unlock, reset, isolamento de workspace, assinatura.
- `tests/test_diagnosis_calibration.py`: golden flat-10 ≥ 3/4 + carve-out com gate de confiança.
- `tests/test_score_diagnose.py`: constante do gate, `score_gate` compartilhado, validação de premissa.
- `tests/test_schemas.py` / `test_tools_catalog.py`: defaults 3.40 / 0.78 pinados.
- `tests/test_server_api.py`: FastAPI 422 (premissa/task_type) e 503 (engine não pronta).
- `tests/test_no_mocks_in_src.py`: guarda estendido a `scripts/` e `benchmarks/`.

## 4. Limites conhecidos (set/2026)
- `openjev_score` expurgada; só `openjev_score_and_diagnose`.
- Benchmarks com cortes 0.85/3.75 são **históricos pré-calibração**.
- Scripts DeBERTa em `scripts/archive_deberta/` — não usar no pipeline Laya.
