# Security Policy

We take the security of **search-guard-agent** seriously. This project is a
deterministic code-verification engine and MCP server that inspects source
code, runs test suites and executes compiler/linter toolchains on the
workspaces you point it at. Because it touches real repositories and real
processes, a responsible disclosure channel matters.

## Supported Versions

Security fixes are applied to the following versions:

| Version | Supported          |
| ------- | ------------------ |
| 0.1.x   | :white_check_mark: |
| < 0.1   | :x: (pre-release)  |

The legacy standalone packages `cirurgic-search-mcp` and `agent-quality-guard`
on NPM are deprecated and will **not** receive security patches. Please
migrate to the unified `search-guard-agent` package.

## Reporting a Vulnerability

**Do not open a public GitHub issue for security vulnerabilities.**

Please report vulnerabilities privately through one of the following channels:

1. **GitHub Security Advisories (preferred):** open a private vulnerability
   report in the official repository at
   [`maiteuxzanela/search-guard-agent` → Security → Advisories](https://github.com/maiteuxzanela/search-guard-agent/security/advisories/new).
2. **Email:** contact the maintainer through the private channel listed on the
   maintainer's GitHub profile (`maiteuxzanela`). If you need a PGP key for
   encrypted email, request it through the GitHub Security Advisory flow.

### What to include

To help us triage and reproduce quickly, please include:

- A description of the vulnerability and its potential impact.
- Affected version(s) (package version and/or commit hash).
- Minimal reproduction steps or a proof-of-concept (a failing workspace, a
  crafted input file, etc.).
- Any known workarounds.

### What to expect

- **Acknowledgement:** within 5 business days of your report.
- **Status updates:** we will keep you informed of progress and expected
  timelines for a fix.
- **Disclosure coordination:** we ask that you do not disclose the
  vulnerability publicly until a fix is released or we agree on a joint
  disclosure date. Coordinated disclosure is preferred.
- **Credit:** with your permission, we will credit reporters in the security
  advisory and release notes.

## Scope

In scope for this policy:

- Remote code execution, path traversal or sandbox escapes reachable through
  the MCP tool surface (`cirurgic_search`, `agent_quality_guard` and their
  aliases).
- Injection vulnerabilities surfaced by the AST guards that the engine itself
  fails to detect (e.g. a SQL-injection pattern that slips past Layer 1/2).
- Denial-of-service through crafted inputs (pathological AST, unbounded
  recursion, resource exhaustion) that breaks the documented circuit breaker.
- Secrets accidentally committed to this repository or leaked through tool
  output.

Out of scope:

- Vulnerabilities in **third-party dependencies** (report upstream; we will
  upgrade once a fix exists).
- Findings that require physical access to the machine running the server.
- Issues in the AI agents or hosts (Claude Desktop, Cursor, OpenCode, etc.)
  that consume this MCP server — report those to the respective vendors.
- Social-engineering or phishing against maintainers.

## Sandbox & Operational Best Practices

This tool is designed to run **deterministic, read-oriented inspections**, but
it does execute real test suites and real compilers by design. To keep your
environment safe:

- **Run inside a sandbox.** Prefer a container, VM, or dedicated CI runner
  with a non-privileged user. Do not run the MCP server as root or with
  unrestricted filesystem access.
- **Scope the workspace.** Point `workspace` only at the repository you intend
  to audit. Avoid home directories or paths containing unrelated secrets.
- **Use `.cirurgicignore` and `exclude_patterns`** to exclude credential
  stores (`.env`, `*.pem`, keystores), vendored code and generated artifacts
  from both search and audit walks.
- **Never commit real secrets to test fixtures.** Use obviously fake values.
  The AST guards flag high-entropy tokens even in tests — that is intentional.
- **Pin versions in production.** Lock `search-guard-agent` to a specific
  version and review the changelog before upgrading.
- **Least-privilege integrations.** When wiring the MCP server into an agent
  host, restrict the host's own permissions (network, filesystem) to what the
  agent legitimately needs.

## Threat Model Notes

- The engine performs **no ML inference and no network calls** as part of its
  verification pipeline (beyond executing the project's own test suite, which
  may hit the network if the tests themselves do).
- All guards (NeedleASTGuard, Tree-Sitter meta-rules, native toolchain bridge)
  are deterministic. There is no probabilistic model in the path, so findings
  are reproducible and auditable.
- The circuit breaker's `EMERGENCY_RELEASE` on the fifth attempt is a
  **process-resilience** feature, not a security control. It prevents agent
  retry deadlocks; it does not bypass the AST/security layers — those still
  run on every attempt.

Thank you for helping keep search-guard-agent and its users safe.
