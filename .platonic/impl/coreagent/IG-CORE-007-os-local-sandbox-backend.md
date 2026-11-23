# Implementation Guide: os_sandbox Local OS-Primitive Sandbox Backend

> Implementation guide for the `os_sandbox` backend (local OS-primitive sandbox for KVM-less hosts) in soothe-nano.
>
> **Crate/Module**: `soothe_nano/sandbox` (+ `soothe_nano/config/models.py`, `soothe_nano/agent/builder.py`)
> **Source**: Derived from RFC-CORE-011 (os_sandbox — Local OS-Primitive Sandbox Backend for KVM-less Hosts)
> **Related RFCs**: RFC-CORE-003 (Secure Filesystem Path Handling), RFC-CORE-008 (OperationSecurityProtocol), RFC-CORE-004 (Thread-Aware Workspace), RFC-LOOP-030 (Risk-Tiered Security)
> **Language**: Python 3.11+ (stdlib only; no new runtime dependencies)
> **Repos**: implementation in the **soothe-nano repo** (agent axis); the soothe host repo only bumps its floor (`packages/soothe/pyproject.toml`)

---

## 1. Overview

### 1.1 Purpose

This document specifies the implementation architecture for the `os_sandbox` backend defined in RFC-CORE-011. It provides:

- Concrete module structure and class design for `OsLocalSandbox`
- The three platform tiers (macOS Seatbelt / Linux bubblewrap+seccomp / Landlock degraded) with spawn pipelines
- Session lifecycle, background-process semantics, and env allowlist construction
- `sandbox_posture` derivation and config validation
- The two P0 wiring fixes as precursors
- A task-by-task coding plan with tests

### 1.2 Scope

**In Scope**:

- `OsLocalSandbox(BaseSandbox)` implementing `execute` / `upload_files` / `download_files` / `id` (zero protocol change)
- `OsLocalSandboxConfig` model + factory registration + `SandboxBackendConfig` dispatch
- Capability probes and the `auto → bwrap → landlock` tier ladder with fail-loud refusal
- Builder-side workspace binding (`bind_workspace`) and posture computation
- P0 fixes in `sandbox/factory.py` and `agent/builder.py`
- Host-repo floor bump once nano releases

**Out of Scope** (per RFC §2.2):

- Egress relay/proxy implementation (config surface + default-deny only)
- ArcBox backend changes beyond the P0 factory fix
- Loop-plane isolation, gVisor tier, MCP wrapping
- Host-repo runtime code (config examples/docs only)

### 1.3 Spec Compliance

This guide **supersedes** RFC-CORE-011 with concrete details but **MUST NOT contradict** it. All invariants — fail-loud construction, no silent host fallback, credentials never in child env, Landlock as declared degraded tier — are preserved. See Appendix A for the requirement mapping.

---

## 2. Architectural Position

### 2.1 System Context

The backend plugs into the existing sandbox machinery unchanged. Every box marked **existing** is touched only by the P0 fixes or not at all.

```text
AgentBuilder._create_sandbox_backend            (existing; P0-2 removes fallback)
  └─ SandboxBackendFactory.create(config)       (existing; + "os_sandbox" dispatch)
      └─ OsLocalSandbox(config)                 (NEW)
          ├─ capability probe → tier selection  (NEW, fail-loud)
          ├─ bind_workspace(host_root)          (NEW, builder-injected)
          └─ BaseSandbox derived FS ops         (existing, via execute())
  └─ _wrap_sandbox_backend → CompositeBackend   (existing: /memories /.soothe
  │                                               /large_tool_results /conversation_history → host)
  └─ _finalize_tool_placements                  (existing; + posture derivation)
      └─ resolver._resolve_sandbox_shell_tools  (existing: 5 shell tools → BackendShellExecutor)
```

Defense layers (RFC-CORE-011 §5.4): L1 (kernel isolation) is this backend; L2 (env/egress seam) is the env construction inside `execute`; L3 (OperationSecurityProtocol) is untouched; L4 is out of scope.

### 2.2 Key architectural decision: identity path mapping

ArcBox remaps the workspace to a guest path (`/workspace`) because the VM is a separate filesystem. `os_sandbox` runs on the **same kernel and filesystem** — so the sandbox's `workspace_root` is the **real host workspace path**, bound identity (`--bind <ws> <ws>`) on Linux and directly allowed in the Seatbelt profile on macOS. Guest paths are host paths; the kernel boundary restricts *reach*, not path identity. Consequences:

- `upload_files` / `download_files` are direct file I/O with workspace-root validation (defense in depth on top of the kernel boundary, not a substitute).
- Background PIDs are host PIDs (no PID-namespace unsharing), so `kill_process`, `tail_background_log`, and the pid-liveness probe keep working without any pid mapping.
- The existing guest background one-liner (`RunBackgroundTool._run_in_sandbox`, `toolkits/shell.py:1026-1071`) works unmodified.

### 2.3 Module Responsibilities

| Module | Responsibility | New/Existing |
|--------|----------------|--------------|
| `soothe_nano/sandbox/os_local.py` | `OsLocalSandbox`, tier builders, probes, env allowlist, errors | New |
| `soothe_nano/sandbox/factory.py` | dispatch branch + `_os_sandbox_factory` + registration; **P0-1** `socket_path` | Modified |
| `soothe_nano/config/models.py` | `OsLocalSandboxConfig`, `SandboxBackendConfig.os_sandbox`, `SandboxConfig.acknowledge_no_isolation` | Modified |
| `soothe_nano/agent/builder.py` | **P0-2** fallback removal; `bind_workspace` injection; posture computation | Modified |
| `soothe_nano/sandbox/__init__.py` | lazy exports for new symbols | Modified |
| `packages/soothe-nano/docs/os-sandbox-backend.md` | backend doc (house convention: one doc per backend) | New |

### 2.4 Dependency Constraints

- **MUST** stay stdlib-only (`subprocess`, `ctypes`, `shutil`, `tempfile`, `platform`); bwrap/seatbelt are external binaries invoked via `subprocess`.
- **MUST NOT** add protocol members to `SandboxBackendProtocol` or `BaseSandbox`.
- **MUST NOT** modify `placement.py` resolution rules, the CompositeBackend route set, or `BackendShellExecutor`.
- **MAY** add the duck-typed `bind_workspace()` extra (guarded by `hasattr` at the call site — not a protocol requirement).

---

## 3. Module Structure

```text
packages/soothe-nano/src/soothe_nano/sandbox/
├── __init__.py          # + lazy exports: OsLocalSandbox, OsSandboxError, OsSandboxTier
├── arcbox.py            # unchanged
├── factory.py           # + "os_sandbox" dispatch branch, _os_sandbox_factory, P0-1 fix
├── os_local.py          # NEW — everything below
│   ├── OsSandboxError / OsSandboxCapabilityError / OsSandboxConfigError
│   ├── OsSandboxTier (Literal: "seatbelt" | "bwrap" | "landlock")
│   ├── OsLocalSandbox(BaseSandbox)
│   ├── _SeatbeltSession   (profile build + sandbox-exec spawn)
│   ├── _BwrapSession      (bwrap argv build + embedded launcher)
│   ├── _LandlockSession   (embedded launcher only)
│   └── _build_child_env() (env allowlist; no secrets)
└── placement.py         # unchanged
```

`os_local.py` is a single module (~700 lines) mirroring `arcbox.py`'s single-file layout. The launcher scripts (seccomp/landlock ctypes snippets) are module-level string constants — embedded, never written to disk.

---

## 4. Core Types

### 4.1 `OsLocalSandboxConfig` (`config/models.py`)

```python
class OsLocalEgressConfig(BaseModel):
    """Egress policy surface (default deny)."""
    enabled: bool = Field(default=False)   # True → validation error until the relay lands


class OsLocalSandboxConfig(BaseModel):
    """Configuration for the local OS-primitive sandbox backend (RFC-CORE-011)."""

    type: Literal["os_sandbox"] = Field(default="os_sandbox")
    workspace_root: str | None = Field(default=None)
    linux_tier: Literal["auto", "bwrap", "landlock"] = Field(default="auto")
    allow_degraded: bool = Field(default=False)
    default_timeout: int = Field(default=120, ge=1)
    env_allowlist: list[str] = Field(
        default_factory=lambda: ["PATH", "LANG", "LC_ALL", "LC_CTYPE", "TERM", "TMPDIR", "HOME"]
    )
    egress: OsLocalEgressConfig = Field(default_factory=OsLocalEgressConfig)

    @model_validator(mode="after")
    def _validate_egress_and_degraded(self) -> OsLocalSandboxConfig:
        # egress.enabled=True is a config-time loud failure (RFC non-goal: relay deferred)
        # allow_degraded=True is required to accept landlock via linux_tier="auto"
        ...
```

**Field semantics**:

| Field | Type | Description |
|-------|------|-------------|
| `workspace_root` | `str \| None` | Real host workspace dir. `None` = builder injects via `bind_workspace()` (thread-aware workspaces). Both unset → `execute()` raises. |
| `linux_tier` | `Literal` | `auto` probes bwrap first, Landlock second (only when `allow_degraded`); explicit values pin a tier and **fail** if unavailable. |
| `allow_degraded` | `bool` | Gate for accepting the Landlock tier from `auto`. Explicit `linux_tier="landlock"` also requires it (degradation is always deliberate). |
| `env_allowlist` | `list[str]` | Exact env keys the child may receive. `HOME` → workspace root, `TMPDIR` → session scratch. |

### 4.2 `SandboxBackendConfig` extension

```python
class SandboxBackendConfig(BaseModel):
    type: str = Field(default="arcbox")
    arcbox: ArcBoxBackendConfig | None = None
    os_sandbox: OsLocalSandboxConfig | None = None   # NEW
```

Validator gains the `type == "os_sandbox"` branch (mirroring the arcbox branch). `SandboxConfig` gains `acknowledge_no_isolation: bool = Field(default=False)` (§6.7).

### 4.3 `OsLocalSandbox`

```python
class OsLocalSandbox(BaseSandbox):
    enable_capture_offload = True   # guest userland = host userland; shell/coreutils guaranteed

    def __init__(self, config: OsLocalSandboxConfig) -> None: ...
    @property
    def id(self) -> str: ...                 # f"os-sandbox-{uuid4().hex[:12]}"; stable per session
    @property
    def tier(self) -> OsSandboxTier: ...     # resolved concrete tier ("seatbelt"/"bwrap"/"landlock")
    @property
    def sandbox_posture(self) -> str: ...    # "full" | "subprocess_only" (landlock ⇒ subprocess_only)
    def bind_workspace(self, host_root: str) -> None: ...
    def execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse: ...
    def upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]: ...
    def download_files(self, paths: list[str]) -> list[FileDownloadResponse]: ...
    def close(self) -> None: ...             # idempotent scratch cleanup
```

**Invariants**:

- `__init__` runs the capability probe for the requested tier and **raises `OsSandboxCapabilityError`** on failure. No construction succeeds into a de facto host execution path.
- `bind_workspace` is called at most once with a real directory; re-binding a different root raises. After binding, `workspace_root` (protocol-visible) == the host root.
- `execute` before any workspace binding raises `OsSandboxConfigError` (fail-loud, not best-effort).

---

## 5. Key Interfaces

### 5.1 `BaseSandbox` contract (existing, unchanged)

Subclasses must implement `execute()`, `upload_files()`, `download_files()`, and `id` (`soothe_deepagents/backends/sandbox.py:824-843`). All FS primitives (`ls`/`read`/`write`/`edit`/`delete`/`glob`/`grep`) are derived by the base class from guest-side `python3 -c` scripts through `execute()` — the sandboxed userland trivially satisfies the python3 + POSIX coreutils requirement. The timeout convention from `BackendShellExecutor` (`toolkits/shell.py:660-663`): exit code 124 + note text = timed out.

### 5.2 Factory registration

```python
# factory.py
def _os_sandbox_factory(cfg: OsLocalSandboxConfig) -> BackendProtocol:
    from soothe_nano.sandbox.os_local import OsLocalSandbox
    return OsLocalSandbox(cfg)

SandboxBackendFactory.register("os_sandbox", _os_sandbox_factory)   # also _register_backend_type

# SandboxBackendFactory.create dispatch branch:
if config.type == "os_sandbox":
    if config.os_sandbox is None:
        msg = "sandbox.backend.type='os_sandbox' requires sandbox.backend.os_sandbox config"
        raise ValueError(msg)
    return factory(config.os_sandbox)
```

### 5.3 Builder injection (duck-typed, not protocol)

`AgentBuilder._create_sandbox_backend`, after `create_sandbox_backend(...)` succeeds:

```python
if hasattr(backend, "bind_workspace"):
    backend.bind_workspace(resolved_host_workspace)   # reuse the workspace resolution
```

The resolved host workspace is the same root the builder's host backend is initialized against (`_initialize_backend` / `WorkspaceRuntime`). If the builder cannot resolve a workspace at this point, the task threads the existing resolution through — it MUST NOT default to CWD.

---

## 6. Implementation Details

### 6.1 Tier selection and capability probes

```text
platform == darwin:
    tier = "seatbelt"    (probe: shutil.which("sandbox-exec"); missing → raise)
platform == linux:
    linux_tier == "bwrap":    probe bwrap; fail → raise
    linux_tier == "landlock": require allow_degraded; probe landlock; fail → raise
    linux_tier == "auto":
        bwrap probe passes      → "bwrap"
        landlock probe passes AND allow_degraded → "landlock"
        else → raise OsSandboxCapabilityError (names both prerequisites)
```

- **bwrap probe**: run `bwrap --ro-bind /usr /usr --dev /dev true` (5s timeout, capture output). Exit 0 = capable unprivileged user namespaces. This is the only trustworthy probe — "unshare succeeds" ≠ "namespaces carry capabilities" (analysis §常见误判之三).
- **Landlock probe**: `fork()` + in the child call `landlock_create_ruleset` via `ctypes.CDLL(None).syscall(444, NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)`; exit code distinguishes available (≥0 → 0) from ENOSYS. Parent waits.
- Probes run once in `__init__` and are logged (INFO) with the resolved tier.

### 6.2 macOS Seatbelt tier

Per-exec spawn:

```python
["sandbox-exec", "-p", profile, "/bin/sh", "-c", wrapped_command]
# cwd semantics via Popen(cwd=workspace_root) (BackendShellExecutor contract)
```

Shell is `/bin/sh` — zsh refuses to start under `(deny default)`. Profile (cached per session; same allow set for every exec; subpaths are `Path.resolve()`-ed real paths because `/tmp` firmlinks resolve to `/private/tmp` and unresolved subpath literals never match):

```lisp
(version 1)
(deny default)
(allow file-read*)           ; v1 trade-off: broad read (narrow read aborts dyld, §6.2 note)
(allow process-exec)
(allow process-fork)
(allow file-write*
  (subpath "<resolved workspace>") (subpath "<scratch-dir>"))
(allow file-write-data (literal "/dev/null"))
```

Network and signals fall to the root `(deny default)`.

**v1 read-side trade-off (implementation finding, macOS 26.x)**: narrow subpath read allowances abort process startup — dyld resolves library paths through firmlinks that only match under unrestricted reads (SIGABRT 134 at exec). Every candidate narrowing was bisected (subpath reads, exec restrictions, zsh) and only broad read survives. The load-bearing v1 boundary is write/network/signal reach; read-side restriction is a documented follow-up (RFC §8). `sandbox-exec` deprecation posture is an Open Question in the RFC — the profile builder is isolated in `_SeatbeltSession.build_profile()` so a future replacement is one class swap.

### 6.3 Linux bwrap + seccomp tier (full)

Two-stage spawn mirroring Codex's `codex-linux-sandbox`:

```python
["bwrap",
  "--ro-bind", "/usr", "/usr",
  "--ro-bind-try", "/usr/local", "/usr/local",
  "--ro-bind-try", "/bin", "/bin",
  "--ro-bind-try", "/lib", "/lib", "--ro-bind-try", "/lib64", "/lib64",
  "--ro-bind-try", "/etc", "/etc",
  "--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp",
  "--bind", ws, ws,                      # identity bind (§2.2)
  "--bind", scratch, scratch,            # identity bind of the session scratch
  # + interpreter-dir ro-binds when /usr/bin/python3 is absent
  "--die-with-parent", "--new-session",
  "--unshare-net",                       # loopback-only netns (egress default-deny)
  interp, "-c", _LAUNCHER_SECCOMP, bpf_hex, seccomp_nr, "--", "/bin/sh", "-c", command]
```

Notes:

- **No `--unshare-pid`** — background PIDs must stay host-visible (§2.2).
- Env is applied uniformly by the parent via `Popen(env=allowlist)` on every tier (bwrap inherits the parent env, i.e. exactly the allowlist) — `--clearenv`/`--setenv` were dropped from the design: one mechanism, three tiers, and `_build_child_env()` stays the single source.
- The scratch dir is an identity `--bind` (not tmpfs) so upload/download path resolution and `TMPDIR` semantics agree across tiers.
- `--die-with-parent` ties sandboxed processes to the agent process lifetime; `--new-session` detaches them from the host controlling terminal.
- The in-namespace interpreter prefers `/usr/bin/python3` (covered by the `/usr` bind); the running interpreter's directory is bound read-only as fallback.

`_LAUNCHER_SECCOMP` (embedded `python3 -c` script, passed via argv):

1. `ctypes` `prctl(PR_SET_NO_NEW_PRIVS, 1)` — **mandatory before seccomp** (an installable filter without `no_new_privs` is bypassable via privilege escalation; both are non-negotiable, RFC §5.2.2). This is the load-bearing privilege-escalation block.
2. Install the classic-BPF program via `seccomp(2)` syscall (`SECCOMP_SET_MODE_FILTER`, no `TSYNC` needed — single-threaded at exec).
3. `os.execvp(argv[1], argv[1:])`.

**Filter shape (implementation revision — deny-list, not allowlist)**: the BPF program is `[LD arch][JEQ arch, else deny][LD nr][JEQ nr_i -> deny]*[RET allow][RET errno(ENOSYS)]`. Denied syscalls fail outright; unknown syscalls return `ENOSYS` (libc falls back gracefully for benign newer syscalls like `rseq` / `clone3`-fallback); unknown architectures are denied. An allowlist was rejected: a static allowlist either breaks benign future syscalls (rseq, clone3, statx variants) or gets widened until meaningless, while the escape surface is a small closed set. The userns/mount-ns capability checks carry the boundary; seccomp is defense in depth on top of `no_new_privs`.

The deny table is a module constant (`_SECCOMP_DENY_SYSCALLS: dict[str, tuple[int, ...]]` — namespace/privilege escapes: `unshare`/`setns`/`mount`/`umount2`/`pivot_root`/`ptrace`/`clone3`/`bpf`/module loading/`perf_event_open`/..., hardcoded per-arch for `x86_64` and `aarch64`) so the set is reviewable and test-assertable, not emergent. The BPF program is built parent-side (`_build_seccomp_bpf(arch, denied)` — layout and jump targets unit-testable on every platform) and shipped to the in-namespace launcher as hex.

### 6.4 Landlock degraded tier

Spawn = plain `Popen([sys.executable, "-c", _LAUNCHER_LANDLOCK, "--", shell, "-c", command], env=child_env)`. The launcher:

1. `prctl(PR_SET_NO_NEW_PRIVS, 1)`.
2. `landlock_create_ruleset` with handled FS accesses; `landlock_add_rule` path-beneath rules: read/execute on system roots, read/write/execute on workspace + scratch; `landlock_restrict_self`.
3. `os.execvp`.

**Declared degraded semantics** (RFC §5.2.3): file-access denial only — no view, no network, no process isolation. The tier forces `sandbox_posture = "subprocess_only"` with engine tag `landlock`. Config-time gating: reachable only via explicit `linux_tier="landlock"` + `allow_degraded=True`, or `auto` + `allow_degraded=True`.

### 6.5 Session lifecycle, scratch, and teardown

- One `OsLocalSandbox` per agent build (the builder already constructs exactly one backend per build — same lifecycle shape as ArcBox).
- Scratch: `tempfile.mkdtemp(prefix="soothe-os-sandbox-")`; `TMPDIR` in the child env points at it (bwrap: a tmpfs inside the view). A module-level `atexit` registry removes scratch dirs on process exit; `close()` is the explicit, idempotent path.
- Session teardown does **not** kill background PIDs — they belong to the shell toolkit's process table (`kill_process` is the model-facing path), and bwrap's `--die-with-parent` bounds their lifetime to the agent process.

### 6.6 Env allowlist and credential hygiene (L2)

`_build_child_env(parent_env, allowlist, workspace, scratch) -> dict[str, str]`:

- Copy only allowlisted keys from the parent env. Unknown-but-present secrets (`*_TOKEN`, `*_KEY`, `*_SECRET`, DSNs) are never copied by accident — the allowlist is the only door.
- Overrides: `HOME` → workspace root; `TMPDIR` → scratch.
- Applied at: `Popen(env=...)` uniformly on **all three tiers** (bwrap inherits the parent env, i.e. exactly the allowlist — the design's `--clearenv/--setenv` variant was dropped for this single-mechanism form).
- HostShellExecutor env behavior is **unchanged** (L2 in host mode is a separate concern; the RFC scopes L2 to sandboxed subprocesses).

### 6.7 Posture derivation and validation

Posture is computed on the **builder** after `_finalize_tool_placements` (it needs the placement report) and mirrored on the backend (`OsLocalSandbox.sandbox_posture` reflects only the tier; the builder's value is authoritative for tool routing):

```python
# builder, after placements are resolved (only in sandbox mode):
backend_posture = getattr(sandbox_backend, "sandbox_posture", "full")   # tier-reported
host_or_shuttle = any(p in ("host", "shuttle") for p in placement_report.placements.values())
posture = "subprocess_only" if backend_posture == "subprocess_only" or host_or_shuttle else "full"
# host builds never enter this branch: builder.sandbox_posture stays None (unset),
# observable as the absence of a sandbox rather than a "none" value
```

- Stored as `builder.sandbox_posture` and logged with the placement report; `host_placement_tools` (RFC §6.3) = the honest ledger of tools outside the boundary, logged at the same line.
- Validation: `sandbox.enabled=False` **and** security config's `hard_deny_severities` excluding `"critical"` **and** `sandbox.acknowledge_no_isolation=False` → `ValueError` at build (RFC-CORE-011 §5.6; `hard_deny_severities` exists at `config/models.py:2121`, default `{"critical"}` — default configs are unaffected).

### 6.8 P0 fixes (precursors)

**P0-1 — factory `socket_path`** (`sandbox/factory.py:97-117`): `_arcbox_factory` calls `ArcBoxBackend.from_config` with `api_url`/`api_key` but omits `cfg.socket_path` even though `ArcBoxBackendConfig.socket_path` exists (`config/models.py:611`). Fix: add `socket_path=cfg.socket_path` to the call. Test: config → factory → assert the constructed client carries the socket path (mirror the existing `_SdkArcBoxClient` seam in `test_arcbox_backend.py`).

**P0-2 — builder fallback removal** (`agent/builder.py:562-591`): `_create_sandbox_backend` catches `ArcBoxError`, logs a warning, and returns `self._initialize_backend(policy)` — a silent host-execution fallback. Fix: delete the try/except (and the "Falls back… gracefully" docstring paragraph); construction errors propagate. The except clause must not be widened to `OsSandboxError` — there is nothing to catch. Update `tests/unit/agent/test_builder_sandbox.py` expectations that assert the fallback (spec change per RFC, not a test cheat: the fallback test becomes a raise test).

---

## 7. Error Handling

```python
class OsSandboxError(Exception): ...            # base
class OsSandboxCapabilityError(OsSandboxError): ...  # probe failed; message names the prerequisite
class OsSandboxConfigError(OsSandboxError): ...      # unbound workspace, rebind, egress not implemented
```

| Category | Handling |
|----------|----------|
| Probe failure at construction | Raise — the configured sandbox either engages or aborts the build (P0-2 guarantees propagation) |
| Command failure inside sandbox | Normal `ExecuteResponse` (exit code, output) — errors as data, per `ExecutorOutcome` |
| Command timeout | Exit 124 + note (BackendShellExecutor convention, `toolkits/shell.py:660`) |
| seccomp kill (SIGSYS) | Surfaces as exit code 159/-31 with coreutils note; test-asserted |
| Write outside workspace | Denied by kernel (tier) — the tool layer formats the denial text; no retry, no fallback |
| `upload/download` path escape | `INVALID_PATH` protocol error after workspace-root validation (defense in depth) |

---

## 8. Configuration

```yaml
sandbox:
  enabled: true
  backend:
    type: os_sandbox
    os_sandbox:
      linux_tier: auto        # auto | bwrap | landlock
      allow_degraded: false
      # workspace_root: /var/lib/soothe/ws   # omit for builder injection
      env_allowlist: [PATH, LANG, LC_ALL, TERM, TMPDIR, HOME]
      egress: { enabled: false }
  acknowledge_no_isolation: false   # required true only for posture=none + relaxed severities
```

| Option | Default | Description |
|--------|---------|-------------|
| `linux_tier` | `auto` | Tier pin or auto-probe |
| `allow_degraded` | `false` | Gate for the Landlock tier |
| `workspace_root` | `None` | Static host workspace; `None` = builder-injected |
| `default_timeout` | `120` | `execute` fallback timeout |
| `env_allowlist` | 7 keys (§4.1) | Child env door |
| `egress.enabled` | `false` | `true` is a loud config error until the relay RFC lands |

---

## 9. Testing Strategy

### 9.1 Unit tests (`tests/unit/sandbox/test_os_local_backend.py`)

| Component | Test focus |
|-----------|------------|
| `_build_child_env` | allowlist copy; secret sentinel keys from parent env absent; `HOME`/`TMPDIR` overrides |
| `_SeatbeltSession.build_profile` | profile contains `(deny default)`, workspace subpath allowance; egress deny line; same profile per session |
| `_BwrapSession.build_argv` | identity bind, `--clearenv` + setenv pairs, `--die-with-parent`, `--unshare-net`, **no** `--unshare-pid`; launcher argv wiring |
| `_SECCOMP_ALLOWLIST` / `_LAUNCHER_SECCOMP` | table shape per arch; launcher source contains `PR_SET_NO_NEW_PRIVS` before `seccomp` |
| `OsLocalSandbox` config errors | unbound workspace raise; rebind raise; `egress.enabled=True` config-time raise |
| Posture | landlock ⇒ `subprocess_only`; full-tier instance reports `"full"` at backend level |
| Factory | `os_sandbox` dispatch; missing sub-config ValueError; unknown type still errors |
| Tier ladder (Linux) | `auto` without bwrap/landlock/allow_degraded → `OsSandboxCapabilityError` naming prerequisites |

Skip-marking: platform-tier classes use `pytest.mark.skipif(sys.platform != ...)`; the profile/argv builders are pure and testable everywhere.

### 9.2 Integration tests (`tests/unit/sandbox/test_os_local_integration.py`, env-gated)

- **Linux CI (bwrap)**: execute `echo hi` (exit 0); write inside workspace succeeds; write outside (`/tmp` via scratch is allowed — use `$HOME`-adjacent path) denied with non-zero exit; `cat /proc/self/status` inside sandbox shows `NoNewPrivs: 1` and `Seccomp: 2`; exit code 124 on timeout.
- **Linux CI (Landlock)**: gated on kernel ≥ 5.13 (probe); open outside workspace denied (`PermissionError`/exit ≠ 0), inside succeeds; posture `subprocess_only`.
- **macOS local (not CI-blockable)**: `sandbox-exec` spawn echo; write outside workspace denied; `network*` deny (curl to 127.0.0.1 fails).
- **Background lifecycle (any platform)**: `run_background`-equivalent one-liner via `execute` (`setsid nohup sh -c 'sleep 30' & echo $!`) → PID is host-visible → `os.kill(pid, 0)` alive → kill → reaped.
- **Credential fuzz**: seed parent env with sentinel secrets, run `env` inside sandbox, assert absence.
- **Builder**: `test_builder_sandbox.py` — no-fallback on construction error; `bind_workspace` injected; posture logged; `acknowledge_no_isolation` validation matrix.

### 9.3 Test utilities

Fake `Popen` recorder for argv/env assertions (the spawn functions take an injectable `popen=` seam defaulting to `subprocess.Popen`, mirroring the `ArcBoxClient` injectable seam).

---

## 10. Coding Plan

**Total Tasks**: 10 (7 implementation, 3 test) · **Estimated Files**: 2 new / 5 modified (nano) + 1 floor line (host repo)

### 10.1 Task Breakdown

> **Status (2026-11-23)**: Tasks 1-9 complete (nano working tree, uncommitted); Task 10 awaits the nano release. Implementation records in §12.

#### Task 1: P0-1 — factory `socket_path` propagation — **DONE**

**Action**: Modify · **Files**: `sandbox/factory.py` · **Section**: §6.8
**Deliverables**: `socket_path=cfg.socket_path` in `_arcbox_factory`'s `from_config` call; regression test in `tests/unit/sandbox/test_arcbox_backend.py`.
**Dependencies**: none (lands independently of the backend).

#### Task 2: P0-2 — remove builder host fallback — **DONE**

**Action**: Modify · **Files**: `agent/builder.py` (L562-591), `tests/unit/agent/test_builder_sandbox.py` · **Section**: §6.8
**Deliverables**: no try/except around `create_sandbox_backend`; docstring updated; fallback tests flipped to raise-assertions.
**Dependencies**: none.

#### Task 3: Config models — **DONE**

**Action**: Modify · **Files**: `config/models.py` · **Section**: §4.1-4.2, §8
**Deliverables**: `OsLocalEgressConfig`, `OsLocalSandboxConfig` (validators), `SandboxBackendConfig.os_sandbox` + dispatch validation, `SandboxConfig.acknowledge_no_isolation`.
**Dependencies**: none. · **Tests**: `tests/unit/core/` config validation cases (type mismatch, egress raise, enabled-requires-sub-config).

#### Task 4: `OsLocalSandbox` skeleton + session + env allowlist — **DONE**

**Action**: Create · **Files**: `sandbox/os_local.py` · **Section**: §4.3, §5.1, §6.5, §6.6
**Deliverables**: class with `id`/`bind_workspace`/`close`/scratch/atexit, `_build_child_env`, error types, `upload_files`/`download_files` with workspace-root validation, `execute` raising until bound (spawn delegated to tier sessions from Task 5).
**Dependencies**: Task 3. · **Tests**: env allowlist, binding, path validation, idempotent close.

#### Task 5: Tier sessions + probes + launchers — **DONE**

**Action**: Create (same file) · **Files**: `sandbox/os_local.py` · **Section**: §6.1-6.4
**Deliverables**: probe functions; `_SeatbeltSession` / `_BwrapSession` / `_LandlockSession`; `_LAUNCHER_SECCOMP` / `_LAUNCHER_LANDLOCK`; `_SECCOMP_ALLOWLIST`; tier ladder in `__init__`.
**Dependencies**: Task 4. · **Tests**: argv/profile builders (pure, all platforms), launcher source invariants, capability-error matrix.

#### Task 6: `execute()` wiring + factory registration — **DONE**

**Action**: Create/Modify · **Files**: `sandbox/os_local.py`, `sandbox/factory.py`, `sandbox/__init__.py` · **Section**: §5.2
**Deliverables**: `execute` → tier session spawn → `ExecuteResponse` (timeout=124 convention, truncation flag); `_os_sandbox_factory` + `register` + dispatch branch; lazy exports.
**Dependencies**: Task 5. · **Tests**: factory dispatch; end-to-end `execute("echo hi")` (skipped per platform availability).

#### Task 7: Builder integration — binding, posture, validation — **DONE**

**Action**: Modify · **Files**: `agent/builder.py` · **Section**: §5.3, §6.7
**Deliverables**: `bind_workspace` injection after construction; posture computation + `sandbox_posture` attr + log line with `host_placement_tools`; `acknowledge_no_isolation` validation.
**Dependencies**: Tasks 2, 6. · **Tests**: `test_builder_sandbox.py` additions (§9.2 builder block).

#### Task 8: Integration test suite — **DONE**

**Action**: Create · **Files**: `tests/unit/sandbox/test_os_local_integration.py` · **Section**: §9.2
**Deliverables**: bwrap escape/status assertions; landlock gate; background lifecycle; credential fuzz; macOS local skips documented.
**Dependencies**: Task 6.

#### Task 9: Backend documentation — **DONE**

**Action**: Create · **Files**: `packages/soothe-nano/docs/os-sandbox-backend.md` · **Section**: house doc (mirror `arcbox-backend.md` structure)
**Deliverables**: contract, tier matrix, posture semantics, config reference, limitations (degraded tier, no egress relay yet).
**Dependencies**: Task 6.

#### Task 10: Host-repo floor bump + docs pointer

**Action**: Modify · **Files**: `packages/soothe/pyproject.toml` (L25, soothe repo) · **Section**: RFC §5.8
**Deliverables**: `soothe-nano>=<released>,<3.0.0` bump once the nano release carrying Tasks 1-9 is published; changelog entry per release-and-governance rules.
**Dependencies**: nano release. (This repo only; no runtime code.)

### 10.2 Execution Order

1. Task 1 → 2 (P0 fixes, independently shippable)
2. Task 3 → 4 → 5 → 6 → 7 (backend spine)
3. Task 8 → 9 (verification + docs)
4. Task 10 (release-time)

### 10.3 Verification Checklist

- [x] All tasks completed (Task 10 pending nano release, by design)
- [x] `ruff check` / `ruff format` green on all changed files (nano's CI/Makefile gates; nano runs no mypy target — the 3 mypy strict-mode findings on touched files predate this IG and sit outside its diff hunks)
- [x] Unit tests pass on macOS (darwin: 2165 passed, 44 skipped — Linux-only integration gates skip); Linux CI (bwrap + landlock-on-5.13) to run on push
- [x] No test observes host execution under a configured `os_sandbox` (no-fallback test asserts build abort)
- [x] All RFC requirements covered (Appendix A), with two recorded deviations (§12: Seatbelt v1 broad read; seccomp deny-list)

---

## 11. Migration / Compatibility

### 11.1 Breaking Changes

- **P0-2**: sandbox construction failures now abort the build instead of degrading to host execution. Configs that relied on the silent fallback (ArcBox SDK missing but sandbox enabled) will fail loudly — the intended RFC behavior.
- **Posture validation**: configs running `sandbox.enabled=false` **and** `hard_deny_severities` without `"critical"` now require `acknowledge_no_isolation: true`. Default severities (`{"critical"}`) are unaffected.

### 11.2 Migration Path

Both changes are config-level and detectable at startup with actionable error messages naming the fix (install the SDK / set the ack flag). No data or storage migration. ArcBox behavior is otherwise unchanged; existing arcbox configs are untouched by the new config branch.

---

## Appendix A: RFC Requirement Mapping

| RFC Requirement | Guide Section | Implementation |
|-----------------|---------------|----------------|
| §5.1 Backend identity, zero contract change | §4.3, §5.1 | `OsLocalSandbox(BaseSandbox)`, four members only |
| §5.2.1 macOS Seatbelt `(deny default)` | §6.2 | `_SeatbeltSession` profile + `sandbox-exec` spawn |
| §5.2.2 Linux bwrap two-stage + `no_new_privs` | §6.3 | bwrap argv + `_LAUNCHER_SECCOMP` (prctl → seccomp → exec) |
| §5.2.3 Landlock declared degraded | §6.1, §6.4 | gated tier, forces `subprocess_only` |
| §5.2.4 probe + refusal | §6.1 | capability probes, `OsSandboxCapabilityError`, no host path |
| §5.3 session-scoped instances, background consistency | §2.2, §6.5 | identity path mapping, no `--unshare-pid`, existing one-liner |
| §5.4 defense layers | §2.1, §6.6 | L1 in backend, L2 env allowlist, L3 untouched |
| §5.5 egress/credential seam | §6.3, §6.6 | `--unshare-net`, `(deny network*)`, env allowlist, egress surface |
| §5.6 posture + validation | §6.7 | builder derivation, `acknowledge_no_isolation` |
| §5.7 P0 fixes | §6.8, Tasks 1-2 | factory `socket_path`; builder no-fallback |
| §5.8 placement (nano repo + floor bump) | §10 Task 10 | host floor line |
| §5.9 acceptance criteria | §9 | unit + integration matrix |

## Appendix B: Revision History

| Date | RFC Version | Changes |
|------|-------------|---------|
| 2026-11-23 | RFC-CORE-011 (Draft) | Initial guide |
| 2026-11-23 | RFC-CORE-011 (Draft, post-implementation revision) | §6.2/§6.3/§6.6/§6.7 aligned to the shipped implementation: Seatbelt v1 broad-read trade-off (`/bin/sh`, resolved subpaths); seccomp redesigned from allowlist to arch-aware **deny-list + ENOSYS default** with parent-side BPF builder; env applied uniformly via `Popen(env=...)` (bwrap `--clearenv/--setenv` dropped); scratch is an identity bind; posture pseudocode matches the builder (`None` for host builds, tier-reported backend posture). §10 tasks marked done; §12 implementation records added |

---

## 12. Implementation Records (2026-11-23)

All changes in the **soothe-nano** working tree (uncommitted; nano repo). Files:
`sandbox/os_local.py` (new, ~700 lines), `sandbox/factory.py`,
`sandbox/arcbox.py`, `sandbox/__init__.py`, `config/models.py`,
`config/__init__.py`, `agent/builder.py`; tests
`tests/unit/sandbox/test_os_local_backend.py` (new),
`tests/unit/sandbox/test_os_local_integration.py` (new, env-gated),
`tests/unit/sandbox/test_arcbox_backend.py` (+socket_path suite),
`tests/unit/agent/test_builder_sandbox.py` (+4 suites); docs
`docs/os-sandbox-backend.md` (new) + CHANGELOG `[Unreleased]`.

1. **P0-1 was deeper than the plan**: `from_config` itself had no
   `socket_path` parameter — the fix is a new parameter threaded into both
   `_SandboxManager` and the `ArcBoxBackend` constructor (manager+client
   paths), plus the factory forwarding. Regression suite asserts
   `_manager._socket_path` and `_client._socket_path`.
2. **P0-2**: the try/except `ArcBoxError` → host fallback in
   `_create_sandbox_backend` is deleted; construction failures propagate.
   Test patches `soothe_nano.sandbox.create_sandbox_backend` (the builder's
   local import point) and asserts the raise.
3. **Workspace binding is duck-typed** (`getattr(backend, "bind_workspace",
   None)`), not a protocol member — ArcBox backends are untouched. The bound
   root comes from `WorkspaceResolver.effective_tool_workspace(config)`,
   never the process CWD.
4. **Posture facts**: on the default placement table the builtin search tool
   group (`search_tools`/`search_skills`/`invoke_skill`) derives to `host`,
   so `os_sandbox` builds report `subprocess_only` **by default** — this is
   the RFC's expected steady state, now pinned by a test that forces a
   guest-only surface (patched `resolve_placement`) to observe `full`.
5. **Env allowlist default** includes `LC_CTYPE` and `TERM` beyond the RFC
   example (locale-correct subprocess output and non-TTY-safe children).
6. **Seatbelt smoke on macOS 26.6 (v1 shipped)**: in-ws write/mkdir/mv
   exit 0; outside write `Operation not permitted`; `/dev/null` writable;
   network connect blocked; env sentinel secret absent; timeout → 124 +
   note; background PID host-visible and killable; upload/download escapes
   (incl. symlink) rejected. The integration suite encodes these.
7. **Testing seams**: `OsLocalSandbox(config, popen=...)` accepts a Popen
   double (the tests record argv/env/cwd without exec); probes are
   monkeypatched for cross-platform unit tests. Linux bwrap/landlock suites
   gate on the real probes at import time and skip elsewhere.
8. **Gates**: ruff check/format green (changed files); nano unit suite
   2165 passed / 44 skipped on darwin. mypy strict surfaces 3 pre-existing
   findings outside this IG's diff hunks (nano CI runs no mypy target).
9. **Docs**: `docs/os-sandbox-backend.md` mirrors the arcbox-backend doc
   structure (contract, tier matrix, boundaries, config, limitations);
   CHANGELOG `[Unreleased]` carries the Added/Fixed/Changed entries.
10. **Isolation-capability integration suite (2026-11-23, second pass)**:
    `tests/integration/test_os_sandbox_isolation.py` (marker
    `integration`, `--run-integration` gated; tier-parametrized fixture
    probe-derived at import). Matrix: filesystem boundary (in/out writes,
    system-tree writes, mkdir, /dev/null), network (host-listener connect
    blocked — seatbelt/bwrap, landlock explicitly skipped as no-network-isolation),
    env hygiene (sentinel secret, HOME/TMPDIR/cwd), process lifecycle
    (timeout kills the whole process group incl. background child;
    background PID host-visible and host-killable — the §5.3 identity PID
    contract), path escapes (relative/../upload/download/symlink), bwrap
    kernel filters (NoNewPrivs=1, Seccomp=2, `unshare(2)` denied), posture
    per tier. Verified: macOS 26.6 Seatbelt (16 passed, 2 bwrap-only
    skips) and anydev4 (devcloud any4, Linux 5.4 tlinux4, 96-core,
    root-in-container) bwrap full tier — **18/18 passed**.
11. **anydev4 root-in-container finding → new spawn strategy**: the
    devcloud any4 host is root inside a container without `CAP_SYS_ADMIN`
    over the initial namespace — bwrap as euid 0 skips its own userns and
    fails namespace creation with EPERM (while `unshare --user
    --map-root-user` works, because a userns grants caps over itself).
    Implemented in `os_local.py`: `_bwrap_spawn_prefix()` (empirical
    direct-then-nested round-trip detection, cached per process) +
    `_bwrap_proc_args()` (the userns-wrapped spawn binds the host /proc
    read-only instead of a fresh `--proc` mount — bwrap < 0.5's
    sysrq-trigger probe fails on userns-mounted proc; visibility is
    equivalent because the PID ns is never unshared). `_probe_bwrap` and
    `_BwrapSession.build_argv` share the resolved form, so a passing probe
    guarantees a working spawn. Unit tests cover the nested argv shape and
    the detection matrix (non-root never probes; root direct-failure →
    nested; both-failure → tier refused).
