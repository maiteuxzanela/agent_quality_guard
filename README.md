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

## Primitivas da Camada de Decisão (Open Jev)

### 1. `noul` (Avaliação Booleana / Guardrail)
Recebe uma premissa e uma hipótese e retorna a probabilidade calibrada da hipótese ser verdadeira.
- **Uso:** Guardrail mandatória antes de chamadas de ferramentas de alto risco (`run_shell_command`, `delete_file`, etc.).

### 2. `choice` (Roteamento Categórico de Especialistas)
Calcula a distribuição de probabilidade sobre um array de opções em única passagem.
- **Uso:** Decisão de qual subagente (`backend`, `security`, `frontend`, `devops`, `documentation`) deve assumir a subtarefa.

### 3. `score` (Avaliação Ordinal e Nota de Rubrica)
Compara a entrega contra uma rubrica de qualidade e interpola uma nota contínua de 1.0 a 5.0.
- **Uso:** Validação de conformidade antes de aceitar código gerado. Se `score < 4.0`, o subagente entra em refatoração.

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
