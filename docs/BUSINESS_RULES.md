# Catálogo de Invariantes e Regras de Negócio — Core Open JEV

> **Documento Normativo:** `docs/BUSINESS_RULES.md`  
> **Status:** Oficial & Ativo  
> **Governança:** Valério (Lead Architect) & Clara (QA & Governança)  
> **Última Atualização:** Setembro/2026

Este catálogo formaliza todas as invariantes e regras de negócio do motor Open JEV em formato indexável (`RN-JEV-xxx`). Nenhuma alteração arquitetural ou de código pode violar as invariantes aqui descritas.

---

## Índice de Regras

1. [RN-JEV-001: Isolamento e Proibição de bfloat16 em Hardware Pascal](#rn-jev-001)
2. [RN-JEV-002: Fallback Gracioso de Inferência ML GPU $\rightarrow$ CPU](#rn-jev-002)
3. [RN-JEV-003: Concorrência e Exclusividade de Acesso à GPU (File Lock)](#rn-jev-003)
4. [RN-JEV-004: Anti-Mock e Proibição de Simulação em Código de Produção](#rn-jev-004)
5. [RN-JEV-005: Execução Isolada de Testes Pesados via Pytest Chunks](#rn-jev-005)
6. [RN-JEV-006: Extração Atômica de Snippet AST ($\le 150$ Tokens)](#rn-jev-006)
7. [RN-JEV-007: Calibração de Threshold e Decisão do JEV Noul Arbitrage ($P \ge 0.78$)](#rn-jev-007)
8. [RN-JEV-008: Circuit Breaker Universal de 5 Tentativas (Emergency Release)](#rn-jev-008)
9. [RN-JEV-009: Proibição de Git Commit Anterior à Aprovação da Entrega](#rn-jev-009)
10. [RN-JEV-010: Higiene Estrutural e Tipagem Estrita em Código de Produção](#rn-jev-010)

---

### RN-JEV-001: Isolamento e Proibição de bfloat16 em Hardware Pascal
* **ID & Título:** `RN-JEV-001: Proibição Estrita de bfloat16 na Arquitetura GP107`
* **Invariante (O que NUNCA pode acontecer):**  
  O motor JEV NUNCA pode inicializar tensores ou carregar checkpoints do Laya RLCD utilizando precisão `torch.bfloat16` quando executando em GPUs da arquitetura NVIDIA Pascal (ex: GeForce GTX 1050 Ti, `sm_61`). A violação resulta em corrupção de memória ou kernel panic de CUDA (`CUDA error: no kernel image is available for execution`).
* **Critério de Aceite / Fluxo Válido:**  
  * O módulo de hardware detecta a capacidade computacional da GPU (`major, minor`).
  * Em dispositivos com `major <= 6` (Pascal e inferiores), os tipos de dados permitidos são estritamente `torch.float16` ou `torch.float32`.
* **Casos de Borda & Exceções:**  
  * Execução em CPU: converte automaticamente para `torch.float32`.
  * Ausência de suporte a half-precision nativo: fallback automático para `float32`.
* **Módulos Relacionados:**  
  * [`src/decision/hardware.py`](file:///home/maiteuxzanela/projeto%20JEV/src/decision/hardware.py)
  * [`src/decision/laya_adapter.py`](file:///home/maiteuxzanela/projeto%20JEV/src/decision/laya_adapter.py)
* **Testes Associados:**  
  * `tests/test_hardware.py`
  * `tests/test_laya_adapter.py::test_device_and_dtype_selection`

---

### RN-JEV-002: Fallback Gracioso de Inferência ML GPU $\rightarrow$ CPU
* **ID & Título:** `RN-JEV-002: Resiliência de Inferência com Fallback Automático para CPU`
* **Invariante (O que NUNCA pode acontecer):**  
  Uma requisição de inferência (`choice`, `noul`, `consulta` ou `verify_delivery`) NUNCA pode abortar com exceção não tratada (`CUDA OOM`, `driver communication failure` ou indisponibilidade de dispositivo) se houver recursos de CPU disponíveis para processar a premissa.
* **Critério de Aceite / Fluxo Válido:**  
  * O motor verifica se CUDA está disponível e se há VRAM livre $\ge 1.5$ GB.
  * Caso CUDA falhe ou a VRAM esteja saturada, o motor chaveia transparentemente para `device="cpu"` com `torch.set_num_threads(2)` (protegendo a CPU do host contra congelamento) e conclui a inferência.
* **Casos de Borda & Exceções:**  
  * Perda de nós em `/dev/nvidia*`: fallback imediato para CPU sem propagar traceback fatal.
  * Timeout de carregamento em CPU: limitado por timeout configurável de socket/IPC.
* **Módulos Relacionados:**  
  * [`src/decision/engine.py`](file:///home/maiteuxzanela/projeto%20JEV/src/decision/engine.py)
  * [`src/decision/hardware.py`](file:///home/maiteuxzanela/projeto%20JEV/src/decision/hardware.py)
* **Testes Associados:**  
  * `tests/test_hardware.py`
  * `tests/test_needle_verify_delivery.py::test_needle_verify_delivery_engine_unloaded_telemetry_fallback`

---

### RN-JEV-003: Concorrência e Exclusividade de Acesso à GPU (File Lock)
* **ID & Título:** `RN-JEV-003: Exclusividade de Memória VRAM via File Descriptor Lock`
* **Invariante (O que NUNCA pode acontecer):**  
  Dois processos distintos (ex: servidor MCP e worker de testes) NUNCA podem alocar contextos CUDA concorrentemente na GTX 1050 Ti (4GB VRAM), sob pena de estouro imediato de memória compartilhada.
* **Critério de Aceite / Fluxo Válido:**  
  * A alocação de GPU é condicionada à obtenção não-bloqueante de lock exclusivo via `fcntl.flock` no arquivo `/tmp/openjev_gpu.lock`.
  * Se o lock falhar (`BlockingIOError` ou `OSError`), o processo solicitante opera em CPU ou delega requisições via cliente IPC.
* **Casos de Borda & Exceções:**  
  * Processo morto de forma abrupta: o sistema operacional libera o lock do file descriptor automaticamente.
* **Módulos Relacionados:**  
  * [`src/decision/hardware.py`](file:///home/maiteuxzanela/projeto%20JEV/src/decision/hardware.py)
* **Testes Associados:**  
  * `tests/test_hardware.py::test_gpu_lock_acquisition_and_release`

---

### RN-JEV-004: Anti-Mock e Proibição de Simulação em Código de Produção
* **ID & Título:** `RN-JEV-004: Integridade Determinística — Proibição Estrita de Mocks em Produção`
* **Invariante (O que NUNCA pode acontecer):**  
  Arquivos sob `src/` NUNCA podem importar ou utilizar `unittest.mock`, `MagicMock`, `pytest_mock` ou qualquer macete de substituição estática de dependências. Testes de produção devem executar contra implementações reais.
* **Critério de Aceite / Fluxo Válido:**  
  * A Camada 1 (`NeedleASTGuard`) inspeciona a AST de todo arquivo de produção alterado.
  * Presença de imports ou chamadas a mocks em `src/` gera violação bloqueante imediata (`status: REJECTED`).
* **Casos de Borda & Exceções:**  
  * Arquivos dentro do diretório `tests/` possuem isenção para fixtures de isolamento unitário em testes de integração de borda.
* **Módulos Relacionados:**  
  * [`src/memory/needle_ast_guard.py`](file:///home/maiteuxzanela/projeto%20JEV/src/memory/needle_ast_guard.py)
* **Testes Associados:**  
  * `tests/test_needle_ast_guard.py::test_detect_mock_in_production`
  * `tests/test_needle_verify_delivery.py::test_needle_verify_delivery_ast_violation_rejected`

---

### RN-JEV-005: Execução Isolada de Testes Pesados via Pytest Chunks
* **ID & Título:** `RN-JEV-005: Isolamento Serial de Cargas de Teste de ML e Governança`
* **Invariante (O que NUNCA pode acontecer):**  
  A suíte de testes de Machine Learning e Hardware NUNCA pode ser executada em paralelo (`pytest -n auto`) ou no mesmo processo dos testes de API/Governança, evitando concorrência destrutiva de memória na CPU/GPU.
* **Critério de Aceite / Fluxo Válido:**  
  * O runner particiona a suíte em 4 chunks sequenciais:
    1. `chunk_1_heavy_ml_isolated`: Testes de inferência ML, hardware, adaptadores e IPC (execução solo).
    2. `chunk_2_core_governance`: Esquemas, contratos Pydantic e personas.
    3. `chunk_3_memory_ast`: Needle 3, AST guard e defect screener.
    4. `chunk_4_api_orchestration`: Servidor MCP, endpoints FastAPI e orquestrador.
  * O pipeline interrompe na primeira falha detectada (Fail-Fast).
  * **Obrigatoriedade de Testes:** Caso nenhum teste seja encontrado em `tests/`, o runner emite `pytest_success: False` e o validador rejeita a entrega. O agente deve criar testes reais em `tests/test_<modulo>.py`. É terminantemente proibido tentar burlar este bloqueio.
* **Casos de Borda & Exceções:**  
  * Timeout individual por chunk: 60 segundos por chunk para evitar travas permanentes.
* **Módulos Relacionados:**  
  * [`src/orchestrator/pytest_chunk_runner.py`](file:///home/maiteuxzanela/projeto%20JEV/src/orchestrator/pytest_chunk_runner.py)
* **Testes Associados:**  
  * `tests/test_needle_verify_delivery.py`

---

### RN-JEV-006: Extração Atômica de Snippet AST ($\le 150$ Tokens)
* **ID & Título:** `RN-JEV-006: Prevenção de Context Dumping no Laya RLCD`
* **Invariante (O que NUNCA pode acontecer):**  
  O motor NUNCA pode enviar arquivos de código inteiros, diffs completos ou trechos com mais de 300 linhas como premissa para a inferência discriminativa do Laya RLCD.
* **Critério de Aceite / Fluxo Válido:**  
  * O `NeedleASTScreener` utiliza `_extract_atomic_snippet(...)` extraindo uma janela cirúrgica de 10 a 15 linhas ao redor do nó AST com anomalia.
  * O tamanho da premissa atômica deve respeitar o teto estrito de 150 tokens.
* **Casos de Borda & Exceções:**  
  * Se o símbolo possuir menos de 15 linhas, o nó inteiro é aproveitado sem truncamento.
* **Módulos Relacionados:**  
  * [`src/memory/needle_defect_screener.py`](file:///home/maiteuxzanela/projeto%20JEV/src/memory/needle_defect_screener.py)
* **Testes Associados:**  
  * `tests/test_needle_defect_screener.py`

---

### RN-JEV-007: Calibração de Threshold e Decisão do JEV Noul Arbitrage ($P \ge 0.78$)
* **ID & Título:** `RN-JEV-007: Threshold de Decisão Categórica do Laya Noul`
* **Invariante (O que NUNCA pode acontecer):**  
  Uma suspeita do Screener NUNCA pode reprovar uma entrega se a probabilidade calculada pelo modelo Laya for estritamente menor que $0.78$ ($P < 0.78$). Da mesma forma, uma suspeita confirmada com $P \ge 0.78$ NUNCA pode ser aprovada sem correção prévia (exceto sob circuit breaker).
* **Critério de Aceite / Fluxo Válido:**  
  * $P \ge 0.78 \implies$ Defeito confirmado (`confirmed_defects`), entrega REPROVADA (`status: REJECTED`).
  * $P < 0.78 \implies$ Falso-positivo descartado (`dismissed_warnings`), entrega APROVADA (`status: APPROVED`).
* **Casos de Borda & Exceções:**  
  * Motor descarregado (`is_ready=False`): os alertas são registrados como telemetria sem reprovação indevida.
* **Módulos Relacionados:**  
  * [`src/mcp_server.py`](file:///home/maiteuxzanela/projeto%20JEV/src/mcp_server.py)
  * [`src/decision/laya_adapter.py`](file:///home/maiteuxzanela/projeto%20JEV/src/decision/laya_adapter.py)
* **Testes Associados:**  
  * `tests/test_needle_verify_delivery.py::test_needle_verify_delivery_noul_arbitrage_confirms_defect`
  * `tests/test_needle_verify_delivery.py::test_needle_verify_delivery_noul_arbitrage_dismisses_false_positive`

---

### RN-JEV-008: Circuit Breaker Universal de 5 Tentativas (Emergency Release)
* **ID & Título:** `RN-JEV-008: Travamento e Saída de Emergência na 5ª Tentativa`
* **Invariante (O que NUNCA pode acontecer):**  
  Um ciclo de entrega ou revisão NUNCA pode entrar em loop infinito de correções que ultrapasse 5 tentativas.
* **Critério de Aceite / Fluxo Válido:**  
  * Se o pipeline de homologação falhar consecutivamente por 4 rodadas, na 5ª tentativa o motor ativa a Saída de Emergência (`status: EMERGENCY_RELEASE`, `approved_with_caveat: true`).
  * A contagem de tentativas é derivada **no servidor**, exclusivamente do histórico persistido do workspace (`resolve_attempt_number`): última tentativa aprovada mantém a contagem (revalidação não consome tentativa); reprovação ou histórico inexistente avança +1.
  * O campo `attempt_number` da requisição é **apenas informativo** — se divergir do valor resolvido, a tool registra o aviso e usa o valor do servidor (`src/mcp_server.py`).
  * A Saída de Emergência **nunca** ocorre antes da execução dos testes, do Guard e do Screener: a resposta de emergência traz `pytest_success: false` + `approved_with_caveat: true`, tornando explícito que a liberação é com ressalva.
  * O relatório registra formalmente as ressalvas técnicas pendentes para deliberação humana da engenharia.
* **Casos de Borda & Exceções:**  
  * `history_clear(workspace)` é **escopado ao workspace** (estado em memória + arquivo daquele workspace); nenhuma tool expõe reset global — a flag `reset_history` foi removida do catálogo, do IPC e do cliente.
  * `history_clear()` sem argumento (global) é reservado a setup de teste, sempre sob `JEV_STATE_DIR` isolado por `tests/conftest.py`: rodar a suíte nunca zera nem reescreve o registro real de homologação.
* **Módulos Relacionados:**  
  * [`src/mcp_server.py`](file:///home/maiteuxzanela/projeto%20JEV/src/mcp_server.py)
  * [`src/core/score_gate.py`](file:///home/maiteuxzanela/projeto%20JEV/src/core/score_gate.py)
* **Testes Associados:**  
  * `tests/test_needle_verify_delivery.py::test_needle_verify_delivery_circuit_breaker_emergency_release`
  * `tests/test_needle_verify_delivery.py::test_client_cannot_forge_emergency_on_first_attempt`
  * `tests/test_needle_verify_delivery.py::test_history_clear_is_scoped_and_state_dir_is_isolated`

---

### RN-JEV-009: Proibição de Git Commit Anterior à Aprovação da Entrega
* **ID & Título:** `RN-JEV-009: Bloqueio Estrito de Git Commit Pré-Homologação`
* **Invariante (O que NUNCA pode acontecer):**  
  Nenhum agente autônomo, orquestrador ou script NUNCA pode executar `git commit` ou limpar o `git status` antes que a ferramenta `needle_verify_delivery` retorne `passed: true` (`APPROVED` ou `EMERGENCY_RELEASE`).
* **Critério de Aceite / Fluxo Válido:**  
  * Todas as alterações de código e testes residem na árvore de trabalho suja (`git status -s`).
  * Somente após o veredito final formal o commit de consolidação é permitido.
* **Casos de Borda & Exceções:**  
  * Não há exceção. A regra é inegociável na governança global e local do projeto.
* **Módulos Relacionados:**  
  * [`AGENTS.md`](file:///home/maiteuxzanela/projeto%20JEV/AGENTS.md)
* **Testes Associados:**  
  * Verificação determinística via `needle_verify_delivery`.

---

### RN-JEV-010: Higiene Estrutural e Tipagem Estrita em Código de Produção
* **ID & Título:** `RN-JEV-010: Diretrizes de Excelência de Engenharia da Camada 2`
* **Invariante (O que NUNCA pode acontecer):**  
  Código de produção em `src/` NUNCA pode introduzir:
  1. I/O bloqueante em corrotinas `async def` (`time.sleep`, `requests.*`, `subprocess.*`, `open()` desprotegido).
  2. Argumentos padrão mutáveis (`def fn(x=[])`, `def fn(x={})` ou `Field(default=[])`).
  3. Relançamento de exceção genérica como novo erro sem encadeamento explícito (`raise ... from err`).
  4. Sessões e conexões desvinculadas de context manager (`httpx.Client()`, `connect()`).
  5. Profundidade de blocos de controle cognitivos $\ge 4$ níveis.
* **Critério de Aceite / Fluxo Válido:**  
  * O código atende à tipagem estrita de parâmetros e retornos (sem `Any` solto).
  * Funções públicas de módulo expõem docstrings com exemplos de uso.
* **Casos de Borda & Exceções:**  
  * Arquivos de teste (`tests/`), stubs (`.pyi`), dunders (`__init__`) e decoradores `@property`/`@overload` são isentos.
* **Módulos Relacionados:**  
  * [`src/memory/needle_defect_screener.py`](file:///home/maiteuxzanela/projeto%20JEV/src/memory/needle_defect_screener.py)
* **Testes Associados:**  
  * `tests/test_needle_defect_screener.py`
