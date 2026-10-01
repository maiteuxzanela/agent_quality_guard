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
from typing import List, Dict, Any, Tuple

logger = logging.getLogger("jev.orchestrator.pytest_chunk_runner")


HEAVY_TEST_PATTERNS = [
    "test_laya_adapter.py",
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
        """Reparte os testes leves em 2 ou 3 chunks nomeados.

        Exemplo:
            >>> PytestChunkRunner._partition_light_tests(["a.py"])[0][0]
            'chunk_2_light_tests'
        """
        total_light = len(light_files)
        if total_light <= 4:
            return [("chunk_2_light_tests", light_files)]

        mid = (total_light + 1) // 3 if total_light >= 9 else (total_light + 1) // 2
        parts = [
            ("chunk_2_core_governance", light_files[:mid]),
            ("chunk_3_memory_ast", light_files[mid:mid * 2]),
            ("chunk_4_api_orchestration", light_files[mid * 2:]),
        ]
        if total_light < 9:
            parts = [
                ("chunk_2_core_governance", light_files[:mid]),
                ("chunk_3_memory_ast", light_files[mid:]),
            ]
        return [chunk for chunk in parts if chunk[1]]

    @staticmethod
    def discover_and_partition_tests(workspace_path: str) -> List[Tuple[str, List[str]]]:
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

        heavy_files = []
        light_files = []

        for tf in all_test_files:
            basename = os.path.basename(tf)
            if any(hp in basename for hp in HEAVY_TEST_PATTERNS):
                heavy_files.append(tf)
            else:
                light_files.append(tf)

        chunks: List[Tuple[str, List[str]]] = []

        # Chunk 1: Testes pesados isolados (ML / Hardware / Engine)
        if heavy_files:
            chunks.append(("chunk_1_heavy_ml_isolated", heavy_files))

        # Repartir os leves em 2 a 3 chunks adicionais
        if light_files:
            chunks.extend(PytestChunkRunner._partition_light_tests(light_files))

        return chunks

    @classmethod
    def run_all_chunks(
        cls,
        workspace_path: str = ".",
        timeout_per_chunk: int = 120,
        fail_fast: bool = True
    ) -> Dict[str, Any]:
        """Executa sequencialmente todos os chunks de testes.

        Retorna dicionário estruturado com os resultados.
        """
        python_bin = sys.executable
        venv_python = Path(workspace_path) / ".venv" / "bin" / "python"
        if venv_python.exists():
            python_bin = str(venv_python)

        chunks = cls.discover_and_partition_tests(workspace_path)
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
                )
            }

        chunk_results = []
        overall_success = True

        for chunk_id, files in chunks:
            start_time = time.time()
            cmd = [python_bin, "-m", "pytest"] + files + ["-q", "--tb=short"]

            env = os.environ.copy()
            env["JEV_USE_HERMES"] = "false"

            try:
                proc = subprocess.run(
                    cmd,
                    cwd=workspace_path,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    timeout=timeout_per_chunk
                )
                duration = round(time.time() - start_time, 2)
                passed = (proc.returncode == 0)

                # Parse de contagem de testes no stdout/stderr
                output = proc.stdout + "\n" + proc.stderr
                passed_count, failed_count = cls._parse_pytest_output(output)

                failure_details = []
                if not passed:
                    overall_success = False
                    # Extrair linhas relevantes do erro
                    err_lines = [l for l in output.splitlines() if "FAILED" in l or "ERROR" in l or "FAILURES" in l]
                    failure_details = err_lines[:10] if err_lines else [output[-500:]]

                chunk_results.append({
                    "chunk_id": chunk_id,
                    "passed": passed,
                    "total_passed": passed_count,
                    "total_failed": failed_count,
                    "execution_time_seconds": duration,
                    "failure_details": failure_details,
                    "file_count": len(files)
                })

                if not passed and fail_fast:
                    # Aborta os chunks subsequentes imediatamente
                    break

            except subprocess.TimeoutExpired:
                duration = round(time.time() - start_time, 2)
                overall_success = False
                chunk_results.append({
                    "chunk_id": chunk_id,
                    "passed": False,
                    "total_passed": 0,
                    "total_failed": len(files),
                    "execution_time_seconds": duration,
                    "failure_details": [f"Timeout de {timeout_per_chunk}s excedido durante execução do chunk."],
                    "file_count": len(files)
                })
                if fail_fast:
                    break

        return {
            "pytest_success": overall_success,
            "total_chunks": len(chunks),
            "chunks": chunk_results,
            "message": "Todos os chunks de teste passaram com sucesso." if overall_success else "Falha detectada em um ou mais chunks de teste."
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
