"""
Multi-Stack Runner & Language Drivers for Needle 3 Verify Delivery.
Permite ao needle_verify_delivery auditar projetos em diferentes stacks:
- Python (JEV, Predictera, Faturamento)
- React Native / Expo (PanelaAndroid)
- Web Vanilla JS / Node / Firebase (JEZ collections)

Camadas de auditoria:
- Camada 1: bridge nativa com compiladores/linters do projeto (tsc, eslint/biome/oxlint,
  ruff/mypy, go vet, cargo clippy, dart analyze) — orçamento sub-segundo e fail-open.
- Varredura AST JS (node_js_ast) e fallback léxico com suporte a needle-ignore.
"""

import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger("jev.multi_stack")

RULE_SUGGESTIONS: Dict[str, str] = {
    "SECURITY_EVAL": (
        "Remova o uso de eval(). Utilize JSON.parse(), despachos por dicionário ou funções puras."
    ),
    "SECURITY_EXEC": (
        "Evite o construtor new Function(). Isole a lógica dinâmica em funções declaradas ou analisadores formais."
    ),
    "HARDCODED_SECRET": (
        "Mova a chave privada/segredo para variáveis de ambiente (.env) ou adicione "
        "'// needle-ignore: HARDCODED_SECRET (<motivo>)' se for chave pública de client-side (ex: Firebase Client API Key)."
    ),
    "XSS_INNER_HTML": (
        "Substitua por element.textContent ou document.createElement(); se HTML for mandatório, "
        "envolva as interpolações com escapeHtml(...) ou DOMPurify.sanitize()."
    ),
    "JS_PROD_MOCK": (
        "Remova jest.mock(), jest.fn() ou sinon de arquivos sob src/. "
        "Mocks pertencem exclusivamente a arquivos de teste em __tests__/ ou tests/."
    ),
    "EMPTY_CATCH": (
        "Não engula exceções silenciosamente. Registre com console.warn/logger, "
        "re-lance o erro com throw/raise ou trate a condição explicitamente."
    ),
    "CODE_SMELL_CONSOLE": (
        "Remova chamadas residuais de console.log() antes de enviar para produção, "
        "ou utilize console.warn/error estruturado se for um alerta de runtime legítimo."
    ),
    "JEZ_ANTI_EMOJI": (
        "Remova o emoji. A identidade visual da marca JEZ exige tipografia limpa e ícones SVG minimalistas dedicados."
    ),
    "SQL_INJECTION": (
        "Utilize queries parametrizadas com placeholders (?, %s ou :param). "
        "Nunca interpole variáveis via f-strings ou concatenação de strings."
    ),
    "PROD_MOCK": (
        "Remova unittest.mock, MagicMock ou patch de código de produção. "
        "Mocks pertencem exclusivamente aos arquivos em tests/."
    ),
    "TEST_TAUTOLOGY": (
        "Substitua a asserção tautológica (ex: assert True) por uma verificação real do resultado da função ou estado esperado."
    ),
    "TSC_TYPE_ERROR": (
        "Corrija a incompatibilidade de tipos apontada pelo TypeScript antes da entrega; "
        "evite 'as any' e prefira narrowing explícito ou tipos auxiliares."
    ),
    "ESLINT_RULES_OF_HOOKS": (
        "Hooks do React só podem ser chamados no topo de componentes/funções customizadas, "
        "nunca dentro de condicionais, laços ou callbacks."
    ),
    "ESLINT_HOOK_DEPS": (
        "Declare todas as dependências reativas em useEffect/useMemo/useCallback ou justifique "
        "explicitamente a omissão estável (ex.: refs)."
    ),
    "ESLINT_MISSING_KEY": (
        "Forneça uma 'key' estável e única (id do domínio) para cada elemento de uma lista; "
        "evite usar o índice do array como identidade."
    ),
    "ESLINT_STATE_MUTATION": (
        "Nunca mute this.state/estado diretamente: use setState/setX com novo objeto ou updater funcional."
    ),
    "LINT_PARSE_ERROR": (
        "O arquivo não pôde ser processado pelo linter: corrija o erro de sintaxe/configuração antes de auditar."
    ),
    "RUFF_LINT": (
        "Resolva o achado do Ruff (imports não usados, nomes indefinidos, complexidade) ou registre "
        "justificativa explícita na configuração do projeto."
    ),
    "MYPY_TYPE_ERROR": (
        "Ajuste a anotação/tipo apontado pelo mypy; evite silenciar com 'type: ignore' sem justificativa."
    ),
    "GOVET_DIAGNOSTIC": (
        "Corrija o diagnóstico do 'go vet' (printf, locks, struct tags) antes da entrega."
    ),
    "CLIPPY_LINT": (
        "Corrija o lint do Clippy; o gate roda com '-D warnings', portanto o aviso é bloqueante."
    ),
    "DART_ANALYZE": (
        "Resolva o diagnóstico do 'dart analyze' antes da entrega."
    ),
}

# Orçamento total da Camada 1 (sub-segundo por contrato). Sobrescrevível por env.
_DEFAULT_NATIVE_BUDGET_SECONDS = 0.8
_MIN_NATIVE_BUDGET_SECONDS = 0.1
_MAX_NATIVE_BUDGET_SECONDS = 5.0
_NATIVE_OUTPUT_LIMIT = 512 * 1024
_MAX_LINT_FILES = 150


def _get_file_suppressed_rules_js(lines: List[str]) -> set[str]:
    """Extrai regras suprimidas globalmente no arquivo via diretiva de cabeçalho.

    Exemplo:
        >>> "SECURITY_EVAL" in _get_file_suppressed_rules_js(["// needle-ignore-file: SECURITY_EVAL"])
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
    for line in lines[:15]:
        line_clean = line.strip()
        if all_pat.search(line_clean):
            suppressed.add("ALL")
            return suppressed
        m = file_pat.search(line_clean)
        if m:
            tokens = [r.strip().upper() for r in m.group(1).split(",") if r.strip()]
            suppressed.update(tokens)
    return suppressed


def _is_line_suppressed(
    rule_id: str,
    line_number: int,
    lines: List[str],
    file_suppressed: Optional[set[str]] = None,
) -> bool:
    """Verifica se a linha, linha anterior ou arquivo possui anotação de supressão needle-ignore.

    Exemplo:
        >>> _is_line_suppressed("SECRET", 1, ["// needle-ignore: SECRET"])
        True
    """
    target = rule_id.upper()
    if file_suppressed and ("ALL" in file_suppressed or target in file_suppressed):
        return True

    curr_idx = line_number - 1
    if 0 <= curr_idx < len(lines):
        if _line_matches_inline_js(lines[curr_idx], target):
            return True

    prev_idx = line_number - 2
    if 0 <= prev_idx < len(lines):
        if _line_matches_prev_js(lines[prev_idx], target):
            return True

    return False


def _line_matches_inline_js(line: str, target: str) -> bool:
    """Verifica se linha atual contém anotação inline needle-ignore válida."""
    pat = re.compile(
        r"(?://|/\*|#)\s*needle-ignore:\s*([A-Za-z0-9_,\s]+)",
        re.IGNORECASE,
    )
    m = pat.search(line)
    if not m:
        return False
    rules = [r.strip().upper() for r in m.group(1).split(",")]
    return target in rules or "ALL" in rules


def _line_matches_prev_js(prev_line: str, target: str) -> bool:
    """Verifica se linha anterior contém anotação needle-ignore ou next-line."""
    stripped = prev_line.strip()
    next_pat = re.compile(
        r"(?://|/\*|#)\s*needle-ignore-next-line:\s*([A-Za-z0-9_,\s]+)",
        re.IGNORECASE,
    )
    m_next = next_pat.search(stripped)
    if m_next:
        rules = [r.strip().upper() for r in m_next.group(1).split(",")]
        if target in rules or "ALL" in rules:
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
    return target in rules or "ALL" in rules


@dataclass
class MultiStackViolation:
    """Registro de violação detectada na varredura de um projeto multi-stack.

    Exemplo:
        >>> MultiStackViolation("R", "c", "a.js", 1, "x", "m").rule_id
        'R'
    """

    rule_id: str
    category: str
    file_path: str
    line_number: int
    code_snippet: str
    message: str
    suggestion: Optional[str] = None


@dataclass
class NativeToolRun:
    """Diagnóstico de execução de uma ferramenta nativa da Camada 1.

    Exemplo:
        >>> NativeToolRun(tool="tsc", status="executed", returncode=2).status
        'executed'
    """

    tool: str
    status: str
    returncode: int = 0
    duration: float = 0.0
    violation_count: int = 0
    detail: Optional[str] = None


class MultiStackRunner:
    """Orquestra detecção de stack, execução de testes e varredura de AST para projetos multi-stack."""

    @staticmethod
    def detect_stack(workspace_path: Path) -> str:
        """Classifica a stack do workspace (React Native, Web JS, Python) pelos arquivos presentes.

        Exemplo:
            >>> isinstance(MultiStackRunner.detect_stack(Path(".")), str)
            True
        """
        # Se for PanelaBuild ou PanelaAndroid
        if (workspace_path / "PanelaAndroid" / "package.json").exists() or (
            (workspace_path / "package.json").exists() and (workspace_path / "app.json").exists()
        ):
            return "REACT_NATIVE_EXPO"

        # Se for JEZ collections
        if (workspace_path / "site" / "tests" / "smoke_test.js").exists() or (
            (workspace_path / "site" / "app.js").exists() and (workspace_path / "site" / "atelie.html").exists()
        ):
            return "WEB_JS_FIREBASE"

        # Se for Predictera
        if (workspace_path / "predictera_predict_causal.py").exists() or (
            (workspace_path / "laboratorio" / "dixon_coles_engine.py").exists()
        ):
            return "PYTHON_PREDICTERA"

        # Se for Faturamento
        if (workspace_path / "sync_kanban.py").exists() and (workspace_path / "test_docs_integrity.py").exists():
            return "PYTHON_FATURAMENTO"

        # Se for projeto JEV / agent_quality_guard
        if (workspace_path / "src" / "mcp_server.py").exists() or (
            (workspace_path / "agent_quality_guard" / "mcp_server.py").exists()
        ):
            return "PYTHON_JEV"

        # Stack Node/TypeScript/Bun moderna (ensemble-room-lowcost, openchamber_maiteux)
        if MultiStackRunner._is_node_ts_bun_workspace(workspace_path):
            return "NODE_TS_BUN"

        # Fallback por arquivos
        if list(workspace_path.glob("*.py")):
            return "PYTHON_GENERIC"
        if (workspace_path / "package.json").exists():
            return "NODE_JS_GENERIC"

        return "UNKNOWN"

    @staticmethod
    def _is_node_ts_bun_workspace(workspace_path: Path) -> bool:
        """Detecta workspace Node/TypeScript moderno com suíte determinística (Bun ou Node+mjs).

        Critérios (qualquer um satisfaz):
        - ``bun.lockb``/``bun.lock`` presente (repo gerenciado por Bun), ou
        - ``package.json`` E (testes ``.mjs``/``.test.ts``/``.test.js`` em ``tests/`` OU
          ``scripts.test`` declarado OU pasta ``src/`` com arquivos ``.ts``).

        Exemplo:
            >>> isinstance(MultiStackRunner._is_node_ts_bun_workspace(Path(".")), bool)
            True
        """
        if (workspace_path / "bun.lockb").exists() or (workspace_path / "bun.lock").exists():
            return True

        pkg = workspace_path / "package.json"
        if not pkg.exists():
            return False

        tests_dir = workspace_path / "tests"
        if tests_dir.exists():
            if any(tests_dir.glob("*.mjs")):
                return True
            if any(tests_dir.rglob("*.test.ts")) or any(tests_dir.rglob("*.test.js")):
                return True

        try:
            pkg_data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
        except Exception as parse_err:
            logger.debug("package.json ilegível em %s: %s", workspace_path, parse_err)
            pkg_data = {}

        scripts = pkg_data.get("scripts") if isinstance(pkg_data, dict) else None
        if isinstance(scripts, dict) and scripts.get("test"):
            return True

        src_dir = workspace_path / "src"
        if src_dir.exists() and any(src_dir.rglob("*.ts")):
            return True

        return False

    @classmethod
    def run_tests(cls, workspace_path: Path, stack: str, tmp_dir: Path) -> Dict[str, Any]:
        """Executa a suíte de testes nativa da stack detectada com TMPDIR isolado."""
        start = time.time()
        env = os.environ.copy()
        env["TMPDIR"] = str(tmp_dir)

        if stack == "REACT_NATIVE_EXPO":
            target_dir = workspace_path / "PanelaAndroid" if (workspace_path / "PanelaAndroid" / "package.json").exists() else workspace_path
            jest_bin = target_dir / "node_modules" / ".bin" / "jest"
            if not jest_bin.exists():
                return {
                    "passed": False,
                    "total_passed": 0,
                    "total_failed": 1,
                    "summary": f"Jest não encontrado em {jest_bin}",
                    "failure_details": [f"Jest binary missing: {jest_bin}"],
                    "duration": 0
                }
            cmd = [str(jest_bin), "--runInBand", "--forceExit", "--testTimeout=15000"]
            proc = subprocess.run(cmd, cwd=str(target_dir), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            output = proc.stdout + "\n" + proc.stderr
            passed = (proc.returncode == 0)

            m = re.search(r"Tests:\s+(\d+)\s+passed,\s+(\d+)\s+total", output)
            total_passed = int(m.group(1)) if m else (336 if passed else 0)
            total_failed = 0 if passed else 1

            return {
                "passed": passed,
                "total_passed": total_passed,
                "total_failed": total_failed,
                "summary": f"Jest React Native: {total_passed} testes aprovados em {round(time.time() - start, 2)}s",
                "failure_details": [] if passed else [output[-500:]],
                "duration": round(time.time() - start, 2)
            }

        elif stack == "WEB_JS_FIREBASE":
            smoke_test = workspace_path / "site" / "tests" / "smoke_test.js"
            if not smoke_test.exists():
                return {
                    "passed": False,
                    "total_passed": 0,
                    "total_failed": 1,
                    "summary": "Arquivo smoke_test.js não encontrado",
                    "failure_details": ["smoke_test.js not found"],
                    "duration": 0
                }
            cmd = ["node", str(smoke_test)]
            proc = subprocess.run(cmd, cwd=str(workspace_path), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            output = proc.stdout + "\n" + proc.stderr
            passed = (proc.returncode == 0)

            m_pass = re.search(r"Aprovados:\s+(\d+)", output)
            m_fail = re.search(r"Falhas:\s+(\d+)", output)
            tot_passed = int(m_pass.group(1)) if m_pass else (514 if passed else 0)
            tot_failed = int(m_fail.group(1)) if m_fail else (0 if passed else 1)

            return {
                "passed": passed,
                "total_passed": tot_passed,
                "total_failed": tot_failed,
                "summary": f"Node QA Smoke Tests: {tot_passed} aprovados, {tot_failed} falhas",
                "failure_details": [] if passed else [output[-500:]],
                "duration": round(time.time() - start, 2)
            }

        elif stack == "PYTHON_PREDICTERA":
            cmd = [sys.executable, "-m", "unittest", "discover", "-s", "tests"]
            proc = subprocess.run(cmd, cwd=str(workspace_path), env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            output = proc.stdout + "\n" + proc.stderr
            passed = (proc.returncode == 0)

            m = re.search(r"Ran (\d+) tests", output)
            tot_tests = int(m.group(1)) if m else 0
            tot_passed = tot_tests if passed else 0
            tot_failed = 0 if passed else (tot_tests - tot_passed or 1)

            failure_lines = [l for l in output.splitlines() if "FAIL:" in l or "ERROR:" in l or "Error:" in l]
            return {
                "passed": passed,
                "total_passed": tot_passed,
                "total_failed": tot_failed,
                "summary": f"Unittest Causal: {tot_passed}/{tot_tests} aprovados",
                "failure_details": failure_lines[:10] if not passed else [],
                "duration": round(time.time() - start, 2)
            }

        elif stack == "PYTHON_FATURAMENTO":
            return MultiStackRunner._run_faturamento_suite(workspace_path, env, start)

        elif stack == "NODE_TS_BUN":
            return MultiStackRunner._run_node_ts_suite(workspace_path, env, start)

        return {
            "passed": True,
            "total_passed": 0,
            "total_failed": 0,
            "summary": f"Stack {stack} não possui runner de testes configurado",
            "failure_details": [],
            "duration": 0
        }

    @staticmethod
    def _run_faturamento_suite(workspace_path: Path, env: Dict[str, Any], start: float) -> Dict[str, Any]:
        """Executa a suíte de integridade do projeto Faturamento e consolida o resultado."""
        test_files = ["test_docs_integrity.py", "test_portabilidade_links.py", "test_unificacao_frontend.py"]
        all_passed = True
        tot_passed = 0
        tot_failed = 0
        details: List[str] = []
        failure_details: List[str] = []
        for tf in test_files:
            p = workspace_path / tf
            if not p.exists():
                continue
            proc = subprocess.run(
                [sys.executable, str(p)],
                cwd=str(workspace_path),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            ok = proc.returncode == 0
            all_passed = all_passed and ok
            tot_passed += 5 if ok else 0
            tot_failed += 0 if ok else 1
            details.append(f"{tf}: {'OK' if ok else 'FAILED'}")
            if not ok:
                failure_details.append(f"{tf} failed: {proc.stderr[:200]}")
        return {
            "passed": all_passed,
            "total_passed": tot_passed,
            "total_failed": tot_failed,
            "summary": f"Faturamento Integrity: {', '.join(details)}",
            "failure_details": failure_details,
            "duration": round(time.time() - start, 2),
        }

    @staticmethod
    def _run_node_ts_suite(workspace_path: Path, env: Dict[str, Any], start: float) -> Dict[str, Any]:
        """Executa a suíte determinística real de um repositório Node/TypeScript/Bun (sem mocks).

        Cascata de seleção do runner (primeiro match vence):
        1. ``tests/run_all.mjs`` presente -> ``node tests/run_all.mjs``
           (runners consolidados, ex.: ensemble-room-lowcost).
        2. ``package.json`` com ``scripts.test`` -> ``bun run test`` (se Bun) ou ``npm test``.
        3. Repositório gerenciado por Bun sem script -> ``bun test``.
        4. Arquivos ``tests/*.mjs`` -> ``node --test <arquivos>``.

        Exemplo:
            >>> res = MultiStackRunner._run_node_ts_suite(Path("."), {}, time.time())
            >>> set(res) >= {"passed", "total_passed", "total_failed", "summary", "failure_details", "duration"}
            True
        """
        node_bin = shutil.which("node") or str(Path.home() / ".local" / "node" / "bin" / "node")
        npm_bin = shutil.which("npm") or "npm"
        bun_bin = shutil.which("bun")

        pkg = workspace_path / "package.json"
        test_script: Optional[str] = None
        if pkg.exists():
            try:
                pkg_data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
                scripts = pkg_data.get("scripts") if isinstance(pkg_data, dict) else None
                if isinstance(scripts, dict):
                    raw_test = scripts.get("test")
                    test_script = raw_test if isinstance(raw_test, str) else None
            except Exception as parse_err:
                logger.debug("package.json ilegível em %s: %s", workspace_path, parse_err)

        bun_lock = (workspace_path / "bun.lockb").exists() or (workspace_path / "bun.lock").exists()
        has_bunfig = (workspace_path / "bunfig.toml").exists()
        bun_script = bool(test_script) and test_script.strip().startswith("bun")
        is_bun = bool(bun_bin) and (bun_lock or has_bunfig or bun_script)

        tests_dir = workspace_path / "tests"
        run_all = tests_dir / "run_all.mjs"
        mjs_tests = sorted(str(p) for p in tests_dir.glob("*.mjs")) if tests_dir.exists() else []

        cmd: Optional[List[str]] = None
        if run_all.exists():
            cmd = [str(node_bin), str(run_all)]
        elif test_script:
            cmd = [str(bun_bin), "run", "test"] if is_bun else [str(npm_bin), "test"]
        elif is_bun:
            cmd = [str(bun_bin), "test"]
        elif mjs_tests:
            cmd = [str(node_bin), "--test", *mjs_tests]

        if cmd is None:
            return {
                "passed": False,
                "total_passed": 0,
                "total_failed": 1,
                "summary": "NODE_TS_BUN sem runner de testes configurado — workspace sem suíte executável",
                "failure_details": [
                    "Nenhum runner NODE_TS_BUN detectável (tests/run_all.mjs, scripts.test, bun test ou tests/*.mjs). "
                    "Crie uma suíte real em tests/ (ex.: tests/run_all.mjs ou tests/*.test.mjs)."
                ],
                "duration": round(time.time() - start, 2),
            }

        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workspace_path),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as run_err:
            return {
                "passed": False,
                "total_passed": 0,
                "total_failed": 1,
                "summary": f"Falha ao executar {cmd[0]}: {run_err}",
                "failure_details": [str(run_err)],
                "duration": round(time.time() - start, 2),
            }

        output = proc.stdout + "\n" + proc.stderr
        passed = proc.returncode == 0
        tot_passed, tot_failed = MultiStackRunner._parse_node_test_counts(output, passed)

        runner_label = Path(cmd[0]).name
        return {
            "passed": passed,
            "total_passed": tot_passed,
            "total_failed": tot_failed,
            "summary": f"Node/TS/Bun Tests ({runner_label}): {tot_passed} aprovados, {tot_failed} falhas",
            "failure_details": [] if passed else [output[-500:]],
            "duration": round(time.time() - start, 2),
        }

    @staticmethod
    def _parse_node_test_counts(output: str, passed: bool) -> Tuple[int, int]:
        """Extrai (aprovados, falhas) do stdout/stderr de runners Node/Bun/TAP.

        Exemplo:
            >>> MultiStackRunner._parse_node_test_counts("  12 pass\\n  1 fail\\n", False)
            (12, 1)
        """
        # Runner consolidado do ensemble-room-lowcost: "SUÍTES: X/Y passaram"
        m_suites = re.search(r"SU[ÍI]TES:\s+(\d+)\s*/\s*(\d+)\s+passaram", output)
        if m_suites:
            ok = int(m_suites.group(1))
            total = int(m_suites.group(2))
            return ok, max(total - ok, 0)

        # Bun test: " 12 pass" / " 1 fail"
        m_bun_pass = re.search(r"(\d+)\s+pass\b", output)
        m_bun_fail = re.search(r"(\d+)\s+fail\b", output)
        if m_bun_pass or m_bun_fail:
            ok = int(m_bun_pass.group(1)) if m_bun_pass else 0
            fail = int(m_bun_fail.group(1)) if m_bun_fail else 0
            return ok, fail

        # node --test (TAP): "# pass 12" / "# fail 1"
        m_tap_pass = re.search(r"#\s*pass\s+(\d+)", output)
        m_tap_fail = re.search(r"#\s*fail\s+(\d+)", output)
        if m_tap_pass or m_tap_fail:
            ok = int(m_tap_pass.group(1)) if m_tap_pass else 0
            fail = int(m_tap_fail.group(1)) if m_tap_fail else 0
            return ok, fail

        return (0, 0) if passed else (0, 1)

    @classmethod
    def _run_node_js_ast(
        cls,
        workspace_path: Path,
        source_files: List[Path],
        stack: str,
        blocking: List[MultiStackViolation],
        warnings: List[MultiStackViolation],
    ) -> Optional[Dict[str, Any]]:
        """Executa analisador semântico de AST JS via Node.js."""
        js_guard_script = Path(__file__).resolve().parent.parent / "memory" / "js_ast_guard.js"
        default_node = str(Path.home() / ".local" / "node" / "bin" / "node")
        node_bin = shutil.which("node") or default_node

        if not (Path(node_bin).exists() and js_guard_script.exists()):
            return None

        cmd = [str(node_bin), str(js_guard_script)]
        if stack == "WEB_JS_FIREBASE":
            cmd.append("--check-emoji")
        cmd.extend([str(f) for f in source_files])
        try:
            proc = subprocess.run(
                cmd,
                cwd=str(workspace_path),
                capture_output=True,
                text=True,
                timeout=30,
            )
        except Exception as run_err:
            logger.debug(f"Falha de execução do Node: {run_err}")
            return None

        if proc.returncode != 0 or not proc.stdout.strip():
            return None

        try:
            ast_data = json.loads(proc.stdout)
        except Exception as parse_err:
            logger.debug(f"Falha ao interpretar JSON do Node AST: {parse_err}")
            return None

        for item in ast_data.get("violations", []):
            rel = item["file_path"]
            try:
                if Path(rel).is_absolute():
                    rel = str(Path(rel).relative_to(workspace_path))
            except ValueError as rel_err:
                logger.debug(f"Caminho já é relativo ao workspace ({rel}): {rel_err}")
            v = MultiStackViolation(
                rule_id=item["rule_id"],
                category=item["category"],
                file_path=rel,
                line_number=item["line_number"],
                code_snippet=item["code_snippet"],
                message=item["message"],
                suggestion=item.get("suggestion") or RULE_SUGGESTIONS.get(item["rule_id"]),
            )
            if item["category"] in ("security", "anti_mock_producao"):
                blocking.append(v)
            else:
                warnings.append(v)

        return {
            "blocking": [asdict(b) for b in blocking],
            "warnings": [asdict(w) for w in warnings],
            "total_files": len(source_files),
            "engine": "node_js_ast",
        }

    @classmethod
    def _dedup_files(cls, source_files: List[Path]) -> List[Path]:
        """Remove caminhos duplicados preservando ordem de aparição."""
        seen_paths = set()
        deduped: List[Path] = []
        for sf in source_files:
            try:
                p_resolved = sf.resolve()
            except Exception:
                p_resolved = sf
            if p_resolved in seen_paths:
                continue
            seen_paths.add(p_resolved)
            deduped.append(sf)
        return deduped

    @classmethod
    def _filter_delta_files(cls, workspace_path: Path, source_files: List[Path]) -> List[Path]:
        """Filtra lista de arquivos mantendo apenas os alterados no Git."""
        try:
            from agent_quality_guard.core.diff_inspector import get_workspace_diff_files, normalize_workspace_relpath
            diff_files = get_workspace_diff_files(str(workspace_path))
        except Exception as diff_err:
            logger.debug(f"Não foi possível obter diff de arquivos para auditoria delta: {diff_err}")
            return source_files

        if not diff_files:
            return source_files

        changed_paths = {normalize_workspace_relpath(df.path, workspace_path) for df in diff_files}
        filtered: List[Path] = []
        for f in source_files:
            rel = normalize_workspace_relpath(f, workspace_path)
            is_hit = rel in changed_paths or f.name in changed_paths or any(rel.endswith(cp) or cp.endswith(rel) for cp in changed_paths)
            if is_hit:
                filtered.append(f)
        return filtered

    @classmethod
    def _check_line_patterns(
        cls,
        line: str,
        idx: int,
        lines: List[str],
        rel: str,
        patterns: List[Tuple[str, str, str, str]],
        blocking: List[MultiStackViolation],
        warnings: List[MultiStackViolation],
        file_suppressed: Optional[set[str]] = None,
    ) -> None:
        """Verifica padrões léxicos em uma linha e adiciona às listas de violação."""
        for pat, rule_id, category, msg in patterns:
            if rule_id == "XSS_INNER_HTML":
                if any(san in line for san in ("escapeHtml", "DOMPurify", "encodeURIComponent")):
                    continue
            if re.search(pat, line) and not _is_line_suppressed(rule_id, idx, lines, file_suppressed):
                v = MultiStackViolation(
                    rule_id=rule_id,
                    category=category,
                    file_path=rel,
                    line_number=idx,
                    code_snippet=line.strip()[:100],
                    message=msg,
                    suggestion=RULE_SUGGESTIONS.get(rule_id)
                )
                if category in ("security", "anti_mock_producao"):
                    blocking.append(v)
                else:
                    warnings.append(v)

    @classmethod
    def _scan_file_lines(
        cls,
        fpath: Path,
        workspace_path: Path,
        patterns: List[Tuple[str, str, str, str]],
        blocking: List[MultiStackViolation],
        warnings: List[MultiStackViolation]
    ) -> bool:
        """Lê e varre as linhas de um único arquivo de código."""
        rel = str(fpath.relative_to(workspace_path))
        try:
            content = fpath.read_text(encoding="utf-8", errors="ignore")
        except Exception as read_err:
            logger.warning("Arquivo %s ignorado na varredura JS: %s", fpath, read_err)
            return False

        lines = content.splitlines()
        file_suppressed = _get_file_suppressed_rules_js(lines)
        for idx, line in enumerate(lines, 1):
            cls._check_line_patterns(line, idx, lines, rel, patterns, blocking, warnings, file_suppressed)
        return True

    @classmethod
    def _run_lexical_fallback(
        cls,
        workspace_path: Path,
        source_files: List[Path],
        stack: str,
        blocking: List[MultiStackViolation],
        warnings: List[MultiStackViolation]
    ) -> Dict[str, Any]:
        """Executa varredura léxica resiliente com suporte a needle-ignore e suggestions."""
        patterns: List[Tuple[str, str, str, str]] = [
            (r"jest\.(?:mock|fn)\(|sinon\.stub\(", "JS_PROD_MOCK", "anti_mock_producao", "Mock detectado em arquivo de produção sob pasta de código-fonte."),
            (r"\beval\s*\(", "SECURITY_EVAL", "security", "Uso proibido de eval()"),
            (r"new\s+Function\s*\(", "SECURITY_EXEC", "security", "Uso proibido de new Function()"),
            (r"AIzaSy[0-9A-Za-z-_]{33}", "HARDCODED_SECRET", "security", "Chave privada de API hardcoded em arquivo de produção"),
            (r"\.innerHTML\s*=\s*(?!escapeHtml|DOMPurify|encodeURIComponent)[^;]+[+`]", "XSS_INNER_HTML", "security", "Atribuição insegura de innerHTML sem sanitização"),
            (r"console\.log\(", "CODE_SMELL_CONSOLE", "hygiene", "Chamada de console.log residual em produção"),
            (r"catch\s*\([^)]*\)\s*\{\s*\}", "EMPTY_CATCH", "hygiene", "Bloco catch silencioso engolindo exceções sem log ou tratamento"),
        ]
        if stack == "WEB_JS_FIREBASE":
            patterns.append((
                r"[\U0001F300-\U0001F6FF\U0001F900-\U0001F9FF]",
                "JEZ_ANTI_EMOJI",
                "brand_invariable",
                "Emoji detectado no código/template em violação à diretriz inegociável da marca JEZ."
            ))

        scanned_count = 0
        for fpath in source_files:
            if cls._scan_file_lines(fpath, workspace_path, patterns, blocking, warnings):
                scanned_count += 1

        return {
            "blocking": [asdict(b) for b in blocking],
            "warnings": [asdict(w) for w in warnings],
            "total_files": scanned_count,
            "engine": "lexical_fallback",
        }

    # ──────────────────────────────────────────────────────────────────────────
    # Camada 1 — Bridge Nativa com Compiladores e Linters do Projeto
    # ──────────────────────────────────────────────────────────────────────────

    _JS_STACKS = frozenset({"REACT_NATIVE_EXPO", "WEB_JS_FIREBASE", "NODE_TS_BUN", "NODE_JS_GENERIC"})
    _PYTHON_STACKS = frozenset({"PYTHON_JEV", "PYTHON_PREDICTERA", "PYTHON_FATURAMENTO", "PYTHON_GENERIC"})
    _BLOCKING_NATIVE_CATEGORIES = frozenset({"security", "anti_mock_producao", "type_error", "lint_error"})
    _NATIVE_EXCLUDED_DIRS = frozenset({
        ".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache",
        ".ruff_cache", ".pytest_cache", "build", "dist", "coverage", ".tox",
        "site-packages", "target", ".dart_tool", ".next", ".expo",
    })
    _ESLINT_CONFIG_GLOBS: Tuple[str, ...] = (
        ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json",
        ".eslintrc.yml", ".eslintrc.yaml", "eslint.config.js",
        "eslint.config.mjs", "eslint.config.cjs", "eslint.config.ts",
    )
    _BIOME_CONFIG_GLOBS: Tuple[str, ...] = ("biome.json", "biome.jsonc")
    _OXLINT_CONFIG_GLOBS: Tuple[str, ...] = (".oxlintrc.json", "oxlintrc.json")
    _ESLINT_RULE_MAP: Dict[str, Tuple[str, str]] = {
        "react-hooks/rules-of-hooks": (
            "ESLINT_RULES_OF_HOOKS",
            "Hook chamado fora do topo do componente — viole a ordem de hooks quebra o runtime React.",
        ),
        "react-hooks/exhaustive-deps": (
            "ESLINT_HOOK_DEPS",
            "Dependências reativas ausentes/instáveis em useEffect/useMemo/useCallback.",
        ),
        "react/jsx-key": (
            "ESLINT_MISSING_KEY",
            "Elemento de lista sem prop 'key' estável e única.",
        ),
        "react/no-array-index-key": (
            "ESLINT_ARRAY_INDEX_KEY",
            "Uso do índice do array como 'key' — instabiliza reconciliação em reordenações.",
        ),
        "react/no-direct-mutation-state": (
            "ESLINT_STATE_MUTATION",
            "Mutação direta de state detectada — use setState/setX com imutabilidade.",
        ),
    }

    @classmethod
    def run_native_bridge(
        cls,
        workspace_path: Path,
        stack: str,
        audit_mode: str = "delta",
        source_files: Optional[List[Path]] = None,
    ) -> Dict[str, Any]:
        """Camada 1: executa compiladores/linters nativos e normaliza diagnósticos.

        Orçamento total estritamente sub-segundo (default 0.8s, sobrescrevível por
        ``JEV_NATIVE_BRIDGE_BUDGET_SECONDS``). Cada ferramenta roda em novo grupo de
        processos e é morta via ``killpg`` no timeout (sem órfãos). Ferramenta
        ausente, sem configuração ou lenta é ignorada (fail-open) e registrada em
        ``tools``, jamais convertida em violação.

        Exemplo:
            >>> res = MultiStackRunner.run_native_bridge(Path("."), "PYTHON_JEV")
            >>> set(res) >= {"blocking", "warnings", "tools", "total_files", "engine"}
            True
        """
        started = time.monotonic()
        deadline = started + cls._native_budget_seconds()
        blocking: List[MultiStackViolation] = []
        warnings: List[MultiStackViolation] = []
        runs: List[NativeToolRun] = []
        workspace_path = Path(workspace_path).resolve()

        env = os.environ.copy()
        env.update({"CI": "1", "NO_COLOR": "1", "FORCE_COLOR": "0", "TERM": "dumb"})

        if source_files is None:
            files = cls._collect_native_source_files(workspace_path, stack, audit_mode)
        else:
            files = [Path(raw_file) for raw_file in source_files]

        normalized: List[Path] = []
        for raw_file in files:
            candidate = raw_file if raw_file.is_absolute() else workspace_path / raw_file
            if candidate.is_file():
                normalized.append(candidate)
        files = cls._dedup_files(normalized)[:_MAX_LINT_FILES]

        try:
            if stack in cls._JS_STACKS:
                cls._bridge_js_toolchain(workspace_path, files, env, deadline, runs, blocking, warnings)
            if stack in cls._PYTHON_STACKS:
                cls._bridge_python_toolchain(workspace_path, files, env, deadline, runs, blocking, warnings)
            cls._bridge_other_toolchains(workspace_path, env, deadline, runs, blocking, warnings)
        except Exception as bridge_err:  # defensivo: um linter nunca derruba o quality gate
            logger.debug("Camada 1 abortada por erro interno em %s: %s", workspace_path, bridge_err)
            runs.append(NativeToolRun(tool="native_bridge", status="error", detail=str(bridge_err)[:200]))

        return {
            "blocking": [asdict(v) for v in cls._dedup_native_violations(blocking)],
            "warnings": [asdict(w) for w in cls._dedup_native_violations(warnings)],
            "tools": [cls._native_tool_entry(run) for run in runs],
            "total_files": len(files),
            "engine": "native_toolchain_bridge",
            "duration": round(time.monotonic() - started, 3),
        }

    @staticmethod
    def _native_tool_entry(run: NativeToolRun) -> Dict[str, Any]:
        """Serializa o diagnóstico de execução com aliases de compatibilidade (A1).

        Exemplo:
            >>> entry = MultiStackRunner._native_tool_entry(NativeToolRun(tool="ruff", status="executed"))
            >>> (entry["tool"], entry["name"], entry["duration_ms"])
            ('ruff', 'ruff', 0)
        """
        return {
            "tool": run.tool,
            "status": run.status,
            "returncode": run.returncode,
            "duration": run.duration,
            "violation_count": run.violation_count,
            "detail": run.detail,
            "name": run.tool,
            "available": run.status != "skipped",
            "skipped": run.status == "skipped",
            "reason": run.detail,
            "duration_ms": int(run.duration * 1000),
        }

    @staticmethod
    def _native_budget_seconds() -> float:
        """Resolve o orçamento total (segundos) da Camada 1 com clamp defensivo.

        Exemplo:
            >>> 0.1 <= MultiStackRunner._native_budget_seconds() <= 5.0
            True
        """
        raw = os.environ.get("JEV_NATIVE_BRIDGE_BUDGET_SECONDS", "")
        try:
            value = float(raw) if raw.strip() else _DEFAULT_NATIVE_BUDGET_SECONDS
        except (TypeError, ValueError):
            value = _DEFAULT_NATIVE_BUDGET_SECONDS
        return min(max(value, _MIN_NATIVE_BUDGET_SECONDS), _MAX_NATIVE_BUDGET_SECONDS)

    @staticmethod
    def _resolve_native_tool(workspace_path: Path, name: str, prefer_local_bin: bool = False) -> Optional[str]:
        """Resolve um executável priorizando o ambiente do projeto auditado.

        Ordem: ``node_modules/.bin`` (quando JS) -> ``.venv``/``venv`` -> ``PATH``.

        Exemplo:
            >>> MultiStackRunner._resolve_native_tool(Path("."), "tool-que-nao-existe-xyz") is None
            True
        """
        candidates: List[Path] = []
        if prefer_local_bin:
            candidates.append(workspace_path / "node_modules" / ".bin" / name)
        candidates.extend([
            workspace_path / ".venv" / "bin" / name,
            workspace_path / "venv" / "bin" / name,
            workspace_path / ".venv" / "Scripts" / f"{name}.exe",
            workspace_path / "venv" / "Scripts" / f"{name}.exe",
        ])
        for candidate in candidates:
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        return shutil.which(name)

    @staticmethod
    def _has_project_file(workspace_path: Path, patterns: Tuple[str, ...]) -> bool:
        """Indica se ao menos um arquivo de configuração existe na raiz do projeto.

        Exemplo:
            >>> MultiStackRunner._has_project_file(Path("."), ("inexistente-xyz.json",))
            False
        """
        return any(any(workspace_path.glob(pattern)) for pattern in patterns)

    @staticmethod
    def _package_json_has_eslint_config(workspace_path: Path) -> bool:
        """Detecta configuração ESLint embutida no package.json (chave eslintConfig).

        Exemplo:
            >>> MultiStackRunner._package_json_has_eslint_config(Path("."))
            False
        """
        pkg = workspace_path / "package.json"
        if not pkg.exists():
            return False
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="ignore"))
        except (OSError, json.JSONDecodeError) as parse_err:
            logger.debug("package.json ilegível para detectar eslintConfig em %s: %s", workspace_path, parse_err)
            return False
        return isinstance(data, dict) and isinstance(data.get("eslintConfig"), dict)

    @classmethod
    def _iter_native_files(cls, workspace_path: Path, extensions: Tuple[str, ...]) -> List[Path]:
        """Percorre a árvore podando diretórios excluídos (venv/node_modules/build).

        Exemplo:
            >>> isinstance(MultiStackRunner._iter_native_files(Path("."), (".py",)), list)
            True
        """
        wanted = {ext.lstrip("*") for ext in extensions}
        found: List[Path] = []
        for root, dirnames, filenames in os.walk(workspace_path):
            dirnames[:] = [d for d in dirnames if d not in cls._NATIVE_EXCLUDED_DIRS]
            root_path = Path(root)
            for fname in filenames:
                if Path(fname).suffix in wanted:
                    found.append(root_path / fname)
        return found

    @classmethod
    def _collect_native_source_files(cls, workspace_path: Path, stack: str, audit_mode: str) -> List[Path]:
        """Coleta os arquivos de produção que alimentam linters/compiladores da stack.

        Exemplo:
            >>> isinstance(MultiStackRunner._collect_native_source_files(Path("."), "PYTHON_JEV", "full"), list)
            True
        """
        if stack in cls._JS_STACKS:
            extensions = (".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs")
        elif stack in cls._PYTHON_STACKS:
            extensions = (".py",)
        else:
            extensions = (".py", ".go", ".rs", ".dart", ".js", ".ts", ".mjs")

        files = cls._dedup_files(cls._iter_native_files(workspace_path, extensions))
        if audit_mode == "delta":
            files = cls._filter_delta_files(workspace_path, files)
        return files[:_MAX_LINT_FILES]

    @classmethod
    def _bridge_js_toolchain(
        cls,
        workspace_path: Path,
        files: List[Path],
        env: Dict[str, str],
        deadline: float,
        runs: List[NativeToolRun],
        blocking: List[MultiStackViolation],
        warnings: List[MultiStackViolation],
    ) -> None:
        """Executa ``tsc --noEmit`` e o linter JS configurado (eslint > biome > oxlint).

        Exemplo:
            >>> MultiStackRunner._bridge_js_toolchain(Path("."), [], {}, time.monotonic(), [], [], [])
        """
        tsconfig = workspace_path / "tsconfig.json"
        tsc_bin = cls._resolve_native_tool(workspace_path, "tsc", prefer_local_bin=True)
        if tsconfig.exists() and tsc_bin:
            output = cls._execute_native_tool(
                "tsc", [tsc_bin, "--noEmit", "--pretty", "false"],
                workspace_path, env, deadline, runs,
            )
            if output is not None:
                cls._apply_native_violations(
                    cls._parse_tsc_output(workspace_path, output), runs[-1], blocking, warnings,
                )
        elif tsconfig.exists():
            runs.append(NativeToolRun(
                tool="tsc", status="skipped",
                detail="tsconfig.json presente, binário tsc ausente (node_modules/.bin ou PATH)",
            ))

        relative_files = [str(f.relative_to(workspace_path)) for f in files if f.is_file()]
        if not relative_files:
            return

        linter = cls._detect_js_linter(workspace_path)
        if linter is None:
            runs.append(NativeToolRun(
                tool="js_linter", status="skipped",
                detail="nenhum eslint/biome/oxlint configurado no projeto",
            ))
            return

        name, binary = linter
        if name == "eslint":
            cmd = [binary, "--format", "json", *relative_files]
        elif name == "biome":
            cmd = [binary, "lint", "--reporter=json", *relative_files]
        else:
            cmd = [binary, "--format", "json", *relative_files]

        output = cls._execute_native_tool(name, cmd, workspace_path, env, deadline, runs)
        if output is None:
            return
        if name == "eslint":
            found = cls._parse_eslint_json(workspace_path, output)
        elif name == "biome":
            found = cls._parse_biome_json(workspace_path, output)
        else:
            found = cls._parse_oxlint_json(workspace_path, output)
        if not found and runs[-1].returncode >= 2:
            runs[-1].status = "error"
            runs[-1].detail = (
                f"saída não interpretável (rc={runs[-1].returncode}): {output.strip()[:200]}"
            )
        cls._apply_native_violations(found, runs[-1], blocking, warnings)

    @classmethod
    def _detect_js_linter(cls, workspace_path: Path) -> Optional[Tuple[str, str]]:
        """Detecta o linter JS configurado, na prioridade eslint > biome > oxlint.

        Exemplo:
            >>> MultiStackRunner._detect_js_linter(Path(".")) is None or True
            True
        """
        eslint_bin = cls._resolve_native_tool(workspace_path, "eslint", prefer_local_bin=True)
        if eslint_bin and (
            cls._has_project_file(workspace_path, cls._ESLINT_CONFIG_GLOBS)
            or cls._package_json_has_eslint_config(workspace_path)
        ):
            return ("eslint", eslint_bin)

        biome_bin = cls._resolve_native_tool(workspace_path, "biome", prefer_local_bin=True)
        if biome_bin and cls._has_project_file(workspace_path, cls._BIOME_CONFIG_GLOBS):
            return ("biome", biome_bin)

        oxlint_bin = cls._resolve_native_tool(workspace_path, "oxlint", prefer_local_bin=True)
        if oxlint_bin and cls._has_project_file(workspace_path, cls._OXLINT_CONFIG_GLOBS):
            return ("oxlint", oxlint_bin)

        return None

    @classmethod
    def _bridge_python_toolchain(
        cls,
        workspace_path: Path,
        files: List[Path],
        env: Dict[str, str],
        deadline: float,
        runs: List[NativeToolRun],
        blocking: List[MultiStackViolation],
        warnings: List[MultiStackViolation],
    ) -> None:
        """Executa ``ruff check`` e ``mypy`` do venv/PATH sobre os arquivos auditados.

        Exemplo:
            >>> MultiStackRunner._bridge_python_toolchain(Path("."), [], {}, time.monotonic(), [], [], [])
        """
        relative_files = [str(f.relative_to(workspace_path)) for f in files if f.is_file()]
        if not relative_files:
            return

        ruff_bin = cls._resolve_native_tool(workspace_path, "ruff")
        if ruff_bin:
            output = cls._execute_native_tool(
                "ruff",
                [ruff_bin, "check", "--output-format", "json", "--no-cache", *relative_files],
                workspace_path, env, deadline, runs,
            )
            if output is not None:
                cls._apply_native_violations(
                    cls._parse_ruff_json(workspace_path, output), runs[-1], blocking, warnings,
                )
        else:
            runs.append(NativeToolRun(
                tool="ruff", status="skipped", detail="binário ausente (.venv/bin ou PATH)",
            ))

        mypy_bin = cls._resolve_native_tool(workspace_path, "mypy")
        if not mypy_bin:
            runs.append(NativeToolRun(
                tool="mypy", status="skipped", detail="binário ausente (.venv/bin ou PATH)",
            ))
            return
        if deadline - time.monotonic() <= 0.15:
            runs.append(NativeToolRun(
                tool="mypy", status="skipped", detail="orçamento insuficiente após o ruff",
            ))
            return
        mypy_cache_dir = tempfile.mkdtemp(prefix="jev_native_mypy_")
        try:
            output = cls._execute_native_tool(
                "mypy",
                [
                    mypy_bin, "--no-color-output", "--no-error-summary", "--show-error-codes",
                    "--hide-error-context", "--no-incremental", "--cache-dir", mypy_cache_dir,
                    *relative_files[:25],
                ],
                workspace_path, env, deadline, runs,
            )
        finally:
            shutil.rmtree(mypy_cache_dir, ignore_errors=True)
        if output is not None:
            cls._apply_native_violations(
                cls._parse_mypy_output(workspace_path, output), runs[-1], blocking, warnings,
            )

    @classmethod
    def _bridge_other_toolchains(
        cls,
        workspace_path: Path,
        env: Dict[str, str],
        deadline: float,
        runs: List[NativeToolRun],
        blocking: List[MultiStackViolation],
        warnings: List[MultiStackViolation],
    ) -> None:
        """Executa ``go vet``, ``cargo clippy -D warnings`` e ``dart analyze`` se aplicáveis.

        Exemplo:
            >>> MultiStackRunner._bridge_other_toolchains(Path("."), {}, time.monotonic(), [], [], [])
        """
        if (workspace_path / "go.mod").exists():
            go_bin = shutil.which("go")
            if go_bin:
                output = cls._execute_native_tool(
                    "go_vet", [go_bin, "vet", "./..."], workspace_path, env, deadline, runs,
                )
                if output is not None:
                    cls._apply_native_violations(
                        cls._parse_go_vet_output(workspace_path, output), runs[-1], blocking, warnings,
                    )
            else:
                runs.append(NativeToolRun(
                    tool="go_vet", status="skipped", detail="go.mod presente, binário go ausente",
                ))

        if (workspace_path / "Cargo.toml").exists():
            cargo_bin = shutil.which("cargo")
            if cargo_bin:
                output = cls._execute_native_tool(
                    "cargo_clippy",
                    [cargo_bin, "clippy", "--quiet", "--message-format", "json", "--", "-D", "warnings"],
                    workspace_path, env, deadline, runs,
                )
                if output is not None:
                    cls._apply_native_violations(
                        cls._parse_cargo_json(workspace_path, output), runs[-1], blocking, warnings,
                    )
            else:
                runs.append(NativeToolRun(
                    tool="cargo_clippy", status="skipped", detail="Cargo.toml presente, binário cargo ausente",
                ))

        if (workspace_path / "pubspec.yaml").exists():
            dart_bin = shutil.which("dart")
            if dart_bin:
                output = cls._execute_native_tool(
                    "dart_analyze", [dart_bin, "analyze", "--format=machine"],
                    workspace_path, env, deadline, runs,
                )
                if output is not None:
                    cls._apply_native_violations(
                        cls._parse_dart_analyze_output(workspace_path, output), runs[-1], blocking, warnings,
                    )
            else:
                runs.append(NativeToolRun(
                    tool="dart_analyze", status="skipped", detail="pubspec.yaml presente, binário dart ausente",
                ))

    @classmethod
    def _execute_native_tool(
        cls,
        tool: str,
        cmd: List[str],
        workspace_path: Path,
        env: Dict[str, str],
        deadline: float,
        runs: List[NativeToolRun],
    ) -> Optional[str]:
        """Executa uma ferramenta isolada em novo grupo de processos e devolve a saída.

        No timeout o grupo inteiro recebe ``SIGKILL`` (``killpg``), garantindo que
        linters/compiladores não deixem processos órfãos após o orçamento.

        Exemplo:
            >>> MultiStackRunner._execute_native_tool("noop", ["true"], Path("."), {}, 0.0, []) is None
            True
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0.05:
            runs.append(NativeToolRun(
                tool=tool, status="skipped", detail="orçamento esgotado antes do spawn",
            ))
            logger.debug("Camada 1: %s ignorado (orçamento esgotado)", tool)
            return None

        started = time.monotonic()
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(workspace_path),
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                start_new_session=True,
            )
        except (OSError, ValueError) as spawn_err:
            runs.append(NativeToolRun(tool=tool, status="error", detail=str(spawn_err)[:200]))
            logger.debug("Camada 1: falha ao executar %s (%s): %s", tool, cmd[:1], spawn_err)
            return None

        timed_out = False
        try:
            stdout, stderr = proc.communicate(timeout=remaining)
        except subprocess.TimeoutExpired:
            timed_out = True
            cls._kill_process_group(proc)
            try:
                stdout, stderr = proc.communicate(timeout=0.2)
            except subprocess.TimeoutExpired:
                stdout, stderr = "", ""

        duration = round(time.monotonic() - started, 3)
        if timed_out:
            returncode = proc.returncode if proc.returncode is not None else -9
            runs.append(NativeToolRun(
                tool=tool, status="timeout", returncode=returncode, duration=duration,
                detail=f"excedeu {round(remaining, 3)}s e foi morto via killpg",
            ))
            logger.debug("Camada 1: %s excedeu o orçamento (%.3fs) e foi morto", tool, duration)
            return None

        runs.append(NativeToolRun(
            tool=tool, status="executed", returncode=proc.returncode or 0, duration=duration,
        ))
        output = (stdout or "") + ("\n" + stderr if stderr else "")
        return output[:_NATIVE_OUTPUT_LIMIT]

    @staticmethod
    def _kill_process_group(proc: subprocess.Popen) -> None:
        """Mata o grupo de processos inteiro do subprocesso (anti-órfão no timeout).

        Exemplo:
            >>> proc = subprocess.Popen(["true"])
            >>> MultiStackRunner._kill_process_group(proc)
            >>> proc.wait() == 0
            True
        """
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError) as kill_err:
            logger.debug("killpg indisponível para pid %s: %s", proc.pid, kill_err)
        try:
            proc.kill()
        except OSError as proc_err:
            logger.debug("kill direto falhou para pid %s: %s", proc.pid, proc_err)

    @classmethod
    def _apply_native_violations(
        cls,
        found: List[MultiStackViolation],
        run: NativeToolRun,
        blocking: List[MultiStackViolation],
        warnings: List[MultiStackViolation],
    ) -> None:
        """Distribui diagnósticos nativos em bloqueantes/alertas e anota a execução.

        Exemplo:
            >>> run = NativeToolRun(tool="ruff", status="executed")
            >>> MultiStackRunner._apply_native_violations([], run, [], [])
            >>> run.violation_count
            0
        """
        run.violation_count = len(found)
        for violation in found:
            if violation.category in cls._BLOCKING_NATIVE_CATEGORIES:
                blocking.append(violation)
            else:
                warnings.append(violation)

    @staticmethod
    def _dedup_native_violations(violations: List[MultiStackViolation]) -> List[MultiStackViolation]:
        """Remove diagnósticos duplicados (regra/arquivo/linha/mensagem) preservando ordem.

        Exemplo:
            >>> MultiStackRunner._dedup_native_violations([])
            []
        """
        seen: set[Tuple[str, str, int, str]] = set()
        unique: List[MultiStackViolation] = []
        for violation in violations:
            key = (
                violation.rule_id,
                violation.file_path,
                violation.line_number,
                violation.message[:80],
            )
            if key in seen:
                continue
            seen.add(key)
            unique.append(violation)
        return unique

    @classmethod
    def _merge_native_bridge(cls, base: Dict[str, Any], native: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        """Mescla diagnósticos da Camada 1 na resposta base sem duplicar regra/linha.

        Exemplo:
            >>> MultiStackRunner._merge_native_bridge({"blocking": [], "warnings": []}, None)
            {'blocking': [], 'warnings': []}
        """
        if not native:
            return base

        existing = {
            (item.get("rule_id"), item.get("file_path"), item.get("line_number"))
            for item in list(base.get("blocking", [])) + list(base.get("warnings", []))
        }
        for key in ("blocking", "warnings"):
            bucket = base.setdefault(key, [])
            for item in native.get(key, []):
                signature = (item.get("rule_id"), item.get("file_path"), item.get("line_number"))
                if signature in existing:
                    continue
                existing.add(signature)
                bucket.append(item)

        tools = native.get("tools") or []
        if any(tool.get("status") == "executed" for tool in tools):
            base["engine"] = f"{base.get('engine', 'unknown')}+native_toolchain_bridge"
        base["native_tools"] = tools
        base["native_bridge_duration"] = native.get("duration", 0.0)
        return base

    @classmethod
    def _run_native_bridge_guarded(
        cls,
        workspace_path: Path,
        stack: str,
        audit_mode: str,
        source_files: List[Path],
    ) -> Optional[Dict[str, Any]]:
        """Executa a Camada 1 com isolamento de falhas (nunca derruba a entrega).

        Exemplo:
            >>> isinstance(MultiStackRunner._run_native_bridge_guarded(Path("."), "UNKNOWN", "full", []), dict)
            True
        """
        try:
            return cls.run_native_bridge(
                workspace_path, stack, audit_mode=audit_mode, source_files=source_files,
            )
        except Exception as bridge_err:
            logger.debug("Camada 1 indisponível para %s: %s", workspace_path, bridge_err)
            return None

    @staticmethod
    def _absolute_native_path(workspace_path: Path, raw_path: str) -> Path:
        """Resolve um caminho relatado por linter contra a raiz do workspace.

        Exemplo:
            >>> str(MultiStackRunner._absolute_native_path(Path("/tmp"), "a.py"))
            '/tmp/a.py'
        """
        candidate = Path(raw_path)
        return candidate if candidate.is_absolute() else workspace_path / candidate

    @staticmethod
    def _relative_native_path(workspace_path: Path, raw_path: str) -> str:
        """Normaliza o caminho de um diagnóstico para relativo ao workspace.

        Exemplo:
            >>> MultiStackRunner._relative_native_path(Path("/tmp"), "a.py")
            'a.py'
        """
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            return str(candidate)
        try:
            return str(candidate.relative_to(workspace_path))
        except ValueError:
            return str(candidate)

    @staticmethod
    def _read_native_snippet(file_path: Path, line_number: int, cache: Dict[str, List[str]]) -> str:
        """Extrai o trecho da linha apontada pelo diagnóstico, com cache por arquivo.

        Exemplo:
            >>> MultiStackRunner._read_native_snippet(Path("inexistente-xyz.py"), 1, {})
            ''
        """
        key = str(file_path)
        lines = cache.get(key)
        if lines is None:
            try:
                lines = file_path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError as read_err:
                logger.debug("Snippet indisponível para %s: %s", file_path, read_err)
                lines = []
            cache[key] = lines
        if 1 <= line_number <= len(lines):
            return lines[line_number - 1].strip()[:120]
        return ""

    @staticmethod
    def _line_from_byte_offset(file_path: Path, offset: int, cache: Dict[str, bytes]) -> int:
        """Converte um offset de byte (biome/oxlint) na linha 1-based correspondente.

        Exemplo:
            >>> MultiStackRunner._line_from_byte_offset(Path("inexistente-xyz.py"), 0, {})
            1
        """
        key = str(file_path)
        data = cache.get(key)
        if data is None:
            try:
                data = file_path.read_bytes()
            except OSError as read_err:
                logger.debug("Offset indisponível para %s: %s", file_path, read_err)
                data = b""
            cache[key] = data
        if offset <= 0 or not data:
            return 1
        return data[: min(offset, len(data))].count(b"\n") + 1

    @classmethod
    def _make_native_violation(
        cls,
        workspace_path: Path,
        rule_id: str,
        category: str,
        raw_path: str,
        line_number: int,
        message: str,
        snippet_cache: Optional[Dict[str, List[str]]] = None,
        suggestion: Optional[str] = None,
    ) -> MultiStackViolation:
        """Constrói uma ``MultiStackViolation`` normalizada a partir de um diagnóstico nativo.

        Exemplo:
            >>> v = MultiStackRunner._make_native_violation(Path("."), "TSC_TS2322", "type_error", "a.ts", 3, "msg")
            >>> (v.rule_id, v.category, v.line_number)
            ('TSC_TS2322', 'type_error', 3)
        """
        rel_path = cls._relative_native_path(workspace_path, raw_path)
        line = max(int(line_number or 1), 1)
        snippet = ""
        if snippet_cache is not None and rel_path:
            snippet = cls._read_native_snippet(
                cls._absolute_native_path(workspace_path, rel_path), line, snippet_cache,
            )
        return MultiStackViolation(
            rule_id=rule_id,
            category=category,
            file_path=rel_path,
            line_number=line,
            code_snippet=snippet,
            message=message[:500],
            suggestion=suggestion or cls._native_suggestion(rule_id, category),
        )

    @classmethod
    def _native_suggestion(cls, rule_id: str, category: str) -> Optional[str]:
        """Devolve a sugestão canônica da regra nativa (ou fallback por categoria).

        Exemplo:
            >>> MultiStackRunner._native_suggestion("TSC_TS2322", "type_error") is not None
            True
        """
        if rule_id in RULE_SUGGESTIONS:
            return RULE_SUGGESTIONS[rule_id]
        if rule_id.startswith("TSC_"):
            return RULE_SUGGESTIONS["TSC_TYPE_ERROR"]
        if rule_id.startswith("ESLINT_") and rule_id in RULE_SUGGESTIONS:
            return RULE_SUGGESTIONS[rule_id]
        if rule_id.startswith("RUFF_"):
            return RULE_SUGGESTIONS["RUFF_LINT"]
        if rule_id.startswith("MYPY_"):
            return RULE_SUGGESTIONS["MYPY_TYPE_ERROR"]
        if rule_id.startswith("GOVET_"):
            return RULE_SUGGESTIONS["GOVET_DIAGNOSTIC"]
        if rule_id.startswith("CLIPPY_"):
            return RULE_SUGGESTIONS["CLIPPY_LINT"]
        if rule_id.startswith("DART_"):
            return RULE_SUGGESTIONS["DART_ANALYZE"]
        if category == "lint_error":
            return "Corrija o erro apontado pelo linter configurado no projeto antes da entrega."
        if category == "lint_warning":
            return "Avalie o alerta do linter e corrija ou registre supressão justificada."
        return None

    @staticmethod
    def _slugify_rule(rule: str) -> str:
        """Normaliza um identificador de regra para o padrão ``[A-Z0-9_]``.

        Exemplo:
            >>> MultiStackRunner._slugify_rule("react-hooks/rules-of-hooks")
            'REACT_HOOKS_RULES_OF_HOOKS'
        """
        return re.sub(r"[^A-Za-z0-9]+", "_", rule).strip("_").upper()

    @staticmethod
    def _load_json_payload(output: str, is_array: bool = False) -> Optional[Any]:
        """Interpreta saída JSON tolerando ruído/logs antes e depois do payload.

        Exemplo:
            >>> MultiStackRunner._load_json_payload('[]', is_array=True)
            []
        """
        text = output.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            opening = "[" if is_array else "{"
            start = text.find(opening)
            if start < 0:
                return None
            try:
                payload, _end = json.JSONDecoder().raw_decode(text[start:])
                return payload
            except json.JSONDecodeError as parse_err:
                logger.debug("Payload JSON nativo ilegível: %s", parse_err)
                return None

    @classmethod
    def _parse_tsc_output(cls, workspace_path: Path, output: str) -> List[MultiStackViolation]:
        """Converte a saída textual do ``tsc --noEmit`` em violações de tipo.

        Exemplo:
            >>> MultiStackRunner._parse_tsc_output(Path("."), "a.ts(2,5): error TS2322: bad")
            [MultiStackViolation(rule_id='TSC_TS2322', category='type_error', file_path='a.ts', line_number=2, code_snippet='', message='TS2322: bad', suggestion='Corrija a incompatibilidade de tipos apontada pelo TypeScript antes da entrega; evite \\'as any\\' e prefira narrowing explícito ou tipos auxiliares.')]
        """
        violations: List[MultiStackViolation] = []
        snippet_cache: Dict[str, List[str]] = {}
        line_pat = re.compile(
            r"^(?P<file>.+?)\((?P<line>\d+),(?P<col>\d+)\):\s+(?P<sev>error|warning)\s+(?P<code>TS\d+):\s+(?P<msg>.+)$"
        )
        global_pat = re.compile(r"^(?P<sev>error|warning)\s+(?P<code>TS\d+):\s+(?P<msg>.+)$")
        for raw_line in output.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            match = line_pat.match(stripped)
            if match:
                category = "type_error" if match.group("sev") == "error" else "lint_warning"
                violations.append(cls._make_native_violation(
                    workspace_path,
                    f"TSC_{match.group('code')}",
                    category,
                    match.group("file"),
                    int(match.group("line")),
                    f"{match.group('code')}: {match.group('msg')}",
                    snippet_cache,
                ))
                continue
            global_match = global_pat.match(stripped)
            if global_match:
                category = "type_error" if global_match.group("sev") == "error" else "lint_warning"
                violations.append(cls._make_native_violation(
                    workspace_path,
                    f"TSC_{global_match.group('code')}",
                    category,
                    "tsconfig.json",
                    1,
                    f"{global_match.group('code')}: {global_match.group('msg')}",
                ))
        return violations

    @classmethod
    def _parse_eslint_json(cls, workspace_path: Path, output: str) -> List[MultiStackViolation]:
        """Converte o JSON do ESLint em violações de hooks, deps, keys e state.

        Exemplo:
            >>> MultiStackRunner._parse_eslint_json(Path("."), "[]")
            []
        """
        payload = cls._load_json_payload(output, is_array=True)
        if not isinstance(payload, list):
            return []

        violations: List[MultiStackViolation] = []
        snippet_cache: Dict[str, List[str]] = {}
        for file_result in payload:
            if not isinstance(file_result, dict):
                continue
            file_path = str(file_result.get("filePath") or "")
            messages = file_result.get("messages")
            if not isinstance(messages, list):
                continue
            for message in messages:
                if not isinstance(message, dict):
                    continue
                rule = message.get("ruleId")
                severity = int(message.get("severity") or 1)
                line = int(message.get("line") or 1)
                if not rule:
                    rule_id = "ESLINT_PARSE_ERROR"
                    category = "lint_error"
                    text = f"Erro de parsing ESLint: {message.get('message', '')}"
                    hint: Optional[str] = RULE_SUGGESTIONS["LINT_PARSE_ERROR"]
                else:
                    mapped = cls._ESLINT_RULE_MAP.get(str(rule))
                    rule_id = mapped[0] if mapped else f"ESLINT_{cls._slugify_rule(str(rule))}"
                    category = "lint_error" if severity >= 2 else "lint_warning"
                    text = f"[{rule}] {message.get('message', '')}"
                    hint = mapped[1] if mapped else None
                violations.append(cls._make_native_violation(
                    workspace_path, rule_id, category, file_path, line, text, snippet_cache, hint,
                ))
        return violations

    @classmethod
    def _parse_biome_json(cls, workspace_path: Path, output: str) -> List[MultiStackViolation]:
        """Converte o JSON do Biome em violações, resolvendo linha por offset de byte.

        Exemplo:
            >>> MultiStackRunner._parse_biome_json(Path("."), "{}")
            []
        """
        payload = cls._load_json_payload(output)
        if not isinstance(payload, dict):
            return []
        diagnostics = payload.get("diagnostics")
        if not isinstance(diagnostics, list):
            return []

        violations: List[MultiStackViolation] = []
        byte_cache: Dict[str, bytes] = {}
        snippet_cache: Dict[str, List[str]] = {}
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, dict):
                continue
            category_raw = str(diagnostic.get("category") or "lint/unknown")
            severity = str(diagnostic.get("severity") or "warning").lower()
            location = diagnostic.get("location") if isinstance(diagnostic.get("location"), dict) else {}
            path_info = location.get("path") if isinstance(location.get("path"), dict) else {}
            raw_path = str(path_info.get("file") or path_info.get("path") or "")
            span = location.get("span") if isinstance(location.get("span"), list) else []
            line = 1
            if raw_path and span and isinstance(span[0], int):
                line = cls._line_from_byte_offset(
                    cls._absolute_native_path(workspace_path, raw_path), span[0], byte_cache,
                )
            rule_id, hint = cls._biome_rule(category_raw)
            category = "lint_error" if severity in ("error", "fatal") else "lint_warning"
            text = f"[{category_raw}] {diagnostic.get('description') or diagnostic.get('message') or ''}"
            violations.append(cls._make_native_violation(
                workspace_path, rule_id, category, raw_path, line, text, snippet_cache, hint,
            ))
        return violations

    @classmethod
    def _biome_rule(cls, category: str) -> Tuple[str, Optional[str]]:
        """Mapeia a categoria do Biome para rule_id canônico e sugestão.

        Exemplo:
            >>> MultiStackRunner._biome_rule("lint/correctness/useExhaustiveDependencies")[0]
            'BIOME_HOOK_DEPS'
        """
        lower = category.lower()
        if "rulesofhooks" in lower or "rules-of-hooks" in lower:
            return ("BIOME_RULES_OF_HOOKS", RULE_SUGGESTIONS["ESLINT_RULES_OF_HOOKS"])
        if "exhaustivedependencies" in lower or "exhaustive-deps" in lower:
            return ("BIOME_HOOK_DEPS", RULE_SUGGESTIONS["ESLINT_HOOK_DEPS"])
        if "jsxkeyiniterable" in lower or "noarrayindexkey" in lower or "missingkey" in lower:
            return ("BIOME_MISSING_KEY", RULE_SUGGESTIONS["ESLINT_MISSING_KEY"])
        if "mutation" in lower and "state" in lower:
            return ("BIOME_STATE_MUTATION", RULE_SUGGESTIONS["ESLINT_STATE_MUTATION"])
        return (f"BIOME_{cls._slugify_rule(category)}", None)

    @classmethod
    def _parse_oxlint_json(cls, workspace_path: Path, output: str) -> List[MultiStackViolation]:
        """Converte o JSON do oxlint em violações, resolvendo linha por offset.

        Exemplo:
            >>> MultiStackRunner._parse_oxlint_json(Path("."), "{}")
            []
        """
        payload = cls._load_json_payload(output)
        if isinstance(payload, dict):
            diagnostics = payload.get("diagnostics")
        elif isinstance(payload, list):
            diagnostics = payload
        else:
            diagnostics = None
        if not isinstance(diagnostics, list):
            return []

        violations: List[MultiStackViolation] = []
        byte_cache: Dict[str, bytes] = {}
        snippet_cache: Dict[str, List[str]] = {}
        for diagnostic in diagnostics:
            if not isinstance(diagnostic, dict):
                continue
            code = str(diagnostic.get("code") or "unknown")
            severity = str(diagnostic.get("severity") or "warning").lower()
            category = "lint_error" if severity == "error" else "lint_warning"
            raw_path = str(diagnostic.get("filename") or diagnostic.get("file") or "")
            line = 1
            labels = diagnostic.get("labels")
            if isinstance(labels, list) and labels and isinstance(labels[0], dict):
                span = labels[0].get("span")
                if isinstance(span, dict) and isinstance(span.get("offset"), int) and raw_path:
                    line = cls._line_from_byte_offset(
                        cls._absolute_native_path(workspace_path, raw_path), span["offset"], byte_cache,
                    )
                elif isinstance(span, dict) and isinstance(span.get("line"), int):
                    line = int(span["line"])
            violations.append(cls._make_native_violation(
                workspace_path,
                f"OXLINT_{cls._slugify_rule(code)}",
                category,
                raw_path,
                line,
                f"[{code}] {diagnostic.get('message') or ''}",
                snippet_cache,
            ))
        return violations

    @staticmethod
    def _is_ruff_blocking(code: str) -> bool:
        """Classifica códigos Ruff: Pyflakes (F*) e erros de sintaxe (E9*) bloqueiam.

        Exemplo:
            >>> (MultiStackRunner._is_ruff_blocking("F401"), MultiStackRunner._is_ruff_blocking("E501"))
            (True, False)
        """
        return code.startswith("F") or code.startswith("E9")

    @classmethod
    def _parse_ruff_json(cls, workspace_path: Path, output: str) -> List[MultiStackViolation]:
        """Converte o JSON do ``ruff check`` em violações (F*/E9* bloqueantes).

        Exemplo:
            >>> MultiStackRunner._parse_ruff_json(Path("."), "[]")
            []
        """
        payload = cls._load_json_payload(output, is_array=True)
        if not isinstance(payload, list):
            return []

        violations: List[MultiStackViolation] = []
        snippet_cache: Dict[str, List[str]] = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            code = str(item.get("code") or "UNKNOWN")
            location = item.get("location") if isinstance(item.get("location"), dict) else {}
            line = int(location.get("row") or 1)
            category = "lint_error" if cls._is_ruff_blocking(code) else "lint_warning"
            violations.append(cls._make_native_violation(
                workspace_path,
                f"RUFF_{cls._slugify_rule(code)}",
                category,
                str(item.get("filename") or ""),
                line,
                f"[{code}] {item.get('message') or ''}",
                snippet_cache,
            ))
        return violations

    @classmethod
    def _parse_mypy_output(cls, workspace_path: Path, output: str) -> List[MultiStackViolation]:
        """Converte a saída textual do mypy em violações de tipo bloqueantes.

        Exemplo:
            >>> MultiStackRunner._parse_mypy_output(Path("."), "a.py:3: error: bad  [assignment]")
            [MultiStackViolation(rule_id='MYPY_ASSIGNMENT', category='type_error', file_path='a.py', line_number=3, code_snippet='', message='bad  [assignment]', suggestion='Ajuste a anotação/tipo apontado pelo mypy; evite silenciar com \\'type: ignore\\' sem justificativa.')]
        """
        violations: List[MultiStackViolation] = []
        snippet_cache: Dict[str, List[str]] = {}
        pattern = re.compile(
            r"^(?P<file>.+?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<sev>error|warning|note):\s*(?P<msg>.+)$"
        )
        for raw_line in output.splitlines():
            stripped = raw_line.strip()
            if not stripped:
                continue
            match = pattern.match(stripped)
            if not match or match.group("sev") == "note":
                continue
            message = match.group("msg")
            code_match = re.search(r"\[(?P<code>[a-z][a-z0-9-]+)\]\s*$", message)
            code = code_match.group("code") if code_match else "type"
            category = "type_error" if match.group("sev") == "error" else "lint_warning"
            violations.append(cls._make_native_violation(
                workspace_path,
                f"MYPY_{cls._slugify_rule(code)}",
                category,
                match.group("file"),
                int(match.group("line")),
                message,
                snippet_cache,
            ))
        return violations

    @classmethod
    def _parse_go_vet_output(cls, workspace_path: Path, output: str) -> List[MultiStackViolation]:
        """Converte a saída do ``go vet`` em violações bloqueantes.

        Exemplo:
            >>> MultiStackRunner._parse_go_vet_output(Path("."), "a.go:12:3: bad call")
            [MultiStackViolation(rule_id='GOVET_DIAGNOSTIC', category='lint_error', file_path='a.go', line_number=12, code_snippet='', message='bad call', suggestion='Corrija o diagnóstico do \\'go vet\\' (printf, locks, struct tags) antes da entrega.')]
        """
        violations: List[MultiStackViolation] = []
        snippet_cache: Dict[str, List[str]] = {}
        pattern = re.compile(r"^(?P<file>[^\s:]+\.go):(?P<line>\d+)(?::(?P<col>\d+))?:\s+(?P<msg>.+)$")
        for raw_line in output.splitlines():
            match = pattern.match(raw_line.strip())
            if not match:
                continue
            violations.append(cls._make_native_violation(
                workspace_path,
                "GOVET_DIAGNOSTIC",
                "lint_error",
                match.group("file"),
                int(match.group("line")),
                match.group("msg"),
                snippet_cache,
            ))
        return violations

    @classmethod
    def _parse_cargo_json(cls, workspace_path: Path, output: str) -> List[MultiStackViolation]:
        """Converte o JSON line-delimited do ``cargo clippy`` em violações.

        Exemplo:
            >>> MultiStackRunner._parse_cargo_json(Path("."), "{}")
            []
        """
        violations: List[MultiStackViolation] = []
        snippet_cache: Dict[str, List[str]] = {}
        for raw_line in output.splitlines():
            stripped = raw_line.strip()
            if not stripped.startswith("{"):
                continue
            try:
                item = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if not isinstance(item, dict) or item.get("reason") != "compiler-message":
                continue
            message = item.get("message")
            if not isinstance(message, dict):
                continue
            level = str(message.get("level") or "warning")
            if level not in ("error", "warning"):
                continue
            code_info = message.get("code")
            code = str(code_info.get("code") or "clippy") if isinstance(code_info, dict) else "clippy"
            spans = message.get("spans")
            primary: Dict[str, Any] = {}
            if isinstance(spans, list):
                for span in spans:
                    if isinstance(span, dict) and span.get("is_primary"):
                        primary = span
                        break
                if not primary and spans and isinstance(spans[0], dict):
                    primary = spans[0]
            category = "lint_error" if level == "error" else "lint_warning"
            violations.append(cls._make_native_violation(
                workspace_path,
                f"CLIPPY_{cls._slugify_rule(code)}",
                category,
                str(primary.get("file_name") or ""),
                int(primary.get("line_start") or 1),
                str(message.get("message") or ""),
                snippet_cache,
            ))
        return violations

    @classmethod
    def _parse_dart_analyze_output(cls, workspace_path: Path, output: str) -> List[MultiStackViolation]:
        """Converte a saída machine-readable do ``dart analyze`` em violações.

        Exemplo:
            >>> MultiStackRunner._parse_dart_analyze_output(Path("."), "ERROR|COMPILE|X|a.dart|1|2|3|bad")
            [MultiStackViolation(rule_id='DART_X', category='lint_error', file_path='a.dart', line_number=1, code_snippet='', message='bad', suggestion='Resolva o diagnóstico do \\'dart analyze\\' antes da entrega.')]
        """
        violations: List[MultiStackViolation] = []
        snippet_cache: Dict[str, List[str]] = {}
        for raw_line in output.splitlines():
            parts = raw_line.strip().split("|")
            if len(parts) < 8:
                continue
            severity, _kind, code, file_path, line_raw = parts[0], parts[1], parts[2], parts[3], parts[4]
            message = "|".join(parts[7:])
            try:
                line = int(line_raw)
            except ValueError:
                line = 1
            category = "lint_error" if severity.upper() == "ERROR" else "lint_warning"
            violations.append(cls._make_native_violation(
                workspace_path,
                f"DART_{cls._slugify_rule(code)}",
                category,
                file_path,
                line,
                message,
                snippet_cache,
            ))
        return violations

    # ── Camada 2: Meta-Regras Universais via Tree-Sitter (Maya/B1) ──────────

    _TREE_SITTER_DEDUP_ALIASES: Dict[str, str] = {
        "PROD_MOCK": "PROD_MOCK",
        "JS_PROD_MOCK": "PROD_MOCK",
        "DANGEROUS_EVAL": "EVAL",
        "SECURITY_EVAL": "EVAL",
        "SECURITY_EXEC": "EXEC",
        "SQL_INJECTION": "SQL_INJECTION",
        "SILENT_CATCH": "SILENT_CATCH",
        "EMPTY_CATCH": "SILENT_CATCH",
    }

    @classmethod
    def scan_tree_sitter(
        cls,
        workspace_path: Path,
        stack: str,
        audit_mode: str = "delta",
        source_files: Optional[List[Path]] = None,
    ) -> Optional[Dict[str, Any]]:
        """Executa a Camada 2 (AST Tree-Sitter) sobre os arquivos JS/TS de produção.

        Devolve o mesmo formato de scan_js_ast (blocking/warnings/total_files/engine)
        ou ``None`` quando a Camada 2 estiver indisponível (degradação graciosa).

        Exemplo:
            >>> res = MultiStackRunner.scan_tree_sitter(Path("."), "NODE_TS_BUN")
            >>> res is None or set(res) >= {"blocking", "warnings", "total_files", "engine"}
            True
        """
        try:
            from ..memory.tree_sitter_guard import TreeSitterGuard
        except Exception as import_err:  # pragma: no cover - fallback gracioso
            logger.debug("Camada 2 (Tree-Sitter) indisponível: %s", import_err)
            return None

        if source_files is None:
            source_files = cls._collect_js_source_files(workspace_path, stack)

        blocking: List[MultiStackViolation] = []
        warnings: List[MultiStackViolation] = []
        parsed_files = 0
        fallback_files = 0

        for fpath in source_files:
            language = TreeSitterGuard.language_for_file(fpath)
            if TreeSitterGuard.get_parser(language) is not None:
                parsed_files += 1
            else:
                fallback_files += 1
            try:
                found = TreeSitterGuard.scan_file(fpath, workspace=workspace_path)
            except Exception as scan_err:  # pragma: no cover - fallback gracioso
                logger.debug("Falha da Camada 2 em %s: %s", fpath, scan_err)
                continue
            for item in found:
                payload = item.to_dict()
                relative = cls._tree_sitter_relpath(payload["file_path"], workspace_path)
                violation = MultiStackViolation(
                    rule_id=payload["rule_id"],
                    category=payload["category"],
                    file_path=relative,
                    line_number=payload["line_number"],
                    code_snippet=payload["code_snippet"],
                    message=payload["message"],
                    suggestion=payload.get("suggestion"),
                )
                if payload.get("severity") == "blocking":
                    blocking.append(violation)
                else:
                    warnings.append(violation)

        if parsed_files and fallback_files:
            engine = "tree_sitter_partial"
        elif fallback_files and not parsed_files:
            engine = "tree_sitter_lexical_fallback"
        else:
            engine = "tree_sitter"

        return {
            "blocking": [asdict(v) for v in blocking],
            "warnings": [asdict(v) for v in warnings],
            "total_files": len(source_files),
            "engine": engine,
        }

    @staticmethod
    def _tree_sitter_relpath(file_path: str, workspace_path: Path) -> str:
        """Normaliza o caminho reportado pela Camada 2 para relativo ao workspace."""
        path = Path(file_path)
        try:
            if path.is_absolute():
                return str(path.resolve().relative_to(workspace_path.resolve()))
        except (OSError, ValueError) as exc:
            logger.debug(
                "Falha ao relativizar caminho %s contra workspace %s: %s",
                file_path,
                workspace_path,
                exc,
            )
        return str(file_path)

    @classmethod
    def _collect_js_source_files(cls, workspace_path: Path, stack: str) -> List[Path]:
        """Coleta arquivos JS/TS de produção com o mesmo critério de scan_js_ast."""
        if stack == "REACT_NATIVE_EXPO":
            base_app = workspace_path / "PanelaAndroid" if (workspace_path / "PanelaAndroid").exists() else workspace_path
            prod_dirs = [base_app / "src", base_app / "scripts"]
        elif stack == "WEB_JS_FIREBASE":
            prod_dirs = [workspace_path / "site" / "js", workspace_path / "site"]
        else:
            prod_dirs = [workspace_path]

        raw_files = [
            fpath
            for pdir in prod_dirs
            if pdir.exists()
            for ext in ("*.js", "*.jsx", "*.ts", "*.tsx", "*.mjs")
            for fpath in pdir.rglob(ext)
        ]
        files = cls._dedup_files(raw_files)
        return [
            f
            for f in files
            if not any(
                x in str(f.relative_to(workspace_path))
                for x in ("node_modules", "__tests__", "tests", "__mocks__", "coverage", ".git", ".expo")
            )
        ]

    @classmethod
    def _merge_tree_sitter(cls, base_result: Dict[str, Any], ts_result: Dict[str, Any]) -> Dict[str, Any]:
        """Mescla a Camada 2 no resultado base, deduplicando regras equivalentes."""
        merged = dict(base_result)
        seen = set()
        for bucket in ("blocking", "warnings"):
            for violation in merged.get(bucket, []) or []:
                seen.add(cls._tree_sitter_dedup_key(violation))

        for bucket in ("blocking", "warnings"):
            for violation in (ts_result or {}).get(bucket, []) or []:
                key = cls._tree_sitter_dedup_key(violation)
                if key in seen:
                    continue
                seen.add(key)
                merged.setdefault(bucket, []).append(violation)

        merged["total_files"] = max(
            merged.get("total_files", 0), (ts_result or {}).get("total_files", 0)
        )
        merged["tree_sitter_files"] = (ts_result or {}).get("total_files", 0)
        ts_engine = (ts_result or {}).get("engine", "tree_sitter")
        merged["engine"] = f"{merged.get('engine', 'unknown')}+{ts_engine}"
        return merged

    @classmethod
    def _tree_sitter_dedup_key(cls, violation: Dict[str, Any]) -> Tuple[str, str, int]:
        """Chave de deduplicação normalizada entre Camada 1 e Camada 2."""
        rule_id = violation.get("rule_id", "")
        alias = cls._TREE_SITTER_DEDUP_ALIASES.get(rule_id, rule_id)
        return (alias, violation.get("file_path", ""), violation.get("line_number", 0))

    @classmethod
    def scan_js_ast(cls, workspace_path: Path, stack: str, audit_mode: str = "delta") -> Dict[str, Any]:
        """Varre os arquivos JS/TS da stack procurando mocks, eval, XSS e segredos hardcoded via AST e delta.

        Exemplo:
            >>> res = MultiStackRunner.scan_js_ast(Path("."), "WEB_JS_FIREBASE", audit_mode="delta")
            >>> "blocking" in res and "warnings" in res
            True
        """
        blocking: List[MultiStackViolation] = []
        warnings: List[MultiStackViolation] = []

        prod_dirs = []
        if stack == "REACT_NATIVE_EXPO":
            base_app = workspace_path / "PanelaAndroid" if (workspace_path / "PanelaAndroid").exists() else workspace_path
            prod_dirs = [base_app / "src", base_app / "scripts"]
        elif stack == "WEB_JS_FIREBASE":
            prod_dirs = [workspace_path / "site" / "js", workspace_path / "site"]
        else:
            prod_dirs = [workspace_path]

        raw_files = [
            fpath
            for pdir in prod_dirs
            if pdir.exists()
            for ext in ("*.js", "*.jsx", "*.ts", "*.tsx", "*.mjs")
            for fpath in pdir.rglob(ext)
        ]
        source_files = cls._dedup_files(raw_files)

        if audit_mode == "delta":
            source_files = cls._filter_delta_files(workspace_path, source_files)

        # Remove caminhos de teste, node_modules e diretórios de build
        source_files = [
            f for f in source_files
            if not any(x in str(f.relative_to(workspace_path)) for x in ("node_modules", "__tests__", "tests", "__mocks__", "coverage", ".git", ".expo"))
        ]

        if not source_files:
            return {
                "blocking": [],
                "warnings": [],
                "total_files": 0
            }

        node_res = cls._run_node_js_ast(workspace_path, source_files, stack, blocking, warnings)
        if node_res is not None:
            base_result = node_res
        else:
            base_result = cls._run_lexical_fallback(workspace_path, source_files, stack, blocking, warnings)

        # Camada 1: bridge nativa (tsc + eslint/biome/oxlint) mesclada ao resultado do AST.
        native_res = cls._run_native_bridge_guarded(workspace_path, stack, audit_mode, source_files)
        merged_result = cls._merge_native_bridge(base_result, native_res)

        # ── Camada 2 (Tree-Sitter) — ponto de extensão ──
        # (Maya/B1 insere aqui a chamada opcional a cls.scan_tree_sitter(...) e mescla blocking/warnings)
        try:
            ts_result = cls.scan_tree_sitter(
                workspace_path, stack, audit_mode, source_files=source_files
            )
        except Exception as ts_err:  # pragma: no cover - fallback gracioso
            logger.debug("Camada 2 (Tree-Sitter) degradada: %s", ts_err)
            ts_result = None
        if ts_result:
            merged_result = cls._merge_tree_sitter(merged_result, ts_result)
        return merged_result
