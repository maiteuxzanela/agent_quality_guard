"""Definições de ferramentas (Tool Definitions) padronizadas para agentes e LLMs orquestradoras.
Compatível com OpenAI Function Calling, Anthropic Tool Use e Gemini Function Declarations.
"""

from typing import Any, Dict, List

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "needle3_search",
            "description": (
                "Aciona o motor Cactus Needle 3 para busca cirúrgica léxico-AST local "
                "(grafo de dependências, não embeddings) em código, documentação densa ou logs. "
                "Retorna trechos com preservação de relações de AST (chamadores, implementações, imports) "
                "e controle estrito de janela de contexto (max_tokens)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Consulta técnica ou semântica sobre o que precisa ser localizado ou compreendido."
                    },
                    "scope": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista opcional de diretórios, módulos ou caminhos para restringir a busca."
                    },
                    "max_tokens": {
                        "type": "integer",
                        "default": 1000,
                        "description": "Teto máximo de tokens para o contexto retornado (canônico 1000; evita sobrecarregar a LLM)."
                    },
                    "relational_depth": {
                        "type": "integer",
                        "default": 2,
                        "description": "Profundidade de dependências no grafo de símbolos (1 = apenas o símbolo, 2 = chamadores diretos, etc.)."
                    },
                    "preserve_relational_integrity": {
                        "type": "boolean",
                        "default": True,
                        "description": "Garante que declarações e referências acopladas sejam trazidas em bloco coeso."
                    }
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "openjev_noul",
            "description": (
                "Executa o modelo Open Jev local para avaliar uma condição lógica/booleana com probabilidade calibrada. "
                "Uso OPCIONAL: a exigência de conferência de segurança pré-ação foi removida (anti premise gaming). "
                "Se usada, reformular a premissa/hipótese para inverter o resultado é proibido; "
                "resultado desfavorável exige mudança de atitude e alerta, não contornar a avaliação."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "premise": {
                        "type": "string",
                        "description": "O comando exato, plano de ação ou estado atual a ser executado."
                    },
                    "hypothesis": {
                        "type": "string",
                        "description": "A condição de segurança ou regra a validar (ex.: 'Esta operação é segura e não causa perda de dados irreversível')."
                    },
                    "threshold": {
                        "type": "number",
                        "default": 0.78,
                        "description": "Limiar mínimo de probabilidade para aprovação da ação (guardrail calibrado 0.78)."
                    }
                },
                "required": ["premise", "hypothesis"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "openjev_choice",
            "description": (
                "Distribuição probabilística de escolha (Laya RLCD local) entre subagentes, "
                "ferramentas, skills, arquivos ou caminhos — não é roteamento determinístico. "
                "Retorna selected, confidence e probabilities para a Protocolo de Dúvida (§3)."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "premise": {
                        "type": "string",
                        "description": "A descrição detalhada do problema, bug, requisito ou subtarefa a ser resolvida."
                    },
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Lista de opções de roteamento disponíveis (ex.: ['backend', 'frontend', 'security', 'devops', 'documentation'])."
                    },
                    "temperature": {
                        "type": "number",
                        "default": 1.0,
                        "description": "Fator de calibração térmica do softmax (padrão 1.0)."
                    }
                },
                "required": ["premise", "options"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "openjev_score_and_diagnose",
            "description": (
                "Avalia a qualidade de um artefato técnico com score contínuo (1.0–5.0) usando "
                "o Screener estrutural Needle 3 (10 categorias reais: 8 duras + 2 de análise) "
                "arbitrado pelo JEV Noul (P >= 0.78), sem choice. Score >= corte: passed=true "
                "(defeito duro confirmado ainda reprova). Score na banda de indiferença "
                "[corte - 0.20, corte): passed=true com approved_with_caveat=true apenas com "
                "zero suspeitas; abaixo da banda: passed=false mesmo sem suspeita. Suspeita "
                "confirmada pelo Noul, ou não-resolvida com score < corte: passed=false com "
                "diagnosis (categoria, arquivo, linhas e candidate_defects) para auto-correção "
                "precisa — no segundo caso a suspeita deve ser INVESTIGADA mesmo com Noul "
                "negativo. "
                "Circuit breaker após 5 tentativas. "
                "Ferramenta única de score do pipeline (a tool openjev_score foi expurgada). "
                "Premissa: padrão 'Pedido | Resultado', "
                "máximo 1500 caracteres; rubrica via task_type do catálogo fechado; retorna "
                "score_margin/borderline."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "premise": {
                        "type": "string",
                        "description": (
                            "Pedido original concatenado com resultado executado (arquivos modificados, "
                            "testes, commits), padrão factual 'Pedido do Prompt: ... | Resultado Executado: "
                            "...', máximo 1500 caracteres."
                        ),
                    },
                    "task_type": {
                        "type": "string",
                        "default": "codigo_producao",
                        "description": (
                            "Chave do catálogo fechado RUBRIC_CATALOG: codigo_producao (padrão único). "
                            "Rubrica livre é ignorada."
                        ),
                    },
                    "rubric": {
                        "type": "string",
                        "description": (
                            "DEPRECIADO e IGNORADO pelo servidor — a rubrica vem exclusivamente "
                            "do catálogo fechado por task_type (rubric gaming proibido)."
                        ),
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Caminho absoluto do repositório Git para rastreamento anti-premise gaming. Padrão: cwd.",
                    },
                    "min_score": {"type": "number", "default": 1.0, "description": "Nota mínima da escala."},
                    "max_score": {"type": "number", "default": 5.0, "description": "Nota máxima da escala."},
                    "threshold": {
                        "type": "number",
                        "default": 3.40,
                        "description": (
                            "Nota de corte. Sem suspeita estrutural do Needle 3, score na banda de "
                            "indiferença [3.20, 3.40) aprova com ressalva (approved_with_caveat); "
                            "abaixo de 3.20 reprova mesmo sem suspeita; com suspeita (confirmada "
                            "pelo Noul ou não-resolvida), score < threshold reprova e exige "
                            "investigação. Padrão Laya calibrado: 3.40."
                        ),
                    },
                    "reset_history": {
                        "type": "boolean",
                        "default": False,
                        "description": "Se verdadeiro, zera o histórico de tentativas e cache para iniciar um ciclo limpo na Tentativa 1.",
                    },
                },
                "required": ["premise"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "openjev_consulta",
            "description": (
                "Consulta direta ao Open Jev local (ex.: 'esse script funciona?', "
                "'esse banco de dados tem essa informação?'). Recebe a evidência real e retorna "
                "um indício textual (probability/confidence) SEM booleano de aprovação. "
                "A resposta é indício para investigação, não veredito: premise gaming proibido; "
                "indício desfavorável exige investigação e alerta à usuária."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "premise": {
                        "type": "string",
                        "description": (
                            "Evidência objetiva: trecho do script, schema do banco, amostra de dados, "
                            "estado real observado — nunca uma versão \"suavizada\" da realidade."
                        ),
                    },
                    "question": {
                        "type": "string",
                        "description": "Pergunta direta a ser respondida como indício (ex.: 'Este script funciona?').",
                    },
                },
                "required": ["premise", "question"],
            },
        },
    },
]


def get_tool_definition(name: str) -> Dict[str, Any]:
    """Recupera a definição de uma ferramenta específica pelo nome."""
    for tool in TOOL_DEFINITIONS:
        if tool["function"]["name"] == name:
            return tool
    raise KeyError(f"Ferramenta '{name}' não encontrada no catálogo de ferramentas do JEV.")
