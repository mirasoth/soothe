# RFC Index

**Last Updated**: 2026-10-22
**Total RFCs**: 96 (77 active, 19 archived)

This index is the canonical catalog of all RFCs in the Soothe project. It also
serves as the deprecation list and archived-RFC table.

> **Namespace Partition (2026-10-21)**: Active specs were partitioned into 9
> namespaces (`coreagent`, `loop`, `protocol`, `daemon`, `client`, `plugin`, `sdk`, `persist`, `sloop`). Active IDs use the
> qualified form `RFC-<PREFIX>-NNN`; cross-cutting RFCs (RFC-000, RFC-001)
> stay at the specs root. Archived RFCs keep the IDs they held at archival time.
> The full old-ID to new-ID mapping is recorded in `rfc-history.md`.

> **⚠️ Autopilot Cleansing (2026-10-07)**: The legacy `soothe-autopilot`
> subsystem has been replaced in code by **LoopRail** (RFC-LOOP-017) +
> **ContextEngine** (RFC-SDK-002). Six RFCs were archived with their still-normative
> content absorbed into RFC-LOOP-017: RFC-204, RFC-222, RFC-228, RFC-229, RFC-230,
> RFC-625. RFC-900 (process/deprecation scheme) was deleted — its content
> duplicated this index and `rfc-history.md`. `docs/rfc-methodology-guide.md`
> was also deleted — RFC templates now come from the platonic-coding skill's
> shipped assets. See
> [IG-LOOP-013](../impl/loop/IG-LOOP-013-rfc-autopilot-cleansing-and-mode-alignment.md)
> for the full migration mapping.

> **⚠️ Path Restructure Notice (2026-08)**: RFCs written before the 2026-07
> `core/` → flat package restructure retain original design-time paths as
> historical context. Canonical path mappings:
>
> | Old path prefix | Current path |
> |-----------------|-------------|
> | `soothe/core/strange_loop/{core,cognition,state,analysis,utils}/` | `soothe/sloop/{engine,cognition,state,utils}/` |
> | `soothe/core/loop/` | `soothe/sloop/` |
> | `soothe/core/goal_engine/` | `soothe/autopilot/` |
> | `soothe/core/runner/` | `soothe/runner/` |
> | `soothe/core/agent/` | `soothe/coreagent/` |
> | `soothe/core/intention/` | `soothe/sloop/intention/` |
> | `soothe/core/prompts/` | `soothe/prompts/` |
> | `soothe/core/events/` | `soothe/events/` |
> | `soothe/core/middleware/` | `soothe_nano.middleware.*` (PyPI) |
> | `soothe/core/security/` | `soothe/security/` |
> | `soothe/core/resolver/` | `soothe/runner/resolver/` |
> | `soothe_daemon/channels/http_rest.py` | Removed (IG-504) |
> | `soothe_daemon/protocol/errors.py` | `soothe_daemon/protocol/error_codes.py` |
> | `ProtocolError` (daemon) | `RpcProtocolError` |
>
> Explicitly patched RFCs note the correction inline.

## Namespace Summary

| Namespace | Prefix | RFCs | Description |
|-----------|--------|------|-------------|
| [`coreagent/](coreagent/rfc-index.md) | CORE | 10 | CoreAgent runtime - tools, workspace, filesystem security, context injection |
| [`loop/](loop/rfc-index.md) | LOOP | 28 | StrangeLoop plan-execute loop - cognition, prompting, context, intake, middleware |
| [`protocol/](protocol/rfc-index.md) | PROT | 7 | Protocol architectures (context, memory, planner, policy, durability, identity) |
| [`daemon/](daemon/rfc-index.md) | DAEM | 8 | Daemon - events, MCP, channels, thread management, IPC |
| [`client/](client/rfc-index.md) | CLNT | 9 | Client & presentation - TUI, display, appkit, streaming, widgets |
| [`plugin/](plugin/rfc-index.md) | PLUG | 4 | Plugin & subagent ecosystem |
| [`sdk/](sdk/rfc-index.md) | SDK | 3 | SDK module structure & shared utilities |
| [`persist/](persist/rfc-index.md) | PERS | 3 | Persistence backends |
| [`sloop/](sloop/rfc-index.md) | SLP | 3 | Sloop graph topology, recursive decomposition & eval threads |

## Root RFCs (cross-cutting)

| ID | Title | Kind | Status |
|----|-------|------|--------|
| [RFC-000](RFC-000-system-conceptual-design.md) | System Conceptual Design | Conceptual Design | Implemented |
| [RFC-001](RFC-001-core-modules-architecture.md) | Architecture Design for Core Protocol Modules | Architecture Design | Implemented |

Non-RFC root docs: [`rest-api-spec.md`](rest-api-spec.md) (daemon REST API contract).

## RFC Status Summary

| Status | Count |
|---|---|
| Implemented (partial) | 39 |
| Implemented | 30 |
| Draft | 8 |

## RFC Kind Summary

| Kind | Count |
|---|---|
| Architecture Design | 58 |
| Implementation Interface Design | 16 |
| Architecture Design + Protocol Specification | 1 |
| Architecture Design + Implementation Interface Design | 1 |
| Conceptual Design | 1 |

## Archived RFCs

Archived specs live in `.platonic/specs/archive/` and keep the IDs they held at archival time.

- **RFC-200**: Autonomous Goal Management Loop - archived; Superseded By: RFC-222 (control flow), RFC-625 (GoalEngine architecture)
- **RFC-203**: StrangeLoop State & Memory Architecture - archived; Superseded By: RFC-SDK-003
- **RFC-204**: Autopilot Mode - archived; Superseded By: RFC-LOOP-017 (report-commit judgment §4.1); UX surfaces moved to RFC-CLNT-001/504 + RFC-DAEM-005
- **RFC-216**: StrangeLoop Multi-Thread Infinite Lifecycle - archived; Superseded By: -
- **RFC-222**: Autopilot Daemon Architecture - archived; Superseded By: RFC-LOOP-017 (LoopRailService runtime §4 + §14 component map)
- **RFC-228**: Autopilot Job IPC Commands - archived; Superseded By: RFC-DAEM-005 (`job_*` IPC commands), RFC-LOOP-017 (§10 rail selection)
- **RFC-229**: Cron Service for Autopilot - archived; Superseded By: RFC-LOOP-017 (§18 cron as external submit path), RFC-DAEM-005 (cron IPC)
- **RFC-230**: Job Maturity Assessment for Autopilot Rails - archived; Superseded By: RFC-LOOP-017 (§4 job maturity + §8 maturity predicates)
- **RFC-300**: Context and Memory Architecture Design - archived; Superseded By: RFC-PROT-002 (ContextProtocol Architecture), RFC-PROT-003 (MemoryProtocol Architecture)
- **RFC-411**: Event Stream Replay & History Reconstruction - archived; Superseded By: RFC-DAEM-004
- **RFC-505**: Soothe Desktop Client Architecture - archived; Superseded By: -
- **RFC-605**: Explore Subagent and Parallel Spawning - archived; Superseded By: RFC-613
- **RFC-613**: Explore Agent — LLM-Orchestrated Iterative Search - archived; Superseded By: -
- **RFC-625**: AutopilotMonitor and ContextEngine Unification - archived; Superseded By: RFC-LOOP-017 (§17 CE `GoalNode.report` commit + `goal_report_committed`), RFC-SDK-002 (Context Engine)
- **RFC-700**: Desktop App Product Redesign - archived; Superseded By: RFC-CLNT-010 (client library architecture)
- **RFC-CLNT-006**: DeepAgents CLI TUI Migration - archived 2026-10-21; Superseded By: Soothe-native TUI (RFC-CLNT-009 step cards, RFC-CLNT-003 presentation engine); deepagents migration withdrawn (IG-166 archived)
- **RFC-CLNT-007**: Progressive Display Refinements Post-Migration - archived 2026-10-21; Superseded By: RFC-CLNT-009 (step-card display) and RFC-CLNT-003 (runtime presentation engine); StreamDisplayPipeline removed by IG-348
- **RFC-LOOP-020**: Reasoning Quality & Progressive Actions - archived 2026-10-21; Superseded By: RFC-SLP-002 (decompose_task) and RFC-LOOP-021; LLMPlanner removed by IG-SLP-003
- **RFC-SDK-001**: SDK Module Structure Refactoring - archived 2026-10-21; Superseded By: soothe-sdk package dissolved 2026-10-13 (commit 23b14a4c7); contracts moved to soothe-nano (PyPI) agent-axis + soothe host-axis
