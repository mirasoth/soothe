# Soothe Production Deployment

PostgreSQL + pgvector + soothed daemon.

## Quick Start

```bash
cd config/production
vim example.env   # Set API keys (+ S3 sync vars if using s3:// workspaces)
mkdir -p "$HOME/.soothe-prod/data" "$HOME/.soothe-prod/logs" "$HOME/.soothe-prod/config"
docker compose up -d
```

Or from repo root: `make docker-prod-up` (creates dirs automatically).

**Colima**: If `chown .../.soothe-prod/logs: permission denied`, create the dirs on the host first. The same applies to `data/workspaces-sync` (S3-backed workspace materialization) — the daemon creates it, but the host dir must be writable by the container user.

## Environment Variables

All vars in `example.env` are auto-imported into the container via `env_file`.

Required: `DASHSCOPE_BASE_URL`, `DASHSCOPE_API_KEY`, `DS1`..`DS16` `_BASE_URL`/`_API_KEY`

Optional: `TAVILY_API_KEY`, `SOOTHE_WORKSPACE_HOST_ROOT`, `SOOTHE_DEBUG`

External S3 (workspace sync): `SOOTHE_SYNC_ENDPOINT_URL`,
`SOOTHE_SYNC_ACCESS_KEY`, `SOOTHE_SYNC_SECRET_KEY`, `SOOTHE_SYNC_BUCKET`,
`SOOTHE_SYNC_PREFIX`.

## Workspace Sync (external S3)

`nano.yml` `workspace_sync` supplies the external S3 service (endpoint +
credentials via the `SOOTHE_SYNC_*` vars). `enabled` is `false` by default:

- **Per-loop opt-in** (default): clients pass an `s3://bucket/prefix`
  workspace (or `workspace_sync_source`) to `loop_new`; the loop's files
  materialize under `~/.soothe-prod/data/workspaces-sync` and dirty writes
  mirror back to S3. Loops that pass a local path keep the local behavior.
- **Force-on**: set `enabled: true` — every new loop opens the
  `source_uri` S3 tree (local `client_workspace` values are ignored).

Notes:

- Only `s3`, `gs`, `az` schemes are accepted (SSRF allowlist).
- Keep `region_name` out of `storage_options` (s3fs rejects it); for
  AWS-native S3 drop `endpoint_url` and use profile/IAM credentials.
- Unset `SOOTHE_SYNC_*` vars leave literal placeholders that fail loudly
  at sync-open time (per loop, not at startup).
- The image must include `s3fs` (the `soothe[s3]` extra — already the
  case for images built from `packages/soothe-daemon/Dockerfile`).

## Sandbox (os_sandbox, default on)

`nano.yml` enables the `os_sandbox` backend for every agent build
(fail-loud — no host fallback). Tier selection on Linux is `auto`:

| Runtime | Tier engaged | Posture |
|---|---|---|
| userns + mount-ns permitted (bwrap installed) | bubblewrap + seccomp, netns off | `full` |
| Docker default seccomp / blocked userns | Landlock (`allow_degraded: true`) | `subprocess_only` |
| neither available | construction error — the daemon refuses to run unsandboxed | — |

The Landlock tier confines **writes** to the workspace + scratch
(file-access denial only; no network/process isolation). The full bwrap
tier additionally denies network egress and applies a seccomp deny-list.

To enable the full tier on Docker, the runtime must allow user/mount
namespaces (e.g. `security_opt: [seccomp=unconfined]` on capable hosts) —
verify with a `loop_new` smoke and the build log's "Sandbox posture" line.

## Architecture

```
soothe-pgvector (PostgreSQL 17 + pgvector)
├── soothe_checkpoints   → LangGraph state
├── soothe_metadata      → Thread metadata
├── soothe_vectors       → Embeddings
└── soothe_memory        → Long-term memory

soothed (daemon)
└── Port 18765 (WebSocket/HTTP)
```

All services localhost-only. PostgreSQL default credentials (postgres/postgres).

## Operations

| Action | Command |
|--------|---------|
| Status | `docker compose ps` |
| Logs | `docker compose logs soothed` |
| Connect DB | `docker compose exec soothe-pgvector psql -U postgres` |
| Backup | `docker compose exec soothe-pgvector pg_dumpall -U postgres > backup.sql` |
| Stop | `docker compose down` |
| Clean restart | `docker compose down -v && docker compose up -d` |

## Security

- API keys in `example.env` (git-tracked, values from host shell env)
- Localhost binding only
- Docker network isolation
- `os_sandbox` on by default; `security` denies paths outside the workspace
