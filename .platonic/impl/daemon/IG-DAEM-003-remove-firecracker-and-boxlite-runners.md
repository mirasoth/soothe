# IG-DAEM-003: Remove Firecracker and BoxLite Runner Substrates

**Implementation Guide**: IG-DAEM-003
**Title**: Remove Firecracker and BoxLite loop runner substrates
**Status**: Implemented
**Created**: 2026-11-23
**Dependencies**: §7b (Package Boundaries), Rule 6 (Cleanse → Verify → Fix), Rule 19 (Dead Code & Legacy Removal)
**Related**: IG-CORE-005 / IG-CORE-006 (deleted with the feature), `.platonic/analysis/2026-11-23-agent-sandbox-no-kvm.md`

---

## Problem

`soothe-daemon` carried five loop runner substrates (`thread_pool`, `process_pool`,
`ray`, `firecracker`, `boxlite`). The two sandbox substrates share a structural
credential-propagation weakness: because LLM calls happen inside the worker,
`api_key` / `api_base_url` must be materialized in the execution environment.
`spawn_safe_config` does not pre-expand `${ENV}` placeholders, and neither the
microVM guest nor the boxlite container inherits daemon env vars — so operators
must write **literal keys into the config**, which then travel pickled into every
spawned VM/container.

Both substrates are also niche in practice: Firecracker is Linux+KVM only, and
BoxLite carries a non-trivial optional dependency (`boxlite`, platform-restricted
wheels) that exists solely for these two modes. Neither is on any default path.

## Decision

Remove both substrates. The runner surface becomes `thread_pool` (default) |
`process_pool` | `ray`. Stronger isolation, if needed later, should be designed
with a credential-broker approach (keys resolved in the daemon, not shipped into
the sandbox) rather than resurrecting literal-key propagation — see the
referenced analysis for the design space.

## Changes

1. Deleted modules: `soothe_daemon/runner/firecracker_runner.py`,
   `soothe_daemon/runner/boxlite_runner.py` and their test files
   (`tests/unit/runner/test_firecracker_runner.py`, `test_boxlite_runner.py`).
2. `LoopRunnerFactory`: removed both modes from constructor validation,
   `get_shared_execution_pool`, `initialize_pool`, `shutdown_pool`,
   `create_runner`; the identity-runtime guard now rejects only `process_pool`.
3. `config/models.py`: deleted `FirecrackerConfig` and `BoxLiteConfig`;
   `LoopRunnerConfig.runner_mode` Literal narrowed to three modes; nested fields
   removed; `config/__init__.py` exports cleaned.
4. `setup/templates/daemon.yml`: removed both blocks and mode-list comments.
5. `pyproject.toml`: removed the `sandbox` optional extra (`boxlite` dependency);
   `uv.lock` regenerated (boxlite dropped).
6. `.agents/rules/package-boundaries.md`: runner placement row updated to
   "thread / process / ray".
7. IG-CORE-005 / IG-CORE-006 deleted; RFC-LOOP-017's runner-implementation
   row and the wiki security page's sandbox posture wording updated to drop
   microVM / container-jail references.

## Verification

- `uv lock` resolved cleanly with boxlite removed.
- Full gate via `./scripts/verify_finally.sh` (lint, tests, import boundaries,
  vulture) on the working tree after removal.
