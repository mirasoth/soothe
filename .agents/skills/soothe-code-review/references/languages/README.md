# Language Pack Contract

This directory is the **language-specific layer**. Everything here is isolated
from `../general/`: a language pack instantiates general rules with concrete
syntax, APIs, conventions, and style authority, and never changes a general
rule.

The general layer is language-agnostic by contract. It contains no language
syntax, language-specific API names, tool names, product paths, or product
identifiers. Conversely, no language pack restates general principles — it
references them by category tag and adds only language-specific detail.

## Dispatch

1. `scripts/analyze_git_diff.py` writes a `language` field for every changed
   file in `{REVIEW_DIR}/changes.json`.
2. Group changed files by that field. Normalize it to lowercase; treat unknown
   or missing values as `unknown`.
3. For each group whose name matches a directory here, load that pack's
   `README.md` first, then the rule files it names.
4. For a group with no matching directory, apply the general rules only and
   record in the report that no pack exists for that language. Do not invent
   language-specific rules.
5. Apply the general rules to every file regardless of packaging. A language
   pack may add detail or raise specificity, but never weaken, override, or
   contradict a general rule.

Pack directory names use the lowercase language identifiers emitted by the diff
script (for example `python`). A pack is discovered by directory name; there is
no central registry file to edit. `SKILL.md` holds only a one-line pointer to
this contract.

## Precedence

From highest to lowest:

1. The reviewed repository's own rules and configuration (they override
   everything below on conflict; cite them in the report appendix).
2. The pack's normative style authority (for example the vendored style guide).
3. The pack's review checklist.
4. The general rules, which always apply and set the severity floor.

A repository rule that permits a pattern a pack flags is a legitimate override;
record it rather than reporting a false positive. Never silently apply a
lower-precedence rule over a higher-precedence one.

## Required files in a pack

| File | Purpose |
| --- | --- |
| `README.md` | Pack index: file extensions and detection, the general categories instantiated here, the normative style authority and where it lives, default tooling described generically, and targeted-read patterns for any large reference |
| `review-checklist.md` | Concrete language-specific checks, each mapped to a general category tag and, where applicable, a section of the style authority |

Optional files: a verbatim vendored style guide, additional topic-specific
checklists, or examples. Large references must ship with grep patterns so a
reviewer loads only the relevant sections (progressive disclosure).

## Add a new language

Adding a language must require **no edits** to `../general/` and no edits to
the scripts.

1. Confirm the language identifier emitted by `analyze_git_diff.py` for the new
   file extensions. Add an extension mapping in that script only if the
   extensions are not recognized yet.
2. Create `references/languages/{identifier}/README.md` following the required
   file table above.
3. Create `references/languages/{identifier}/review-checklist.md`. For each
   check, state the concrete pattern, the general category tag it instantiates,
   and the rule file/section it comes from. Do not restate general principles.
4. Vendor a normative style guide only if the project needs offline fidelity;
   add an attribution header (source, license, retrieval date, verbatim notice)
   and document grep patterns for targeted reads.
5. Smoke-test: run the diff script on a change set containing the new
   extensions, confirm the `language` field matches the new directory name, and
   verify a finding from the new checklist is reachable.
6. Update only the `SKILL.md` file-map comment if a new top-level pack should
   be listed for discoverability. No general rule, mode table, workflow step,
   or severity definition changes.

## What a pack must never contain

- Product paths, product identifiers, internal service names, or repository
  module maps.
- Credentials, environment-variable secret names, or network endpoints.
- Duplicated general rules, severity definitions, workflow steps, or report
  format specifications.
- Rules that silently contradict `../general/`.
