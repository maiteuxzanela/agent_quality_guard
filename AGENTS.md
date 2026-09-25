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
  * `openjev_score_and_diagnose` — **única tool de score (a tool `openjev_score` foi expurgada)**: executa Score (corte ≥ 3.40; premissa recusada se ausente de `Pedido`/`Resultado` ou > 1500 caracteres; rubrica centralizada estritamente em `task_type="codigo_producao"`; retorna `score_margin`/`borderline`/`rubric_source`). **O choice de diagnóstico foi removido (set/2026)** — o fluxo é: **AST Guardrail fail-fast** (bloco determinístico, `score=1.0`) → **Needle 3 Screener estrutural** sobre as **8 categorias reais** (`DEFECT_OPTIONS`) → **JEV Noul (P ≥ 0.78) em cada suspeita** → **Score Laya**. Decisão final: sem nenhuma suspeita ⇒ `passed=true` (nota < corte vira `approved_with_caveat=true`, divulgar `score_margin`); defeito confirmado pelo Noul ⇒ `passed=false` com categoria/arquivo/linhas/`candidate_defects`; suspeita refutada mas nota < corte ⇒ `passed=false` com `diagnosis` = suspeita de maior probabilidade para **INVESTIGAÇÃO** (mesmo com Noul negativo). Anti-premise gaming e circuit breaker universal (**5 tentativas**, `MAX_ATTEMPTS` — destrava a Saída de Emergência imediatamente na 5ª) valem neste caminho.
  * `openjev_consulta` — consulta direta (*"esse script funciona?"*) retornando **indício textual** (probability/confidence) **sem booleano de aprovação**; postura de indício obrigatória (premise gaming proibido).
* **`src/core/schemas.py`:** Contratos Pydantic estritos incluindo `DiagnosisResult`, `ScoreDiagnoseResponse`, `ConsultaRequest`/`ConsultaResponse` e o catálogo fechado de rubricas `RUBRIC_CATALOG` centralizado exclusivamente em `codigo_producao`.

> **🚨 REGRA INEGOCIÁVEL DE COMMIT:** O `git status` NÃO PODE ser limpo antes da aprovação completa da entrega técnica. O agente está terminantemente proibido de realizar `git commit` antes de obter a nota de corte $\ge 3.40$ ou o desbloqueio da Saída de Emergência. Caso o repositório já se encontre limpo, o motor inspeciona as alterações reais do último commit (`HEAD~1..HEAD`), mas comitar antecipadamente oculta o working tree e viola o protocolo.

---

## 2. Taxonomia de Diagnóstico e Borderline Mass Gate (openjev_score_and_diagnose)

> **Mudança de contrato (set/2026):** o **choice único flat de diagnóstico foi removido** do fluxo de score. Quem diagnostica agora é o **Needle 3 Screener** (hipóteses estruturais) arbitrado pelo **JEV Noul (P ≥ 0.78)**. As categorias nulas (`sem_defeito_identificavel`, `evidencia_insuficiente`) **não existem mais** — restam 8 categorias técnicas reais.

Fluxo em quatro camadas:
1. **AST Guardrail fail-fast:** violação determinística (mock em produção, SQL injection, `eval` perigoso, segredo hardcoded, ...) ⇒ `passed=false`, `score=1.0`, `diagnosis` com a categoria da regra e `description` `🚨 [Needle 3 AST Block] ...` (arquivo:linha). *Exceção de vocabulário mantida por decisão (set/2026):* a regra `mock_in_production` emite `mock_stub_em_producao`, categoria do próprio guard **fora** das 8 opções de `DEFECT_OPTIONS` — a mensagem do bloco AST carrega o `file:line` completo.
2. **Needle 3 Screener estrutural:** extrai os símbolos AST do diff e dispara triggers determinísticos (ex.: `open()` fora de try/with → `robustez_excecoes_recursos`; `# TODO:` → `requisito_incompleto_ou_ausente`; `while True` → `logica_ou_regra`; nested ifs ≥ 3 → `logica_ou_regra`; `os.getenv` sem default → `documentacao_configuracao`). Cada suspeita vai ao **Noul**:
   * **Confirmada (P ≥ 0.78)** ⇒ `passed=false` **sempre** (mesmo com score ≥ corte), com categoria, arquivo, linhas, `evidence` e `candidate_defects`.
   * **Refutada (P < 0.78)** ⇒ entra em `refuted_suspicions`.
3. **Decisão do Score Laya (corte ≥ 3.40):**
   * **Zero suspeitas** do Screener ⇒ `passed=true`; se `score < 3.40` vira **aprovação com ressalva** (`approved_with_caveat=true`) — divulgar `score_margin` e a `message`.
   * **Suspeitas presentes + `score < 3.40`** ⇒ `passed=false` e `diagnosis` aponta a suspeita de **maior probabilidade** para **INVESTIGAÇÃO** no disco — mesmo com Noul negativo.
   * **Suspeitas refutadas + `score ≥ 3.40`** ⇒ `passed=true`.
4. **Borderline Mass Gate:** no **caminho mono** (workspace sem símbolos AST — repositório limpo ou diff não-code), `borderline: true` com massa saudável ≥ 0.85 e falhas ≤ 0.12 ⇒ `approved_as_borderline: true`; senão `passed=false` com `diagnosis=None` e `message` indicando caminho mono. No caminho com símbolos, `approved_as_borderline` só acompanha a ressalva (`approved_with_caveat` **e** massa saudável).

> **Calibração obrigatória (ago/2026):** o head do Laya só discrimina com **≤ 10 opções** (bucket de temperature `6-10`). Listas ≥ 11 defeitos finos colapsam na opção-savor (precisão medida 1/4 no golden set); ≤ 5 opções concentradas mediram apenas 1–2/4. Essa calibração continua valendo para `openjev_choice` de roteamento e para ferramentas externas que ainda disparam `evaluate_choice` com as 8 categorias; não ampliar a lista sem revalidar `tests/test_diagnosis_calibration.py`.

**Categorias (8 opções flat — sem nulas):**

`logica_ou_regra` | `robustez_excecoes_recursos` | `seguranca` | `qualidade_de_testes` | `contrato_ambiente` | `performance_escopo_estilo` | `requisito_incompleto_ou_ausente` | `documentacao_configuracao`

Instructions + `DEFECT_DESCRIPTIONS` são textos curtos medidos para caber em `head_max_len=192` **sem truncar** (`scripts/measure_diag_choice_tokens.py`); não alongar sem re-medir.

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

O Projeto JEV opera com um roster calibrado de especialistas coordenados pelo Orquestrador Valério. O roteamento obrigatório está suspenso: Valério possui autonomia plena para desenvolver, planejar e refatorar diretamente, acionando os subagentes sob demanda ao longo do fluxo de trabalho quando o isolamento de contexto ou especialidade técnica pontual for conveniente via `openjev_spawn_subagent`.

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


