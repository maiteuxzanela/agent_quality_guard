# Contributing to search-guard-agent

Thanks for your interest in contributing! This project is a **100%**
deterministic code-verification engine and MCP server — no ML, no neural
inference, no probabilistic scoring. Every contribution must preserve that
invariant.

## Getting Started

### Prerequisites

- **Python 3.10+** (the package requires `>=3.10`).
- **Node.js 18+** or **Bun** (for the JS toolchain bridge and NPM entry
  points).
- **ripgrep** (`rg`) — strongly recommended for the lexical leg of
  `cirurgic_search`. The engine falls back to plain `grep` if `rg` is absent,
  but hybrid ranking quality is best with `rg` installed.

### Local Setup

```bash
# 1. Fork and clone your fork
git clone git@github.com:<your-username>/search-guard-agent.git
cd search-guard-agent

# 2. Create and activate a virtual environment
python3 -m venv .venv
source .venv/bin/activate

# 3. Install the package with dev dependencies
pip install -e ".[dev]"

# 4. Verify the environment by running the real test suite
pytest tests/ -v
```

### Running the MCP Server Locally

```bash
# Python entry point
agent-quality-guard-mcp

# Or directly
python -m agent_quality_guard.mcp_server
```

To wire it into an MCP host for manual testing (OpenCode / OpenChamber
`opencode.json`):

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

## How We Work

### 1. Real, deterministic tests — no mocks in production code

This is the single most important rule.

- **Production code** (`agent_quality_guard/**`) must never import
  `unittest.mock`, `MagicMock`, `pytest-mock`, `jest.mock`, `sinon`, or
  similar. The NeedleASTGuard Layer 1 gate blocks `mock`/`stub` usage under
  `src`/production paths outright.
- **Tests must run against real dependencies.** No monkeypatching real
  modules to fake behavior in ways that would hide integration breakage.
  Environment-level stubs (e.g. a React Native jest setup for native modules)
  are the only carve-out and must be justified in the PR.
- Every behavioral change ships with at least one test in `tests/` that
  exercises the real code path. A PR that only changes production code with
  no test will be rejected.

### 2. Conventional Commits

All commits must follow [Conventional Commits](https://www.conventionalcommits.org/):
`type(scope): description`. Allowed types:

| Type       | Use for                                                    |
| ---------- | ---------------------------------------------------------- |
| `feat`     | New user-facing capability                                 |
| `fix`      | Bug fix                                                    |
| `docs`     | Documentation only                                         |
| `test`     | Adding or correcting tests                                 |
| `refactor` | Code change with no behavior change                        |
| `chore`    | Tooling, CI, dependencies                                  |
| `perf`     | Performance improvement                                    |
| `revert`   | Reverting a previous commit                                |

Examples:

```
feat(search): add exclude_patterns parameter to cirurgic_search
fix(guard): stop flagging float() casts as DANGEROUS_EVAL
docs(readme): document token-budget heuristic
test(qmd): cover .cirurgicignore glob parsing
```

Use imperative mood in the subject ("add", not "added" or "adds"). Keep the
subject under 72 characters. Reference the ticket ID in the body when
applicable.

### 3. Quality Gate before you push

Before opening a PR, run the project's own verification pipeline against your
change:

```bash
# Full deterministic suite
pytest tests/ -v

# Lint and type-check (the native toolchain bridge runs these in CI)
ruff check agent_quality_guard/ tests/
mypy --no-incremental agent_quality_guard/
```

You can also invoke the quality gate end-to-end via the MCP tool itself
(`audit_mode: "delta"` audits only staged/modified files):

```
agent_quality_guard(
  workspace="/path/to/search-guard-agent",
  audit_mode="delta",
  pedido="<what you set out to change>",
  resultado="<what you actually changed>"
)
```

A contribution is ready for review when this gate returns `passed: true` /
`APPROVED` on your branch.

### 4. Code Style

- **Python:** `ruff`-formatted, 120-column lines, full type annotations on
  public functions. Docstrings in the same language as the surrounding module
  (existing code mixes English and Portuguese — follow the file you touch).
- **No dead code.** If you replace a path, delete the old one in the same PR.
- **No probabilistic logic.** Any change that would require a model, a
  heuristic score presented as ground truth, or runtime inference is
  out-of-scope by design (see `AGENTS.md` §5).

### 5. Pull Requests

1. Open PRs against `master`.
2. Fill in the PR template: what changed, why, how it was tested.
3. Link the ticket/issue.
4. Ensure CI is green (pytest, ruff, mypy, the AST guard layers).
5. At least one maintainer review is required before merge.

## Reporting Bugs & Security Issues

- **Bugs:** open a GitHub issue with reproduction steps, expected vs. actual
  behavior, and your environment (OS, Python/Node version).
- **Security vulnerabilities:** follow the private disclosure process in
  [SECURITY.md](./SECURITY.md). Do not open a public issue for security
  reports.

## Questions

If anything in this guide is unclear, open a discussion or issue and we will
improve the docs. Thank you for contributing!
