#!/usr/bin/env node
/**
 * agent-quality-guard — Executável universal do servidor MCP Quality Gate determinístico.
 *
 * Fala JSON-RPC sobre stdio, integrando nativamente com Claude Desktop,
 * Cursor, OpenCode, Windsurf e qualquer cliente compatível com MCP.
 *
 * Uso:
 *   npx -y agent-quality-guard
 *   bunx agent-quality-guard
 */
import { spawn } from "child_process";
import path from "path";
import { fileURLToPath } from "url";
import fs from "fs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");
const srcDir = path.join(rootDir, "src");

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

const pythonBin = findPython();
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
