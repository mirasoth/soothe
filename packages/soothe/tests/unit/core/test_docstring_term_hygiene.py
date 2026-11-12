"""Docstring hygiene: internal design-doc identifiers never appear in docstrings.

Docstrings must stand alone (agent rules); RFC-/IG- identifiers are allowed
in comments and .platonic docs only.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

_SRC = Path(__file__).parents[3] / "src" / "soothe"
_REF = re.compile(r"(?:RFC|IG)-\d")
# Mechanically ill-formed docstrings from identifier removals (leading colon).
_COLON_PREFIX = re.compile(r"^(\s*(?:\"\"\"|''')?): ")

DOC_NODES = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)


def _docstring_violations() -> list[str]:
    violations: list[str] = []
    for path in _SRC.rglob("*.py"):
        src = path.read_text(encoding="utf-8")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, DOC_NODES):
                doc = ast.get_docstring(node, clean=False)
                if doc and _REF.search(doc):
                    violations.append(f"{path.relative_to(_SRC)}:{node.lineno} docstring")
                if doc and _COLON_PREFIX.search(doc):
                    violations.append(f"{path.relative_to(_SRC)}:{node.lineno} leading colon")
            # Attribute docstrings: bare string statements after assignments.
            elif isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant):
                value = node.value.value
                if isinstance(value, str) and _REF.search(value):
                    violations.append(f"{path.relative_to(_SRC)}:{node.lineno} attribute doc")
    return violations


def _config_description_violations() -> list[str]:
    violations: list[str] = []
    for path in _SRC.rglob("*.py"):
        for i, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if "description=" in line and _REF.search(line):
                violations.append(f"{path.relative_to(_SRC)}:{i} field description")
    return violations


def test_docstrings_have_no_design_doc_identifiers() -> None:
    assert _docstring_violations() == []


def test_config_descriptions_have_no_design_doc_identifiers() -> None:
    assert _config_description_violations() == []


@pytest.mark.parametrize("category", ["docstrings", "descriptions"])
def test_term_hygiene_summary(category: str) -> None:
    """Explicit failure listing (the empty-assert tests above stop at first diff)."""
    violations = (
        _docstring_violations() if category == "docstrings" else _config_description_violations()
    )
    assert not violations, "\n".join(violations)
