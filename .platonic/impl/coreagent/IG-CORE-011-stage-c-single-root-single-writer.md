# Implementation Guide: Stage C — Single Root and Single Writer

**Guide**: IG-CORE-011
**Title**: Stage C — 单根（single root）与单写者（single writer）
**Created**: 2026-11-26
**Related RFCs**: RFC-CORE-010 (Workspace Sync Architecture), RFC-AGSP-001 §7 (agentspace 执行面), RFC-002 (flowjet)

## Overview

Stage C of the workspace sync simplification (IG-CORE-009/010). Three
decisions land together:

1. **单根（single root）** — the manifest moves under the workspace's own
   content prefix (`workspaces/<wsId>/.workspace/manifest.json`); the
   `system/engine` root and the `{workspace}` URI-template mechanism are
   deleted. One source URI (`s3://<bucket>/workspaces`), one per-run
   backend, one credential/config surface.
2. **单写者（single writer）** — agentspace adds a per-workspace
   single-flight prompt gate (409 on a second in-flight turn for the same
   workspace, chat turns AND one-shot tasks); the engine deletes the
   concurrent-turn bookkeeping (`_OpenTurnEntry.active_turns` refcounting,
   sole-active-turn reconcile gating, last-turn-out close). The engine
   contract becomes: hosts serialize turns per workspace; concurrent turns
   are undefined.
3. **persisted 可见（commit failures surface in the turn result）** —
   `close_turn` returns a `TurnCommit(persisted, version, error)` result;
   flowjet's runtime backend commits BEFORE surfacing the terminal event
   and converts a failed commit of an otherwise-successful run into
   `RunFailed(code="workspace_persist_failed")`. "Turn completion =
   persisted" is now observable, not just logged.

## Layout (after)

```
s3://<bucket>/workspaces/<wsId>/
├── <relpath>…                        ← plain content tree (authoritative)
└── .workspace/manifest.json          ← commit record (version, artifacts)
```

Migration: clean cutover. Old `system/engine/runs/<wsId>/manifest.json`
objects become inert orphans (nothing reads them); the first turn after
upgrade re-materializes the content tree (intact) and starts a fresh
manifest at version 1. Per-host concrete notes:

* the reserved `.workspace/` key prefix is engine-private: the soothe
  materialize/reconcile passes skip it, the dirty tracker excludes it, and
  the agentspace file API hides it from listings and rejects reads/writes
  into it;
* local `.workspace/` dir creation in the manager stays (engine marker).

## Functional contract

1. Turn-scoped sync boundary — one `open_turn`/`close_turn` pair per
   prompt; hosts serialize turns per workspace (single writer).
2. `open_turn` — open (or reuse) the workspace, load the manifest,
   materialize the content tree (sha-skip), reconcile deletions, start
   dirty tracking.
3. `close_turn` — commit (hash → content upload → deletion mirror →
   manifest write, version+1 with `if_match`), then close the workspace
   and release its resources. Returns `TurnCommit`.
4. Failed commit ⇒ `persisted=False` AND the workspace stays open so the
   dirty set survives to the next turn's commit (a fresh tracker would
   baseline the uncommitted files as clean and reconcile could delete
   them). Resource cost is bounded by the number of failing workspaces.
5. Read-path failures (manifest load, materialization) stay logged
   best-effort warnings — local state is usable and the commit still
   reports its own outcome.

## Implementation Plan

### Task 1 — soothe engine

- [x] `factory.py`: unchanged (cloud scheme allowlist stays SSRF-strict);
      hosts build the per-run closure (base URI + `/` + run_id).
- [x] `backends/fsspec.py`: manifest path becomes
      `<root>/.workspace/manifest.json` (per-run root layout); `run_id`
      parameters are validated but no longer part of the path;
      `list_content_files` excludes the engine key space.
- [x] `manager.py`: `backend_factory: Callable[[str], WorkspaceSyncBackend | None]`
      replaces `backend` + `content_backend_factory`; one per-run backend
      serves manifest AND content; factory failure ⇒ warn + local-only run.
- [x] `workspace.py`: single `backend` parameter (the per-run backend serves
      both key spaces); `close()` closes it.
- [x] `turns.py`: single-writer facade — no entry map / refcounting;
      `open_turn` reuses an open workspace (kept open after a failed
      commit); `close_turn` returns `TurnCommit` and closes only after a
      clean commit.
- [x] `workspace_sync.py` / `__init__.py`: protocol docstring (one root,
      two key spaces); export `TurnCommit`.
- [x] Tests: turns (reuse-on-failure, persisted flag, resource release,
      close-after-clean-commit), manager (factory contract), fsspec
      (manifest path layout, engine-key-space exclusion), checkpoint
      suite (single-backend construction), content-mirror (single-root
      round trip incl. local recycle).

### Task 2 — flowjet

- [x] Resolver: `sync_source` becomes the single root; `content_source`
      (the `{workspace}` template) is deleted; the per-run backend factory
      appends `<run_id>` to the source URI (file:// special case kept).
- [x] `Settings` / `wiring.py` / `app.py`: drop `workspace_content_source`.
- [x] `backend.py`: hold the terminal event; commit before surfacing it;
      failed commit of a successful run ⇒ `RunFailed(code="workspace_persist_failed")`;
      abort paths keep the best-effort close.
- [x] Sandbox resolver wrapper: threads the `close_session` result through.
- [x] Tests + CHANGELOG.

### Task 3 — agentspace (Go)

- [x] Per-workspace single-flight: chat `StartTurn` acquires the workspace
      key (new `ErrWorkspaceBusy`, 409); the task executor leaves tasks
      queued while the workspace is busy (scan loop retries); the
      reservation spans the whole turn goroutine (released after every
      exit path).
- [x] File API: hide/reject the reserved `.workspace/` prefix (listings
      skip it; reads → `ErrNotFound`; writes/moves/deletes →
      `ErrInvalidPath`).
- [x] Purge: the `workspaces/<wsId>/` prefix now covers the manifest; the
      legacy `system/engine/runs/<wsId>/` delete stays as cleanup.
- [x] Tests: gate unit tests (single-flight, unscoped passthrough);
      integration tests (engine dir hidden/protected, purged with the
      workspace).

### Task 4 — miracloud (deploy + specs)

- [x] Helm values: `FLOWJET_WORKSPACE_SYNC_SOURCE` →
      `s3://mirasoth-agentspace/workspaces`; delete
      `FLOWJET_WORKSPACE_CONTENT_SOURCE`.
- [x] RFC-AGSP-001 §7: layout tree, env block, single-writer + persisted
      semantics, purge, Stage C landed; rfc-history Revision 110;
      RFC-AGL-001 §5.3.6 env enumeration updated.

## Verification

- [x] soothe: 153 sync tests + full unit suite (3216) green
- [x] flowjet: 689 unit tests green against the local Stage C soothe tree
      (`PYTHONPATH=packages/soothe/src`); backend-level
      persisted-visibility tests; ruff + mypy clean
- [x] agentspace: go build/vet/gofmt/golangci-lint (incl.
      `--build-tags integration`) clean; unit tests green (one
      pre-existing `TestVarsMatchNamingConvention` failure —
      `AGENTSPACE_S3_BUCKET_LOOKUP` vocabulary gap, reproduces on the
      clean tree); engine-dir integration tests added (env-gated)
- [x] Existing deployments: clean cutover — old `system/engine/` prefix
      becomes an inert orphan; the first turn after upgrade re-materializes
      the content tree and starts a fresh manifest at version 1

## Related Documents

- IG-CORE-009 (Stage A facade), IG-CORE-010 (Stage B content-authoritative)
- RFC-AGSP-001 §7: `.platonic/specs/agentspace/RFC-AGSP-001-agentspace.md`
