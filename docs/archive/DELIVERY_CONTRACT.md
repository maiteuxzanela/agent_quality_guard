# Contrato de Entrega e Checklist de Revisão — Core Open JEV

> **Documento Normativo:** `docs/DELIVERY_CONTRACT.md`  
> **Status:** Oficial & Ativo  
> **Governança:** Valério (Lead Architect) & Clara (QA & Governança)  
> **Última Atualização:** Setembro/2026

Todo desenvolvedor humano, orquestrador ou subagente especialista DEVE preencher este checklist formal ao submeter uma alteração de código ou documento para revisão e fechamento.

---

## Template de Submissão de Entrega (Preenchimento Obrigatório)

```markdown
### 📋 Submissão de Entrega Técnica — Projeto JEV

#### 1. Identificação da Demanda
* **Título da Tarefa:** [Descreva sucintamente o objetivo]
* **Tipo da Tarefa:** [feature | bugfix | refactor | docs | infra]
* **Responsável/Persona:** [@Valerio | @Maya | @Kael | @Tulio | @Clara | @Otavio]
* **Número da Tentativa:** [1 a 5]

#### 2. Impacto em Regras de Negócio (`docs/BUSINESS_RULES.md`)
* **Regras Existentes Impactadas:**
  * [Ex: RN-JEV-006: Prevenção de Context Dumping - atualizada extração de snippet]
  * [Ex: RN-JEV-010: Higiene Estrutural - adicionadas regras A11 a A15]
* **Novas Regras Criadas:** [ID e Título ou Nenhuma]
* **Houve quebra de alguma Invariante?** [NÃO / SIM (justificar formalmente)]

#### 3. Contratos de Dados e Máquinas de Estado (`docs/STATE_MACHINES.md`)
* **Alteração em Schemas Pydantic / DTOs:** [NÃO / SIM (listar schemas alterados)]
* **Alteração de Banco de Dados / Migrations:** [NÃO / SIM]
* **Transições de Estado Adicionadas/Alteradas:** [NÃO / SIM (descrever a nova transição)]

#### 4. Evidências Factuais de Testes Reais (Sem Mocks)
* **Comando do Runner em Chunks:**
  * Chunk 1 (ML Isolado): [X passed, 0 failed, time: Ys]
  * Chunk 2 (Governança): [X passed, 0 failed, time: Ys]
  * Chunk 3 (Memória & AST): [X passed, 0 failed, time: Ys]
  * Chunk 4 (API & MCP): [X passed, 0 failed, time: Ys]
  * **Total Geral:** [XXX passed, 0 failed]
* **Testes de Regressão Específicos Adicionados:**
  * `tests/test_...py::test_...`

#### 5. Validação Determinística (`needle_verify_delivery`)
* **Status Final:** [APPROVED | EMERGENCY_RELEASE]
* **Violações Bloqueantes (Camada 1):** [0]
* **Suspeitas Arbitradas pelo Laya Noul (Camada 3):**
  * Confirmadas ($P \ge 0.78$): [X defeitos]
  * Descartadas ($P < 0.78$): [Y alertas de falso-positivo]
* **Alertas Informativos de Telemetria:** [Z alertas]

#### 6. Parecer do Revisor Crítico
* **Veredito:** [PASS | PASS-WITH-CAVEATS]
* **Observações:** [Apontamentos factuais ou aprovação limpa]
```

---

## Diretrizes de Aceite do Revisor Crítico

1. **Rejeição Automática:**  
   Submissões sem preenchimento dos testes factuais ou com testes contendo mocks em `src/` serão rejeitadas sumariamente (`FAIL`).
2. **Invariantes Inegociáveis:**  
   Tentativas de commit antes da aprovação do item 5 violam `RN-JEV-009` e invalidam o ciclo.
3. **Circuit Breaker:**  
   Se o campo `Número da Tentativa` atingir 5, o item 5 pode aceitar `EMERGENCY_RELEASE`, exigindo registro obrigatório das ressalvas no parecer final.
