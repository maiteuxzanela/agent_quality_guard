# 🛡️ Agent Quality Guard

[![npm version](https://img.shields.io/npm/v/agent-quality-guard.svg?color=cb3837&style=flat-square)](https://www.npmjs.com/package/agent-quality-guard)
[![npm downloads](https://img.shields.io/npm/dm/agent-quality-guard.svg?style=flat-square)](https://www.npmjs.com/package/agent-quality-guard)
[![MCP Standard](https://img.shields.io/badge/MCP-Standard%20stdio-blue.svg?style=flat-square)](https://modelcontextprotocol.io/)
[![100% Deterministic](https://img.shields.io/badge/Validation-100%25%20Deterministic-brightgreen.svg?style=flat-square)]()
[![Zero ML Overhead](https://img.shields.io/badge/Inference-Zero%20GPU%20%2F%20Zero%20ML-success.svg?style=flat-square)]()
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg?style=flat-square)](./LICENSE)

**Agent Quality Guard** is a 100% deterministic, zero-ML code verification engine and Model Context Protocol (MCP) server designed to safeguard autonomous AI agent workflows (Claude Desktop, Cursor, OpenCode, Codex).

```bash
# Run universally via NPX (Node/Bun):
npx -y agent-quality-guard

# Or add to Claude Desktop / Cursor / OpenCode MCP config:
# { "command": "npx", "args": ["-y", "agent-quality-guard"] }
```

It prevents the most common failure modes of AI code generation: **over-mocking**, **fabricated tests**, **type contract breakage**, **silent error swallows**, and **security vulnerabilities** before code reaches production or remote repositories.

---

## ⚡ Key Highlights

- **100% Deterministic:** Zero probabilistic inference, zero neural hallucinations, and zero GPU/VRAM requirement. Executes in sub-second time.
- **Fail-Fast Anti-Mock:** Enforces real dependencies in production code and test suites. Detects synthetic fixtures and mock gaming.
- **Native Toolchain Bridge:** Does not reinvent linters or compilers—it orchestrates the native toolchain of the project (`tsc`, `eslint`, `ruff`, `mypy`, `clippy`, `go vet`).
- **Universal Tree-Sitter AST Meta-Rules:** Language-agnostic syntactic inspection detecting SQL injection, command execution, hardcoded credentials, floating promises, and silent catch blocks.
- **Resistance Circuit Breaker:** Built-in 5-attempt circuit breaker with deterministic emergency release to eliminate agent retry deadlocks.
- **Dual MCP Tool Interface:** Exposes the canonical `agent_quality_guard` tool with backward-compatible alias `needle_verify_delivery`.

---

## 🏛️ Verification Pipeline Architecture

Agent Quality Guard runs a fail-fast, hierarchical verification pipeline:

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
│ 2. Layer 1: Native Compiler & Linter Bridge                            │
│    • TypeScript / JavaScript: `tsc --noEmit`, `eslint`, `biome`        │
│    • Python: `ruff check`, `mypy --no-incremental`                     │
│    • Rust / Go: `cargo clippy`, `go vet`                               │
├────────────────────────────────────────────────────────────────────────┤
│ 3. Layer 2: Universal Tree-Sitter AST Meta-Rules                       │
│    • PROD_MOCK: Unittest.mock / jest.mock / sinon in production src    │
│    • HARDCODED_SECRETS: High-entropy tokens, API keys, private keys    │
│    • SQL_INJECTION / DANGEROUS_EVAL / COMMAND_INJECTION                │
│    • FLOATING_PROMISES: Orphan unawaited promises in async code        │
│    • SILENT_CATCH: Empty except/catch blocks without telemetry         │
├────────────────────────────────────────────────────────────────────────┤
│ 4. Resilience Circuit Breaker                                          │
│    • State tracked across attempts (1 to 5)                            │
│    • Attempt 5: Triggers deterministic EMERGENCY_RELEASE               │
└────────────────────────────────────────────────────────────────────────┘
```

---

## 🛠️ MCP Tools Reference

The server runs over standard `stdio` and registers two tools serving the same underlying deterministic implementation:

### 1. `agent_quality_guard` (Canonical)
The primary tool to audit and validate delivery before closing an issue or card.

**Parameters:**
- `workspace` *(string, optional)*: Absolute path to the repository/workspace being audited (defaults to current working directory).
- `audit_mode` *(string, optional)*: `"delta"` (only staged/modified files) or `"full"` (entire repository). Defaults to `"delta"`.
- `attempt_number` *(integer, optional)*: Current cycle attempt (1 to 5) for circuit breaker tracking.
- `pedido` *(string, optional)*: Description of the requested delivery for context.
- `resultado` *(string, optional)*: Summary of the implemented changes.
- `task_type` *(string, optional)*: Optional classification tag.

**Output Schema:**
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

### 2. `needle_verify_delivery` (Legacy Alias)
A 100% compatible legacy alias that routes to the exact same implementation, ensuring older clients or cached agent system prompts continue working without changes.

---

## 🚀 Installation & Usage

### Running Locally with Python
```bash
# Clone the repository
git clone https://github.com/maiteuxzanela/agent_quality_guard.git
cd agent_quality_guard

# Create virtual environment and install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run the MCP Server via stdio
python3 src/mcp_server.py
```

### Configuring in OpenCode / OpenChamber (`opencode.json`)
```json
{
  "mcp": {
    "agent-quality-guard": {
      "type": "stdio",
      "command": "/path/to/agent_quality_guard/.venv/bin/python3",
      "args": ["/path/to/agent_quality_guard/src/mcp_server.py"],
      "env": {
        "PYTHONPATH": "/path/to/agent_quality_guard"
      }
    }
  }
}
```

### Configuring in Claude Desktop (`claude_desktop_config.json`)
```json
{
  "mcpServers": {
    "agent-quality-guard": {
      "command": "/path/to/agent_quality_guard/.venv/bin/python3",
      "args": ["/path/to/agent_quality_guard/src/mcp_server.py"]
    }
  }
}
```

---

## 🧪 Testing

Agent Quality Guard enforces rigorous anti-mock testing on itself:

```bash
# Run the complete deterministic test suite
pytest tests/ -v
```

---

## 📄 License

MIT License. See [LICENSE](./LICENSE) for details.
