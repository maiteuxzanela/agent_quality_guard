# Máquinas de Estado e Ciclos de Vida — Core Open JEV

> **Documento Normativo:** `docs/STATE_MACHINES.md`  
> **Status:** Oficial & Ativo  
> **Governança:** Valério (Lead Architect) & Clara (QA & Governança)  
> **Última Atualização:** Setembro/2026

Este documento define formalmente o ciclo de vida, estados permitidos, transições válidas e proibições explícitas para as entidades transacionais críticas do Core Open JEV.

---

## 1. Ciclo de Vida da Entrega Técnica (`DeliveryLifecycle`)

A entidade `Delivery` governa a passagem de qualquer alteração de código ou funcionalidade pelo pipeline de homologação determinístico até a aprovação final.

### 1.1. Estados Permitidos

| Estado | Significado Semântico |
| :--- | :--- |
| `DRAFT` | Alterações em andamento na árvore de trabalho (`git status` sujo). Código sendo escrito ou refatorado. |
| `TESTING_CHUNKS` | Execução em lote dos testes reais particionados em 4 chunks sequenciais pelo `PytestChunkRunner`. |
| `AST_GUARD_SCAN` | Varredura estática de segurança da Camada 1 (`NeedleASTGuard` — anti-mock, SQLi, eval, segredos). |
| `AST_SCREENER_SCAN` | Triagem estrutural da Camada 2 (`NeedleASTScreener` — tipagem, docstring, async blocking, mutabilidade). |
| `NOUL_ARBITRATION` | Avaliação semântica atômica da Camada 3 (`JEV Noul` na GPU/CPU com threshold $P \ge 0.78$). |
| `APPROVED` | Entrega técnica aprovada com sucesso determinístico (100% dos chunks verdes, zero violações de Guard, sem defeitos confirmados). |
| `REJECTED` | Entrega rejeitada por falha de teste, violação de segurança ou defeito estrutural confirmado pelo Laya Noul. |
| `EMERGENCY_RELEASE` | Liberação emergencial com ressalva (`approved_with_caveat: true`) ativada pelo Circuit Breaker na 5ª tentativa. |

### 1.2. Diagrama de Transições Válidas

```text
       ┌──────────┐
       │  DRAFT   │◄────────────────────────────────┐
       └────┬─────┘                                 │
            │ (Submeter entrega)                    │
            ▼                                       │
  ┌──────────────────┐                              │
  │  TESTING_CHUNKS  ├──────(Falha em teste)───────►│
  └─────────┬────────┘                              │
            │ (4 chunks verdes)                     │
            ▼                                       │
  ┌──────────────────┐                              │
  │  AST_GUARD_SCAN  ├──────(Mock / SQLi / Eval)───►│
  └─────────┬────────┘                              │
            │ (Zero violações graves)               │
            ▼                                       │
┌───────────────────────┐                           │
│   AST_SCREENER_SCAN   │                           │
└───────────┬───────────┘                           │
            │ (Suspeitas detectadas)                │
            ▼                                       │
┌───────────────────────┐                           │
│   NOUL_ARBITRATION    ├─(P >= 0.78 e tentativas<5)┘
└───────────┬───────────┘
            │
            ├─► (P < 0.78 ou Fast-Path) ───────────► ┌────────────┐
            │                                        │  APPROVED  │
            │                                        └────────────┘
            │
            └─► (Tentativa >= 5 / Circuit Breaker) ─► ┌──────────────────────┐
                                                      │  EMERGENCY_RELEASE   │
                                                      └──────────────────────┘
```

### 1.3. Transições Explicitamente Proibidas
* **PROIBIDO `REJECTED` $\rightarrow$ `APPROVED`:** Não é permitida aprovação direta de um estado rejeitado sem nova passagem obrigatória pelo pipeline completo (`DRAFT` $\rightarrow$ `TESTING_CHUNKS` $\rightarrow$ ...).
* **PROIBIDO `DRAFT` $\rightarrow$ `APPROVED`:** Nenhuma entrega pode saltar etapas de teste ou inspeção de AST (Fast-Bypass proibido).
* **PROIBIDO Aprovação sem Testes Reais:** Se o workspace não contiver arquivos de teste em `tests/`, o estado transiciona obrigatoriamente para `REJECTED`. O agente deve criar testes reais em `tests/` antes de re-submeter.
* **PROIBIDO `AST_GUARD_SCAN` $\rightarrow$ `EMERGENCY_RELEASE`:** Violações de segurança e presença de mocks em produção (`src/`) **NUNCA** são liberadas pelo Circuit Breaker. O Circuit Breaker só liberta suspeitas da Camada 2/3.
* **PROIBIDO Qualquer Estado $\rightarrow$ `Git Commit` sem estar em `APPROVED` ou `EMERGENCY_RELEASE`:** Invariante `RN-JEV-009`.

---

## 2. Ciclo de Vida da Alocação de Hardware e Inferência (`HardwareContextLifecycle`)

Governa a contenção de recursos na GPU GTX 1050 Ti (4GB VRAM) e CPU do host.

### 2.1. Estados Permitidos

| Estado | Significado Semântico |
| :--- | :--- |
| `UNINITIALIZED` | Motor discriminativo instanciado em repouso sem pesos alocados na memória. |
| `ACQUIRING_LOCK` | Tentativa não-bloqueante de obter `/tmp/openjev_gpu.lock` via `fcntl.flock`. |
| `CUDA_ACTIVE` | Pesos do Laya carregados em `cuda:0` utilizando precisão `float16`. |
| `CPU_FALLBACK` | Pesos do Laya carregados na memória RAM do host em `cpu` com `torch.float32` e threads limitadas a 2. |
| `OFFLOADED` | Memória de tensores liberada explicitamente (`torch.cuda.empty_cache()` ou desalocação de modelo). |

### 2.2. Transições Válidas

```text
                ┌──────────────────┐
                │  UNINITIALIZED   │
                └────────┬─────────┘
                         │ (load_model)
                         ▼
                ┌──────────────────┐
                │  ACQUIRING_LOCK  │
                └────┬────────┬────┘
     (Lock obtido &  │        │ (Lock ocupado, VRAM < 1.5GB
     CUDA disponível)│        │  ou CUDA indisponível)
                     ▼        ▼
       ┌─────────────┐        ┌──────────────┐
       │ CUDA_ACTIVE │        │ CPU_FALLBACK │
       └──────┬──────┘        └───────┬──────┘
              │                       │
              │ (liberar recursos)    │ (liberar recursos)
              └───────────┬───────────┘
                          ▼
                   ┌──────────────┐
                   │  OFFLOADED   │
                   └──────────────┘
```

### 2.3. Transições Proibidas e Invariantes
* **PROIBIDO `ACQUIRING_LOCK` $\rightarrow$ `CUDA_ACTIVE` com `bfloat16`:** Invariante `RN-JEV-001`.
* **PROIBIDO Concorrência em `CUDA_ACTIVE`:** Se o lock estiver detido por outro processo, é terminantemente proibido tentar forçar inicialização CUDA; a transição obrigatória é para `CPU_FALLBACK`.
* **Idempotência de Fechamento:** A transição para `OFFLOADED` deve ser segura para múltiplas invocações sem levantar `ValueError` ou falha de descritor de arquivo.
