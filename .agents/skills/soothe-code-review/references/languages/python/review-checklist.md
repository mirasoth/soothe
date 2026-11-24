# Python Review Checklist

Python-specific instantiations of `../../general/`. Each section names its
general rule file and category tag; **only** rows that add concrete Python
syntax, APIs, or conventions are listed here. General principles are not
restated — load the named general rule for the principle and this file for the
concrete pattern.

Style authority: `google-python-style-guide.md` (official guide, verbatim). A
`—` in the Guide column means the check comes from a general rule, not from the
style guide. When the reviewed repository's own style configuration conflicts,
it wins — see `../README.md`. Locate a cited section with the `rg` patterns in
`README.md` before reading it.

## 1. Security `[security]`

General rule: `../../general/scanning.md` §1 and `../../general/io.md` §1–2.

| Python-specific check | Guide section |
| --- | --- |
| Never pass untrusted input to `eval`, `exec`, `compile`, or `__import__`; prefer an explicit dispatch table | `2.19 Power Features` |
| Use an argument-list `subprocess` call; avoid `shell=True` with interpolated data, and quote with `shlex.quote` when a shell is unavoidable | — |
| Never load untrusted data with `pickle`, `marshal`, or an unsafe YAML loader; use `yaml.safe_load` or an explicit schema | `2.19 Power Features` |
| Normalize and validate untrusted paths with `os.path.normpath` against an allow-listed root; a string-prefix check alone is insufficient | — |
| Create external files with `tempfile` (randomized names), never a fixed name in a shared directory | `3.11 Files, Sockets, and similar Stateful Resources` |
| Disable external entity resolution when parsing untrusted XML | — |

Grade a reachable injection, traversal, or unsafe-deserialization path as
Critical per `../../general/severity-and-feedback.md`.

## 2. Potential bugs `[bug]`

General rule: `../../general/scanning.md` §2.

| Python-specific check | Guide section |
| --- | --- |
| No mutable defaults (`[]`, `{}`, `set()`); use `None` and construct inside the body | `2.12 Default Argument Values` |
| A closure or lambda defined in a loop binds variables late; bind them explicitly as default arguments or use a factory | `2.16 Lexical Scoping`, `2.10 Lambda Functions` |
| No bare `except:` or `except Exception: pass`; catch the narrowest type and handle, log, or re-raise | `2.4 Exceptions` |
| Use `if x is None` rather than implicit falsiness when `0`, `""`, and empty collections are valid values | `2.14 True/False Evaluations` |
| Await every coroutine; use the asynchronous IO variant in async code; release locks on cancellation | — |
| Do not rely on the atomicity of built-in types for cross-thread state | `2.18 Threading` |
| Module-level mutable values and caches need an explicit invalidation policy | `2.5 Mutable Global State` |
| An `except` block that neither logs, re-raises, nor establishes a fallback is silent degradation | `2.4 Exceptions` |

Boundary cases (zero, negative, oversized, empty collections, off-by-one) come
from the general rule and are not repeated here.

## 3. Performance `[performance]`

General rule: `../../general/scanning.md` §3.

| Python-specific check | Guide section |
| --- | --- |
| Build strings in loops with `join`, f-strings, or a builder rather than repeated concatenation | `3.10 Strings` |
| Use a `set` or `dict` for repeated membership checks, not a `list` | — |
| Prefer generators and comprehensions for large sequences; avoid materializing an intermediate list | `2.7 Comprehensions & Generator Expressions`, `2.9 Generators` |
| Independently configured concurrency levels compound; verify total resource demand | — |

N+1 patterns, blocking I/O on hot paths, and unbounded growth come from the
general rule.

## 4. Resource management `[resources]`

General rule: `../../general/scanning.md` §4.

| Python-specific check | Guide section |
| --- | --- |
| Manage files, sockets, locks, and similar stateful objects with `with` (or an explicit close on every path) | `3.11 Files, Sockets, and similar Stateful Resources` |
| Use `ThreadPoolExecutor` / `ProcessPoolExecutor` as context managers, or shut them down with `wait=True` | `3.11 Files, Sockets, and similar Stateful Resources` |
| Clean up temporary artifacts in `finally` or a context manager | `3.11 Files, Sockets, and similar Stateful Resources` |

## 5. Testing `[testing]`

General rule: `../../general/scanning.md` §5.

| Python-specific check | Guide section |
| --- | --- |
| Isolate external dependencies with `unittest.mock` or the repository's chosen mocking facility; parameterize cases instead of hiding them | — |
| Assert against values derived from the implementation under test, not hard-coded magic constants | — |

Coverage of happy/boundary/error paths, determinism, and regression value come
from the general rule.

## 6. Documentation `[documentation]`

General rule: `../../general/documentation.md`.

| Python-specific check | Guide section |
| --- | --- |
| Public modules, classes, and functions have docstrings with `Args:`, `Returns:`, and `Raises:` sections where applicable | `3.8 Comments and Docstrings` |
| Documented defaults, legal values, and `Raises:` entries match the signature and validation branches | `3.8 Comments and Docstrings` |
| Documented result type matches every return path, including annotated `Optional` / `None` cases | `3.19 Type Annotations` |
| Examples use current import paths, names, and arguments | `3.8 Comments and Docstrings` |
| TODO comments follow the repository's owner/context form and are not stale | `3.12 TODO Comments` |
| Type stubs and public export lists are regenerated, not hand-edited | `2.21 Type Annotated Code` |

A documented promise contradicted by behavior is Critical per
`../../general/documentation.md`.

## 7. Concurrency `[concurrency]`

General rule: `../../general/concurrency.md`.

| Python-specific check | Guide section |
| --- | --- |
| Communicate between threads with `queue.Queue` or `threading` locking primitives rather than relying on built-in atomicity | `2.18 Threading` |
| Prefer condition variables over time-based polling and sleeps | `2.18 Threading` |
| Every submitted executor task has an owner that observes its result and exception | — |

Execution-context affinity, lock ordering, pool shutdown, and lost-result
aggregation come from the general rule.

## 8. IO `[io]`

General rule: `../../general/io.md`.

| Python-specific check | Guide section |
| --- | --- |
| Pass a file-like object to consumers that accept one; materialize to a real local file only for subprocesses or native extensions | `3.11 Files, Sockets, and similar Stateful Resources` |
| Use the repository's traversal-safe path helper instead of raw `os.path.join` or f-string concatenation of a caller-supplied component | — |
| Propagate or log write failures; clean up partial outputs | `2.4 Exceptions` |

Credential handling and temporary-file lifecycle come from the general rule.

## 9. Dependencies `[dependencies]`

General rule: `../../general/dependencies.md`.

| Python-specific check | Guide section |
| --- | --- |
| A new `import` has a matching dependency declaration; a removed declaration has no surviving imports | `2.2 Imports`, `2.3 Packages` |
| Large or license-restricted packages sit in an optional dependency group | `2.2 Imports` |
| An exact `==` pin carries a documented reason and an unpin condition | — |
| Copied third-party code carries its source and license identifier in a comment | `3.8 Comments and Docstrings` |

Constraint conflicts and severity grading come from the general rule.

## 10. Style `[style]`

General rule for severity and reporting: `../../general/severity-and-feedback.md`
and `../../general/reporting.md`. All items below are Minor unless the
repository's own rules say otherwise, and are not reported when its configured
formatter covers them.

| Check | Detail | Guide section |
| --- | --- | --- |
| Line length | Maximum 80 characters, with only the upstream exceptions; no backslash continuation | `3.2 Line length` |
| Indentation | 4 spaces; wrapped lines use implicit joining inside parentheses | `3.4 Indentation`, `3.3 Parentheses` |
| Import grouping | Standard library → third-party → first-party, each sorted; absolute imports preferred; no wildcard imports | `2.2 Imports`, `3.13 Imports formatting` |
| Naming | `module_name`, `ClassName`, `function_name`, `GLOBAL_CONSTANT_NAME`; descriptive, no unfamiliar abbreviations, `.py` extension, no dashes | `3.16 Naming` |
| Whitespace | No trailing whitespace; no whitespace around keyword arguments or inside brackets | `3.6 Whitespace`, `3.5 Blank Lines` |
| Statements | One statement per line; no semicolon-joined statements | `3.1 Semicolons`, `3.14 Statements` |
| Strings | Prefer f-strings or `format` for interpolation; consistent quoting per the configured formatter | `3.10 Strings` |
| Properties | Accessors only when they add behavior; no getters/setters that merely forward a field | `2.13 Properties`, `3.15 Getters and Setters` |
| Type hints | Public signatures annotated; annotations accurate and do not mask `None` | `2.21 Type Annotated Code`, `3.19 Type Annotations` |
| Main guard | Executable modules use `if __name__ == '__main__':` | `3.17 Main` |
| Function length | Long functions are a readability signal; report only with a concrete decomposition | `3.18 Function length` |
| Comprehensions | No nested multi-clause comprehensions replacing a clearer loop | `2.7 Comprehensions & Generator Expressions` |
| Conditional expressions | Keep them short; no nested ternaries | `2.11 Conditional Expressions` |
| Power features | Metaclasses, bytecode access, and dynamic compilation need justification | `2.19 Power Features` |
| Future imports | Used only where required, with the compatibility reason documented | `2.20 Modern Python: from \_\_future\_\_ imports` |
| Decorators | Use only where they clarify behavior; avoid stacking opaque decorators | `2.17 Function and Method Decorators` |
| Default iterators | Do not override iteration protocols in a way that breaks `for` / `in` semantics | `2.8 Default Iterators and Operators` |

## Finding format

Attach the general tag from the section heading, the severity per
`../../general/severity-and-feedback.md`, and the cited guide section. Critical
and Major findings additionally require the evidence triple defined in
`../../general/reporting.md`.
