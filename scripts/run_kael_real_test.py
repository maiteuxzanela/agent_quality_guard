"""Script de teste real: Despacho do subagente @Kael com contexto isolado."""

import asyncio
import json
from pathlib import Path

from src.core.schemas import SpawnSubagentRequest
from src.orchestrator.subagent_runner import SubagentRunner


async def main():
    print(">>> Iniciando despacho do subagente real: @Kael (ML & Hardware Pascal)")
    runner = SubagentRunner()

    req = SpawnSubagentRequest(
        persona="kael",
        task="Conferir o estado atual da placa de vídeo (GTX 1050 Ti Pascal GP107), incluindo VRAM utilizada, temperatura, driver e compatibilidade de tipos",
        workspace=str(Path("/home/maiteuxzanela/projeto JEV").resolve()),
        auto_validate=True,
    )

    resp = await runner.spawn(req)

    print("\n" + "="*70)
    print(f"SUBAGENTE: {resp.persona}")
    print(f"SUCESSO: {resp.success}")
    print(f"TEMPO DE EXECUÇÃO: {resp.execution_time_ms} ms")
    print(f"TOKENS DE CONTEXTO ISOLADO: {resp.context_tokens_used}")
    print("="*70)
    print("\nRELATÓRIO ENTREGUE:\n")
    print(resp.summary)
    print("="*70 + "\n")


if __name__ == "__main__":
    asyncio.run(main())
