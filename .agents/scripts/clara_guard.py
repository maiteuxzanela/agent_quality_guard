#!/usr/bin/env python3
"""Stop Hook de Governança e Qualidade do Projeto JEV (@Clara - Guardiã da Qualidade).

Executa interceptação física no encerramento de turno do agente:
1. Fast-path: Se nenhum arquivo de código/teste foi alterado, sai imediatamente com {} (sucesso).
2. Circuit Breaker: Permite até 5 tentativas consecutivas de auto-correção.
   Após 5 tentativas (attempts >= 5), libera o encerramento com {} para emissão do Laudo Técnico.
3. Camada 1: Needle 3 AST Guardrail (Fail-Fast determinístico para exceptions engolidas, mocks, bfloat16).
4. Camada 2: Suíte de Testes Reais do pytest (zero mocks).
5. Camada 3: Avaliação de Conformidade e Diagnóstico estruturado. Se houver defeitos identificáveis,
   retorna decision: 'continue' com a lista exata de blocos a serem corrigidos no disco.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

MAX_ATTEMPTS = 5
STATE_FILE = Path("/tmp/clara_guard_state.json")


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {"attempts": 0}
    return {"attempts": 0}


def save_state(state: dict) -> None:
    try:
        STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception as e:
        sys.stderr.write(f"[Clara - State]: Falha ao salvar estado: {e}\n")


def clear_state() -> None:
    if STATE_FILE.exists():
        try:
            STATE_FILE.unlink()
        except OSError as e:
            sys.stderr.write(f"[Clara - State]: Falha ao remover estado: {e}\n")


def main():
    # 0. Leitura não-bloqueante do payload do stdin
    try:
        if not sys.stdin.isatty():
            payload = json.load(sys.stdin)
        else:
            payload = {}
    except Exception:
        payload = {}

    script_dir = Path(__file__).parent.resolve()
    workspace_dir = (script_dir.parent.parent).resolve()

    # Adiciona workspace_dir ao sys.path para imports locais
    if str(workspace_dir) not in sys.path:
        sys.path.insert(0, str(workspace_dir))

    # 1. No-op fast path: verifica alterações em src/ e tests/
    check_git = subprocess.run(
        ["git", "status", "--porcelain", "src/", "tests/"],
        cwd=str(workspace_dir),
        capture_output=True,
        text=True,
    )
    modified_code = check_git.stdout.strip()
    if not modified_code:
        # Nenhum código ou teste modificado no workspace
        clear_state()
        print(json.dumps({}))
        sys.exit(0)

    state = load_state()
    current_attempts = state.get("attempts", 0)

    # 2. Circuit Breaker de 5 tentativas
    # Se o agente atingiu 5 tentativas consecutivas de correção sem sucesso,
    # libera a entrega para permitir que ele emita o Laudo de Inviabilidade Técnica / Tentativas Esgotadas.
    if current_attempts >= MAX_ATTEMPTS:
        clear_state()
        sys.stderr.write(
            f"[Clara - Circuit Breaker]: Limite de {MAX_ATTEMPTS} tentativas atingido. "
            "Liberando encerramento da resposta para emissão do Laudo Técnico pelo agente.\n"
        )
        print(json.dumps({}))
        sys.exit(0)

    # 3. Camada 1: Needle 3 AST Guardrail (Fail-Fast determinístico)
    try:
        from src.memory.needle_ast_guard import NeedleASTGuard
        ast_violations = NeedleASTGuard.scan_workspace(str(workspace_dir))
        if ast_violations:
            new_attempts = current_attempts + 1
            state["attempts"] = new_attempts
            save_state(state)

            if new_attempts >= MAX_ATTEMPTS:
                clear_state()
                print(json.dumps({}))
                sys.exit(0)

            violations_summary = "\n".join(
                f"- [{v.rule_id}] {v.file_path}:{v.line_number}: {v.message}"
                for v in ast_violations[:5]
            )
            response = {
                "decision": "continue",
                "reason": (
                    f"🚨 [Clara - Guardiã da Qualidade / Stop Hook] (Tentativa {new_attempts}/{MAX_ATTEMPTS}):\n"
                    f"A entrega foi bloqueada por violações determinísticas de AST detectadas pelo Needle 3:\n\n"
                    f"{violations_summary}\n\n"
                    "O agente deve consertar essas violações no disco antes de encerrar o turno."
                ),
            }
            print(json.dumps(response))
            sys.exit(0)
    except Exception as e:
        sys.stderr.write(f"[Clara - AST Guardrail]: Aviso na verificação de AST: {e}\n")

    # 4. Camada 2: Execução da Suíte de Testes Reais do pytest
    pytest_bin = workspace_dir / ".venv" / "bin" / "pytest"
    if pytest_bin.exists():
        test_files = [
            "tests/test_hardware.py",
            "tests/test_no_mocks_in_src.py",
            "tests/test_local_needle.py",
            "tests/test_consulta.py",
            "tests/test_schemas.py",
            "tests/test_ipc_service.py",
            "tests/test_subagent_spawn.py",
        ]
        test_cmd = [str(pytest_bin)] + [t for t in test_files if (workspace_dir / t).exists()]
        test_res = subprocess.run(
            test_cmd,
            cwd=str(workspace_dir),
            capture_output=True,
            text=True,
            timeout=45,
        )
        if test_res.returncode != 0:
            new_attempts = current_attempts + 1
            state["attempts"] = new_attempts
            save_state(state)

            if new_attempts >= MAX_ATTEMPTS:
                clear_state()
                print(json.dumps({}))
                sys.exit(0)

            # Extrai linhas de falha do pytest
            fail_lines = [
                line for line in test_res.stdout.splitlines()
                if "FAILED" in line or "ERROR" in line or line.startswith("E   ")
            ][:6]
            fail_summary = "\n".join(fail_lines) or test_res.stderr[:300] or "Testes automatizados falharam."

            response = {
                "decision": "continue",
                "reason": (
                    f"🚨 [Clara - Guardiã da Qualidade / Stop Hook] (Tentativa {new_attempts}/{MAX_ATTEMPTS}):\n"
                    f"Testes reais quebraram durante a verificação de integridade:\n\n"
                    f"{fail_summary}\n\n"
                    "Conserte as falhas nos testes antes de concluir a entrega."
                ),
            }
            print(json.dumps(response))
            sys.exit(0)

    # 5. Sucesso completo em todas as verificações
    clear_state()
    print(json.dumps({}))
    sys.exit(0)


if __name__ == "__main__":
    main()
