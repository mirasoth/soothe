# RFC-CORE-008: OperationSecurityProtocol for Workspace and Tool Execution

**RFC**: 901
**Title**: OperationSecurityProtocol: Unified Workspace and Tool Operation Security
**Status**: Implemented
**Kind**: Architecture Design
**Created**: 2026-04-30
**Authors**: Soothe Team
**Updated**: 2026-10-30
**Depends on**: RFC-CORE-003, RFC-CORE-004, RFC-PROT-005, RFC-613
**Note**: Reclassified from 6xx to 9xx segment (security/policy specs consolidation).

---

## Abstract

Introduces `OperationSecurityProtocol`, a dedicated security protocol for operation-level enforcement across filesystem and execution tools. Centralizes security checks currently scattered between policy, workspace backend path normalization, and tool-local command guards. Evaluates normalized operation intents (path access, shell execution, process management) and returns a structured allow/deny/need-approval decision before permission-profile matching proceeds.

---

## Motivation

Current security controls are effective but fragmented:

- Workspace/path checks are implemented in `ConfigDrivenPolicy` and backend path resolution.
- Shell command safety checks are primarily local to `run_command`.
- Equivalent execution surfaces such as `run_background` are not uniformly checked by policy.
- There is no single structured decision record for operation-level security decisions.

This fragmentation increases the chance of drift between tools and makes audits harder.

---

## Design Goals

1. **Single operation-security contract** for filesystem and execution tools.
2. **Workspace-first path enforcement** aligned with RFC-CORE-004 dynamic workspace resolution.
3. **Consistent execution risk checks** across all command-bearing tools.
4. **Composable with PolicyProtocol** (operation checks first, permission checks second).
5. **Structured reasoning** for telemetry, approval UX, and troubleshooting.

---

## Protocol Interface

```python
class OperationSecurityProtocol(Protocol):
    def evaluate(
        self,
        request: OperationSecurityRequest,
        context: OperationSecurityContext,
    ) -> OperationSecurityDecision:
        ...
```

### Core Types

- `OperationSecurityRequest`
  - `action_type`: `tool_call`, `subagent_spawn`, `mcp_connect`
  - `tool_name`: tool identifier
  - `tool_args`: original arguments
  - `operation_kind`: normalized operation kind (`filesystem_read`, `filesystem_write`, `shell_execute`, `python_execute`, `process_control`, `generic`)
  - `target_path`: extracted path-like target when available
  - `command`: command string for execution tools when available

- `OperationSecurityContext`
  - `thread_id`: optional thread id
  - `workspace`: optional dynamic workspace root
  - `security_config`: optional security configuration object

- `OperationSecurityDecision`
  - `verdict`: `allow`, `deny`, `need_approval`, `risk_hint`
  - `reason`: human-readable reason
  - `rule_id`: optional stable rule id for auditing
  - `severity`: optional rule risk level (`critical`, `high`, `medium`)

---

## Evaluation Model

Operation security is evaluated before profile permission matching:

1. Normalize operation kind from tool metadata.
2. Apply operation-specific rules:
   - Filesystem operation -> path and file-type checks (RFC-CORE-003 semantics).
   - Execution operation -> command safety checks.
3. Map the matched rule's severity through `security.hard_deny_severities`
   (default `{"critical"}`):
   - severity in the hard set (or an operator-configured match) -> `deny`.
   - sub-critical match -> `risk_hint` carrying `rule_id` + `severity`.
4. Return decision:
   - `deny` or `need_approval` short-circuits permission matching.
   - `risk_hint` is a hard-rule miss with advisory metadata: enforcement
     points without a judgment layer treat it as **allow-with-audit** (the
     action executes; the decision carries the rule id / severity and logs a
     warning). Judgment-capable hosts (soothe) consume the hint as dynamic
     routing input instead of allowing blindly.
   - `allow` proceeds to existing `PolicyProtocol` permission checks.

---

## Reference Rules

### Filesystem Rules

- Denied path patterns (`security.denied_paths`) are highest priority.
- Path must match allowed patterns (`security.allowed_paths`).
- Workspace boundary enforced using dynamic workspace (`context.workspace`) first.
- File-type deny and approval lists are applied.
- Severity is operation-kind aware: sensitive system paths and the workspace
  boundary are `critical` on write and advisory (`high`/`medium`) on read;
  dangerous path components (`.git`, `.env*`, shell rc, …) are `high` on
  write, `medium` on read. Operator-configured matches stay absolute
  (`critical`).

### Execution Rules

- Block explicit destructive command patterns (for example, recursive root deletion and privileged destructive sequences).
- Rules apply consistently to command-bearing host execution tools:
  - `run_command` (SDK display aliases: `shell`, `bash`)
  - `run_background`
- Only `critical` rules (irreversible system destruction, device writes,
  pipe-to-shell, daemon/process self-protection) hard-deny by default; the
  remainder (privileged execution, recursive permission changes, package
  installs, force-push, …) surface as `risk_hint`. The rule registry is the
  single source of truth — soothe's built-in host-side deny list has been
  consolidated here (RFC-LOOP-030 §4.4).

---

## Integration Plan

1. Add `OperationSecurityProtocol` and models in `soothe.protocols`.
2. Add `WorkspaceToolOperationSecurity` implementation in `soothe.core.security`.
3. Update `ConfigDrivenPolicy.check()` to call operation security first for tool calls.
4. Keep existing permission-profile logic unchanged after operation-security pass.
5. Add tests for:
   - Outside workspace path decisions.
   - File type approval decisions.
   - `run_background` and `run_command` command denial parity.

---

## Backward Compatibility

- Existing `PolicyProtocol` public contracts remain unchanged.
- Existing profile names and permission sets remain unchanged.
- Rules inside the hard set (`critical`, plus operator-configured matches)
  keep their existing deny semantics.
- **Posture change (RFC-LOOP-030)**: sub-critical rule matches no longer
  hard-deny by default — they return `risk_hint` and downstream consumers
  without a judgment layer treat them as allow-with-audit. Operators restore
  the previous behavior by setting `hard_deny_severities` to include `high`
  and `medium`.

---

## Security Considerations

- Centralized checks reduce policy drift and bypass risk.
- Dynamic workspace from execution context remains the source of truth for boundary checks.
- Structured `rule_id` enables stable auditing and future allowlist exception workflows.

---

## References

- RFC-CORE-003: Secure Filesystem Path Handling and Security Policy
- RFC-CORE-004: Thread-Aware Workspace
- RFC-PROT-005: PolicyProtocol Architecture
- RFC-613: Explore Agent LLM-Orchestrated Search
- RFC-LOOP-030: Risk-Tiered Security, Loop Grants, and the Veritas Decision
  Fast Path (severity tiers, `risk_hint` consumption, grant write-back)

---

## Changelog

- 2026-10-30: RFC-LOOP-030 adds severity tiering to the rule registry and a
  `risk_hint` verdict; `OperationSecurityDecision` gains `severity`, and
  `SecurityConfig` gains `hard_deny_severities` (default `{"critical"}`,
  uniform for all downstream consumers). soothe's built-in tool-approval deny
  rules consolidate into this registry. `allow` / `deny` / `need_approval`
  semantics for rules inside the hard set are unchanged.
