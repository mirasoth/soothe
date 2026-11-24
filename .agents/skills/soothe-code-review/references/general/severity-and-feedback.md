# Severity, Feedback, and Boundaries

## 1. Severity levels

| Level | Meaning |
| --- | --- |
| **Critical** | Must fix: functional defect, security flaw, data race or lost concurrency result, or a documented contract contradicted by behavior |
| **Major** | Should fix: performance, quality, maintainability, or a workaround whose removal condition has passed |
| **Minor** | Optional: style, best practice, small clarity gains |
| **Learning** | Educational note; no action required |
| **Praise** | Something done well — report good work as well as problems |

Grade the **impact of the behavior**, not the size of the diff. A one-line error
with a security consequence is Critical; a hundred lines of consistent
boilerplate is not a finding. When a finding's consequence cannot be
demonstrated from the code, downgrade it rather than escalating on suspicion.

## 2. Conclusions

- **Approve** — no blocking findings.
- **Comment** — minor suggestions only; the change can proceed.
- **Request Changes** — at least one Critical finding, or a
  "delegate / reuse / decompose" necessity verdict.

The conclusion must follow the findings. If any Critical finding is open, the
conclusion cannot be Approve.

## 3. Feedback tone

- Be specific and actionable; never vague.
- Address the code, not the author. Educate rather than blame.
- Report strengths as well as problems; praise observed good patterns.
- Prefer questions over commands: "What happens if the input collection is
  empty?" rather than "This fails on empty input".
- Suggest rather than order: "Consider X..." / "Have we considered X?".
- Prefer a concrete fix example over a bare criticism.
- Attach location, description, severity, category tag, and a suggestion to
  every finding; Critical/Major additionally need the evidence triple defined
  in `reporting.md`.

A finding an author cannot act on is not feedback. Before writing one, ensure
it says what to change, where, and why.

## 4. Common traps

- **Perfectionism**: blocking a change over personal style preference.
- **Scope creep**: smuggling unrelated requests into the review.
- **Inconsistent standards**: applying a different bar to different authors or
  different parts of the same change.
- **Rubber-stamping**: approving without actually reading the code.
- **Bikeshedding**: over-arguing trivia while real risks go unexamined.

Handle disagreement by first understanding the author's reasoning, then
acknowledging what is sound, then using data. Escalate to an architect only
when the decision cannot be resolved from the code. Let non-critical points go.

## 5. Boundaries

- **Static analysis only.** This skill does not execute reviewed code or run its
  tests. Security and performance conclusions are pattern-based, not runtime
  observations; state that limitation where it matters.
- When dynamic verification is required, point to the reviewed repository's own
  test entry points and mark the finding "requires dynamic verification". Never
  claim a test was run or that a vulnerability was reproduced.
- **Never modify the reviewed code.** Deliver findings only.
- Missing optional tooling, an undetectable VCS, or an unreadable requirement
  source is an environment note, not a code defect: record it, warn once, and
  continue.
- Do not report findings already covered by the repository's automated
  formatting or lint checks unless they change behavior.
- Do not invent project rules. When the repository has its own rules, they
  override bundled defaults on conflict; cite them by name in the appendix.
