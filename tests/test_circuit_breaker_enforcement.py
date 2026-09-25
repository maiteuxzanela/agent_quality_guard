"""Testes reais e sem mocks para a Trava Inegociável do Circuit Breaker (5 Tentativas).

Valida que:
1. O contador de tentativas acumula progressivamente mesmo quando o código é alterado no disco.
2. Na 5ª tentativa consecutiva sem aprovação (attempts >= 5), a Saída de Emergência é obrigatoriamente destravada (emergency_exit_unlocked=True).
3. Qualquer tentativa subsequente (6ª+) é sumariamente bloqueada no guardrail de entrada sem gastar novas inferências.
"""

import os
import subprocess
import tempfile
import pytest
from pathlib import Path

from src.mcp_server import openjev_score_and_diagnose
from src.core.score_gate import history_get, history_clear


@pytest.mark.asyncio
async def test_circuit_breaker_locks_at_max_attempts():
    with tempfile.TemporaryDirectory(prefix="jev_circuit_test_") as tmp_dir:
        ws = Path(tmp_dir).resolve()
        
        # Inicializa repositório git real para teste físico de estados
        subprocess.run(["git", "init"], cwd=str(ws), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=str(ws), capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=str(ws), capture_output=True, check=True)
        
        # Arquivo inicial commitado
        f_main = ws / "main.py"
        f_main.write_text("def hello(): pass\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=str(ws), capture_output=True, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=str(ws), capture_output=True, check=True)
        
        # Limpa qualquer resíduo anterior para o workspace
        history_clear()

        # Simula 5 tentativas consecutivas com alterações reais no código do disco
        premise = "Pedido do Prompt: Implementar lógica de pagamento com gateway seguro | Resultado Executado: Falha de implementação sem integração real nem testes."
        
        for attempt in range(1, 6):
            # Modifica o código no disco a cada tentativa para gerar novo hash de git.
            # A entrega precisa de SUSPEITA ESTRUTURAL real: sob o contrato do
            # Screener + Noul, nota baixa sem suspeita aprova com ressalva e o
            # circuit breaker nunca seria exercitado.
            f_main.write_text(
                "def hello():\n"
                "    # TODO: tratar erro de leitura do arquivo\n"
                '    arquivo = open("dados.txt")\n'
                f"    return {attempt}  # tentativa {attempt}\n",
                encoding="utf-8",
            )
            
            res = await openjev_score_and_diagnose(
                premise=premise,
                workspace=str(ws),
                task_type="codigo_producao",
                threshold=3.40,
                reset_history=False,
            )
            
            # Nas 4 primeiras tentativas, a saída de emergência NÃO deve estar destravada
            if attempt < 5:
                assert res.get("passed") is False
                assert res.get("attempts") == attempt
                assert res.get("emergency_exit_unlocked") is False
            else:
                # Na 5ª tentativa, a Saída de Emergência DEVE ESTAR OBRIGATORIAMENTE DESBLOQUEADA!
                assert res.get("passed") is False
                assert res.get("attempts") == 5
                assert res.get("emergency_exit_unlocked") is True
                assert "SAÍDA DE EMERGÊNCIA DESBLOQUEADA" in str(res.get("message") or "")

        # 6ª tentativa: Mesmo com nova alteração de código, o guardrail de entrada DEVE TRAVAR IMEDIATAMENTE!
        f_main.write_text("def hello(): return 6  # tentativa extra 6\n", encoding="utf-8")
        res_6 = await openjev_score_and_diagnose(
            premise=premise,
            workspace=str(ws),
            task_type="codigo_producao",
            threshold=3.40,
            reset_history=False,
        )
        assert res_6.get("emergency_exit_unlocked") is True
        assert res_6.get("attempts") == 5
        assert "SAÍDA DE EMERGÊNCIA DESBLOQUEADA" in str(res_6.get("message") or "")
