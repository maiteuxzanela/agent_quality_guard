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

from ..core.diff_inspector import get_workspace_diff_files, is_ignored_path, normalize_workspace_relpath
from .ast_cache import get_cached_ast

logger = logging.getLogger("agent_quality_guard.needle.ast_guard")

# Comandos SQL clássicos sujeitos a injeção via interpolação de strings
SQL_KEYWORDS = re.compile(
    r"\b(SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM|DROP\s+TABLE|ALTER\s+TABLE|CREATE\s+TABLE)\b",
    re.IGNORECASE,
)


@dataclass
class ASTViolation:
    """Representa uma violação estrutural detectada na AST do código.

    Exemplo:
        >>> ASTViolation(file_path="a.py", line_number=1, rule_id="r", category="c", message="m").to_dict()["rule_id"]
        'r'
    """
    file_path: str
    line_number: int
    rule_id: str
    category: str
    message: str
    code_snippet: str = ""
    snippet: str = ""
    suggestion: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.code_snippet and self.snippet:
            self.code_snippet = self.snippet
        elif not self.snippet and self.code_snippet:
            self.snippet = self.code_snippet

    def to_dict(self) -> dict[str, Any]:
        """Converte a violação em dicionário serializável para relatórios e respostas MCP.

        Exemplo:
            >>> ASTViolation(file_path="a.py", line_number=1, rule_id="r", category="c", message="m").to_dict()["line_number"]
            1
        """
        return {
            "rule_id": self.rule_id,
            "category": self.category,
            "file_path": self.file_path,
            "line_number": self.line_number,
            "code_snippet": self.code_snippet,
            "message": self.message,
            "snippet": self.snippet,
            "suggestion": self.suggestion,
        }


class NeedleASTGuard:
    """Guardião determinístico de AST baseado no motor do Cactus Needle 3."""

    RULE_SUGGESTIONS: dict[str, str] = {
        "sql_injection": "Utilize queries parametrizadas com placeholders (?, %s ou :param). Nunca interpole variáveis via f-strings ou concatenação de strings.",
        "SQL_INJECTION": "Utilize queries parametrizadas com placeholders (?, %s ou :param). Nunca interpole variáveis via f-strings ou concatenação de strings.",
        "mock_in_production": "Remova unittest.mock, MagicMock ou patch de código de produção. Mocks pertencem exclusivamente aos arquivos em tests/.",
        "PROD_MOCK": "Remova unittest.mock, MagicMock ou patch de código de produção. Mocks pertencem exclusivamente aos arquivos em tests/.",
        "swallowed_exception": "Trate a exceção explicitamente, registre com logging.exception() ou utilize 'raise' para propagar o erro.",
        "SWALLOWED_EXCEPTION": "Trate a exceção explicitamente, registre com logging.exception() ou utilize 'raise' para propagar o erro.",
        "tautological_test": "Substitua a asserção tautológica (ex: assert True) por uma verificação real do resultado da função ou estado esperado.",
        "TEST_TAUTOLOGY": "Substitua a asserção tautológica (ex: assert True) por uma verificação real do resultado da função ou estado esperado.",
        "hardcoded_secret": "Mova a credencial/chave para variáveis de ambiente (.env) ou adicione '# needle-ignore: HARDCODED_SECRET' se for um valor de teste/desenvolvimento.",
        "HARDCODED_SECRET": "Mova a credencial/chave para variáveis de ambiente (.env) ou adicione '# needle-ignore: HARDCODED_SECRET' se for um valor de teste/desenvolvimento.",
        "command_injection": "Evite shell=True com comandos dinâmicos; passe argumentos como lista de strings (ex: ['cmd', arg1, arg2]).",
        "COMMAND_INJECTION": "Evite shell=True com comandos dinâmicos; passe argumentos como lista de strings (ex: ['cmd', arg1, arg2]).",
        "insecure_deserialization": "Evite pickle.loads() com dados não confiáveis; utilize json ou formatos seguros de serialização. Para YAML, use yaml.safe_load().",
        "INSECURE_DESERIALIZATION": "Evite pickle.loads() com dados não confiáveis; utilize json ou formatos seguros de serialização. Para YAML, use yaml.safe_load().",
        "dangerous_eval_exec": "Remova eval() ou exec(). Utilize ast.literal_eval() para dados literais ou funções estruturadas.",
        "DANGEROUS_EVAL_EXEC": "Remova eval() ou exec(). Utilize ast.literal_eval() para dados literais ou funções estruturadas.",
        "insecure_ssl_verify": "Mantenha verify=True para conexões seguras. Em ambientes de desenvolvimento/teste isolados, utilize certificados confiáveis locais.",
        "INSECURE_SSL": "Mantenha verify=True para conexões seguras. Em ambientes de desenvolvimento/teste isolados, utilize certificados confiáveis locais.",
    }

    @classmethod
    def _get_file_suppressed_rules(cls, lines: List[str]) -> set[str]:
        """Extrai regras suprimidas globalmente no arquivo via diretiva de cabeçalho.

        Exemplo:
            >>> "SECURITY_EVAL" in NeedleASTGuard._get_file_suppressed_rules(["# needle-ignore-file: SECURITY_EVAL"])
            True
        """
        suppressed: set[str] = set()
        file_pat = re.compile(
            r"(?://|/\*|#)\s*needle-ignore-file:\s*([A-Za-z0-9_,\s]+)",
            re.IGNORECASE,
        )
        all_pat = re.compile(
            r"(?://|/\*|#)\s*needle-ignore-all\b",
            re.IGNORECASE,
        )
        header_lines = lines[:15]
        for line in header_lines:
            line_clean = line.strip()
            if all_pat.search(line_clean):
                suppressed.add("ALL")
                return suppressed
            m = file_pat.search(line_clean)
            if m:
                tokens = [r.strip().upper() for r in m.group(1).split(",") if r.strip()]
                suppressed.update(tokens)

        # Expansão de aliases fora do loop de linhas para zero aninhamento
        all_aliases: set[str] = set()
        for tok in list(suppressed):
            all_aliases.update(cls._get_rule_aliases(tok))
        suppressed.update(all_aliases)
        return suppressed

    @classmethod
    def _is_suppressed(
        cls,
        rule_id: str,
        line_number: int,
        lines: List[str],
        file_suppressed: Optional[set[str]] = None,
    ) -> bool:
        """Verifica se a linha, linha anterior ou arquivo possui anotação needle-ignore.

        Exemplo:
            >>> NeedleASTGuard._is_suppressed("SQL_INJECTION", 1, ["# needle-ignore: SQL_INJECTION"])
            True
        """
        target = rule_id.upper()
        match_targets = cls._get_rule_aliases(target)

        if file_suppressed and ("ALL" in file_suppressed or any(t in file_suppressed for t in match_targets)):
            return True

        curr_idx = line_number - 1
        if 0 <= curr_idx < len(lines):
            if cls._line_matches_inline(lines[curr_idx], match_targets):
                return True

        prev_idx = line_number - 2
        if 0 <= prev_idx < len(lines):
            if cls._line_matches_prev(lines[prev_idx], match_targets):
                return True

        return False

    @staticmethod
    def _get_rule_aliases(target: str) -> set[str]:
        """Retorna conjunto de aliases conhecidos para a regra informada."""
        aliases = {
            "MOCK_IN_PRODUCTION": ["PROD_MOCK", "MOCK_IN_PRODUCTION"],
            "PROD_MOCK": ["PROD_MOCK", "MOCK_IN_PRODUCTION"],
            "TAUTOLOGICAL_TEST": ["TEST_TAUTOLOGY", "TAUTOLOGICAL_TEST"],
            "TEST_TAUTOLOGY": ["TEST_TAUTOLOGY", "TAUTOLOGICAL_TEST"],
            "INSECURE_SSL_VERIFY": ["INSECURE_SSL", "INSECURE_SSL_VERIFY"],
            "INSECURE_SSL": ["INSECURE_SSL", "INSECURE_SSL_VERIFY"],
        }
        result = set(aliases.get(target, [target]))
        result.add(target)
        return result

    @staticmethod
    def _line_matches_inline(line: str, match_targets: set[str]) -> bool:
        """Verifica se linha atual contém anotação inline needle-ignore válida."""
        pat = re.compile(
            r"(?://|/\*|#)\s*needle-ignore:\s*([A-Za-z0-9_,\s]+)",
            re.IGNORECASE,
        )
        m = pat.search(line)
        if not m:
            return False
        rules = [r.strip().upper() for r in m.group(1).split(",")]
        return any(t in rules for t in match_targets) or "ALL" in rules

    @staticmethod
    def _line_matches_prev(prev_line: str, match_targets: set[str]) -> bool:
        """Verifica se linha anterior contém anotação needle-ignore ou next-line."""
        stripped = prev_line.strip()
        next_pat = re.compile(
            r"(?://|/\*|#)\s*needle-ignore-next-line:\s*([A-Za-z0-9_,\s]+)",
            re.IGNORECASE,
        )
        m_next = next_pat.search(stripped)
        if m_next:
            rules = [r.strip().upper() for r in m_next.group(1).split(",")]
            if any(t in rules for t in match_targets) or "ALL" in rules:
                return True

        if not stripped.startswith(("//", "/*", "#", "*")):
            return False

        inline_pat = re.compile(
            r"(?://|/\*|#)\s*needle-ignore:\s*([A-Za-z0-9_,\s]+)",
            re.IGNORECASE,
        )
        m_inline = inline_pat.search(stripped)
        if not m_inline:
            return False
        rules = [r.strip().upper() for r in m_inline.group(1).split(",")]
        return any(t in rules for t in match_targets) or "ALL" in rules

    @staticmethod
    def is_test_file(rel_path: str) -> bool:
        """Determina se o arquivo pertence à suíte de testes (pasta tests/ ou prefixo test_).

        Exemplo:
            >>> NeedleASTGuard.is_test_file("tests/test_engine.py")
            True
        """
        p = Path(rel_path.replace("\\", "/"))
        parts = [part.lower() for part in p.parts]
        if not parts:
            return False
        # Prefixo test_ vale só no NOME do arquivo: aceitá-lo em componente de
        # diretório (ex.: src/test_utils/) isentava produção da Camada 1.
        dir_parts, name = parts[:-1], parts[-1]
        if any(part in ("tests", "test", "__tests__", "spec") for part in dir_parts):
            return True
        return name.startswith("test_") or name.endswith("_test.py")

    @classmethod
    def scan_content(cls, content: str, file_path: str) -> List[ASTViolation]:
        """Realiza a varredura completa da AST em um conteúdo de código Python."""
        if not content.strip() or not file_path.endswith(".py"):
            return []

        tree = get_cached_ast(content, filename=file_path)
        if tree is None:
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

        # 5. Detecção de Credenciais e Segredos Hardcoded (somente fora de testes)
        if not is_test:
            violations.extend(cls._detect_hardcoded_secrets(tree, lines, file_path))

        # 6. Detecção de Injeção de Comandos do Sistema (shell=True com f-string)
        violations.extend(cls._detect_command_injection(tree, lines, file_path))

        # 7. Detecção de Deserialização Insegura (pickle.loads / yaml.load)
        violations.extend(cls._detect_insecure_deserialization(tree, lines, file_path))

        # 8. Detecção de Execução Dinâmica Perigosa (eval / exec dinâmico)
        violations.extend(cls._detect_dangerous_eval_exec(tree, lines, file_path))

        # 9. Detecção de Desativação de Verificação SSL (verify=False)
        violations.extend(cls._detect_insecure_ssl(tree, lines, file_path))

        file_suppressed = cls._get_file_suppressed_rules(lines)
        filtered_violations: List[ASTViolation] = []
        for v in violations:
            if not cls._is_suppressed(v.rule_id, v.line_number, lines, file_suppressed):
                if not v.suggestion:
                    v.suggestion = cls.RULE_SUGGESTIONS.get(v.rule_id)
                filtered_violations.append(v)

        return filtered_violations

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
        """Detecta importações ou instanciações de unittest.mock / mock em código de produção."""
        violations: List[ASTViolation] = []

        for node in ast.walk(tree):
            # Caso 1: import unittest.mock ou import mock
            if isinstance(node, ast.Import):
                mock_module_names = [
                    alias.name
                    for alias in node.names
                    if alias.name in ("unittest.mock", "mock")
                    or alias.name.startswith("unittest.mock.")
                ]
                violations.extend(
                    ASTViolation(
                        file_path=file_path,
                        line_number=node.lineno,
                        rule_id="mock_in_production",
                        category="mock_stub_em_producao",
                        message=f"Uso proibido de '{module_name}' em código de produção.",
                        code_snippet=cls._get_snippet(lines, node.lineno),
                    )
                    for module_name in mock_module_names
                )

            # Caso 2: from unittest.mock import MagicMock, patch, etc. ou from mock import ...
            elif isinstance(node, ast.ImportFrom):
                if node.module and (
                    node.module in ("unittest.mock", "mock")
                    or node.module.startswith("unittest.mock.")
                ):
                    violations.append(
                        ASTViolation(
                            file_path=file_path,
                            line_number=node.lineno,
                            rule_id="mock_in_production",
                            category="mock_stub_em_producao",
                            message=f"Importação proibida de '{node.module}' em código de produção.",
                            code_snippet=cls._get_snippet(lines, node.lineno),
                        )
                    )

            # Caso 3: Chamada direta a MagicMock(), Mock(), AsyncMock(), PropertyMock(), patch()
            elif isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Name):
                    func_name = node.func.id
                elif isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name in ("MagicMock", "Mock", "AsyncMock", "PropertyMock", "patch", "create_autospec"):
                    violations.append(
                        ASTViolation(
                            file_path=file_path,
                            line_number=node.lineno,
                            rule_id="mock_in_production",
                            category="mock_stub_em_producao",
                            message=f"Instanciação proibida de '{func_name}' em código de produção.",
                            code_snippet=cls._get_snippet(lines, node.lineno),
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
                fstring_parts = node.value.values if isinstance(node.value, ast.JoinedStr) else []
                # Verifica se alguma parte constante da f-string contém comando SQL
                has_sql = any(
                    isinstance(part, ast.Constant)
                    and isinstance(part.value, str)
                    and cls._is_sql_string_value(part.value)
                    for part in fstring_parts
                )
                # Verifica se há valores interpolados (ast.FormattedValue)
                has_interpolation = any(isinstance(part, ast.FormattedValue) for part in fstring_parts)

                if has_sql and has_interpolation:
                    sql_interpolated_vars.update(
                        target.id for target in node.targets if isinstance(target, ast.Name)
                    )

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

                is_sql_execute = (
                    func_name in ("execute", "executemany", "raw", "cursor_execute") and bool(node.args)
                )
                execute_violation = (
                    cls._detect_execute_sql_violation(
                        node, node.args[0], sql_interpolated_vars, file_path, lines
                    )
                    if is_sql_execute
                    else None
                )
                if execute_violation is not None:
                    violations.append(execute_violation)

        # Remove duplicatas por linha
        unique_violations: dict[int, ASTViolation] = {}
        for v in violations:
            if v.line_number not in unique_violations:
                unique_violations[v.line_number] = v
        return list(unique_violations.values())

    @classmethod
    def _detect_execute_sql_violation(
        cls,
        node: ast.Call,
        first_arg: ast.expr,
        sql_interpolated_vars: Set[str],
        file_path: str,
        lines: List[str],
    ) -> Optional[ASTViolation]:
        """Retorna a violação de SQL Injection de uma chamada `.execute()`, quando houver."""
        # 2.1 Passagem direta de f-string no .execute(f"SELECT ... {id}")
        has_interpolation = isinstance(first_arg, ast.JoinedStr) and any(
            isinstance(part, ast.FormattedValue) for part in first_arg.values
        )
        if has_interpolation:
            return ASTViolation(
                file_path=file_path,
                line_number=node.lineno,
                rule_id="sql_injection",
                category="seguranca",
                message="Vulnerabilidade de SQL Injection: f-string dinâmica passada diretamente para método de execução SQL.",
                snippet=cls._get_snippet(lines, node.lineno),
            )

        # 2.2 Concatenação com operador + no .execute("SELECT ... " + id)
        if isinstance(first_arg, ast.BinOp) and isinstance(first_arg.op, ast.Add):
            return ASTViolation(
                file_path=file_path,
                line_number=node.lineno,
                rule_id="sql_injection",
                category="seguranca",
                message="Vulnerabilidade de SQL Injection: query concatenada com '+' passada para método de execução SQL.",
                snippet=cls._get_snippet(lines, node.lineno),
            )

        # 2.3 Variável rastreada previamente como f-string sem parâmetros separados
        is_tracked_var = isinstance(first_arg, ast.Name) and first_arg.id in sql_interpolated_vars
        if is_tracked_var and isinstance(first_arg, ast.Name) and len(node.args) < 2 and not node.keywords:
            return ASTViolation(
                file_path=file_path,
                line_number=node.lineno,
                rule_id="sql_injection",
                category="seguranca",
                message=f"Vulnerabilidade de SQL Injection: variável '{first_arg.id}' interpolada executada sem parametrização.",
                snippet=cls._get_snippet(lines, node.lineno),
            )
        return None

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
    def _is_trivially_true_assert(cls, test_expr: ast.expr) -> bool:
        """True para `assert True` ou comparação entre literais (tautologia trivial).

        Exemplo:
            >>> NeedleASTGuard._is_trivially_true_assert(ast.parse("1 == 1").body[0].value)
            True
        """
        if isinstance(test_expr, ast.Constant) and test_expr.value is True:
            return True
        if isinstance(test_expr, ast.Compare):
            is_literal_left = isinstance(test_expr.left, ast.Constant)
            is_literal_comparators = all(isinstance(c, ast.Constant) for c in test_expr.comparators)
            return is_literal_left and is_literal_comparators
        return False

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

                all_tautological = all(
                    cls._is_trivially_true_assert(a_node.test) for a_node in assert_nodes
                )

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

    SECRET_VAR_NAMES = re.compile(
        r"^(API_KEY|SECRET_KEY|PRIVATE_KEY|ACCESS_TOKEN|AUTH_TOKEN|PASSWORD|PASSWD|DB_PASSWORD|SECRET|API_TOKEN|TOKEN)$",
        re.IGNORECASE,
    )
    SECRET_PATTERNS = (
        re.compile(r"sk-[a-zA-Z0-9_\-]{3,}"),
        re.compile(r"ghp_[a-zA-Z0-9]{20,}"),
        re.compile(r"AKIA[0-9A-Z]{16}"),
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY"),
    )

    @classmethod
    def _is_hardcoded_secret_literal(cls, raw_value: str) -> bool:
        """True apenas para literais com cara de segredo real (não placeholder nem curto demais).

        Exemplo:
            >>> NeedleASTGuard._is_hardcoded_secret_literal("sk-live-12345")
            True
            >>> NeedleASTGuard._is_hardcoded_secret_literal("your_api_key_here")
            False
        """
        val = raw_value.strip()
        if len(val) < 3:
            return False
        placeholders = (
            "dummy", "test", "fake", "placeholder", "xxx",
            "your_", "example", "sample", "change_me",
        )
        return not any(p in val.lower() for p in placeholders)

    @classmethod
    def _detect_hardcoded_secrets(
        cls, tree: ast.AST, lines: List[str], file_path: str
    ) -> List[ASTViolation]:
        """Detecta credenciais, tokens de API e senhas expostas no código de produção."""
        violations: List[ASTViolation] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                # Caso 1: Nome da variável sugestivo (API_KEY, PASSWORD, etc.)
                is_secret_var = any(
                    isinstance(target, ast.Name) and cls.SECRET_VAR_NAMES.match(target.id)
                    for target in node.targets
                )
                literal_value = (
                    node.value.value
                    if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
                    else None
                )

                if (
                    is_secret_var
                    and literal_value is not None
                    and cls._is_hardcoded_secret_literal(literal_value)
                ):
                    violations.append(
                        ASTViolation(
                            file_path=file_path,
                            line_number=node.lineno,
                            rule_id="hardcoded_secret",
                            category="seguranca",
                            message="Credencial ou segredo hardcoded detectado em código de produção.",
                            code_snippet=cls._get_snippet(lines, node.lineno),
                        )
                    )
                    continue

                # Caso 2: Padrões regex de tokens conhecidos em qualquer string constante
                matches_known_token = (
                    literal_value is not None
                    and any(pat.search(literal_value) for pat in cls.SECRET_PATTERNS)
                )
                if matches_known_token:
                    violations.append(
                        ASTViolation(
                            file_path=file_path,
                            line_number=node.lineno,
                            rule_id="hardcoded_secret",
                            category="seguranca",
                            message="Token de serviço ou chave privada reconhecida exposta no código.",
                            code_snippet=cls._get_snippet(lines, node.lineno),
                        )
                    )

        return violations

    @classmethod
    def _detect_command_injection(
        cls, tree: ast.AST, lines: List[str], file_path: str
    ) -> List[ASTViolation]:
        """Detecta execução de comandos do sistema operacional com shell=True e interpolação."""
        violations: List[ASTViolation] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    func_name = node.func.id

                # subprocess.run/Popen/call com shell=True
                if func_name in ("run", "Popen", "call", "check_output", "check_call"):
                    has_shell_true = any(
                        kw.arg == "shell" and isinstance(kw.value, ast.Constant) and kw.value.value is True
                        for kw in node.keywords
                    )
                    if has_shell_true and node.args:
                        cmd_arg = node.args[0]
                        if isinstance(cmd_arg, (ast.JoinedStr, ast.BinOp)):
                            violations.append(
                                ASTViolation(
                                    file_path=file_path,
                                    line_number=node.lineno,
                                    rule_id="command_injection",
                                    category="seguranca",
                                    message="Injeção de comando do sistema: subprocess com shell=True e comando interpolado dinamicamente.",
                                    snippet=cls._get_snippet(lines, node.lineno),
                                )
                            )

                # os.system / os.popen com interpolação
                elif func_name in ("system", "popen") and node.args:
                    cmd_arg = node.args[0]
                    if isinstance(cmd_arg, (ast.JoinedStr, ast.BinOp)):
                        violations.append(
                            ASTViolation(
                                file_path=file_path,
                                line_number=node.lineno,
                                rule_id="command_injection",
                                category="seguranca",
                                message=f"Injeção de comando do sistema: chamada '{func_name}' com comando concatenado/interpolado.",
                                snippet=cls._get_snippet(lines, node.lineno),
                            )
                        )

        return violations

    @classmethod
    def _detect_insecure_deserialization(
        cls, tree: ast.AST, lines: List[str], file_path: str
    ) -> List[ASTViolation]:
        """Detecta deserialização insegura que permite Execução Remota de Código (RCE)."""
        violations: List[ASTViolation] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = ""
                module_name = ""
                if isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                    if isinstance(node.func.value, ast.Name):
                        module_name = node.func.value.id

                # pickle.loads / pickle.load
                if module_name == "pickle" and func_name in ("loads", "load"):
                    violations.append(
                        ASTViolation(
                            file_path=file_path,
                            line_number=node.lineno,
                            rule_id="insecure_deserialization",
                            category="seguranca",
                            message=f"Deserialização insegura com pickle.{func_name} (risco de RCE).",
                            snippet=cls._get_snippet(lines, node.lineno),
                        )
                    )

                # yaml.load sem SafeLoader
                elif module_name == "yaml" and func_name == "load":
                    has_safe_loader = any(
                        kw.arg == "Loader" and "safe" in getattr(kw.value, "id", "").lower()
                        for kw in node.keywords
                    )
                    if not has_safe_loader:
                        violations.append(
                            ASTViolation(
                                file_path=file_path,
                                line_number=node.lineno,
                                rule_id="insecure_deserialization",
                                category="seguranca",
                                message="yaml.load sem SafeLoader: risco de deserialização arbitrária.",
                                snippet=cls._get_snippet(lines, node.lineno),
                            )
                        )

        return violations

    @classmethod
    def _detect_dangerous_eval_exec(
        cls, tree: ast.AST, lines: List[str], file_path: str
    ) -> List[ASTViolation]:
        """Detecta execução dinâmica arbitrária de código via eval/exec com entradas dinâmicas."""
        violations: List[ASTViolation] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                if node.func.id in ("eval", "exec") and node.args:
                    arg = node.args[0]
                    # Se não for uma constante literal fixa
                    if not isinstance(arg, ast.Constant):
                        violations.append(
                            ASTViolation(
                                file_path=file_path,
                                line_number=node.lineno,
                                rule_id="dangerous_eval_exec",
                                category="seguranca",
                                message=f"Execução dinâmica perigosa de código via '{node.func.id}()' com argumento dinâmico.",
                                snippet=cls._get_snippet(lines, node.lineno),
                            )
                        )

        return violations

    @classmethod
    def _detect_insecure_ssl(
        cls, tree: ast.AST, lines: List[str], file_path: str
    ) -> List[ASTViolation]:
        """Detecta desativação explícita de verificação de certificados SSL/TLS."""
        violations: List[ASTViolation] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                if func_name in ("get", "post", "put", "delete", "request", "patch"):
                    has_verify_false = any(
                        kw.arg == "verify" and isinstance(kw.value, ast.Constant) and kw.value.value is False
                        for kw in node.keywords
                    )
                    if has_verify_false:
                        violations.append(
                            ASTViolation(
                                file_path=file_path,
                                line_number=node.lineno,
                                rule_id="insecure_ssl_verify",
                                category="seguranca",
                                message=f"Desativação insegura de validação SSL (verify=False) na chamada '{func_name}'.",
                                snippet=cls._get_snippet(lines, node.lineno),
                            )
                        )

        return violations


    @classmethod
    def scan_workspace(cls, workspace_dir: str, audit_mode: str = "delta") -> List[ASTViolation]:
        """Varre os arquivos do workspace via AST (modo delta por diff do Git ou full para varredura completa).

        Exemplo:
            >>> NeedleASTGuard.scan_workspace(".", audit_mode="delta")
            []
        """
        root = Path(workspace_dir).resolve()
        if not root.is_dir():
            return []

        all_violations: List[ASTViolation] = []

        if audit_mode == "full":
            py_files = [
                p
                for p in root.rglob("*.py")
                if not is_ignored_path(str(p.relative_to(root)))
            ]
            for full_p in py_files:
                rel_path = str(full_p.relative_to(root))
                try:
                    content = full_p.read_text(encoding="utf-8", errors="replace")
                    file_violations = cls.scan_content(content, rel_path)
                    all_violations.extend(file_violations)
                except Exception as e:
                    logger.warning(
                        f"Erro ao ler arquivo para varredura AST ({rel_path}): {e}",
                        exc_info=True,
                    )
            return all_violations

        # Default: audit_mode == "delta"
        diff_files = get_workspace_diff_files(str(root))
        for df in diff_files:
            rel_norm = normalize_workspace_relpath(df.path, str(root))
            if df.is_binary or not rel_norm.endswith(".py"):
                continue
            if is_ignored_path(rel_norm):
                continue

            full_p = root / rel_norm
            if full_p.is_file():
                try:
                    content = full_p.read_text(encoding="utf-8", errors="replace")
                    file_violations = cls.scan_content(content, rel_norm)
                    all_violations.extend(file_violations)
                except Exception as e:
                    logger.warning(
                        f"Erro ao ler arquivo para varredura AST ({rel_norm}): {e}",
                        exc_info=True,
                    )

        return all_violations

    # ── Camada 2: Meta-Regras Universais via Tree-Sitter ─────────────────────

    _TREESITTER_RULE_MAP: dict[str, str] = {
        "PROD_MOCK": "mock_in_production",
        "SQL_INJECTION": "sql_injection",
        "DANGEROUS_EVAL": "dangerous_eval_exec",
        "SECURITY_EXEC": "dangerous_eval_exec",
        "COMMAND_INJECTION": "command_injection",
        "SILENT_CATCH": "swallowed_exception",
    }

    _TREESITTER_CATEGORY_MAP: dict[str, str] = {
        "anti_mock_producao": "mock_stub_em_producao",
        "security": "seguranca",
        "structural": "robustez_excecoes_recursos",
    }

    @classmethod
    def scan_workspace_multilang(
        cls,
        workspace_dir: str,
        audit_mode: str = "delta",
        scope: Optional[Any] = None,
    ) -> List[ASTViolation]:
        """Combina o AST Python nativo com as meta-regras Tree-Sitter multi-linguagem.

        A Camada 2 (``TreeSitterGuard``) cobre JS/TS/TSX/Java/Go/Rust/Ruby/PHP
        e demais linguagens mapeadas, enquanto o AST Python permanece 100%
        nativo (``ast``) para evitar dupla sinalização. A indisponibilidade da
        Camada 2 nunca interrompe a varredura: é degradada graciosamente.

        Exemplo:
            >>> NeedleASTGuard.scan_workspace_multilang("/tmp/inexistente")
            []
        """
        violations: List[ASTViolation] = cls.scan_workspace(
            workspace_dir, audit_mode=audit_mode
        )
        try:
            from .tree_sitter_guard import TreeSitterGuard
        except Exception as import_err:  # pragma: no cover - fallback gracioso
            logger.debug("Camada 2 (Tree-Sitter) indisponível: %s", import_err)
            return violations

        try:
            multilang = TreeSitterGuard.scan_workspace(
                workspace_dir, audit_mode=audit_mode, scope=scope
            )
        except Exception as scan_err:  # pragma: no cover - fallback gracioso
            logger.warning("Varredura Tree-Sitter multi-linguagem falhou: %s", scan_err)
            return violations

        for item in multilang:
            if str(item.file_path).endswith((".py", ".pyi")):
                continue
            violations.append(
                ASTViolation(
                    file_path=item.file_path,
                    line_number=item.line_number,
                    rule_id=cls._TREESITTER_RULE_MAP.get(item.rule_id, item.rule_id),
                    category=cls._TREESITTER_CATEGORY_MAP.get(item.category, item.category),
                    message=item.message,
                    code_snippet=item.code_snippet,
                    suggestion=item.suggestion,
                )
            )
        return violations
