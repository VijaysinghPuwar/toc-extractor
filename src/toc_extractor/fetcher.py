"""The concurrent, polite, resumable fetch loop.

Composes politeness.RateLimiter with bounded concurrency. Those two have to
compose correctly or the tool quietly stops being polite, which is the one
property the rest of this codebase is built to guarantee — so the interval is
enforced inside the limiter's per-host lock rather than by spacing task
starts, and there is a test that runs five workers at one host and asserts the
observed spacing survives.

Imports nothing from Playwright; the PageSource protocol is the only way out.
"""

from __future__ import annotations

import asyncio
import itertools
import random
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlparse

from .models import ChapterRecord, FailedChapter, RunResult
from .pagesource import (
    ChapterPage,
    PageBlocked,
    PageError,
    PageSource,
    PageTimeout,
    SelectorNotFound,
)
from .parser import RobotsDecision, SelectorSet, collect_links
from .politeness import RateLimiter, RobotsPolicy, UrlGuard
from .sinks import Sink
from .text import clean_text

Sleeper = Callable[[float], Awaitable[None]]


async def _sleep_seconds(seconds: float) -> None:
    await asyncio.sleep(seconds)


@dataclass(frozen=True, slots=True)
class FetchOptions:
    """Everything the loop needs that is not a collaborator."""

    concurrency: int = 3
    retries: int = 2
    timeout: float = 25.0
    min_delay: float = 1.2
    max_delay: float = 2.5
    wait_after_load: float = 0.5
    include_links: bool = False
    strip_ads: bool = True
    max_links: int | None = None
    dry_run: bool = False
    capture_html: bool = False
    screenshot_path: Path | None = None
    session_authenticated: bool = False


@dataclass
class _Progress:
    """Mutable run state, guarded by the fact that asyncio is single-threaded.

    No lock: every mutation happens in a coroutine step with no await between
    read and write, so there is no interleaving point.
    """

    completed: dict[int, ChapterRecord] = field(default_factory=dict)
    failed: dict[int, FailedChapter] = field(default_factory=dict)


class Fetcher:
    """Runs one extraction against one PageSource."""

    def __init__(
        self,
        source: PageSource,
        *,
        guard: UrlGuard,
        sink: Sink,
        options: FetchOptions | None = None,
        limiter: RateLimiter | None = None,
        robots: RobotsPolicy | None = None,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleeper | None = None,
        now: Callable[[], datetime] | None = None,
        rng: random.Random | None = None,
        already_done: Callable[[str], bool] | None = None,
        on_record: Callable[[ChapterRecord], None] | None = None,
        on_failure: Callable[[FailedChapter], None] | None = None,
    ) -> None:
        self._source = source
        self._guard = guard
        self._sink = sink
        self._options = options or FetchOptions()
        self._robots = robots
        self._clock = clock
        self._sleep: Sleeper = sleep or _sleep_seconds
        self._now = now or (lambda: datetime.now(UTC))
        self._rng = rng or random.Random()
        self._already_done = already_done or (lambda _url: False)
        self._on_record = on_record
        self._on_failure = on_failure
        self._limiter = limiter or RateLimiter(
            min_interval=self._options.min_delay, clock=clock, sleep=self._sleep
        )

    async def run(self, toc_url: str, selectors: SelectorSet) -> RunResult:
        options = self._options

        toc = await self._source.load_toc(
            toc_url,
            link_selector=selectors.link,
            capture_html=options.capture_html,
            screenshot_path=options.screenshot_path,
        )

        collection, decisions = collect_links(
            toc.raw_links,
            guard=self._guard,
            robots=self._robots,
            session_authenticated=options.session_authenticated,
            max_links=options.max_links,
        )

        if options.dry_run:
            # Deliberately before any sink.open(): a dry run must not create
            # the output directory, let alone write to it.
            return RunResult(toc_url=toc_url, collection=collection)

        pending: list[tuple[int, str, RobotsDecision | None]] = []
        skipped: list[str] = []
        for position, url in enumerate(collection.kept, start=1):
            decision = decisions[position - 1] if position - 1 < len(decisions) else None
            if self._already_done(url):
                skipped.append(url)
                continue
            pending.append((position, url, decision))

        progress = _Progress()
        await self._sink.open()

        semaphore = asyncio.Semaphore(max(1, options.concurrency))
        try:
            async with asyncio.TaskGroup() as group:
                for index, url, decision in pending:
                    group.create_task(
                        self._fetch_one(index, url, decision, selectors, semaphore, progress)
                    )
        except* PageError:
            # Per-chapter failures are recorded, not raised; anything reaching
            # here is a bug in _fetch_one's own error handling.
            raise

        result = RunResult(
            toc_url=toc_url,
            collection=collection,
            completed=tuple(progress.completed[i] for i in sorted(progress.completed)),
            failed=tuple(progress.failed[i] for i in sorted(progress.failed)),
            skipped_resumed=tuple(skipped),
        )
        await self._sink.close(result)
        return result

    async def _fetch_one(
        self,
        index: int,
        url: str,
        decision: RobotsDecision | None,
        selectors: SelectorSet,
        semaphore: asyncio.Semaphore,
        progress: _Progress,
    ) -> None:
        options = self._options
        host = urlparse(url).hostname or ""

        async with semaphore:
            attempt = 0
            while True:
                attempt += 1
                # Inside the semaphore, so a worker holding a slot is the one
                # waiting on the host. Acquiring before the semaphore would let
                # more workers than the concurrency limit queue on the limiter.
                await self._limiter.acquire(host)
                try:
                    page = await self._load(url, selectors)
                except (PageBlocked, SelectorNotFound) as exc:
                    # Neither is worth retrying: the target is disallowed, or
                    # the page loaded fine and simply lacks the selector.
                    self._record_failure(progress, index, url, exc, attempt)
                    return
                except PageError as exc:
                    if attempt > options.retries:
                        self._record_failure(progress, index, url, exc, attempt)
                        return
                    await self._sleep(self._backoff(attempt))
                    continue

                await self._record_success(progress, index, url, page, decision, attempt)
                return

    async def _load(self, url: str, selectors: SelectorSet) -> ChapterPage:
        # One wall-clock cap over the whole operation, reusing --timeout rather
        # than adding a second knob. Navigation timeouts alone do not cover a
        # selector evaluation that never settles.
        #
        # asyncio raises its own TimeoutError, which is outside the PageError
        # vocabulary the retry rules are written against. Translating here is
        # what keeps _fetch_one's except clauses exhaustive; without it the
        # error escapes the task group instead of being retried. An external
        # cancellation still arrives as CancelledError and is left alone.
        try:
            async with asyncio.timeout(self._options.timeout):
                page = await self._source.load_chapter(
                    url,
                    title_selector=selectors.title,
                    content_selector=selectors.content,
                )
        except TimeoutError as exc:
            raise PageTimeout(f"{url} did not settle within {self._options.timeout}s") from exc
        if self._options.wait_after_load > 0:
            await self._sleep(self._options.wait_after_load)
        return page

    def _backoff(self, attempt: int) -> float:
        """Exponential backoff with full jitter, capped at max_delay.

        Full jitter rather than a fixed multiplier: retries that all failed at
        the same moment must not resynchronise on the way back in.
        """
        ceiling = min(self._options.max_delay, self._options.min_delay * (2 ** (attempt - 1)))
        return self._rng.uniform(0.0, max(0.0, ceiling))

    async def _record_success(
        self,
        progress: _Progress,
        index: int,
        url: str,
        page: ChapterPage,
        decision: RobotsDecision | None,
        attempts: int,
    ) -> None:
        cleaned = clean_text(
            page.body,
            remove_links=not self._options.include_links,
            strip_ads=self._options.strip_ads,
        )
        record = ChapterRecord(
            index=index,
            requested_url=url,
            final_url=page.final_url,
            title=page.title,
            text=cleaned.text,
            stripped_urls=cleaned.stripped_urls,
            fetched_at=self._now(),
            attempts=attempts,
            robots=decision,
        )
        progress.completed[index] = record
        await self._sink.write(record)
        if self._on_record is not None:
            self._on_record(record)

    def _record_failure(
        self,
        progress: _Progress,
        index: int,
        url: str,
        exc: PageError,
        attempts: int,
    ) -> None:
        failure = FailedChapter(
            index=index,
            url=url,
            reason=_reason_for(exc),
            detail=str(exc),
            attempts=attempts,
        )
        progress.failed[index] = failure
        if self._on_failure is not None:
            self._on_failure(failure)


def _reason_for(exc: PageError) -> str:
    if isinstance(exc, PageTimeout):
        return "timeout"
    if isinstance(exc, PageBlocked):
        return exc.reason.value
    if isinstance(exc, SelectorNotFound):
        return "selector_not_found"
    return "error"


def observed_intervals(times: Sequence[float]) -> list[float]:
    """Gaps between consecutive request times. Used by the composition test."""
    ordered = sorted(times)
    return [later - earlier for earlier, later in itertools.pairwise(ordered)]
