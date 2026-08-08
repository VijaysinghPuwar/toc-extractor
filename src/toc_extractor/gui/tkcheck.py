"""Report an unusable Tk before something crashes on import.

The advice is specific because the generic advice is wrong on this machine:
Homebrew's python3 ships without _tkinter at all, the python-tk@3.11 and
python-tk@3.12 formulas only help if a matching Homebrew interpreter is also
installed, and /usr/bin/python3 carries Apple's deprecated Tk 8.5.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass

FRAMEWORK_HINT = "/Library/Frameworks/Python.framework/Versions/3.14/bin/python3"
MINIMUM_TK = (8, 6)


@dataclass(frozen=True, slots=True)
class TkStatus:
    usable: bool
    version: str = ""
    message: str = ""


def check() -> TkStatus:
    try:
        import tkinter
    except ImportError:
        return TkStatus(usable=False, message=_missing_message())

    version = str(getattr(tkinter, "TkVersion", "unknown"))
    try:
        parts = tuple(int(part) for part in version.split(".")[:2])
    except ValueError:
        return TkStatus(usable=True, version=version)

    if parts < MINIMUM_TK:
        return TkStatus(usable=False, version=version, message=_old_message(version))
    return TkStatus(usable=True, version=version)


def _missing_message() -> str:
    return (
        f"This Python ({sys.executable}) has no _tkinter, so the GUI cannot start.\n"
        "The command line front end does not need Tk:\n"
        "    python -m toc_extractor --help\n"
        "\n"
        "For the GUI, build the environment against a python.org framework build:\n"
        "    make clean\n"
        f"    make setup PYTHON={FRAMEWORK_HINT}\n"
        "\n"
        "Homebrew's python3 ships without _tkinter. Installing python-tk@3.11 or\n"
        "python-tk@3.12 only helps if you also have that exact Homebrew\n"
        "interpreter, and /usr/bin/python3 carries the deprecated Tk 8.5."
    )


def _old_message(version: str) -> str:
    return (
        f"This Python has Tk {version}; the GUI needs 8.6 or newer.\n"
        "Tk 8.5 is Apple's deprecated build and misrenders on current macOS.\n"
        "\n"
        "Build the environment against a python.org framework build:\n"
        "    make clean\n"
        f"    make setup PYTHON={FRAMEWORK_HINT}"
    )


def require() -> None:
    """Exit with an actionable message rather than an ImportError traceback."""
    status = check()
    if not status.usable:
        print(status.message, file=sys.stderr)
        raise SystemExit(2)
