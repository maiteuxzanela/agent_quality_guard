"""Teste E2E: SubagentRunner invocando Hermes Agent + DeepSeek Flash 4.1.

Regra: ZERO MOCKS. Cria um repo temporário real, despacha a Lumi para criar um
componente de utilitário, e verifica o código criado pelo DeepSeek + auditoria JEV.
"""

import asyncio
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from src.core.schemas import SpawnSubagentRequest
from src.decision.engine import OpenJevEngine
from src.orchestrator.subagent_runner import SubagentRunner


async def main():
    print(">>> Iniciando Teste E2E: SubagentRunner + Hermes Agent (DeepSeek Flash 4.1)")

    # 1. Cria workspace temporário
    tmp_dir = tempfile.mkdtemp(prefix="jev_e2e_hermes_")
    ws_path = Path(tmp_dir).resolve()
    subprocess.run(["git", "init"], cwd=str(ws_path), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.name", "Test Runner"], cwd=str(ws_path), check=True)
    subprocess.run(["git", "config", "user.email", "test@jev.local"], cwd=str(ws_path), check=True)

    # Copia persona da Lumi
    personas_dir = ws_path / "personas"
    personas_dir.mkdir(parents=True, exist_ok=True)
    (personas_dir / "lumi_ui_ux_frontend.md").write_text(
        "# Lumi — UI/UX & Frontend\n\n"
        "```markdown\n"
        "[ROLE: Especialista sênior em Frontend e acessibilidade]\n"
        "Você cria código limpo, sem mocks, com foco em simplicidade e robustez.\n"
        "```\n",
        encoding="utf-8",
    )
    (ws_path / "README.md").write_text("# Test Repo\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=str(ws_path), check=True)
    subprocess.run(["git", "commit", "-m", "Initial commit"], cwd=str(ws_path), check=True)

    try:
        # Carrega engine para score real se possível
        engine = OpenJevEngine(model_id_or_path="convaiinnovations/laya", allow_cpu_fallback=True)
        engine.load_model()
        runner = SubagentRunner(engine=engine)

        req = SpawnSubagentRequest(
            persona="lumi",
            task="Crie um arquivo math_utils.py com uma função clamp(value, min_val, max_val) devidamente tipada e documentada.",
            workspace=str(ws_path),
            auto_validate=True,
        )

        resp = await runner.spawn(req)

        print("\n" + "="*70)
        print(f"SUBAGENTE: {resp.persona}")
        print(f"SUCESSO: {resp.success}")
        print(f"ARQUIVOS MODIFICADOS: {resp.files_modified}")
        print(f"AST BLOQUEADA: {resp.ast_blocked}")
        print(f"SCORE JEV: {resp.score}")
        print(f"STATUS APROVADO: {resp.passed}")
        print(f"TEMPO DE EXECUÇÃO: {resp.execution_time_ms} ms")
        print("="*70)
        print("\nRESUMO DO SUBAGENTE (HERMES + DEEPSEEK):\n")
        print(resp.summary)
        print("="*70)

        # Inspeciona o arquivo que o DeepSeek gerou
        math_file = ws_path / "math_utils.py"
        if math_file.exists():
            print("\nCONTEÚDO DO ARQUIVO GERADO PELO DEEPSEEK:")
            print(math_file.read_text(encoding="utf-8"))
        else:
            print(f"\nArquivos presentes no workspace: {[p.name for p in ws_path.iterdir()]}")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    asyncio.run(main())
