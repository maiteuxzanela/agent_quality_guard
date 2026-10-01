/**
 * Visual QA Actor & Natural Language Web Inspector (Open JEV + Laya + Chrome CDP)
 * 
 * Executa comandos em linguagem natural em qualquer site/web app resolvendo
 * os elementos do DOM via Laya Choice, capturando evidências visuais a cada passo.
 */

const { spawn } = require('child_process');
const fs = require('fs');
const path = require('path');
const http = require('http');

async function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

async function callLayaChoice(premise, optionsMap) {
  return new Promise((resolve, reject) => {
    const pythonProc = spawn('/home/maiteuxzanela/projeto JEV/.venv/bin/python3', [
      '-c',
      `
import sys, json, asyncio
sys.path.insert(0, '/home/maiteuxzanela/projeto JEV')
from src.decision.engine import OpenJevEngine
from src.core.schemas import ChoiceRequest

engine = OpenJevEngine(model_id_or_path='convaiinnovations/laya', allow_cpu_fallback=True)
engine.load_model()

premise = sys.argv[1]
criteria = json.loads(sys.argv[2])
options = list(criteria.keys())

req = ChoiceRequest(premise=premise, options=options, criteria=criteria)
res = asyncio.run(engine.evaluate_choice(req))

print(json.dumps({
  "selected": res.selected,
  "confidence": res.top_confidence,
  "probabilities": res.probabilities
}))
      `,
      premise,
      JSON.stringify(optionsMap)
    ]);

    let stdout = '';
    let stderr = '';
    pythonProc.stdout.on('data', d => stdout += d.toString());
    pythonProc.stderr.on('data', d => stderr += d.toString());

    pythonProc.on('close', code => {
      if (code === 0) {
        try {
          const lines = stdout.trim().split('\n');
          const jsonLine = lines[lines.length - 1];
          resolve(JSON.parse(jsonLine));
        } catch (e) {
          reject(new Error(`Falha ao parsear JSON do Laya: ${stdout}`));
        }
      } else {
        reject(new Error(`Laya Choice falhou com código ${code}: ${stderr}`));
      }
    });
  });
}

class VisualQAActor {
  constructor(options = {}) {
    this.url = options.url || 'http://127.0.0.1:8080/index.html';
    this.port = options.port || 9222;
    this.screenshotsDir = options.screenshotsDir || path.join(__dirname, '..', '..', '..', 'deepseek harness', 'screenshots');
    this.delayMs = options.delayMs || 2000;
  }

  async runCommands(commandsList) {
    if (!fs.existsSync(this.screenshotsDir)) {
      fs.mkdirSync(this.screenshotsDir, { recursive: true });
    }

    const chromeProc = spawn('/usr/bin/google-chrome', [
      '--headless=new',
      '--disable-gpu',
      `--remote-debugging-port=${this.port}`,
      '--user-data-dir=/tmp/chrome_cdp_visual_qa',
      '--window-size=1280,900',
      this.url
    ], { stdio: 'ignore' });

    await sleep(1500);

    const res = await fetch(`http://127.0.0.1:${this.port}/json/list`);
    const targets = await res.json();
    const target = targets.find(t => t.type === 'page') || targets[0];
    const ws = new WebSocket(target.webSocketDebuggerUrl);

    let msgId = 1;
    const pending = new Map();

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      if (data.id && pending.has(data.id)) {
        pending.get(data.id)(data);
        pending.delete(data.id);
      }
    };

    const send = (method, params = {}) => new Promise(resolve => {
      const id = msgId++;
      pending.set(id, resolve);
      ws.send(JSON.stringify({ id, method, params }));
    });

    await new Promise(r => ws.onopen = r);

    await send('Page.enable');
    await send('DOM.enable');
    await send('Runtime.enable');

    console.log(`🌐 1. Navegando para ${this.url}...`);
    await send('Page.navigate', { url: this.url });
    await sleep(2000);

    const report = [];

    for (let stepIdx = 0; stepIdx < commandsList.length; stepIdx++) {
      const command = commandsList[stepIdx];
      console.log(`\n🤖 [Passo ${stepIdx + 1}/${commandsList.length}] Processando instrução: "${command}"`);

      const domScan = await send('Runtime.evaluate', {
        expression: `
          (() => {
            const elements = Array.from(document.querySelectorAll('button, a, input, select, textarea, [data-action], [id]'));
            const map = {};
            elements.forEach((el, idx) => {
              const text = (el.innerText || el.placeholder || el.getAttribute('aria-label') || el.value || el.id || el.className || '').trim();
              if (text && el.offsetWidth > 0 && el.offsetHeight > 0) {
                const selector = el.id ? '#' + el.id : (el.getAttribute('data-id') ? '[data-id="' + el.getAttribute('data-id') + '"]' : (el.className ? '.' + el.className.split(' ')[0] : el.tagName.toLowerCase()));
                map['elem_' + idx] = {
                  text: text.slice(0, 100),
                  tag: el.tagName.toLowerCase(),
                  id: el.id || null,
                  dataId: el.getAttribute('data-id') || null,
                  selector: selector,
                  description: el.tagName.toLowerCase() + ' (' + text.slice(0, 60) + ')' + (el.id ? ' #' + el.id : '')
                };
              }
            });
            return map;
          })()
        `,
        returnByValue: true
      });

      const elementsMap = domScan.result.value;
      const optionsCriteria = {};
      for (const [k, v] of Object.entries(elementsMap)) {
        optionsCriteria[k] = v.description;
      }

      if (Object.keys(optionsCriteria).length === 0) {
        console.log('⚠️ Nenhum elemento interativo encontrado no DOM nesta etapa.');
        continue;
      }

      console.log('🧠 Resolvendo elemento no Laya via Choice...');
      const choiceResult = await callLayaChoice(
        `Comando em Linguagem Natural do Usuário: "${command}"`,
        optionsCriteria
      );

      const targetKey = choiceResult.selected;
      const targetElem = elementsMap[targetKey];

      console.log(`🎯 Elemento Eleito pelo Laya: ${targetKey} -> "${targetElem.description}" (Confiança: ${(choiceResult.confidence * 100).toFixed(1)}%)`);

      const isInput = targetElem.tag === 'input' || targetElem.tag === 'textarea';
      const textMatch = command.match(/(?:digite|preencha|digitar|digita|cep)\s+([0-9A-Za-z\-\@\.]+)/i);

      if (isInput && textMatch) {
        const textToType = textMatch[1];
        console.log(`⌨️ Digitando "${textToType}" no campo "${targetElem.description}"...`);
        await send('Runtime.evaluate', {
          expression: `
            (() => {
              const el = document.querySelector('${targetElem.selector}');
              if (el) {
                el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                el.focus();
                el.value = ${JSON.stringify(textToType)};
                el.dispatchEvent(new Event('input', { bubbles: true }));
                el.dispatchEvent(new Event('change', { bubbles: true }));
                el.dispatchEvent(new KeyboardEvent('keypress', { key: 'Enter', bubbles: true }));
              }
            })()
          `
        });
      } else {
        console.log(`🖱️ Clicando no elemento "${targetElem.description}"...`);
        await send('Runtime.evaluate', {
          expression: `
            (() => {
              const el = document.querySelector('${targetElem.selector}');
              if (el) {
                el.scrollIntoView({ behavior: 'smooth', block: 'center' });
                el.style.outline = '4px solid #ff2a6d';
                el.click();
              }
            })()
          `
        });
      }

      await sleep(this.delayMs);

      const shot = await send('Page.captureScreenshot', { format: 'png' });
      const shotName = `passo_${stepIdx + 1}_${targetElem.id || 'elem'}.png`;
      const shotPath = path.join(this.screenshotsDir, shotName);
      fs.writeFileSync(shotPath, Buffer.from(shot.result.data, 'base64'));

      report.push({
        step: stepIdx + 1,
        command: command,
        target: targetElem.description,
        confidence: choiceResult.confidence,
        screenshot: shotPath
      });

      console.log(`📸 Evidência gerada: screenshots/${shotName}`);
    }

    ws.close();
    chromeProc.kill();
    console.log('\n✅ Execução de todas as instruções concluída!');
    return report;
  }
}

module.exports = VisualQAActor;
