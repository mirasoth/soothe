# Document-to-Code Consistency

Documentation aimed at callers is a contract asset. Documentation that
disagrees with code is worse than no documentation at all.

- **A documented promise contradicted by behavior → Critical** (an incorrect
  contract is equivalent to a functional defect).
- Vague or incomplete wording, with no behavioral contradiction → Minor.

Language-agnostic by contract: API documentation conventions belong to the
language pack. This file covers only the consistency obligation.

## 1. Contract consistency — high-frequency drift points

- **Terminology**: one concept must use one term across code, comments, error
  messages, and documentation. Derive the canonical vocabulary from the
  reviewed repository's own rules and existing code; do not invent a glossary
  here. A rename must be applied everywhere in one change.
- **Default values**: documented defaults must equal the actual defaults.
  Changing a signature without updating its documentation is the most common
  drift.
- **Enumerated values**: documented legal values must equal the validated set.
- **Documented combination rules**: when documentation states how two settings
  interact, the implementation must actually compute that combination.
- **Soft versus hard constraints**: distinguish best-effort guidance from a
  guaranteed bound. Never describe a best-effort limit as a guarantee.
- **Scope of applicability**: a setting documented as applying only in a stated
  scope must be inert, or explicitly warned about, outside it.
- **Boundary behavior**: documented handling of empty input, oversized input,
  failure fallback, and timeout must match the code branches.
- **Exception contracts**: documented failure conditions must match the
  conditions that actually raise; new validation needs a documented contract.
- **Return values**: the declared result type and meaning must match what the
  code returns on all paths.

## 2. Runnable examples

Examples must be genuinely executable: referenced symbols resolve, names and
arguments match the current signature, and setup steps reflect the current API.
A renamed parameter with an unupdated example is a common defect. Verify each
example against the changed code rather than assuming it still works.

## 3. Multi-location synchronization

When behavior changes, check every place that documents it: inline API
documentation, standalone guides and reference pages, README files, and command
help text. List the locations checked in the report's appendix.

## 4. Comment-to-code consistency

- Version-gated comments must match the version comparison in the code.
- Linked issue or ticket references must describe the problem the code actually
  addresses.
- TODO markers must still be relevant, not already resolved elsewhere.
- Code declared as a temporary workaround must state its removal condition;
  when that condition is already satisfied (for example, an upstream fix has
  landed), recommend deleting it.
