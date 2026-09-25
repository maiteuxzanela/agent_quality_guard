"""Rastreador Exaustivo de Suspeitas Estruturais (Needle 3 Defect Screener).

Inspeciona símbolos sintáticos (AST) procurando marcadores estruturais concretos
das 8 categorias reais de defeito do ecossistema Open JEV.

Se o símbolo não apresentar anomalias estruturais, retorna lista vazia (habilitando
o Fast-Path de aprovação sem custos de inferência).
Se apresentar indícios, retorna todas as suspeitas para validação booleana via JEV Noul.
"""

import ast
import logging
import textwrap
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set

from src.memory.ast_diff_extractor import AstSymbolSlice

logger = logging.getLogger("openjev.defect_screener")

# Hipóteses calibradas universais para cada uma das 8 categorias reais do Laya Noul
DEFECT_NOUL_HYPOTHESES: Dict[str, str] = {
    "logica_ou_regra": (
        "O código desta função possui erro lógico de controle de fluxo, inconsistência de estado "
        "ou regra de negócio com predicado incorreto."
    ),
    "robustez_excecoes_recursos": (
        "O código desta função apresenta risco de vazamento de recursos, falta de fechamento seguro "
        "ou ausência de tratamento defensivo de exceção."
    ),
    "seguranca": (
        "O código desta função apresenta vulnerabilidade de segurança, sanitização insuficiente "
        "ou autorização frouxa."
    ),
    "qualidade_de_testes": (
        "A implementação desta função não possui testes automatizados reais cobrindo seus caminhos críticos de erro."
    ),
    "contrato_ambiente": (
        "Esta função quebra o contrato de tipos, assinatura de parâmetros ou schema de dados esperado pelo sistema."
    ),
    "performance_escopo_estilo": (
        "Esta implementação introduz gargalo severo de desempenho, computação redundante ou desvio injustificado de escopo."
    ),
    "requisito_incompleto_ou_ausente": (
        "A implementação deste símbolo deixou de atender a um dos comportamentos expressamente requeridos na demanda."
    ),
    "documentacao_configuracao": (
        "A alteração realizada exige parâmetros ou configurações de ambiente que não foram providenciados ou documentados."
    ),
}


@dataclass
class DefectSuspicion:
    """Representa uma suspeita estrutural identificada na AST pelo Needle 3."""
    category: str
    symbol_name: str
    file_path: str
    line_number: int
    trigger_evidence: str
    noul_hypothesis: str


class SymbolAstVisitor(ast.NodeVisitor):
    """Varre os nós da AST do símbolo isolado procurando marcadores das 8 categorias."""

    def __init__(self, symbol_slice: AstSymbolSlice):
        self.slice = symbol_slice
        self.suspicions: List[DefectSuspicion] = []
        self._loop_depth = 0
        self._if_depth = 0
        self._with_depth = 0
        self._in_try_block = False

    def _add_suspicion(self, category: str, line_no: int, evidence: str):
        hypothesis = DEFECT_NOUL_HYPOTHESES.get(category, "")
        self.suspicions.append(
            DefectSuspicion(
                category=category,
                symbol_name=self.slice.symbol_name,
                file_path=self.slice.file_path,
                line_number=line_no,
                trigger_evidence=evidence,
                noul_hypothesis=hypothesis,
            )
        )

    def visit_If(self, node: ast.If):
        is_elif = getattr(node, "_is_elif", False)
        if not is_elif:
            self._if_depth += 1

        # Detecção de lógica complexa: condicionais aninhadas profundas reais (profundidade >= 3)
        if self._if_depth >= 3:
            self._add_suspicion(
                "logica_ou_regra",
                node.lineno,
                f"Condicional aninhada profunda (nível {self._if_depth}) com alto risco de inconsistência de ramificação.",
            )
        # Detecção de predicados compostos com mais de 3 operadores booleanos (> 3 termos)
        if isinstance(node.test, ast.BoolOp) and len(node.test.values) > 3:
            self._add_suspicion(
                "logica_ou_regra",
                node.lineno,
                f"Predicado booleano complexo com {len(node.test.values)} condições encadeadas.",
            )

        # Marca nós If no orelse que são ramificações planas de elif
        if len(node.orelse) == 1 and isinstance(node.orelse[0], ast.If):
            setattr(node.orelse[0], "_is_elif", True)

        self.generic_visit(node)
        if not is_elif:
            self._if_depth -= 1

    def visit_For(self, node: ast.For):
        self._loop_depth += 1
        if self._loop_depth >= 2:
            self._add_suspicion(
                "performance_escopo_estilo",
                node.lineno,
                f"Laços de repetição aninhados (profundidade {self._loop_depth}): risco de complexidade O(N^2).",
            )
        self.generic_visit(node)
        self._loop_depth -= 1

    def visit_While(self, node: ast.While):
        self._loop_depth += 1
        # While True com manipulação de ponteiros ou break condicional
        if isinstance(node.test, ast.Constant) and node.test.value is True:
            self._add_suspicion(
                "logica_ou_regra",
                node.lineno,
                "Laço infinito 'while True': requer controle rigoroso de condições de parada para evitar loop infinito.",
            )
        self.generic_visit(node)
        self._loop_depth -= 1

    def visit_With(self, node: ast.With):
        self._with_depth += 1
        self.generic_visit(node)
        self._with_depth -= 1

    def visit_AsyncWith(self, node: ast.AsyncWith):
        self._with_depth += 1
        self.generic_visit(node)
        self._with_depth -= 1

    def visit_Call(self, node: ast.Call):
        func_name = ""
        if isinstance(node.func, ast.Name):
            func_name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            func_name = node.func.attr

        # 1. Robustez de E/S e Recursos
        io_triggers = {"open", "seek", "connect", "socket", "acquire"}
        is_protected = self._in_try_block or self._with_depth > 0
        if func_name in io_triggers and not is_protected:
            self._add_suspicion(
                "robustez_excecoes_recursos",
                getattr(node, "lineno", self.slice.start_line),
                f"Operação de E/S ou recurso crítico '{func_name}()' invocada fora de bloco protegido.",
            )

        # 2. Segurança: shell=True ou exec/eval
        if func_name in {"exec", "eval"}:
            self._add_suspicion(
                "seguranca",
                getattr(node, "lineno", self.slice.start_line),
                f"Uso perigoso de função dinâmica de execução '{func_name}()'.",
            )
        for kw in node.keywords:
            if kw.arg == "shell" and isinstance(kw.value, ast.Constant):
                if kw.value.value is True:
                    self._add_suspicion(
                        "seguranca",
                        getattr(node, "lineno", self.slice.start_line),
                        "Chamada de subprocess com 'shell=True' detectada.",
                    )

        # 3. Documentação e Configuração: leitura de variáveis de ambiente sem fallback
        if func_name == "getenv" and not node.keywords:
            if len(node.args) == 1:
                self._add_suspicion(
                    "documentacao_configuracao",
                    getattr(node, "lineno", self.slice.start_line),
                    "Leitura de variável de ambiente via 'os.getenv()' sem valor padrão defensivo.",
                )

        self.generic_visit(node)

    def visit_Try(self, node: ast.Try):
        prev_try = self._in_try_block
        self._in_try_block = True
        for handler in node.handlers:
            # Captura bare except ou except Exception genérico
            if handler.type is None:
                self._add_suspicion(
                    "robustez_excecoes_recursos",
                    handler.lineno,
                    "Tratamento de exceção cru 'except:' (bare except).",
                )
            elif isinstance(handler.type, ast.Name) and handler.type.id in {"Exception", "BaseException"}:
                # Se dentro do handler tiver apenas pass, vira violação grave
                if len(handler.body) == 1 and isinstance(handler.body[0], ast.Pass):
                    self._add_suspicion(
                        "robustez_excecoes_recursos",
                        handler.lineno,
                        f"Exceção genérica '{handler.type.id}' silenciada com 'pass'.",
                    )
        self.generic_visit(node)
        self._in_try_block = prev_try

    def visit_Raise(self, node: ast.Raise):
        if isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name):
            if node.exc.func.id == "NotImplementedError":
                self._add_suspicion(
                    "requisito_incompleto_ou_ausente",
                    node.lineno,
                    "Lançamento explícito de 'NotImplementedError': funcionalidade incompleta ou stub.",
                )
        self.generic_visit(node)


def screen_symbol_defects(symbol_slice: AstSymbolSlice) -> List[DefectSuspicion]:
    """Analisa o símbolo sintático via AST e retorna todas as suspeitas concretas identificadas."""
    content_to_parse = textwrap.dedent(symbol_slice.content)
    try:
        tree = ast.parse(content_to_parse)
    except Exception as e:
        logger.debug(f"Falha ao parsear conteúdo do símbolo {symbol_slice.symbol_name}: {e}")
        return [
            DefectSuspicion(
                category="contrato_ambiente",
                symbol_name=symbol_slice.symbol_name,
                file_path=symbol_slice.file_path,
                line_number=symbol_slice.start_line,
                trigger_evidence=f"Erro de sintaxe AST: {e}",
                noul_hypothesis=DEFECT_NOUL_HYPOTHESES["contrato_ambiente"],
            )
        ]

    visitor = SymbolAstVisitor(symbol_slice)
    visitor.visit(tree)

    # Verificação de comentários de pendência real (ex: # TODO:, # FIXME:)
    import re
    todo_re = re.compile(r"^\s*#\s*(?:TODO|FIXME|HACK)\s*[:\-]", re.IGNORECASE)
    for i, line in enumerate(symbol_slice.content.splitlines(), start=symbol_slice.start_line):
        stripped = line.strip()
        if todo_re.match(line):
            visitor.suspicions.append(
                DefectSuspicion(
                    category="requisito_incompleto_ou_ausente",
                    symbol_name=symbol_slice.symbol_name,
                    file_path=symbol_slice.file_path,
                    line_number=i,
                    trigger_evidence=f"Marcador de pendência encontrado: {line.strip()}",
                    noul_hypothesis=DEFECT_NOUL_HYPOTHESES["requisito_incompleto_ou_ausente"],
                )
            )

    # Remove duplicatas da mesma categoria no mesmo símbolo (mantém evidência mais relevante)
    seen_categories: Set[str] = set()
    unique_suspicions: List[DefectSuspicion] = []
    for s in visitor.suspicions:
        if s.category not in seen_categories:
            seen_categories.add(s.category)
            unique_suspicions.append(s)

    return unique_suspicions


def screen_workspace_symbols(slices: List[AstSymbolSlice]) -> Dict[str, List[DefectSuspicion]]:
    """Varre todos os símbolos extraídos e retorna mapa {symbol_key: lista_de_suspeitas}."""
    result: Dict[str, List[DefectSuspicion]] = {}
    for s in slices:
        key = f"{s.file_path}::{s.symbol_name}"
        suspicions = screen_symbol_defects(s)
        if suspicions:
            result[key] = suspicions
    return result
