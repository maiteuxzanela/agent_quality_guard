"""Rastreador Exaustivo de Suspeitas Estruturais (Needle 3 Defect Screener).

Inspeciona símbolos sintáticos (AST) procurando marcadores estruturais concretos
das 10 categorias reais de defeito do ecossistema Open JEV (8 categorias duras
+ 2 categorias de análise/qualidade: `tipagem_ausente`, `docstring_ausente`).

Se o símbolo não apresentar anomalias estruturais, retorna lista vazia (habilitando
o Fast-Path de aprovação sem custos de inferência).
Se apresenta indícios, retorna todas as suspeitas para validação booleana via JEV Noul.
"""

import ast
import logging
import re
import textwrap
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Set, Tuple

from src.memory.ast_diff_extractor import AstSymbolSlice

logger = logging.getLogger("openjev.defect_screener")

# Constante de texto: caminho absoluto ou URL embutida (gatilho A9).
_PATH_URL_RE = re.compile(r"^(https?://|ftp://|/home/|/Users/|[A-Za-z]:\\)")
# Docstring com exemplo de uso (gatilho A2): doctest ou seção "Exemplo:".
_DOC_EXAMPLE_RE = re.compile(r"(>>>|exemplo)", re.IGNORECASE)
# Marcador de pendência real em comentário (verificação textual pós-AST).
_TODO_RE = re.compile(r"^\s*#\s*(?:TODO|FIXME|HACK)\s*[:\-]", re.IGNORECASE)


def _call_name(node: ast.Call) -> str:
    """Retorna o nome simples alvo da chamada (Name.id ou Attribute.attr)."""
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return ""


def _is_open_call(value: Any) -> bool:
    """True quando value é chamada direta a open(...) — rastreio A3."""
    if not isinstance(value, ast.Call):
        return False
    if not isinstance(value.func, ast.Name):
        return False
    return value.func.id == "open"


def _is_docstring_expr(stmt: Any) -> bool:
    """True quando stmt é uma expressão-string (docstring) de corpo de função."""
    if not isinstance(stmt, ast.Expr):
        return False
    if not isinstance(stmt.value, ast.Constant):
        return False
    return isinstance(stmt.value.value, str)


def _needs_usage_example(node: ast.FunctionDef | ast.AsyncFunctionDef, all_args: list) -> bool:
    """A2 (calibração 09/2026): docstring de função pública de módulo sem uso.

    Exige doctest/`Exemplo:` apenas em função pública de módulo; métodos
    (self/cls primeiro) e helpers privados ficam isentos — exigir doctest
    neles gerava falso positivo sistêmico em código saudável.
    """
    docstring = ast.get_docstring(node)
    if docstring is None:
        return False
    if _DOC_EXAMPLE_RE.search(docstring):
        return False
    if node.name.startswith("_"):
        return False
    if all_args and all_args[0].arg in {"self", "cls"}:
        return False
    return True

# Hipóteses calibradas universais para cada uma das 10 categorias reais do Laya Noul
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
    "tipagem_ausente": (
        "Esta função foi entregue sem anotações de tipo em parâmetros ou retorno onde o contrato "
        "do projeto exige tipagem estrita, enfraquecendo contratos estáticos e manutenibilidade."
    ),
    "docstring_ausente": (
        "Esta função ou classe pública foi entregue sem docstring, deixando contrato, efeitos "
        "colaterais e uso esperado não documentados para quem vai manter o código."
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


def _is_test_path(file_path: str) -> bool:
    """True quando o símbolo pertence a código de teste (gatilhos de análise não valem lá)."""
    parts = [p.lower() for p in file_path.replace("\\", "/").split("/")]
    for part in parts[:-1]:
        if part == "tests" or part.startswith("test_"):
            return True
    return bool(parts) and parts[-1].startswith("test_")


def _is_dunder(name: str) -> bool:
    """True para métodos mágicos (`__init__`, `__str__`, ...) — anotações/docstring por convenção."""
    return name.startswith("__") and name.endswith("__")


class SymbolAstVisitor(ast.NodeVisitor):
    """Varre os nós da AST do símbolo isolado procurando marcadores das 10 categorias."""

    def __init__(self, symbol_slice: AstSymbolSlice):
        self.slice = symbol_slice
        self.suspicions: List[DefectSuspicion] = []
        self._loop_depth = 0
        self._if_depth = 0
        self._with_depth = 0
        self._in_try_block = False
        self._is_test_file = _is_test_path(symbol_slice.file_path)
        # Estado dos gatilhos Grupo A (set/2026)
        self._class_depth = 0
        self._func_depth = 0
        self._in_main_guard = False
        self._open_assigned: List[Tuple[str, int]] = []
        self._closed_names: Set[str] = set()
        self._with_names: Set[str] = set()
        self._str_params: Set[str] = set()
        self._docstring_nodes: Set[int] = set()
        self._str_vars: Set[str] = set()

    def _add_suspicion(self, category: str, line_no: int, evidence: str) -> None:
        """Registra uma suspeita da categoria indicada para o símbolo atual."""
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

    def visit_If(self, node: ast.If) -> None:
        """Varre um nó if/elif aninhado, rastreando profundidade de condicionais."""
        is_elif = getattr(node, "_is_elif", False)
        if not is_elif:
            self._if_depth += 1

        # Bloco `if __name__ == "__main__":` — saída de CLI é legítima (A13).
        is_main = (
            isinstance(node.test, ast.Compare)
            and isinstance(node.test.left, ast.Name)
            and node.test.left.id == "__name__"
            and len(node.test.comparators) == 1
            and isinstance(node.test.comparators[0], ast.Constant)
            and node.test.comparators[0].value == "__main__"
        )
        prev_main = self._in_main_guard
        if is_main:
            self._in_main_guard = True

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
        self._in_main_guard = prev_main
        if not is_elif:
            self._if_depth -= 1

    def visit_For(self, node: ast.For) -> None:
        """Varre um laço for, rastreando profundidade de aninhamento."""
        self._loop_depth += 1
        if self._loop_depth >= 2:
            self._add_suspicion(
                "performance_escopo_estilo",
                node.lineno,
                f"Laços de repetição aninhados (profundidade {self._loop_depth}): risco de complexidade O(N^2).",
            )
        self.generic_visit(node)
        self._loop_depth -= 1

    def visit_While(self, node: ast.While) -> None:
        """Varre um laço while, marcando 'while True' sem quebra evidente."""
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

    def visit_With(self, node: ast.With) -> None:
        """Varre um bloco with síncrono rastreando profundidade de gerenciadores."""
        self._with_depth += 1
        self._record_with_names(node)
        self.generic_visit(node)
        self._with_depth -= 1

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        """Varre um bloco with assíncrono rastreando profundidade de gerenciadores."""
        self._with_depth += 1
        self._record_with_names(node)
        self.generic_visit(node)
        self._with_depth -= 1

    def _record_with_names(self, node: Any) -> None:
        """Registra nomes ligados a `with` para o gatilho de fechamento de recurso (A3)."""
        for item in node.items:
            self._collect_names(item.context_expr)
            self._collect_names(item.optional_vars)

    def _collect_names(self, tree: Any) -> None:
        """Acumula ids de Name da sub-árvore em _with_names (sem laço aninhado)."""
        if tree is None:
            return
        for n in ast.walk(tree):
            if isinstance(n, ast.Name):
                self._with_names.add(n.id)

    def visit_Expr(self, node: ast.Expr) -> None:
        """Marca strings-nua (docstrings) para o gatilho A9 não disparar nelas."""
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            self._docstring_nodes.add(id(node.value))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        """A9: caminho absoluto ou URL hardcoded fora de docstring e de teste."""
        if self._is_test_file or not isinstance(node.value, str):
            return
        if id(node) in self._docstring_nodes:
            return
        if _PATH_URL_RE.match(node.value):
            self._add_suspicion(
                "documentacao_configuracao",
                getattr(node, "lineno", self.slice.start_line),
                f"String literal com caminho absoluto ou URL hardcoded: {node.value[:60]!r}.",
            )

    def visit_Subscript(self, node: ast.Subscript) -> None:
        """A8: acesso direto a os.environ[...] sem get() com fallback."""
        if (
            not self._is_test_file
            and isinstance(node.value, ast.Attribute)
            and node.value.attr == "environ"
        ):
            self._add_suspicion(
                "documentacao_configuracao",
                getattr(node, "lineno", self.slice.start_line),
                "Acesso direto a 'os.environ[...]' sem get() com valor padrão defensivo.",
            )
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        """A7: concatenação de string dentro de laço (acumulação O(N^2))."""
        if not (self._loop_depth > 0 and isinstance(node.op, ast.Add)):
            self.generic_visit(node)
            return
        value_is_str = isinstance(node.value, (ast.Constant, ast.JoinedStr)) and (
            not isinstance(node.value, ast.Constant) or isinstance(node.value.value, str)
        )
        target_is_str = isinstance(node.target, ast.Name) and node.target.id in self._str_vars
        if value_is_str or target_is_str:
            self._add_suspicion(
                "performance_escopo_estilo",
                getattr(node, "lineno", self.slice.start_line),
                "Concatenação de string com '+=' dentro de laço: acumulação O(N^2).",
            )
        self.generic_visit(node)

    def visit_Global(self, node: ast.Global) -> None:
        """A6: declaração global de estado mutável em código de produção."""
        if not self._is_test_file:
            self._add_suspicion(
                "logica_ou_regra",
                node.lineno,
                f"Declaração 'global {', '.join(node.names)}': estado mutável compartilhado entre chamadas.",
            )
        self.generic_visit(node)

    def visit_Assign(self, node: ast.Assign) -> None:
        """Registra `f = open(...)` (A3) e locais de string (A7) do símbolo."""
        if _is_open_call(node.value):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self._open_assigned.append((t.id, node.lineno))
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self._str_vars.add(t.id)
        elif isinstance(node.value, ast.JoinedStr):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self._str_vars.add(t.id)
        elif (
            isinstance(node.value, ast.Call)
            and isinstance(node.value.func, ast.Name)
            and node.value.func.id in {"str", "repr"}
        ):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    self._str_vars.add(t.id)
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        """Registra `f: IO = open(...)` para o gatilho A3."""
        if _is_open_call(node.value) and isinstance(node.target, ast.Name):
            self._open_assigned.append((node.target.id, node.lineno))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        """Varre chamadas, marcando exec/execfile e subprocess com shell=True."""
        func_name = _call_name(node)

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

        if (
            func_name == "close"
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
        ):
            self._closed_names.add(node.func.value.id)

        # A11: parâmetro str repassado diretamente a open() sem sanitização de caminho
        if self._is_str_param_open(node):
            self._add_suspicion(
                "seguranca",
                getattr(node, "lineno", self.slice.start_line),
                f"Parâmetro str '{node.args[0].id}' repassado diretamente a open() sem sanitização de caminho.",
            )

        # A13: print/breakpoint/pdb em produção fora do guardra de CLI
        if (
            not self._is_test_file
            and not self._in_main_guard
            and func_name in {"print", "breakpoint", "set_trace"}
        ):
            self._add_suspicion(
                "performance_escopo_estilo",
                getattr(node, "lineno", self.slice.start_line),
                f"Chamada de saída/deburação '{func_name}()' em produção fora de bloco CLI (use logging).",
            )

        self.generic_visit(node)

    def _is_str_param_open(self, node: ast.Call) -> bool:
        """A11: open(<parâmetro str>) repassado sem sanitização de caminho."""
        if not node.args or _call_name(node) != "open":
            return False
        first = node.args[0]
        return isinstance(first, ast.Name) and first.id in self._str_params

    def visit_Try(self, node: ast.Try) -> None:
        """Varre blocos try/except, marcando bare except e exceções silenciadas."""
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
            # A4: engolir a exceção por controle de fluxo (corpo de 1 instrução)
            if len(handler.body) == 1:
                stmt = handler.body[0]
                if isinstance(stmt, ast.Return) and stmt.value is None:
                    self._add_suspicion(
                        "robustez_excecoes_recursos",
                        handler.lineno,
                        "Exceção engolida por 'return' silencioso (retorna None sem sinalizar a falha).",
                    )
                elif isinstance(stmt, ast.Continue):
                    self._add_suspicion(
                        "robustez_excecoes_recursos",
                        handler.lineno,
                        "Exceção engolida por 'continue' silencioso (descarta o erro sem log nem re-raise).",
                    )
        self.generic_visit(node)
        self._in_try_block = prev_try

    def visit_Raise(self, node: ast.Raise) -> None:
        """Varre raise, marcando NotImplementedError como funcionalidade incompleta."""
        if isinstance(node.exc, ast.Call) and isinstance(node.exc.func, ast.Name):
            if node.exc.func.id == "NotImplementedError":
                self._add_suspicion(
                    "requisito_incompleto_ou_ausente",
                    node.lineno,
                    "Lançamento explícito de 'NotImplementedError': funcionalidade incompleta ou stub.",
                )
        self.generic_visit(node)

    # ── Categorias de análise (tier de qualidade, set/2026) ──────────────────

    def _screen_quality_signature(
        self, node: ast.FunctionDef | ast.AsyncFunctionDef
    ) -> None:
        """Checa anotações de tipo da assinatura e docstring (tier de análise).

        Só valem em código de produção (fora de tests/); nunca disparam em
        símbolos dunder (convenção de anotação própria). Não reprovam entregas
        com score >= threshold (QUALITY_DEFECT_CATEGORIES no mcp_server).
        """
        if self._is_test_file or _is_dunder(node.name):
            return
        missing: List[str] = []
        all_args = [
            *node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
        ]
        for a in all_args:
            if a.arg in {"self", "cls"}:
                continue
            if a.annotation is None:
                missing.append(f"parâmetro '{a.arg}'")
        if node.args.vararg is not None and node.args.vararg.annotation is None:
            missing.append(f"*{node.args.vararg.arg}")
        if node.args.kwarg is not None and node.args.kwarg.annotation is None:
            missing.append(f"**{node.args.kwarg.arg}")
        if node.returns is None:
            missing.append("retorno")
        if missing:
            self._add_suspicion(
                "tipagem_ausente",
                node.lineno,
                f"Função '{node.name}' sem anotações de tipo em: {', '.join(missing)}.",
            )
        if ast.get_docstring(node) is None:
            self._add_suspicion(
                "docstring_ausente",
                node.lineno,
                f"Função '{node.name}' entregue sem docstring.",
            )
        elif (
            self._class_depth == 0
            and self._func_depth == 0
            and _needs_usage_example(node, all_args)
        ):
            # A2 — calibração 09/2026 (usuária): só funções PÚBLICAS de módulo
            # no topo; closures aninhadas (ex.: lifespan) ficam isentas.
            self._add_suspicion(
                "docstring_ausente",
                node.lineno,
                f"Docstring de '{node.name}' sem exemplo de uso (>>> ou 'Exemplo:').",
            )

    def _screen_function_extras(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        """Gatilhos estruturais por função: default mutável (A5), stub (A10) e teste de classe (A12)."""
        # A5 — argumento padrão mutável (bug de estado em qualquer arquivo)
        default_values = [
            *node.args.defaults,
            *[d for d in node.args.kw_defaults if d is not None],
        ]
        for dv in default_values:
            if isinstance(dv, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
                self._add_suspicion(
                    "logica_ou_regra",
                    node.lineno,
                    f"Argumento padrão mutável em '{node.name}': estado compartilhado entre chamadas.",
                )
                break

        if self._is_test_file:
            # A12 — método de classe sem nenhuma asserção real
            if (
                self._class_depth > 0
                and node.name.startswith("test_")
                and not self._has_assertions(node)
            ):
                self._add_suspicion(
                    "qualidade_de_testes",
                    node.lineno,
                    f"Teste de classe '{node.name}' sem nenhuma asserção (falso positivo de cobertura).",
                )
            return

        if _is_dunder(node.name):
            return
        # A10 vale só para funções de módulo: métodos `pass`/`...` são
        # frequentemente no-ops legítimos de protocolo (ex.: close() fake).
        if self._class_depth > 0:
            return

        # A10 — stub com corpo vazio (exceto patterns de convenção)
        deco_src = " ".join(ast.unparse(d) for d in node.decorator_list)
        if any(k in deco_src for k in ("abstractmethod", "property", "setter", "getter", "overload")):
            return
        body = list(node.body)
        if body and _is_docstring_expr(body[0]):
            body = body[1:]  # ignora docstring
        is_empty = not body or (
            len(body) == 1
            and (
                isinstance(body[0], ast.Pass)
                or (
                    isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and body[0].value.value is Ellipsis
                )
            )
        )
        if is_empty:
            self._add_suspicion(
                "requisito_incompleto_ou_ausente",
                node.lineno,
                f"Função '{node.name}' com corpo vazio (stub de implementação pendente).",
            )

    @staticmethod
    def _has_assertions(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        """True quando o teste contém assert real ou chamada assert*/self.assert* (A12)."""
        for n in ast.walk(node):
            if isinstance(n, ast.Assert):
                return True
            if isinstance(n, ast.Call):
                f = n.func
                name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else "")
                if name.startswith("assert"):
                    return True
        return False

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        """Aplica os screeneres de estrutura/qualidade e desce nos nós filhos do símbolo."""
        self._screen_function_extras(node)
        prev_params = self._str_params
        own_str: Set[str] = set()
        all_args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        for a in all_args:
            if a.annotation is not None and "str" in ast.unparse(a.annotation):
                own_str.add(a.arg)
        if node.args.vararg and node.args.vararg.annotation is not None:
            if "str" in ast.unparse(node.args.vararg.annotation):
                own_str.add(node.args.vararg.arg)
        if node.args.kwarg and node.args.kwarg.annotation is not None:
            if "str" in ast.unparse(node.args.kwarg.annotation):
                own_str.add(node.args.kwarg.arg)
        self._str_params = own_str
        self._screen_quality_signature(node)
        self._func_depth += 1
        self.generic_visit(node)
        self._func_depth -= 1
        self._str_params = prev_params

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        """Aplica os screeneres de estrutura/qualidade e desce nos nós filhos do símbolo async."""
        self._screen_function_extras(node)
        self._str_params = set()
        all_args = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        for a in all_args:
            if a.annotation is not None and "str" in ast.unparse(a.annotation):
                self._str_params.add(a.arg)
        self._screen_quality_signature(node)
        self._func_depth += 1
        self.generic_visit(node)
        self._func_depth -= 1
        self._str_params = set()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        """Checa docstring da classe (fora de testes e de símbolos dunder) e rastreia escopo."""
        self._class_depth += 1
        if not self._is_test_file and not _is_dunder(node.name):
            if ast.get_docstring(node) is None:
                self._add_suspicion(
                    "docstring_ausente",
                    node.lineno,
                    f"Classe '{node.name}' entregue sem docstring.",
                )
        self.generic_visit(node)
        self._class_depth -= 1


def screen_symbol_defects(symbol_slice: AstSymbolSlice) -> List[DefectSuspicion]:
    """Analisa o símbolo sintático via AST e retorna todas as suspeitas concretas identificadas.

    Exemplo:
        >>> suspeitas = screen_symbol_defects(sl)
        >>> [s.category for s in suspeitas]
        []
    """
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

    # A3: recurso aberto e nunca fechado nem vinculado a `with` no mesmo símbolo
    for name, lineno in visitor._open_assigned:
        if name not in visitor._closed_names and name not in visitor._with_names:
            visitor.suspicions.append(
                DefectSuspicion(
                    category="robustez_excecoes_recursos",
                    symbol_name=symbol_slice.symbol_name,
                    file_path=symbol_slice.file_path,
                    line_number=lineno,
                    trigger_evidence=f"Recurso aberto em '{name} = open(...)' sem .close() nem bloco with no mesmo símbolo.",
                    noul_hypothesis=DEFECT_NOUL_HYPOTHESES["robustez_excecoes_recursos"],
                )
            )

    # Verificação de comentários de pendência real (ex: # TODO:, # FIXME:)
    for i, line in enumerate(symbol_slice.content.splitlines(), start=symbol_slice.start_line):
        stripped = line.strip()
        if _TODO_RE.match(line):
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
    """Varre todos os símbolos extraídos e retorna mapa {symbol_key: lista_de_suspeitas}.

    Exemplo:
        >>> mapa = screen_workspace_symbols(extract_workspace_ast_symbols("."))
        >>> isinstance(mapa, dict)
        True
    """
    result: Dict[str, List[DefectSuspicion]] = {}
    for s in slices:
        key = f"{s.file_path}::{s.symbol_name}"
        suspicions = screen_symbol_defects(s)
        if suspicions:
            result[key] = suspicions
    _screen_test_files_without_error_paths(slices, result)
    return result


def _screen_test_files_without_error_paths(
    slices: List[AstSymbolSlice], result: Dict[str, List[DefectSuspicion]]
) -> None:
    """A1: arquivo de teste sem nenhum caminho de erro — uma suspeita por módulo.

    Procura 'pytest.raises'/'assertRaises' em qualquer símbolo do arquivo; se
    ausente, registra uma única suspeita no símbolo de menor linha (representante).
    """
    by_file: Dict[str, List[AstSymbolSlice]] = {}
    for s in slices:
        by_file.setdefault(s.file_path, []).append(s)
    for fpath, file_slices in by_file.items():
        if not _is_test_path(fpath):
            continue
        joined = "\n".join(s.content for s in file_slices)
        if "pytest.raises" in joined or "assertRaises" in joined:
            continue
        rep = min(file_slices, key=lambda s: s.start_line)
        key = f"{rep.file_path}::{rep.symbol_name}"
        existing = result.get(key, [])
        if any(s.category == "qualidade_de_testes" for s in existing):
            continue
        result[key] = [
            *existing,
            DefectSuspicion(
                category="qualidade_de_testes",
                symbol_name=rep.symbol_name,
                file_path=rep.file_path,
                line_number=rep.start_line,
                trigger_evidence="Arquivo de teste sem nenhum caminho de erro: 'pytest.raises'/'assertRaises' ausente em todo o módulo.",
                noul_hypothesis=DEFECT_NOUL_HYPOTHESES["qualidade_de_testes"],
            ),
        ]
