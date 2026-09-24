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
3. **Decisão Tipada Local (Laya GTX 1050 Ti):** Roteamento categórico de especialistas via `openjev_choice` com roster $\le 10$ opções.
4. **Quality Gate de Fechamento:** Validação obrigatória com JEV Score $\ge 3.40$ (Pedido vs Resultado factual $\le 1500$ chars).

---

## 2. Matriz da Equipe e Especialistas

| Subagente | Documento de Persona | Especialidade | Quando Acionar via `openjev_choice` |
| :--- | :--- | :--- | :--- |
| **Valério** | [`00_valerio_lead_architect.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/00_valerio_lead_architect.md) | Orquestrador Geral & Lead Architect | Braço direito de Maiteux Zanela; triagem executiva, decomposição de tarefas e despacho de demandas. |
| **@Kael** | [`01_kael_ml_hardware.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/01_kael_ml_hardware.md) | ML Local, PyTorch & Hardware Pascal | Modelo Laya, profiling da GTX 1050 Ti (4GB VRAM), bloqueio de `bfloat16`, coercão `float16`/`float32` e CPU fallback. |
| **@Maya** | [`02_maya_ast_memory.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/02_maya_ast_memory.md) | Memória Relacional & AST (Needle 3) | Extração cirúrgica de código, parsing de AST Python, grafos de chamada e combate a context dumping. |
| **@Túlio** | [`03_tulio_mcp_api.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/03_tulio_mcp_api.md) | Protocolos MCP, APIs FastAPI & Schemas | Servidor MCP (`src/mcp_server.py`), API HTTP (`src/decision/server.py`), contratos Pydantic e chamadas assíncronas. |
| **@Clara** | [`04_clara_qa_governance.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/04_clara_qa_governance.md) | Quality Gate, Anti-Mock & Calibração | **Guardiã do Quality Gate**. Testes reais, calibração ordinal Laya, Borderline Mass Gate e travas anti-gaming. |
| **@Otávio** | [`05_otavio_audit_bridge.md`](file:///home/maiteuxzanela/projeto%20JEV/personas/05_otavio_audit_bridge.md) | Auditoria Multi-Repo & Pontes de Cliente | Scripts `audit_*.py`, biblioteca `jev-client-bridge`, stop hooks e integração com repositórios externos. |

---

## 3. Roteamento Formal via Open Jev e Despacho de Subagentes

Quando uma tarefa técnica exigir implementação (Nível 2), o Orquestrador Valério formula o roteamento em `openjev_choice` e despacha em contexto isolado via `openjev_spawn_subagent`:
```python
# 1. Eleição formal do especialista via Laya RLCD
selected = openjev_choice(
    premise="Implementar novo endpoint de streaming no FastAPI",
    options=["kael", "maya", "tulio", "clara", "otavio"]
)

# 2. Despacho autônomo com contexto limpo (Hermes Agent)
result = openjev_spawn_subagent(
    persona=selected,
    task="Criar rota SSE em src/decision/server.py com testes unitários",
    workspace="/home/maiteuxzanela/projeto JEV",
    auto_validate=True
)
```
O especialista eleito assume a titularidade da execução técnica com contexto isolado, sem carregar o histórico de conversa do orquestrador, e o resultado é verificado deterministicamente antes do retorno.

