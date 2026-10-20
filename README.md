# 🛡️ Search Guard Agent

[![npm version](https://img.shields.io/npm/v/agent-quality-guard.svg?color=cb3837&style=flat-square)](https://www.npmjs.com/package/agent-quality-guard)
[![MCP Standard](https://img.shields.io/badge/MCP-Standard%20stdio-blue.svg?style=flat-square)](https://modelcontextprotocol.io/)
[![100% Deterministic](https://img.shields.io/badge/Validation-100%25%20Deterministic-brightgreen.svg?style=flat-square)]()
[![Zero ML Overhead](https://img.shields.io/badge/Inference-Zero%20GPU%20%2F%20Zero%20ML-success.svg?style=flat-square)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)](./LICENSE)

**Search Guard Agent** is a 100% deterministic, zero-ML code-verification and
surgical-search engine delivered as a Model Context Protocol (MCP) server. It
safeguards autonomous AI agent workflows (Claude Desktop, Cursor, OpenCode,
Codex) with two canonical tools:

- **`cirurgic_search`** — hybrid AST + Ripgrep + RRF code search with strict
  context-budget control.
- **`agent_quality_guard`** — a fail-fast, multi-layer delivery-verification
  pipeline that runs real test suites, AST security guards, the project's own
  compiler/linter toolchain, and universal Tree-Sitter meta-rules.

```bash
# Run universally via NPX (Node/Bun):
npx -y agent-quality-guard

# Or add to Claude Desktop / Cursor / OpenCode MCP config:
# { "command": "npx", "args": ["-y", "agent-quality-guard"] }
```

It prevents the most common failure modes of AI code generation —
**over-mocking**, **fabricated tests**, **type-contract breakage**, **silent
error swallows**, and **security vulnerabilities** — before code reaches
production or remote repositories.

> **📦 Migration notice:** the standalone NPM packages `cirurgic-search-mcp`
> and `agent-quality-guard` are **deprecated** in favor of this unified
> package. Existing installs keep working, but all new features land here.
> See [Migration](#-migration-from-legacy-packages) below.

---

## ⚡ Key Highlights

- **100% Deterministic:** Zero probabilistic inference, zero neural
  hallucinations, zero GPU/VRAM requirement. Sub-second execution.
- **Fail-Fast Anti-Mock:** Enforces real dependencies in production code and
  test suites. Detects synthetic fixtures and mock gaming.
- **Native Toolchain Bridge:** Does not reinvent linters or compilers — it
  orchestrates the native toolchain of the project (`tsc`, `eslint`, `biome`,
  `ruff`, `mypy`, `clippy`, `go vet`).
- **Universal Tree-Sitter AST Meta-Rules:** Language-agnostic syntactic
  inspection detecting SQL injection, command execution, hardcoded
  credentials, floating promises, and silent catch blocks.
- **Resistance Circuit Breaker:** Built-in 5-attempt circuit breaker with
  deterministic emergency release to eliminate agent retry deadlocks.
- **Configurable Exclusions:** Workspace-level `.cirurgicignore` plus a
  per-call `exclude_patterns` parameter keep credentials, vendored code and
  generated artifacts out of every scan.
- **Dual Tool Interface:** Each capability is exposed under a canonical name
  with a fully compatible legacy alias.

---

## 🏛️ Verification Pipeline Architecture

`agent_quality_guard` runs a fail-fast, hierarchical verification pipeline:

```text
┌────────────────────────────────────────────────────────────────────────┐
│                   AGENT QUALITY GUARD PIPELINE                         │
├────────────────────────────────────────────────────────────────────────┤
│ 1. Layer 0: Real Deterministic Test Suites                             │
│    • Pytest (isolated sequential chunks)                               │
│    • Node.js / Bun test (`node:test`, `bun test`, `vitest`)            │
│    • Flutter / Dart (`flutter test`)                                   │
│    • Cargo / Go / C# native test runners                               │
├────────────────────────────────────────────────────────────────────────┤
│ 2. Layer 1: NeedleASTGuard — Anti-Mock & Security (Fail-Fast)          │
│    • PROD_MOCK: unittest.mock / jest.mock in production src             │
│    • HARDCODED_SECRETS: high-entropy tokens, API keys, private keys    │
│    • SQL_INJECTION / DANGEROUS_EVAL / COMMAND_INJECTION                 │
├────────────────────────────────────────────────────────────────────────┤
│ 3. Layer 2: Native Compiler & Linter Bridge                            │
│    • TypeScript / JavaScript: `tsc --noEmit`, `eslint`, `biome`        │
│    • Python: `ruff check`, `mypy --no-incremental`                     │
│    • Rust / Go: `cargo clippy`, `go vet`                               │
├────────────────────────────────────────────────────────────────────────┤
│ 4. Layer 3: Universal Tree-Sitter AST Meta-Rules                       │
│    • FLOATING_PROMISES: orphan unawaited promises in async code         │
│    • SILENT_CATCH: empty except/catch blocks without telemetry          │
│    • Resource leaks, dangerous eval, command injection                  │
├────────────────────────────────────────────────────────────────────────┤
│ 5. Resilience Circuit Breaker                                          │
│    • State tracked across attempts (1 to 5)                            │
│    • Attempt 5: Triggers deterministic EMERGENCY_RELEASE               │
└────────────────────────────────────────────────────────────────────────┘
```

Any blocking violation short-circuits the remaining layers (`fail-fast`).

---

## 🛠️ MCP Tools Reference

The server runs over standard `stdio` and registers four tool names serving
two underlying deterministic implementations:

| Canonical tool          | Legacy alias (deprecated)  | Purpose                              |
| ----------------------- | -------------------------- | ------------------------------------ |
| `cirurgic_search`       | `qmd_search`               | Hybrid AST + Ripgrep + RRF search    |
| `agent_quality_guard`   | `needle_verify_delivery`   | Deterministic delivery verification  |

### 1. `cirurgic_search` (canonical search)

Hybrid code search combining exact lexical matching (**Ripgrep**) with
semantic/AST extraction (**Needle 3 / Tree-Sitter**) and **Reciprocal Rank
Fusion (RRF)** ranking — all on CPU, with no ML dependency. Use it to explore
code, locate functions, understand business rules and investigate bugs while
keeping the LLM context window under control.

**Parameters:**

- `query` *(string, required)*: Technical or semantic question about what to
  locate in the code.
- `workspace` *(string, optional)*: Path to the repository/workspace to search
  (defaults to the current working directory).
- `scope` *(string, optional)*: Comma-separated list of directories, modules
  or paths to restrict the search to.
- `exclude_patterns` *(string[] | string, optional)*: Additional gitignore-style
  globs to exclude for this call only, using the same semantics as
  `.cirurgicignore`. A single string is also accepted. The exclusions are
  **additive** — they are summed with the universal builtins and the workspace
  `.cirurgicignore`, never overriding them. Applied identically on the lexical
  (ripgrep/grep) and AST legs of the hybrid engine.
- `max_tokens` *(integer, optional)*: Hard cap on the token budget of the
  returned context (default `1200`). Protects the caller's context window.
- `depth` *(integer, optional)*: Maximum search depth — the number of
  directory-tree levels walked (ripgrep `--max-depth` / walk cutoff) and,
  when applicable, the AST relational depth (`Needle3Query.relational_depth`,
  valid range 0–5). **Default `None` = unlimited depth**, identical to the
  historical behavior for callers that omit the parameter. MCP clients that
  already send an explicit `depth` remain 100% backward compatible.

**Output:** a ranked list of snippets. Each entry carries `file`, `lines`,
`rrf_score`, `relations` (AST links), `exact_matches`, `content`, `source`
(`hybrid` / `needle_semantic` / `grep_literal`) and an estimated `tokens`
count.

### 2. `qmd_search` (legacy alias — deprecated)

A 100% compatible alias routing to the **exact same implementation and
signature** as `cirurgic_search`, kept for clients and cached agent prompts
that still call the old name. **New integrations should use
`cirurgic_search`.** The alias will be maintained for backward compatibility
indefinitely, but no new behavior will be added to it.

### 3. `agent_quality_guard` (canonical verification)

The primary tool to audit and validate a delivery before closing an issue or
card. Runs the full pipeline above.

**Parameters:**

- `workspace` *(string, optional)*: Absolute path to the repository/workspace
  being audited (defaults to the current working directory).
- `audit_mode` *(string, optional)*: `"delta"` (only staged/modified files) or
  `"full"` (entire repository). Defaults to `"delta"`.
- `attempt_number` *(integer, optional)*: Current cycle attempt (1–5). The
  authoritative circuit-breaker count is tracked server-side; this value is
  informational.
- `pedido` *(string, optional)*: Description of the requested delivery, for
  the audit log.
- `resultado` *(string, optional)*: Summary of the implemented changes, for
  the audit log.
- `task_type` *(string, optional)*: Classification tag (default
  `"codigo_producao"`).

**Output schema:**

```json
{
  "passed": true,
  "status": "APPROVED",
  "attempt_number": 1,
  "pytest_success": true,
  "blocking_ast_violations": [],
  "structural_ast_warnings": [],
  "layer1_toolchain_diagnostics": [],
  "toolchain_status": [
    { "tool": "tsc", "available": true, "status": "clean" },
    { "tool": "eslint", "available": true, "status": "clean" }
  ],
  "layer2_meta_rule_violations": [],
  "tool_name": "agent_quality_guard"
}
```

Statuses: `APPROVED`, `REJECTED`, or `EMERGENCY_RELEASE` (fifth attempt
without a clean deterministic pass — the code is released with a documented
caveat rather than deadlocking the agent).

### 4. `needle_verify_delivery` (legacy alias — deprecated)

A fully compatible alias routing to the **exact same implementation and
signature** as `agent_quality_guard`. Kept for older clients and cached
system prompts. **New integrations should call `agent_quality_guard`
directly.**

---

## 🔍 Configurable Exclusions

Both tools honor layered exclusion rules so that credentials, vendored code
and generated artifacts never leak into search results or audit reports.

### `.cirurgicignore` (workspace-level)

Place a `.cirurgicignore` file at the **root of the workspace** (e.g.
`<repo>/.cirurgicignore`). The file is loaded only if it exists. Syntax is a
subset of gitignore: one pattern per line, `#` starts a full-line comment,
blank lines are ignored, and a trailing `\r` is tolerated (CRLF-safe).

```gitignore
# .cirurgicignore — keep secrets and vendored code out of every scan
.env
*.pem
*.key
node_modules/
vendor/
dist/
build/
*.min.js
screenshots
panel/*.js
legacy/
```

**Pattern semantics:**

| Pattern form        | Meaning                                                        |
| ------------------- | -------------------------------------------------------------- |
| Bare name (no `/`)  | e.g. `screenshots` or `.agent-teams` — matches at **any** level, as a file **or** a directory. |
| Contains `/`        | e.g. `panel/*.js` — anchored to the **workspace root**.        |
| Trailing `/`        | e.g. `legacy/` — directory-only match.                         |
| Leading `!`         | **Not supported.** A line starting with `!` is ignored entirely (no negation). |

The file is re-read on every call, so edits take effect immediately without
restarting the MCP server. Its patterns are **summed** with the universal
builtins and any per-call `exclude_patterns` — they never remove or override
those.

### `exclude_patterns` (per-call, search tools only)

Pass extra globs for a single invocation without touching the file. This
parameter is available on the **search tools** (`cirurgic_search` and its
legacy alias `qmd_search` — same signature); it is not a parameter of
`agent_quality_guard`, which is not a search tool.

```json
{
  "tool": "cirurgic_search",
  "args": {
    "query": "payment retry logic",
    "exclude_patterns": ["**/fixtures/**", "**/*.snapshot.ts"]
  }
}
```

Patterns from `exclude_patterns` are **additive**: they are summed with the
universal builtins and `.cirurgicignore`, never overriding them. The engine
applies them identically on both legs of the hybrid search — the lexical
route (ripgrep/grep) and the AST route (`_find_candidate_files`).

### Builtin safe defaults

Even without configuration, the engine always skips a universal set of noise
directories: `.git`, `node_modules`, `dist`, `__pycache__`, `.venv`,
`pytest_cache`, and `.mypy_cache`.

---

## 📊 Token-Budget Estimation (`CHARS_PER_TOKEN = 3.8`)

Context returned by `cirurgic_search` is bounded by `max_tokens` (default
`1200`). To enforce that bound without loading a real tokenizer, the engine
uses a **fast heuristic**:

```text
estimated_tokens = ceil(len(text) / CHARS_PER_TOKEN)     where CHARS_PER_TOKEN = 3.8
```

`CHARS_PER_TOKEN` is a **named constant** defined once in
`agent_quality_guard/core/schemas.py` (single source of truth) and imported
by the search and needle engines in place of any inline magic number.

**This is a high-fidelity heuristic approximation for fast budget calculation
of snippets — it is not a strict cryptographic guarantee of token count.**
It is calibrated on the empirical ratio for mixed code/natural-language
snippets (≈ 3.8 characters per token for English prose and common programming
syntax) and is deliberately chosen because:

- it costs **zero milliseconds** and zero dependencies — no BPE table is
  loaded;
- it is **deterministic** — the same input always yields the same budget;
- it is **conservative enough** — the `ceil()` and a floor of 1 ensure we
  never under-count so badly that a single snippet blows the caller's window.

Treat the reported `tokens` as a **budget-control approximation** for keeping
LLM context windows in check, not as a billing-exact or model-exact token
count. Different tokenizers (Claude, GPT, Gemini, Llama) will disagree with
this figure by a few percent in either direction — which is acceptable for
its purpose: truncating *before* the context overflows.

---

## 🚀 Installation & Usage

### Running Locally with Python

```bash
# Clone the repository
git clone https://github.com/maiteuxzanela/search-guard-agent.git
cd search-guard-agent

# Create a virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"

# Run the MCP server via stdio
agent-quality-guard-mcp
```

### Configuring in OpenCode / OpenChamber (`opencode.json`)

```json
{
  "mcp": {
    "search-guard-agent": {
      "type": "stdio",
      "command": "/path/to/search-guard-agent/.venv/bin/python",
      "args": ["/path/to/search-guard-agent/agent_quality_guard/mcp_server.py"],
      "env": { "PYTHONPATH": "/path/to/search-guard-agent" }
    }
  }
}
```

### Configuring in Claude Desktop (`claude_desktop_config.json`)

```json
{
  "mcpServers": {
    "search-guard-agent": {
      "command": "/path/to/search-guard-agent/.venv/bin/python",
      "args": ["/path/to/search-guard-agent/agent_quality_guard/mcp_server.py"]
    }
  }
}
```

---

## 📦 Migration from Legacy Packages

The former standalone projects **`cirurgic-search-mcp`** and
**`agent-quality-guard`** have been unified into this single package. Summary
of what changed:

| Before                                             | After                                          |
| -------------------------------------------------- | ---------------------------------------------- |
| `npx -y cirurgic-search-mcp`                       | `npx -y agent-quality-guard`                   |
| `npx -y agent-quality-guard`                       | `npx -y agent-quality-guard` (unchanged)       |
| Tool `qmd_search`                                  | Tool `cirurgic_search` (alias kept)            |
| Tool `needle_verify_delivery`                      | Tool `agent_quality_guard` (alias kept)        |
| Python package `agent-quality-guard-mcp`           | Same package, unified docs                     |

**What you need to do:**

1. **NPM users of `cirurgic-search-mcp`:** switch your `npx`/`package.json`
   reference to `agent-quality-guard`. The old package is deprecated and will
   not receive new features.
2. **NPM users of `agent-quality-guard`:** no action required — you are
   already on the unified package. Update at your convenience to pick up the
   new tools.
3. **Callers of `qmd_search`:** rename to `cirurgic_search` when convenient.
   The alias continues to work with an identical signature; it is soft-deprecated
   and only maintained for backward compatibility.
4. **Callers of `needle_verify_delivery`:** rename to `agent_quality_guard`
   when convenient. Same compatibility guarantee.

See [`CHANGELOG.md`](./CHANGELOG.md) for the full history.

---

## 🧪 Testing

Search Guard Agent enforces rigorous anti-mock testing on itself:

```bash
# Run the complete deterministic test suite
pytest tests/ -v

# Lint and type-check (what the quality gate runs)
ruff check agent_quality_guard/ tests/
mypy --no-incremental agent_quality_guard/
```

Contributions must include real tests against real dependencies — no mocks in
production code. See [`CONTRIBUTING.md`](./CONTRIBUTING.md).

---

## 🔐 Security

To report a vulnerability, please follow the responsible-disclosure process in
[`SECURITY.md`](./SECURITY.md). Do not open a public issue for security
reports.

---

## 📄 License

MIT License. See [LICENSE](./LICENSE) for details.
