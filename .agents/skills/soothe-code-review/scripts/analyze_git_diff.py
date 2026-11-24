#!/usr/bin/env python3
"""Analyze a VCS change set (Git or SVN) and emit a machine-readable inventory.

Produces one JSON document describing every changed text file, including its
detected language and its added/deleted line counts. Configuration, manifest,
lock, and CI files are included deliberately: filtering them out would hide
dependency and build-pipeline changes.

Standard library only. All subprocess calls use argument lists (never a shell).

Usage:
    analyze_git_diff.py --range main...HEAD --output changes.json
    analyze_git_diff.py --vcs svn --range BASE:HEAD --output changes.json
    analyze_git_diff.py --range HEAD~3..HEAD --files a.py b.go --format markdown

Exit codes: 0 success (including "no changes"), 1 runtime or VCS failure.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Language(Enum):
    """Language identifier emitted per changed file; used for pack dispatch."""

    GO = "go"
    PYTHON = "python"
    JAVA = "java"
    CPP = "cpp"
    C = "c"
    JAVASCRIPT = "javascript"
    TYPESCRIPT = "typescript"
    CSHARP = "csharp"
    SQL = "sql"
    RUST = "rust"
    RUBY = "ruby"
    PHP = "php"
    KOTLIN = "kotlin"
    SWIFT = "swift"
    SHELL = "shell"
    UNKNOWN = "unknown"


@dataclass
class CodeChange:
    """Per-file change inventory."""

    file_path: str
    language: Language
    added_lines: int = 0
    deleted_lines: int = 0
    # Non-blank counts are inventory metadata, excluding blank lines.
    added_non_blank: int = 0
    deleted_non_blank: int = 0
    hunks: list[dict] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        """Total added plus deleted lines."""
        return self.added_lines + self.deleted_lines


# File extension -> language. Extend this map when adding a language pack whose
# extensions are not yet recognized; the rest of this script is language-neutral.
EXTENSION_LANGUAGE_MAP = {
    ".go": Language.GO,
    ".py": Language.PYTHON,
    ".pyi": Language.PYTHON,
    ".java": Language.JAVA,
    ".kt": Language.KOTLIN,
    ".kts": Language.KOTLIN,
    ".cpp": Language.CPP,
    ".cc": Language.CPP,
    ".cxx": Language.CPP,
    ".hpp": Language.CPP,
    ".hh": Language.CPP,
    ".c": Language.C,
    ".h": Language.C,
    ".js": Language.JAVASCRIPT,
    ".jsx": Language.JAVASCRIPT,
    ".ts": Language.TYPESCRIPT,
    ".tsx": Language.TYPESCRIPT,
    ".cs": Language.CSHARP,
    ".sql": Language.SQL,
    ".rs": Language.RUST,
    ".rb": Language.RUBY,
    ".php": Language.PHP,
    ".swift": Language.SWIFT,
    ".sh": Language.SHELL,
    ".bash": Language.SHELL,
    ".zsh": Language.SHELL,
}

CODE_EXTENSIONS = set(EXTENSION_LANGUAGE_MAP)

# Not code, but critical to review: dependency declarations, CI definitions,
# build configuration, and lock files.
CONFIG_EXTENSIONS = {
    ".toml", ".cfg", ".ini", ".lock", ".yml", ".yaml", ".json", ".pip",
    ".properties", ".gradle", ".xml", ".env", ".rst", ".md",
}

ANALYZED_EXTENSIONS = CODE_EXTENSIONS | CONFIG_EXTENSIONS

# Extensionless files that matter to a review.
KNOWN_FILENAMES = {
    "Makefile", "Dockerfile", "CMakeLists.txt", "Justfile",
    "requirements.txt", "Gemfile", "go.mod", "go.sum",
}

BINARY_EXTENSIONS = {
    ".exe", ".dll", ".so", ".dylib", ".a", ".o", ".obj",
    ".png", ".jpg", ".jpeg", ".gif", ".bmp", ".ico", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".bz2", ".xz", ".7z",
    ".mp3", ".mp4", ".avi", ".mov", ".wav",
    ".ttf", ".otf", ".woff", ".woff2", ".eot",
    ".pyc", ".pyo", ".class", ".jar", ".wasm",
}

# Generic build-output, generated-code, and vendor exclusions. These are
# heuristics, not language knowledge; a repository can override them via the
# .codereview exclude_paths key.
DEFAULT_EXCLUDE_PATTERNS = [
    "vendor/*",
    "node_modules/*",
    "*.pb.*",
    "*.gen.*",
    "*_generated.*",
    "test/*",
    "tests/*",
    ".git/*",
    ".cache/*",
    "dist/*",
    "build/*",
    "target/*",
]


def run_command(cmd: list[str], cwd: str | None = None) -> tuple[int, str, str]:
    """Run a command with an argument list and return (code, stdout, stderr)."""
    try:
        result = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=False)
        return result.returncode, result.stdout, result.stderr
    except OSError as exc:
        return 1, "", str(exc)


def get_vcs_type(path: str | None = None) -> str:
    """Detect the VCS by walking upward for a marker, then by probing commands."""
    current = Path(path).resolve() if path else Path.cwd().resolve()
    while True:
        if (current / ".git").exists():
            return "git"
        if (current / ".svn").exists():
            return "svn"
        parent = current.parent
        if parent == current:
            break
        current = parent

    code, _, _ = run_command(["git", "rev-parse", "--show-toplevel"], cwd=str(Path.cwd()))
    if code == 0:
        return "git"
    code, _, _ = run_command(["svn", "info"], cwd=str(Path.cwd()))
    if code == 0:
        return "svn"
    return "unknown"


def get_git_root() -> str | None:
    """Return the Git working-copy root, or None when outside a repository."""
    code, stdout, _ = run_command(["git", "rev-parse", "--show-toplevel"])
    return stdout.strip() if code == 0 and stdout.strip() else None


def get_svn_root() -> str | None:
    """Return the SVN working-copy root, or None when outside a working copy."""
    code, stdout, _ = run_command(["svn", "info", "--show-item", "wc-root"])
    if code == 0 and stdout.strip():
        return stdout.strip()
    code, stdout, _ = run_command(["svn", "info"])
    if code == 0:
        for line in stdout.splitlines():
            if line.startswith("Working Copy Root Path:"):
                return line.split(":", 1)[1].strip()
    return None


def detect_language(file_path: str) -> Language:
    """Determine a changed file's language from its extension."""
    return EXTENSION_LANGUAGE_MAP.get(Path(file_path).suffix.lower(), Language.UNKNOWN)


def load_codereview_config(repo_root: str) -> dict:
    """Load the optional repo-root `.codereview` file, with declared defaults.

    Recognized keys: exclude_paths, ignore_categories, ignore_rules, severity.
    A malformed or unreadable file is a warning, never a fatal error.
    """
    config = {
        "exclude_paths": [],
        "ignore_categories": [],
        "ignore_rules": [],
        "severity": "normal",
    }
    config_path = Path(repo_root) / ".codereview"
    if not config_path.is_file():
        return config
    try:
        with config_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = (part.strip() for part in line.split("=", 1))
                if key in ("exclude_paths", "ignore_categories", "ignore_rules"):
                    config[key] = [item.strip() for item in value.split(",") if item.strip()]
                elif key == "severity":
                    config[key] = value
    except OSError as exc:
        print(f"Warning: could not read .codereview: {exc}", file=sys.stderr)
    return config


def should_exclude_file(file_path: str, exclude_patterns: list[str]) -> bool:
    """Return True when a path matches any exclusion pattern."""
    return any(fnmatch.fnmatch(file_path, pattern) for pattern in exclude_patterns)


def is_binary_file(file_path: str) -> bool:
    """Return True for a known binary extension."""
    return Path(file_path).suffix.lower() in BINARY_EXTENSIONS


def parse_diff_output(diff_output: str, verbose: bool = False) -> list[CodeChange]:
    """Parse unified diff output into per-file CodeChange records."""
    changes: list[CodeChange] = []
    current_file: str | None = None
    current_hunks: list[dict] = []
    added_lines = deleted_lines = 0
    added_non_blank = deleted_non_blank = 0

    def flush() -> None:
        """Store the file accumulated so far."""
        nonlocal current_file
        if current_file is None:
            return
        changes.append(CodeChange(
            file_path=current_file,
            language=detect_language(current_file),
            added_lines=added_lines,
            deleted_lines=deleted_lines,
            added_non_blank=added_non_blank,
            deleted_non_blank=deleted_non_blank,
            hunks=current_hunks,
        ))
        if verbose:
            print(f"  parsed: {current_file} (+{added_lines}/-{deleted_lines})", file=sys.stderr)

    for line in diff_output.split("\n"):
        if line.startswith("diff --git"):
            flush()
            match = re.search(r"diff --git a/(.+) b/(.+)", line)
            current_file = match.group(2) if match else None
            current_hunks = []
            added_lines = deleted_lines = 0
            added_non_blank = deleted_non_blank = 0
        elif line.startswith("@@"):
            match = re.search(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@", line)
            if match:
                current_hunks.append({
                    "old_start": int(match.group(1)),
                    "old_count": int(match.group(2)) if match.group(2) else 1,
                    "new_start": int(match.group(3)),
                    "new_count": int(match.group(4)) if match.group(4) else 1,
                    "content": [],
                })
        elif line.startswith("+") and not line.startswith("+++"):
            added_lines += 1
            if line[1:].strip():
                added_non_blank += 1
            if current_hunks:
                current_hunks[-1]["content"].append({"type": "add", "content": line[1:]})
        elif line.startswith("-") and not line.startswith("---"):
            deleted_lines += 1
            if line[1:].strip():
                deleted_non_blank += 1
            if current_hunks:
                current_hunks[-1]["content"].append({"type": "delete", "content": line[1:]})
        elif line.startswith(" "):
            if current_hunks:
                current_hunks[-1]["content"].append({"type": "context", "content": line[1:]})

    flush()
    return changes


def _normalize_svn_diff(svn_diff_output: str) -> str:
    """Rewrite SVN `Index:` headers into Git-style headers for the shared parser."""
    lines = svn_diff_output.split("\n")
    result: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("Index: "):
            file_path = line[len("Index: "):].strip()
            result.append(f"diff --git a/{file_path} b/{file_path}")
            if index + 1 < len(lines) and lines[index + 1].startswith("=" * 10):
                index += 1
        elif line.startswith(("--- ", "+++ ")) and "\t" in line:
            result.append(line.split("\t")[0])
        else:
            result.append(line)
        index += 1
    return "\n".join(result)


def _filter_changes(changes: list[CodeChange], exclude: list[str] | None,
                    verbose: bool) -> list[CodeChange]:
    """Keep analyzed text files, dropping binaries and excluded paths."""
    patterns = DEFAULT_EXCLUDE_PATTERNS.copy()
    if exclude:
        patterns.extend(exclude)
    original_count = len(changes)
    kept = [
        change for change in changes
        if (Path(change.file_path).suffix.lower() in ANALYZED_EXTENSIONS
            or Path(change.file_path).name in KNOWN_FILENAMES)
        and not is_binary_file(change.file_path)
        and not should_exclude_file(change.file_path, patterns)
    ]
    if verbose and original_count > len(kept):
        print(f"  kept {len(kept)}/{original_count} files after filtering", file=sys.stderr)
    return kept


def analyze_svn_diff(revision_range: str, files: list[str] | None = None,
                     exclude_patterns: list[str] | None = None,
                     verbose: bool = False) -> list[CodeChange]:
    """Analyze an SVN revision range (for example BASE:HEAD or 100:200)."""
    svn_root = get_svn_root()
    if not svn_root:
        print("Error: current directory is not an SVN working copy", file=sys.stderr)
        return []
    if verbose:
        print(f"SVN working copy root: {svn_root}", file=sys.stderr)
        print(f"Analyzing revision range: {revision_range}", file=sys.stderr)

    cmd = ["svn", "diff", "-r", revision_range, "--diff-cmd", "diff", "-x", "-U3"]
    if files:
        cmd.extend(files)
    code, stdout, stderr = run_command(cmd, cwd=svn_root)
    if code != 0:
        print(f"Error: svn diff failed: {stderr.strip()}", file=sys.stderr)
        return []
    if not stdout.strip():
        print("No changes found")
        return []
    if verbose:
        print("Parsing SVN diff output...", file=sys.stderr)
    return _filter_changes(parse_diff_output(_normalize_svn_diff(stdout), verbose),
                           exclude_patterns, verbose)


def analyze_git_diff(commit_range: str, files: list[str] | None = None,
                     exclude_patterns: list[str] | None = None,
                     verbose: bool = False) -> list[CodeChange]:
    """Analyze a Git commit range (for example HEAD~3..HEAD or main...HEAD)."""
    git_root = get_git_root()
    if not git_root:
        print("Error: current directory is not a Git repository", file=sys.stderr)
        return []
    if verbose:
        print(f"Git repository root: {git_root}", file=sys.stderr)
        print(f"Analyzing commit range: {commit_range}", file=sys.stderr)

    cmd = ["git", "diff", "--unified=3", commit_range]
    if files:
        cmd.append("--")
        cmd.extend(files)
    code, stdout, stderr = run_command(cmd, cwd=git_root)
    if code != 0:
        print(f"Error: git diff failed: {stderr.strip()}", file=sys.stderr)
        return []
    if not stdout.strip():
        print("No changes found")
        return []
    if verbose:
        print("Parsing diff output...", file=sys.stderr)
    return _filter_changes(parse_diff_output(stdout, verbose), exclude_patterns, verbose)


def format_as_json(changes: list[CodeChange]) -> str:
    """Serialize the change inventory as JSON."""
    result = [{
        "file_path": change.file_path,
        "language": change.language.value,
        "added_lines": change.added_lines,
        "deleted_lines": change.deleted_lines,
        "added_non_blank": change.added_non_blank,
        "deleted_non_blank": change.deleted_non_blank,
        "total_changes": change.total_changes,
        "hunk_count": len(change.hunks),
    } for change in changes]
    return json.dumps(result, indent=2, ensure_ascii=False)


def format_as_markdown(changes: list[CodeChange]) -> str:
    """Render a human-readable Markdown summary of the change inventory."""
    if not changes:
        return "No changes found"

    total_added = sum(change.added_lines for change in changes)
    total_deleted = sum(change.deleted_lines for change in changes)
    lang_stats: dict[str, dict] = {}
    for change in changes:
        stats = lang_stats.setdefault(change.language.value,
                                      {"files": 0, "added": 0, "deleted": 0})
        stats["files"] += 1
        stats["added"] += change.added_lines
        stats["deleted"] += change.deleted_lines

    lines = [
        "## Change Inventory",
        "",
        f"- **Files**: {len(changes)}",
        f"- **Added lines**: +{total_added}",
        f"- **Deleted lines**: -{total_deleted}",
        f"- **Total changed lines**: {total_added + total_deleted}",
        "",
        "### By language",
        "",
        "| Language | Files | Added | Deleted | Total |",
        "|----------|-------|-------|---------|-------|",
    ]
    for lang, stats in sorted(lang_stats.items()):
        lines.append(
            f"| {lang} | {stats['files']} | +{stats['added']} | "
            f"-{stats['deleted']} | {stats['added'] + stats['deleted']} |"
        )
    lines += [
        "",
        "### Per-file detail",
        "",
        "| File | Language | Added | Deleted | Hunks |",
        "|------|----------|-------|---------|-------|",
    ]
    for change in sorted(changes, key=lambda c: c.total_changes, reverse=True):
        lines.append(
            f"| `{change.file_path}` | {change.language.value} | "
            f"+{change.added_lines} | -{change.deleted_lines} | {len(change.hunks)} |"
        )
    return "\n".join(lines)


def print_summary(changes: list[CodeChange]) -> None:
    """Print a short summary to stdout."""
    if not changes:
        print("No changes found")
        return
    total_added = sum(change.added_lines for change in changes)
    total_deleted = sum(change.deleted_lines for change in changes)
    print("\nChange summary:")
    print(f"  Files: {len(changes)}")
    print(f"  Added: +{total_added}")
    print(f"  Deleted: -{total_deleted}")
    print(f"  Total: {total_added + total_deleted}")


def main() -> int:
    """Parse arguments, analyze the change set, and write or print the result."""
    parser = argparse.ArgumentParser(
        description="Analyze a VCS (Git/SVN) change set into a JSON inventory.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples (Git):
  %(prog)s --range HEAD~1..HEAD
  %(prog)s --range main...HEAD --output changes.json
  %(prog)s --range HEAD~3..HEAD --files src/a.py src/b.go
  %(prog)s --range HEAD~1..HEAD --format markdown --output changes.md

Examples (SVN):
  %(prog)s --vcs svn --range BASE:HEAD
  %(prog)s --vcs svn --range 100:200
  %(prog)s --vcs svn --range BASE:HEAD --files src/main.go
""",
    )
    parser.add_argument("--range", default=None,
                        help="Commit/revision range. Git default: HEAD~1..HEAD; "
                             "SVN default: BASE:HEAD. Passed through to the VCS.")
    parser.add_argument("--vcs", choices=["git", "svn", "auto"], default="auto",
                        help="Version control system (default: auto-detect)")
    parser.add_argument("--files", nargs="*", help="Restrict analysis to these files")
    parser.add_argument("--output", help="Write the result to this path")
    parser.add_argument("--format", choices=["json", "markdown"], default="json",
                        help="Output format (default: json)")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Print diagnostic detail to stderr")
    parser.add_argument("--no-config", action="store_true",
                        help="Ignore the repo-root .codereview file")
    args = parser.parse_args()

    vcs_type = get_vcs_type() if args.vcs == "auto" else args.vcs
    if vcs_type == "unknown":
        print("Error: could not detect the VCS; pass --vcs git or --vcs svn",
              file=sys.stderr)
        return 1
    if args.verbose and args.vcs == "auto":
        print(f"Detected VCS: {vcs_type}", file=sys.stderr)

    if args.range is None:
        args.range = "HEAD~1..HEAD" if vcs_type == "git" else "BASE:HEAD"
    if args.range.startswith("-"):
        print("Error: --range must be a revision range, not an option", file=sys.stderr)
        return 1

    exclude_patterns: list[str] | None = None
    if not args.no_config:
        repo_root = get_git_root() if vcs_type == "git" else get_svn_root()
        if repo_root:
            exclude_patterns = load_codereview_config(repo_root).get("exclude_paths")
            if args.verbose and exclude_patterns:
                print(f"Loaded .codereview exclusions: {exclude_patterns}", file=sys.stderr)

    if vcs_type == "git":
        changes = analyze_git_diff(args.range, args.files, exclude_patterns, args.verbose)
    else:
        changes = analyze_svn_diff(args.range, args.files, exclude_patterns, args.verbose)

    if not changes:
        return 0

    output = (format_as_markdown(changes) if args.format == "markdown"
              else format_as_json(changes))

    if args.output:
        destination = Path(args.output)
        try:
            destination.parent.mkdir(parents=True, exist_ok=True)
            with destination.open("w", encoding="utf-8") as handle:
                handle.write(output)
            print(f"Result written to: {destination}")
            if not args.verbose:
                print_summary(changes)
        except OSError as exc:
            print(f"Error: could not write output file: {exc}", file=sys.stderr)
            return 1
    else:
        print(output)
    return 0


if __name__ == "__main__":
    sys.exit(main())
