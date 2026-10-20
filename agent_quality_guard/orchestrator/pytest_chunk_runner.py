"""
Pytest Chunk Runner — Execução de Testes Reais em Chunks Sequenciais.

Para evitar estressar a GPU GTX 1050 Ti e a CPU do ambiente local, este módulo
divide a suíte de testes do repositório em 3 a 4 chunks sequenciais,
garantindo que os testes mais pesados de ML, hardware e inferência rodem isolados.
"""

import os
import sys
import time
import logging
import subprocess
from pathlib import Path
from typing import List, Dict, Any, Tuple, Optional

logger = logging.getLogger("agent_quality_guard.orchestrator.pytest_chunk_runner")


HEAVY_TEST_PATTERNS = [
    "test_laya_adapter.py",
    "test_consulta.py",
    "test_blender_visual_loop.py",
    "test_hardware.py",
    "test_engine_failfast.py",
    "test_score_calibration.py",
    "test_diagnosis_calibration.py",
    "test_ipc_service.py",
    "test_ipc_client.py",
]


def _count_from_pytest_line(part: str) -> Tuple[int, int]:
    """Extrai a tupla (passados, falhados) de um trecho da linha de resumo do pytest.

    Exemplo:
        >>> _count_from_pytest_line("3 passed")
        (3, 0)
    """
    part = part.strip()
    if "passed" in part:
        try:
            return int(part.split()[0]), 0
        except (ValueError, IndexError) as parse_err:
            logger.debug(f"Não foi possível converter contagem de testes passados: {parse_err}")
    elif "failed" in part or "error" in part:
        try:
            return 0, int(part.split()[0])
        except (ValueError, IndexError) as parse_err:
            logger.debug(f"Não foi possível converter contagem de testes falhados: {parse_err}")
    return 0, 0


class PytestChunkRunner:
    """Executa a suíte pytest dividida em 3 a 4 chunks sequenciais com isolamento dos testes pesados."""

    @staticmethod
    def _partition_light_tests(light_files: List[str]) -> List[Tuple[str, List[str]]]:
        """Reparte os testes leves em 2 ou 3 chunks nomeados e balanceados.

        Exemplo:
            >>> PytestChunkRunner._partition_light_tests(["a.py"])[0][0]
            'chunk_2_light_tests'
        """
        total_light = len(light_files)
        if total_light <= 4:
            return [("chunk_2_light_tests", light_files)]

        if total_light < 9:
            mid = (total_light + 1) // 2
            return [
                ("chunk_2_core_governance", light_files[:mid]),
                ("chunk_3_memory_ast", light_files[mid:]),
            ]

        # Distribui arquivos leves uniformemente entre os 3 chunks de governança/memória/api
        c2: List[str] = []
        c3: List[str] = []
        c4: List[str] = []
        for f in light_files:
            target = min((c2, c3, c4), key=len)
            target.append(f)

        parts = [
            ("chunk_2_core_governance", c2),
            ("chunk_3_memory_ast", c3),
            ("chunk_4_api_orchestration", c4),
        ]
        return [chunk for chunk in parts if chunk[1]]

    @staticmethod
    def _is_heavy_test_relevant(test_file: str, diff_file_paths: List[str]) -> bool:
        """Verifica se um teste pesado tem correlação direta com os arquivos alterados no git diff.

        Exemplo:
            >>> PytestChunkRunner._is_heavy_test_relevant("tests/test_laya_adapter.py", ["src/decision/laya_adapter.py"])
            True
        """
        basename = os.path.basename(test_file)
        for df in diff_file_paths:
            if test_file in df or basename in df:
                return True

        module_keywords: Dict[str, List[str]] = {
            "test_blender_visual_loop.py": ["blender", "visual_qa"],
            "test_laya_adapter.py": ["laya", "engine", "hardware", "checkpoint"],
            "test_consulta.py": ["consulta", "engine"],
            "test_hardware.py": ["hardware"],
            "test_engine_failfast.py": ["engine", "score_gate", "failfast"],
            "test_score_calibration.py": ["score", "calibration"],
            "test_diagnosis_calibration.py": ["diagnosis", "calibration"],
            "test_ipc_service.py": ["ipc"],
            "test_ipc_client.py": ["ipc"],
        }

        keywords = module_keywords.get(basename, [basename.replace("test_", "").replace(".py", "")])
        for df in diff_file_paths:
            df_lower = df.lower()
            if any(kw in df_lower for kw in keywords):
                return True
        return False

    @classmethod
    def _filter_delta_tests(
        cls,
        heavy_files: List[str],
        light_files: List[str],
        workspace_path: str,
    ) -> Tuple[List[str], List[str]]:
        """Filtra listas de testes pesados e leves com base nos arquivos alterados no git diff.

        Exemplo:
            >>> heavy, light = PytestChunkRunner._filter_delta_tests([], [], ".")
            >>> isinstance(heavy, list) and isinstance(light, list)
            True
        """
        try:
            from ..core.diff_inspector import get_workspace_diff_files, normalize_workspace_relpath
            diff_files = get_workspace_diff_files(workspace_path)
            diff_paths = [normalize_workspace_relpath(df.path, workspace_path) for df in diff_files]
            if not diff_paths:
                return heavy_files, light_files

            filtered_heavy = [
                tf for tf in heavy_files
                if cls._is_heavy_test_relevant(normalize_workspace_relpath(tf, workspace_path), diff_paths)
            ]
            filtered_light = [
                tf for tf in light_files
                if cls._is_heavy_test_relevant(normalize_workspace_relpath(tf, workspace_path), diff_paths)
            ]
            final_light = filtered_light if filtered_light else light_files
            return filtered_heavy, final_light
        except Exception as delta_err:
            logger.debug(f"Falha ao inspecionar diff para filtrar testes: {delta_err}")
            return heavy_files, light_files

    @classmethod
    def discover_and_partition_tests(
        cls,
        workspace_path: str,
        audit_mode: str = "delta",
    ) -> List[Tuple[str, List[str]]]:
        """Descobre os testes em tests/ do workspace e reparticiona em 3 a 4 chunks.

        Exemplo:
            >>> chunks = PytestChunkRunner.discover_and_partition_tests("tests")
            >>> all(files for _, files in chunks)
            True
        """
        tests_dir = Path(workspace_path) / "tests"
        if not tests_dir.exists() or not tests_dir.is_dir():
            return []

        all_test_files = sorted([
            str(p.relative_to(workspace_path)) for p in tests_dir.glob("test_*.py")
            if p.is_file() and p.name != "verify_all.py"
        ])

        if not all_test_files:
            return []

        heavy_files: List[str] = []
        light_files: List[str] = []

        for tf in all_test_files:
            basename = os.path.basename(tf)
            if any(hp in basename for hp in HEAVY_TEST_PATTERNS):
                heavy_files.append(tf)
            else:
                light_files.append(tf)

        if audit_mode == "delta":
            heavy_files, light_files = cls._filter_delta_tests(heavy_files, light_files, workspace_path)

        chunks: List[Tuple[str, List[str]]] = []

        # Chunk 1: Testes pesados particionados para maximizar paralelismo real
        blender_heavy = [f for f in heavy_files if "blender" in f]
        ml_heavy = [f for f in heavy_files if "blender" not in f]

        if blender_heavy:
            chunks.append(("chunk_1_heavy_blender_isolated", blender_heavy))
        if ml_heavy:
            chunks.append(("chunk_1_heavy_ml_isolated", ml_heavy))

        # Repartir os leves em 2 a 3 chunks adicionais
        if light_files:
            chunks.extend(cls._partition_light_tests(light_files))

        return chunks

    @classmethod
    def _execute_single_chunk(
        cls,
        chunk: Tuple[str, List[str]],
        workspace_path: str,
        python_bin: str,
        timeout_per_chunk: int,
    ) -> Dict[str, Any]:
        """Executa um chunk individual em subprocesso pytest com isolamento de ambiente.

        Exemplo:
            >>> res = PytestChunkRunner._execute_single_chunk(("chk", ["tests/test_cli_defaults.py"]), ".", sys.executable, 30)
            >>> "passed" in res
            True
        """
        chunk_id, files = chunk
        start_time = time.time()
        cmd = [python_bin, "-m", "pytest"] + files + ["-q", "--tb=short"]

        env = os.environ.copy()
        env["JEV_USE_HERMES"] = "false"
        env["HF_HUB_OFFLINE"] = "1"
        env["TRANSFORMERS_OFFLINE"] = "1"

        try:
            proc = subprocess.run(
                cmd,
                cwd=workspace_path,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=timeout_per_chunk,
            )
            duration = round(time.time() - start_time, 2)
            passed = (proc.returncode == 0)

            output = proc.stdout + "\n" + proc.stderr
            passed_count, failed_count = cls._parse_pytest_output(output)

            failure_details = []
            if not passed:
                err_lines = [l for l in output.splitlines() if "FAILED" in l or "ERROR" in l or "FAILURES" in l]
                failure_details = err_lines[:10] if err_lines else [output[-500:]]

            return {
                "chunk_id": chunk_id,
                "passed": passed,
                "total_passed": passed_count,
                "total_failed": failed_count,
                "execution_time_seconds": duration,
                "failure_details": failure_details,
                "file_count": len(files),
            }
        except subprocess.TimeoutExpired:
            duration = round(time.time() - start_time, 2)
            return {
                "chunk_id": chunk_id,
                "passed": False,
                "total_passed": 0,
                "total_failed": len(files),
                "execution_time_seconds": duration,
                "failure_details": [f"Timeout de {timeout_per_chunk}s excedido durante execução do chunk."],
                "file_count": len(files),
            }

    @classmethod
    def run_all_chunks(
        cls,
        workspace_path: str = ".",
        timeout_per_chunk: int = 120,
        fail_fast: bool = True,
        audit_mode: str = "delta",
        max_total_timeout: Optional[int] = None,
    ) -> Dict[str, Any]:
        """Executa a suíte particionada com chunk pesado isolado (solo) e leves em paralelo.

        Exemplo:
            >>> res = PytestChunkRunner.run_all_chunks(".", timeout_per_chunk=30)
            >>> isinstance(res, dict) and "pytest_success" in res
            True
        """
        python_bin = sys.executable
        venv_python = Path(workspace_path) / ".venv" / "bin" / "python"
        if venv_python.exists():
            python_bin = str(venv_python)

        chunks = cls.discover_and_partition_tests(workspace_path, audit_mode=audit_mode)
        if not chunks:
            return {
                "pytest_success": False,
                "total_chunks": 0,
                "chunks": [],
                "message": (
                    "🚨 [AÇÃO OBRIGATÓRIA DO AGENTE: TESTES AUSENTES]\n"
                    "Nenhum arquivo de teste ('test_*.py') foi localizado no diretório 'tests/'.\n"
                    "O que você DEVE fazer imediatamente:\n"
                    "1. NÃO tente burlar ou forçar aprovação — criar pelo menos um teste real é mandatório.\n"
                    "2. Crie um arquivo em 'tests/test_<modulo>.py' cobrindo os caminhos críticos e de erro da sua alteração.\n"
                    "3. Execute os testes no ambiente real (.venv/bin/pytest) e apenas então re-submeta à validação."
                ),
            }

        chunk_results: List[Dict[str, Any]] = []
        overall_success = True
        start_suite = time.time()

        # Execução estritamente sequencial (RN-JEV-005) com isolamento solo do chunk ML
        for chunk in chunks:
            if max_total_timeout is not None:
                elapsed = time.time() - start_suite
                remaining = max_total_timeout - elapsed
                if remaining <= 0:
                    logger.warning("Tempo limite total da suíte (%ss) esgotado antes de executar %s", max_total_timeout, chunk[0])
                    overall_success = False
                    chunk_results.append({
                        "chunk_id": chunk[0],
                        "passed": False,
                        "total_passed": 0,
                        "total_failed": len(chunk[1]),
                        "execution_time_seconds": 0.0,
                        "failure_details": [f"Orçamento de tempo global ({max_total_timeout}s) excedido."],
                        "file_count": len(chunk[1]),
                    })
                    break
                actual_timeout = min(timeout_per_chunk, max(3, int(remaining)))
            else:
                actual_timeout = timeout_per_chunk

            res = cls._execute_single_chunk(chunk, workspace_path, python_bin, actual_timeout)
            chunk_results.append(res)
            if not res["passed"]:
                overall_success = False
                if fail_fast:
                    break

        return {
            "pytest_success": overall_success,
            "total_chunks": len(chunks),
            "chunks": chunk_results,
            "message": "Todos os chunks de teste passaram com sucesso." if overall_success else "Falha detectada em um ou mais chunks de teste.",
        }

    @staticmethod
    def _parse_pytest_output(output: str) -> Tuple[int, int]:
        """Tenta extrair quantidade de testes passados e falhados da saída do pytest."""
        summary_parts = [
            part
            for line in output.splitlines()
            if "passed" in line or "failed" in line or "error" in line
            for part in line.split(",")
        ]
        passed_count = 0
        failed_count = 0
        for part in summary_parts:
            part_passed, part_failed = _count_from_pytest_line(part)
            if part_passed:
                passed_count = part_passed
            failed_count += part_failed
        return passed_count, failed_count
