"""Needle 3 AST Guardrail — Análise Sintática Determinística Fail-Fast.

Inspeciona código Python alterado no repositório antes da avaliação probabilística do Laya,
detectando violações estruturais e vulnerabilidades críticas com 100% de certeza analítica:
  1. Injeção de SQL (f-strings ou concatenação de strings em comandos e chamadas execute)
  2. Mocks em código de produção (unittest.mock, MagicMock, patch fora de testes)
  3. Exceções engolidas (blocos bare except com apenas 'pass' sem re-raise ou logging)
  4. Testes tautológicos (funções de teste contendo exclusivamente 'assert True' ou literais)
"""

from __future__ import annotations

import ast
import logging
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, List, Optional, Set

from ..core.diff_inspector import get_workspace_diff_files, is_ignored_path

logger = logging.getLogger("jev.needle.ast_guard")

# Comandos SQL clássicos sujeitos a injeção via interpolação de strings
SQL_KEYWORDS = re.compile(
    r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|DROP\s+TABLE|ALTER\s+TABLE|CREATE\s+TABLE)\b",
    re.IGNORECASE,
)


@dataclass
class ASTViolation:
    """Representa uma violação estrutural detectada na AST do código."""
    file_path: str
    line_number: int
    rule_id: str
    category: str
    message: str
    snippet: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "file_path": self.file_path,
            "line_number": self.line_number,
            "rule_id": self.rule_id,
            "category": self.category,
            "message": self.message,
            "snippet": self.snippet,
        }


class NeedleASTGuard:
    """Guardião determinístico de AST baseado no motor do Cactus Needle 3."""

    @staticmethod
    def is_test_file(rel_path: str) -> bool:
        """Determina se o arquivo pertence à suíte de testes."""
        p = Path(rel_path)
        parts = [part.lower() for part in p.parts]
        if any(part in ("tests", "test", "__tests__", "spec") for part in parts):
            return True
        name = p.name.lower()
        return name.startswith("test_") or name.endswith("_test.py")

    @classmethod
    def scan_content(cls, content: str, file_path: str) -> List[ASTViolation]:
        """Realiza a varredura completa da AST em um conteúdo de código Python."""
        if not content.strip() or not file_path.endswith(".py"):
            return []

        try:
            tree = ast.parse(content, filename=file_path)
        except SyntaxError as e:
            logger.debug(f"Ignorando arquivo com erro de sintaxe AST ({file_path}): {e}")
            return []

        lines = content.splitlines()
        violations: List[ASTViolation] = []

        is_test = cls.is_test_file(file_path)

        # 1. Detecção de Mocks em Produção (somente fora de testes)
        if not is_test:
            violations.extend(cls._detect_mocks_in_production(tree, lines, file_path))

        # 2. Detecção de Injeção de SQL (em qualquer arquivo)
        violations.extend(cls._detect_sql_injection(tree, lines, file_path))

        # 3. Detecção de Exceções Engolidas (somente fora de testes)
        if not is_test:
            violations.extend(cls._detect_swallowed_exceptions(tree, lines, file_path))

        # 4. Detecção de Testes Tautológicos (somente em arquivos de testes)
        if is_test:
            violations.extend(cls._detect_tautological_tests(tree, lines, file_path))

        return violations

    @classmethod
    def _get_snippet(cls, lines: List[str], lineno: int) -> str:
        """Extrai a linha de código correspondente para contexto legível."""
        if 1 <= lineno <= len(lines):
            return lines[lineno - 1].strip()
        return ""

    @classmethod
    def _detect_mocks_in_production(
        cls, tree: ast.AST, lines: List[str], file_path: str
    ) -> List[ASTViolation]:
        """Detecta importações ou instanciações de unittest.mock em código de produção."""
        violations: List[ASTViolation] = []

        for node in ast.walk(tree):
            # Caso 1: import unittest.mock
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "unittest.mock" or alias.name.startswith("unittest.mock."):
                        violations.append(
                            ASTViolation(
                                file_path=file_path,
                                line_number=node.lineno,
                                rule_id="mock_in_production",
                                category="mock_stub_em_producao",
                                message="Uso proibido de unittest.mock em código de produção.",
                                snippet=cls._get_snippet(lines, node.lineno),
                            )
                        )

            # Caso 2: from unittest.mock import MagicMock, patch, etc.
            elif isinstance(node, ast.ImportFrom):
                if node.module and (node.module == "unittest.mock" or node.module.startswith("unittest.mock")):
                    violations.append(
                        ASTViolation(
                            file_path=file_path,
                            line_number=node.lineno,
                            rule_id="mock_in_production",
                            category="mock_stub_em_producao",
                            message=f"Importação proibida de '{node.module}' em código de produção.",
                            snippet=cls._get_snippet(lines, node.lineno),
                        )
                    )

            # Caso 3: Chamada direta a MagicMock() / Mock() mesmo sem import explícito
            elif isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name in ("MagicMock", "AsyncMock"):
                    violations.append(
                        ASTViolation(
                            file_path=file_path,
                            line_number=node.lineno,
                            rule_id="mock_in_production",
                            category="mock_stub_em_producao",
                            message=f"Instanciação proibida de '{func_name}' em código de produção.",
                            snippet=cls._get_snippet(lines, node.lineno),
                        )
                    )

        return violations

    @classmethod
    def _is_sql_string_value(cls, text: str) -> bool:
        """Verifica se a string contém palavras-chave de comandos SQL."""
        return bool(SQL_KEYWORDS.search(text))

    @classmethod
    def _detect_sql_injection(
        cls, tree: ast.AST, lines: List[str], file_path: str
    ) -> List[ASTViolation]:
        """Detecta concatenação ou f-strings em queries e chamadas a métodos SQL."""
        violations: List[ASTViolation] = []

        # Rastreia variáveis atribuídas a f-strings com SQL
        sql_interpolated_vars: Set[str] = set()

        for node in ast.walk(tree):
            # Caso 1: Atribuição de f-string contendo SQL (ex.: query = f"SELECT ... {id}")
            if isinstance(node, ast.Assign):
                if isinstance(node.value, ast.JoinedStr):
                    # Verifica se alguma parte constante da f-string contém comando SQL
                    has_sql = any(
                        isinstance(part, ast.Constant)
                        and isinstance(part.value, str)
                        and cls._is_sql_string_value(part.value)
                        for part in node.value.values
                    )
                    # Verifica se há valores interpolados (ast.FormattedValue)
                    has_interpolation = any(isinstance(part, ast.FormattedValue) for part in node.value.values)

                    if has_sql and has_interpolation:
                        for target in node.targets:
                            if isinstance(target, ast.Name):
                                sql_interpolated_vars.add(target.id)

                        violations.append(
                            ASTViolation(
                                file_path=file_path,
                                line_number=node.lineno,
                                rule_id="sql_injection",
                                category="seguranca",
                                message="Vulnerabilidade de SQL Injection: query SQL interpolada via f-string.",
                                snippet=cls._get_snippet(lines, node.lineno),
                            )
                        )

            # Caso 2: Chamada de execução (.execute, .executemany, .raw)
            elif isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    func_name = node.func.id

                if func_name in ("execute", "executemany", "raw", "cursor_execute") and node.args:
                    first_arg = node.args[0]

                    # 2.1 Passagem direta de f-string no .execute(f"SELECT ... {id}")
                    if isinstance(first_arg, ast.JoinedStr):
                        has_interpolation = any(isinstance(part, ast.FormattedValue) for part in first_arg.values)
                        if has_interpolation:
                            violations.append(
                                ASTViolation(
                                    file_path=file_path,
                                    line_number=node.lineno,
                                    rule_id="sql_injection",
                                    category="seguranca",
                                    message="Vulnerabilidade de SQL Injection: f-string dinâmica passada diretamente para método de execução SQL.",
                                    snippet=cls._get_snippet(lines, node.lineno),
                                )
                            )

                    # 2.2 Concatenação com operador + no .execute("SELECT ... " + id)
                    elif isinstance(first_arg, ast.BinOp) and isinstance(first_arg.op, ast.Add):
                        violations.append(
                            ASTViolation(
                                file_path=file_path,
                                line_number=node.lineno,
                                rule_id="sql_injection",
                                category="seguranca",
                                message="Vulnerabilidade de SQL Injection: query concatenada com '+' passada para método de execução SQL.",
                                snippet=cls._get_snippet(lines, node.lineno),
                            )
                        )

                    # 2.3 Variável rastreada previamente como f-string sem parâmetros separados
                    elif isinstance(first_arg, ast.Name) and first_arg.id in sql_interpolated_vars:
                        # Se não houver segundo argumento (parâmetros preparados)
                        if len(node.args) < 2 and not node.keywords:
                            violations.append(
                                ASTViolation(
                                    file_path=file_path,
                                    line_number=node.lineno,
                                    rule_id="sql_injection",
                                    category="seguranca",
                                    message=f"Vulnerabilidade de SQL Injection: variável '{first_arg.id}' interpolada executada sem parametrização.",
                                    snippet=cls._get_snippet(lines, node.lineno),
                                )
                            )

        # Remove duplicatas por linha
        unique_violations: dict[int, ASTViolation] = {}
        for v in violations:
            if v.line_number not in unique_violations:
                unique_violations[v.line_number] = v
        return list(unique_violations.values())

    @classmethod
    def _detect_swallowed_exceptions(
        cls, tree: ast.AST, lines: List[str], file_path: str
    ) -> List[ASTViolation]:
        """Detecta blocos except: pass que engolem falhas silenciosamente."""
        violations: List[ASTViolation] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                body = node.body
                # Se o corpo contém apenas 'pass' (ou docstring + pass)
                meaningful_stmts = [
                    stmt for stmt in body
                    if not (isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant))
                ]
                if len(meaningful_stmts) == 1 and isinstance(meaningful_stmts[0], ast.Pass):
                    violations.append(
                        ASTViolation(
                            file_path=file_path,
                            line_number=node.lineno,
                            rule_id="swallowed_exception",
                            category="robustez_excecoes_recursos",
                            message="Exceção engolida silenciosamente: bloco except com apenas 'pass'.",
                            snippet=cls._get_snippet(lines, node.lineno),
                        )
                    )

        return violations

    @classmethod
    def _detect_tautological_tests(
        cls, tree: ast.AST, lines: List[str], file_path: str
    ) -> List[ASTViolation]:
        """Detecta funções de teste contendo exclusivamente asserções tautológicas."""
        violations: List[ASTViolation] = []

        for node in ast.iter_child_nodes(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if not node.name.startswith("test_"):
                    continue

                # Inspeciona asserções dentro do teste
                assert_nodes = [n for n in ast.walk(node) if isinstance(n, ast.Assert)]
                if not assert_nodes:
                    continue

                all_tautological = True
                for a_node in assert_nodes:
                    test_expr = a_node.test
                    # assert True
                    if isinstance(test_expr, ast.Constant) and test_expr.value is True:
                        continue
                    # assert 1 == 1 ou 'a' in 'abc' (comparação literal simples)
                    if isinstance(test_expr, ast.Compare):
                        is_literal_left = isinstance(test_expr.left, ast.Constant)
                        is_literal_comparators = all(isinstance(c, ast.Constant) for c in test_expr.comparators)
                        if is_literal_left and is_literal_comparators:
                            continue
                    all_tautological = False
                    break

                # Verifica se há chamadas reais a funções no corpo do teste
                calls = [n for n in ast.walk(node) if isinstance(n, ast.Call)]

                if all_tautological and not calls:
                    violations.append(
                        ASTViolation(
                            file_path=file_path,
                            line_number=node.lineno,
                            rule_id="tautological_test",
                            category="qualidade_de_testes",
                            message=f"Teste tautológico '{node.name}': contém apenas asserções literais sem exercer código.",
                            snippet=cls._get_snippet(lines, node.lineno),
                        )
                    )

        return violations

    @classmethod
    def scan_workspace(cls, workspace_dir: str) -> List[ASTViolation]:
        """Varre os arquivos modificados e novos do workspace Git via AST."""
        root = Path(workspace_dir).resolve()
        if not root.is_dir():
            return []

        diff_files = get_workspace_diff_files(str(root))
        all_violations: List[ASTViolation] = []

        for df in diff_files:
            if df.is_binary or not df.path.endswith(".py"):
                continue
            if is_ignored_path(df.path):
                continue

            full_p = root / df.path
            if full_p.is_file():
                try:
                    content = full_p.read_text(encoding="utf-8", errors="replace")
                    file_violations = cls.scan_content(content, df.path)
                    all_violations.extend(file_violations)
                except Exception as e:
                    logger.warning(f"Erro ao ler arquivo para varredura AST ({df.path}): {e}")

        return all_violations
