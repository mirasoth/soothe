# Implementation Guide: Stage B — Content-Authoritative Workspace Sync

**Guide**: IG-CORE-010
**Title**: Stage B — Content-Authoritative Representation and Daemon De-Wiring
**Created**: 2026-11-26
**Related RFCs**: RFC-CORE-010 (Workspace Sync Architecture), RFC-AGSP-001 (agentspace 执行面), RFC-002 (flowjet)

## Overview

Stage B of the workspace sync simplification (IG-CORE-009). Two decisions land
together:

1. **Daemon de-wiring** — soothe-daemon no longer wires workspace sync at all.
   The daemon-side integration was half-wired (sync source detection on
   `loop_new`, a manager singleton, sync-out only via debounced auto
   checkpoints, no read path) and the agent plane (flowjet via
   `TurnScopedWorkspaces`) is the only supported sync surface. `loop_new`
   requests carrying a `workspace_sync_source` or a URI-shaped workspace are
   rejected fail-loud.
2. **Content-authoritative representation** — the plain content tree
   (`workspaces/<wsId>/…`) becomes the single authoritative file store; the
   manifest (`system/engine/runs/<wsId>/manifest.json`) is the atomic commit
   record. The CAS blob duplicate, SNAPSHOT/DELTA checkpoint payloads, the
   background uploader, the checkpoint debouncer, the per-run state store, and
   the publish/artifacts channel are deleted outright: after daemon de-wiring,
   content-authoritative is the only remaining mode, so nothing consumes them.

## Functional contract (preserved)

1. Turn-scoped sync boundary — one `open_turn`/`close_turn` pair per prompt.
2. Host-visible plain file tree — the content root stays a natural file tree.
3. Crash recovery — `open_turn` re-materializes the content tree (sha-skip) and
   reconciles deletions (sole active turn); the manifest records the
   engine-owned artifact set for reconcile protection.
4. Per-file commit semantics — `close_turn` uploads dirty files to the content
   tree, mirrors deletions, then writes the manifest (version+1, `if_match`
   guard) as the commit point. A crash mid-close leaves per-file atomic state;
   the next turn re-materializes whatever landed.
5. Mid-turn durability policy: **turn completion = persisted**. No debounced
   auto-checkpoints, no staging rows.

## Object-store layout (after)

```
s3://<bucket>/workspaces/<wsId>/…        ← plain file tree (authoritative)
s3://<bucket>/system/engine/
    runs/<wsId>/manifest.json            ← commit record (version, artifacts)
```

`blobs/sha256/`, `runs/<wsId>/checkpoints/`, `artifacts/` key spaces are no
longer written. Existing deployments keep their old objects as inert orphans;
manifests written by Stage A validate unchanged (unknown fields ignored).

## Implementation Plan

### Task 1 — Daemon de-wiring (soothe-daemon)

- [x] `server/core.py`: delete `_workspace_manager` attr + `_get_workspace_manager()`.
- [x] `protocol/router.py`: delete the RFC-906 sync-source detection block and
      `_open_sync_workspace()`; `loop_new` now rejects `workspace_sync_source`
      and URI-shaped workspaces with `WORKSPACE_RESOLUTION_FAILED` ("daemon
      does not support workspace sync").
- [x] Remove `workspace_sync` config surface consumed by daemon wiring.
- [x] Persistence: drop the dead `workspace_sync_source` loop-metadata field
      (sqlite column/migrations, postgres allowed-key whitelist,
      `daemon_loop_metadata` key) — nothing writes it anymore.
- [x] Config templates + deploy surface: remove the `workspace_sync` blocks from
      `config/{develop,production}/nano.yml`, the `SOOTHE_SYNC_*` vars from
      `config/production/example.env`, and the dev-stack `soothe-minio` +
      `soothe-minio-init` services (their only consumer was the daemon sync
      path); production README/compose notes updated to state the daemon
      de-wiring explicitly.
- [x] Tests: remove the sync-wiring tests; loop_new tests assert the fail-loud
      rejection; the old sync-defaults integration test is rewritten as a
      de-wiring test (no config key, no manager attribute, RPC-level rejection).

### Task 2 — Engine: content-authoritative commit (soothe)

- [x] `workspace_sync.py`: prune wire models (`Resource`, `Artifact`,
      `ArtifactSpec`, `CheckpointType`, `CheckpointPayload`, `Manifest.resources`)
      and the protocol surface (blob ops, checkpoint ops, publish, `stream_blob`,
      `stream_checkpoint`). Old manifests' unknown fields are ignored on load.
- [x] `backends/fsspec.py` + `streaming.py`: drop blob/checkpoint/publish key
      spaces and helpers; keep manifest + content-tree methods.
- [x] Delete modules: `cas.py`, `uploader.py`, `debouncer.py`,
      `materializer.py`, `checkpoint.py`, and the `workspace/state` package
      (staging DB whose only consumers were the uploader and daemon wiring).
- [x] `workspace.py`: `checkpoint()` becomes the commit (hash → content upload
      → deletion mirror → manifest write → ack); `recover()` deleted (the
      manager loads the manifest at open); publish/cleanup pruned.
- [x] `manager.py`: no CAS, no state store, no debouncer/uploader wiring;
      `open_from_uri` deleted (daemon-only).
- [x] `turns.py` (facade): signature drops `cas_root`/`state_store_factory`;
      `open_turn` = manager.open (manifest load) → materialize_content →
      start_tracking; CAS artifact-restore path deleted.

### Task 3 — flowjet

- [x] Resolver: sync now REQUIRES a content source (fail loud at engine
      construction — dual-source is the contract); `cas_root` setting and the
      SQLite state-store factory deleted.
- [x] Tests + CHANGELOG.

### Task 4 — miracloud (agentspace plane)

- [x] Helm values / flowjet-agentspace template: drop `FLOWJET_WORKSPACE_CAS_ROOT`.
- [x] RFC-AGSP-001 §7: layout tree (`runs/<wsId>/manifest.json` only),
      purge semantics (two prefixes — content tree + runs — no shared-CAS
      carve-out), rfc-history Revision 109.

## Verification

- [x] soothe: 3211 unit tests green (148 sync incl. rewritten
      checkpoint/content-mirror/manager/turns/fsspec suites); S3 integration
      test rewritten to the new contract (env-gated)
- [x] soothe-daemon: 1522 tests green incl. fail-loud loop_new + de-wiring
      integration tests
- [x] flowjet: 689 unit tests green against the local Stage B soothe tree
      (`PYTHONPATH=packages/soothe/src`); ruff + mypy clean
- [x] ruff/format clean on all touched packages; vulture dead-code gate green;
      dependency boundary validation green
- [x] Existing deployments: old `blobs/`, `runs/<wsId>/checkpoints/`, and
      `artifacts/` objects become inert orphans (nothing reads them); cleanup
      is a manual bucket GC concern, not a migration

## Related Documents

- IG-CORE-009 (Stage A facade + staged plan)
- RFC-CORE-010: `.platonic/specs/coreagent/RFC-CORE-010-workspace-sync-materialization.md`
