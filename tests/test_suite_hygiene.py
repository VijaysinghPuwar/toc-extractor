"""Rules about the test suite itself.

Each one here exists because the mistake was actually made, not because it was
imagined. Structural checks beat remembering.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

TESTS_DIR = Path(__file__).resolve().parent
SRC_DIR = TESTS_DIR.parent / "src" / "toc_extractor"


def python_test_files() -> list[Path]:
    return sorted(TESTS_DIR.rglob("*.py"))


@pytest.mark.parametrize("path", python_test_files(), ids=lambda p: p.name)
def test_no_literal_keyboard_interrupt(path: Path) -> None:
    """Raising KeyboardInterrupt in a test tears down the pytest session.

    Made twice: once drafting the error-boundary tests, once in the checkpoint
    resume test. Both times the suite still reported passes while printing an
    interrupt banner, which is exactly the kind of half-failure nobody chases.
    Use a private BaseException subclass instead - same code path, no
    collateral damage.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    for node in ast.walk(tree):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        raised = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        name = getattr(raised, "id", None) or getattr(raised, "attr", None)
        assert name != "KeyboardInterrupt", (
            f"{path.name}:{node.lineno} raises KeyboardInterrupt; use a private "
            f"BaseException subclass so pytest is not torn down"
        )


# A check on sleep() durations was drafted and dropped. It flagged the
# deliberate 10s hang in test_error_boundary.py, which is cancelled long before
# it elapses and costs nothing - an AST scan cannot tell an intentional hang
# from a real wait. A test that cries wolf is one a later reader silences by
# deleting it, so wall-clock cost is left to whoever notices the suite slowing
# down.


def test_browser_tests_are_marked() -> None:
    """An unmarked browser test makes `make test-fast` silently slower and networked."""
    browser_files = [p for p in python_test_files() if "browser" in p.name]
    assert browser_files, "expected at least one browser test module"

    for path in browser_files:
        source = path.read_text(encoding="utf-8")
        assert "pytestmark = pytest.mark.browser" in source, path.name


def test_src_never_imports_the_legacy_scripts() -> None:
    """The package must not depend on files scheduled for deletion."""
    for path in SRC_DIR.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "import cli_runner" not in source, path.name
        assert "import toc_playwright" not in source, path.name


def test_only_the_browser_module_imports_playwright() -> None:
    """The PageSource protocol is the boundary; one module may cross it."""
    offenders = []
    for path in SRC_DIR.rglob("*.py"):
        if path.name == "browser.py":
            continue
        if "playwright" in path.read_text(encoding="utf-8"):
            offenders.append(path.name)
    assert offenders == [], f"playwright referenced outside browser.py: {offenders}"
