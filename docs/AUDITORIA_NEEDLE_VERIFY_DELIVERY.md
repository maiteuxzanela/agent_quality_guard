# Guia de Auditoria — `needle_verify_delivery`

> **Destinatário:** revisor externo (auditoria imparcial de estrutura, não de uso).
> **Escopo:** o pipeline de homologação determinística de entregas de código do Projeto JEV
> (`needle_verify_delivery`) e toda a camada que o sustenta (Pytest em chunks, NeedleASTGuard,
> NeedleASTScreener e arbitragem Noul).
> **Modo de leitura:** siga a ordem "Mapa de arquivos → Pipeline → Como reproduzir → Checklist".

---

## 1. O que é esta ferramenta

`needle_verify_delivery` é a **única tool de homologação de entrega de código** do ecossistema.
Ela responde uma única pergunta: *esta entrega pode ser considerada aprovada?*
A resposta é 100% determinística em nível estrutural; a única parte probabilística
(inferência local Laya/GPU) é usada **apenas para arbitrar suspeitas já detectadas**
(nunca para criar aprovação do zero).

Formalização:

| Sinal | Significado |
| :--- | :--- |
| `status: "APPROVED"` | Todos os testes reais passaram e nenhuma suspeita estrutural foi confirmada. |
| `status: "REJECTED"` | Pelo menos um teste falhou **ou** a arbitragem Noul confirmou um defeito (P ≥ 0.78). |
| `status: "EMERGENCY_RELEASE"` | Saída de Emergência na **5ª tentativa** (`approved_with_caveat: true`). |

---

## 2. Mapa de arquivos (por onde começar a leitura)

| Arquivo | Linhas | Papel |
| :--- | ---: | :--- |
| `src/mcp_server.py` | 1136 | Servidor MCP; tool `needle_verify_delivery` (Etapa 0→3, circuit breaker, montagem da resposta). |
| `src/memory/needle_defect_screener.py` | 1160 | **Camada 2** — rastreio de suspeitas estruturais (10 categorias, gatilhos A1–A14). |
| `src/memory/needle_ast_guard.py` | 691 | **Camada 1** — bloqueios fail-fast (mock em produção, SQL Injection, `eval`, segredos, SSL, etc.). |
| `src/orchestrator/pytest_chunk_runner.py` | 237 | **Etapa 0** — descoberta, particionamento em 3–4 chunks e execução sequencial de testes reais. |
| `src/orchestrator/multi_stack_runner.py` | 334 | Varredura de stacks (React Native / Web JS / Python) + varredura de arquivos `.js/.ts` com regex. |
| `src/core/score_gate.py` | 367 | Histórico escopado por workspace, `resolve_attempt_number` e `MAX_ATTEMPTS = 5` (circuit breaker). |
| `src/core/schemas.py` | — | Contratos `NeedleVerifyDeliveryRequest/Response`, `ASTViolation`-like schemas. |
| `src/decision/ipc_service.py` | — | IPC Unix socket + fallback HTTP Bridge (chamada síncrona/assíncrona). |
| `tests/conftest.py` | 20 | Isolamento `JEV_STATE_DIR` da suíte (na importação — cobre testes `unittest.TestCase`). |
| `tests/test_needle_verify_delivery.py` | 386 | Testes da tool: aprovação, violação, emergência server-side, forge bloqueada, workspace sem testes, escopo do histórico. |
| `tests/` | 31 arquivos | Suíte real (276 testes coletados) — é a fonte de verdade da Etapa 0. |

### Regras da Camada 1 (`NeedleASTGuard`) — violação = rejeição imediata

`mock_in_production`, `sql_injection`, `swallowed_exception`, `tautological_test`,
`hardcoded_secret`, `command_injection`, `insecure_deserialization`,
`dangerous_eval_exec`, `insecure_ssl_verify`.

### Categorias da Camada 2 (`NeedleASTScreener`) e prioridade de arbitragem

| # | Categoria | # | Categoria |
| ---: | :--- | ---: | :--- |
| 1 | `seguranca` | 6 | `qualidade_de_testes` |
| 2 | `robustez_excecoes_recursos` | 7 | `performance_escopo_estilo` |
| 3 | `logica_ou_regra` | 8 | `documentacao_configuracao` |
| 4 | `contrato_ambiente` | 9 | `tipagem_ausente` |
| 5 | `requisito_incompleto_ou_ausente` | 10 | `docstring_ausente` |

Somente as **10 prioridades mais altas** (`MAX_ARBITRATIONS = 10`) seguem para a Etapa 3;
o restante fica como telemetria (`structural_ast_warnings`).

---

## 3. Pipeline (ordem fail-fast)

```
Etapa 0  pytest em chunks (chunk_1 heavy ML isolado → 2 → 3 → 4)
           └─ qualquer falha ⇒ REJECTED (aborta as etapas seguintes)
Etapa 1  NeedleASTGuard  (Camada 1: bloqueios inegociáveis)
           └─ qualquer violação ⇒ REJECTED
Etapa 2  NeedleASTScreener (Camada 2: 0..N suspeitas por símbolo)
           └─ 0 suspeitas ⇒ Fast-Path de aprovação (sem inferência)
Etapa 3  JEV Noul Arbitrage (Laya RLCD local, threshold 0.78 por micro-premissa)
           └─ alguma suspeita confirmada ⇒ REJECTED
Circuit breaker: tentativa ≥ MAX_ATTEMPTS(5) ⇒ EMERGENCY_RELEASE
```

**Anti-gaming relevante:**
- `passed` depende de **testes reais** e de **AST**, nunca de nota contínua do modelo.
- A tentativa é calculada **no servidor** por `resolve_attempt_number` (em `src/core/score_gate.py`),
  sempre a partir do histórico persistido do workspace: última tentativa aprovada mantém a contagem
  (revalidação não consome tentativa), reprovação avança +1. O campo `attempt_number` da requisição é
  **informativo** — se divergir, a tool registra o aviso e usa o valor do servidor. Por isso
  `attempt_number=5` numa primeira chamada **não** destrava a Saída de Emergência (regressão coberta
  por `test_client_cannot_forge_emergency_on_first_attempt`).
- O histórico vive fora do repositório (`/tmp/openjev_state/score_state_*.json`), é **escopado por
  workspace** e só pode ser zerado por `history_clear(workspace)`. A flag `reset_history` foi
  removida da tool, do catálogo (`src/core/tools.py`), do IPC e do cliente — nenhum chamador zera
  registro alheio.
- Vários testes da suíte limpam histórico (do próprio workspace de teste): por isso
  `tests/conftest.py` aponta `JEV_STATE_DIR` para um diretório exclusivo da sessão **na importação**,
  cobrindo também os testes `unittest.TestCase`. Rodar `pytest` nunca apaga nem reescreve o registro
  real de homologação (regressão coberta por
  `test_history_clear_is_scoped_and_state_dir_is_isolated`).
- Entregas sem nenhum teste coletável são **rejeitadas** (`pytest_chunks` vazio ⇒
  `passed: false`), e a Saída de Emergência só vem depois de pytest + Guard + Screener, com
  `pytest_success: false` + `approved_with_caveat: true` — nunca como aprovação limpa.

---

## 4. Como reproduzir localmente

```bash
cd "/home/maiteuxzanela/projeto JEV"

# 1) Suíte completa (a mesma que a Etapa 0 executa em chunks)
.venv/bin/pytest -q

# 2) Dump local das suspeitas do screener (deve imprimir TOTAL: 0)
cat > /tmp/dump_sus.py <<'EOF'
import sys
sys.path.insert(0, ".")
from src.memory.ast_diff_extractor import extract_workspace_ast_symbols
from src.memory.needle_defect_screener import screen_workspace_symbols
syms = extract_workspace_ast_symbols(".")
m = screen_workspace_symbols(syms, workspace_root=".")
items = [(s.category, s.file_path, s.line_number, k) for k, lst in m.items() for s in lst]
print("TOTAL:", len(items))
for it in items:
    print(it)
EOF
.venv/bin/python /tmp/dump_sus.py

# 3) Pipeline completo fora do transporte MCP (mesmo código da tool)
cat > /tmp/run_verify.py <<'EOF'
import asyncio, json, sys
sys.path.insert(0, ".")
from src.mcp_server import needle_verify_delivery
print(json.dumps(asyncio.run(needle_verify_delivery(
    task_type="codigo_producao",
    workspace="/home/maiteuxzanela/projeto JEV",
)), indent=2, ensure_ascii=False, default=str))
EOF
.venv/bin/python /tmp/run_verify.py
```

> **Nota de transporte:** a tool roda a suíte inteira (~2 min; soma dos chunks ≈ 90 s — o mais
> pesado, `chunk_2_core_governance`, ≈ 58 s).
> O `mcp_timeout` do opencode foi ajustado de `30000` para `240000` ms justamente para que a
> chamada oficial da tool consiga retornar o payload. Se você clonar o repo em outra máquina,
> lembre de que a tool **não deve** ser acionada com timeout menor que o tempo do chunk pesado.

---

## 5. Decisões de engenharia desta entrega (alvos de auditoria)

Cada uma dessas decisões foi tomada para remover um alerta concreto do screener —
audite se a solução é **genuína** ou apenas contorna o gatilho:

1. **Aninhamento (A: nível ≥ 4 / laço dentro de laço)** — extração de helpers
   (`_read_ipc_line`, `_count_from_pytest_line`) e substituição de laços aninhados por
   *list/gen comprehensions* (`mcp_server`, `needle_ast_guard`, `multi_stack_runner`,
   `pytest_chunk_runner`). Ponto crítico: comprehensions não são nós `ast.For` —
   **verifique se a refatoração não piorou a legibilidade**.
2. **URLs e caminhos hardcoded (A9)** — `_loopback_url()` via `urllib.parse.urlunsplit`,
   `SCRIPT_DIR = Path(__file__).resolve().parents[2]`, `GLOBAL_PERSONAS_DIR` derivado de `__file__`,
   `tempfile.gettempdir()` no lugar de `/home/...`. Objetivo real: portabilidade (o repo vai ser
   clonado em outra máquina — paths da Maiteux quebrariam lá).
3. **`os.environ["PYTHONWARNINGS"]` → `setdefault` (A8)** — mudança semântica mínima e desejável
   (não sobrescreve configuração do usuário).
4. **`print()` → `logging` (A13)** em `visual_qa_actor.py` (ferramenta de CLI): o `basicConfig` foi
   movido para o bloco `if __name__ == "__main__":`, preservando a saída visível no uso direto.
5. **`subprocess.Popen` em `async def` (A11)** — envolvido em `await asyncio.to_thread(...)`.
6. **Tipagem** — `clean_gpu_memory() -> None`, `main() -> None`.
7. **Docstrings (A2)** — docstrings com `Exemplo:`/`>>>` em funções públicas de módulo.
   Os exemplos foram transformados em *doctests reais* e executados com
   `pytest --doctest-modules` nos 9 módulos tocados (0 falhas), mas **não são coletados pela
   suíte padrão** (a Etapa 0 não usa `--doctest-modules`). Exemplos meramente ilustrativos
   (não executáveis, ex.: `>>> fn.__name__` de funções inexistentes) foram removidos —
   regra A2 agora exige docstring em função pública de módulo, sem exemplos decorativos.
8. **Error-path tests reais (prioridade 6)** — testes adicionados em
   `tests/test_needle_ast_guard.py`, `test_needle_noul_validation.py`,
   `test_needle_verify_delivery.py`, `test_server_api.py`, `test_personas_catalog.py`,
   `test_feedback_formatter.py`, `test_validate_opencode_skills.py` usando `pytest.raises`
   contra dependências reais (zero mocks).
9. **Tentativa server-side + histórico escopado (anti-gaming do circuit breaker)** —
   `resolve_attempt_number(hist)` em `src/core/score_gate.py` é a única fonte da contagem;
   `attempt_number` vira telemetria. `history_clear(workspace)` só zera o estado do próprio
   workspace, e a flag `reset_history` saiu da tool/catálogo/IPC/cliente.
   Regressões: `test_client_cannot_forge_emergency_on_first_attempt`,
   `test_history_clear_is_scoped_and_state_dir_is_isolated`.
10. **Workspace sem testes ⇒ REJECTED** — `pytest_chunk_runner.run_all_chunks` devolve
    `pytest_success: false` quando nenhum teste é coletado e a tool rejeita antes das Camadas 1/2
    (regressão: `test_workspace_without_tests_is_rejected`).

---

## 6. Registro de homologação desta entrega

| Campo | Valor |
| :--- | :--- |
| Status | **`APPROVED`** |
| Tentativa | 2 / 5 (server-side — o chamador nem informa a tentativa) |
| Chunks pytest | 4/4 aprovados — 39 + 70 + 88 + 75 = **272 testes, 0 falhas** (276 coletados, 4 skipped) |
| Violações Camada 1 | 0 (`blocking_ast_violations: []`) |
| Alertas Camada 2 | 0 (`structural_ast_warnings: []`) — screener em **230 símbolos / 6526 linhas** |
| Arbitragem Noul | não acionada (Fast-Path: 0 suspeitas ⇒ sem inferência) |
| Execução | ~115 s (soma dos chunks ≈ 90 s; chunk 2 (governança) ≈ 58 s) |
| Payload completo | `/tmp/verify_official.json` (reprodução: passo 3 da seção 4) |

Regressão de suspeitas ao longo da correção: **48 → 38 → 17 → 0**.

---

## 7. Checklist do auditor externo

- [ ] A Etapa 0 realmente executa testes **reais** (nada de stubs/mocks) — olhe
      `src/orchestrator/pytest_chunk_runner.py` e confirme o particionamento.
- [ ] A Camada 1 tem `fail-fast` verdadeiro: nenhuma violação pode ser "só alerta".
- [ ] A Etapa 3 **só** arbitra suspeitas e nunca aprova sozinha: `confirmed_defects` sempre
      derruba `passed`.
- [ ] Nenhum caminho de código permite `passed=true` sem pytest (procure atalhos/early-returns
      em `src/mcp_server.py`).
- [ ] As refatorações por *comprehension* da seção 5 preservam o comportamento original
      (compare com `git diff`).
- [ ] Os exemplos de docstring são verdadeiros ou pelo menos inofensivos.
- [ ] Nenhum mock foi introduuído em `src/` ou em testes (`grep -rn "unittest.mock\|MagicMock" src/ tests/`).
- [ ] Estado do circuit breaker em `/tmp/openjev_state/` não é commitável, é **escopado por
      workspace** (`history_clear(workspace)`), não tem reset global exposto em tool — e a suíte
      corre sob `JEV_STATE_DIR` isolado (`tests/conftest.py`), sem tocar no registro real.
- [ ] Nenhum caminho entrega `EMERGENCY_RELEASE` sem executar pytest + Guard + Screener, e
      `attempt_number` do chamador não muda o resultado (ver `resolve_attempt_number`).
- [ ] Varredura de segredos: `grep -rn "ghp_\|sk-\|AKIA" src/` (apenas as regexes do próprio guard devem aparecer).

---

## 8. Limitações conhecidas (transparência)

1. **Parte da varredura JS é regex-based** (`multi_stack_runner.scan_js_ast`), não AST real de JS/TS.
2. **Paths em arquivos não-Python não entram no screener** — ex.:
   `visual_qa_actor.js` contém `/home/maiteuxzanela/projeto JEV` e continua hardcoded.
3. **A arbitragem Noul depende de hardware** (GTX 1050 Ti com fallback CPU): em máquina sem GPU
   a Etapa 3 fica mais lenta, mas o resultado estrutural (Etapas 0–2) não muda.
4. **`asyncio` resource warnings** pré-existentes na suíte (2 warnings) — não bloqueiam.
5. **Só símbolos git-modified** entram na análise (`extract_workspace_ast_symbols` usa
   `get_git_modified_line_ranges`) — um arquivo nunca tocado não é auditado.
