# Implementation Guide: Workspace Sync — Review Remediation

**Guide**: IG-CORE-008
**Title**: Workspace Sync Review Remediation
**Created**: 2026-11-26
**Status**: Implemented 2026-11-26 (re-scoped 2026-11-26 after consumer clarification)
**Related RFCs**: RFC-CORE-010 (Workspace Sync — Materialization and Incremental Persistence), RFC-CORE-003 (Security Filesystem Policy)
**Supersedes**: none (remediates the implementation delivered by IG-CORE-004)
**Code base**: commit `454c8d995` (all file/line references below are as of this commit)

---

## 1. Overview

### 1.1 Purpose and consumer model

**This subsystem is a library for downstream host apps, not a soothe-daemon feature.** The reference consumer is flowjet's `SyncAwareWorkspaceResolver` (`../flowjet/src/flowjet/core/runtime/isolation/workspace.py`), which owns the full lifecycle. soothe-daemon's sync wiring is intentionally absent; findings premised on "the daemon never drives the lifecycle" were retracted in this re-scope (see §3.1).

The consumer contract, extracted from flowjet (the shape this guide must keep working):

```text
manager = WorkspaceManager(workspaces_root, cas_root, backend,
                           state_store_factory, content_backend_factory,
                           watched_subdirs)          # flowjet workspace.py:202-209
ws = await manager.open(run_id)                     # one per run_id, refcounted by consumer
payload = await ws.recover()                        # every open — manifest recovery
await ws.materialize_content(protected=...,         # every turn — content tree
                             reconcile=...)
await ws.materialize(manifest)                      # when manifest.resources non-empty
await ws.start_tracking()                           # every turn
... agent runs ...
await ws.checkpoint()                               # every turn close
await manager.close(run_id)                         # last concurrent run out
await manager.close_all()                           # process shutdown
```

A full audit of the subsystem against this contract found library-level defects — including one **live data-loss chain** exercised by flowjet's restart path — plus resource leaks and performance problems in the hot paths. This guide turns the verified findings into an ordered remediation plan with paired regression tests.

### 1.2 Evidence Base

Findings were verified by static call-site tracing plus runtime reproductions, then re-validated against the flowjet consumer code. Four defects have hard reproductions (marked **[REPRO]** in §3); live-impact findings are marked **[LIVE]** (reachable through the flowjet call sequence above).

1. `Workspace.materialize` is broken in both directions (**[REPRO]**): passing the documented `list[Resource]` crashes in `Materializer.materialize` (`AttributeError: 'list' object has no attribute 'resources'`); passing the `Manifest` that flowjet actually passes (flowjet `workspace.py:431`) completes materialization then raises `TypeError` at the `len(resources)` logging line (`workspace.py:111`).
2. Mid-checkpoint writes lost (**[REPRO]**): a file written during a slow checkpoint upload never appears in any manifest; the dirty set is empty afterwards.
3. CAS blob corruption via hardlink in-place edit (**[REPRO]**): `CAS blob after in-place agent edit: b'MUTATED'`.
4. Broken-symlink arbitrary write in `materialize_content` (**[REPRO]**): remote content written outside the workspace root.

Environment facts pinned during verification: `fsspec 2026.9.0`, `s3fs` (0 references to `use_cache`), `sqlite3.threadsafety == 1`.

### 1.3 Scope

**In Scope**:
- Library-level defects in `packages/soothe/src/soothe/workspace/sync/` and `workspace/state/` — everything reachable through the §1.1 consumer contract.
- Performance remediation for the checkpoint and materialization hot paths (both run per turn in flowjet).
- Regression tests for every fix, including a soothe-side consumer-contract test that mirrors the flowjet sequence.
- RFC text amendments where the audit found the RFC wrong or stale (§5 Task D2).

**Out of Scope**:
- **soothe-daemon wiring** — the daemon is not a consumer by design; the router/core wiring gaps are not defects (retracted findings, §3.1). Flowjet-side changes live in the flowjet repo.
- `(mtime, size)` dirty-detection granularity — full hashing is a design-draft non-goal (§20); the real detection hole is the clear-after-snapshot window (F-C2, in scope).
- Native watcher (inotify/FSEvents) enablement — `watchdog` stays unused; stat-scan remains, only moved off the event loop.
- True ETag-conditional manifest writes — RFC constraint 4 sanctions read-then-write fallback; we harden the fallback and align docs.
- Text diff/patch journals, chunking, lazy materialization, bidirectional sync (RFC non-goals).

---

## 2. Architectural Position

No new modules. The remediation keeps the RFC-CORE-010 component set (`WorkspaceManager`, `Workspace`, CAS, dirty tracker, debouncer, uploader, `FsspecSyncBackend`, `WorkspaceStateStore`) and the library boundary: `soothe.workspace.sync` is consumed by downstream hosts (flowjet today) via `WorkspaceManager`/`Workspace`.

Three RFC text amendments are *proposed* (task D2); until accepted, the guide records them as declared deviations — no behavior contradicts the RFC's normative requirements:

1. RFC Package Placement lists "Workspace lifecycle RPCs | soothe-daemon" — stale; the lifecycle is owned by downstream host apps (flowjet). 
2. RFC Security S3 names `fs.use_cache = False` as the credential-isolation mechanism — verified no-op (F-C8).
3. RFC Flow 1 step 2 names "hardlink" as a materialization mechanism — conflicts with invariant 1 (blob immutability); this guide resolves in favor of the invariant (F-C5, §4.6).

Dependency direction is unchanged; `fsspec` stays a `soothe` (host) dependency.

---

## 3. Finding Register

### 3.1 Retracted findings (not defects)

| Retracted claim | Reason |
|-----------------|--------|
| "Daemon never drives the workspace lifecycle — pipeline inert" | By design: the subsystem targets downstream host apps. flowjet drives the full lifecycle (§1.1). The daemon's `_open_sync_workspace` constructing a manager is an unused-but-harmless vestige; not remediated here. |
| "Per-loop opt-in broken in soothe-daemon (`_get_workspace_manager` RuntimeError with `enabled: false`)" | Daemon-specific; the daemon is not a consumer. The README text describing per-loop daemon opt-in is stale docs (folded into D2's amendment list). |

### 3.2 Library lifecycle (P0)

| ID | Sev | Finding | Evidence (file:line @ `454c8d995`) | Fixed by |
|----|-----|---------|--------------------------------------|----------|
| F-L1 | P0 | Debounced auto-checkpointing is inert for **every** consumer: `manager.open()` constructs `DirtyTracker` without `on_dirty` (never calls `debouncer.notify_dirty()`), so `_dirty_since_last` stays `False` — no debounced checkpoint ever fires and `flush()` at close is a no-op. flowjet compensates with a synchronous `ws.checkpoint()` at turn close → **zero crash protection mid-turn** (a long agent turn that dies loses all its writes). Backpressure wiring (`set_pending_count`/`on_backpressure`) equally dead. | `manager.py:132-135`, `manager.py:168-179`, `debouncer.py:88-99`, flowjet `workspace.py:469` | A1 |
| F-L2 | P0 | `manager.open()` never calls `CheckpointManager.initialize()`; manifest state depends on the consumer calling `recover()`. Without recover: first checkpoint builds `Manifest(version=1, artifacts=<this checkpoint's dirty only>)` — drops all prior artifacts (`checkpoint.py:196-202`). flowjet calls `recover()` every open, which instead activates F-C3's snapshot staleness. Either way, restart loses manifest state. | `checkpoint.py:98-127`, `:196-202`, flowjet `workspace.py:389` | A1, B3 |
| F-L3 | P0 | `BackgroundUploader` is dead **and** contract-incompatible: `insert_checkpoint` has no production caller, so the loop polls an empty table every 2 s per open workspace; and the state rows are `{id, timestamp, manifest_hash, status}` with no payload, while `_upload_one` reads `item["checkpoint_id"]`/`item["data"]` → `KeyError` → logged-and-retried forever. flowjet was forced to supply a state-store factory purely because `Workspace.start_tracking()` crashes without one (their comment: `workspace.py:54-58`). | `uploader.py:140-160` vs `sqlite.py:192-197` | A2 |

### 3.3 Correctness (P1)

| ID | Sev | Finding | Evidence | Fixed by |
|----|-----|---------|----------|----------|
| F-C1 | P1 | **[REPRO]** `Workspace.materialize` broken in both directions: signature documents `list[Resource]` but `Materializer.materialize` requires a `Manifest`; flowjet passes a `Manifest` (works until the `len(resources)` logging line raises `TypeError`, after files are materialized). | `workspace.py:96-111`, `materializer.py:49-80`, flowjet `workspace.py:431` | B1 |
| F-C2 | P1 | **[REPRO]** Mid-checkpoint writes lost: the stat-scan poll runs during the checkpoint's awaits, marks new writes dirty and advances `_baseline`; `create_checkpoint()` then `clear()`s the whole dirty set; `_baseline` already includes them so they are never re-detected. `Workspace.checkpoint()` double-clears. Currently dormant in flowjet (turns end before checkpointing) but becomes **live data loss the moment F-L1 is fixed** — the two must co-land. | `checkpoint.py:263`, `workspace.py:267-268`, `dirty_tracker.py:317-343` | B2 |
| F-C3 | P1 | **[LIVE]** Restart data loss via snapshot-stale manifest: `recover()` sets `_manifest` from the SNAPSHOT payload only — post-snapshot DELTA artifacts are absent (up to the last `_max_deltas`-1 turns). DELTA payloads carry no tombstones, so deletions resurrect. **Compounding**: flowjet passes `protected=<snapshot artifacts>` to `materialize_content(reconcile=True)` — the newer (unlisted) artifacts are deleted locally by `_reconcile_content` before `_restore_prior_state` runs. Net effect: after a flowjet restart, the last `N mod 10` turns' artifacts vanish (blobs remain, nothing references them). | `workspace_sync.py:217-252`, `checkpoint.py:236-247`, `:309-350`, `workspace.py:175-176`, flowjet `workspace.py:326-344`, `:358-360` | B3, B13 |
| F-C4 | P1 | `put_manifest` read-then-write guard: when `existing is None` the `if_match` check is skipped entirely (a missing manifest is silently overwritten); protocol docstring says "MUST reject … (conditional write)" while the impl is a TOCTOU. Latent today (single writer per run in flowjet); live with multi-replica hosts. | `fsspec.py:179-213`, `workspace_sync.py:329-349` | B4 |
| F-C5 | P1 | **[REPRO]** CAS `HARDLINK` strategy shares the inode between workspace file and CAS blob — an in-place agent edit corrupts the "immutable" cache; `has_blob` still `True`; later materializations serve corrupted content with no hash check. Violates RFC invariant 1. Live in flowjet whenever `manifest.resources` are materialized and the agent edits `input/` in place. | `cas.py:172-180`, `materializer.py:107-121` | B5 |
| F-C6 | P1 | `CASCache.store_blob` writes a deterministic `<hash>.tmp` → concurrent same-hash stores interleave and one `os.replace` fails; also unsafe across processes sharing the CAS root. | `cas.py:130-136` | B6 |
| F-C7 | P1 | **[REPRO] [LIVE]** `materialize_content` writes via `dest.write_bytes(data)` with no symlink check; a **dangling** symlink in the workspace makes `is_file()`/`exists()` both false and the write follows the link outside the workspace root. Called every turn by flowjet. Violates RFC S2. | `workspace.py:148-166` | B7 |
| F-C8 | P1 | `fs.use_cache = False` is a no-op (s3fs has zero `use_cache` references; fsspec's real switch is `skip_instance_cache`, never passed); `url_to_fs` returns the globally cached instance. RFC S3's mechanism text is ineffective — the intent (credentials never reach the agent process) still holds. | `factory.py:58-63` | B8 |
| F-C9 | P1 | **[LIVE]** Checkpoint identity/growth: stored IDs are `{run_id}-c001` but `_last_checkpoint_id`/`parent_checkpoint_id` use bare `c001`; `list_checkpoints` sorts lexicographically (`c1000` < `c999`); nothing ever prunes checkpoints; `_checkpoint_seq = len(checkpoints)` assumes contiguity. flowjet creates one checkpoint per turn — a long-lived workspace accumulates thousands of objects with O(all) listing per open. | `checkpoint.py:216-261`, `:284-350`, `fsspec.py:219-234` | B9 |
| F-C10 | P1 | **[LIVE, leak half]** Lifecycle hygiene: `Workspace.close()` never calls `debouncer.stop()` → the 20 Hz `_max_interval_loop` task leaks per `manager.close(run_id)` — one leaked task per turn until process exit. `start_tracking`/`checkpoint`/`publish` deref possibly-`None` components (flowjet documented the `start_tracking` crash workaround). `open_from_uri` mutates the singleton manager's `_backend` (latent — flowjet uses `manager.open`). | `manager.py:197-212`, `workspace.py:240-247`, `:267`, `:289`, `:296-327` | A3, B10 |
| F-C11 | P1 | `SqliteWorkspaceStateStore` shares one connection across `asyncio.to_thread` workers (`check_same_thread=False`, no lock) — `sqlite3.threadsafety == 1` forbids connection sharing by contract. Low exposure today (per-workspace DB, effectively serialized polling). | `sqlite.py:63-83` | B11 |
| F-C12 | P1 | `stream_blob`/`stream_checkpoint` claim 64 KiB streaming but `cat_file` the whole object then slice; missing objects `return` silently while the protocol default raises `KeyError`. No production caller. | `fsspec.py:297-319` | B12 |
| F-C13 | P1 | Reconcile protection is consumer-supplied only: `_reconcile_content` deletes every local file absent from the remote content tree; `protected` defaults to `()`. flowjet mitigates — but passes the stale snapshot's artifacts (compounding F-C3). `publish_artifact` ignores `content_type` and always overwrites despite the idempotency contract. | `workspace.py:175-176`, `:193-217`, `fsspec.py:263-291` | B13 |

### 3.4 Performance (P2)

| ID | Sev | Finding | Evidence | Fixed by |
|----|-----|---------|----------|----------|
| F-P1 | P2 | `create_checkpoint` strictly sequential: per new file ~2 HEAD + 2 PUT + bucket probes; `read_bytes`/SHA-256/CAS writes run on the event loop; full bytes resident in memory. Runs at every turn close in flowjet. | `checkpoint.py:139-195` | C2 |
| F-P2 | P2 | `put_blob` does an internal `exists` HEAD duplicating the caller's `head_blob`; `_ensure_parent_dir` calls `makedirs` on every write (cloud prefixes are implicit; s3fs `_mkdir` cold-probes the bucket and can even attempt `create_bucket`). | `fsspec.py:104-113`, `:137-159` | C1 |
| F-P3 | P2 | **[LIVE]** `materialize_content`: full GET per file *including unchanged ones* (downloads remote bytes just to hash-compare), second GET for changed files, sequential loop, blocking `read_bytes`/`write_bytes` on the event loop, full `rglob` reconcile. Flowjet's docstring expects "sha-skip keeps repeat turns cheap" — the implementation downloads the entire content tree every turn. | `workspace.py:143-177`, `:203-238`, flowjet `workspace.py:299-306` | C3 |
| F-P4 | P2 | Deletions cost 2 sequential RTTs each (`exists` + `rm_file`). | `fsspec.py:375-386` | C1 |
| F-P5 | P2 | `Materializer`: one task per manifest entry (unbounded), full-file hash on every materialize, `gather` without `return_exceptions`, synchronous `os.walk` symlink scan on the event loop. | `materializer.py:49-80`, `:92-95`, `:125-139` | C4 |
| F-P6 | P2 | `initialize`/`recover` each do a full `ls` + one GET per checkpoint until the snapshot (done twice, no pruning → O(all checkpoints ever)). | `checkpoint.py:98-127`, `:284-350` | B9, C5 |
| F-P7 | P2 | Dirty tracker: full `os.walk` + `lstat` every 2 s synchronously on the event loop per open workspace; debouncer wakes 20×/s (`sleep(0.05)`); uploader polls the state DB every 2 s when idle. | `dirty_tracker.py:307-379`, `debouncer.py:146-161` | C6 |
| F-P8 | P2 | Whole-object buffering everywhere (`cat`/`cat_file`/`read_bytes`). | `fsspec.py:129-135` | B12 (doc) |

---

## 4. Design Decisions

### 4.1 Library lifecycle completeness (F-L1, F-L2)

`WorkspaceManager.open()` becomes the one-stop lifecycle entry so every consumer gets correct behavior without glue:

- Constructs `DirtyTracker(on_dirty=debouncer.notify_dirty, watched_subdirs=…)` and `BackgroundUploader(on_backpressure=debouncer.set_pending_count)`.
- Calls `await checkpoint_mgr.initialize()` after construction (loads remote manifest + counters; cheap, idempotent).
- `Workspace.start_tracking()` null-guards optional components so a state-store-less consumer no longer crashes (flowjet's workaround becomes unnecessary).

**Co-landing rule**: F-L1 makes the debouncer fire mid-turn, which activates F-C2's lost-write window. B2 must land in the same change or before.

### 4.2 Recovery semantics (F-L2, F-C3)

`recover()` treats the **remote manifest** (`get_manifest`) as the authoritative artifact state and uses checkpoint replay only to rebuild the dirty set; the snapshot manifest remains the fallback when the remote manifest is missing. Tombstones apply to the dirty-set reconstruction. `manager.open()` → `initialize()` covers the no-recover path, so a fresh process never resets the manifest to `version=1`.

### 4.3 Dirty-state acknowledgment (F-C2)

`DirtyTracker.clear()` gains `clear(acked_paths: Iterable[str] | None)`: `create_checkpoint()` passes exactly the dirty + deleted paths captured at snapshot time; anything the poll loop marks *during* the upload survives for the next checkpoint. `Workspace.checkpoint()` drops its redundant second `clear()`. The §1.2 repro becomes the regression test.

### 4.4 Deletions (F-C3)

`CheckpointPayload` gains `deleted_files: list[ManifestEntry] = []` (tombstones), populated for DELTA payloads; `recover()` applies them. Deletions become representable and no longer resurrect on recovery.

### 4.5 Manifest concurrency (F-C4)

Keep read-then-write (RFC constraint 4 sanctions the fallback) but close the hole: when `if_match` is provided and `existing is None`, raise `ConcurrentModificationError` (expected a manifest, found none). The protocol docstring's "conditional write" phrasing is relaxed to "version guard". True ETag `If-Match` is a follow-up, not in scope.

### 4.6 CAS immutability (F-C5)

The materialization strategy chain becomes **reflink → copy**; `HARDLINK` is removed as a materialization strategy (the enum value may remain for `LinkStrategy` capability reporting, but `materialize()` never links). Rationale: RFC invariant 1 outranks hardlink disk savings; reflink keeps the savings on CoW filesystems (APFS/btrfs/XFS). If disk usage matters on non-CoW filesystems later, add hash-verification on CAS materialization instead of reintroducing hardlinks.

**Spec deviation (declared):** RFC-CORE-010 Flow 1 step 2 itself names "hardlink/reflink to workspace", so the current code follows the RFC *letter* while violating its invariant 1 — the RFC is internally inconsistent on this point. This guide resolves the conflict in favor of the invariant; Flow 1's wording is part of the D2 amendment.

### 4.7 fsspec instance handling (F-C8)

Delete the `fs.use_cache = False` mutation (verified no-op) and correct the factory docstring: instances ARE shared via fsspec's cache keyed on storage options (desirable — identical config across runs); per-run isolation, if ever needed, is `skip_instance_cache=True`. RFC S3 needs the matching amendment (task D2).

### 4.8 Checkpoint identity (F-C9)

IDs zero-pad to 6 (`c000001`), `list_checkpoints` sorts numerically by parsed sequence, `_checkpoint_seq` derives from `max(parsed_seq) + 1`. After each compaction snapshot, checkpoints older than the new snapshot are pruned (keep latest snapshot + its deltas). In-memory `_last_checkpoint_id`/`parent_checkpoint_id` use the same prefixed form as stored IDs.

### 4.9 Uploader repair (F-L3)

The uploader stays (RFC Flow 2 step 5 requires asynchronous persistence) and its contract is repaired: the state stores gain a `data` payload column; `create_checkpoint` writes the serialized payload locally with `status='pending_upload'` **before** any remote call (local durability first), then returns. The background uploader pushes `data` bytes and marks `uploaded`; row keys align on `checkpoint_id`. Crash recovery = startup scan of pending rows (loop already implemented).

### 4.10 `Workspace.materialize` signature (F-C1)

Change the signature to `materialize(manifest: Manifest)` — the shape the only real caller (flowjet) already passes — and fix the logging line accordingly. The `list[Resource]` form has no caller and never worked; it is not kept as an overload.

### 4.11 Streaming (F-C12)

Remove the fake-streaming overrides from `FsspecSyncBackend` so the protocol defaults apply (documented full-buffer behavior, `KeyError` on missing). Less code, honest semantics.

---

## 5. Remediation Tasks

### Phase A — Library lifecycle (P0)

#### Task A1: One-stop lifecycle in `manager.open()`

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/manager.py`, `packages/soothe/src/soothe/workspace/sync/workspace.py`, `packages/soothe/src/soothe/workspace/sync/checkpoint.py`
**Guide Section**: §4.1, §4.2
**Description**: Wire `on_dirty`→`debouncer.notify_dirty` and `on_backpressure`→`debouncer.set_pending_count` in `manager.open()`; call `await checkpoint_mgr.initialize()`; null-guard `start_tracking()`/`checkpoint()`/`publish()` against missing components.
**Deliverables**: debounced auto-checkpointing fires for all consumers (F-L1); manifest state initialized without consumer glue (F-L2 half).
**Dependencies**: **B2 must co-land or precede** (activating the debouncer activates F-C2).

#### Task A2: Repair the background uploader contract

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/state/sqlite.py`, `packages/soothe/src/soothe/workspace/state/postgres.py`, `packages/soothe/src/soothe/workspace/state/protocol.py`, `packages/soothe/src/soothe/workspace/sync/checkpoint.py`
**Guide Section**: §4.9
**Description**: Add `data` (payload bytes) to checkpoint rows; `create_checkpoint` inserts the pending row before remote calls; uploader reads `checkpoint_id`/`data` matching the new schema.
**Deliverables**: local-durability-then-async-push per RFC Flow 2; uploader no longer dead/incompatible (F-L3).
**Dependencies**: B2.

#### Task A3: Stop the debouncer on workspace close

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/workspace.py`
**Guide Section**: §3.3 F-C10
**Description**: `Workspace.close()` calls `debouncer.stop()` before state-store close.
**Deliverables**: per-turn 20 Hz task leak eliminated (F-C10 live half).
**Dependencies**: none.

### Phase B — Correctness (P1)

#### Task B1: `Workspace.materialize(manifest: Manifest)` **[REPRO-backed]**

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/workspace.py`
**Guide Section**: §4.10
**Description**: Accept a `Manifest` (flowjet's actual call shape); fix the logging line; no `list[Resource]` overload.
**Dependencies**: none.

#### Task B2: Snapshot-ack dirty clear **[REPRO-backed]**

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/dirty_tracker.py`, `packages/soothe/src/soothe/workspace/sync/checkpoint.py`, `packages/soothe/src/soothe/workspace/sync/workspace.py`
**Guide Section**: §4.3
**Description**: `clear(acked_paths)` clears only acknowledged paths; `create_checkpoint` passes its captured dirty+deleted sets; remove `Workspace.checkpoint()`'s second `clear()`.
**Dependencies**: none.

#### Task B3: Delta tombstones + recovery from remote manifest **[LIVE]**

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/workspace_sync.py` (`CheckpointPayload.deleted_files`), `packages/soothe/src/soothe/workspace/sync/checkpoint.py`
**Guide Section**: §4.2, §4.4
**Description**: DELTA payloads carry `deleted_files`; `recover()` prefers `get_manifest()` for artifact state, applies tombstones to the dirty-set replay; snapshot manifest only as fallback.
**Deliverables**: restart no longer loses post-snapshot artifacts (F-C3 core).
**Dependencies**: none.

#### Task B4: Manifest version guard — close the `None` hole

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/backends/fsspec.py`, `packages/soothe/src/soothe/workspace/sync/workspace_sync.py` (docstring)
**Guide Section**: §4.5
**Description**: `if_match is not None and existing is None` → raise `ConcurrentModificationError`; relax the protocol docstring to "version guard".
**Dependencies**: none.

#### Task B5: Remove hardlink materialization **[REPRO-backed]**

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/cas.py`
**Guide Section**: §4.6
**Description**: `materialize()` uses reflink → copy only.
**Dependencies**: none.

#### Task B6: Unique CAS temp files

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/cas.py`
**Description**: `store_blob` temp path gains pid + thread id + random suffix; cleanup on failure unchanged.
**Dependencies**: none.

#### Task B7: Symlink guard in `materialize_content` **[REPRO-backed, LIVE]**

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/workspace.py`
**Guide Section**: RFC S2
**Description**: Before `write_bytes`, `os.lstat` the destination; any symlink (dangling or not) is unlinked first, mirroring `Materializer`'s behavior.
**Dependencies**: none.

#### Task B8: Remove the `fs.use_cache` no-op

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/factory.py`
**Guide Section**: §4.7
**Description**: Delete the mutation; correct the docstring (shared-instance reality, `skip_instance_cache` as the real switch).
**Dependencies**: none.

#### Task B9: Checkpoint identity, ordering, pruning

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/checkpoint.py`, `packages/soothe/src/soothe/workspace/sync/backends/fsspec.py` (delete-checkpoint helper for pruning)
**Guide Section**: §4.8
**Description**: 6-digit IDs, numeric sort, `max(parsed)+1` seq, prune pre-snapshot checkpoints after compaction, consistent prefixed in-memory IDs.
**Dependencies**: B3 (payload shape settled first).

#### Task B10: Remaining lifecycle hygiene (latent)

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/manager.py`
**Guide Section**: §3.3 F-C10
**Description**: `open_from_uri` no longer mutates `self._backend` (backend becomes an explicit per-open argument); `close_all` logs instead of raising mid-loop.
**Dependencies**: none.

#### Task B11: Serialize SQLite state-store access

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/state/sqlite.py`
**Description**: `threading.Lock` around `_execute` (single connection retained).
**Dependencies**: none.

#### Task B12: Remove fake-streaming overrides

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/backends/fsspec.py`
**Guide Section**: §4.11
**Description**: Drop `stream_blob`/`stream_checkpoint` overrides; protocol defaults apply.
**Dependencies**: none.

#### Task B13: Default reconcile protection

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/workspace.py`
**Guide Section**: §3.3 F-C13
**Description**: `materialize_content` internally extends `protected` with the current dirty set and the in-memory manifest artifact paths (callers may add more — flowjet's stale-snapshot protection stops being the only line of defense). `publish_artifact` records the `content_type` deviation in its docstring.
**Deliverables**: compounding leg of the F-C3 chain closed even when consumers pass stale protection.
**Dependencies**: A1 (dirty set available), B3.

### Phase C — Performance (P2)

#### Task C1: Backend round-trip reduction

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/backends/fsspec.py`
**Guide Section**: §3.4 F-P2, F-P4
**Description**: `put_blob` drops its internal `exists` (callers gate via `head_blob`; content-addressed PUT is idempotent). `_ensure_parent_dir` runs only for directory-bearing filesystems (local/memory). `delete_content_file` drops the `exists` pre-check.
**Dependencies**: none.

#### Task C2: Concurrent, off-loop checkpoint uploads

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/checkpoint.py`
**Guide Section**: §3.4 F-P1
**Description**: Per-file work (read + hash + CAS store + blob put + content mirror) under a bounded `asyncio.Semaphore(8)`; `read_bytes`/SHA-256/CAS writes via `asyncio.to_thread`. Content-mirror deletions batched with the same semaphore.
**Dependencies**: B2, A2.

#### Task C3: `materialize_content` incremental compare **[LIVE]**

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/workspace.py`, `packages/soothe/src/soothe/workspace/sync/backends/fsspec.py` (size via `fs.info`)
**Guide Section**: §3.4 F-P3
**Description**: Compare local vs remote by size first (cheap `fs.info`), full GET only on mismatch; single fetched buffer (no double GET); concurrent fetches under the same semaphore; `read_bytes`/`write_bytes`/`rglob` via `asyncio.to_thread`.
**Deliverables**: repeat turns stop re-downloading the content tree.
**Dependencies**: B7.

#### Task C4: Materializer hardening

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/materializer.py`
**Guide Section**: §3.4 F-P5
**Description**: `gather(..., return_exceptions=True)` with per-entry error collection; `_remove_escaping_symlinks` via `asyncio.to_thread`.
**Dependencies**: none.

#### Task C5: Checkpoint listing cost

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/checkpoint.py`
**Guide Section**: §4.8
**Description**: Covered by B9 (pruning bounds the list); `initialize` and `recover` share one listing pass per open.
**Dependencies**: B9.

#### Task C6: Idle-overhead reduction

**Action**: Modify
**File(s)**: `packages/soothe/src/soothe/workspace/sync/debouncer.py`, `packages/soothe/src/soothe/workspace/sync/dirty_tracker.py`
**Guide Section**: §3.4 F-P7
**Description**: Max-interval loop sleeps 0.5 s instead of 0.05 s; `_scan_workspace` runs via `asyncio.to_thread`.
**Dependencies**: none.

### Phase D — Verification and documentation

#### Task D1: Full verification pass

**Action**: Verify
**File(s)**: n/a
**Description**: `pytest packages/soothe/tests/unit/workspace/ packages/soothe/tests/unit/context/test_recovery.py` green; `ruff check` + `ruff format` clean on touched modules; `./scripts/verify_finally.sh` green.
**Dependencies**: all.

#### Task D2: RFC amendment + changelog

**Action**: Modify
**File(s)**: `.platonic/specs/coreagent/RFC-CORE-010-workspace-sync-materialization.md` (via specs-refine), `CHANGELOG.md`
**Guide Section**: §2, §4.5–§4.7
**Description**: Amend: (a) Package Placement row "Workspace lifecycle RPCs | soothe-daemon" → downstream host apps (flowjet) + drop the stale per-loop daemon opt-in docs; (b) S3 mechanism text (`fs.use_cache` → skip_instance_cache/shared-instance statement); (c) constraint-4 fallback wording; (d) Flow 1 step 2's "hardlink/reflink" wording (§4.6 declared deviation). CHANGELOG entry per release-and-governance rules (no AI co-authors).
**Dependencies**: D1.

---

## 6. Testing Strategy

### 6.1 Regression matrix (each repro/chain becomes a pinned test)

| Defect | Test | Location |
|--------|------|----------|
| F-C1 materialize signature | `Workspace.materialize(manifest)` round-trip on `MemoryFileSystem` (both the list-form rejection and the Manifest form) | new `test_workspace_handle.py` |
| F-C2 mid-upload write loss | slow-backend checkpoint; write during upload; assert file in next manifest or still dirty | `test_checkpoint.py` |
| F-C3 restart data loss chain | checkpoint N turns (deltas post-snapshot) → `manager.open` + `recover()` + `materialize_content(reconcile=True, protected=<snapshot artifacts>)` → assert all turn artifacts present | `test_checkpoint.py` + `test_content_mirror.py` |
| F-C5 CAS corruption | force non-CoW strategy; in-place edit; assert CAS blob bytes unchanged | `test_cas.py` |
| F-C7 symlink escape | dangling symlink + fake content backend; assert nothing written outside root | `test_content_mirror.py` |
| F-L3 uploader schema | pending row with payload → uploader drains → status `uploaded` | `test_uploader.py`, `test_sqlite_state.py` |
| F-L1 debouncer wiring | `manager.open()` + file write → debounced checkpoint fires without consumer glue | `test_manager.py` |
| F-C2 ack clear | poll during checkpoint; assert surviving dirty entries | `test_dirty_tracker.py` |
| F-C3 tombstones | delete file → delta → recover; assert file absent | `test_checkpoint.py` |
| F-C4 None-hole | `if_match` + missing manifest → `ConcurrentModificationError` | `test_fsspec_backend.py` |
| F-C9 IDs | >999 IDs order; pruning keeps snapshot + deltas | `test_checkpoint.py` |
| F-C10 debouncer leak | open → start_tracking → `manager.close(run_id)` → assert no pending tasks (`asyncio.all_tasks()` diff) | `test_manager.py` |
| F-C11 sqlite | concurrent upserts via `asyncio.gather` → no exceptions | `test_sqlite_state.py` |

### 6.2 Consumer-contract integration test (mirrors flowjet)

A soothe-side test drives the exact §1.1 sequence against `MemoryFileSystem`: `WorkspaceManager(state_store_factory, content_backend_factory, watched_subdirs)` → `open` → `recover` → `materialize_content(protected, reconcile)` → `materialize(manifest)` → `start_tracking` → agent writes → `checkpoint` → `manager.close` → re-`open` → verify artifacts survive. This pins the API shape flowjet depends on (including the kwargs flowjet signature-probes at `workspace.py:366-379`).

### 6.3 Call-count instrumentation (performance assertions)

Wrap `MemoryFileSystem` in a counting subclass to assert: `put_blob` issues no `exists` after caller `head_blob`; `materialize_content` issues one GET per changed file and zero per unchanged (size-equal) file; `delete_content_file` is single-call.

### 6.4 Live integration

Existing `tests/integration/workspace/sync/test_s3_integration.py` (env-gated live S3/MinIO) gains the consumer-contract round-trip (§6.2) against real storage.

---

## 7. Verification Checklist

- [x] All Phase A–C tasks completed (see §8 implementation record)
- [x] §6.1–§6.2 regression tests green (201 workspace unit tests; consumer-contract test in `test_manager.py::TestConsumerContract`)
- [x] Full unit suites green (3264 passed / 16 skipped); `ruff` clean
- [x] Live S3 integration green against Tencent COS (24 tests) + 64 MiB streaming smoke (bounded-memory assertions)
- [ ] Flowjet compatibility confirmed against the §1.1 contract (consumer repo CI or manual pass)
- [ ] RFC-CORE-010 amendments accepted or deviations recorded
- [x] CHANGELOG entry added

---

## 8. Implementation Record (2026-11-26)

All Phase A/B/C tasks landed plus two user decisions taken mid-implementation:

**D-A (checkpoint persistence)** — Option **A** adopted: the object store stays the durable source of truth; the state DB (sqlite/PG) stages pending rows. `create_checkpoint` inserts `status='pending_upload'` rows with the serialized payload **before** any remote call, writes the manifest synchronously (authoritative, `if_match`-guarded), and returns; the repaired background uploader pushes payload bytes, calls the new atomic `mark_checkpoint_uploaded` (sets `uploaded` + clears `data`), and prunes checkpoints superseded by a pushed snapshot. When no state store is configured the payload is pushed synchronously (no staging row, no uploader). `initialize()` merges pending-row sequence numbers into the next ID so a crashed process's checkpoint is never overwritten.

**D-S (streaming)** — Supersedes §4.11: **true chunked streaming** was implemented instead of removing the streaming overrides. `put_blob_from_file`/`put_content_file_from_file` (backend capability methods, dispatched via `sync/streaming.py` helpers with buffered fallbacks for duck-typed backends) and `stream_blob`/`stream_content_file`/`stream_checkpoint` (ranged reads on s3) give bounded-memory transfers for MB–GB files; `CASCache.cache_file` hashes while copying in a single pass and `fetch_and_cache` streams downloads with a running hash. Materialization and content restore (`workspace.materialize_content`) stream through temp files with `os.replace` (which also closes the F-C14 symlink escape: a replace never follows a symlink target).

s3fs-specific facts discovered during live COS validation:

- s3fs's `open(path, "wb")` incremental writes and default `put` whole-buffer (< 100 MiB files) both fail or buffer unbounded on chunked-intolerant stores; fixed by explicit `chunksize=8 MiB` + `max_concurrency=2` on `put` (known-length multipart parts).
- s3fs's `open(path, "rb")` + incremental `read` caches the whole remaining object; streaming reads use ranged `cat_file(start, end)` instead.
- botocore ≥ 1.36 default request checksums switch bodies to aws-chunked framing, which Tencent COS rejects (`You must provide the Content-Length HTTP header`); COS configs must set `request_checksum_calculation="when_required"` (end-to-end integrity is provided by the CAS sha256 layer). Recorded in `test_s3_integration.py::_factory_config`.

Deviations from the plan as written: §4.11 replaced by D-S above; `_DIR_BEARING_PROTOCOLS` gating and debouncer wake cadence (0.5 s) as planned; `Workspace.materialize` keeps no resource-list overload per §4.10.

---

## Appendix A: RFC-CORE-010 Requirement / Finding Mapping

| RFC requirement / invariant | Status at `454c8d995` | Restored by |
|------------------------------|----------------------|-------------|
| Guiding principle 4 ("Writes are eventually persisted") | Violated for mid-turn crash protection — debounced checkpointing inert for all consumers (F-L1) | A1, B2 |
| Flow 1 (materialization; "zero object-data bandwidth" for unchanged) | Violated — unchanged files fully downloaded on compare (F-P3). Flow 1's "hardlink" wording conflicts with invariant 1 (§4.6) | C3, D2 |
| Flow 2 steps 1–2 (dirty → debounce → checkpoint) | Violated — steps 1–2 dead at library level (F-L1) | A1 |
| Flow 2 step 5 (background uploader persists asynchronously) | Violated — uploader dead and contract-incompatible (F-L3) | A2 |
| WorkspaceManager capability "recover interrupted runs" | Partially — consumer must call `recover()`; recovery itself loses post-snapshot state (F-L2, F-C3) | A1, B3 |
| Architectural constraint 3 (blobs immutable, idempotent `put_blob`) | Violated by hardlink CAS (F-C5) | B5 |
| Architectural constraint 4 (manifest optimistic concurrency) | Partially — fallback sanctioned, but `None` hole (F-C4) | B4 |
| Security S2 (symlink escape prevention) | Violated in `materialize_content` (F-C7) | B7 |
| Security S3 (credential isolation) | Intent holds; mechanism text wrong (F-C8) | B8, D2 |
| Security S7 (blob integrity) | Weakened — CAS serves corrupted blobs without re-verification (F-C5) | B5 |
| Invariant 6 ("Every remote checkpoint references a complete manifest") | Weakened — recovery uses stale snapshot manifest (F-C3) | B3 |
| Package placement / DAG | Code compliant; RFC's "soothe-daemon" lifecycle row stale | D2 |

## Appendix B: Revision History

| Date | Guide Version | Changes |
|------|---------------|---------|
| 2026-11-26 | IG-CORE-008 Draft | Initial guide from the workspace-sync audit (4 runtime reproductions, environment-pinned verification) |
| 2026-11-26 | IG-CORE-008 Draft (re-scoped) | Consumer clarification: subsystem targets downstream hosts (flowjet), not soothe-daemon. Retracted F-W1/F-W5 (daemon wiring); reframed lifecycle findings as library defects (F-L1–L3); F-C3 upgraded to live restart data-loss chain (snapshot-stale manifest + reconcile compounding); F-C1 fix direction inverted (accept `Manifest`); F-C10 leak half upgraded to live per-turn; F-P3 upgraded to live per-turn full-tree download; added consumer-contract integration test and RFC Package Placement amendment |
