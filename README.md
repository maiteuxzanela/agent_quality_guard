# Projeto JEV: Arquitetura Tri-Camada de Agentes Autônomos

Arquitetura de orquestração autônoma estruturada em 3 camadas complementares de alta eficiência:
1. **Camada de Memória & Busca (Cactus Needle 3)**: Busca cirúrgica léxico-AST local (`LocalNeedleEngine`; cliente cloud Cactus é opcional/experimental).
2. **Camada de Decisão & Roteamento (Open Jev Local)**: Modelo discriminativo em passagem única (single forward pass), sem geração de texto livre, retornando probabilidades tipadas (`noul`, `choice`, `score`).
3. **Camada de Execução & Síntese (LLM Orquestradora + Subagentes)**: Raciocínio, geração de código e execução guiada por guardrails e roteamento probabilístico.

---

## Restrições de Hardware: NVIDIA GeForce GTX 1050 Ti (4GB VRAM, Pascal GP107)

A arquitetura Pascal possui particularidades determinantes para a estabilidade da inferência:
- **Proibição Estrita de `bfloat16`**: Pascal (compute capability 6.1) **NÃO possui suporte de hardware para `bfloat16`**. O módulo `hardware.py` intercepta e coage qualquer solicitação de bfloat16 para `float16` ou `float32`.
- **Gestão de VRAM na 1050 Ti (4096 MiB)**: Para conviver com o display manager do Linux Pop!_OS sem disparar CUDA Out-of-Memory (OOM):
  * Teto de alocação de VRAM na GPU fixado em **3000 MiB**.
  * Janela de contexto compacta limitada em **1024 tokens**.
  * Suporte a fallback seguro para CPU (`device="cpu"`, float32) caso a VRAM seja disputada.
- **Política Estrita sem Mocks**: Em caso de indisponibilidade da GPU ou dos pesos do modelo, a engine lança exceções explícitas (`OpenJevNotReadyError`, `HardwareUnavailableError`) e retorna HTTP 503, impedindo execuções cegas.

---

## Estrutura do Repositório

```text
projeto JEV/
├── AGENTS.md                   # Governança e regras mandatórias do orquestrador
├── README.md                   # Este guia
├── pyproject.toml              # Metadados e configuração do pytest
├── requirements.txt            # Dependências
├── configs/
│   └── settings.yaml           # Manifesto de referência (não é loader runtime)
├── src/
│   ├── core/
│   │   ├── schemas.py          # Contratos Pydantic + RUBRIC_CATALOG + taxonomia
│   │   ├── score_gate.py       # Premissa, rubrica fechada, histórico anti-gaming (MCP+FastAPI)
│   │   └── tools.py            # Tool Calling (nomes canônicos = MCP)
│   ├── decision/
│   │   ├── hardware.py         # Profiler de GPU, VRAM manager e coerção de dtypes
│   │   ├── laya_adapter.py     # Adaptador Laya (thresholds 0.78/3.40, score_margin)
│   │   ├── engine.py           # Wrapper Open Jev / Laya (passagem única)
│   │   └── server.py           # FastAPI (score com gate portado)
│   ├── memory/
│   │   ├── local_needle.py     # Needle 3 local (AST) — pipeline padrão
│   │   └── needle_client.py    # Cliente cloud opcional
│   └── orchestrator/
│       ├── guardrail.py        # Validador opcional pré-ação (noul >= 0.78)
│       ├── router.py           # Despachante de subagentes (choice)
│       └── evaluator.py        # Auditor de qualidade e rubrica (score >= 3.40)
├── scripts/
│   ├── archive_deberta/        # HISTORICO — audits era DeBERTa (não usar)
│   ├── audit_mocks.py          # Auditoria de mocks (cascata Laya 0.78/3.40)
│   └── verify_laya_gpu_engine.py # Verificação ponta-a-ponta da engine Laya na GPU
└── tests/                      # Suíte de testes automatizados
```

---

## Primitivas da Camada de Decisão (Open Jev / Laya RLCD)

O motor discriminativo padrão do JEV opera localmente com o modelo **Laya** (`convaiinnovations/laya`, 421M) acelerado na GPU GTX 1050 Ti via `sdpa` (float16).

### 1. `noul` (Avaliação Booleana — uso opcional)
Recebe uma premissa e uma hipótese e retorna a probabilidade calibrada da hipótese ser verdadeira.
- **Uso:** Avaliação booleana **opcional** — a exigência de conferência pré-ação foi removida (set/2026) porque o agente praticava premise gaming (reformular a premissa até `passed=true`) em vez de mudar de atitude ou alertar perigo. Threshold de referência ao usar: **$\ge 0.78$**. Resultado desfavorável ⇒ mudar de atitude e alertar a usuária, nunca contornar a avaliação.

### 2. `choice` (Roteamento Categórico de Especialistas)
Calcula a distribuição de probabilidade sobre um array de opções em única passagem.
- **Uso:** Decisão de qual subagente (`backend`, `security`, `frontend`, `devops`, `documentation`) deve assumir a subtarefa. Acurácia comprovada de 89.6% com latência de ~627 ms na GPU.

### 3. `openjev_score_and_diagnose` (Score + Diagnóstico sem choice)
**Única tool de score do pipeline MCP** (a tool `openjev_score` foi expurgada em set/2026). Compara a entrega contra a rubrica de qualidade e mapeia a escala nativa ordinal (0–4) para a escala contínua JEV de 1.0 a 5.0 ($\text{Score} = 1.0 + \text{Ordinal}$). O diagnóstico de defeito **não usa mais choice flat** (removido em set/2026): quem diagnostica é o **Needle 3 Screener estrutural** (gatilhos determinísticos sobre as **8 categorias reais** de `DEFECT_OPTIONS`, sem categorias nulas) arbitrado pelo **JEV Noul (P ≥ 0.78)**.

**Pipeline em 4 camadas:**
1. **AST Guardrail fail-fast:** violação determinística (SQL injection, `eval`, mock em produção, segredo hardcoded, ...) ⇒ `passed=false` com `score=1.0` e `description` `🚨 [Needle 3 AST Block] ...` — sem inferência. *Exceção (mantida por decisão):* a regra `mock_in_production` usa a categoria `mock_stub_em_producao`, fora das 8 de `DEFECT_OPTIONS`.
2. **Needle 3 Screener:** extrai os símbolos AST do diff e dispara triggers (`open()` fora de try/with, `# TODO:`, `while True`, `os.getenv` sem default, ifs aninhados ≥ 3, ...). Cada suspeita vai ao Noul: **confirmada (P ≥ 0.78)** ⇒ reprovação com categoria/arquivo/linhas; **refutada** ⇒ `refuted_suspicions`.
3. **Score Laya (corte ≥ 3.40) + decisão:** zero suspeitas ⇒ `passed=true` (nota < corte vira `approved_with_caveat=true` com `message` e `score_margin`); suspeita presente + nota < corte ⇒ `passed=false` com `diagnosis` = suspeita de maior probabilidade para **INVESTIGAÇÃO** no disco, mesmo com Noul negativo; nota ≥ corte + suspeitas refutadas ⇒ `passed=true`.
4. **Caminho mono** (workspace sem símbolos AST): score ≥ corte aprova; `borderline` + Mass Gate saudável (≥ 0.85 / ≤ 0.12) aprova como `approved_as_borderline`; senão reprovado com `diagnosis=None` e `message` do caminho mono.

**Contrato de score:**
- **Uso:** Validação de conformidade antes de aceitar código gerado. Nota de corte calibrada padrão: **$\ge 3.40$**. Se `score < 3.40`, o subagente entra em refatoração real no disco (proteção anti-premise gaming e circuit breaker universal de **5 tentativas** (`MAX_ATTEMPTS`) — a Saída de Emergência é destravada imediatamente na 5ª).
- **Premissa:** padrão factual obrigatório `Pedido do Prompt: ... | Resultado Executado: ...`, **máximo 1500 caracteres** — o MCP recusa (não trunca) premissas fora do padrão ou acima do teto.
- **Rubrica:** resolvida exclusivamente do catálogo fechado `RUBRIC_CATALOG` (`src/core/schemas.py`) pelo parâmetro `task_type` (chave única `codigo_producao`; chaves legadas `entrega`/`auditoria` mapeiam para ela); o campo `rubric` livre do cliente é **ignorado** (`rubric_source: catalog:<task_type>`).
- **Incerteza:** a resposta inclui `score_margin` (massa probabilística acima − abaixo do threshold) e `borderline` (≥35% de cada lado da fronteira). Se `borderline: true`, a margem deve ser mencionada na resposta final (AGENTS.md §4.7).
- **Observabilidade de device:** `device` e `dtype` na `ScoreResponse` expõem se a nota veio de fp16-GPU ou fp32-CPU (migração em OOM desloca o numérico perto do corte).
- **Regressão de notas:** `tests/test_score_calibration.py` mantém golden set de casos com resposta conhecida (positivos ≥ 3.40 e negativos semânticos < 3.40) contra o motor Laya real.

**Contrato de diagnóstico** (origem: Screener + Noul, ou AST Guardrail):
- `diagnosis.selected`: a categoria identificada (ex: `seguranca`, `robustez_excecoes_recursos`)
- `diagnosis.confidence`: probabilidade do Noul (confirmada/refutada) ou `1.0` no bloco AST
- `diagnosis.probabilities`: distribuição da categoria (uma entrada — o choice sumiu)
- `diagnosis.description`: descrição legível da categoria, ou o bloco `🚨 [Needle 3 AST Block] ...`
- `diagnosis.candidate_defects`: folhas candidatas (as de `DEFECT_MEMBERS`) para guiar a refatoração
- `refuted_suspicions`: suspeitas estruturais refutadas pelo Noul (P < 0.78) que ainda assim exigem investigação quando a nota < corte

**Taxonomia de defeitos (8 categorias flat — sem nulas):**

| # | Categoria | Folhas candidatas (`candidate_defects`) |
|---|---|---|
| 1 | `logica_ou_regra` | `erro_off_by_one`, `regra_de_negocio_incompleta`, `estado_mutavel_inconsistente`, `inversao_de_predicado` |
| 2 | `robustez_excecoes_recursos` | `falha_de_tratamento_de_excecoes`, `falta_tratamento_nulo_ou_vazio`, `estouro_de_limites_numericos`, `condicao_de_corrida_deadlock`, `vazamento_de_recursos` |
| 3 | `seguranca` | `injecao_e_sanitizacao`, `credenciais_hardcoded`, `autorizacao_e_autenticacao_frouxa` |
| 4 | `qualidade_de_testes` | `ausencia_de_testes_unitarios`, `teste_tautologico_ou_falso_positivo`, `falta_cobertura_caminhos_de_erro`, `testes_nao_reprodutiveis_flaky` |
| 5 | `contrato_ambiente` | `dependencia_ou_import_inexistente`, `incompatibilidade_de_assinatura`, `violacao_de_schema_de_dados`, `quebra_de_retrocompatibilidade`, `incompatibilidade_de_ambiente_ou_runtime` |
| 6 | `performance_escopo_estilo` | `gargalo_de_performance_ou_complexidade`, `alucinacao_de_efeitos_colaterais_externos`, `desvio_de_escopo_ou_overengineering`, `inconsistencia_estilistica_ou_linting_estrito` |
| 7 | `requisito_incompleto_ou_ausente` | `funcionalidade_do_pedido_ausente`, `requisito_do_prompt_ignorado`, `caso_de_borda_do_pedido_faltando` |
| 8 | `documentacao_configuracao` | `documentacao_ausente_ou_incorreta`, `configuracao_ou_governanca_incorreta` |

As categorias nulas `sem_defeito_identificavel` e `evidencia_insuficiente` foram **aposentadas** (set/2026): o universo de decisão é só técnico. Instruções e descrições são textos curtos medidos em `head_max_len=192` para **nenhum truncamento** — ver `scripts/measure_diag_choice_tokens.py` (ainda relevante para `openjev_choice` de roteamento e scripts de auditoria externos).

### 4. `openjev_consulta` (Consulta Direta — Indício Textual)
Para perguntas diretas do tipo *"esse script funciona?"* ou *"esse banco de dados tem essa informação?"*. Recebe a evidência real (`premise`: trecho de código, schema, amostra de dados) e a `question`, retornando:
- `indicio`: texto derivado da probabilidade (*"Indício forte a favor..."*, *"Indício fraco contra..."*)
- `probability` e `confidence`: valores calibrados do Laya
- **Sem campo `passed`** (contrato `ConsultaResponse` com `extra='forbid'`) — a resposta é **indício para investigação, não veredito**.

**Postura obrigatória (AGENTS.md):** proibido reformular a premissa para inverter o indício (premise gaming); indício desfavorável ⇒ investigar, consertar e alertar a usuária.

---

## Auditoria de Mocks (`scripts/audit_mocks.py`)

Script genérico que aplica o mesmo molde dos audits arquiteturais aos **4 projetos já auditados** (Faturamento, JEZ Collections, Predictera, PanelaAndroid), com calibração vigente **Laya (noul 0.78 / score 3.40)**:

1. **Scrub determinístico léxico** — varre arquivos de código (excluindo `node_modules`, `.venv`, etc.) por 10 padrões (`unittest.mock`, `MagicMock`, `@patch`, `jest.mock`, `sinon`, `__mocks__/`, ...) e registra `file:line`. Qualquer ocorrência conta igual (sem distinguir teste de produção na severidade — `em_testes`/`em_producao` é só metadata).
2. **Needle 3** — trechos cirúrgicos de mocks via `LocalNeedleEngine` (`max_tokens=1200`).
3. **Cascata JEV** — `choice` (`sem_mocks` | `apenas_em_testes` | `mocks_em_producao`) → `noul` (0.78) → `score` (3.40, rubrica `codigo_producao` do catálogo fechado) → se reprovado, o **próprio script** dispara diagnóstico via `evaluate_choice` direto sobre `DEFECT_OPTIONS` (8 categorias; a tool de score do MCP não tem mais choice de diagnóstico).
4. **Saída** — `mocks_audit_results.json` na raiz do projeto (ocorrências completas, contagens, notas, diagnóstico).

```bash
.venv/bin/python scripts/audit_mocks.py
```

Erros por alvo são capturados em `_safe_audit_target` e registrados no JSON (`campo error`) sem derrubar a auditoria dos demais projetos.

---

## Benchmarks Comparativos (Laya vs DeBERTa-v3)

> **HISTÓRICO pré-calibração:** os laudos abaixo usam cortes DeBERTa (noul 0.85 / score 3.75) e **não** refletem o pipeline Laya vigente (0.78 / 3.40).

Os relatórios detalhados de benchmark de latência e consumo de VRAM estão documentados nos arquivos:
- `benchmarks/laya_stage1_results.json`: Benchmark CPU (latência, escolha categórica e guardrails).
- `benchmarks/laya_stage2_results.json`: Benchmark GPU isolado na GTX 1050 Ti (4GB VRAM).

---

## Execução e Inicialização

### 1. Executar a Suíte de Testes
```bash
.venv/bin/pytest tests/ -v
```

### 2. Iniciar o Microserviço Open Jev (FastAPI)
```bash
python3 -m uvicorn src.decision.server:app --host 0.0.0.0 --port 8005 --reload
```

### 3. Verificar a Saúde do Serviço e GPU
```bash
curl http://localhost:8005/v1/decision/health
```
