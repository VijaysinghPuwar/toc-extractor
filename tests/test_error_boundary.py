"""Nothing may escape a PageSource call except PageError or CancelledError.

This exists because the invariant was already violated once: asyncio.timeout
raises a stdlib TimeoutError, which is outside the PageError vocabulary the
retry rules are written against, so it bypassed every except clause in
_fetch_one and took out the whole task group. The prose said the protocol was
a boundary; nothing enforced it.

Parametrising over exception families is what keeps this true when the
browser-backed source lands and introduces a second source of failures.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from toc_extractor.fetcher import Fetcher, FetchOptions
from toc_extractor.pagesource import (
    ChapterPage,
    PageBlocked,
    PageError,
    PageTimeout,
    SelectorNotFound,
    TocPage,
)
from toc_extractor.parser import SelectorSet
from toc_extractor.politeness import RateLimiter, RejectionReason, UrlGuard
from toc_extractor.sinks import NullSink

TOC = "https://example.com/toc"
CH1 = "https://example.com/ch/1"
SELECTORS = SelectorSet.create(link="a", title="h1", content="article")
FIXED_TIME = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def resolver(_host: str) -> Sequence[str]:
    return ["93.184.216.34"]


GUARD = UrlGuard(resolver=resolver)


class PlaywrightShapedError(Exception):
    """Stands in for playwright.sync_api.Error without importing Playwright."""


class FaultyPageSource:
    """Raises a chosen exception from load_chapter, or from load_toc."""

    def __init__(self, error: BaseException, *, fail_toc: bool = False) -> None:
        self._error = error
        self._fail_toc = fail_toc
        self.chapter_calls = 0

    async def load_toc(
        self,
        url: str,
        *,
        link_selector: str,
        capture_html: bool = False,
        screenshot_path: Path | None = None,
    ) -> TocPage:
        if self._fail_toc:
            raise self._error
        return TocPage(requested_url=url, final_url=url, raw_links=(CH1,))

    async def load_chapter(
        self,
        url: str,
        *,
        title_selector: str,
        content_selector: str,
    ) -> ChapterPage:
        self.chapter_calls += 1
        raise self._error

    async def aclose(self) -> None:
        return None


def build(source: FaultyPageSource, *, retries: int = 0) -> Fetcher:
    return Fetcher(
        source,
        guard=GUARD,
        sink=NullSink(),
        options=FetchOptions(retries=retries, min_delay=0.0, wait_after_load=0.0),
        limiter=RateLimiter(min_interval=0.0),
        now=lambda: FIXED_TIME,
    )


ESCAPING_FAILURES = [
    pytest.param(TimeoutError("stdlib timeout"), id="stdlib-TimeoutError"),
    pytest.param(OSError("connection reset"), id="OSError"),
    pytest.param(ConnectionResetError("peer hung up"), id="ConnectionResetError"),
    pytest.param(ValueError("bad value"), id="ValueError"),
    pytest.param(RuntimeError("event loop is closed"), id="RuntimeError"),
    pytest.param(Exception("bare"), id="bare-Exception"),
    pytest.param(PlaywrightShapedError("Target page closed"), id="playwright-shaped"),
    pytest.param(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid"), id="UnicodeDecodeError"),
    pytest.param(KeyError("missing"), id="KeyError"),
    pytest.param(AttributeError("no attribute href"), id="AttributeError"),
]


@pytest.mark.parametrize("error", ESCAPING_FAILURES)
async def test_no_foreign_exception_escapes_the_fetch_loop(error: BaseException) -> None:
    source = FaultyPageSource(error)
    result = await build(source).run(TOC, SELECTORS)

    assert result.completed == ()
    assert len(result.failed) == 1
    assert result.accounts_for_every_link()


@pytest.mark.parametrize("error", ESCAPING_FAILURES)
async def test_foreign_exceptions_from_the_toc_surface_as_page_errors(
    error: BaseException,
) -> None:
    """run() should still raise here - there is no chapter to record it against."""
    source = FaultyPageSource(error, fail_toc=True)
    with pytest.raises(PageError):
        await build(source).run(TOC, SELECTORS)


@pytest.mark.parametrize("error", ESCAPING_FAILURES)
async def test_foreign_exceptions_are_retried_like_page_errors(error: BaseException) -> None:
    """Translation must not also silently disable the retry budget."""
    source = FaultyPageSource(error)
    await build(source, retries=2).run(TOC, SELECTORS)
    assert source.chapter_calls == 3


async def test_stdlib_timeout_is_translated_to_page_timeout() -> None:
    source = FaultyPageSource(TimeoutError("stdlib"))
    result = await build(source).run(TOC, SELECTORS)
    assert result.failed[0].reason == "timeout"


@pytest.mark.parametrize(
    "error,expected_reason",
    [
        (PageTimeout("slow"), "timeout"),
        (SelectorNotFound("nothing matched"), "selector_not_found"),
        (PageBlocked("http://10.0.0.1/x", RejectionReason.PRIVATE_ADDRESS), "private_address"),
        (PageError("generic"), "error"),
    ],
)
async def test_native_page_errors_keep_their_identity(
    error: PageError, expected_reason: str
) -> None:
    """Translation must not flatten the vocabulary the retry rules depend on."""
    source = FaultyPageSource(error)
    result = await build(source).run(TOC, SELECTORS)
    assert result.failed[0].reason == expected_reason


async def test_source_raising_cancelled_error_is_recorded_not_absorbed() -> None:
    """TaskGroup absorbs a child's CancelledError, which loses the chapter.

    Measured: kept=1, completed=0, failed=0, and accounts_for_every_link()
    returning False. A spurious CancelledError from a page source must become
    an ordinary recorded failure, or a chapter disappears with no trace.
    """
    source = FaultyPageSource(asyncio.CancelledError())
    result = await build(source).run(TOC, SELECTORS)

    assert result.failed[0].reason == "error"
    assert "CancelledError" in result.failed[0].detail
    assert result.accounts_for_every_link()


async def test_real_cancellation_still_propagates() -> None:
    """Cooperative cancellation must not be turned into recorded failures."""

    class SlowSource(FaultyPageSource):
        async def load_chapter(self, url, *, title_selector, content_selector):  # type: ignore[no-untyped-def]
            self.chapter_calls += 1
            await asyncio.sleep(10)
            raise AssertionError("unreachable")

    source = SlowSource(RuntimeError("unused"))
    task = asyncio.create_task(build(source).run(TOC, SELECTORS))
    await asyncio.sleep(0.02)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


class _FakeAbort(BaseException):
    """A BaseException that is not CancelledError and not KeyboardInterrupt.

    Stands in for the KeyboardInterrupt case without aborting the pytest
    session, which is what a literal KeyboardInterrupt test does.
    """


async def test_base_exception_is_not_swallowed() -> None:
    source = FaultyPageSource(_FakeAbort("stop everything"))
    with pytest.raises(BaseExceptionGroup) as caught:
        await build(source).run(TOC, SELECTORS)

    flattened = list(_flatten(caught.value))
    assert any(isinstance(exc, _FakeAbort) for exc in flattened)


def _flatten(group: BaseExceptionGroup) -> list[BaseException]:  # type: ignore[type-arg]
    out: list[BaseException] = []
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            out.extend(_flatten(exc))
        else:
            out.append(exc)
    return out


class HangingSource(FaultyPageSource):
    """Sleeps past the deadline so asyncio.timeout is the one doing the cancel."""

    def __init__(self, hang: float) -> None:
        super().__init__(RuntimeError("unused"))
        self._hang = hang
        self.cancelling_seen: list[int] = []

    async def load_chapter(  # type: ignore[override]
        self,
        url: str,
        *,
        title_selector: str,
        content_selector: str,
    ) -> ChapterPage:
        self.chapter_calls += 1
        try:
            await asyncio.sleep(self._hang)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            self.cancelling_seen.append(task.cancelling() if task is not None else -1)
            raise
        raise AssertionError("unreachable")


async def test_timeout_driven_cancellation_is_not_mistaken_for_a_stop() -> None:
    """asyncio.timeout cancels the task, so cancelling() is non-zero mid-flight.

    If _fetch_one's discriminator saw that window it would re-raise a timeout
    as a real stop and abort the whole run. It does not, because
    asyncio.timeout's __aexit__ converts to TimeoutError and uncancels before
    the CancelledError can reach _fetch_one's handler. This test pins that
    ordering, since it is the only reason cancelling() alone is a sufficient
    discriminator.
    """
    source = HangingSource(hang=10.0)
    fetcher = Fetcher(
        source,
        guard=GUARD,
        sink=NullSink(),
        options=FetchOptions(timeout=0.05, retries=0, min_delay=0.0, wait_after_load=0.0),
        limiter=RateLimiter(min_interval=0.0),
        now=lambda: FIXED_TIME,
    )
    result = await fetcher.run(TOC, SELECTORS)

    assert source.cancelling_seen == [1], "the timeout should cancel exactly once"
    assert result.failed[0].reason == "timeout"
    assert result.accounts_for_every_link()


async def test_timeout_then_retry_still_works_after_uncancel() -> None:
    """A retried timeout must not leave the task in a cancelling state."""
    source = HangingSource(hang=10.0)
    fetcher = Fetcher(
        source,
        guard=GUARD,
        sink=NullSink(),
        options=FetchOptions(timeout=0.02, retries=2, min_delay=0.0, wait_after_load=0.0),
        limiter=RateLimiter(min_interval=0.0),
        now=lambda: FIXED_TIME,
    )
    result = await fetcher.run(TOC, SELECTORS)

    assert source.chapter_calls == 3, "retries must survive the uncancel dance"
    assert result.failed[0].reason == "timeout"
