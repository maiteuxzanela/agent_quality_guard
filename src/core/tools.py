"""Definições de ferramentas (Tool Definitions) padronizadas para agentes e LLMs orquestradoras.
Compatível com OpenAI Function Calling, Anthropic Tool Use e Gemini Function Declarations.
"""

from typing import Any, Dict, List

TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "needle3_relational_search",
            "description": (
                "Aciona o motor Cactus Needle 3 para realizar busca cirúrgica, semântica e relacional "
                "em bases de código, documentação densa ou logs extensos. "
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
                        "default": 1500,
                        "description": "Teto máximo de tokens para o contexto retornado (evita sobrecarregar a LLM)."
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
            "name": "openjev_noul_guardrail",
            "description": (
                "Executa o modelo Open Jev local para avaliar uma condição lógica/booleana com probabilidade calibrada. "
                "MANDATÓRIO: Deve ser acionada antes de qualquer chamada de ferramenta crítica/destrutiva "
                "(ex: remoção de arquivos, modificação de infraestrutura, comandos shell de risco, commits em produção)."
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
                        "default": 0.85,
                        "description": "Limiar mínimo de probabilidade para aprovação da ação (guardrail)."
                    }
                },
                "required": ["premise", "hypothesis"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "openjev_choice_route",
            "description": (
                "Executa o modelo Open Jev local para determinar com máxima precisão e distribuição de probabilidade "
                "qual subagente especialista deve assumir a execução de uma subtarefa complexa."
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
            "name": "openjev_score_evaluate",
            "description": (
                "Executa o modelo Open Jev local para avaliar quantitativamente a qualidade de um artefato gerado "
                "(código, refatoração, documentação, plano de testes) contra uma rubrica predefinida em escala ordinal."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "premise": {
                        "type": "string",
                        "description": "O conteúdo completo ou diff do código/artefato submetido para validação."
                    },
                    "rubric": {
                        "type": "string",
                        "description": "Critérios de aceitação, diretrizes de código limpo, cobertura de testes e regras arquiteturais."
                    },
                    "min_score": {
                        "type": "number",
                        "default": 1.0,
                        "description": "Nota mínima da escala (padrão 1.0)."
                    },
                    "max_score": {
                        "type": "number",
                        "default": 5.0,
                        "description": "Nota máxima da escala (padrão 5.0)."
                    },
                    "threshold": {
                        "type": "number",
                        "default": 4.0,
                        "description": "Nota de corte para aprovação da entrega (se menor, o subagente deve refatorar)."
                    }
                },
                "required": ["premise", "rubric"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "openjev_score_and_diagnose",
            "description": (
                "Avalia a qualidade de um artefato técnico com score contínuo (1.0–5.0) e, "
                "se a nota de corte não for atingida, executa automaticamente dois choice em cascata "
                "para identificar a natureza do defeito: "
                "(L1) Macro-categoria ortogonal de falha entre 5 dimensões; "
                "(L2) Micro-diagnóstico específico entre os 4 subtipos da categoria eleita. "
                "Use este tool como substituto completo do openjev_score_evaluate quando feedback "
                "estruturado de auto-correção for necessário."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "premise": {
                        "type": "string",
                        "description": "Pedido original concatenado com resultado executado (arquivos modificados, testes, commits).",
                    },
                    "rubric": {
                        "type": "string",
                        "description": "Rubrica de avaliação. Padrão calibrado: critérios de rigor analítico, clareza e fidelidade ao repositório.",
                    },
                    "workspace": {
                        "type": "string",
                        "description": "Caminho absoluto do repositório Git para rastreamento anti-premise gaming. Padrão: cwd.",
                    },
                    "min_score": {"type": "number", "default": 1.0, "description": "Nota mínima da escala."},
                    "max_score": {"type": "number", "default": 5.0, "description": "Nota máxima da escala."},
                    "threshold": {
                        "type": "number",
                        "default": 3.60,
                        "description": (
                            "Nota de corte. Se score < threshold, dispara a árvore de diagnóstico L1→L2. "
                            "Padrão Laya calibrado: 3.60."
                        ),
                    },
                },
                "required": ["premise"],
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
