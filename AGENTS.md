# AGENTS.md — Diretrizes Locais do Projeto JEV (Core Engine & Ferramentas)

Este documento estabelece as diretrizes específicas para o desenvolvimento, testes e manutenção do código-fonte do próprio motor JEV.

> **Governança Global:** As regras mandatórias da Arquitetura Tri-Camada (Needle 3, Open Jev Choice/Noul/Score com corte $\ge 3.75$) estão ativas globalmente no System Prompt via `~/.gemini/config/AGENTS.md`.

---

## 1. Arquitetura do Repositório

O projeto JEV é composto pelos seguintes componentes centrais sob `src/`:
* **`src/decision/`:** Motor discriminativo local (Laya / Open JEV RLCD) para as primitivas tipadas (`choice`, `noul`, `score`).
  * `engine.py`: Wrapper de inferência primário (Laya na GPU GTX 1050 Ti com fallback seguro para CPU). O modelo DeBERTa-v3 foi removido do pipeline em set/2026.
  * `laya_adapter.py`: Adaptador modular para o modelo Laya com mapeamento ordinal 0–4 → 1–5 e thresholds calibrados (noul 0.78, score 3.60).
  * `hardware.py`: Profilagem de hardware e travamento contra `bfloat16` na arquitetura Pascal GP107 (GTX 1050 Ti).
* **`src/memory/`:** Motor de busca relacional de AST (Cactus Needle 3).
  * `local_needle.py`: Extração cirúrgica de código com limitação estrita de 500 a 1.500 tokens.
* **`src/mcp_server.py`:** Servidor MCP (Model Context Protocol) que expõe as **5 ferramentas universais**:
  * `needle3_search` — busca cirúrgica relacional de código (AST).
  * `openjev_choice` — roteamento de subagentes por distribuição de probabilidade.
  * `openjev_noul` — guardrail booleano pré-execução (threshold ≥ 0.78).
  * `openjev_score` — avaliação ordinal de conformidade (corte ≥ 3.60).
  * `openjev_score_and_diagnose` — **tool preferencial para validação final**: executa Score e, se reprovado, dispara Choice L1 (5 macro-categorias ortogonais de defeito) seguido de Choice L2 (4 micro-diagnósticos da categoria eleita), retornando feedback estruturado para auto-correção precisa.
* **`src/core/schemas.py`:** Contratos Pydantic estritos incluindo `DiagnosisResult` e `ScoreDiagnoseResponse` para a árvore de diagnóstico.

---

## 2. Taxonomia de Diagnóstico (openjev_score_and_diagnose)

Quando `score < threshold`, o motor executa um **choice único** sobre **26 opções flat** (sem hierarquia L1/L2):

**Falhas funcionais/lógicas:** `erro_off_by_one` | `regra_de_negocio_incompleta` | `estado_mutavel_inconsistente` | `inversao_de_predicado`

**Robustez e limites:** `falta_tratamento_nulo_ou_vazio` | `estouro_de_limites_numericos` | `falha_de_tratamento_de_excecoes` | `condicao_de_corrida_deadlock`

**Integridade e segurança:** `injecao_e_sanitizacao` | `vazamento_de_recursos` | `credenciais_hardcoded` | `autorizacao_e_autenticacao_frouxa`

**Qualidade de testes:** `ausencia_de_testes_unitarios` | `teste_tautologico_ou_falso_positivo` | `falta_cobertura_caminhos_de_erro` | `testes_nao_reprodutiveis_flaky`

**Contrato e acoplamento:** `dependencia_ou_import_inexistente` | `incompatibilidade_de_assinatura` | `violacao_de_schema_de_dados` | `quebra_de_retrocompatibilidade`

**Ambiente, performance e escopo:** `incompatibilidade_de_ambiente_ou_runtime` | `gargalo_de_performance_ou_complexidade` | `alucinacao_de_efeitos_colaterais_externos` | `desvio_de_escopo_ou_overengineering` | `inconsistencia_estilistica_ou_linting_estrito` | `stub_ou_mock_em_producao`

Ver `DEFECT_OPTIONS` e `DEFECT_DESCRIPTIONS` em `src/core/schemas.py` para o mapeamento completo.

---

## 3. Padrões de Hardware e Engenharia

1. **Hardware Alvo (GTX 1050 Ti - 4GB VRAM):**
   * Suporte estrito a `float16` e `float32`.
   * **Proibição de bfloat16:** GPUs Pascal (sm_61) não suportam `bfloat16`. O hardware guardrail deve coagir automaticamente para `float16` ou `float32`.
   * **Resiliência:** Em caso de `OutOfMemoryError` ou indisponibilidade de driver, o motor executa fallback imediato para CPU.
2. **Ambiente Virtual Python:**
   * Execução restrita ao ambiente em `.venv/bin/python` e `.venv/bin/pytest tests/`.
3. **Política Fail-Fast:**
   * Proibido simular decisões ou criar mocks em tempo de execução; o pipeline opera estritamente contra os modelos locais.

   * **Proibição de bfloat16:** GPUs Pascal (sm_61) não suportam `bfloat16`. O hardware guardrail deve coagir automaticamente para `float16` ou `float32`.
   * **Resiliência:** Em caso de `OutOfMemoryError` ou indisponibilidade de driver, o motor executa fallback imediato para CPU.
2. **Ambiente Virtual Python:**
   * Execução restrita ao ambiente em `.venv/bin/python` e `.venv/bin/pytest tests/`.
3. **Política Fail-Fast:**
   * Proibido simular decisões ou criar mocks em tempo de execução; o pipeline opera estritamente contra os modelos locais.
