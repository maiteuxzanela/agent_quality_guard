#!/usr/bin/env node
/**
 * Runner determinístico de testes para `npm test`.
 *
 * Resolve o interpretador Python do projeto com prioridade no virtualenv
 * (.venv/bin primeiro, fallback para o PATH/CI) e executa o subconjunto de
 * testes do Quality Gate com JEV_USE_HERMES=false fixado.
 * Mesmo padrão de resolução de bin/agent-quality-guard.js e
 * agent_quality_guard/orchestrator/pytest_chunk_runner.py.
 */
import { spawn, spawnSync } from "child_process";
import path from "path";
import { fileURLToPath } from "url";
import fs from "fs";

const __dirname = path.dirname(fileURLToPath(import.meta.url));
const rootDir = path.resolve(__dirname, "..");

// Subconjunto determinístico do gate (inalterado — sem skip/xfail).
const testFiles = [
  "tests/test_agent_quality_guard.py",
  "tests/test_tree_sitter_guard.py",
  "tests/test_needle_ast_guard.py",
];

// Venv-first: .venv/bin antes do PATH/CI (pytest_chunk_runner.py:295-298).
const pythonCandidates = [
  process.env.PYTHON,
  path.join(rootDir, ".venv", "bin", "python3"),
  path.join(rootDir, ".venv", "bin", "python"),
  "python3",
  "python",
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

// Blindagem pré-spawn (TICKET-SGA-03): a suíte determinística exige
// Python >= 3.10 (requires-python do pyproject.toml). Falha amigável
// em vez de SyntaxError opaco vindo do pytest no filho.
const versionCheck = spawnSync(
  pythonBin,
  ["-c", "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)"],
  { stdio: ["ignore", "ignore", "pipe"], encoding: "utf8" }
);
if (versionCheck.error || versionCheck.status !== 0) {
  const versionLine = versionCheck.error
    ? "indisponível"
    : (spawnSync(pythonBin, ["--version"], { encoding: "utf8" }).stdout?.trim() || "versão desconhecida");
  console.error(
    `[agent-quality-guard] Python >= 3.10 é obrigatório para rodar a suíte, mas "${pythonBin}" (${versionLine}) não atende.\n` +
    "Atualize o interpretador ou aponte PYTHON para um virtualenv Python 3.10+:\n" +
    "  python3.10 -m venv .venv && export PYTHON=\"$PWD/.venv/bin/python3\""
  );
  process.exit(1);
}

const env = {
  ...process.env,
  // Ressalva do SDET: fora do CI, rodar sempre com JEV_USE_HERMES=false.
  JEV_USE_HERMES: "false",
};

console.error(
  `[agent-quality-guard] test runner: ${pythonBin} (JEV_USE_HERMES=${env.JEV_USE_HERMES})`
);

// `-m pytest` garante o pytest do interpretador resolvido (venv), não o
// binário solto do PATH do host (que pode faltar tree_sitter).
const child = spawn(pythonBin, ["-m", "pytest", ...testFiles], {
  stdio: "inherit",
  cwd: rootDir,
  env,
});

child.on("error", (err) => {
  console.error("[agent-quality-guard] Falha ao iniciar processo Python:", err.message);
  process.exit(1);
});

child.on("exit", (code, signal) => {
  if (signal) {
    process.kill(process.pid, signal);
  } else {
    // Default a falha (1) se o processo sair sem código explícito — anti-gaming.
    process.exit(code ?? 1);
  }
});
