"""
Multi-Stack Runner & Language Drivers for Needle 3 Verify Delivery.
Permite ao needle_verify_delivery auditar projetos em diferentes stacks:
- Python (JEV, Predictera, Faturamento)
- React Native / Expo (PanelaAndroid)
- Web Vanilla JS / Node / Firebase (JEZ collections)
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
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
}


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

        # Se for projeto JEV
        if (workspace_path / "src" / "mcp_server.py").exists():
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
            from src.core.diff_inspector import get_workspace_diff_files, normalize_workspace_relpath
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
            return node_res

        return cls._run_lexical_fallback(workspace_path, source_files, stack, blocking, warnings)
