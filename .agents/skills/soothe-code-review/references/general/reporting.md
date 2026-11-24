# Single Report Format

Produce exactly one deliverable: `{REVIEW_DIR}/code-review.md`. The format is
defined here in prose and enforced mechanically by
`{SKILL_HOME}/scripts/validate_report.py`.

## 1. Required section skeleton

The heading list below is the authoritative skeleton. It is encoded verbatim as
`REQUIRED_SECTIONS` in `scripts/validate_report.py`; this section and that
constant are the two places that must stay in sync. Heading text, level, and
numbering are fixed, so render every `{{var}}` placeholder in the report body
before validation.

```
# Code Review Report
## 1. Basic Information
## 2. Results Overview
## 3. Findings
### 3.1 Critical
### 3.2 Major
### 3.3 Minor
### 3.4 Learning
### 3.5 Praise
## 4. Conclusion and Todo
## 5. Appendix: Review Metadata
```

## 2. Section contents

### 1. Basic Information

- Mode (`full` or a single category) and the requested scope.
- Pinned head SHA, base SHA, exact comparison range, VCS, and `review-id`.
- Requirement source and whether one was provided.
- Effective configuration: `.codereview` keys applied, or "not found, defaults
  applied"; project rules read and applied.
- Language packs applied, and any changed-file language with no pack.

### 2. Results Overview

One row per category tag, with a status and finding count:

| Tag | Status | Findings |
| --- | --- | --- |
| `[security]` | reviewed / not reviewed / no pack | count |

Status must be `not reviewed` for categories outside the active mode. Counts
here must equal the number of findings actually written in section 3. Always
include a total row. Keep the overview to counts and status only — no finding
descriptions.

### 3. Findings

One subsection per severity bucket. Each bucket without findings contains the
single line `None.`. Every finding is written as:

```
- **`[tag]` `path/to/file:line`** — short description.
  - What/why: the concrete behavior and its consequence.
  - Evidence: the evidence triple (below).
  - Suggestion: a concrete, actionable fix, preferably with a short code example.
```

Rules:

- Use the exact category tags indexed in `SKILL.md`.
- Grade with the level definitions in `severity-and-feedback.md`; a finding's
  bucket must match its stated severity.
- Prefer question-first suggestions where it helps, but always state the fix.
- **Evidence triple** (mandatory for Critical and Major): `file:line` +
  the observed code form + the specific rule or contract it deviates from.
  "Looks risky" is not evidence. Reference the rule file and the section it
  came from when useful.
- Findings are the substance of the report. Do not paste process narration,
  command logs, or the contents of whole files into them.

### 4. Conclusion and Todo

- A conclusion of `Approve`, `Comment`, or `Request Changes`, with one sentence
  of rationale tied to the findings.
- A graded todo list, ordered Critical → Major → Minor. Each item carries its
  category tag and `file:line`, and points at the finding it resolves:

  ```
  - [ ] `[security]` `path/to/file:line` — action to take (Critical).
  ```

- If the verdict is to delegate externally, reuse existing code, or decompose
  into atomic units, state that here explicitly (see `necessity.md`); never
  leave such a verdict only in a detail section.

### 5. Appendix: Review Metadata

- Rule files loaded (general + language packs) and their paths relative to
  `{SKILL_HOME}`.
- Project rules consulted and which bundled defaults they overrode.
- Artifacts in `{REVIEW_DIR}` referenced by filename only, for example
  `changes.json`.
- Environment notes: unavailable optional tooling, undetectable VCS, missing
  requirement source, changed-file languages without a pack.
- Any finding marked "requires dynamic verification", plus the repository test
  entry points that would verify it.

## 3. Conventions

- **Placeholders**: `{{var}}` denotes a value to render. `{CODE_HOME}`,
  `{SKILL_HOME}`, and `{REVIEW_DIR}` are path bases, not placeholders. No
  `{{var}}` may remain in a delivered report.
- **Sibling references**: reference artifacts in the same directory by
  filename only (`changes.json`), never by an absolute path. Validation checks
  that each referenced artifact file exists. Recognized sibling artifact names
  are `changes.json` and `code-review.md`; other `.md`/`.json` tokens (for
  example rule file names) are skill resources and are not checked.
- **No detail duplication**: state each finding once, in section 3. The
  overview, conclusion, and todo list reference findings; they do not repeat
  their descriptions.
- **Language-neutral prose**: the report describes behavior and consequences,
  not product-specific identifiers that are not part of the reviewed code.

## 4. Validation

```bash
python3 {SKILL_HOME}/scripts/validate_report.py \
  {REVIEW_DIR}/code-review.md --review-dir {REVIEW_DIR}
```

Validation checks section-skeleton completeness, leftover placeholders, and
existence of referenced sibling artifacts. Exit `0` means deliverable; exit `1`
means fix the report and re-run; exit `2` means a usage or input error. Never
deliver a report that fails validation.
