# Diretório Oficial de Personas — Equipe do Projeto JEV

Este diretório estabelece a matriz de personas e subagentes especialistas que operam o **Core Engine do Projeto JEV (RLCD Laya, Cactus Needle 3 & Protocolos MCP/FastAPI)**.

> **Liderança Técnica & Orquestração:** [Valério — Lead AI Architect & Orquestrador Geral](file:///home/maiteuxzanela/projeto%20JEV/personas/00_valerio_lead_architect.md)  
> **Fundadora & Engenheira Líder:** [Maiteux Zanela](file:///home/maiteuxzanela/projeto%20JEV/AGENTS.md)  
> **Projeto:** JEV (Motor Central de Decisão Discriminativa, Memória AST e Ferramental Universal)  
> **Governança do Repositório:** [AGENTS.md](file:///home/maiteuxzanela/projeto%20JEV/AGENTS.md)

---

## 1. Filosofia Operacional e Arquitetura Tri-Camada

O Projeto JEV não é apenas um repositório isolado: ele é a infraestrutura de inteligência e busca cirúrgica que alimenta todos os projetos do ecossistema (**Panela de Sobra**, **JEZ Collections**, **Faturamento Médico** e **Predictera**).

Para manter a integridade do motor sob alta demanda, os agentes seguem a governança inegociável:
1. **Zero Mocks (Fail-Fast):** Proibição absoluta de simulações, stubs e mocks em testes e produção.
2. **Busca Cirúrgica AST (Needle 3):** Contexto estritamente limitado a 500–1.500 tokens; proibido despejo de monólitos (>300 linhas).
3. **Decisão Tipada Local (Laya GTX 1050 Ti):** Especialistas eleitos por `openjev_choice` (Protocolo de Dúvida — só com $\ge 2$ candidatos genuínos) com roster $\le 10$ opções; com 1 candidato claro, despacha-se direto.
4. **Homologação Determinística de Fechamento:** Validação pela suíte de testes reais da stack e auditoria da Diana (com `needle_verify_delivery` de uso exclusivo da Diana quando aplicável).

---

## 2. Matriz da Equipe e Especialistas

> **Nota (set/2026):** [Valério](file:///home/maiteuxzanela/projeto%20JEV/personas/00_valerio_lead_architect.md) é o **Orquestrador** — atua como Capitão nas sessões de equipe do AgentTeams e não deve ser despachado como subagente. A matriz abaixo lista os especialistas do squad.

| Subagente | Documento de Persona | Especialidade | Quando Acionar |
| :--- | :--- | :--- | :--- |
| **@Kael** | [`01_kael_ml_hardware.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/01_kael_ml_hardware.md) | ML Local, PyTorch & Hardware Pascal | Modelo Laya, profiling da GTX 1050 Ti (4GB VRAM), bloqueio de `bfloat16`, coercão `float16`/`float32` e CPU fallback. |
| **@Maya** | [`02_maya_ast_memory.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/02_maya_ast_memory.md) | Memória Relacional & AST (Needle 3) | Extração cirúrgica de código, parsing de AST Python, grafos de chamada e combate a context dumping. |
| **@Túlio** | [`03_tulio_mcp_api.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/03_tulio_mcp_api.md) | Protocolos MCP, APIs FastAPI & Schemas | Servidor MCP (`src/mcp_server.py`), API HTTP (`src/decision/server.py`), contratos Pydantic e chamadas assíncronas. |
| **@Clara** | [`04_clara_qa_governance.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/04_clara_qa_governance.md) | Quality Gate, Anti-Mock & Calibração | **Guardiã do Quality Gate**. Testes reais, calibração ordinal Laya, Borderline Mass Gate e travas anti-gaming. |
| **@Otávio** | [`05_otavio_audit_bridge.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/05_otavio_audit_bridge.md) | Auditoria Multi-Repo & Pontes de Cliente | Scripts `audit_*.py`, biblioteca `jev-client-bridge`, stop hooks e integração com repositórios externos. |

---

## 3. Orquestração Mandatória de Squad via AgentTeams

Em conformidade com o Protocolo Mandatório de Multi-Agente (~/.dsh/AGENTS.md), tarefas que exijam debate técnico, múltiplos papéis ou colaboração de squad devem operar compulsoriamente via plugin **AgentTeams** (`agent_teams_create`).

Havendo **dúvida genuína** entre $\ge 2$ especialistas plausíveis para compor o time, a eleição pode ser auxiliada probabilisticamente com `openjev_choice`:

```python
# Eleição formal via Laya RLCD (opcional — em caso de dúvida na formação do squad)
selected = openjev_choice(
    premise="Implementar novo endpoint de streaming no FastAPI",
    options=["kael", "maya", "tulio", "clara", "otavio"]
)
```

Cada especialista atua com sua persona carregada diretamente no motor do AgentTeams, trocando mensagens técnicas via mailbox durável sob governança por DAG. O retorno técnico é verificado deterministicamente (`needle_verify_delivery`) antes da homologação final.

Especialidade ausente no roster ⇒ propor nova persona em `personas/<nome>.md` (expansão orgânica), nunca improvisar generalista.

