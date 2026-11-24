# Python Pack

## Detection

Applies to changed files whose `language` field in `changes.json` is `python`,
covering source and stub files (`.py`, `.pyi`) as well as executable scripts
without an extension that declare a Python interpreter. Files with an unknown
language receive the general rules only.

## Normative style authority

`google-python-style-guide.md` is the complete official **Google Python Style
Guide**, vendored verbatim (only an attribution header was prepended; upstream
uses CRLF line endings). It is the normative style reference for this pack.

If the reviewed repository declares its own style configuration, that
configuration takes precedence on conflict — see `../README.md` for the
precedence order — and the conflict should be cited in the report appendix.

## General categories instantiated here

`review-checklist.md` instantiates these general tags with Python-specific
patterns:

| General rule file | Tags instantiated | Python pack adds |
| --- | --- | --- |
| `../../general/scanning.md` | `[security]`, `[bug]`, `[performance]`, `[resources]`, `[testing]`, `[conciseness]` | Concrete unsafe APIs, mutable defaults, exception breadth, context managers, membership structures, test determinism |
| `../../general/documentation.md` | `[documentation]` | Google-style docstring sections, default/enum/`Raises` alignment, runnable examples |
| `../../general/concurrency.md` | `[concurrency]` | Executor lifetime, synchronization primitives |
| `../../general/io.md` | `[io]` | Path composition, temporary-file handling, stream versus local-path consumers |
| `../../general/dependencies.md` | `[dependencies]` | Manifest and optional-dependency placement, exact-pin rationale |
| — (pack-owned) | `[style]` | Naming, imports, line length, indentation, comments, type annotations, main guard |

Severity, conclusions, feedback tone, and the report format are **not**
redefined here; they stay in `../../general/severity-and-feedback.md` and
`../../general/reporting.md`.

## Tooling

Do not hard-code tool commands. Use the reviewed repository's own configured
formatter, linter/type checker, and test runner; when it has none, note that in
the report appendix and rely on the static checks in this pack. Automated
formatting and lint findings are not reported unless they change behavior (see
the general scanning rules).

## Targeted reads of the large guide

The vendored guide is roughly 4000 lines. Never load it wholesale into context.
Locate the relevant section, then read only that range:

```bash
GUIDE="{SKILL_HOME}/references/languages/python/google-python-style-guide.md"
rg -n '^### ' "$GUIDE"                     # list all rule sections with line numbers
rg -n '^## ' "$GUIDE"                      # list the four top-level parts
rg -n '### 3.8 Comments and Docstrings' "$GUIDE"
rg -n '### 3.16 Naming|### 2.2 Imports|### 2.12 Default Argument Values' "$GUIDE"
sed -n '2112,2200p' "$GUIDE"               # read one located section
```

Frequently used anchors (line numbers are from the vendored copy and shift only
if the guide is re-vendored — always relocate with `rg` first):

| Topic | Section heading |
| --- | --- |
| Lint | `### 2.1 Lint` |
| Imports | `### 2.2 Imports`, `### 3.13 Imports formatting` |
| Packages | `### 2.3 Packages` |
| Exceptions | `### 2.4 Exceptions` |
| Global state | `### 2.5 Mutable Global State` |
| Comprehensions / generators | `### 2.7 Comprehensions & Generator Expressions`, `### 2.9 Generators` |
| Lambdas / conditionals | `### 2.11 Conditional Expressions`, `### 2.10 Lambda Functions` |
| Default arguments | `### 2.12 Default Argument Values` |
| Truthiness | `### 2.14 True/False Evaluations` |
| Lexical scoping | `### 2.16 Lexical Scoping` |
| Decorators | `### 2.17 Function and Method Decorators` |
| Threading | `### 2.18 Threading` |
| Future imports | `### 2.20 Modern Python: from \_\_future\_\_ imports` |
| Type annotations | `### 2.21 Type Annotated Code`, `### 3.19 Type Annotations` |
| Line length | `### 3.2 Line length` |
| Parentheses / indentation | `### 3.3 Parentheses`, `### 3.4 Indentation` |
| Whitespace / blank lines | `### 3.5 Blank Lines`, `### 3.6 Whitespace` |
| Comments and docstrings | `### 3.8 Comments and Docstrings` |
| Strings | `### 3.10 Strings` |
| Stateful resources | `### 3.11 Files, Sockets, and similar Stateful Resources` |
| TODO comments | `### 3.12 TODO Comments` |
| Statements | `### 3.14 Statements` |
| Getters / setters | `### 3.15 Getters and Setters` |
| Naming | `### 3.16 Naming` |
| Main guard | `### 3.17 Main` |
| Function length | `### 3.18 Function length` |

## Usage

Load this file first, then `review-checklist.md`. For each candidate finding,
relocate the cited guide section with `rg` and read only that section before
grading. Locate the check in `review-checklist.md` by its category tag or by
searching the pattern name.
