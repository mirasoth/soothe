<div align="center">
  <img src="assets/soothe-logo.png" alt="Soothe Logo" width="200" />

  #

  [![CI](https://github.com/mirasoth/soothe/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/mirasoth/soothe/actions/workflows/ci.yml)
  [![PyPI version](https://img.shields.io/pypi/v/soothe)](https://pypi.org/project/soothe/)
  [![Python](https://img.shields.io/pypi/pyversions/soothe)](https://pypi.org/project/soothe/)

  🎥 [Watch the demo video on Vimeo](https://player.vimeo.com/video/1185023866?h=72febe1ed2) · 📖 [Documentation Wiki](https://mirasoth.github.io/soothe/)
</div>

> **Define intent. Let Soothe handle the rest.**

Soothe is a **goal-driven orchestration framework** for 24/7 autonomous agents. It keeps humans out of the execution loop: you describe *what* you want, and Soothe plans, executes, and steers the work to completion — across sessions, across goals, across time.

Built on [soothe-nano](https://github.com/mirasoth/soothe-nano) and an [enhanced deepagents](https://github.com/mirasoth/soothe-deepagents), it adds a persistent **agentic loop** and **goal engine**: context carries across sessions, long-running goals keep moving, and interdependent objectives coordinate through a typed dependency graph. Move from *human-in-the-loop* to **agent-in-the-loop**.

---

## ✨ Vision

We believe autonomous agents should work the way great teams do: given a clear objective, they plan, act, remember, and adapt — without someone watching every step.

Soothe is built on five ideas:

- 🎯 **Goals, not prompts** — A goal is a first-class, persisted entity with its own lifecycle, not a string in a prompt. Soothe tracks dependencies between goals, coordinates them, and keeps each one moving.
- 🔁 **A loop that thinks** — StrangeLoop is a compiled plan → assess → execute graph that actively pulls the next ready goal and reports back. Two-phase planning keeps it frugal; evidence-bound steps keep it honest.
- 🧠 **One memory, one truth** — A single DAG is the source of truth for goals, steps, and the message ledger. Every record stores the reasoning that created it, so projections show *why*, not just *what*.
- 🔄 **Always-on, never-loses** — A persistent daemon owns 24/7 state. `soothe loop detach` keeps a loop running while your client exits; crash recovery bounds re-execution loss to one bundle of work per goal.
- 🌐 **One wire contract** — A published [AsyncAPI 3.0 spec](.platonic/specs/asyncapi.yaml) is the single source of truth for the WebSocket protocol. Schema validation at the transport boundary, capability negotiation, full `subscribe → next → complete` lifecycle. No compatibility shims.

---

## What Can Soothe Do?

| Capability | Features |
|------------|----------|
| **Autonomous Execution** | Multi-step workflows, file ops, code execution, shell commands, web research, browser-use |
| **Long-Running Ops** | Background daemon, loop management, persistent state, `soothe loop detach` / `attach` |
| **Rail-Guided Workflows** | Built-in LoopRails (`feature-dev`, `hotfix`, `greenfield-system`, `spike`, `pr-review`, `maker-checker`) — declarative job-scoped pipelines for plan → implement → review → QA |
| **Custom Plugins** | `@tool`, `@subagent`, `@plugin` decorators, MCP server integration, semantic skill discovery |
| **Multi-Provider** | OpenAI, OpenAI-compatible endpoints, vLLM; configurable router profiles (default / fast / think) |
| **Durable Memory** | SQLite (dev) or PostgreSQL + pgvector (prod); thread checkpoints, vector stores, semantic memory |

## Milestones

| Status | Milestone |
|--------|-----------|
| ✅ | **Single-Session Autonomy** — End-to-end goal execution |
| ✅ | **Cross-Thread Continuity** — Persistent context across threads |
| ✅ | **Multi-Goal Orchestration** — Interdependent long-horizon workflows |
| ✅ | **LoopRails** — Declarative rail-governed pipelines (merged into `soothe` core) |
| ⏳ | **Benchmark Reproduction** — [Compiler experiment](https://github.com/anthropics/claudes-c-compiler) |

---

## Quick Start

**Install** the full stack (Python 3.11+):

```bash
pip install -U soothe soothe-cli soothe-daemon
```

**Start the daemon** and run your first prompt:

```bash
soothed setup          # scaffold config (interactive) — or: soothed setup --yes
soothed start          # background daemon on ws://127.0.0.1:8765
soothe -p "Summarize this repo"   # one-shot headless query
```

Or open the interactive TUI:

```bash
soothe                 # Textual TUI — slash commands, live step cards, plan panel
```

### Autonomous goals (Autopilot)

Submit a long-running, rail-governed goal via the `/autopilot` slash command —
either in the TUI or headless:

```bash
# Headless — runs through the loop-native rail path
soothe -p "/autopilot hotfix fix the login bug"
soothe -p "/autopilot greenfield-system Build a REST API for inventory tracking"

# TUI — same syntax, plus a live dashboard
soothe
# inside the TUI:
/autopilot feature-dev Refactor the auth module and add tests
```

Built-in rails: `feature-dev` · `hotfix` · `greenfield-system` · `spike` · `pr-review` · `maker-checker`.
Omit the rail name and Soothe auto-picks one from the catalog.

Watch live jobs, goals, and step DAGs in the TUI's `/context` modal — an
htop-style dashboard ported from the classic `top` view (toggle views with
`a` / `s` / `l` / `d`, scroll with `j`/`k`).

📖 **[Quick Start guide](docs/wiki/getting-started/Quick-Start.md)** — Docker, local daemon, production stack, first prompt.

### Language clients

| | Package | Latest |
|--|---------|--------|
| Python | [`soothe-client-python`](https://pypi.org/project/soothe-client-python/) | [![PyPI](https://img.shields.io/pypi/v/soothe-client-python.svg)](https://pypi.org/project/soothe-client-python/) |
| TypeScript | [`@mirasoth/soothe-client`](https://www.npmjs.com/package/@mirasoth/soothe-client) | [![npm](https://img.shields.io/npm/v/@mirasoth/soothe-client.svg)](https://www.npmjs.com/package/@mirasoth/soothe-client) |
| Go | [`soothe-client-go`](https://github.com/mirasoth/soothe-client-go) | [![Go](https://img.shields.io/github/v/release/mirasoth/soothe-client-go?label=go)](https://github.com/mirasoth/soothe-client-go/releases) |
| Rust | [`soothe-client`](https://crates.io/crates/soothe-client) | [![crates.io](https://img.shields.io/crates/v/soothe-client.svg)](https://crates.io/crates/soothe-client) |

📖 **[Clients guide](docs/wiki/clients.md)** — install, API tiers, protocol notes.

---

## 🌐 Ecosystem

Soothe is the center of a small, focused ecosystem. Each project has one job:

### Core dependencies (PyPI)

| Project | What it is |
|---------|------------|
| [**soothe-nano**](https://github.com/mirasoth/soothe-nano) | The Coding CoreAgent — a batteries-included deepagents stack (tools, skills, MCP, backends). The foundation Soothe builds on. |
| [**soothe-deepagents**](https://github.com/mirasoth/soothe-deepagents) | A deepagents fork with `MemoryMiddleware` and host-friendly extensions. Leaf dependency. |
| [**soothe-sdk**](https://github.com/mirasoth/soothe-sdk) | Shared wire contracts — event types, display contracts, plugin protocols. Consumed by every Soothe package and language client. |

### Monorepo packages (this repo)

| Package | PyPI | Role |
|---------|------|------|
| `soothe` | [`soothe`](https://pypi.org/project/soothe/) | Core runtime — StrangeLoop, ContextEngine, LoopRails, runner, protocols |
| `soothe-cli` | [`soothe-cli`](https://pypi.org/project/soothe-cli/) | `soothe` command — Typer CLI + Textual TUI |
| `soothe-daemon` | [`soothe-daemon`](https://pypi.org/project/soothe-daemon/) | `soothed` command — WebSocket/HTTP server, cron, Autopilot dispatch |

### Companion projects

| Project | What it is |
|---------|------------|
| [**FlowJet**](https://github.com/mirasoth/flowjet) | A coding-agent host that runs Soothe's StrangeLoop stream in-process — no daemon required. Optimized for direct stream consumption and server sessions. |

### Dependency direction

```text
soothe-sdk            ← shared contracts (leaf)
soothe-deepagents     ← deepagents fork (leaf)
        ↓
soothe-nano           ← Coding CoreAgent
        ↓
soothe                ← host: StrangeLoop, ContextEngine, LoopRails, runner
        ↓
soothe-daemon         ← soothed process
        ↑
soothe-cli            ← Typer + Textual TUI
```

One-way DAG, enforced by `scripts/check_module_import_boundaries.sh` in CI.

---

## Documentation

| Resource | Description |
|----------|-------------|
| [Wiki](https://mirasoth.github.io/soothe/) | User, operator, and developer guides |
| [Quick Start](docs/wiki/getting-started/Quick-Start.md) | Install, daemon, first prompt, Docker, production |
| [Installation](docs/wiki/getting-started/Installation.md) | Package overview, methods, troubleshooting |
| [CLI Reference](docs/wiki/cli-reference.md) | All `soothe` and `soothed` commands |
| [TUI Guide](docs/wiki/tui-guide.md) | Slash commands, keyboard shortcuts, `/context` dashboard |
| [Autonomous Mode](docs/wiki/autonomous-mode.md) | `/autopilot` rails, progress events, configuration |
| [Language clients](docs/wiki/clients.md) | Python / TypeScript / Go / Rust WebSocket SDKs |
| [Configuration guide](docs/wiki/configuration-guide/) | YAML, env vars, providers, common patterns |
| [Architecture](docs/wiki/architecture/index.md) | System design, module map, protocol hierarchy |
| [LoopRails](packages/soothe/src/soothe/rails/builtin_rails/README.md) | Built-in rails catalog and custom rail authoring |
| [RFCs](.platonic/specs/) | Architecture specs and the AsyncAPI 3.0 wire contract |
| [CHANGELOG](CHANGELOG.md) | Release notes |
| [AGENTS.md](AGENTS.md) | AI agent development guide |

## License

MIT
