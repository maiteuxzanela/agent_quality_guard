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
4. **Homologação Determinística de Fechamento:** Validação com `needle_verify_delivery` (1º Pytest em Chunks Sequenciais $\rightarrow$ 2º NeedleASTGuard $\rightarrow$ 3º NeedleASTScreener).

---

## 2. Matriz da Equipe e Especialistas

> **Nota (set/2026):** [Valério](file:///home/maiteuxzanela/projeto%20JEV/personas/00_valerio_lead_architect.md) é o **Orquestrador** — não é subagente e **não deve ser despachado** via `openjev_spawn_subagent` (colocá-lo em janela isolada inverte a arquitetura). A matriz abaixo lista apenas os especialistas despacháveis.

| Subagente | Documento de Persona | Especialidade | Quando Acionar |
| :--- | :--- | :--- | :--- |
| **@Kael** | [`01_kael_ml_hardware.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/01_kael_ml_hardware.md) | ML Local, PyTorch & Hardware Pascal | Modelo Laya, profiling da GTX 1050 Ti (4GB VRAM), bloqueio de `bfloat16`, coercão `float16`/`float32` e CPU fallback. |
| **@Maya** | [`02_maya_ast_memory.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/02_maya_ast_memory.md) | Memória Relacional & AST (Needle 3) | Extração cirúrgica de código, parsing de AST Python, grafos de chamada e combate a context dumping. |
| **@Túlio** | [`03_tulio_mcp_api.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/03_tulio_mcp_api.md) | Protocolos MCP, APIs FastAPI & Schemas | Servidor MCP (`src/mcp_server.py`), API HTTP (`src/decision/server.py`), contratos Pydantic e chamadas assíncronas. |
| **@Clara** | [`04_clara_qa_governance.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/04_clara_qa_governance.md) | Quality Gate, Anti-Mock & Calibração | **Guardiã do Quality Gate**. Testes reais, calibração ordinal Laya, Borderline Mass Gate e travas anti-gaming. |
| **@Otávio** | [`05_otavio_audit_bridge.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/05_otavio_audit_bridge.md) | Auditoria Multi-Repo & Pontes de Cliente | Scripts `audit_*.py`, biblioteca `jev-client-bridge`, stop hooks e integração com repositórios externos. |

---

## 3. Despacho de Subagentes Sob Demanda (Choice Opcional)

O roteamento obrigatório está **suspenso** (Global AGENTS.md §3.1): o Orquestrador Valério tem autonomia plena para executar diretamente e aciona especialistas via `openjev_spawn_subagent` **sob demanda**, quando o isolamento de contexto ou a especialidade pontual for conveniente.

Havendo **dúvida genuína** entre $\ge 2$ especialistas plausíveis, a eleição é formalizada com `openjev_choice` (Protocolo de Dúvida); havendo **1 candidato claro**, despacha-se direto:

```python
# 1. (Opcional) Eleição formal via Laya RLCD — só com dúvida genuína
selected = openjev_choice(
    premise="Implementar novo endpoint de streaming no FastAPI",
    options=["kael", "maya", "tulio", "clara", "otavio"]
)

# 2. Despacho em contexto isolado, com contrato de entrega da persona
result = openjev_spawn_subagent(
    persona=selected,            # ex.: "clara" (resolve persona do projeto ou catálogo global)
    task="Criar rota SSE em src/decision/server.py com testes unitários reais",
    workspace="/home/maiteuxzanela/projeto JEV",
    auto_validate=True           # AST Guardrail + Score no retorno
)
```

O especialista assume a execução em janela isolada, **sem carregar o histórico da conversa do orquestrador** (o `task` deve conter todo o contexto necessário), e o retorno segue a seção **Contrato de Entrega** de cada persona — verificado deterministicamente (`needle_verify_delivery`) antes de voltar ao orquestrador.

Especialidade ausente no roster ⇒ propor nova persona em `personas/<nome>.md` (expansão orgânica), nunca improvisar generalista.

