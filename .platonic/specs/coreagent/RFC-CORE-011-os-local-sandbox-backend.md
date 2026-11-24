# RFC-CORE-011: os_sandbox — Local OS-Primitive Sandbox Backend for KVM-less Hosts

**RFC**: 907
**Title**: os_sandbox — Local OS-Primitive Sandbox Backend for KVM-less Hosts
**Status**: Draft
**Kind**: Architecture Design
**Created**: 2026-11-23
**Authors**: Xiaming Chen
**Depends on**: RFC-CORE-003, RFC-CORE-008
**Input**: [2026-11-23 agent-sandbox-no-kvm analysis](../../analysis/2026-11-23-agent-sandbox-no-kvm.md) (survey of sandboxing without `/dev/kvm`, incl. OpenAI Codex CLI and Anthropic sandbox-runtime evidence)
**Related**: [IG-CORE-007](../../impl/coreagent/IG-CORE-007-os-local-sandbox-backend.md) (implementation architecture and coding plan)

---

## 1. Abstract

This RFC specifies `os_sandbox`, a local sandbox backend for the agent tool-execution plane that requires no hardware virtualization. It wraps spawned subprocesses with OS-level primitives — macOS Seatbelt profiles, Linux bubblewrap mount-namespaces plus seccomp-bpf and `no_new_privs`, and an explicitly degraded Landlock tier — giving KVM-less hosts (macOS laptops, CI containers, locked-down Linux servers) a real isolation boundary between agent-generated commands and the host. The backend implements the existing `BaseSandbox` contract unchanged, keeps framework state on the host via the existing `CompositeBackend` routing, and introduces `sandbox_posture` (`full | subprocess_only | none`) as an explicit, observable declaration of what is actually isolated. Two P0 consistency bugs in the current sandbox wiring (silent host-backend fallback, config-path missing `socket_path`) are fixed as mandatory precursors.

---

## 2. Scope and Non-Goals

### 2.1 Scope

* A new backend type `sandbox.backend.type = "os_sandbox"` in the soothe-nano tool-execution plane.
* The OS-primitive matrix per platform and the degrade ladder (full tier → Landlock tier → explicit refusal), with availability probes that fail loudly.
* Session-scoped sandbox instances so background-process tools (`run_background` with `setsid`, `tail_background_log`, `kill_process`) stay namespace-consistent across calls.
* The egress-and-credential seam contract (L2): what the host guarantees around a sandboxed subprocess.
* The `sandbox_posture` surface and its config validation, including interaction with `hard_deny_severities` (RFC-CORE-008 / RFC-LOOP-030).
* Two P0 fixes in the sandbox wiring (`sandbox/factory.py` config-driven path; `agent/builder.py` build-time fallback).
* Repository placement: implementation lands in the **soothe-nano repository** (agent axis); the soothe host repo consumes it via a floor bump. No host-side runtime code.

### 2.2 Non-Goals

* Hardware virtualization. ArcBox (microVM) remains the only hardware-level isolation tier and is unchanged by this RFC.
* Loop-plane isolation. On KVM-less hosts `loop_runner.runner_mode` provides no isolation; this RFC does not touch the daemon/loop plane.
* gVisor/systrap, container runtimes, and remote execution as backends. The input analysis covers them; they are follow-up tiers, not this RFC.
* Language-runtime-layer isolation (virtual shells, Python capability models). Not an OS boundary; rejected as a substitute in the analysis.
* Auto-wrapping MCP servers or other long-running external processes. Codex deliberately does not (W-001 §sandboxing); we follow the same default — only the shell tool plane is routed through the backend.
* A full network-egress proxy implementation. This RFC fixes the seam contract (§5.5); the proxy service itself is a separate follow-up spec.

---

## 3. Background and Motivation

### 3.1 ArcBox needs hardware virtualization

Today's only sandbox backend with a real isolation boundary is ArcBox: on Linux it needs `/dev/kvm`; on macOS it needs nested virtualization on M3+. A large fraction of real deployment surfaces have neither — developer MacBooks, CI containers, locked-down Linux hosts. On those hosts the sandbox subsystem is effectively unusable, and the agent's shell plane runs against the host directly.

### 3.2 The wiring makes it worse (two P0 bugs)

The current wiring does not fail loudly when the sandbox cannot engage:

* `soothe_nano/sandbox/factory.py` (config-driven path, ~L105-117): constructs the backend without `socket_path`, so the config path cannot produce a working ArcBox backend even when the daemon side exists.
* `soothe_nano/agent/builder.py` (~L574-591): catches `ArcBoxError` during build and **silently falls back to the host backend**. A user who configured a sandbox gets host execution with no signal.

Both must be fixed before any new backend is meaningful — otherwise `os_sandbox` would inherit the same silent-fallback failure mode.

### 3.3 The research conclusion that shapes this design

The input analysis (50 primary sources) establishes two facts:

1. KVM absence only closes the microVM door. Kernel security modules (Landlock), syscall filtering (seccomp-bpf + `no_new_privs`), namespaces (bubblewrap), and user-space kernels (gVisor systrap) all work without hardware virtualization.
2. Production precedents exist for exactly this shape: OpenAI Codex CLI wraps every agent-spawned command via `spawn_command_under_sandbox()` (macOS Seatbelt, Linux `codex-linux-sandbox` coordinating bwrap → launcher seccomp → exec), and Anthropic's sandbox-runtime wraps arbitrary CLI/library processes with a deny-by-default egress.

But both are **command wrappers, not execution backends**: their isolation covers the spawned subprocess only. Naively wrapping `HostShellExecutor` from the outside would leave the framework's own FS tools (host/shuttle placement) executing inside the host process with no boundary. The correct adoption is the inverse: implement their OS-primitive mechanics *inside* a `BaseSandbox.execute` implementation, so the existing placement/composite machinery decides what runs where, and the backend provides the kernel boundary for everything routed to it.

### 3.4 The placement gap must be visible

`soothe_nano/sandbox/placement.py` (~L124) defaults unlabeled tools to `PLACEMENT_HOST`. In sandbox mode the five shell tools rebind to `BackendShellExecutor` (via `utils/resolver.py` ~L333-349), but FS-primitive tools with host or shuttle placement still run in the host process. That is a deliberate architectural boundary, not a bug — but it must never be silent. Hence `sandbox_posture`.

---

## 4. Design Principles

1. **Explicit posture, never silent fallback.** If the configured isolation cannot engage, startup fails loudly. The achieved posture is a first-class, observable value.
2. **Isolation at the process boundary, policy in the host.** The kernel primitives isolate subprocesses; allow/deny/approval decisions stay in `OperationSecurityProtocol` (RFC-CORE-008) on the host side, unchanged.
3. **One backend contract, many OS fronts.** No changes to the `BaseSandbox` four-member contract; each platform is a strategy inside one backend.
4. **Framework state outlives the sandbox.** `/memories`, `/.soothe`, `/large_tool_results`, `/conversation_history` route back to the host backend via the existing `CompositeBackend` wrapping — never into the sandbox view.
5. **Credentials never cross the boundary.** The subprocess environment is constructed by the host and contains no secrets; per-request credentials are injected by the policy layer, not exported.
6. **Degradation is a declared tier, not a quiet downgrade.** Landlock is a documented, opt-in, degraded tier — never a transparent substitute for the full tier.

---

## 5. Specification

### 5.1 Backend identity and contract

`sandbox.backend.type = "os_sandbox"` selects the new backend. The class implements the existing sandbox backend protocol with exactly the current members — `id`, `execute`, `upload_files`, `download_files` — and derives FS primitives from `execute` via embedded `python3 -c` calls, exactly like existing backends. **Zero protocol change**: the trigger chain (`agent/builder.py::_extract_sandbox_backend` → `utils/resolver.py` rebinding the five shell tools to `BackendShellExecutor`) is reused as-is.

### 5.2 Platform matrix

#### 5.2.1 macOS — Seatbelt

Each spawned subprocess is launched under a generated Seatbelt (sandbox-exec) profile:

* Root rule `(deny default)`.
* `file-write*` allowed **only** under the session workspace and the session scratch directory (resolved subpaths — `/tmp` firmlinks resolve to `/private/tmp`, so subpath filters must use resolved real paths), plus write-data to `/dev/null`.
* `process-exec` and `process-fork` allowed broadly; the command runs under `/bin/sh -c` (zsh fails to start under deny-default).
* `network*` denied by default (egress tier §5.5 can open an explicit relay).

**v2 read-side (parity with the bwrap bind list)**: `file-read*` is allowed only for the system tree set (`/usr`, `/bin`, `/sbin`, `/System`, `/Library`, `/opt`, `/private/etc`, `/private/var/select`) plus the workspace/scratch and the entropy devices (`/dev/urandom`, `/dev/random`, `/dev/null`). Everything else — other users' data under `/Users`, `/private/var/db`, `/Applications`, host `/private/tmp` — is denied. Three macOS-specific allowances keep the toolchain alive: `file-read-metadata` (literal) on the firmlink/volume roots and every workspace/scratch ancestor (path resolution; git `lstat`s the cwd chain), `file-read-data (literal "/")` (`getcwd` walks `..`; top-level names only), and the `/private/var/select` shell-selection tree consulted when exec'ing `/bin/sh`. Paths are resolved before embedding — seatbelt checks firmlink-redirected paths in their resolved form.

**v1 read-side trade-off (superseded)**: the original finding blamed narrow reads for SIGABRT at exec ("dyld resolves library paths through firmlinks that only match under unrestricted reads"). The v2 bisect corrected this: the abort was the missing `/` root-dir readdir (getcwd) and the `/private/var/select/sh` read, not firmlink resolution. Narrow reads ship today with the full toolchain verified (sh, git, python).

#### 5.2.2 Linux — bubblewrap + seccomp (full tier)

Two-stage spawn, mirroring Codex's `codex-linux-sandbox` coordination:

1. **bubblewrap** builds the process environment: mount namespace with read-only binds of `/usr`, `/lib*`, `/etc`; **identity** read-write bind of the session workspace (guest path == host path, one kernel, one filesystem — no path rewriting); tmpfs on `/tmp`; `/dev` via `--dev`; `--die-with-parent --unshare-net`. **No `--new-session`** (implementation revision): `start_new_session` on the spawn already detaches the tree from the host controlling terminal, and an inner `setsid` moves sandboxed children out of the tracked process group — timeout/close `killpg` could not reach background children after the outer command exited. **PID namespace is intentionally NOT unshared** — background PIDs stay host-visible so `kill_process` / `tail_background_log` semantics are unchanged (§5.3).
2. **launcher** inside the namespace applies a seccomp-bpf syscall filter plus `PR_SET_NO_NEW_PRIVS` (both are mandatory — seccomp without `no_new_privs` is bypassable by privilege escalation), then `exec`s the target command.

**Filter shape (implementation revision)**: the seccomp filter is an **arch-aware deny-list**, not an allowlist. Unknown syscalls return `ENOSYS` (libc falls back gracefully for benign newer syscalls like `rseq` / `clone3`-fallback), while the denied set — namespace and privilege escapes (`unshare`, `setns`, `mount`, `pivot_root`, `ptrace`, `clone3`, `bpf`, module loading, `perf_event_open`, …) — fails outright, and unknown architectures are denied. `PR_SET_NO_NEW_PRIVS` is the load-bearing privilege-escalation block and is always applied before the filter; the userns/mount-ns capability checks carry the boundary, with seccomp as defense in depth on top. The BPF program is built parent-side (unit-testable) and shipped to the in-namespace launcher as hex.

Requirements: unprivileged **capable** user namespaces for bwrap. The constructor probes this (create + map a range) and fails loudly when unavailable — "namespace creation succeeds" is not proof of "namespace carries capabilities" (a documented common misjudgment in the analysis). One environment shape deserves note: hosts that are **root inside a container without `CAP_SYS_ADMIN`** over the initial namespace reject bwrap's direct mount-namespace creation; the implementation detects this with a real round-trip and wraps bwrap in `unshare --user --map-root-user` (a userns grants full caps over itself), binding the host `/proc` read-only instead of a fresh proc mount in that form. The probe and every subsequent spawn use the same resolved form — a passing probe guarantees a working spawn.

#### 5.2.3 Linux — Landlock (degraded tier)

Kernel ≥ 5.13. The backend self-applies a Landlock ruleset restricting file access to the workspace + scratch dirs (read-only elsewhere denied). This tier provides **file-access denial only** — no filesystem view, no network restriction, no process isolation — and is therefore:

* opt-in (`sandbox.backend.os_sandbox.linux_tier = "landlock"` or explicit `allow_degraded: true`);
* reported as `sandbox_posture = subprocess_only` with engine tag `landlock` so downstream consumers can distinguish it from the full tier.

#### 5.2.4 Availability probe and refusal

Backend construction runs a capability probe for the requested tier (Seatbelt present; bwrap + capable userns; Landlock syscall available). On failure: raise a configuration error naming the missing prerequisite. There is **no** code path from a configured `os_sandbox` to host execution.

### 5.3 Session-scoped instances and background processes

One sandbox instance per agent **session**, not per call. The workspace bind, scratch/tmp dirs, and (Linux) the namespace skeleton must be identical across every tool invocation in the session, otherwise:

* `run_background` (which `setsid`s the child) would orphan the process from later `tail_background_log` / `kill_process` calls;
* per-call mounts would make path identity unstable between read and write.

Consequently: background children are spawned through the same session sandbox entry point (same bwrap skeleton / same profile), so they land in the same view and remain enumerable by the background-process tools. Because the PID namespace is not unshared, background PIDs are host-visible and `kill_process` kills them directly — no pid mapping layer exists or is needed. Command timeouts kill the per-command process group (`killpg`); session teardown removes the scratch directory. On macOS, per-process profiles are regenerated per exec but derive from one session profile template (same allow/deny set), keeping semantics uniform.

### 5.4 Defense layers

| Layer | Boundary | Owner | Spec |
|---|---|---|---|
| L1 | Kernel isolation of spawned subprocesses | `os_sandbox` backend (nano) | This RFC |
| L2 | Egress + credential hygiene around the subprocess | host, at `CommandExecutor` env construction | This RFC §5.5 |
| L3 | Application-layer operation policy | `OperationSecurityProtocol` | RFC-CORE-008, unchanged |
| L4 | Loop/daemon plane | `loop_runner.runner_mode` | Out of scope; none on KVM-less hosts |

The layers are additive: L1 without L2 is a sandboxed process that can still exfiltrate via an open egress; L2 without L1 is a filtered process with full filesystem reach.

### 5.5 Egress and credential seam (L2)

The host-side `CommandExecutor` (env construction site, `toolkits/shell.py`) is the single seam where the subprocess environment is built. Contract:

* **Environment allowlist**: the child env is constructed from an explicit allowlist (`PATH`, `HOME` → workspace-relative, locale, `TMPDIR` → sandbox scratch). No wholesale env inheritance.
* **No credentials**: tokens, API keys, and DSNs never enter the child env; per-request secrets are injected by the policy/approval layer only for explicitly approved operations (mirrors the analysis: "credentials should be injected by the policy layer, not resident in the guest").
* **Egress policy shape** (contract now, proxy implementation later): domain allowlist, default deny; DNS resolution happens only after the domain is approved; deny loopback, link-local, private ranges, cloud metadata (`169.254.169.254`), and raw IP literals (deny-by-IP-bypass). On the Linux full tier, the bwrap skeleton defaults to a network namespace with loopback only; any egress relay is a host-side process outside the sandbox.

### 5.6 Posture surface and config validation

`sandbox_posture` is a derived, read-only value exposed on the session/builder metadata and emitted in session start events:

* `full` — a sandbox backend is engaged and every FS/shell tool is routed through it (all placements resolved to `guest`/backend-executed).
* `subprocess_only` — the shell plane is sandboxed, but FS-primitive tools with `host`/`shuttle` placement still execute in the host process, or the backend is on the Landlock degraded tier. This is the **expected steady state** for `os_sandbox` on default placement tables.
* `none` — no backend (explicit `sandbox.backend.type = "none"`), allowed only when the host config also opts out explicitly.

Validation rules at config load:

* `sandbox_posture` must be consistent with `sandbox.backend.type` (e.g. `os_sandbox` + Landlock tier can never yield `full`).
* When `hard_deny_severities` (RFC-CORE-008 / RFC-LOOP-030) is set below `critical` **and** the sandbox is disabled, agent build requires `sandbox.acknowledge_no_isolation = true` — the two risk-relaxing knobs must never be silently combined.

### 5.7 P0 consistency fixes (mandatory precursors)

Both fixes land in the **soothe-nano repository**; this repo bumps its nano floor when the fix releases:

1. **factory.py config path**: the config-driven construction (~L105-117) must pass `socket_path` identically to the programmatic path. Symptom today: config-built ArcBox backend is non-functional.
2. **builder.py fallback removal**: `_wrap_sandbox_backend`/build path (~L574-591) must not catch `ArcBoxError` (or, after this RFC, `OsSandboxError`) and continue with the host backend. Sandbox construction failures abort agent build with the original error.

### 5.8 Repository placement and dependency direction

* Implementation: `soothe_nano/sandbox/` (agent axis, nano repo). No new dependencies beyond stdlib `subprocess`; bwrap/seatbelt invocation is external process calls; Landlock via `ctypes` syscall (no third-party binding).
* Host repo (soothe): config schema/documentation updates + nano floor bump in the dependency pin. No host runtime code — the backend is injected through the existing factory.
* Placement table: no changes required; existing `guest`/`shuttle`/`host` semantics apply unchanged.

### 5.9 Acceptance criteria

* **Unit** (nano): profile/ruleset generation per platform; env allowlist scrubbing; session-scoped instance lifecycle; posture derivation matrix; config validation rejections.
* **Integration — Linux CI**: bwrap full tier (write-outside-workspace denied; `no_new_privs` and seccomp active — assert via `/proc/self/status` inside the sandbox); Landlock tier on kernels ≥ 5.13 (file denial asserted, degraded posture reported).
* **Integration — macOS local** (not CI-blockable): Seatbelt profile denies writes outside workspace + network by default.
* **Background lifecycle**: `run_background` → sleep process → `tail_background_log` reads → `kill_process` kills, all through the same session sandbox.
* **Credential hygiene**: child env contains no keys from the host env (fuzz: seed host env with sentinel secrets, assert absence).
* **No-fallback**: with `os_sandbox` configured and prerequisites missing, agent build fails; no test may observe host execution.
* **P0 regressions**: two dedicated tests asserting config-path `socket_path` propagation and no-build-fallback.

---

## 6. Examples

### 6.1 Configuration

```yaml
sandbox:
  enabled: true                # was: enabled + arcbox (needs KVM)
  acknowledge_no_isolation: false  # required true only when sandbox off
                                      # AND hard_deny_severities drops critical
  backend:
    type: os_sandbox
    os_sandbox:
      workspace_root: null     # null = builder binds the effective tool workspace
      linux_tier: auto         # auto | bwrap | landlock
      allow_degraded: false
      default_timeout: 120
      env_allowlist: [PATH, LANG, LC_ALL, LC_CTYPE, TERM, TMPDIR, HOME]
      egress:
        enabled: false         # default deny; relay is a follow-up (fail-loud)
```

### 6.2 Execution sequence (Linux full tier)

```text
tool call: run_command("cargo test")
  └─ resolver: shell tool bound to BackendShellExecutor (sandbox mode)
      └─ CompositeBackend: workspace path → os_sandbox; /.soothe, /memories → host
          └─ os_sandbox.execute(cmd, workspace_view)
              ├─ CommandExecutor: build env from allowlist (no credentials)
              ├─ bwrap --ro-bind /usr /usr --bind <ws> <ws> --tmpfs /tmp ...
              │    └─ launcher: no_new_privs + seccomp deny-list → exec cargo test
              └─ capture streams, exit code → tool result
```

### 6.3 Posture reporting

```json
{
  "sandbox_posture": "subprocess_only",
  "sandbox_backend": "os_sandbox",
  "engine": "bwrap+seccomp",
  "host_placement_tools": ["read_file", "write_file", "list_dir"]
}
```

The `host_placement_tools` list is the honest ledger of what is *not* behind the kernel boundary.

---

## 7. Relationship to Other RFCs

* **RFC-CORE-003** (Secure Filesystem Path Handling and Security Policy): the sandbox view is an additional enforcement layer beneath path policy; policy remains authoritative on the host.
* **RFC-CORE-008** (OperationSecurityProtocol): unchanged L3 policy; this RFC adds `sandbox_posture` validation interacting with `hard_deny_severities`.
* **RFC-CORE-004** (Thread-Aware Workspace): session-scoped sandbox instances bind to the thread workspace resolution.
* **RFC-CORE-007** (Workspace Host Convention): path mapping for containerized daemons composes with the bwrap bind set; the workspace view is constructed from already-mapped host paths.
* **RFC-LOOP-030** (Risk-Tiered Security): posture validation guard references its `hard_deny_severities` knob.
* **RFC-CORE-010** (Workspace Sync): the materialized workspace tree is exactly what gets bound read-write into the sandbox; no sync changes.

---

## 8. Open Questions

* **Egress proxy**: a session-scoped relay with domain allowlist and DNS-after-approval is the natural follow-up spec — is it its own RFC (PROT or CORE namespace)?
* **gVisor systrap tier**: viable Linux-only escalation between bwrap and microVM; worth a tier on the ladder later?
* **seccomp deny-list maintenance**: the shipped arch-aware deny-list covers namespace/privilege escapes; new escape syscalls (or per-language tightening toward an allowlist) — who owns updates?
* **Hard-crash orphans on seatbelt/landlock**: SIGKILL of the host interpreter bypasses close/atexit cleanup; only bwrap carries kernel-level `--die-with-parent`. A host-side reaper (pidfile sweep at next start) could shrink the window — worth it?
* **macOS Seatbelt deprecation posture**: Apple's long-term direction for `sandbox-exec` is undocumented; if removed, macOS falls to `subprocess_only`-by-policy or posture `none` — should the backend pre-compute that guard?
* **MCP server wrapping**: intentionally excluded (Codex precedent); revisit if MCP tools become an exfiltration surface in practice.

---

## 9. Conclusion

`os_sandbox` closes the practical gap the analysis identified: on hosts without `/dev/kvm`, the choice is not "microVM or nothing" — Seatbelt and bubblewrap+seccomp give the shell plane a real kernel boundary today, with production precedents in Codex and Anthropic's sandbox-runtime. The design keeps Soothe's architecture intact: one backend contract, the existing placement machinery deciding routing, framework state on the host, policy on the host, and a `sandbox_posture` value that tells the model and the operator exactly how much isolation exists. The two P0 fixes make "configured a sandbox" mean "got a sandbox, or got a loud failure" — which is the property the whole design depends on.

> The sandbox is a subprocess boundary plus an honest posture label — never a silent fallback to host execution.
