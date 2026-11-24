# IG-XXX: Full-Package Review Remediation (review 8b544d9c2)

## Summary

Fix the 6 Critical + 39 Major + 35 Minor findings from the whole-package static
review of `packages/soothe-daemon` (`.review/8b544d9c2/code-review.md`). No
scope beyond the review's todo list; each fix follows the suggestion recorded
in the report's Section 3/4.

## Design Decisions

1. **RPC authorization (Critical)** — opt-in enforcement tied to the existing
   AKSK identity service, zero behavior change when identity is disabled
   (trusted-local deployments keep working; no config migration).
   - `ClientSession` gains `authenticated: bool` / `auth_user_id: str | None`,
     set by the router's `auth` / `auth_refresh` handlers on success.
   - New `ErrorCode.AUTH_REQUIRED`; router helper `_require_admin_auth`
     returns True when `identity_service is None`, else requires the
     authenticated session. Applied to the admin/destructive set:
     `daemon_shutdown`, `config_reload`, `config_get`, `loop_delete`,
     `skills_list`.
   - Standalone WebSocket channel command dispatch (`cron_*`/`memory_*`)
     checks the same session flag via its `session_manager` (constructor
     gains `identity_enabled: bool`, wired from the daemon by
     `channel_manager`).
   - ACP WebSocket route: fail-closed when identity is enabled — the upgrade
     must carry a valid identity JWT via `?token=` or `x-soothe-token`;
     identity-disabled deployments keep accepting unauthenticated upgrades.
   - `config_get` masks secret-bearing values (`api_key`, `storage_options`,
     `secret*`, `password`, `token*`) with `"***"` before sending.
2. **Feishu session key (Critical)** — the group/topic session key travels in
   `metadata["session_key"]`; `ChannelManager.handle_inbound` uses it as the
   loop routing key when present (validated by the same charset rule as
   `loop_id`), falling back to `chat_id`. The phantom kwarg is removed.
3. **AKSK plaintext once (Critical)** — `AKSKPair` is defined in soothe-nano
   (separate package), so the plaintext rides on a soothe-side subclass
   `CreatedAKSKPair(AKSKPair)` with `secret_key: str | None` populated at
   creation only; the store persists the hash exactly as before and
   `list_aksk` rebuilds plain rows. The CLI prints that value and fails
   loudly when absent. Round-trip test added
   (`packages/soothe/tests/unit/core/security/test_identity_service.py`).
   Promoting the field to nano's `AKSKPair` would need a nano release and is
   left to the release train.
4. **Pool bridge startup race (Critical)** — `ProcessPool.start()` sets
   `_running = True` before spawning workers/warmup wait; the two skipped
   bridge-routing tests are un-skipped as the regression gate.
5. **Stale-worker reaper (Critical)** — positive evidence only: a spawn worker
   is reaped only when its cmdline references the soothe root or a soothe
   marker (`soothe_daemon`/`Soothe`); the bare `"spawn_main"` fallback and the
   bare `"soothe"` substring marker are removed (duplicate marker dropped).
6. **Chunk-loss honesty** — process-pool stream chunk drops mark the stream
   degraded (truncation notice) instead of completing silently.
7. **Reaper/reconcile guards** — cron RUNNING-strand sweep on start; schedule
   reconcile only on timezone change; orphan reconcile paginates `list_loops`
   before deleting; clarification-parked loops win over cancel-by-age.
8. Everything else follows the report's per-finding suggestions (mkstemp fd
   hygiene, `ray.kill` on turn exit, indexer hash-cache-on-success, mutable
   config defaults, unbounded dict caps, docstring/comment hygiene, README and
   Dockerfile metadata fixes).

## Verification

Per-package: `ruff check`/`ruff format`, `mypy`, unit pytest for
`packages/soothe-daemon` (plus `packages/soothe` identity tests for the AKSK
change); then `./scripts/verify_finally.sh` for the monorepo gate before any
commit. Daemon integration tests may hit EADDRINUSE when the local port
18765 is taken by an ssh tunnel — environment noise, not a regression.
