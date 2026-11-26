# Package Boundaries (MUST)

> Soothe is a **one-way dependency DAG**. Place code in the correct **monorepo-owned** package. **Never reverse an arrow.** Enforcement: `scripts/check_module_import_boundaries.sh` (wired into `./scripts/verify_finally.sh`).

**Owned**: `soothe`, `soothe-daemon`, `soothe-cli`. **Submodules** (`client/*`): consumed as code — do **not** format/lint/test/release them here. **PyPI-only**: `soothe-deepagents`, `soothe-client-python`, `soothe-nano` (maintain/release in their own repos). `soothe-nano` is published to PyPI from its own repo and is **independent of the soothe project** — see the soothe-nano Independence rule below.

> **soothe-sdk was dissolved (2026-10-13).** Contracts now live with their axis:
> - **Agent axis (canonical)**: `soothe-nano` ≥1.4 — plugin decorator API, `core` events/registry/verbosity, `protocols`, `paths`, `intention`, `observability.langfuse`, `ux.stream_tool_wire`, `tools` security metadata, `utils.serde`.
> - **Host axis (server copies)**: `soothe` — wire codec/protocol, display binder/ledger/wire, ux task/classification helpers, `workspace_sync` (the agent-plane sync engine — consumed by external host applications; `soothe-daemon` does **not** wire it), `identity.errors`, `skillify` models. `soothe-daemon` imports these from `soothe`.
> - **Client axis (client copies)**: `soothe-client-python` ≥1.1 — wire codec/protocol, core event constants, display parse side, ux render helpers, tools display metadata, utils, paths. `soothe-cli` consumes these and depends on nothing else first-party.
> - The two axes couple **only via the wire protocol** (RFC-450) — the same model as the Go/Rust clients. Cross-axis copies are kept aligned by contract tests, not shared code.
>
> **Cleansing note (2026-10-07):** `soothe-autopilot` is no longer a separate package. Rails (LoopRail + Rail Exec) and the ContextEngine live under `soothe`; the daemon runtime (`LoopRailService`, worker dispatch, cron) lives under `soothe-daemon`. See [IG-779](../../.platonic/impl/IG-779-rfc-autopilot-cleansing-and-mode-alignment.md).

## Placement (where new code goes)

| Concern | Package |
|---------|---------|
| Plugin author API (`@plugin`/`@tool`/`@subagent`), agent-side event/protocol contracts | `soothe-nano` (PyPI, own repo) |
| Coding CoreAgent, skills/MCP/backends in-proc | `soothe-nano` (PyPI) |
| Host wire codec, display binder/ledger, ux task namespaces, workspace sync, identity errors, skillify models | `soothe` |
| StrangeLoop, Context Engine, identity, host runner, **rails (LoopRail + Rail Exec)** | `soothe` |
| Process lifecycle, channels, HTTP/WS server, admin IO, cron, **loop runner implementations** (thread / process / ray) | `soothe-daemon` |
| Human CLI / TUI | `soothe-cli` |
| Language WS clients + client-side contract copies | `client/*` (submodules) |

## Import allow / deny (MUST) — this table IS the DAG

| Package | May import | Must NOT import |
|---------|------------|-----------------|
| `soothe` | `soothe-nano`, `soothe-deepagents` | `soothe_daemon`, `soothe_cli`, `soothe_client`, `soothe_sdk` (dissolved) |
| `soothe-daemon` | `soothe`, `soothe-nano`, `soothe-deepagents` | `soothe_cli`, `soothe_client`, `soothe_sdk` (dissolved) |
| `soothe-cli` | `soothe-client-python` only (first-party) | `soothe`, `soothe_daemon`, `soothe_nano`, `soothe_sdk` (use WebSocket, not Python imports) |

## Hard bans (owned packages)
1. **CLI sits above the daemon** — `soothe_cli` must not import daemon/host; communicate via wire contracts in `soothe-client-python`.
2. **Daemon does not depend on the WS client** — `soothe_daemon` must not import `soothe_client` in runtime source; admin RPCs use `soothe.wire` (tests may use the client via the `dev` extra).
3. **Private nano middleware is closed** — owned packages must not import `soothe_nano.middleware._*`.
4. **The dissolved SDK is closed** — no owned package may import `soothe_sdk`; contracts live in `soothe-nano` (agent axis), `soothe` (host axis), or `soothe-client-python` (client axis).

Host packages (`soothe`, `soothe-daemon`, `soothe-cli`) MAY reference internal design doc identifiers in comments.

## soothe-nano Independence (MUST)

`soothe-nano` is a standalone upstream library (own repo `mirasoth/soothe-nano`, published to PyPI). It sits **below** the soothe monorepo in the DAG: `soothe` / `soothe-daemon` consume it as a dependency. The dependency is strictly **one-way** — the soothe project may depend on `soothe-nano`, but `soothe-nano` MUST remain independent of the soothe project.

1. **No reverse imports** — `soothe-nano` MUST NOT import `soothe`, `soothe_daemon`, `soothe_cli`, `soothe_client`, or any monorepo-owned-package module. Its first-party dependencies are separate upstream PyPI libraries (`soothe-deepagents`, and the agent-axis contracts formerly from `soothe-sdk`), never the soothe monorepo itself; everything else is third-party.
2. **No host coupling** — do not move host concepts (StrangeLoop, Context Engine, rails, wire codec, display binder/ledger, workspace sync, identity errors, skillify, daemon/CLI runtime) into `soothe-nano`. These belong to owned packages. `soothe-nano` owns only the agent axis (CoreAgent, plugin API, in-proc skills/MCP/backends, agent-side event/protocol contracts).
3. **No build/version coupling** — `soothe-nano` keeps its own `pyproject.toml` version, `CHANGELOG.md`, `AGENTS.md`, CI, and release workflow. It MUST NOT read the monorepo root `VERSION`, root `CHANGELOG.md`, or share release triggers. Monorepo releases pin a compatible `soothe-nano` floor; they do not bump or publish `soothe-nano`.
4. **PyPI-only here** — `soothe-nano` is consumed as a PyPI dependency only; it is no longer vendored as a submodule. Do not format, lint, test, edit, or release it from the monorepo. Make changes in the `soothe-nano` repo, release there, then bump the PyPI floor here. See [project-reference.md](project-reference.md) and [release-and-governance.md](release-and-governance.md).
5. **Contracts flow downward only** — shared contracts live on the agent axis in `soothe-nano` (≥1.4) and are consumed by the host, never defined in owned packages and imported back into `soothe-nano`. The two axes couple only via the wire protocol (RFC-450), not shared Python imports.

## API Exposure (Minimum-Exposure) (MUST)
- A parent `__init__.py` re-exports only what users are expected to import. For processors, that is exactly the operator class(es) — nothing else.
- Do not re-export type schemas, builders, or helpers through parent packages when direct module imports suffice.
- Never list private `_`-prefixed names in `__all__`.
