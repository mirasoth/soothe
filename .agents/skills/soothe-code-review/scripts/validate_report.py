#!/usr/bin/env python3
"""Validate the single code-review report mechanically.

Three checks turn "was the report written correctly" from a human judgement
into a deterministic one:

1. **Section skeleton** — every required heading is present at the right level.
   The authoritative skeleton is encoded below as `REQUIRED_SECTIONS` and
   documented in `references/general/reporting.md` and `SKILL.md`.
2. **No unrendered placeholders** — no `{{var}}`, `{{#if}}`, or similar remains.
   Single braces (`{CODE_HOME}`) are path bases and are not placeholders.
3. **Sibling references exist** — every referenced review artifact (by bare
   filename, or as a `.review/<id>/<file>` path) must be a real file.

Standard library only.

Usage:
    validate_report.py <report.md> [--review-dir <dir>] [--json]

Exit codes:
    0  passed
    1  validation failed (missing heading / leftover placeholder / missing artifact)
    2  usage or input error
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

# Authoritative report skeleton. Keep in sync with the "Required section
# skeleton" section of references/general/reporting.md and the Report section
# of SKILL.md. Heading level is part of the contract.
REQUIRED_SECTIONS = (
    "# Code Review Report",
    "## 1. Basic Information",
    "## 2. Results Overview",
    "## 3. Findings",
    "### 3.1 Critical",
    "### 3.2 Major",
    "### 3.3 Minor",
    "### 3.4 Learning",
    "### 3.5 Praise",
    "## 4. Conclusion and Todo",
    "## 5. Appendix: Review Metadata",
)

# Artifact names that a report may reference by bare filename. Other .md/.json
# tokens (for example rule file names) are skill resources, not review
# artifacts, and are deliberately not checked here.
SIBLING_ARTIFACTS = (
    "changes.json",
    "code-review.md",
)

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*$")
PLACEHOLDER_RE = re.compile(r"\{\{.*?\}\}", re.DOTALL)
REVIEW_PATH_RE = re.compile(r"\.review/[\w.\-]+/([\w.\-]+\.(?:md|json))")
SIBLING_RE = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in SIBLING_ARTIFACTS) + r")\b"
)


def extract_headings(text: str) -> list[tuple[int, str]]:
    """Extract (level, text) for every Markdown heading in the text."""
    headings: list[tuple[int, str]] = []
    for line in text.splitlines():
        match = HEADING_RE.match(line)
        if match:
            headings.append((len(match.group(1)), match.group(2).strip()))
    return headings


def heading_pattern(heading: str) -> re.Pattern:
    """Build a matcher treating `{{...}}` tokens inside a heading as wildcards."""
    parts = re.split(r"\{\{.*?\}\}", heading)
    return re.compile(r"^" + ".*?".join(re.escape(part) for part in parts) + r"$")


def check_section_skeleton(report_text: str) -> list[str]:
    """Return an error for each required heading missing from the report."""
    actual = extract_headings(report_text)
    missing: list[str] = []
    for required in REQUIRED_SECTIONS:
        match = HEADING_RE.match(required)
        if match is None:  # pragma: no cover - guards the constant itself
            continue
        level, text = len(match.group(1)), match.group(2).strip()
        pattern = heading_pattern(text)
        if not any(candidate_level == level and pattern.match(candidate_text)
                   for candidate_level, candidate_text in actual):
            missing.append(required)
    if missing:
        return ["Missing sections: " + "; ".join(missing)]
    return []


def check_placeholders(report_text: str) -> list[str]:
    """Return an error when unrendered `{{...}}` placeholders remain."""
    leftover = PLACEHOLDER_RE.findall(report_text)
    if not leftover:
        return []
    unique = sorted(set(leftover))
    return [f"Unrendered placeholders ({len(leftover)} occurrences): "
            + ", ".join(unique[:10])]


def check_siblings(report_text: str, review_dir: Path) -> list[str]:
    """Verify that referenced review artifacts exist as real files."""
    errors: list[str] = []
    for name in sorted(set(REVIEW_PATH_RE.findall(report_text))):
        candidate = review_dir / name
        if not candidate.is_file():
            errors.append(f"Referenced artifact does not exist: {name} "
                          f"(expected {candidate})")
    for name in sorted(set(SIBLING_RE.findall(report_text))):
        candidate = review_dir / name
        if not candidate.is_file():
            errors.append(f"Referenced sibling artifact does not exist: {name} "
                          f"(expected {candidate})")
    return errors


def check_report(report: Path, review_dir: Path) -> list[str]:
    """Run all three checks and return the list of errors (empty means passed)."""
    report_text = report.read_text(encoding="utf-8")
    return (check_section_skeleton(report_text)
            + check_placeholders(report_text)
            + check_siblings(report_text, review_dir))


def main() -> int:
    """Parse arguments, validate the report, and emit the result."""
    parser = argparse.ArgumentParser(
        description="Validate the code-review report's skeleton, placeholders, "
                    "and sibling references.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("report", help="Path to the report to validate")
    parser.add_argument("--review-dir", default=None,
                        help="Artifact directory (default: the report's directory)")
    parser.add_argument("--json", action="store_true",
                        help="Emit the result as JSON")
    args = parser.parse_args()

    report = Path(args.report)
    if not report.is_file():
        print(f"Error: report file does not exist: {report}", file=sys.stderr)
        return 2

    review_dir = Path(args.review_dir) if args.review_dir else report.parent
    if not review_dir.is_dir():
        print(f"Error: artifact directory does not exist: {review_dir}",
              file=sys.stderr)
        return 2

    try:
        errors = check_report(report, review_dir)
    except OSError as exc:
        print(f"Error: cannot read report: {exc}", file=sys.stderr)
        return 2

    payload = {
        "report": str(report),
        "review_dir": str(review_dir),
        "required_sections": len(REQUIRED_SECTIONS),
        "passed": not errors,
        "errors": errors,
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not errors else 1

    if errors:
        print(f"FAILED: {report}")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(f"PASSED: {report}")
    print(f"  Sections: {len(REQUIRED_SECTIONS)} required headings present")
    print(f"  Artifact directory: {review_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
