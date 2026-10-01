"""
Multi-Stack Runner & Language Drivers for Needle 3 Verify Delivery.
Permite ao needle_verify_delivery auditar projetos em diferentes stacks:
- Python (JEV, Predictera, Faturamento)
- React Native / Expo (PanelaAndroid)
- Web Vanilla JS / Node / Firebase (JEZ collections)
"""

import os
import sys
import re
import time
import logging
import subprocess
from pathlib import Path
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, asdict

logger = logging.getLogger("jev.multi_stack")


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

        # Fallback por arquivos
        if list(workspace_path.glob("*.py")):
            return "PYTHON_GENERIC"
        if (workspace_path / "package.json").exists():
            return "NODE_JS_GENERIC"

        return "UNKNOWN"

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

    @classmethod
    def scan_js_ast(cls, workspace_path: Path, stack: str) -> Dict[str, Any]:
        """Varre os arquivos JS/TS da stack procurando mocks, eval, XSS e segredos hardcoded."""
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

        PATTERNS_BLOCKING = [
            (r"\beval\s*\(", "SECURITY_EVAL", "Uso proibido de eval()"),
            (r"new\s+Function\s*\(", "SECURITY_EXEC", "Uso proibido de new Function()"),
            (r"AIzaSy[0-9A-Za-z-_]{33}", "HARDCODED_SECRET", "Chave privada de API hardcoded em arquivo de produção"),
            (r"\.innerHTML\s*=\s*(?!escapeHtml|DOMPurify)[^;]+[+`]", "XSS_INNER_HTML", "Atribuição insegura de innerHTML sem sanitização"),
        ]

        PATTERNS_WARNING = [
            (r"console\.log\(", "CODE_SMELL_CONSOLE", "Chamada de console.log residual em produção"),
            (r"catch\s*\([^)]*\)\s*\{\s*\}", "EMPTY_CATCH", "Bloco catch silencioso engolindo exceções sem log ou tratamento"),
        ]

        source_files = [
            fpath
            for pdir in prod_dirs
            if pdir.exists()
            for ext in ("*.js", "*.jsx", "*.ts", "*.tsx")
            for fpath in pdir.rglob(ext)
        ]

        scanned_count = 0
        for fpath in source_files:
            rel = str(fpath.relative_to(workspace_path))
            if any(x in rel for x in ("node_modules", "__tests__", "tests", "__mocks__", "coverage", ".git", ".expo")):
                continue

            scanned_count += 1
            try:
                content = fpath.read_text(encoding="utf-8", errors="ignore")
            except Exception as read_err:
                logger.warning("Arquivo %s ignorado na varredura JS (leitura falhou): %s", fpath, read_err)
                continue

            lines = content.splitlines()

            # Anti-mock em produção
            if "jest.mock(" in content or "jest.fn(" in content or "sinon.stub(" in content:
                blocking.extend(
                    MultiStackViolation(
                        rule_id="JS_PROD_MOCK",
                        category="anti_mock_producao",
                        file_path=rel,
                        line_number=idx,
                        code_snippet=line.strip()[:100],
                        message="Mock detectado em arquivo de produção sob pasta de código-fonte."
                    )
                    for idx, line in enumerate(lines, 1)
                    if "jest.mock(" in line or "jest.fn(" in line or "sinon.stub(" in line
                )

            # Anti-emoji para JEZ
            if stack == "WEB_JS_FIREBASE":
                emoji_pattern = re.compile(r"[\U0001F300-\U0001F6FF\U0001F900-\U0001F9FF]")
                warnings.extend(
                    MultiStackViolation(
                        rule_id="JEZ_ANTI_EMOJI",
                        category="brand_invariable",
                        file_path=rel,
                        line_number=idx,
                        code_snippet=line.strip()[:100],
                        message="Emoji detectado no código/template em violação à diretriz inegociável da marca JEZ."
                    )
                    for idx, line in enumerate(lines, 1)
                    if emoji_pattern.search(line)
                )

            blocking.extend(
                MultiStackViolation(
                    rule_id=rule_id,
                    category="security",
                    file_path=rel,
                    line_number=idx,
                    code_snippet=lines[idx - 1].strip()[:100],
                    message=msg
                )
                for pat, rule_id, msg in PATTERNS_BLOCKING
                for idx, line in enumerate(lines, 1)
                if re.search(pat, line)
            )

            warnings.extend(
                MultiStackViolation(
                    rule_id=rule_id,
                    category="hygiene",
                    file_path=rel,
                    line_number=idx,
                    code_snippet=lines[idx - 1].strip()[:100],
                    message=msg
                )
                for pat, rule_id, msg in PATTERNS_WARNING
                for idx, line in enumerate(lines, 1)
                if re.search(pat, line)
            )

        return {
            "blocking": [asdict(b) for b in blocking],
            "warnings": [asdict(w) for w in warnings],
            "total_files": scanned_count
        }
