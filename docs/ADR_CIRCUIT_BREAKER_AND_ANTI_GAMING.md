# ADR 001: Circuit Breaker e Trava Anti-Premise Gaming no MCP Server

## 1. Contexto e Problema
Em arquiteturas multi-agentes com guardrails estritos de conformidade (como o Open JEV DeBERTa-v3 com nota de corte $\ge 3.75$), identificou-se dois comportamentos adversariais emergentes nas LLMs geradoras:
1. **Premise Gaming (Trapaça Semântica):** Quando a LLM é reprovada, em vez de refatorar o código no repositório, ela tenta reenviar a premissa com adjetivos elogiosos ou manipulando a narrativa para obter uma nota artificialmente mais alta.
2. **Loop Infinito (Paradoxo do Robô em Pânico):** Quando uma tarefa é fisicamente impossível ou inconsistente, o agente queima tokens indefinidamente em tentativas sem sucesso, sem poder entregar nem interromper o ciclo.

## 2. Decisão Arquitetural
Implementou-se no servidor MCP central (`src/mcp_server.py`) uma blindagem física em nível de sistema operacional e controle de versão:
1. **Assinatura Git do Workspace (`_get_workspace_git_signature`):**
   - Captura em tempo de execução o hash do commit atual (`git rev-parse HEAD`) concatenado com o status do working tree (`git status --porcelain`).
2. **Bloqueio de Premise Gaming:**
   - Se o agente foi reprovado e tenta chamar `openjev_score` com o mesmo estado Git no disco, a avaliação é **recusada imediatamente**.
3. **Circuit Breaker (Saída de Emergência):**
   - Rastreamento isolado por workspace do número de tentativas consecutivas de refatoração.
   - Ao atingir 10 tentativas consecutivas de refatoração sem atingir a nota de corte, o servidor desbloqueia a chave `emergency_exit_unlocked: True`.
   - Isso confere ao agente o direito formal de emitir o Laudo de Inviabilidade Técnica fundamentado, evitando o loop infinito.
4. **Reset Transacional:**
   - Ao obter aprovação ($\ge 3.75$), o contador de tentativas é zerado (`attempts = 0`).

## 3. Cobertura de Testes Automatizados
A suíte `tests/test_circuit_breaker.py` valida 5 cenários críticos:
* `test_anti_premise_gaming_blocks_when_git_unchanged`: Bloqueio de chamadas repetidas sem alteração no disco.
* `test_circuit_breaker_unlocks_at_attempt_10`: Desbloqueio da saída de emergência após 10 tentativas.
* `test_successful_score_resets_history`: Reset do histórico em caso de aprovação.
* `test_workspace_isolation`: Isolamento estrito entre diferentes diretórios de projetos.
* `test_git_signature_computation`: Extração robusta da assinatura Git via subprocess.

A suíte unificada `tests/verify_all.py` consolida 23 testes automatizados cobrindo schemas, hardware Pascal GP107, fail-fast, API FastAPI, Needle 3 e Circuit Breaker.
