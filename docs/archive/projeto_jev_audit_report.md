# Relatório de Auditoria Arquitetural: Projeto JEV (Core Engine & Ferramentas)

> **Nota de proveniência (set/2026):** os números e probabilidades abaixo são evidência de uma **execução pontual** do pipeline sob o contrato antigo (choice flat de diagnóstico, 10 categorias). Desde a refatoração de set/2026 o fluxo de `openjev_score_and_diagnose` **não usa choice**: diagnóstico via Screener Needle 3 + Noul sobre **10 categorias técnicas sem nulas** (8 duras + 2 de análise; ver AGENTS.md §2), com banda de indiferença **[3.20, 3.40)**. Os dados medidos não foram reexecutados.

Auditoria profunda executada via **Pipeline Tri-Camada JEV** sobre o próprio código-fonte do motor discriminativo:
1. **Cactus Needle 3 Local Engine**: Varredura relacional AST cirúrgica de nós, schemas, adaptadores e servidores MCP (trechos limitados entre 500 e 1.500 tokens).
2. **Open Jev Local Engine (Modelo Laya RLCD)**: Roteamento de veredito (`choice`), validação de hipóteses de risco (`noul` threshold 0.78) e auditoria de conformidade factual (`score` corte 3.40).
3. **Diagnóstico Estruturado**: Classificação discriminativa sobre as 10 categorias flat (`DEFECT_OPTIONS`) com identificação de causa-raiz.

---

## 1. Painel Geral de Conformidade e Desempenho

| Dimensão Auditada | Arquivos Alvo | Choice (Veredito) | Noul (Risco $\ge 0.78$) | Score Laya | Diagnóstico de Defeito |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1. Motor & Hardware** | [`hardware.py`](file:///home/maiteuxzanela/projeto%20JEV/src/decision/hardware.py), [`laya_adapter.py`](file:///home/maiteuxzanela/projeto%20JEV/src/decision/laya_adapter.py) | `overhead_cpu` (39.4%) | ⚠️ Alerta ($P=0.7460$) | **3.05 / 5.00** | `performance_escopo_estilo` (59.0%) |
| **2. Servidor MCP** | [`mcp_server.py`](file:///home/maiteuxzanela/projeto%20JEV/src/mcp_server.py) | `concorrencia_ok` (76.1%) | 🚨 Alerta ($P=0.0896$) | **2.64 / 5.00** | `qualidade_de_testes` (22.8%) |
| **3. Memória AST** | [`local_needle.py`](file:///home/maiteuxzanela/projeto%20JEV/src/memory/local_needle.py), [`needle_ast_guard.py`](file:///home/maiteuxzanela/projeto%20JEV/src/core/needle_ast_guard.py) | `falhas_parsing` (59.8%) | ⚠️ Alerta ($P=0.6501$) | **2.23 / 5.00** | `qualidade_de_testes` (48.8%) |
| **4. Orquestrador** | [`subagent_runner.py`](file:///home/maiteuxzanela/projeto%20JEV/src/orchestrator/subagent_runner.py) | `isolamento_ok` (50.6%) | ⚠️ Alerta ($P=0.6938$) | **2.79 / 5.00** | `logica_ou_regra` (23.0%) |
| **5. Governança** | [`schemas.py`](file:///home/maiteuxzanela/projeto%20JEV/src/core/schemas.py), [`score_gate.py`](file:///home/maiteuxzanela/projeto%20JEV/src/core/score_gate.py) | `inconsistencia_limiares` (43.0%) | ✅ Seguro ($P=0.7874$) | **2.84 / 5.00** | `robustez_excecoes_recursos` (25.9%) |

> **Média Global:** **2.71 / 5.00** | **Tempo de Execução:** **27.14 segundos** | **Status:** **REQUER AJUSTES**

```mermaid
radar-beta
    title "Avaliação de Rigor Arquitetural — Projeto JEV"
    axis MotorHardware["Hardware & VRAM"], ServidorMCP["Servidor MCP"], MemoriaAST["Needle 3 AST"], Orquestrador["Subagentes"], Governanca["Contratos & Schemas"]
    curve "Pontuação Obtida" [3.05, 2.64, 2.23, 2.79, 2.84]
    curve "Linha de Corte (3.40)" [3.40, 3.40, 3.40, 3.40, 3.40]
```

---

## 2. Diagnóstico Aprofundado por Dimensão

### 🔴 Dimensão 1: Motor de Inferência, Profilagem de GPU e Gestão de VRAM
* **Arquivos-chave:** [`src/decision/hardware.py`](file:///home/maiteuxzanela/projeto%20JEV/src/decision/hardware.py), [`src/decision/laya_adapter.py`](file:///home/maiteuxzanela/projeto%20JEV/src/decision/laya_adapter.py)
* **Score:** **3.05 / 5.00** | **Noul:** Alerta ($P = 0.7460$) | **Choice:** `overhead_ou_degradacao_cpu` (39.4%)
* **Diagnóstico Flat:** `performance_escopo_estilo` (58.99%)
* **Problemas Detectados:**
  1. **Degradação Severa de CPU no Fallback:** Quando a GPU está bloqueada por outra janela (`try_acquire_gpu_lock() == False`), o carregamento do Laya na CPU consome **2.3 GB de RAM física** e aciona todas as threads do OpenMP/MKL em 100% de CPU. Em uma máquina com 8GB de RAM, essa sobrecarga empurra o sistema imediatamente para a fronteira do `oom-killer`.
  2. **Limiar Rígido de VRAM (`free_mb < 2200`):** Com o compositor COSMIC ativo (~400 MB alocados), a VRAM livre inicial fica em torno de 3.690 MB. O limiar de 2.200 MB é seguro para o primeiro processo, mas qualquer processo concorrente cai direto para CPU, sem mecanismo de fila (*queue/mutex*) compartilhada entre processos para reutilizar a inferência via socket.

---

### 🔴 Dimensão 2: Servidor MCP Universal, Sinais do Kernel e Concorrência
* **Arquivos-chave:** [`src/mcp_server.py`](file:///home/maiteuxzanela/projeto%20JEV/src/mcp_server.py)
* **Score:** **2.64 / 5.00** | **Noul:** Alerta Crítico ($P = 0.0896$) | **Choice:** `concorrencia_saudavel` (76.1%)
* **Diagnóstico Flat:** `qualidade_de_testes` (22.77%)
* **Problemas Detectados:**
  1. **Ausência de Testes Automatizados de Ciclo de Vida MCP:** Não há testes end-to-end em `tests/` que validem o comportamento de desconexão abrupta do cliente MCP (envio de `SIGTERM`, queda do processo-pai e `_parent_watchdog`).
  2. **Sobrecarga de Preload Corrigida no Voo:** O `_preload_engine()` pré-carregava o Laya na CPU incondicionalmente em instâncias secundárias. Embora tenhamos adicionado a trava `try_acquire_gpu_lock()` nesta sessão, falta cobertura de teste unitário comprovando que processos secundários iniciam com RSS leve (<60 MB).

---

### 🚨 Dimensão 3: Memória Relacional AST (Needle 3) e Guardrails Estáticos
* **Arquivos-chave:** [`src/memory/local_needle.py`](file:///home/maiteuxzanela/projeto%20JEV/src/memory/local_needle.py), [`src/core/needle_ast_guard.py`](file:///home/maiteuxzanela/projeto%20JEV/src/core/needle_ast_guard.py)
* **Score:** **2.23 / 5.00 (Pior nota do projeto)** | **Noul:** Alerta ($P = 0.6501$) | **Choice:** `falhas_de_parsing_ast` (59.8%)
* **Diagnóstico Flat:** `qualidade_de_testes` (48.8%)
* **Problemas Detectados:**
  1. **Fragilidade de Tipagem no Schema (`scope`):** O schema `Needle3Query` exige rigorosamente `scope: List[str]`. Quando chamadores enviam `scope: str` (como ocorreu na primeira execução do script), o Pydantic estoura `ValidationError` fatal em vez de fazer coerção automática para lista (`before_validator`).
  2. **Robustez a Erros de Sintaxe / Arquivos Binários:** Se o `LocalNeedleEngine` encontra um arquivo Python com erro de sintaxe (`SyntaxError`) durante a varredura relacional, a árvore AST é abortada para aquele arquivo sem fallback léxico inteligente.

---

### 🔴 Dimensão 4: Orquestrador de Subagentes e Execução Hermes
* **Arquivos-chave:** [`src/orchestrator/subagent_runner.py`](file:///home/maiteuxzanela/projeto%20JEV/src/orchestrator/subagent_runner.py)
* **Score:** **2.79 / 5.00** | **Noul:** Alerta ($P = 0.6938$) | **Choice:** `isolamento_efetivo` (50.6%)
* **Diagnóstico Flat:** `logica_ou_regra` (23.0%)
* **Problemas Detectados:**
  1. **Acoplamento Antecipado do Motor de Score:** O runner de subagentes legado chamava `_get_engine_async()` **antes** de disparar o subagente. Em máquinas de 8GB, manter o motor carregado na memória enquanto o Hermes executa ferramentas e subprocessos externos provoca contenção de RAM desnecessária.
  2. **Falta de Sandboxing de Comandos do Subagente:** O subagente despachado pelo Hermes opera com a flag `--yolo` e executa comandos diretamente no host. Como vimos com a persona Clara, o subagente pode disparar de forma autônoma comandos pesadíssimos como `pytest tests/`, sem limite de threads ou restrição de escopo, derrubando o ambiente gráfico.

---

### 🟡 Dimensão 5: Contratos Pydantic, Catálogo Fechado e Borderline Mass Gate
* **Arquivos-chave:** [`src/core/schemas.py`](file:///home/maiteuxzanela/projeto%20JEV/src/core/schemas.py), [`src/core/score_gate.py`](file:///home/maiteuxzanela/projeto%20JEV/src/core/score_gate.py)
* **Score:** **2.84 / 5.00** | **Noul:** ✅ **SEGURO ($P = 0.7874$)** | **Choice:** `inconsistencia_de_limiares` (43.0%)
* **Diagnóstico Flat:** `robustez_excecoes_recursos` (25.9%)
* **Problemas Detectados:**
  1. **Drift de Documentação vs Código:** O `AGENTS.md` e alguns testes ainda guardavam referências ao antigo modelo DeBERTa e limiares desatualizados (`0.85 / 3.75`), enquanto o código de produção já opera calibrado no Laya (`0.78 / 3.40`).
  2. **Truncamento Rígido de Premissas (1.500 caracteres):** Se um orquestrador formula uma premissa com 1.501 caracteres, o servidor MCP rejeita sumariamente a validação com erro de contrato em vez de normalizar metadados redundantes, causando atrito no fechamento.

---

## 3. Matriz de Priorização das Correções

```
   ALTO IMPACTO
      ▲
      │  [D1] Fila/IPC de inferência GPU    [D4] Guardrail de comandos no Hermes
      │  (eliminar fallback pesado de CPU)   (evitar 'pytest tests/' autônomo)
      │
      │  [D3] Coerção de scope no Needle     [D2] Teste unitário de ciclo de vida
      │  (aceitar str ou list no query)      (desconexão de clientes MCP)
      │
      │  [D5] Limpeza de menções a DeBERTa   [D5] Sanitização tolerante de premissas
      ▼
   BAIXO IMPACTO ───► BAIXO ESFORÇO                           ALTO ESFORÇO
```

---

## 4. Plano de Ação Recomendado (Top 4 Correções)

1. **Auto-coerção no `Needle3Query` ([`src/core/schemas.py`](file:///home/maiteuxzanela/projeto%20JEV/src/core/schemas.py)):** Adicionar `@field_validator('scope', mode='before')` para converter strings únicas (ex.: `"src/decision"`) automaticamente em `["src/decision"]`, eliminando falhas bobas de Pydantic.
2. **Diretriz de Escopo no Prompt de Subagentes ([`src/orchestrator/subagent_runner.py`](file:///home/maiteuxzanela/projeto%20JEV/src/orchestrator/subagent_runner.py)):** Injetar na missão do Hermes uma restrição mandatória para nunca executar suites de teste globais (`pytest tests/`) sem aprovação explícita, priorizando testes pontuais de arquivo único.
3. **Desacoplamento de Carregamento no Spawn:** Consolidar a instanciação estritamente *lazy* do motor de avaliação pós-Hermes, liberando 100% da RAM enquanto o subagente estiver ativo.
4. **Alinhamento Documental:** Expurgar menções aos limiares legados de 0.85 / 3.75 da base documental.
