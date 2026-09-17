"""Unit tests for the statewave_tools drop-in helpers — no network calls.

This file is meant to be copied into other people's projects and imported with
`from statewave_tools import ...`, so every exported name lands directly in a
namespace this repo never sees. A helper named after a builtin rebinds it there,
and the failure surfaces in the caller's code rather than here.
"""
import builtins
import re
from pathlib import Path

import statewave_tools

REPO_ROOT = Path(__file__).resolve().parents[1]
# Indented: the quick-start block in the module docstring sits inside it.
IMPORT_RE = re.compile(r"^[ \t]*from statewave_tools import (.+)$", re.MULTILINE)


def _public_names() -> set[str]:
    return {name for name in vars(statewave_tools) if not name.startswith("_")}


def _imported_names(text: str) -> list[str]:
    """Every name any `from statewave_tools import ...` line in text pulls in."""
    return [
        name.strip()
        for line in IMPORT_RE.findall(text)
        for name in line.split(",")
        if name.strip()
    ]


def test_no_helper_shadows_a_builtin():
    shadowed = sorted(name for name in _public_names() if hasattr(builtins, name))
    assert shadowed == [], f"statewave_tools exports shadow builtins: {shadowed}"


def test_compile_helper_is_exported():
    assert callable(statewave_tools.compile_subject)


def test_module_docstring_quick_start_imports_resolve():
    names = _imported_names(statewave_tools.__doc__ or "")
    assert names, "the module docstring no longer shows a quick-start import"
    missing = [name for name in names if not hasattr(statewave_tools, name)]
    assert missing == [], f"docstring imports names that do not exist: {missing}"


def test_readme_snippet_imports_resolve():
    names = _imported_names((REPO_ROOT / "README.md").read_text(encoding="utf-8"))
    assert names, "the README no longer shows a statewave_tools import"
    missing = sorted({name for name in names if not hasattr(statewave_tools, name)})
    assert missing == [], f"README imports names that do not exist: {missing}"
