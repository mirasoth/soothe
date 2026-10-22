# IG-LOOP-013: RFC Autopilot Cleansing and Agent-Mode Alignment

**Created**: 2026-10-07
**Status**: Draft
**Scope**: Docs-only RFC cleansing (no code changes; code already aligned)

---

## Problem

The legacy `soothe-autopilot` daemon-level autonomous goal management subsystem
has been replaced in code by **LoopRail** (the loop's job-scoped, event-driven
workflow-pattern layer) plus the **ContextEngine** (CE) for goal DAG state.
`packages/soothe-autopilot/` no longer exists in the monorepo; rails live under
`packages/soothe/src/soothe/rails/`; the InlineGate (`AutoModeMiddleware`) and
`AskUserGateMiddleware` already implement the design clarified below.

The RFC corpus has not been updated to match:

- Six RFCs (RFC-204, RFC-222, RFC-228, RFC-229, RFC-230, RFC-625) still
  normatively describe the legacy autopilot subsystem and are referenced as
  normative by RFC-LOOP-017 (LoopRail) and several dependent RFCs.
- RFC-LOOP-017 (the replacement) is still expressed in legacy terminology —
  "AutopilotService" as the consuming service, `soothe/autopilot/rails/` as
  the module path, "Autopilot" as the framing concept.
- RFC-LOOP-028 (AutoModeMiddleware) §3.5 documents `active_in_bypass: true` as the
  config default, contradicting the implemented `false` default and the design
  rule that **bypass mode permits all tool calls**.
- RFC-LOOP-029 (AskUserGateMiddleware) §3 documents the gate as firing in
  "auto mode only", contradicting the implemented behavior and the design rule
  that **bypass mode also routes `ask_user` to veritas inline**.
- RFC-LOOP-023 / RFC-LOOP-024 reference "autopilot runs" / "autopilot worker" as the
  headless context; the framing must be updated without changing semantics.

## Design Clarification (binding)

| Mode | Mutating tool calls (`edit_file`, `write_file`, `delete`, `run_command`) | `ask_user` calls |
|------|-----------------------------------------------------------------------------|------------------|
| **Bypass** | ALL permitted (no deny/safety/allow gate evaluation; `active_in_bypass: false` default) | Routed to veritas inline (gate fires in bypass mode) |
| **Agent auto** | Gate evaluates (deny → safety → allow → veritas fallback for ambiguous) | Routed to veritas inline (gate fires in auto mode) |
| **Manual** | Gate + human relay | Human relay |

LoopRail replaces legacy autopilot:
- "AutopilotService" → **LoopRailService** (the loop-rail-aware service that
  binds rails, judges `goal_report_committed`, schedules workers, and runs job
  maturity).
- Rails live under `soothe/rails/` (not `soothe/autopilot/rails/`).
- `AutopilotMonitor` dreaming/backoff for no-rail jobs is removed from the
  normative RFC surface; no-rail jobs use CE opportunistic dispatch.

## Scope

### In scope (RFC docs only)

| # | Action | RFCs / files |
|---|--------|--------------|
| 1 | Archive (move to `.platonic/specs/archive/`) with supersession notices | RFC-204, RFC-222, RFC-228, RFC-229, RFC-230, RFC-625 |
| 2 | Rewrite in place: replace `AutopilotService` → `LoopRailService`, `soothe/autopilot/rails/` → `soothe/rails/`, "Autopilot" framing → "loop rail"; absorb still-normative report-commit judgment (from RFC-204 §1.3) and CE `GoalNode.report` commit (from RFC-625) so RFC-LOOP-017 is self-contained | RFC-LOOP-017 |
| 3 | Fix config default: `active_in_bypass: true` → `false`; clarify §3.1 row 1 means bypass permits all calls | RFC-LOOP-028 |
| 4 | Extend §3 decision table to cover bypass mode (gate fires in auto AND bypass; defer/fail → station human relay in auto, → retry sentinel / hard defer in bypass when no human attached) | RFC-LOOP-029 |
| 5 | Replace "autopilot" terminology with "loop-rail runs" / "headless runs" / "LoopRailService"; preserve all semantics including the seven-day `awaiting_clarification` park and the headless no-fallback contract | RFC-LOOP-023, RFC-LOOP-024 |
| 6 | Cleanse autopilot references that point to archived RFCs; re-point to RFC-LOOP-017 | RFC-LOOP-018, RFC-SDK-003, RFC-SDK-002, RFC-DAEM-005, RFC-DAEM-007, RFC-CLNT-001, RFC-CLNT-005, RFC-LOOP-026, RFC-CLNT-010, RFC-CLNT-006, RFC-SLP-002, RFC-SLP-003, RFC-CORE-009, RFC-LOOP-007, RFC-LOOP-014, RFC-LOOP-011, RFC-DAEM-002, RFC-DAEM-008, RFC-CLNT-008, RFC-DAEM-004, RFC-DAEM-006 |
| 7 | Add the 6 newly-archived RFCs to the deprecation list with `Superseded By: RFC-LOOP-017` | RFC-900 |
| 8 | Move 6 RFCs to archived section; update active/archived counts; refresh RFC-LOOP-017 entry to mark it as the normative replacement | `../../specs/rfc-index.md` |
| 9 | Record the cleansing event with date, rationale, and migration mapping | `../../specs/rfc-history.md` |
| 10 | Remove `soothe-autopilot` package from the package list (already gone from `packages/`); re-word "Autopilot, rails, verify, dispatch" → "Rails, verify, dispatch" under `soothe` package | `AGENTS.md`, `.agents/rules/project-reference.md`, `.agents/rules/package-boundaries.md`, `.agents/rules/release-and-governance.md` |

### Out of scope

- **Code cleansing** of remaining `AutopilotService` references in
  `packages/soothe-daemon/src/soothe_daemon/{cron/service.py,protocol/router.py,server/core.py}`
  and `packages/soothe-cli/src/soothe_cli/tui/app/_model.py`. Tracked as a
  follow-up code task; this IG is RFC-only.
- Renumbering any RFC (per RFC-900 §8.2 "When NOT to Renumber": widely
  referenced RFCs keep their numbers; only the status changes to Archived).
- Changing the report-commit judgment pattern or CE GoalNode.report semantics
  — they are absorbed verbatim into RFC-LOOP-017 §4 (architectural invariant) and a
  new §17 (CE report commit) so the normative content survives archival of
  RFC-204/RFC-625.
- Behavioral changes to `AutoModeMiddleware` or `AskUserGateMiddleware` — the
  code is already correct; only the RFC text is wrong.

## Migration mapping

| Archived RFC | Still-normative content absorbed into |
|--------------|---------------------------------------|
| RFC-204 §1.3 (report-commit judgment) | RFC-LOOP-017 §4 (expanded) |
| RFC-204 §1.4 (dreaming) | Dropped — no-rail jobs use CE opportunistic dispatch |
| RFC-204 §2-12 (UX surfaces, scheduler, channel protocol) | Dropped — covered by RFC-CLNT-001/504 + RFC-DAEM-005 daemon protocol |
| RFC-222 (AutopilotService runtime) | RFC-LOOP-017 §4 + §14 (LoopRailService component map) |
| RFC-228 (Job IPC commands) | RFC-DAEM-005 daemon protocol; commands renamed `job_*` (no `autopilot_` prefix) |
| RFC-229 (Cron Service) | Absorbed into RFC-LOOP-017 §18 (cron as external submit path) |
| RFC-230 (Job maturity) | Absorbed into RFC-LOOP-017 §4 (job maturity runs in LoopRailService) |
| RFC-625 (AutopilotMonitor + CE unification) | RFC-LOOP-017 §17 (CE GoalNode.report commit + report_committed event); RFC-SDK-002 (Context Engine) remains the CE spec |

## Verification

- `rg -n 'AutopilotService|soothe\.autopilot\.|soothe/autopilot/' .platonic/specs/*.md`
  returns no hits in active (non-archived) RFCs.
- `rg -n 'active_in_bypass: true' .platonic/specs/RFC-LOOP-028*.md` returns no hits.
- `rg -n 'auto mode only' .platonic/specs/RFC-LOOP-029*.md` returns no hits.
- All 6 archived RFCs are listed in `.platonic/specs/archive/` and in RFC-900's
  archived table.
- `../../specs/rfc-index.md` counts: 78 active (was 84), 15 archived (was 9).
- `./scripts/verify_finally.sh` green (docs build only — no code touched).

## Out-of-scope follow-ups

1. Code cleansing of `AutopilotService` references in soothe-daemon and
   soothe-cli (separate IG).
2. Migration of `soothe.autopilot.*` event types in `events/catalog.py`
   (already partially done — verify in follow-up).
3. RFC-LOOP-018 (WavePlan) deeper rewrite if it references archived rail concepts
   beyond terminology.

## Changelog

- 2026-10-07: Initial draft. Captures the design clarification confirmed by
  operator: archive all 6 legacy autopilot RFCs; `LoopRailService` as the
  replacement term; `active_in_bypass: false` default; RFC-LOOP-029 gate fires in
  auto AND bypass.
