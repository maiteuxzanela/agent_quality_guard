# AGENTS.md — Diretrizes Locais do Projeto JEV (Core Engine & Ferramentas)

Este documento estabelece as diretrizes específicas para o desenvolvimento, testes e manutenção do código-fonte do próprio motor JEV.

> **Governança Global:** As regras mandatórias da Arquitetura Tri-Camada (Needle 3, Open Jev Choice/Noul/Score com corte $\ge 3.40$) estão ativas globalmente no System Prompt via `~/.gemini/config/AGENTS.md` e `~/.config/opencode/AGENTS.md` (inclui o **Protocolo de Dúvida** §3 e o **Relatório Resumido de Validação** §4.7: Status, Tentativas e Notas).

---

## 1. Arquitetura do Repositório

O projeto JEV é composto pelos seguintes componentes centrais sob `src/`:
* **`src/decision/`:** Motor discriminativo local (Laya / Open JEV RLCD) para as primitivas tipadas (`choice`, `noul`, `score`).
  * `engine.py`: Wrapper de inferência primário (Laya na GPU GTX 1050 Ti com fallback seguro para CPU). O modelo DeBERTa-v3 foi removido do pipeline em set/2026.
  * `laya_adapter.py`: Adaptador modular para o modelo Laya com mapeamento ordinal 0–4 → 1–5 e thresholds calibrados (noul 0.78, score 3.40).
  * `hardware.py`: Profilagem de hardware e travamento contra `bfloat16` na arquitetura Pascal GP107 (GTX 1050 Ti).
* **`src/memory/`:** Motor de busca relacional de AST (Cactus Needle 3).
  * `local_needle.py`: Extração cirúrgica de código com limitação estrita de 500 a 1.500 tokens.
* **`src/mcp_server.py`:** Servidor MCP (Model Context Protocol) que expõe as **5 ferramentas universais**:
  * `needle3_search` — busca cirúrgica relacional de código (AST).
  * `openjev_choice` — roteamento de subagentes e seleção de ferramenta/busca por distribuição de probabilidade.
  * `openjev_noul` — avaliação booleana **opcional** (threshold ≥ 0.78; exigência pré-ação removida — anti premise gaming).
  * `openjev_score_and_diagnose` — **única tool de score (a tool `openjev_score` foi expurgada)**: executa Score (corte ≥ 3.40; premissa recusada se ausente de `Pedido`/`Resultado` ou > 1500 caracteres; rubrica prioritária unificada do catálogo fechado por `task_type="entrega"`; retorna `score_margin`/`borderline`/`rubric_source`). Se `borderline: true`: avalia o **Borderline Mass Gate** ($p_{\text{saudavel}} \ge 0.85$ e $p_{\text{falhas}} \le 0.12$). Preenchido o gate $\implies$ entrega **aprovada** com `approved_as_borderline: true` (sem viés léxico do choice). Se reprovado por falha real ou incerteza excessiva, dispara o **choice único flat sobre 10 categorias** (`DEFECT_OPTIONS`, critérios `DEFECT_DESCRIPTIONS`, instrução neutra), retornando `selected`, `confidence`, `probabilities`, `description` e `candidate_defects` (folhas de `DEFECT_MEMBERS`) para auto-correção. Anti-premise gaming e circuit breaker (**5 tentativas**, `MAX_ATTEMPTS`) valem neste único caminho.
  * `openjev_consulta` — consulta direta (*"esse script funciona?"*) retornando **indício textual** (probability/confidence) **sem booleano de aprovação**; postura de indício obrigatória (premise gaming proibido).
* **`src/core/schemas.py`:** Contratos Pydantic estritos incluindo `DiagnosisResult`, `ScoreDiagnoseResponse`, `ConsultaRequest`/`ConsultaResponse` e o catálogo fechado de rubricas `RUBRIC_CATALOG` (`entrega` unificada | `auditoria` | `codigo_producao`).

---

## 2. Taxonomia de Diagnóstico e Borderline Mass Gate (openjev_score_and_diagnose)

Quando `score < threshold`:
1. **Borderline Mass Gate:** Se `borderline: true` e a distribuição ordinal concentrar $p_{\text{acceptable}} + p_{\text{good quality}} + p_{\text{exceptional}} \ge 0.85$ com falhas $p_{\text{critical flaws}} + p_{\text{poor quality}} \le 0.12$, a entrega é aprovada na fronteira com `approved_as_borderline: true`.
2. **Choice Flat Diagnóstico:** Se reprovado fora da fronteira ou com falhas relevantes ($p_{\text{falhas}} > 0.12$), o motor executa um **choice único** sobre **10 categorias flat** (`DEFECT_OPTIONS`) com instrução neutra e critérios `DEFECT_DESCRIPTIONS`. A resposta traz `diagnosis` detalhado com `candidate_defects` para guiar a refatoração real no disco.

> **Calibração obrigatória (ago/2026):** o head do Laya só discrimina com **≤ 10 opções** (bucket de temperature `6-10`). Listas ≥ 11 defeitos finos colapsam na opção-savor (precisão medida 1/4 no golden set); ≤ 5 opções concentradas mediram apenas 1–2/4. Não ampliar a lista sem revalidar `tests/test_diagnosis_calibration.py`.

**Categorias (10 opções flat):**

`sem_defeito_identificavel` (1º) | `logica_ou_regra` | `robustez_excecoes_recursos` | `seguranca` | `qualidade_de_testes` | `evidencia_insuficiente` (6º) | `contrato_ambiente` | `performance_escopo_estilo` | `mock_stub_em_producao` | `documentacao_configuracao`

Ordem fixa: nulas em **1º e 6º**. Instructions + `DEFECT_DESCRIPTIONS` são textos curtos medidos para caber em `head_max_len=192` **sem truncar** o álibi das nulas nem as descrições (`scripts/measure_diag_choice_tokens.py`); não alongar sem re-medir.

Ver `DEFECT_OPTIONS`, `DEFECT_DESCRIPTIONS` e `DEFECT_MEMBERS` em `src/core/schemas.py` para o mapeamento completo (categorias → folhas).

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

---

## 4. Matriz da Equipe e Roteamento de Subagentes Especialistas

> **Liderança Técnica & Orquestração:** [Valério — Lead AI Architect & Orquestrador Geral](file:///home/maiteuxzanela/projeto%20JEV/personas/00_valerio_lead_architect.md)  
> **Fundadora & Engenheira Líder:** [Maiteux Zanela](file:///home/maiteuxzanela/projeto%20JEV/AGENTS.md)  
> **Diretório Oficial de Personas:** [personas/README.md](file:///home/maiteuxzanela/projeto%20JEV/personas/README.md)

O Projeto JEV opera com um roster calibrado de especialistas coordenados pelo Orquestrador Valério. O orquestrador opera estritamente sob os **Três Níveis de Execução** (Global AGENTS.md §3.1):

1. **Nível 1 (Auto-Execução Racional):** Dúvidas conceituais, arquitetura e edição pontual de texto/docs sem alteração de lógica de código.
2. **Nível 2 (Delegação Obrigatória via Hermes Subagent):** Implementações técnicas em `src/`, algoritmos, schemas, hardware ou testes DEVEM ser delegados. Valério **NÃO edita código de produção diretamente**. O fluxo mandatório é:
   - Eleição da persona: `openjev_choice(premise="...", options=["kael", "maya", "tulio", "clara", "otavio", "nova_persona_especialista"])`
   - Despacho em contexto isolado: se eleito um especialista, despachar via `openjev_spawn_subagent(persona=selected, task="...", workspace="/home/maiteuxzanela/projeto JEV")`. Se eleito `"nova_persona_especialista"`, transicionar imediatamente para o **Nível 3**.
3. **Nível 3 (Proposição de Nova Persona):** Se o choice eleger `"nova_persona_especialista"` ou a demanda técnica exigir especialidade ausente no roster atual, Valério propõe e cria a persona em `personas/<nome>.md`, registra no catálogo abaixo e despacha via subagente.

| Subagente | Documento de Persona | Domínio Técnico | Quando Acionar via `openjev_choice` |
| :--- | :--- | :--- | :--- |
| **@Kael** | [`01_kael_ml_hardware.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/01_kael_ml_hardware.md) | ML Local, PyTorch & Pascal | Inferência do modelo Laya, profilagem da GTX 1050 Ti, bloqueio de `bfloat16`, fallback CPU e benchmarks. |
| **@Maya** | [`02_maya_ast_memory.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/02_maya_ast_memory.md) | Memória Relacional & AST (Needle 3) | Parsing de AST Python, busca relacional, recorte de código (500–1.500 tok) e combate a context dumping. |
| **@Túlio** | [`03_tulio_mcp_api.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/03_tulio_mcp_api.md) | Protocolos MCP, APIs & Schemas | Servidor MCP universal, API FastAPI, schemas Pydantic, concorrência assíncrona (`asyncio.to_thread`) e SSE. |
| **@Clara** | [`04_clara_qa_governance.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/04_clara_qa_governance.md) | Quality Gate, Anti-Mock & Calibração | **Guardiã da Qualidade**. Testes reais em `tests/`, erradicação de mocks, Borderline Mass Gate e Golden Sets. |
| **@Otávio** | [`05_otavio_audit_bridge.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/05_otavio_audit_bridge.md) | Auditorias Multi-Repo & Bridges | Molds de auditoria (`audit_*.py`), biblioteca `jev-client-bridge`, stop hooks e validação de skills. |

---

## 5. Relatório Resumido de Validação de Entrega (Score & Diagnose)

Toda entrega técnica concluída nos repositórios do ecossistema deve reportar a validação final através de um bloco conciso e direto, contendo exclusivamente:
* **Status:** Aprovado ($\ge 3.40$) OU Reprovado OU Saída de Emergência
* **Tentativas:** N
* **Notas:** X.XX / 5.00 (em caso de reavaliações, relacionar as notas de cada tentativa: ex.: `3.15, 3.45`)


