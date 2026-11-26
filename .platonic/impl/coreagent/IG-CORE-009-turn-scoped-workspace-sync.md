# Implementation Guide: Turn-Scoped Workspace Sync Facade

**Guide**: IG-CORE-009
**Title**: Turn-Scoped Workspace Sync Facade and Engine Simplification
**Created**: 2026-11-26
**Related RFCs**: RFC-CORE-010 (Workspace Sync Architecture), RFC-002/RFC-003 (flowjet), RFC-AGSP-001 (agentspace 执行面接线)

## Overview

flowjet (and any host that brackets agent turns with open/close hooks) currently
reimplements the same per-turn sequence on top of `WorkspaceManager`: recover the
manifest snapshot, materialize the content tree with reconcile gating, restore prior
artifacts from CAS, start dirty tracking, then checkpoint and close on the last turn
out — plus concurrent-turn bookkeeping (`active_runs`, shared-open entries). That
~200-line orchestration is engine knowledge living outside the engine.

This guide consolidates the sequence into a single soothe-owned facade
(`TurnScopedWorkspaces`) and records the staged plan to simplify the whole
agentspace → flowjet → soothe → S3 chain while preserving its functional contract.

## Functional contract (must not regress)

1. **Turn-scoped sync boundary** — one open/close pair per prompt (RFC-RD-607 §4.3).
2. **Dual-source sync** — engine checkpoint/CAS root + plain-file content tree; host
   and non-engine readers see a natural file tree under `workspaces/<wsId>/`.
3. **Crash recovery** — restart/crash restores prior artifacts, declared resources,
   and the content tree before tracking restarts.
4. **Concurrent turns on one workspace** — shared open, deletion reconcile only when
   sole active turn, manager close on last turn out.
5. **Best-effort open** — content materialization/recovery failures log and continue
   with checkpoint state; checkpoint failures on close never take the turn down.

## Implementation Plan

### Stage A (this guide): turn-scoped facade

**Goal**: Own the per-turn orchestration inside `soothe.workspace.sync`; hosts keep
only session→run_id mapping and backend construction.

**Tasks**:
- [x] New `packages/soothe/src/soothe/workspace/sync/turns.py` —
      `TurnScopedWorkspaces` with `open_turn` / `close_turn` / `close_all` /
      `workspace_root`, porting flowjet's inbound sequence (recover →
      `materialize_content(protected=artifact paths, reconcile=sole turn)` →
      artifact CAS restore → `start_tracking`) and outbound sequence
      (checkpoint → last-turn-out `manager.close`), including shared-open
      bookkeeping and best-effort failure semantics.
- [x] Export `TurnScopedWorkspaces` from `soothe.workspace.sync` (minimum-exposure:
      the facade class only).
- [x] Unit tests `packages/soothe/tests/unit/workspace/sync/test_turns.py` —
      orchestration semantics (shared open, reconcile gating, last-out close,
      error swallowing, artifact restore with missing-blob skip) plus an
      end-to-end MemoryFileSystem round trip (content materialize → dirty write →
      checkpoint mirror → fresh-engine reopen restores both trees).
- [x] flowjet switches `SyncAwareWorkspaceResolver` to the facade and deletes its
      duplicated lifecycle/restore/bookkeeping code (flowjet repo, unreleased).
- [x] CHANGELOG entries in both repos.

### Stage B: single storage representation (turn-scoped checkpoint)

**Goal**: The plain content tree becomes the single authoritative file store;
the manifest is the atomic commit record.

- `close_turn` = upload dirty files to the content tree (etag/sha dedupe) → write
  `manifest.json` (version+1, path/sha/deleted list) as the commit point.
- Delete: CAS blob duplication, `blobs/` key space, SNAPSHOT/DELTA/compaction,
  background uploader, debouncer, per-workspace state store, `if_match` guard.
- `open_turn` recovery = read manifest → fetch paths from the content tree →
  verify sha.
- Prerequisite decision: mid-turn durability policy (turn completion = persisted).
  Optional minute-level manifest snapshots if a plane needs it; no delta stack.
- Purge collapses to one prefix delete (no shared-CAS carve-out).

### Stage C: single root + host-side single-flight

- Merge `system/engine` and `workspaces/<wsId>` into one root (`.engine/` prefix
  inside the workspace tree, or one root with two derived prefixes); delete the
  `{workspace}` template machinery.
- agentspace gains a per-workspace single-flight gate for prompts; flowjet/soothe
  delete concurrent-turn bookkeeping and reconcile gating.
- Persistence failure becomes observable to the host turn result
  (`persisted: true/false` + checkpoint id) instead of log-only.

Stage B/C are not implemented by this guide; they are the recorded direction for
follow-up guides and require their own design docs before landing.

## File Structure

```
packages/soothe/src/soothe/workspace/sync/
├── __init__.py            (MODIFY — export TurnScopedWorkspaces)
└── turns.py               (NEW — the facade)

packages/soothe/tests/unit/workspace/sync/
└── test_turns.py          (NEW)
```

## Testing Strategy

Orchestration tests inject a mocked `WorkspaceManager` (duck-typed `Workspace`
fakes) exactly like the flowjet tests they replace; the round-trip test uses
`MemoryFileSystem` backends (zero network, zero credentials) and re-opens with a
fresh facade instance to pin the crash-recovery contract.

## Verification

- [x] `pytest packages/soothe/tests/unit/workspace/sync/test_turns.py` green
- [x] ruff check/format clean on changed modules
- [x] mypy clean on changed modules
- [x] flowjet `tests/unit/server/test_workspace_sync.py` green against local soothe

## Related Documents

- RFC-CORE-010: `.platonic/specs/coreagent/RFC-CORE-010-workspace-sync-materialization.md`
- IG-CORE-004 / IG-CORE-008: workspace sync implementation and review remediation
- flowjet RFC-002/RFC-003 (`.platonic/specs/` in the flowjet repo)
- agentspace RFC-AGSP-001 §7 (miracloud repo)
