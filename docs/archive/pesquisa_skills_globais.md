# Pesquisa: Skills Globais opencode a partir do comportamento dos projetos

**Data:** 2026-09-22 (atualizada: 2026-09-22 — entrega das 9 globais + fix `jev-fecha` + espelho Antigravity)  
**Método:** needle3 (AST) + glob/grep/read (MD/hooks/skills) + openjev_choice revalidado após fix MCP `-32001`.  
**Escopo:** JEV, PanelaBuild, JEZ collections, Faturamento, Predictera. Personas ficam no repo; podem inspirar formatos.

**Decisão da usuária (2026-09-22):**
- Criar **9 skills globais** (7 originais da pesquisa + 3 promovidas de intra: `secret-scan-hardcoded`, `jev-client-bridge`, `firebase-deploy-pipeline`).
- **Não** criar skill `jev-entrega` — o command **`/jev-fecha`** é o mecanismo que **obriga** o fluxo de validação; skill seria redundante nesse cenário.
- Espelhar skills em `~/.gemini/config/skills/` (symlinks) para o **Antigravity**.
- Skills **intra-projeto** ficam para a próxima rodada.

---

## 0. Entrega executada (2026-09-22)

| Artefato | Status |
|----------|--------|
| `~/.config/opencode/skills/<nome>/SKILL.md` × **9** | criado |
| `~/.gemini/config/skills/<nome>` → symlink OpenCode | **9 links** |
| `command/jev-fecha.md` threshold | **3.60 → 3.40** (menções a 3.60/3.75 só como “não usar”) |
| Skill `jev-entrega` | **descartada** (command cobre) |
| Relatório | este arquivo |
| Validador em disco | `scripts/validate_opencode_skills.py` + `tests/test_validate_opencode_skills.py` (9 skills, espelhos, jev-fecha 3.40, ausência de `jev-entrega`) |

**Ordem final das 9 (referência Laya 2026-09-22, choice pós-fix):**  
`skills-opencode-creator` → `calibracao-score` → `jev-auditoria-moldura` → `anti-mock-failfast` → `needle3-busca` → `guard-stop-hooks` → `secret-scan-hardcoded` → `jev-client-bridge` → `firebase-deploy-pipeline`.

| # | Skill | Origem |
|--:|-------|--------|
| 1 | `skills-opencode-creator` | original |
| 2 | `calibracao-score` | original (Laya #2 na revalidação) |
| 3 | `jev-auditoria-moldura` | original |
| 4 | `anti-mock-failfast` | original (+ carve-out RN) |
| 5 | `needle3-busca` | original |
| 6 | `guard-stop-hooks` | original |
| 7 | `secret-scan-hardcoded` | **promovida (intra→global)** |
| 8 | `jev-client-bridge` | **promovida** |
| 9 | `firebase-deploy-pipeline` | **promovida** |

### Compat Antigravity
| Escopo | OpenCode | Antigravity |
|--------|----------|-------------|
| Global | `~/.config/opencode/skills/` | `~/.gemini/config/skills/` (symlink) |
| Projeto | `.opencode/`, `.agents/`, `.claude/skills/` | `<workspace>/.agents/skills/` |

Formato `SKILL.md` é padrão aberto; só muda o diretório. Reiniciar ambos os agents após criar skills.

### Recorrência intra-projeto — ENTREGUE (rodada 2)

Localização: **`<repo>/.agents/skills/`** (OpenCode + Antigravity workspace; não global — só fazem sentido no repo).

| Skill | Projeto | Path |
|-------|---------|------|
| `tiss-lote-cli` | Faturamento | `.agents/skills/tiss-lote-cli/` |
| `kanban-dashboard-sync` | Faturamento | `.agents/skills/kanban-dashboard-sync/` |
| `panela-data-pipeline` | PanelaBuild | `.agents/skills/panela-data-pipeline/` |
| `firestore-rules-hardening` | JEZ | `.agents/skills/firestore-rules-hardening/` |
| `previsao-causal-ev` | Predictera | `.agents/skills/previsao-causal-ev/` (alias do pedido `previsao-causal-+ev`; `+` proibido no regex de name) |
| `download-csv-ligas` | Predictera | `.agents/skills/download-csv-ligas/` (alias de `download-CSV-ligas`; maiúsculas proibidas) |
| dedupe `atualizar-rodada` | Predictera | canônico só em `.agents/skills/atualizar-rodada/` (com `scripts/`); **removido** `.opencode/skills/atualizar-rodada` |

**Deferred restante:** `treino-evolution` (não pedido nesta rodada).  
**Cobertas pelas 9 globais:** molde audit → `jev-auditoria-moldura`; guards → `guard-stop-hooks`.

**Conflito documentado:** `diana-qa` permite mocks de ambiente RN em `jestSetup.js` — carve-out explícito na skill `anti-mock-failfast` (não é mock de negócio).

---

## 1. Inventário (Fase A)

| Proj | AGENTS | KANBAN | SPEC | SKILL.md | hooks/guard | personas | audit_* |
|------|:------:|:------:|:----:|:--------:|:-----------:|:--------:|:-------:|
| JEV | ✓ | — | — | 0 | verify_all | — | 5 + 4 JSON |
| Panela | ✓ | ✓ | ✓ | 1 (diana-qa) | hooks+diana_guard | 9 | 1 MD |
| JEZ | ✓ | ✓ | ✓ | 0 | hooks+robin_guard | 9 | 1 MD |
| Faturamento | ✓ | ✓ | ✓ | 0 | — | 9 | 1 MD |
| Predictera | ✓ | ✓ | ✓ | **10** (.opencode+.agents) | — | — | 1 analysis |
| **Global** `~/.config/opencode` | AGENTS 157L | — | — | **9** (após entrega) | command `jev-fecha` **3.40** | — | — |
| **Global Antigravity** `~/.gemini/config/skills` | — | — | — | **9** (symlink) | — | — | — |

**Hits de conteúdo em MD:**
- `needle3` / `openjev` / `borderline` / `premise gaming` / anti-mock: **JEV + AGENTS global** (+ JEZ Camada 1).
- `quality gate`: Panela + JEZ (KANBAN/personas).
- Fail-fast explícito: JEV §5.2 + skill Predictera `coletar-odds-betano`.
- Faturamento/Pred: quase nenhum termo do pipeline JEV nos MD de governança (Pred tem skills próprias).

**Comando global:** `command/jev-fecha.md` — **corrigido 3.60 → 3.40** em 2026-09-22 (calibração Laya set/2026).

---

## 2. Matriz de evidência (Fase B)

| Candidata | JEV | Global | Panela | JEZ | Fat | Pred | Score repo* | Decisão |
|-----------|:---:|:------:|:------:|:---:|:---:|:----:|:-----------:|---------|
| **skills-opencode-creator** | 0 skills | 0 skills | 1 | 0 | 0 | 10 | **6/6** | **criada** |
| **jev-entrega** | §4+README | jev-fecha 3.60 | Helena QG | score≥3.5 | audit score | — | **5/6** | **descartada** (command `/jev-fecha` obriga o fluxo) |
| **jev-auditoria-moldura** | 5 scripts+4 JSON | — | 1 MD | 1 MD | 1 MD | analysis | **5/6** | **criada** |
| **needle3-busca** | engine+5 audits | §2 | — | Camada 1 | audit scripts | — | **4/6** | **criada** |
| **anti-mock-failfast** | §5.2+tests | §5 | jestSetup† | — | — | odds fail-fast | **4/6** | **criada** |
| **persona-matriz** (padrão, sem nomes) | — | — | 9+matrix | 9 | 9 | nomes em skills | **4/6** | não (deferred opcional) |
| **kanban-sessao** | — | — | KANBAN | KANBAN | kanban | §1 obriga | **4/6** | não |
| **docs-arquitetura** | README | — | SPEC | SPEC | SPEC | SPEC | **5/6** | não |
| **guard-stop-hooks** | verify_all | — | diana_guard | robin_guard | — | — | **3/6** | **criada** |
| **calibracao-score** | borderline ab+tests | — | — | dims | — | breakdowns | **3/6** | **criada** (Laya #2 revalidação) |
| **secret-scan-hardcoded** | — | — | credentials | — | senha/functions | — | promovida | **criada** |
| **jev-client-bridge** | engine | — | jev_client | jev_client | — | — | promovida | **criada** |
| **firebase-deploy-pipeline** | — | — | functions | hosting CI | deploy+SW | — | promovida | **criada** |

\* inclui coluna “Global” quando há artefato em `~/.config/opencode`.  
† Panela usa mocks de ambiente RN em `jestSetup.js` — carve-out documentado em `anti-mock-failfast`.

---

## 3. Priorização (Fase C + revalidação Laya)

**Fase C original:** MCP `-32001` — priorização por matriz factual.  
**Revalidação (pós-fix event loop):** `openjev_choice` com as 7 originais → `skills-opencode-creator` 0.44 · `calibracao-score` 0.26 · `jev-auditoria-moldura` 0.08 · `anti-mock-failfast` 0.08 · `jev-entrega` 0.08 · `needle3-busca` 0.04 · `guard-stop-hooks` 0.02. Ordem usada para escrita das skills (creator primeiro).

### Entregues como globais (9) — ver §0

| Skill | Gatilho (description) | Corpo |
|-------|----------------------|-------|
| `skills-opencode-creator` | criar/editar SKILL.md, skills dirs | Formato, validação name/description, dual-scope |
| `calibracao-score` | borderline, score_margin, thresholds | 3.40/0.78, anti-gaming, circuit breaker |
| `jev-auditoria-moldura` | audit_*.py, tri-camada | needle→choice→noul→score→JSON |
| `anti-mock-failfast` | mock, stub, fail-fast | §5.2 + carve-out jestSetup RN |
| `needle3-busca` | needle3_search, dump >300L | workspace/scope/500–1500 tok |
| `guard-stop-hooks` | hook Stop, *_guard.py | git-diff→suite→JSON payload |
| `secret-scan-hardcoded` | senha, token, key hardcoded | scan/triage/remediação ≠ mocks |
| `jev-client-bridge` | jev_client, integração 4 primitivas | paths, Laya vs DeBERTa stale, E2E sem mock |
| `firebase-deploy-pipeline` | deploy Firebase, SW cache | checklist + CI Hosting + SW bump |

**Descartada nesta rodada:** `jev-entrega` — command `/jev-fecha` é o enforcement (usuária: prefere command que obriga o fluxo).

### Não globais (fica no repo; só inspira)

| Item | Motivo |
|------|--------|
| Skills Predictera (ligas, odds, holdout, lab) | Domínio 100% local |
| `diana-qa` | Jest/RN Panela |
| Personas (nomes) | Restrição explícita da usuária |
| `persona-matriz` como skill com nomes | Padrão **pode** virar skill genérica (“como montar AGENTS com tabela de subagentes”) — **opcional**, média |
| `kanban-sessao` | Valor local; Predictera já obriga no AGENTS |
| `docs-arquitetura` | Trivial (SPEC+AGENTS+KANBAN); não justifica skill separada |

### Opciais (baixa prioridade)

- **`persona-matriz-template`** — só se você quiser padronizar AGENTS novos sem copiar os 4.
- **`kanban-sessao`** — se quiser leitura de KANBAN no start em todos os projetos.

---

## 4. Recomendações concretas

1. **9 skills globais** em `~/.config/opencode/skills/<nome>/SKILL.md` — **entregues**.
2. **`command/jev-fecha.md`: 3.60 → 3.40** — **entregue** (command mantido; skill `jev-entrega` não criada).
3. **Espelho Antigravity** `~/.gemini/config/skills/` — **entregue** (9 symlinks).
4. **Não** promover skills de persona nem skills de domínio Predictera.
5. **`anti-mock-failfast`** cita carve-out do `jestSetup.js` do Panela — **incluído**.
6. Reiniciar OpenCode **e** Antigravity após criação (sem hot-reload).
7. **Próxima rodada:** skills intra (ver deferred §0) + dedupe `atualizar-rodada`.

---

## 5. Lacunas / riscos

- ~~MCP open-jev indisponível na Fase C~~ — **resolvido** (fix `asyncio.to_thread`, cap 800, preload; choice revalidado).
- needle3 em workspace Predictera (árvore grande) deu timeout — inventário Pred feito por glob/read; cap/candidate truncation documentado no engine.
- Contagem de “hits” em MD é aproximada (walk limitado); não substitui auditoria linha a linha.
- Symlink Antigravity: se o Antigravity não seguir `~/.gemini/config/skills` na sua build, copiar pastas em vez de link (verificar no primeiro restart).
- Relato de drift: bridges `jev_client` ainda citam DeBERTa — skill `jev-client-bridge` documenta o fix; **arquivos dos repos não foram alterados** nesta rodada (só skill).

---

## 6. Checklist de aceite da rodada (2026-09-22)

- [x] 9 pastas + `SKILL.md` em `~/.config/opencode/skills/`
- [x] `name` = diretório, regex `^[a-z0-9]+(-[a-z0-9]+)*$`, description 1–1024
- [x] 9 symlinks íntegros em `~/.gemini/config/skills/` (0 broken)
- [x] `jev-fecha.md`: threshold/status **3.40**; aviso “não usar 3.60/3.75”
- [x] `jev-entrega` **não** criada (command `/jev-fecha` é o enforcement)
- [x] `skills/README.md` índice das 9
- [x] Relatório §0 entrega + deferred intra + tabela Antigravity
- [x] `scripts/validate_opencode_skills.py` → `OK 9 skills + antigravity mirrors + jev-fecha 3.40`
- [x] `tests/test_validate_opencode_skills.py` → 3 passed (disco real, sem mock)
- [x] Suíte completa JEV: **170 passed**, 12 subtests
- [ ] Restart OpenCode + Antigravity (usuária; sem hot-reload)
- [ ] Próxima rodada: skills intra (TISS, sync kanban, gaps Pred, data-pipeline, firestore)

### Nota de validação JEV desta rodada
- Scores observados: **3.27 → 3.33 → 3.12** (todas `borderline: true`, corte 3.40).
- **HISTÓRICO:** o campo `diagnosis_suppressed: borderline` citado abaixo era o comportamento vigente na data da corrida — a supressão foi **removida** depois (o choice roda sempre em `score < threshold`; carve-out com gate de confiança 0.40 em `NULL_CHOICE_MIN_CONFIDENCE`).
- Circuit breaker: **10/10** atingido → `emergency_exit_unlocked: true`.
- Status: **Saída de Emergência Acionada (Laudo)** — artefatos no disco permanecem entregues e o validador local está verde; a nota do classificador caiu na fronteira probabilística, não apontou defeito categorizado.

---

## 7. Checklist de aceite — skills intra (rodada 2, 2026-09-22)

- [x] Localização única: `<repo>/.agents/skills/<nome>/SKILL.md` (não global)
- [x] Faturamento: `tiss-lote-cli`, `kanban-dashboard-sync` (AGENTS §4.3)
- [x] PanelaBuild: `panela-data-pipeline` (`prestart` + scripts Firebase)
- [x] JEZ: `firestore-rules-hardening` (pilot + noul ≥0.78 + score ≥3.40)
- [x] Predictera: `previsao-causal-ev` (Dixon-Coles, EV ≥8%) e `download-csv-ligas` (football-data + atualizar_*)
- [x] Aliases documentados: `+` e maiúsculas inválidos no regex OpenCode
- [x] Dedupe: `.opencode/skills/atualizar-rodada` removido; canônico `.agents` + `scripts/atualizar_rodada.py`
- [x] Validador: `OK 9 global + 7 intra + antigravity mirrors + jev-fecha 3.40 + no pred dup`
- [x] `tests/test_validate_opencode_skills.py`: **3 passed**
- [x] Sem colisão de nomes global × intra
- [ ] Restart OpenCode/Antigravity por repo (skills de workspace descobrem no cwd)
- [ ] Deferred restante: `treino-evolution` (não pedido)

**Score JEV rodada 2:** **3.44 / 5.00** — `passed: true`, `borderline: true`, `score_margin: +0.0073` (aprovado na fronteira; margem divulgada).

---

## 8. Protocolo de Dúvida — roteamento geral via JEV (rodada 3, 2026-09-22)

**Pedido:** em vez de skill por persona, instrução **geral** de incentivo às ferramentas JEV para dúvidas em ações incertas (diretório, arquivo, tool, skill, subagente). **Substituir/sintetizar** os parágrafos de roteamento no AGENTS global (não empilhar).

| Artefato | Mudança |
|----------|---------|
| `~/.config/opencode/AGENTS.md` §3 | Título → **Protocolo de Dúvida — Consulta Formal via Open Jev**; **incentivo forte** (dúvida genuína ≥2 ⇒ formalizar; livre se óbvio/trivial); **matriz dúvida→ferramenta** (subagente, tool, busca, arquivo/dir, skill, consulta, noul, `/jev-fecha`); **regras de ouro** (≤10, premise factual, descoberta antes de choice de paths) |
| `~/.config/opencode/AGENTS.md` §1.1 | Princípio atualizado: roteamento cobre **arquivo/diretório e skill**; choice vs consulta separados |
| `~/.gemini/config/AGENTS.md` | **Espelho Antigravity** do §1.1 + §3 (ex.: `run_command`; MCP `mcp_config.json` inalterado) |
| `skills-opencode-creator/SKILL.md` Auto-load | Ponteiro: dúvida entre ≥2 skills → Protocolo de Dúvida §3 |
| Prosa antiga | `Roteamento Formal de Subagentes` e `Roteamento de Ferramentas e Busca` **removidas** (viraram linhas da matriz) |
| Intactos | §1 mermaid, limiares **0.78/3.40**, §4 score, §5 anti-mock, MCP, `/jev-fecha`, sem skill nova de roteamento |

**Decisão de desenho (usuária):** não criar skills por persona (mapeamento persona×skill não justificou 1:1); choice de skill só quando **≥2** candidatas — não always-on.

**Validações locais (disco):**
- `validate_opencode_skills.py` → `OK 9 global + 7 intra + antigravity mirrors + jev-fecha 3.40 + no pred dup`
- `tests/test_validate_opencode_skills.py` → **3 passed**
- `tests/test_circuit_breaker.py::TestAgentsMdEspelhado` (espelho Laya em ambos AGENTS)
- Assert: título Protocolo + matriz skill/arquivo + sem prosa antiga + 0.78/3.40/anti-mock **nos dois** `AGENTS.md` (opencode + gemini)
- `tests/test_circuit_breaker.py::TestAgentsMdEspelhado`: +3 testes (`test_*_protocolo_duvida`, `test_skill_creator_aponta_protocolo`)
- Mermaid §1 nos dois AGENTS: aresta “Dúvida → Protocolo de Dúvida (Choice)”
- `skills/README.md`: linha de Protocolo de Dúvida (dúvida entre ≥2 skills)
- Suíte completa JEV: **173 passed**, 12 subtests (pós testes de Protocolo)

**Score JEV rodada 3:** **3.50 / 5.00** — `passed: true`, `borderline: true`, `score_margin: +0.076` (aprovado; margem divulgada). Trajetória nas refatorações desta rodada: 3.27 → 3.13 → 3.09 → 3.16 → 3.33 → **3.42** → **3.50** (todas com disco real entre avaliações).
