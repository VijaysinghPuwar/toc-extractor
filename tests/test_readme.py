"""The README's factual claims, asserted.

Documentation is the part of a repository most likely to rot, because nothing
fails when it does. Every check here is on a claim that would be wrong-and-
silent otherwise: a version floor that drifted from pyproject, a make target
that was renamed, a CLI reference that stopped matching the parser.
"""

from __future__ import annotations

import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
README = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
PYPROJECT = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
MAKEFILE = (REPO_ROOT / "Makefile").read_text(encoding="utf-8")


def test_the_stated_python_range_matches_pyproject() -> None:
    floor = PYPROJECT["project"]["requires-python"]
    assert floor == ">=3.11"

    classifiers = PYPROJECT["project"]["classifiers"]
    ceiling = max(
        int(item.rsplit(".", 1)[1])
        for item in classifiers
        if item.startswith("Programming Language :: Python :: 3.")
    )
    # The README uses an en dash for the range; noqa because that is the
    # character being asserted, not an accident.
    assert f"Python 3.11\u20133.{ceiling}" in README


def test_the_only_runtime_dependency_claim_holds() -> None:
    """The README says Playwright is the only one. A second would make it false."""
    dependencies = PYPROJECT["project"]["dependencies"]
    assert len(dependencies) == 1
    assert dependencies[0].startswith("playwright")
    assert "the only runtime dependency" in README


@pytest.mark.parametrize(
    "target",
    ["setup", "deps", "lint", "typecheck", "test", "test-fast", "run", "gui"],
)
def test_every_documented_make_target_exists(target: str) -> None:
    assert f"\n{target}:" in MAKEFILE, f"README documents `make {target}`"
    assert f"make {target}" in README


def test_the_run_target_invokes_the_module() -> None:
    """The README shows `make run ARGS=...`; the target must accept ARGS."""
    assert "make run ARGS=" in README
    run_body = MAKEFILE.split("\nrun:", 1)[1].split("\n\n", 1)[0]
    assert "-m toc_extractor" in run_body
    assert "$(ARGS)" in run_body


def test_the_cli_reference_matches_the_parser() -> None:
    """Generated, so a flag added without regenerating fails here."""
    from scripts_gen_readme import cli_help

    match = re.search(
        r"<!-- cli-reference: generated, do not edit by hand -->\s*```\n(.*?)\n```",
        README,
        re.DOTALL,
    )
    assert match is not None, "the CLI reference markers are missing"
    assert match.group(1).rstrip() == cli_help().rstrip(), (
        "run ./.venv/bin/python scripts_gen_readme.py"
    )


def test_every_format_the_readme_names_is_registered() -> None:
    from toc_extractor.exporters import available

    for name in available():
        assert f"`{name}`" in README, name


def test_the_v1_tag_resolves() -> None:
    """`git diff v1.0.0..v2.0.0` is offered as the way to read the rewrite."""
    result = subprocess.run(
        ["git", "rev-parse", "--verify", "v1.0.0^{commit}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, "v1.0.0 must remain tagged"


def test_the_readme_does_not_mention_the_deleted_scripts() -> None:
    for name in ("toc_playwright.py", "cli_runner.py"):
        assert name not in README, f"{name} no longer exists"


def test_the_test_count_claim_is_not_overstated() -> None:
    """Any number in the README must be one the suite can actually meet."""
    claims = re.findall(r"(\d[\d,]*)\s+tests", README)
    for claim in claims:
        assert int(claim.replace(",", "")) <= _collected(), (
            f"README claims {claim} tests; the suite collects {_collected()}"
        )


def _collected() -> int:
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    match = re.search(r"(\d+) tests? collected", result.stdout)
    return int(match.group(1)) if match else 0


def test_no_emoji() -> None:
    """A standing constraint on this repository, checked rather than remembered."""
    assert not re.search(r"[\U0001F300-\U0001FAFF☀-➿]", README)


def test_the_example_profile_the_readme_points_at_exists() -> None:
    assert "profiles/example.toml" in README
    assert (REPO_ROOT / "profiles" / "example.toml").exists()


def test_the_generator_the_readme_names_exists() -> None:
    assert "scripts_gen_readme.py" in README
    assert (REPO_ROOT / "scripts_gen_readme.py").exists()
