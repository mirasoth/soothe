# Necessity Assessment

A prerequisite gate when a change adds behavior. Judge by the **actual diff** —
requirement descriptions and implementations routinely diverge, and the
implementation is authoritative.

Every added public capability carries long-term costs: an API contract to keep,
documentation and examples to maintain, tests to run, dependencies to carry,
and cross-version compatibility to preserve. Ask whether those costs are
warranted before reviewing how well the code is written.

Language-agnostic by contract. No language syntax, product path, or product
identifier belongs here.

## 1. Responsibility boundary

- Capability that belongs to a lower-level or external component belongs there.
  Prefer upgrading or integrating the existing component over reimplementing it.
- Defects in an upstream component belong upstream; a local workaround should
  have a documented removal condition.
- **Test**: if the external component were fixed tomorrow, would this
  implementation be deleted? If yes, it does not belong here.

Weigh the cost of an extra dependency lock and upgrade conflicts, compatibility
matrices across runtimes and platforms, documentation and example upkeep, and a
widening test matrix. When cost clearly exceeds benefit, delegate externally.

## 2. Real demand

- The requirement comes from a real scenario, a user need, or an explicit
  comparison — not from speculation.
- Check whether existing capability already covers most of the need. Is the new
  coverage a long tail or a frequent case?
- Compare the benefit (capability gain, measurable performance) against the
  cost (contract, docs, tests, examples, compatibility).
- A one-off need of a single caller should be composed at the caller, not added
  to shared code.

## 3. Reuse and decomposition

- Search the repository for existing capability with the same shape before
  adding a new unit. Start from public entry points and exported symbols, then
  follow their references and callers.
- A new capability that is a composition of existing units should not be added
  as its own unit; compose it at the call site.
- When a sub-step is independently reusable, extract it as an atomic unit and
  compose the higher-level behavior from it. Benefits: reuse, isolated testing,
  and clear boundaries.
- Cite file:line evidence for every "reuse" or "delegate" claim.

## 4. Verdict levels

| Verdict | Meaning | Follow-up |
| --- | --- | --- |
| Implement | Correct responsibility, real demand, no duplication | Proceed to detailed review |
| Delegate upstream | Belongs to an external component's maintainers | Pursue an upstream fix or dependency upgrade; do not implement locally |
| Reuse | Existing units compose to cover the need | Add no new unit; compose at the call site |
| Decompose | Internal sub-steps warrant separate atomic units | Re-evaluate after decomposition |

Record the verdict in the report. A verdict other than `Implement` must appear
in the conclusion area (Comment or Request Changes) — never bury a "should not
be built" judgment in a detail section.

## Scope note

Assessment covers every changed file in scope. Record verdicts as findings in
the single report.
