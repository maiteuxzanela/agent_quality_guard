"""Definições de ferramentas (Tool Definitions) padronizadas para agentes e LLMs orquestradoras.

Compatível com OpenAI Function Calling, Anthropic Tool Use e Gemini Function Declarations.

TICKET-HARNESS-47: o catálogo espelha estritamente a superfície MCP ativa:

* ``cirurgic_search`` (canônica) + alias legado ``qmd_search``;
* ``agent_quality_guard`` (canônica) + alias legado ``needle_verify_delivery``.

As primitivas probabilísticas do motor Laya RLCD (``openjev_noul``,
``openjev_choice``, ``openjev_consulta``) e o antigo ``needle3_search`` foram
expurgados — não há caminho de inferência ML/GPU no catálogo.
"""

import copy
from typing import Any, Dict, List

_SEARCH_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Consulta técnica ou semântica sobre o que precisa ser localizado "
                "ou compreendido no código."
            ),
        },
        "workspace": {
            "type": "string",
            "default": "",
            "description": "Caminho do repositório/workspace a buscar (padrão: cwd).",
        },
        "scope": {
            "type": "string",
            "description": (
                "Lista opcional de diretórios, módulos ou caminhos separados por "
                "vírgula para restringir a busca."
            ),
        },
        "max_tokens": {
            "type": "integer",
            "default": 1200,
            "description": (
                "Teto máximo de tokens para o contexto retornado "
                "(canônico 1200; evita sobrecarregar a LLM)."
            ),
        },
        "depth": {
            "type": "integer",
            "default": 2,
            "description": (
                "Profundidade de dependências no grafo de símbolos "
                "(1 = apenas o símbolo, 2 = chamadores diretos, etc.)."
            ),
        },
    },
    "required": ["query"],
}

_SEARCH_DESCRIPTION = (
    "Busca cirúrgica híbrida (Ripgrep + Needle 3/Árvore AST + RRF em CPU). "
    "Ferramenta primária para explorar código, funções, regras de negócio e "
    "investigar bugs, com controle estrito de janela de contexto (max_tokens)."
)

_QUALITY_GUARD_PARAMETERS: Dict[str, Any] = {
    "type": "object",
    "properties": {
        "task_type": {
            "type": "string",
            "default": "codigo_producao",
            "description": "Tipo de tarefa (codigo_producao).",
        },
        "workspace": {
            "type": "string",
            "default": "",
            "description": "Caminho do repositório/workspace a validar (padrão: cwd).",
        },
        "attempt_number": {
            "type": "integer",
            "default": 1,
            "description": (
                "Informativo (1 a 5). A contagem de tentativas é exclusivamente "
                "server-side: o valor do chamador não participa do circuit breaker."
            ),
        },
        "pedido": {
            "type": "string",
            "description": "Descrição do pedido original (opcional para audit log).",
        },
        "resultado": {
            "type": "string",
            "description": "Descrição do resultado executado (opcional para audit log).",
        },
        "audit_mode": {
            "type": "string",
            "default": "delta",
            "description": "Modo de auditoria do workspace ('delta' ou 'full').",
        },
    },
}

_QUALITY_GUARD_DESCRIPTION = (
    "Homologação de entrega de código 100% DETERMINÍSTICA e sem ML. "
    "Executa: 1º testes reais (pytest em chunks / runner multi-stack), "
    "2º NeedleASTGuard (anti-mock em produção, SQLi, eval, segredos), "
    "3º bridge nativa de compiladores/linters do projeto (tsc, eslint/biome, "
    "ruff/mypy, clippy, go vet), 4º meta-regras universais de AST via Tree-Sitter "
    "(floating promises, silent catch, resource leaks). "
    "Circuit breaker universal de 5 tentativas: na 5ª tentativa sem aprovação "
    "determinística, o código é liberado com ressalva (EMERGENCY_RELEASE)."
)

_NEEDLE_VERIFY_DELIVERY_DESCRIPTION = (
    "Alias legado de agent_quality_guard (MESMA implementação e assinatura), "
    "mantido para compatibilidade retroativa total. "
    "Validação de entrega de código 100% determinística e sem ML. "
    "Executa: 1º Pytest em chunks sequenciais, "
    "2º NeedleASTGuard (anti-mock, SQLi, eval, segredos), "
    "3º Camada 1 (compiladores/linters nativos) e "
    "4º Camada 2 (meta-regras universais de AST via Tree-Sitter). "
    "Circuit breaker universal de 5 tentativas: se a 5ª tentativa for atingida "
    "sem aprovação determinística, o código é aprovado com ressalva (EMERGENCY_RELEASE)."
)


TOOL_DEFINITIONS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "cirurgic_search",
            "description": _SEARCH_DESCRIPTION,
            "parameters": copy.deepcopy(_SEARCH_PARAMETERS),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "qmd_search",
            "description": (
                "Alias legado de cirurgic_search (MESMA implementação e assinatura), "
                "mantido para compatibilidade retroativa total com clientes antigos. "
                + _SEARCH_DESCRIPTION
            ),
            "parameters": copy.deepcopy(_SEARCH_PARAMETERS),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "agent_quality_guard",
            "description": _QUALITY_GUARD_DESCRIPTION,
            "parameters": copy.deepcopy(_QUALITY_GUARD_PARAMETERS),
        },
    },
    {
        "type": "function",
        "function": {
            "name": "needle_verify_delivery",
            "description": _NEEDLE_VERIFY_DELIVERY_DESCRIPTION,
            "parameters": copy.deepcopy(_QUALITY_GUARD_PARAMETERS),
        },
    },
]


def get_tool_definition(name: str) -> Dict[str, Any]:
    """Recupera a definição de uma ferramenta específica pelo nome."""
    for tool in TOOL_DEFINITIONS:
        if tool["function"]["name"] == name:
            return tool
    raise KeyError(f"Ferramenta '{name}' não encontrada no catálogo de ferramentas do JEV.")
