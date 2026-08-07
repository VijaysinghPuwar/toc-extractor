"""Canonical text cleaning and filename sanitisation.

This is the single implementation that replaces the two drifted copies in the v1
scripts. `tests/golden/v1_golden.json` records what those copies did; anything
here that differs from it is a deliberate change listed in the CHANGING section
of `tests/golden/corpus.py`.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

# v1 capped titles at 150 characters. Kept because the golden pins it.
NAME_MAX_CHARS = 150

# APFS and HFS+ cap a single path component at 255 *bytes*, not characters. v1
# used a character cap only, so a 150-character CJK title produced a 450-byte
# component that the filesystem rejects.
NAME_MAX_BYTES = 255

_AD_PATTERNS = (
    re.compile(r"Ads by\s+\w+", re.IGNORECASE),
    re.compile(r"Sponsored\s+Content", re.IGNORECASE),
)
_URL_RE = re.compile(r"https?://\S+")
_WS_BEFORE_NL = re.compile(r"\s+\n")
_WS_AFTER_NL = re.compile(r"\n\s+")
_BLANK_RUN = re.compile(r"\n{3,}")

_LINEBREAKS = re.compile(r"[\t\n\r]")
# Remaining C0 controls and DEL. v1 handled only tab/newline/CR, so a title
# carrying a NUL produced a name that os.open rejects outright.
_CONTROLS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
# Canonical form is the v1 CLI regex, which has the trailing + that the v1 GUI
# regex lacked. Collapsing runs *increases* collision probability: "A//B" and
# "A/B" both become "A_B". That is only acceptable because FilenameAllocator
# resolves collisions case-insensitively before anything touches the disk. Drop
# the allocator and this line becomes silent data loss.
_FORBIDDEN = re.compile(r'[\\/:*?"<>|]+')
_WHITESPACE = re.compile(r"\s+")
_LEADING_DOTS = re.compile(r"^\.+")


@dataclass(frozen=True, slots=True)
class CleanedText:
    """Cleaned body text plus what cleaning removed.

    `stripped_urls` exists because v1 deleted URLs from prose with no record.
    The deletion itself is unchanged; returning the count as a peer of the text
    is what makes it impossible for a caller to report the text without the loss.
    """

    text: str
    stripped_urls: int


def clean_text(text: str, *, remove_links: bool = True, strip_ads: bool = True) -> CleanedText:
    """Normalise extracted body text.

    Defaults match v1 exactly. Both flags are wired to CLI options, so the
    substitution order below is load-bearing: URLs are removed before whitespace
    collapses, which is why removing a URL on its own line leaves no blank gap.
    """
    if strip_ads:
        for pattern in _AD_PATTERNS:
            text = pattern.sub("", text)

    stripped_urls = 0
    if remove_links:
        text, stripped_urls = _URL_RE.subn("", text)

    text = _WS_BEFORE_NL.sub("\n", text)
    text = _WS_AFTER_NL.sub("\n", text)
    text = _BLANK_RUN.sub("\n\n", text)
    return CleanedText(text=text.strip(), stripped_urls=stripped_urls)


def truncate_utf8(value: str, max_bytes: int) -> str:
    """Trim to at most `max_bytes` UTF-8 bytes without splitting a character."""
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max_bytes].decode("utf-8", errors="ignore")


def safe_filename(
    name: str,
    *,
    max_chars: int = NAME_MAX_CHARS,
    max_bytes: int = NAME_MAX_BYTES,
) -> str:
    """Turn a chapter title into one safe path component.

    Not collision-free on its own — use FilenameAllocator for that.
    """
    # NFC first, because macOS returns NFD from the filesystem while the DOM
    # supplies NFC. Without this the same title compares unequal depending on
    # whether it came from a page or from a directory listing, which breaks
    # both dedup and resume.
    name = unicodedata.normalize("NFC", name)
    name = _LINEBREAKS.sub(" ", name).strip()
    name = _CONTROLS.sub("", name)
    name = _FORBIDDEN.sub("_", name)
    name = _WHITESPACE.sub(" ", name)
    # A leading dot hides the file in Finder and in ls, so a chapter titled
    # ".Prologue" silently vanishes from the output directory.
    name = _LEADING_DOTS.sub("", name).strip()

    name = name or "untitled"
    name = name[:max_chars]
    return truncate_utf8(name, max_bytes) or "untitled"


@dataclass(frozen=True, slots=True)
class AllocatedName:
    """One allocated path component and how it was reached."""

    name: str
    collided_with: str | None = None

    @property
    def deduplicated(self) -> bool:
        return self.collided_with is not None


class FilenameAllocator:
    """Hands out unique path components for one output directory.

    Uniqueness is case-insensitive because APFS is case-insensitive by default:
    "Chapter One.txt" and "chapter one.txt" are the same file, and v1 let the
    second silently overwrite the first.
    """

    def __init__(self, *, suffix: str = ".txt", max_bytes: int = NAME_MAX_BYTES) -> None:
        self._suffix = suffix
        self._max_bytes = max_bytes
        self._taken: dict[str, str] = {}

    @staticmethod
    def _key(value: str) -> str:
        # NFC and casefold together, matching how the filesystem compares.
        return unicodedata.normalize("NFC", value).casefold()

    def allocate(self, index: int, title: str) -> AllocatedName:
        """Allocate `NNN - Title.txt`, deduplicating against earlier calls."""
        prefix = f"{index:03d} - "
        reserved = len(prefix.encode("utf-8")) + len(self._suffix.encode("utf-8"))
        base = safe_filename(title, max_bytes=max(1, self._max_bytes - reserved))

        candidate = f"{prefix}{base}{self._suffix}"
        key = self._key(candidate)
        if key not in self._taken:
            self._taken[key] = candidate
            return AllocatedName(name=candidate)

        original = self._taken[key]
        counter = 2
        while True:
            marker = f" ({counter})"
            # Re-trim the title so the marker cannot push the component over the
            # byte limit on a name that was already at it.
            trimmed = truncate_utf8(
                base, max(1, self._max_bytes - reserved - len(marker.encode("utf-8")))
            )
            candidate = f"{prefix}{trimmed}{marker}{self._suffix}"
            key = self._key(candidate)
            if key not in self._taken:
                self._taken[key] = candidate
                return AllocatedName(name=candidate, collided_with=original)
            counter += 1
