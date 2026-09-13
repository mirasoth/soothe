# Soothe Production Deployment

PostgreSQL + pgvector + soothed daemon.

## Quick Start

```bash
cd config/production
vim example.env   # Set API keys
mkdir -p "$HOME/.soothe-prod/data" "$HOME/.soothe-prod/logs" "$HOME/.soothe-prod/config"
docker compose up -d
```

Or from repo root: `make docker-prod-up` (creates dirs automatically).

**Colima**: If `chown .../.soothe-prod/logs: permission denied`, create the dirs on the host first.

## Environment Variables

All vars in `example.env` are auto-imported into the container via `env_file`.

Required: `DASHSCOPE_BASE_URL`, `DASHSCOPE_API_KEY`, `DS1`..`DS16` `_BASE_URL`/`_API_KEY`

Optional: `TAVILY_API_KEY`, `SOOTHE_WORKSPACE_HOST_ROOT`, `SOOTHE_DEBUG`

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
