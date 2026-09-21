# AGENTS.md — Diretrizes Locais do Projeto JEV (Core Engine & Ferramentas)

Este documento estabelece as diretrizes específicas para o desenvolvimento, testes e manutenção do código-fonte do próprio motor JEV.

> **Governança Global:** As regras mandatórias da Arquitetura Tri-Camada (Needle 3, Open Jev Choice/Noul/Score com corte $\ge 3.75$) estão ativas globalmente no System Prompt via `~/.gemini/config/AGENTS.md`.

---

## 1. Arquitetura do Repositório

O projeto JEV é composto pelos seguintes componentes centrais sob `src/`:
* **`src/decision/`:** Motor discriminativo local (Open Jev / DeBERTa-v3) para as primitivas tipadas (`choice`, `noul`, `score`).
  * `engine.py`: Wrapper de inferência com failover automático para CPU e gerenciamento de VRAM.
  * `hardware.py`: Profilagem de hardware e travamento contra `bfloat16` na arquitetura Pascal GP107 (GTX 1050 Ti).
* **`src/memory/`:** Motor de busca relacional de AST (Cactus Needle 3).
  * `local_needle.py`: Extração cirúrgica de código com limitação estrita de 500 a 1.500 tokens.
* **`src/mcp_server.py`:** Servidor MCP (Model Context Protocol) que expõe as 4 ferramentas universais:
  * `needle3_search`, `openjev_choice`, `openjev_noul`, `openjev_score`.

---

## 2. Padrões de Hardware e Engenharia

1. **Hardware Alvo (GTX 1050 Ti - 4GB VRAM):**
   * Suporte estrito a `float16` e `float32`.
   * **Proibição de bfloat16:** GPUs Pascal (sm_61) não suportam `bfloat16`. O hardware guardrail deve coagir automaticamente para `float16` ou `float32`.
   * **Resiliência:** Em caso de `OutOfMemoryError` ou indisponibilidade de driver, o motor executa fallback imediato para CPU.
2. **Ambiente Virtual Python:**
   * Execução restrita ao ambiente em `.venv/bin/python` e `.venv/bin/pytest tests/`.
3. **Política Fail-Fast:**
   * Proibido simular decisões ou criar mocks em tempo de execução; o pipeline opera estritamente contra os modelos locais.
