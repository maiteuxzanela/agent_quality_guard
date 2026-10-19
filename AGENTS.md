# AGENTS.md — Diretrizes Locais do Projeto JEV (Core Engine & Ferramentas)

Este documento estabelece as diretrizes específicas para o desenvolvimento, testes e manutenção do código-fonte do próprio motor JEV.

> **Governança Global:** A validação de entrega do ecossistema JEV opera sob o modelo determinístico por ecossistema real (testes reais da stack sem mocks, tipagem e anti-mock) e auditoria da Diana. A ferramenta `needle_verify_delivery` é o pipeline de verificação de uso exclusivo da Diana quando a stack exigir. A ferramenta antiga `openjev_score_and_diagnose` foi expurgada de todo o repositório e ecossistema.

---

## 1. Arquitetura do Repositório

O projeto JEV é composto pelos seguintes componentes centrais sob `src/`:
* **`src/decision/`:** Camada determinística de hardware e ferramentas de decisão do JEV (sem motor neural local).
  * `hardware.py`: Profilagem de hardware e travamento contra `bfloat16` na arquitetura Pascal GP107 (GTX 1050 Ti).
  * O motor Laya/RLCD (`engine.py`, `laya_adapter.py`) e as primitivas `choice`/`noul`/`consulta` foram removidos do ecossistema (TICKET-HARNESS-47).
* **`src/memory/`:** Motor de busca relacional de AST e análise de código (Cactus Needle 3).
  * `needle_ast_guard.py`: Camada 1 determinística (Fail-Fast) — varredura de mocks em produção, SQL Injection, `eval`, segredos hardcoded.
  * `needle_defect_screener.py`: Camada 2 estrutural — alertas de tipagem, docstrings e recursos.
* **`src/orchestrator/`:** Orquestração e gerenciamento de execução.
  * `pytest_chunk_runner.py`: Execução sequencial de testes reais em 3-4 chunks com isolamento de testes pesados (ML/Hardware).
* **`src/mcp_server.py`:** Servidor MCP `cirurgic-search-mcp` (Model Context Protocol) que expõe as ferramentas universais:
  * `cirurgic_search` — busca cirúrgica híbrida de código (AST + Ripgrep + RRF). **Nome canônico**; o alias legado `qmd_search` permanece registrado e resolve para a MESMA implementação (retrocompatibilidade total).
  * `agent_quality_guard` — **tool canônica de homologação de entrega de código de uso exclusivo da Diana**: validação determinística (1º Pytest em Chunks Sequenciais $\rightarrow$ 2º NeedleASTGuard $\rightarrow$ 3º toolchain/AST). O alias legado `needle_verify_delivery` permanece registrado com a MESMA implementação.
  * **Nota:** o catálogo core de definições para LLM em `src/core/tools.py` mantém a entrada `needle3_search`, que **não** é registrada no app MCP de `src/mcp_server.py`.

> **🚨 REGRA INEGOCIÁVEL DE COMMIT:** O `git status` NÃO PODE ser limpo antes da aprovação completa da entrega técnica. O agente está terminantemente proibido de realizar `git commit` antes da homologação final dos testes e aprovação formal da Diana.

---

## 2. Pipeline Determinístico de Homologação (`needle_verify_delivery`)

A validação de entrega é **100% determinística** (testes reais + AST Guard + toolchain/AST), sem notas probabilísticas, sem motor neural local e sem *premise gaming*. A homologação é executada em 3 etapas sequenciais (Fail-Fast):

1. **Etapa 0: Execução Sequencial de Testes Reais (`pytest`) em Chunks**
   - Os testes de `tests/` são particionados em 3 a 4 chunks sequenciais para evitar estressar a CPU e a GPU GTX 1050 Ti.
   - O chunk mais pesado (testes de Hardware/IPC) roda **isolado (solo)**.
   - **Rejeição Estrita de Workspace sem Testes:** Se o workspace não contiver arquivos de teste reais em `tests/`, o pipeline rejeita sumariamente a entrega com instrução explícita de criação de teste. **O agente é estritamente obrigado a produzir pelo menos um teste real em `tests/test_<modulo>.py`** antes de tentar nova validação. É proibido tentar burlar esse bloqueio.
   - Se qualquer teste falhar, o pipeline é abortado imediatamente (`passed: false`, `status: "REJECTED"`).
2. **Etapa 1: NeedleASTGuard (Camada 1 — Bloqueios Determinísticos)**
   - Bloqueia violações graves e inegociáveis de segurança e integridade (mocks em arquivos de produção sob `src/`, segredos hardcoded, `eval`/`exec`, SQL Injection, `verify=False`).
   - Violações nesta camada causam rejeição imediata (`passed: false`, `status: "REJECTED"`).
3. **Etapa 2: NeedleASTScreener (Camada 2 — Qualidade Estrutural)**
   - Inspeciona o repositório em busca de alertas de higiene (tipagem ausente, docstrings ausentes, exceções engolidas), ignorando dunders (`__init__`), stubs (`.pyi`), decoradores `@property` e arquivos de teste.
4. **Circuit Breaker Universal (Limite de 5 Tentativas)**
   - Se o repositório atingir a **5ª tentativa** de validação sem aprovação limpa, o motor destrava automaticamente a **Saída de Emergência (Aprovação com Ressalva - `approved_with_caveat: true`, `status: "EMERGENCY_RELEASE"`)**.

---

## 3. Padrões de Hardware e Engenharia

1. **Hardware Alvo (GTX 1050 Ti - 4GB VRAM):**
   * Suporte estrito a `float16` e `float32`.
   * **Proibição de bfloat16:** GPUs Pascal (sm_61) não suportam `bfloat16`.
   * **Resiliência:** Fallback automático para CPU caso ocorra indisponibilidade de VRAM.
2. **Ambiente Virtual Python:**
   * Execução restrita ao ambiente em `.venv/bin/python` e `.venv/bin/pytest`.
3. **Política Fail-Fast:**
   * Proibido simular decisões ou criar mocks em tempo de execução; o pipeline opera estritamente contra as dependências reais.

---

## 4. Comandos Determinísticos de Teste & Execução

A validação de código e qualidade no ecossistema JEV opera sob os seguintes comandos determinísticos reais:
* **Runner em Chunks Sequenciais (Pytest isolado):**
  ```bash
  .venv/bin/python src/orchestrator/pytest_chunk_runner.py
  ```
* **Suíte Geral de Testes (Pytest da Stack):**
  ```bash
  .venv/bin/pytest tests/
  ```
  *(Os testes mais pesados de Hardware/IPC devem rodar isolados).*
* **Anti-Mock / Camada AST Guard:**
  ```bash
  python3 -m pytest tests/unit/
  ```

---

## 5. Restrições de Domínio & Invariantes Inegociáveis

1. **Veto a bfloat16 na GTX 1050 Ti:** Arquitetura Pascal GP107 suporta estritamente float16/float32. Chamadas com bfloat16 causam panic/crash de hardware.
2. **Rejeição Imediata de Workspace sem Testes:** É terminantemente proibido workspace sem arquivos de teste reais em `tests/` (mínimo `tests/test_<modulo>.py`).
3. **Circuit Breaker Universal (Limite de 5 Tentativas):** Na 5ª tentativa consecutiva sem aprovação limpa, o motor aciona a Saída de Emergência (`EMERGENCY_RELEASE`).
4. **Links e Comunicação:** Referências a arquivos e endpoints devem sempre utilizar links absolutos em markdown `[nome](file:///caminho/absoluto)`. Honestidade radical sobre métricas, tempos e fronteiras de modelo.
5. **Expansão Orgânica do Time:** Necessidades de novas especialidades devem ser formalizadas via persona em `personas/<nome>.md`.

---

## 6. Orquestração e Matriz da Equipe de Especialistas

> **Orquestrador Global Universal:** [Dante — Lead Runtime & Systems Architect](file:///mnt/94CCB337CCB3130A/opencode_chambers_harness/personas/00_dante_runtime_architect.md)  
> **Fundadora & Engenheira Líder:** [Maiteux Zanela](file:///home/maiteuxzanela/projeto%20JEV/AGENTS.md)  
> **Diretório de Personas Especialistas:** [personas/README.md](file:///home/maiteuxzanela/projeto%20JEV/personas/README.md)

A orquestração técnica opera de forma unificada sob **Dante** via **Ensemble Teams** (`ensemble-room-lowcost`, `team_spawn`), mantendo o workspace compartilhado (`worktree: false`) e comunicação direta entre especialistas.

| Subagente | Documento de Persona | Domínio Técnico | Quando Acionar |
| :--- | :--- | :--- | :--- |
| **@Kael** | [`01_kael_ml_hardware.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/01_kael_ml_hardware.md) | Hardware Linux, CUDA & Pascal | Profilagem de VRAM, drivers CUDA, contenção de processos GPU e fallback CPU. |
| **@Maya** | [`02_maya_ast_memory.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/02_maya_ast_memory.md) | Memória Relacional & AST (Needle 3) | Parsing de AST Python, busca relacional e refinamento do `NeedleASTGuard`/`Screener`. |
| **@Túlio** | [`03_tulio_mcp_api.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/03_tulio_mcp_api.md) | Protocolos MCP, APIs & Schemas | Servidor MCP universal, schemas Pydantic e ferramenta `needle_verify_delivery`. |
| **@Clara** | [`04_clara_qa_governance.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/04_clara_qa_governance.md) | Quality Gate, Pytest & Anti-Mock | **Guardiã da Qualidade**. Suíte de testes reais em `tests/`, runner em chunks e políticas anti-mock. |
| **@Otávio** | [`05_otavio_audit_bridge.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/05_otavio_audit_bridge.md) | Auditorias Multi-Repo & Bridges | Scripts de auditoria multi-repositório e integração com a biblioteca `jev-client-bridge`. |

---

## 7. Relatório Resumido de Validação de Entrega (`needle_verify_delivery`)

Toda entrega técnica concluída deve reportar o resultado da homologação via:
* **Status:** Aprovado (`APPROVED`) OU Rejeitado (`REJECTED`) OU Saída de Emergência (`EMERGENCY_RELEASE`)
* **Tentativa:** N / 5
* **Pytest Chunks:** Total de chunks executados e status de aprovação de cada chunk
* **Violações AST:** Quantidade de violações bloqueantes (Camada 1) e alertas estruturais (Camada 2)

---

## Regras de Orquestração, Contenção de Tokens, Quality Gate e Hardware

> Movidas para evitar duplicação em cada repositório: orquestração, contenção de tokens (sem polling, lead silencioso), Quality Gate in_review e desacoplamento da Diana estão no bloco `LEAD_ORCHESTRATION` do agente Dante; hardware/modelos/custo estão em `~/.config/opencode/AGENTS.md` §7.

## 8. Arquitetura do Pool Global Único de Especialistas sob Orquestrador Universal Dante

*(Cláusula global mandatória — `~/.config/opencode/AGENTS.md` §8)*

1. **Orquestrador Global Universal:** **Dante (Lead Runtime & Systems Architect)** é o orquestrador geral do ecossistema. Dante atua exclusivamente com leitura, diagnóstico, planejamento e delegação via Ensemble Teams (`team_spawn`), carregando dinamicamente as regras locais de `<workspace>/AGENTS.md`.
2. **Pool Global Único de Especialistas:** Cris (unificada web/back-office), Sam, Lumi, Carla, Ariel, Rowan, Kael, Maya, Túlio, Silas, Bia, Juliana, Ana, Iara, Morgan-sec, Letícia, Clara, Otávio, Noa, Érica, Ellis, Beatriz, Camila, Daniela, Fernanda, Gabriela, Isabela — convocáveis por Dante via `team_spawn` para executar implementações, testes e inspeções dentro do repositório alvo.
