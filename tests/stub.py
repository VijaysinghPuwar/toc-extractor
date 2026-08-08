"""A dict-backed PageSource for tests.

No socket, no browser, no port allocation. Retry rules, concurrency,
politeness composition, and checkpoint behaviour are all decided by
fetcher.py, and none of them need a real page to be exercised — so none of
those tests should pay for one.

The stub records the time of every load against the injected clock, which is
what lets the composition test assert observed intervals without the suite
spending real seconds.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from toc_extractor.pagesource import (
    CaptureUnsupported,
    ChapterPage,
    PageError,
    PageTimeout,
    SelectorNotFound,
    TocPage,
)


@dataclass
class StubPage:
    """One canned page.

    `fail_times` makes a page fail its first N loads and then succeed, which
    is how retry behaviour is tested without inducing real network failures.
    """

    links: Sequence[object] = ()
    title: str = "Chapter"
    body: str = "Body text."
    html: str = "<html></html>"
    redirect_to: str | None = None
    fail_times: int = 0
    failure: type[PageError] = PageTimeout
    missing_selector: bool = False
    # Real seconds, not fake-clock seconds. Only the hard per-page timeout
    # needs it, and that one is enforced by asyncio against the event loop's
    # own clock, so it cannot be driven by an injected clock.
    hang: float = 0.0
    _failures_served: int = field(default=0, init=False)


class StubPageSource:
    """A PageSource backed by a dict of canned pages."""

    def __init__(
        self,
        pages: Mapping[str, StubPage],
        *,
        clock: Callable[[], float] = time.monotonic,
        supports_capture: bool = False,
        max_concurrent: int = 1,
    ) -> None:
        self._pages = dict(pages)
        self._clock = clock
        self._supports_capture = supports_capture
        # A real page cannot serve two navigations at once: concurrent goto()
        # calls abort each other with net::ERR_ABORTED. The stub defaulted to
        # unlimited, so the fetcher happily drove one page from several workers
        # and 447 passing tests said nothing. Modelling the limit here means a
        # caller has to state how many pages it believes it has.
        self._max_concurrent = max(1, max_concurrent)
        self._in_flight = 0
        self.max_observed_concurrency = 0
        self.loads: list[tuple[float, str]] = []
        self.closed = False

    # -- test observability -------------------------------------------------

    @property
    def urls_loaded(self) -> list[str]:
        return [url for _, url in self.loads]

    def load_times_for(self, url: str) -> list[float]:
        return [when for when, loaded in self.loads if loaded == url]

    def attempts_for(self, url: str) -> int:
        return sum(1 for _, loaded in self.loads if loaded == url)

    # -- PageSource ---------------------------------------------------------

    async def load_toc(
        self,
        url: str,
        *,
        link_selector: str,
        capture_html: bool = False,
        screenshot_path: Path | None = None,
    ) -> TocPage:
        if (capture_html or screenshot_path is not None) and not self._supports_capture:
            raise CaptureUnsupported(
                "the stub page source has no renderer; --dump-html and --screenshot "
                "need the browser-backed source"
            )
        page, final_url = self._resolve(url)
        return TocPage(
            requested_url=url,
            final_url=final_url,
            raw_links=tuple(page.links),
            html=page.html if capture_html else None,
        )

    async def load_chapter(
        self,
        url: str,
        *,
        title_selector: str,
        content_selector: str,
    ) -> ChapterPage:
        self._enter()
        try:
            page, final_url = self._resolve(url)
            if page.hang:
                await asyncio.sleep(page.hang)
            if page.missing_selector:
                raise SelectorNotFound(f"{content_selector} matched nothing on {final_url}")
            return ChapterPage(
                requested_url=url,
                final_url=final_url,
                title=page.title,
                body=page.body,
            )
        finally:
            self._in_flight -= 1

    async def aclose(self) -> None:
        self.closed = True

    # -- internals ----------------------------------------------------------

    def _enter(self) -> None:
        self._in_flight += 1
        self.max_observed_concurrency = max(self.max_observed_concurrency, self._in_flight)
        if self._in_flight > self._max_concurrent:
            in_flight = self._in_flight
            self._in_flight -= 1
            raise PageError(
                f"{in_flight} concurrent loads against {self._max_concurrent} page(s): "
                f"a real page aborts the earlier navigation"
            )

    def _resolve(self, url: str) -> tuple[StubPage, str]:
        self.loads.append((self._clock(), url))

        seen: list[str] = []
        current = url
        while True:
            if current in seen:
                raise PageError(f"redirect loop: {' -> '.join([*seen, current])}")
            seen.append(current)

            page = self._pages.get(current)
            if page is None:
                raise PageError(f"no stub page registered for {current}")

            if page.redirect_to is not None:
                current = page.redirect_to
                continue

            if page._failures_served < page.fail_times:
                page._failures_served += 1
                raise page.failure(f"stub failure {page._failures_served} for {current}")

            return page, current
