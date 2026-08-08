"""Selector profiles.

A profile is how you use this on a particular site without anything
site-specific entering the code. The three selectors and a few pacing
defaults live in a TOML file you own; the package ships one example built
against the local test fixtures and none against any real site.

stdlib tomllib, which is why the Python floor is 3.11 rather than 3.10.
"""

from __future__ import annotations

import tomllib
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SELECTOR_KEYS = ("link", "title", "content")

# Profile key -> the flag that overrides it. Explicit flags always win, so a
# profile is a starting point rather than something you have to edit to run
# one different command.
OPTION_FLAGS = {
    "max": "--max",
    "out": "--out",
    "concurrency": "--concurrency",
    "retries": "--retries",
    "min_delay": "--min-delay",
    "max_delay": "--max-delay",
    "wait_after_load": "--wait-after-load",
    "timeout": "--timeout",
    "include_links": "--include-links",
    "formats": "--format",
    "ua": "--ua",
}


class ProfileError(ValueError):
    """A profile that cannot be used, with a message naming the problem."""


@dataclass(frozen=True, slots=True)
class Profile:
    """A parsed profile. Absent values are None so they cannot mask a flag."""

    path: Path
    link: str | None = None
    title: str | None = None
    content: str | None = None
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def selectors(self) -> dict[str, str]:
        return {
            key: value
            for key, value in (
                ("link", self.link),
                ("title", self.title),
                ("content", self.content),
            )
            if value is not None
        }


def load_profile(path: Path) -> Profile:
    """Read a profile, refusing anything malformed rather than half-applying it."""
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ProfileError(f"no profile at {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ProfileError(f"{path} is not valid TOML: {exc}") from exc
    except OSError as exc:
        raise ProfileError(f"could not read {path}: {exc}") from exc

    selectors = raw.get("selectors", {})
    if not isinstance(selectors, dict):
        raise ProfileError(f"{path}: [selectors] must be a table")
    unknown_selectors = set(selectors) - set(SELECTOR_KEYS)
    if unknown_selectors:
        raise ProfileError(
            f"{path}: unknown key(s) in [selectors]: {', '.join(sorted(unknown_selectors))}. "
            f"Valid keys: {', '.join(SELECTOR_KEYS)}"
        )
    for key, value in selectors.items():
        if not isinstance(value, str):
            raise ProfileError(f"{path}: [selectors].{key} must be a string")

    options = raw.get("options", {})
    if not isinstance(options, dict):
        raise ProfileError(f"{path}: [options] must be a table")
    unknown_options = set(options) - set(OPTION_FLAGS)
    if unknown_options:
        # A typo that is silently ignored is a profile that does not do what it
        # says, which is worse than one that refuses to load.
        raise ProfileError(
            f"{path}: unknown key(s) in [options]: {', '.join(sorted(unknown_options))}. "
            f"Valid keys: {', '.join(sorted(OPTION_FLAGS))}"
        )

    if "formats" in options:
        formats = options["formats"]
        if not isinstance(formats, list) or not all(isinstance(item, str) for item in formats):
            raise ProfileError(f"{path}: [options].formats must be a list of strings")

    return Profile(
        path=path,
        link=selectors.get("link"),
        title=selectors.get("title"),
        content=selectors.get("content"),
        options=dict(options),
    )


def explicit_flags(argv: Sequence[str]) -> set[str]:
    """The long options actually typed on the command line.

    Comparing a parsed value against the parser default cannot tell "not
    given" from "given, and happens to equal the default" - so a profile would
    override a flag the user did pass. Reading argv answers the question
    directly.
    """
    found: set[str] = set()
    for token in argv:
        if token.startswith("--"):
            found.add(token.split("=", 1)[0])
    return found


def merge(namespace: Any, profile: Profile, argv: Sequence[str]) -> list[str]:
    """Apply profile values the command line did not set. Returns what changed."""
    given = explicit_flags(argv)
    applied: list[str] = []

    for key, value in profile.selectors.items():
        if f"--{key}" not in given:
            setattr(namespace, key, value)
            applied.append(key)

    for key, value in profile.options.items():
        flag = OPTION_FLAGS[key]
        if flag in given:
            continue
        setattr(namespace, key if key != "formats" else "formats", value)
        applied.append(key)

    return applied
