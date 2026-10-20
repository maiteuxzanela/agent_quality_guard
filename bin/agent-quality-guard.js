#!/usr/bin/env node
/**
 * agent-quality-guard — Executável universal do servidor MCP Quality Gate determinístico.
 *
 * Fala JSON-RPC sobre stdio, integrando nativamente com Claude Desktop,
 * Cursor, OpenCode, Windsurf e qualquer cliente compatível com MCP.
 *
 * Blindagem pré-spawn (TICKET-SGA-03): antes de spawnar o Python, valida-se
 * de forma determinística (1) versão do interpretador >= 3.10 e (2) presença
 * dos módulos obrigatórios `mcp` e `pydantic`, emitindo diagnóstico amigável
 * quando o host não está apto — em vez de um stacktrace opaco vindo do filho.
 *
 * Uso:
 *   npx -y agent-quality-guard
 *   bunx agent-quality-guard
 */
import { spawn, spawnSync } from "child_process";
import path from "path";
import { fileURLToPath } from "url";
import fs from "fs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");
const srcDir = path.join(rootDir, "agent_quality_guard");

// Identificar executável Python disponível no host
const pythonCandidates = [
  process.env.PYTHON,
  path.join(rootDir, ".venv", "bin", "python3"),
  path.join(rootDir, ".venv", "bin", "python"),
  "python3",
  "python"
].filter(Boolean);

function findPython() {
  for (const py of pythonCandidates) {
    if (py.includes(path.sep)) {
      if (fs.existsSync(py)) return py;
    } else {
      return py;
    }
  }
  return "python3";
}

/**
 * Valida o interpretador Python antes do spawn (fail-fast com mensagem amigável).
 * Retorna true se apto; caso contrário imprime o diagnóstico e retorna false.
 */
function preflightPython(pythonBin) {
  // 1) Versão >= 3.10 (requires-python do pyproject.toml).
  const versionCheck = spawnSync(
    pythonBin,
    ["-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"],
    { stdio: ["ignore", "ignore", "pipe"], encoding: "utf8" }
  );

  if (versionCheck.error) {
    console.error(
      `[agent-quality-guard] Não foi possível executar o Python em "${pythonBin}": ${versionCheck.error.message}\n` +
      "Instale o Python >= 3.10 (https://www.python.org/downloads/) ou aponte a variável de ambiente PYTHON para um interpretador apto."
    );
    return false;
  }

  if (versionCheck.status !== 0) {
    const versionLine = spawnSync(pythonBin, ["--version"], { encoding: "utf8" })
      .stdout?.trim() || "versão desconhecida";
    console.error(
      `[agent-quality-guard] Python >= 3.10 é obrigatório, mas "${pythonBin}" (${versionLine}) é mais antigo.\n` +
      "Atualize o interpretador (https://www.python.org/downloads/) ou crie um virtualenv com Python 3.10+ e aponte PYTHON para ele:\n" +
      "  python3.10 -m venv .venv && export PYTHON=\"$PWD/.venv/bin/python3\""
    );
    return false;
  }

  // 2) Módulos obrigatórios (mcp, pydantic) no ambiente do interpretador resolvido.
  const moduleCheck = spawnSync(
    pythonBin,
    ["-c", "import importlib.util, sys; missing = [m for m in ('mcp', 'pydantic') if importlib.util.find_spec(m) is None]; sys.exit(1 if missing else 0)"],
    { stdio: ["ignore", "ignore", "pipe"], encoding: "utf8" }
  );

  if (moduleCheck.error || moduleCheck.status !== 0) {
    console.error(
      `[agent-quality-guard] Dependências obrigatórias (mcp, pydantic) ausentes no interpretador "${pythonBin}".\n` +
      "Instale o pacote e suas dependências com:\n" +
      "  pip install agent-quality-guard-mcp\n" +
      "ou, a partir do checkout local:\n" +
      "  pip install .\n" +
      "Recomenda-se virtualenv para isolar o ambiente:\n" +
      "  python3 -m venv .venv && .venv/bin/pip install ."
    );
    return false;
  }

  return true;
}

const pythonBin = findPython();

if (!preflightPython(pythonBin)) {
  process.exit(1);
}

const env = {
  ...process.env,
  PYTHONPATH: process.env.PYTHONPATH
    ? `${rootDir}${path.delimiter}${srcDir}${path.delimiter}${process.env.PYTHONPATH}`
    : `${rootDir}${path.delimiter}${srcDir}`
};

const child = spawn(pythonBin, [path.join(srcDir, "mcp_server.py")], {
  stdio: "inherit",
  env
});

child.on("error", (err) => {
  console.error("[agent-quality-guard] Falha ao iniciar processo Python:", err.message);
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
  } else {
    process.exit(code ?? 0);
  }
});

process.on("SIGINT", () => child.kill("SIGINT"));
process.on("SIGTERM", () => child.kill("SIGTERM"));
