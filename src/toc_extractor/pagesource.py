"""The seam between the fetch loop and whatever actually loads pages.

Shaped by the two page kinds this tool understands, not by Playwright's API.
If this protocol ever starts mirroring `Page`, it has stopped buying isolation
and should be reconsidered.

Nothing downstream of this module imports Playwright: the browser-backed
implementation translates its exceptions into the ones defined here, so the
fetch loop's retry rules are written against this vocabulary alone.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from .politeness import RejectionReason


class PageError(Exception):
    """Base for anything that went wrong loading a page."""


class PageTimeout(PageError):
    """The page did not load, or a selector did not resolve, in time.

    Distinct from PageError because it is the one failure the fetch loop
    retries by default.
    """


class PageBlocked(PageError):
    """A navigation or one of its redirect hops was refused by the URL guard.

    Never retried: the target is disallowed, and trying again cannot change
    that. Carries the offending hop rather than the originally requested URL,
    because with a redirect chain those differ and only the hop is actionable.
    """

    def __init__(self, url: str, reason: RejectionReason, detail: str = "") -> None:
        self.url = url
        self.reason = reason
        self.detail = detail
        suffix = f" ({detail})" if detail else ""
        super().__init__(f"blocked {url}: {reason.value}{suffix}")


class CaptureUnsupported(PageError):
    """This page source cannot produce HTML dumps or screenshots."""


class SelectorNotFound(PageError):
    """A selector matched nothing on an otherwise healthy page.

    Separate from PageTimeout so the fetch loop does not burn retries on a
    page that loaded correctly and simply does not contain what was asked for.
    """


@dataclass(frozen=True, slots=True)
class TocPage:
    """A loaded table-of-contents page.

    `raw_links` is deliberately untyped and uncounted — exactly what the DOM
    produced, including any non-string values. parser.py is the single place
    that decides what is fetchable, so filtering here would move the
    accounting boundary and reintroduce the silent-drop bug it exists to stop.
    """

    requested_url: str
    final_url: str
    raw_links: tuple[object, ...]
    html: str | None = None


@dataclass(frozen=True, slots=True)
class ChapterPage:
    """A loaded chapter page with its two extracted fields."""

    requested_url: str
    final_url: str
    title: str
    body: str


class PageSource(Protocol):
    """Load pages and read named fields out of them.

    `final_url` is reported by the implementation rather than read back off
    the page afterwards. The browser-backed source fulfils intercepted
    responses at the originally requested URL, so the browser never learns a
    redirect happened and its own idea of the current URL is wrong.
    """

    async def load_toc(
        self,
        url: str,
        *,
        link_selector: str,
        capture_html: bool = False,
        screenshot_path: Path | None = None,
    ) -> TocPage: ...

    async def load_chapter(
        self,
        url: str,
        *,
        title_selector: str,
        content_selector: str,
    ) -> ChapterPage: ...

    async def aclose(self) -> None: ...
