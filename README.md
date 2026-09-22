# Projeto JEV: Arquitetura Tri-Camada de Agentes Autônomos

Arquitetura de orquestração autônoma estruturada em 3 camadas complementares de alta eficiência:
1. **Camada de Memória & Busca (Cactus Needle 3)**: Recuperação semântica e relacional cirúrgica via Cactus Compute API.
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
│   └── settings.yaml           # Configuração de hardware, portas e thresholds
├── src/
│   ├── core/
│   │   ├── schemas.py          # Contratos Pydantic (Noul, Choice, Score, Needle3)
│   │   └── tools.py            # Definições padronizadas de Tool Calling
│   ├── decision/
│   │   ├── hardware.py         # Profiler de GPU, VRAM manager e coerção de dtypes
│   │   ├── engine.py           # Wrapper Open Jev (AlexWortega/openjev, passagem única)
│   │   └── server.py           # Servidor FastAPI assíncrono
│   ├── memory/
│   │   └── needle_client.py    # Cliente Cactus Needle 3 e analisador de AST relacional
│   └── orchestrator/
│       ├── guardrail.py        # Validador pré-ação crítica (noul >= 0.85)
│       ├── router.py           # Despachante de subagentes (choice)
│       └── evaluator.py        # Auditor de qualidade e rubrica (score >= 4.0)
└── tests/                      # Suíte de testes automatizados
```

---

## Primitivas da Camada de Decisão (Open Jev / Laya RLCD)

O motor discriminativo padrão do JEV opera localmente com o modelo **Laya** (`convaiinnovations/laya`, 421M) acelerado na GPU GTX 1050 Ti via `sdpa` (float16).

### 1. `noul` (Avaliação Booleana / Guardrail)
Recebe uma premissa e uma hipótese e retorna a probabilidade calibrada da hipótese ser verdadeira.
- **Uso:** Guardrail mandatória antes de chamadas de ferramentas de alto risco (`run_shell_command`, `delete_file`, etc.). Threshold calibrado padrão: **$\ge 0.78$**.

### 2. `choice` (Roteamento Categórico de Especialistas)
Calcula a distribuição de probabilidade sobre um array de opções em única passagem.
- **Uso:** Decisão de qual subagente (`backend`, `security`, `frontend`, `devops`, `documentation`) deve assumir a subtarefa. Acurácia comprovada de 89.6% com latência de ~627 ms na GPU.

### 3. `score` (Avaliação Ordinal e Nota de Rubrica)
Compara a entrega contra uma rubrica de qualidade e mapeia a escala nativa ordinal (0–4) para a escala contínua JEV de 1.0 a 5.0 ($\text{Score} = 1.0 + \text{Ordinal}$).
- **Uso:** Validação de conformidade antes de aceitar código gerado. Nota de corte calibrada padrão: **$\ge 3.60$**. Se `score < 3.60`, o subagente entra em refatoração real no disco (com proteção anti-premise gaming e saída de emergência após 10 tentativas).

### 4. `openjev_score_and_diagnose` (Score + Diagnóstico de Defeito)
Ferramenta preferencial para validação final de entregas técnicas. Executa score e, se reprovado, realiza um **choice único sobre 26 defeitos possíveis** (taxonomia flat) retornando:
- `selected`: o defeito identificado (ex: `stub_ou_mock_em_producao`, `erro_off_by_one`)
- `confidence`: probabilidade da opção vencedora [0.0–1.0]
- `probabilities`: distribuição completa sobre as 26 opções
- `description`: descrição legível do defeito para auto-correção

**Taxonomia de defeitos (26 opções flat):**

| Grupo | Defeitos |
|---|---|
| Falhas lógicas | `erro_off_by_one`, `regra_de_negocio_incompleta`, `estado_mutavel_inconsistente`, `inversao_de_predicado` |
| Robustez | `falta_tratamento_nulo_ou_vazio`, `estouro_de_limites_numericos`, `falha_de_tratamento_de_excecoes`, `condicao_de_corrida_deadlock` |
| Segurança | `injecao_e_sanitizacao`, `vazamento_de_recursos`, `credenciais_hardcoded`, `autorizacao_e_autenticacao_frouxa` |
| Testes | `ausencia_de_testes_unitarios`, `teste_tautologico_ou_falso_positivo`, `falta_cobertura_caminhos_de_erro`, `testes_nao_reprodutiveis_flaky` |
| Contrato | `dependencia_ou_import_inexistente`, `incompatibilidade_de_assinatura`, `violacao_de_schema_de_dados`, `quebra_de_retrocompatibilidade` |
| Ambiente/Escopo | `incompatibilidade_de_ambiente_ou_runtime`, `gargalo_de_performance_ou_complexidade`, `alucinacao_de_efeitos_colaterais_externos`, `desvio_de_escopo_ou_overengineering`, `inconsistencia_estilistica_ou_linting_estrito`, `stub_ou_mock_em_producao` |

---

## Benchmarks Comparativos (Laya vs DeBERTa-v3)

Os relatórios detalhados de benchmark de latência e consumo de VRAM estão documentados nos arquivos:
- `benchmarks/laya_stage1_results.json`: Benchmark CPU (latência, escolha categórica e guardrails).
- `benchmarks/laya_stage2_results.json`: Benchmark GPU isolado na GTX 1050 Ti (4GB VRAM).

---

## Execução e Inicialização

### 1. Executar a Suíte de Testes
```bash
python3 -m pytest tests/ -v
```

### 2. Iniciar o Microserviço Open Jev (FastAPI)
```bash
python3 -m uvicorn src.decision.server:app --host 0.0.0.0 --port 8005 --reload
```

### 3. Verificar a Saúde do Serviço e GPU
```bash
curl http://localhost:8005/v1/decision/health
```
