"""Record what v1's clean_text and safe_filename actually do.

The v1 scripts have been deleted, so this only runs against the tag that
still contains them:

    git worktree add /tmp/v1 v1.0.0
    ./.venv/bin/python /tmp/v1/tests/golden/capture_v1.py

Kept rather than deleted because it documents how v1_golden.json was produced
and lets anyone reproduce it. The fixture itself is committed and carries the
source hashes it was captured from.

It writes v1_golden.json next to this file. The output is provenance-stamped
with the interpreter version, the git commit, and a hash of each source file it
imported, so a reviewer can tell whether the golden was captured from the real
v1 or from something that had already been touched.

Both v1 implementations are captured separately. They have drifted, and a
capture that silently picked one would hide that.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
OUTPUT = Path(__file__).resolve().parent / "v1_golden.json"
V1_FILES = ("toc_playwright.py", "cli_runner.py")


def load_v1_modules() -> dict[str, ModuleType]:
    """Import the two v1 scripts by path.

    Imported inside a function so the sys.path insert happens before the import
    without tripping the module-level-import lint.
    """
    sys.path.insert(0, str(REPO_ROOT))
    import cli_runner
    import toc_playwright

    return {"gui": toc_playwright, "cli": cli_runner}


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args: str) -> str:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unavailable"


def record(value: str) -> dict[str, Any]:
    """Store the value, its byte length, and its hash.

    The hash is what makes a byte-identity claim checkable; the raw value is
    what makes a failure readable.
    """
    encoded = value.encode("utf-8")
    return {"value": value, "utf8_bytes": len(encoded), "sha256": sha256_text(value)}


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from corpus import CLEAN_TEXT_COMBOS, FILENAME_CASES, TEXT_CASES

    mods = load_v1_modules()

    golden: dict[str, Any] = {
        "_provenance": {
            "python": sys.version.split()[0],
            "platform": sys.platform,
            "git_commit": git("rev-parse", "HEAD"),
            "git_describe": git("describe", "--tags", "--always"),
            "git_dirty": bool(git("status", "--porcelain", *V1_FILES)),
            "source_sha256": {f: sha256_file(REPO_ROOT / f) for f in V1_FILES},
        },
        "clean_text": {},
        "safe_filename": {},
    }

    for impl, mod in mods.items():
        golden["clean_text"][impl] = {}
        for combo, remove_links, strip_ads in CLEAN_TEXT_COMBOS:
            golden["clean_text"][impl][combo] = {
                case.id: record(
                    mod.clean_text(case.value, remove_links=remove_links, strip_ads=strip_ads)
                )
                for case in TEXT_CASES
            }
        golden["safe_filename"][impl] = {
            case.id: record(mod.safe_filename(case.value)) for case in FILENAME_CASES
        }

    OUTPUT.write_text(json.dumps(golden, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    divergences = report_divergences(golden)
    print(f"wrote {OUTPUT.relative_to(REPO_ROOT)}")
    print(f"clean_text cases:    {len(TEXT_CASES)} x {len(CLEAN_TEXT_COMBOS)} combos x 2 impls")
    print(f"safe_filename cases: {len(FILENAME_CASES)} x 2 impls")
    print(f"gui/cli divergences: {divergences}")
    return 0


def report_divergences(golden: dict[str, Any]) -> int:
    """Print every input where the two v1 implementations disagree."""
    count = 0
    for combo in golden["clean_text"]["gui"]:
        gui = golden["clean_text"]["gui"][combo]
        cli = golden["clean_text"]["cli"][combo]
        for case_id in gui:
            if gui[case_id]["sha256"] != cli[case_id]["sha256"]:
                count += 1
                print(f"  clean_text[{combo}][{case_id}]:")
                print(f"    gui={gui[case_id]['value']!r}")
                print(f"    cli={cli[case_id]['value']!r}")

    gui_fn = golden["safe_filename"]["gui"]
    cli_fn = golden["safe_filename"]["cli"]
    for case_id in gui_fn:
        if gui_fn[case_id]["sha256"] != cli_fn[case_id]["sha256"]:
            count += 1
            print(f"  safe_filename[{case_id}]:")
            print(f"    gui={gui_fn[case_id]['value']!r}")
            print(f"    cli={cli_fn[case_id]['value']!r}")
    return count


if __name__ == "__main__":
    raise SystemExit(main())
