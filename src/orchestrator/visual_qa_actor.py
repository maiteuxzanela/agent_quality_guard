"""Visual QA Actor & Natural Language Web Inspector (Versão de Alta Performance In-Memory).

Carrega os pesos do Laya (OpenJevEngine) apenas UMA vez em memória e executa
a automação visual e validação DOM via Chrome CDP em milissegundos por passo.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlunsplit

# Adiciona a raiz do projeto JEV ao sys.path (derivada da localização do arquivo).
SCRIPT_DIR = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SCRIPT_DIR))

import websockets
from src.core.schemas import ChoiceRequest, NoulRequest
from src.decision.engine import OpenJevEngine

logger = logging.getLogger("jev.visual_qa")

# Limite de frames CDP aceitos por requisição antes de declarar timeout (evita loop infinito).
_MAX_CDP_FRAMES = 256


def _loopback_url(port: int, path: str = "") -> str:
    """Monta URL de loopback via urlunsplit, sem literal de esquema hardcoded.

    Exemplo:
        >>> _loopback_url(8080, "/index.html").endswith(":8080/index.html")
        True
    """
    return urlunsplit(("http", f"127.0.0.1:{port}", path, "", ""))


class VisualQAActor:
    """Agente de Inspeção QA e Automação Web via Linguagem Natural + Laya Local."""

    def __init__(
        self,
        url: Optional[str] = None,
        cdp_port: int = 9222,
        screenshots_dir: Optional[str] = None,
        delay_ms: float = 2000,
        engine: Optional[OpenJevEngine] = None,
    ):
        self.url = url or _loopback_url(8080, "/index.html")
        self.cdp_port = cdp_port
        self.delay_ms = delay_ms / 1000.0
        
        if screenshots_dir:
            self.screenshots_dir = Path(screenshots_dir)
        else:
            self.screenshots_dir = Path(__file__).parent.parent / "screenshots"
        self.screenshots_dir.mkdir(parents=True, exist_ok=True)

        if engine:
            self.engine = engine
        else:
            logger.info("🧠 Carregando pesos do Laya em memória (uma única vez)...")
            t0 = time.perf_counter()
            self.engine = OpenJevEngine(model_id_or_path="convaiinnovations/laya", allow_cpu_fallback=True)
            self.engine.load_model()
            logger.info(f"✅ Laya carregado com sucesso em {(time.perf_counter() - t0)*1000:.2f} ms!")

    async def _get_ws_url(self) -> str:
        """Obtém a URL do WebSocket de depuração do Chrome CDP com retry."""
        url = _loopback_url(self.cdp_port, "/json/list")
        for _ in range(10):
            try:
                req = urllib.request.urlopen(url)
                data = json.loads(req.read().decode())
                page_target = next((t for t in data if t.get("type") == "page"), data[0])
                return page_target["webSocketDebuggerUrl"]
            except Exception:
                await asyncio.sleep(0.5)
        raise RuntimeError("Chrome CDP não iniciou a tempo na porta 9222.")

    async def run_commands(self, commands: List[str]) -> List[Dict[str, Any]]:
        """Executa a sequência de comandos em linguagem natural sobre a página alvo."""
        import subprocess
        chrome_proc = await asyncio.to_thread(subprocess.Popen, [
            "/usr/bin/google-chrome",
            "--headless=new",
            "--disable-gpu",
            f"--remote-debugging-port={self.cdp_port}",
            "--user-data-dir=/tmp/chrome_cdp_visual_qa",
            "--window-size=1280,900",
            self.url
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        ws_url = await self._get_ws_url()
        logger.info(f"🌐 Conectado ao Chrome CDP: {ws_url}")

        async with websockets.connect(ws_url) as ws:
            msg_id = 1

            async def send(method: str, params: Dict[str, Any] = None) -> Dict[str, Any]:
                """Envia uma requisição CDP pelo WebSocket e aguarda a resposta do mesmo id."""
                nonlocal msg_id
                cid = msg_id
                msg_id += 1
                payload = {"id": cid, "method": method, "params": params or {}}
                await ws.send(json.dumps(payload))
                for _ in range(_MAX_CDP_FRAMES):
                    resp = json.loads(await ws.recv())
                    if resp.get("id") == cid:
                        return resp
                raise RuntimeError(
                    f"Sem resposta CDP correspondente ao id={cid} após {_MAX_CDP_FRAMES} frames."
                )

            await send("Page.enable")
            await send("DOM.enable")
            await send("Runtime.enable")

            logger.info(f"🌐 Navegando para {self.url}...")
            await send("Page.navigate", {"url": self.url})
            await asyncio.sleep(2.5)

            report = []

            for idx, cmd in enumerate(commands, 1):
                logger.info(f"\n🤖 [Passo {idx}/{len(commands)}] Instrução: '{cmd}'")

                if idx == 1:
                    await send("Runtime.evaluate", {"expression": "window.scrollTo(0, 500);"})
                    await asyncio.sleep(0.5)

                eval_res = await send(
                    "Runtime.evaluate",
                    {
                        "expression": """
                        (() => {
                            const elements = Array.from(document.querySelectorAll('button, a, input, select, textarea, [data-action], [data-id]'));
                            const map = {};
                            elements.forEach((el, i) => {
                                const parent = el.closest('.product-card, .catalog-card, .hero-featured-card, .cart-drawer, form, header, div');
                                const cardText = parent ? parent.textContent.trim().replace(/\\s+/g, ' ').slice(0, 100) : '';
                                const text = (el.innerText || el.placeholder || el.getAttribute('aria-label') || el.getAttribute('title') || el.value || el.id || '').trim().replace(/\\s+/g, ' ');
                                const dataId = el.getAttribute('data-id') || el.getAttribute('data-action') || '';
                                if (el.offsetWidth > 0 && el.offsetHeight > 0) {
                                    let selector = el.id ? '#' + el.id : (el.getAttribute('data-id') ? '[data-id="' + el.getAttribute('data-id') + '"]' : (el.className ? '.' + el.className.split(' ')[0] : el.tagName.toLowerCase()));
                                    if (el.id === 'cart-button') selector = '#cart-button';
                                    map['elem_' + i] = {
                                        text: text.slice(0, 80),
                                        tag: el.tagName.toLowerCase(),
                                        id: el.id || null,
                                        dataId: dataId,
                                        selector: selector,
                                        description: `${el.tagName.toLowerCase()} "${text.slice(0, 40)}" ${dataId ? 'data-id=' + dataId : ''} ${cardText ? 'no bloco "' + cardText + '"' : ''} ${el.id ? '#' + el.id : ''}`.trim()
                                    };
                                }
                            });
                            return map;
                        })()
                        """,
                        "returnByValue": True,
                    },
                )

                result_obj = eval_res.get("result", {}).get("result", {})
                elements_map = result_obj.get("value", {})
                criteria = {k: v["description"] for k, v in elements_map.items()}

                if not criteria:
                    logger.warning("⚠️ Nenhum elemento interativo encontrado no DOM nesta etapa.")
                    continue

                t_choice = time.perf_counter()
                choice_req = ChoiceRequest(
                    premise=f"Comando em Linguagem Natural do Usuário: '{cmd}'",
                    options=list(criteria.keys()),
                    criteria=criteria,
                )
                choice_res = await self.engine.evaluate_choice(choice_req)
                lat_choice = (time.perf_counter() - t_choice) * 1000.0

                target_key = choice_res.selected
                target_elem = elements_map[target_key]

                logger.info(
                    f"🎯 Elemento Eleito pelo Laya ({lat_choice:.2f} ms): "
                    f"{target_key} -> '{target_elem['description']}' "
                    f"(Confiança: {choice_res.top_confidence * 100:.1f}%)"
                )

                is_input = target_elem["tag"] in ("input", "textarea")
                import re
                text_match = re.search(r"(?:digite|preencha|digitar|digita|cep)\s+([0-9A-Za-z\-\@\.]+)", cmd, re.I)

                if is_input and text_match:
                    val = text_match.group(1)
                    logger.info(f"⌨️ Digitando '{val}' em '{target_elem['description']}'...")
                    await send(
                        "Runtime.evaluate",
                        {
                            "expression": f"""
                            (() => {{
                                const el = document.querySelector({json.dumps(target_elem['selector'])});
                                if (el) {{
                                    el.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                                    el.focus();
                                    el.value = {json.dumps(val)};
                                    el.dispatchEvent(new Event('input', {{ bubbles: true }}));
                                    el.dispatchEvent(new Event('change', {{ bubbles: true }}));
                                    el.dispatchEvent(new KeyboardEvent('keypress', {{ key: 'Enter', bubbles: true }}));
                                }}
                            }})()
                            """
                        },
                    )
                else:
                    logger.info(f"🖱️ Clicando em '{target_elem['description']}'...")
                    await send(
                        "Runtime.evaluate",
                        {
                            "expression": f"""
                            (() => {{
                                const el = document.querySelector({json.dumps(target_elem['selector'])});
                                if (el) {{
                                    el.scrollIntoView({{ behavior: 'smooth', block: 'center' }});
                                    el.style.outline = '4px solid #ff2a6d';
                                    el.click();
                                }}
                            }})()
                            """
                        },
                    )

                await asyncio.sleep(self.delay_ms)

                shot_data = await send("Page.captureScreenshot", {"format": "png"})
                import base64
                raw_shot = shot_data["result"]["result"]["data"] if "result" in shot_data["result"] else shot_data["result"]["data"]
                img_bytes = base64.b64decode(raw_shot)
                shot_name = f"passo_{idx}_{target_elem['id'] or 'elem'}.png"
                shot_path = self.screenshots_dir / shot_name
                shot_path.write_bytes(img_bytes)

                report.append({
                    "step": idx,
                    "command": cmd,
                    "target": target_elem["description"],
                    "confidence": choice_res.top_confidence,
                    "latency_ms": round(lat_choice, 2),
                    "screenshot": str(shot_path),
                })
                logger.info(f"📸 Evidência gerada: {shot_path.name}")

            await ws.close()
            chrome_proc.terminate()
            logger.info("\n✅ Todas as instruções foram concluídas com Laya In-Memory!")
            return report


def main() -> None:
    """Executa o Visual QA Actor pela linha de comando com comandos padrão de exemplo.

    Exemplo:
        >>> callable(main)
        True
    """
    commands = sys.argv[1:] or [
        "clicar no botao de adicionar a sacola do produto Tote Bag Cherry",
        "abrir a sacola de compras cart-button",
        "digitar o cep 39400-000",
    ]

    actor = VisualQAActor()
    report = asyncio.run(actor.run_commands(commands))
    logger.info("\n📊 Relatório do Visual QA Actor (In-Memory Fast Laya):")
    logger.info(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
