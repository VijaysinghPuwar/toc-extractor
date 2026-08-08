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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlparse

from .models import ChapterRecord, FailedChapter, RunResult
from .pagesource import (
    ChapterPage,
    PageBlocked,
    PageError,
    PageSource,
    PageTimeout,
    SelectorNotFound,
    TocPage,
)
from .parser import LinkCollection, RobotsDecision, SelectorSet, collect_links
from .politeness import RateLimiter, RobotsPolicy, UrlGuard
from .sinks import Sink
from .text import clean_text

_T = TypeVar("_T")

Sleeper = Callable[[float], Awaitable[None]]


async def _sleep_seconds(seconds: float) -> None:
    await asyncio.sleep(seconds)


async def _only_page_errors(awaitable: Awaitable[_T], *, context: str) -> _T:
    """Guarantee that a PageSource call raises only PageError or CancelledError.

    The protocol is meant to be the boundary where implementation-specific
    failures stop, but nothing enforced it: asyncio's own TimeoutError escaped
    on the first pass and took out the whole task group instead of being
    retried. The browser-backed source adds a second family of exceptions, so
    the guarantee is made structural here and asserted by a fault-injection
    test rather than left to each implementation's discipline.

    CancelledError derives from BaseException and so passes through untouched,
    which is what keeps cooperative cancellation working.
    """
    try:
        return await awaitable
    except PageError:
        raise
    except TimeoutError as exc:
        raise PageTimeout(f"{context}: timed out") from exc
    except Exception as exc:
        raise PageError(f"{context}: {type(exc).__name__}: {exc}") from exc


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


@dataclass(frozen=True, slots=True)
class CollectedLinks:
    """The TOC page and the verdict on every link it offered."""

    toc_url: str
    toc: TocPage
    collection: LinkCollection
    decisions: tuple[RobotsDecision | None, ...]

    @property
    def kept(self) -> list[str]:
        return list(self.collection.kept)


class _Progress:
    """Mutable run state.

    Every mutation goes through one synchronous method. That is what makes the
    no-lock argument durable rather than a comment: a coroutine step with no
    await between read and write cannot interleave, and adding an await inside
    a `def` is a visible change rather than a silent one.
    """

    def __init__(self) -> None:
        self._completed: dict[int, ChapterRecord] = {}
        self._failed: dict[int, FailedChapter] = {}

    def record_success(self, record: ChapterRecord) -> None:
        self._completed[record.index] = record

    def record_failure(self, failure: FailedChapter) -> None:
        self._failed[failure.index] = failure

    def ordered_completed(self) -> tuple[ChapterRecord, ...]:
        return tuple(self._completed[index] for index in sorted(self._completed))

    def ordered_failed(self) -> tuple[FailedChapter, ...]:
        return tuple(self._failed[index] for index in sorted(self._failed))


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

    def set_sink(self, sink: Sink) -> None:
        """Swap the sink before fetch().

        The text exporter needs to know which chapters an earlier run already
        wrote, and that is only known after the checkpoint has been consulted -
        which in turn needs the link set, which needs collect(). So the sink is
        chosen between collect() and fetch() rather than at construction.
        """
        self._sink = sink

    async def collect(self, toc_url: str, selectors: SelectorSet) -> CollectedLinks:
        """Load the TOC and vet its links. No chapter is fetched here.

        Split out of run() because resume has to be planned against the link
        set the TOC actually offers today, and that is only known after this
        step. A caller that wants to consult a checkpoint calls collect(),
        decides, then calls fetch().
        """
        options = self._options
        toc = await _only_page_errors(
            self._source.load_toc(
                toc_url,
                link_selector=selectors.link,
                capture_html=options.capture_html,
                screenshot_path=options.screenshot_path,
            ),
            context=toc_url,
        )
        collection, decisions = collect_links(
            toc.raw_links,
            guard=self._guard,
            robots=self._robots,
            session_authenticated=options.session_authenticated,
            max_links=options.max_links,
        )
        return CollectedLinks(
            toc_url=toc_url,
            toc=toc,
            collection=collection,
            decisions=tuple(decisions),
        )

    async def fetch(
        self,
        collected: CollectedLinks,
        selectors: SelectorSet,
        *,
        already_done: Callable[[str], bool] | None = None,
    ) -> RunResult:
        """Fetch everything in `collected` that is not already done."""
        options = self._options
        collection = collected.collection
        decisions = collected.decisions
        is_done = already_done if already_done is not None else self._already_done

        pending: list[tuple[int, str, RobotsDecision | None]] = []
        skipped: list[str] = []
        for position, url in enumerate(collection.kept, start=1):
            decision = decisions[position - 1] if position - 1 < len(decisions) else None
            if is_done(url):
                skipped.append(url)
                continue
            pending.append((position, url, decision))

        progress = _Progress()
        await self._sink.open()

        semaphore = asyncio.Semaphore(max(1, options.concurrency))
        async with asyncio.TaskGroup() as group:
            for index, url, decision in pending:
                group.create_task(
                    self._fetch_one(index, url, decision, selectors, semaphore, progress)
                )

        result = RunResult(
            toc_url=collected.toc_url,
            collection=collection,
            completed=progress.ordered_completed(),
            failed=progress.ordered_failed(),
            skipped_resumed=tuple(skipped),
        )
        if not result.accounts_for_every_link():
            # Backstop for the same class of bug as LinkCollection's assertion:
            # a kept link that produced neither a record nor a failure has been
            # lost, and a silent loss is the one outcome this pipeline refuses.
            raise AssertionError(
                f"run accounting lost links: kept={len(collection.kept)} "
                f"completed={len(result.completed)} failed={len(result.failed)} "
                f"skipped={len(result.skipped_resumed)}"
            )
        await self._sink.close(result)
        return result

    async def run(self, toc_url: str, selectors: SelectorSet) -> RunResult:
        collected = await self.collect(toc_url, selectors)
        if self._options.dry_run:
            # Deliberately before any sink.open(): a dry run must not create
            # the output directory, let alone write to it.
            return RunResult(toc_url=toc_url, collection=collected.collection)
        return await self.fetch(collected, selectors)

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
                except asyncio.CancelledError:
                    # Distinguish a real cancellation from a source that raised
                    # CancelledError of its own accord. TaskGroup treats a child
                    # raising CancelledError as *cancelled* rather than failed and
                    # absorbs it silently, so without this the chapter leaves no
                    # record at all: kept=1, completed=0, failed=0. cancelling()
                    # is non-zero only when someone actually asked us to stop.
                    task = asyncio.current_task()
                    if task is not None and task.cancelling() > 0:
                        raise
                    self._record_failure(
                        progress,
                        index,
                        url,
                        PageError(f"{url}: source raised CancelledError"),
                        attempt,
                    )
                    return
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
                page = await _only_page_errors(
                    self._source.load_chapter(
                        url,
                        title_selector=selectors.title,
                        content_selector=selectors.content,
                    ),
                    context=url,
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
        progress.record_success(record)
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
        progress.record_failure(failure)
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
