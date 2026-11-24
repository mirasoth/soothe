# Review Workflow

Concrete commands, guards, and anti-patterns for the four steps indexed in
`SKILL.md`. The step index in `SKILL.md` is the sole authority for order, gates,
and hand-offs; this file does not restate it.

Language-agnostic by contract: no language syntax, language-specific APIs,
product paths, or product identifiers belong here. The skill's own scripts,
VCS commands, and generic configuration keys are execution mechanics, not
language knowledge. Language-specific instantiations belong in `../languages/`.

## Step 1 — Prepare

Goal: turn "what are we reviewing and why was it changed" into reproducible
facts on disk. Make no review judgments in this step.

### Pin the revision

Resolve the reviewed reference to an immutable commit identifier before reading
any code. A branch name is not a stable review target; the report must cite a
SHA, never "the latest main". Record:

- the immutable head identifier,
- the base identifier (the merge/divergence point when reviewing a branch),
- the exact comparison range used for the diff,
- whether the working copy was dirty,
- the `review-id` used for `{REVIEW_DIR}` (derive it from the head SHA, and
  prefix an MR/PR number when the user supplies one).

Obtain these with the VCS's own revision-resolution commands and record them in
the report's Basic Information section. All artifacts for one review live in the
single `{REVIEW_DIR}`.

### Read the effective configuration

Check for an optional `.codereview` file at the root of the reviewed repository.
Recognized keys: `exclude_paths` (path patterns left out of scope),
`ignore_categories` (category tags skipped), `ignore_rules` (individual checks
skipped), `severity` (a floor for reported findings). When the file is absent,
use the whole working-copy root as scope, skip common build/output directories,
and state in the report that no configuration was found and defaults were
applied.

Also read the reviewed repository's own contribution, architecture, and style
rules when present. These take precedence over this skill's bundled defaults on
conflict; record which project rules were applied. Never assume a particular
rule file name or location beyond the repository root.

### Collect the requirement source

Accept a ticket/issue reference, a Markdown or text document, or a verbal
description. If a ticket system is available, fetch it; otherwise ask the user
to paste the text. For a verbal description, restate it and confirm. When no
requirement source exists, record "not provided" and continue — do not block.

Read the requirement source as plain text. An unreadable requirement source is
an environment note, not a defect.

### Analyze the change set

```bash
python3 {SKILL_HOME}/scripts/analyze_git_diff.py \
  --range <pinned-range> --output {REVIEW_DIR}/changes.json
python3 {SKILL_HOME}/scripts/analyze_git_diff.py \
  --vcs svn --range BASE:HEAD --output {REVIEW_DIR}/changes.json
```

`--range` passes through to the VCS diff, so three-dot branch comparisons work
unchanged. The script auto-detects the VCS; pass `--vcs git|svn` only when
detection fails. Use `--files` for a user-specified file list and `--no-config`
to ignore `.codereview`.

Include configuration, manifest, lock, and CI files in the analysis. Filtering
them out hides dependency and build-pipeline changes that can be more
consequential than source edits. `changes.json` carries a `language` field per
changed file — this is the dispatch key for language packs in step 3.

### Gates and anti-patterns

- Stop and fill gaps if the revision is not pinned or `changes.json` is missing.
- Do not sample a few files and call it a change set. Read every changed file
  listed in `changes.json`.
- Do not re-run the diff in later steps; consume the artifacts from disk.
- Do not install packages, contact external services, or require optional
  tooling to complete this step. Warn and continue instead.
- Produce one report for the whole review; all findings and details belong in it.

## Step 2 — Context

Goal: understand where the changed code sits before judging it.

- Build a module map: what each changed file owns, what calls it, and which
  public surface it affects.
- Trace data flow across process, thread, and network boundaries; note where
  ownership of a resource or the shape of a value changes.
- Note the change's intent from the requirement source, then compare with what
  the diff actually does. Requirement text and implementation often diverge, and
  the implementation is authoritative.
- Assign a language to every changed file using the `language` field from
  `changes.json`. Files with an unknown language still receive general rules.
- Record scope notes in the report's Basic Information section.

Gate: every changed file appears in the module map and has a language. A
change that cannot be placed in the flow is a gap to resolve, not a reason to
guess.

## Step 3 — Review

Goal: produce severity-graded, evidence-backed findings.

1. Load general rule files per the loading strategy in `SKILL.md`, then load the
   language packs selected by the `language` field. Load only what the active
   mode and the changed code require.
2. Scan each changed file against the loaded checks. Read the surrounding
   implementation of anything a changed line calls, so a finding is based on
   actual behavior rather than a signature guess.
3. For each candidate finding, decide whether it is real. A false positive is
   worse than a missed nit: confirm the behavior, check for a guard elsewhere in
   the call chain, and drop the finding if the code already handles it.
4. Attach the exact category tag from `SKILL.md`, a severity, and for
   Critical/Major the evidence triple defined in `reporting.md`.
5. Prefer a concrete fix example over an abstract criticism. Report strong
   patterns as praise as well as problems.

Gate: every Critical/Major finding carries the evidence triple and a category
tag. Do not report problems the repository's own automated formatting or lint
checks already cover (unused declarations, formatting, declaration ordering)
unless they change behavior.

Anti-patterns: sampling a subset of files; reporting "looks risky" without
behavior evidence; duplicating the same problem under several tags; inventing
language-specific rules for a language without a pack; letting a bundled rule
override a project rule.

## Step 4 — Report

Goal: compose, validate, and deliver one report.

1. Compose `{REVIEW_DIR}/code-review.md` per `reporting.md`, with the required
   section headings, severity buckets, graded todo list, and a conclusion.
2. Cross-check the finding counts in the results overview against the findings
   section; they must match.
3. Validate mechanically:

   ```bash
   python3 {SKILL_HOME}/scripts/validate_report.py \
     {REVIEW_DIR}/code-review.md --review-dir {REVIEW_DIR}
   ```

4. On a non-zero exit, fix the report and re-run. On success, present the
   report location, the conclusion, and the top findings. Do not reproduce the
   entire report in conversation.

Gate: validation exits 0 and finding counts match. Never deliver a report that
fails validation.
