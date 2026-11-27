# IG-LOOP-020: Structured Audit Trace Replacing conversation.jsonl

**Created**: 2026-11-27
**Status**: Implemented
**Related**: RFC-624/626 (checkpoint slimming), persistence-and-loops.md (Unified Persistence Backend)

## Implementation notes (deviation from the original draft)

- Config keys: `observability.thread_logging_*` lives in PyPI-only
  `soothe_nano.config.ObservabilityConfig`, so the planned rename to
  `observability.audit.*` is **deferred** — retention (`thread_logging_retention_days`)
  and the size budget (`thread_logging_max_size_mb`) keep the existing nano keys.
  Audit-specific knobs (`_MAX_ARG_BYTES` = 256 KiB, `_RESULT_CONTENT_LIMIT` = 2000) are
  module constants in `thread_audit_logger.py` until nano grows config fields for them.
- SQLite path: `$SOOTHE_DATA_DIR/databases/audit.db` via nano's generic
  `resolve_sqlite_db_path("audit")` (no nano change needed).
- Thread deletion test fixture (`test_thread_deletion.py`) no longer seeds a
  `conversation.jsonl`.

## Problem

`conversation.jsonl` (`soothe/logging/thread_logger.py`) is a per-thread JSONL audit tail:
unindexed, args truncated to 500 chars, results truncated to 2000 chars, no thinking
records, retention by mtime sweep (`cleanup_stale_thread_logs`). Investigation of a step
(what did the model think, which tool, with what args) requires hand-rolling grep over
flat lines or digging into the langgraph checkpointer blob. There is no queryable
step-level audit surface.

## Goal / acceptance criteria

1. **Structured audit trace** replaces `conversation.jsonl` entirely — JSONL is deleted,
   not dual-written. No compat shim, no export flag (Dead Code & Legacy Removal rule).
2. **Full fidelity where it matters**: tool call args stored **untruncated** (configurable
   hard cap for pathological payloads); thinking/reasoning captured from
   `additional_kwargs`; tool **results truncated to 2000 chars** — results are not audit
   material, the langgraph checkpointer holds full `ToolMessage`s and compensates.
3. **Queryable**: reads go through the audit store (per-thread trace fetch, stats,
   last-human-message) — `read_recent_records` and its three
   `_thread_manager.py` consumers migrate to store queries.
4. **Unified backend compliance**: branches on `persistence.default_backend` — SQLite
   mode → `$SOOTHE_HOME/data/` SQLite file; Postgres mode → database under
   `postgres_databases`. Never JSONL-as-database.
5. **Audit is derived, never authoritative**: the store is append-only and read-only;
   resumption/replay stays with checkpointer + Context Engine. Audit write failure is
   fail-soft (debug log, loop continues) — same semantics as today (`thread_logger.py:193-194`).

## Data model

Single store, one table:

```sql
CREATE TABLE audit_records (
    seq        INTEGER  -- per-thread monotonic, (thread_id, seq) unique
    thread_id  TEXT NOT NULL,
    ts         TEXT NOT NULL,            -- UTC ISO-8601
    kind       TEXT NOT NULL,            -- conversation | tool_call | tool_result | thinking | event
    phase      TEXT | NULL,              -- goal_completion, plan_direct, ...
    namespace  JSONB/JSON | NULL,        -- stream namespace tuple
    payload    JSONB/JSON NOT NULL       -- kind-specific dict, see below
);
-- index (thread_id, seq); index (thread_id, kind) for filtered fetch
```

Payload by kind:

| kind | payload |
|---|---|
| `conversation` | `{"role": "user"\|"assistant", "text": str}` |
| `tool_call` | `{"tool_name": str, "args": <full JSON>}` |
| `tool_result` | `{"tool_name": str, "content": str(truncated 2000)}` |
| `thinking` | `{"text": str, "source": "additional_kwargs"}` |
| `event` | `{"classification": str, "data": dict}` |

**Tool args full storage**: serialized args JSON written verbatim. Hard cap
`observability.audit.max_arg_bytes` (default 256 KiB) guards against pathological
payloads; over-cap args are stored truncated with a `"truncated": true` marker.

**Tool results stay truncated**: `_LOG_CONTENT_LIMIT = 2000` semantics carried over.
Full `ToolMessage` content remains in the checkpointer state — audit deliberately does
not duplicate it (storage volume control; the duplicate would double-checkpointer-sized
storage with no investigation value).

**Thinking capture**: extracted from `AIMessage.additional_kwargs` reasoning/thinking
blocks at the interception point (same source the TUI reads at
`_runner_strange_loop.py:394-412`), recorded once per assistant turn on the terminal
`AIMessage` — streaming thinking deltas are not accumulated (decision: terminal-only;
revisit if post-mortems need streaming-order evidence).

## Single interception point

No new hooks. `ThreadLogger._log_message_event` / `log_user_input` /
`log_assistant_response` / `log()` already receive every recordable signal from the
stream path. The class becomes `ThreadAuditLogger` (rename — JSONL identity is gone)
writing to the store instead of `_write_record` JSONL append. Buffering + `flush()`
(100 records / 1.0s, stream-finally flush at `query/engine.py`) carried over unchanged.

## Placement & boundaries

- New module `soothe/logging/audit_store.py` (host package `soothe`; **not** nano —
  nano independence rule).
- `AuditStore` sqlite/postgres implementations behind the persistence factory pattern
  (class-based, per `SqliteRuntimeRegistry`/`PostgresPoolRegistry` precedent).
- Postgres mode: dedicated database registered in `postgres_databases` (e.g. `audit`).
- Config: `observability.thread_logging_*` keys replaced by `observability.audit.*`
  (`retention_days`, `max_arg_bytes`, `result_truncate_chars=2000`). Old keys deleted,
  all call sites updated in the same change.

## Retention

Same policy, store-native: delete records `ts < now - retention_days` (default 30),
plus total-size budget deletion oldest-first. Replaces `cleanup_stale_thread_logs`
mtime sweep and the 100 MiB JSONL budget. Thread-status-aware extension (keep failed
threads longer) is **out of scope** — noted for a follow-up if post-mortems demand it.

## Changes by phase

### Phase 1 — `soothe` (store + writer)

| File | Change |
|---|---|
| `soothe/logging/audit_store.py` (new) | `AuditStore` protocol + sqlite/pg implementations, `append(thread_id, record)`, `fetch(thread_id, *, kinds, limit, offset)`, `stats(thread_id)`, `last_human_message(thread_id)`, retention sweep. |
| `soothe/logging/thread_logger.py` | Renamed/reworked to `ThreadAuditLogger`: JSONL buffer/flush/dir logic deleted, writes delegate to `AuditStore`; args truncation (500) deleted → full args; thinking extraction added. |
| `soothe/runner/_thread_manager.py` | `get_thread_messages` / `get_thread_stats` / `get_last_human_message` (`:487/:563/:638`) read from `AuditStore`; `ThreadLogger` init (`:140`) → audit logger. |
| `soothe/config` | `observability.thread_logging_*` → `observability.audit.*`. |

### Phase 2 — `soothe-daemon`

| File | Change |
|---|---|
| `query/engine.py` | Logger construction (`:1123`), `log_user_input` (`:1218`), suppression flag, stream-finally `flush()` — call sites updated, no behavior change. |
| retention wiring | `cleanup_stale_thread_logs` call sites → store retention sweep. |

**No query surface in this change.** The audit trace is write-only from the products'
perspective: record, retain, nothing else. The three internal readers in
`_thread_manager.py` (thread messages / stats / last human message) migrate to store
queries only because their current data source is the deleted JSONL — they are existing
internal functionality, not a new query product. External analysis tooling (trace
inspectors, CLI, dashboards) is deliberately deferred to a separate design.

### Phase 3 — cleanse (same change, per rule 6/19)

- Delete `conversation.jsonl` writing/reading path entirely: `_write_record`,
  `_flush_buffer` JSONL branches, `read_recent_records`, `log_path`,
  `cleanup_stale_thread_logs`.
- Tests: `tests/unit/logging/test_thread_logger_*` rewritten against the store;
  `soothe-daemon` tests that mock `ThreadLogger.read_recent_records` migrated.
- Docs: `docs/wiki/howto_debug.md` §3 (`:183-207`) and log-structure sections
  (`:421-433`, `:506-508`) rewritten to describe the audit trace.

## Verification

`./scripts/verify_finally.sh` green; new unit tests cover: full-args persistence,
2000-char result truncation, thinking extraction, per-thread seq monotonicity, sqlite+pg
store parity (pg behind existing test-DSN gating), retention sweep, fail-soft on store
error.

## Resolved decisions (operator, 2026-11-27)

1. **Postgres mode**: the audit store gets its **own dedicated `audit` database**,
   registered under `postgres_databases` — not shared with any existing daemon-owned
   database.
2. **No query surface**: record-only. External analysis tooling is a separate future
   design; no daemon protocol command, no CLI command in this change.
