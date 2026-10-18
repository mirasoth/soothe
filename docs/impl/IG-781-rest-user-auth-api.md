# IG-781: REST API for User Management with Authentication

**Implementation Guide**: IG-781
**Title**: REST API for User Management with Authentication
**Status**: Draft
**Created**: 2026-10-18
**Dependencies**: §7b (Package Boundaries), Rule 10 (Unified Persistence), Rule 16 (Minimum-Exposure), IG-770 (unified FastAPI app)
**Related**: step PHW-01

---

## Problem

Soothe's identity capabilities — users, AKSK pairs, JWT access/refresh tokens —
are reachable today only through two surfaces:

1. **WebSocket wire**: `AuthHandler` (`soothe_daemon/server/auth_handler.py`)
   handles `auth` / `auth_refresh` messages for WS clients.
2. **CLI**: `soothe_daemon/identity_cli.py` exposes `soothed identity create-user`,
   `list-users`, `delete-user`, `create-aksk`, `list-aksk`, `revoke-aksk`, … to
   operators.

There is **no HTTP/REST surface**. HTTP integrators (curl, web backends, CI
provisioning scripts) cannot create users, mint AKSK, or authenticate without
speaking the WebSocket wire protocol or shelling out to the CLI. The daemon
already runs a unified FastAPI app (`ChannelManager._unified_app`, created when
WebSocket or ACP-WS is enabled) that today only exposes `/healthz` and WS
routes — it has no REST router.

## Goal

Expose the existing `IdentityService` (`packages/soothe/src/soothe/identity/`)
as a versioned REST API on the daemon's unified FastAPI app, so that:

- User management (create / list / get / delete) is callable over HTTP.
- AKSK lifecycle (create / list / revoke) is callable over HTTP.
- Authentication (AKSK → JWT token pair) and token refresh work over HTTP,
  mirroring the existing WS `AuthHandler` behavior.
- Token validation is exposed as a reusable dependency for guarding other
  future REST routes.

**Non-goal**: password/username login flow. Soothe's identity model is
AKSK-based (access key + secret key → JWT), not username/password. This plan
does not introduce a password store. (See Alternatives Considered.)

## Existing Assets (reuse — do NOT reimplement)

| Asset | Location | Reuse |
|-------|----------|-------|
| `IdentityService` | `packages/soothe/src/soothe/identity/identity_service.py` | All REST handlers delegate to its public methods (`create_user`, `list_users`, `get_user`, `delete_user`, `create_aksk`, `list_aksk`, `revoke_aksk`, `authenticate`, `refresh_token`, `validate_token`, `revoke_token`, `get_status`) |
| `IdentityProtocol` | `soothe_nano.protocols.identity` | Type the router against the protocol, not the concrete class |
| Models (`User`, `AKSKPair`, `AuthResult`, `TokenRefreshResult`, `TokenClaims`, `TokenInfo`, `IdentityStatus`) | `soothe_nano.protocols.identity` | Pydantic response schemas (already v2 models) |
| `AuthHandler` | `soothe_daemon/server/auth_handler.py` | HTTP auth endpoint reuses the same error-message dicts and result→response shape; share the dict constants, do not duplicate |
| Unified FastAPI app | `soothe_daemon/channel_manager.py:352` | Register the REST router on `self._unified_app` |
| `IdentityConfig` | `soothe.identity.runtime` (via `soothe_daemon/config/settings.py:59`) | Gate the router on `config.identity.enabled` |
| `PyJWT`, `cryptography` | already in `soothe-daemon` deps | No new dependencies |

**Ecosystem check (Rule 3)**: FastAPI + Pydantic v2 are already pinned
(`fastapi>=0.104`, `pydantic>=2.0`). No new third-party auth library is
introduced. `IdentityService` is the single source of truth for credentials,
token signing, and revocation — the REST layer is a thin transport adapter,
exactly the pattern IG-770 established for ACP.

## Package Placement (§7b)

All new code lives in **`soothe-daemon`**. Rationale:

- The REST router is a *transport* concern: it maps HTTP ↔ `IdentityService`
  calls, just as `AuthHandler` maps WS ↔ `IdentityService`. Transports are a
  daemon responsibility (placement table: "HTTP/WS server, admin IO" →
  `soothe-daemon`).
- `soothe-daemon` may import `soothe` (where `IdentityService` lives) and
  `soothe-nano` (where the protocol/models live). DAG arrow is respected.
- `soothe` itself gains **nothing** — the identity service is already complete
  there. No host-axis changes.

| New component | Package / path |
|---------------|----------------|
| REST router (user + AKSK + auth + token endpoints) | `packages/soothe-daemon/src/soothe_daemon/server/rest/identity_router.py` |
| Request/response Pydantic schemas (HTTP-specific only) | `packages/soothe-daemon/src/soothe_daemon/server/rest/schemas.py` |
| JWT bearer auth dependency (token validation guard) | `packages/soothe-daemon/src/soothe_daemon/server/rest/auth_dependency.py` |
| Router registration in `ChannelManager` | edit `packages/soothe-daemon/src/soothe_daemon/channel_manager.py` |
| Unit tests | `packages/soothe-daemon/tests/unit/server/rest/` |

`__init__.py` for `server/rest/` re-exports **only** `build_identity_router`
(the router factory). No schemas or helpers leak through the parent
(Minimum-Exposure, Rule 16).

## API Design

Base path: `/api/v1`. All responses are JSON. Errors use a uniform
`{"error": {"code": "...", "message": "..."}}` envelope matching the WS
`AuthHandler` error codes (`invalid_credentials`, `aksk_expired`, …) so the
two transports share a vocabulary.

### Authentication & token endpoints (no auth required)

| Method | Path | Body | → `IdentityService` method | Response |
|--------|------|------|---------------------------|----------|
| `POST` | `/api/v1/auth/token` | `{access_key, secret_key}` | `authenticate` | `200 {access_token, refresh_token, expires_in, user_id}` / `401 invalid_credentials` |
| `POST` | `/api/v1/auth/refresh` | `{refresh_token}` | `refresh_token` | `200 {access_token, refresh_token, expires_in}` / `401 invalid_refresh_token` |
| `POST` | `/api/v1/auth/introspect` | Bearer JWT | `validate_token` | `200 {claims}` / `401 token_invalid` |

`/auth/token` reuses `AuthHandler.handle_auth`'s response shape verbatim; the
HTTP handler calls the same `identity.authenticate(access_key, secret_key)`
path. The shared `_AUTH_ERROR_MESSAGES` / `_REFRESH_ERROR_MESSAGES` dicts in
`auth_handler.py` are extracted to a small `server/rest/error_codes.py` (or
imported directly) so WS and HTTP return identical error strings.

### User management (Bearer JWT required — admin scope)

| Method | Path | → method | Response |
|--------|------|----------|----------|
| `POST` | `/api/v1/users` | `create_user(user_id, metadata)` | `201 User` |
| `GET` | `/api/v1/users` | `list_users()` | `200 [User]` |
| `GET` | `/api/v1/users/{user_id}` | `get_user(user_id)` | `200 User` / `404 user_not_found` |
| `DELETE` | `/api/v1/users/{user_id}` | `delete_user(user_id)` | `204` / `404 user_not_found` |

### AKSK management (Bearer JWT required — admin scope)

| Method | Path | → method | Response |
|--------|------|----------|----------|
| `POST` | `/api/v1/users/{user_id}/aksk` | `create_aksk(user_id, expiry_days)` | `201 AKSKPair` (secret_key shown once) |
| `GET` | `/api/v1/users/{user_id}/aksk` | `list_aksk(user_id)` | `200 [AKSKPair]` |
| `DELETE` | `/api/v1/aksk/{aksk_id}` | `revoke_aksk(aksk_id)` | `204` / `404 aksk_not_found` |

### Status (Bearer JWT required)

| Method | Path | → method | Response |
|--------|------|----------|----------|
| `GET` | `/api/v1/identity/status` | `get_status()` | `200 IdentityStatus` |

## Authentication Model

- **Access token**: JWT bearer, issued by `/auth/token`, validated via
  `IdentityService.validate_token` → `TokenClaims`. The
  `auth_dependency.py` FastAPI dependency calls `validate_token` and raises
  `HTTPException(401)` on `None`.
- **Admin scope**: the management endpoints (user/AKSK CRUD, status) require a
  valid JWT. Initially, *any* valid JWT is accepted as admin (Soothe has no
  role model today). A `TokenClaims` extension for `role`/`scope` is noted as
  future work but **out of scope** for this plan — adding it would change the
  token schema and the `soothe-nano` protocol, which is a separate change.
- **Disabled identity**: when `config.identity.enabled is False`, the router
  mounts but every endpoint returns `503 identity_disabled` (mirrors the CLI
  `_require_enabled` gate and the WS `identity_disabled` error code).

## Router Wiring

`ChannelManager.__init__` (around line 352, where `_unified_app` is created):
after the `/healthz` route, if `config.identity.enabled` and a unified app
exists, call `build_identity_router(identity_service)` and
`self._unified_app.include_router(router, prefix="/api/v1")`.

The `IdentityService` instance is already constructed by the daemon at startup
(`SootheDaemon` builds it from `SootheConfig` / `SootheDaemonConfig.identity`).
The router factory takes the already-built service as a parameter — no second
construction, no second DB connection, honoring Rule 10 (unified persistence
backend: one `IdentityService`, one writer).

If the unified app is **not** created (neither WS nor ACP-WS enabled), the
REST router is not mounted and a startup log line notes that REST identity is
unavailable without an HTTP-listening transport. (Future: a dedicated
`transports.rest` config could force the unified app on for REST-only
deployments — out of scope here.)

## Changes

### NEW `packages/soothe-daemon/src/soothe_daemon/server/rest/__init__.py`
Re-export `build_identity_router` only.

### NEW `packages/soothe-daemon/src/soothe_daemon/server/rest/schemas.py`
HTTP-only Pydantic request bodies (`TokenRequest`, `RefreshRequest`,
`CreateUserRequest`, `CreateAKSKRequest`). Response models reuse the
`soothe_nano.protocols.identity` models directly (`User`, `AKSKPair`,
`AuthResult`, …) — do not re-declare them (Rule 16, no duplicate schemas).

### NEW `packages/soothe-daemon/src/soothe_daemon/server/rest/auth_dependency.py`
`get_current_identity(...)` FastAPI dependency: extracts Bearer token from
`Authorization` header, calls `identity.validate_token`, returns `TokenClaims`
or raises `HTTPException(401, "token_invalid")`. Also exports
`require_identity_enabled(config)` to gate the 503 path.

### NEW `packages/soothe-daemon/src/soothe_daemon/server/rest/identity_router.py`
`build_identity_router(identity: IdentityProtocol, *, enabled: bool) -> APIRouter`.
Defines all endpoints in the API Design table. Each handler is a thin
`await identity.<method>(...)` call with Pydantic response_model. Error
mapping: `UserNotFoundError` → 404, `AKSKNotFoundError` → 404,
`InvalidCredentialsError` → 401, `IdentityDisabledError` → 503, other
`IdentityError` → 400.

### EDIT `packages/soothe-daemon/src/soothe_daemon/channel_manager.py`
After the `/healthz` route block (~line 370), add:
```python
if self._config.identity.enabled and self._unified_app is not None:
    from soothe_daemon.server.rest import build_identity_router
    self._unified_app.include_router(
        build_identity_router(self._identity_service, enabled=True),
        prefix="/api/v1",
    )
    logger.debug("Mounted REST identity API at /api/v1")
```
Requires `ChannelManager` to receive the `identity_service` (it already has
access via the daemon; pass it through `__init__` or set via a setter like
`set_message_handler`). The daemon's `SootheDaemon` already constructs
`IdentityService` — wire it into `ChannelManager` the same way `runner` /
`session_manager` are passed.

### NEW `packages/soothe-daemon/tests/unit/server/rest/test_identity_router.py`
FastAPI `TestClient` against a router built with a stub `IdentityProtocol`
(e.g. an in-memory or SQLite-backed `IdentityService` in a tmp path). Covers:
- happy path for every endpoint,
- 401 on missing/invalid bearer,
- 404 on unknown user/aksk,
- 503 when `enabled=False`,
- `/auth/token` returns the same field set as the WS `auth_response`.

Test location follows Rule 4: `packages/soothe-daemon/tests/unit/`.

## Verification (Rule 5 / Rule 6)

After implementation:
1. `make lint` (ruff) — zero errors.
2. `./scripts/verify_finally.sh` — lint + tests + vulture + module-boundary
   check (must confirm `soothe_daemon` imports only `soothe` / `soothe-nano`,
   never `soothe_cli` / `soothe_client`).
3. Cleanse pass: confirm no duplicated auth-error strings between
   `auth_handler.py` and the new REST layer — extract shared constants if
   duplication appears (deletion/consolidation only, no behavior rewrite).
4. Fix to green, re-verify.

## Alternatives Considered

1. **Username/password auth instead of AKSK.** Rejected: would require a new
   password-hash store and a credential model not present in
   `IdentityService`. Soothe's identity axis is AKSK→JWT; a password flow is a
   separate, larger change and not what the existing service supports.
2. **REST router in `soothe` (host package) instead of `soothe-daemon`.**
   Rejected by §7b: HTTP/WS server and admin IO are daemon concerns. `soothe`
   holds the *service*; the daemon holds the *transports*. This mirrors how
   `AuthHandler` (a transport) lives in the daemon while `IdentityService`
   lives in `soothe`.
3. **Separate FastAPI app for REST (not the unified app).** Rejected: would
   need a second uvicorn listener and a second port. The unified app already
   exists when any HTTP-listening transport is on; mounting a sub-router is
   one line and shares the port.
4. **Add role/scope to JWT claims now.** Deferred: changes the
   `soothe-nano` `TokenClaims` protocol and token schema — a cross-axis
   contract change needing its own IG. This plan accepts "any valid JWT =
   admin" as the initial policy and notes the extension point.

## Open Questions (for plan approval)

1. **Admin authorization**: is "any valid JWT may manage users" acceptable
   for v1, or must we gate management endpoints to a bootstrap admin token /
   config-defined admin user list *in this same change*?
2. **REST-only deployments**: should this plan also add a
   `transports.rest.enabled` config so the unified app is created even when
   neither WS nor ACP-WS is on? (Currently REST rides on the unified app,
   which is only created when a WS transport is enabled.)
