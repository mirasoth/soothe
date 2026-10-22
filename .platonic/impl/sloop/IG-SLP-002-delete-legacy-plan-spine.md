# IG-SLP-002: Delete Legacy Plan Spine

> Status: **Done** (station deletion + resume remap + budget re-home).
> Spec: [RFC-SLP-002](../../specs/sloop/RFC-SLP-002-sloop-recursive-decomposition.md), [RFC-LOOP-027](../../specs/loop/RFC-LOOP-027-planner-plan-artifact-and-human-review.md)
> Depends: [IG-SLP-001](IG-SLP-001-sloop-recursive-decomposition.md) P3 graph cutover

---

## 1. Executive Summary

The live StrangeLoop graph is DISPATCH-centric (RFC-SLP-002). This IG deleted the
unwired plan-spine stations, remapped clarification resume to `DISPATCH`,
re-homed the iteration budget gate onto DISPATCH, and removed spine-only unit
tests.

**Follow-up:** deleting `LLMPlanner` / `PlanPhase` and plan prompt kinds — see IG-SLP-003.

---

## 2. Done

| Phase | Scope |
|-------|--------|
| **A** | Clarification origins → DISPATCH |
| **B** | `enforce_loop_budget` on DISPATCH; deleted `check_limits` |
| **C** | Deleted `generate_plan` / `assess` / `evaluate` / `gather_evidence` / `_helpers` / `commit_plan` |
| **D** | Removed spine stage tests; updated cancel-then-retry + clarification routing |

## 3. Kept

- `stages/plan/phase_status.py`
- `PlanResult` / `AgentDecision` / ledger phase tags (`PLANNING_LEDGER_PHASES`)
- `plans/grounding.py`
- Legacy clarification origins (resume → DISPATCH); not exported as live station IDs
