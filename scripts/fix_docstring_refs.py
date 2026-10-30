#!/usr/bin/env python3
"""Drop IG references and RFC section numbers from docstrings and description fields.

Per operator policy: docstrings may cite RFC identifiers (provenance), but must
not cite IG docs or RFC section numbers — those rot as documents are revised.
Comments (# ...) are never touched.

Matches both numbering schemes: `RFC-634`/`IG-774` and the namespaced
`RFC-LOOP-030`/`IG-LOOP-014`/`RFC-CORE-008`.
"""

from __future__ import annotations

import os
import re
import sys

# One design-doc identifier: RFC-123, IG-456, RFC-LOOP-030, IG-LOOP-014, ...
_IG = r"[Ii][Gg]-(?:[A-Z][A-Za-z]+-)?\d+(?:[ \t]+P\d+)?"
_SECTION = r"§[\w/-]+(?:\.\d+)*"
_KEEP_POINTER = re.compile(r"(AGENTS\.md|README\.md)[ \t]*§\d+")


def remove_rfc_ig_refs(text: str) -> str:
    """Remove IG references and section numbers from a text string.

    RFC identifiers are kept. Only modifies text that contains an IG
    reference or a section marker; leading indentation is preserved.
    """
    if "IG-" not in text and "§" not in text:
        return text

    result = text

    # `AGENTS.md §10` style pointers stay (in-repo doc, not an RFC/IG).
    result = _KEEP_POINTER.sub(lambda m: m.group(0).replace("§", "\x01"), result)

    # Parenthetical whose only content is refs: "(IG-751)", "(§32, P10)", ...
    def _paren(match: re.Match[str]) -> str:
        inner = match.group(1)
        if "IG-" not in inner and "§" not in inner:
            return match.group(0)
        rest = re.sub(_SECTION, "", inner)
        rest = re.sub(_IG, "", rest)
        rest = re.sub(r"(?i)\bP\d+\b", "", rest)
        rest = re.sub(r"[\s,;:—–-]", "", rest)
        return "\x00" if rest == "" else match.group(0)

    result = re.sub(r"[ \t]*\(([^()]*)\)", _paren, result, flags=re.S)

    # `RFC-450 §6.3` -> `RFC-450`
    result = re.sub(rf"(RFC-[A-Za-z0-9_-]+)[ \t]*{_SECTION}", r"\1", result)

    # Remaining IG references, with the phrasing they were embedded in.
    result = re.sub(rf"Regression \({_IG}\):", "Regression:", result)
    result = re.sub(rf"Before {_IG},[ \t]*", "Previously, ", result)
    result = re.sub(rf"After {_IG},[ \t]*(\w)", lambda m: m.group(1).upper(), result)
    result = re.sub(rf"[ \t]*/[ \t]*{_IG}", "", result)
    result = re.sub(_IG, "\x00", result)

    # Section numbers not attached to an RFC id.
    result = re.sub(rf"(?m)^([ \t]*){_SECTION}\.[ \t]+", lambda m: m.group(1) + "\x00", result)
    result = re.sub(rf"[ \t]*{_SECTION}[ \t]*:[ \t]*", "\x00", result)
    result = re.sub(rf"[ \t]*{_SECTION}", "\x00", result)

    # Tidy only the lines that lost something.
    out = []
    for line in result.split("\n"):
        if "\x00" not in line:
            out.append(line)
            continue
        stripped = line.lstrip(" ")
        indent = line[: len(line) - len(stripped)]
        s = stripped.replace("\x00", "")
        s = re.sub(r"[ \t]+\.(?!\.)", ".", s)
        s = re.sub(r"[ \t]+,(?!,)", ",", s)
        s = re.sub(r"[ \t]{2,}", " ", s)
        s = re.sub(r'^""":[ \t]*', '"""', s)
        s = re.sub(r"^:[ \t]+", "", s)
        s = s.rstrip()
        if s == "" and stripped.strip() == "":
            continue
        out.append(indent + s)

    result = "\n".join(out).replace("\x01", "§")
    return result


def fix_file(filepath: str, dry_run: bool = False) -> tuple[bool, int]:
    """Fix IG/§-references in docstrings and description fields.

    Returns (changed, fix_count).

    Strategy: use a state machine to track whether we're inside a
    triple-quoted string (docstring). Only modify lines that are:
    1. Inside a triple-quoted docstring AND contain IG-/§-
    2. description="..." fields with IG-/§-
    3. Lines that are part of multi-line string literals with IG-/§-

    Comments (# ...) are always preserved.
    """
    with open(filepath, encoding="utf-8") as f:
        content = f.read()

    original = content
    lines = content.split("\n")
    new_lines = []
    fix_count = 0

    in_triple_docstring = False
    docstring_char = None

    for i, line in enumerate(lines):
        stripped = line.lstrip()

        # Pure comment line - keep as is (unless inside docstring)
        if stripped.startswith("#") and not in_triple_docstring:
            new_lines.append(line)
            continue

        # If inside a triple-quoted docstring, fix the line
        if in_triple_docstring:
            # Check if this line ends the docstring
            if docstring_char in line:
                # The docstring closes on this line
                idx = line.find(docstring_char)
                before = line[:idx]
                after = line[idx + len(docstring_char) :]

                # Fix the content part (before the closing quotes) only if it has IG-/§-
                if "IG-" in before or "§" in before:
                    fixed_before = remove_rfc_ig_refs(before)
                    if fixed_before != before:
                        fix_count += 1
                else:
                    fixed_before = before

                # After closing quotes could be code or comment - leave as is
                new_line = fixed_before + docstring_char + after

                in_triple_docstring = False
                docstring_char = None
                new_lines.append(new_line)
                continue
            else:
                # Inside docstring, fix the line only if it has RFC-/IG-
                if "IG-" in line or "§" in line:
                    fixed_line = remove_rfc_ig_refs(line)
                    if fixed_line != line:
                        fix_count += 1
                    new_lines.append(fixed_line)
                else:
                    new_lines.append(line)
                continue

        # Not in docstring - check for description= or docstring start

        # Check for description="..." or description='...' (single line)
        desc_match = re.search(r'(description\s*=\s*)(["\'])(.*?)(\2)', line)
        if desc_match and ("IG-" in desc_match.group(3) or "§" in desc_match.group(3)):
            prefix = desc_match.group(1)
            quote = desc_match.group(2)
            content_str = desc_match.group(3)
            suffix = desc_match.group(4)
            fixed_content = remove_rfc_ig_refs(content_str)
            line = (
                line[: desc_match.start()]
                + prefix
                + quote
                + fixed_content
                + suffix
                + line[desc_match.end() :]
            )
            fix_count += 1

        # Check for triple-quoted strings on this line
        for tq in ['"""', "'''"]:
            if tq in line:
                count_tq = line.count(tq)
                if count_tq >= 2 and count_tq % 2 == 0:
                    # Single-line docstring/string: """content"""
                    # Fix content between first and last occurrence
                    first_idx = line.find(tq)
                    last_idx = line.rfind(tq)
                    before = line[: first_idx + len(tq)]
                    content = line[first_idx + len(tq) : last_idx]
                    after = line[last_idx:]

                    if "IG-" in content or "§" in content:
                        fixed_content = remove_rfc_ig_refs(content)
                        if fixed_content != content:
                            fix_count += 1
                    else:
                        fixed_content = content

                    line = before + fixed_content + after
                    # Don't enter docstring mode

                elif count_tq % 2 == 1:
                    # We enter a docstring (odd count means unclosed)
                    last_tq_idx = line.rfind(tq)
                    before_open = line[:last_tq_idx]
                    after_open = line[last_tq_idx + len(tq) :]

                    # Fix the content after opening quote only if it has IG-/§-
                    if "IG-" in after_open or "§" in after_open:
                        fixed_after = remove_rfc_ig_refs(after_open)
                        if fixed_after != after_open:
                            fix_count += 1
                    else:
                        fixed_after = after_open

                    line = before_open + tq + fixed_after
                    in_triple_docstring = True
                    docstring_char = tq
                    break  # Don't check other tq type

        new_lines.append(line)

    new_content = "\n".join(new_lines)

    if new_content != original:
        if not dry_run:
            with open(filepath, "w", encoding="utf-8") as f:
                f.write(new_content)
        return True, fix_count
    return False, 0


def main():
    dry_run = "--dry-run" in sys.argv
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    packages = [
        os.path.join(repo_root, "packages/soothe/src"),
        os.path.join(repo_root, "packages/soothe-daemon/src"),
        os.path.join(repo_root, "packages/soothe-cli/src"),
    ]

    total_files = 0
    total_fixes = 0
    changed_files = []

    for pkg in packages:
        for root, dirs, files in os.walk(pkg):
            for f in sorted(files):
                if f.endswith(".py"):
                    filepath = os.path.join(root, f)
                    changed, fixes = fix_file(filepath, dry_run=dry_run)
                    if changed:
                        total_files += 1
                        total_fixes += fixes
                        changed_files.append(os.path.relpath(filepath, repo_root))

    mode = "DRY RUN" if dry_run else "APPLIED"
    print(f"\n{mode}: {total_files} files changed, {total_fixes} lines fixed")
    for f in changed_files:
        print(f"  {f}")


if __name__ == "__main__":
    main()
