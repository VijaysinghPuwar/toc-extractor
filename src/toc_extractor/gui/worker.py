"""Runs the asyncio fetch loop on a thread of its own. No Tk import.

The whole GUI threading model is here, so it can be tested without a display.

One asyncio loop, one thread, owned for the lifetime of a session. The browser
context is created on that loop and every later call is scheduled onto it with
run_coroutine_threadsafe: Playwright objects are bound to the loop that made
them, and the human gate means the browser must stay alive across a pause of
arbitrary length while somebody logs in.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import threading
from collections.abc import Callable
from concurrent.futures import Future
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from ..browser import BrowserPageSource
from ..checkpoint import Checkpoint, fingerprint, plan_resume
from ..exporters import build_sink, text_exporter_of
from ..fetcher import Fetcher, FetchOptions
from ..models import ChapterRecord, FailedChapter, PriorChapter
from ..pagesource import PageError
from ..parser import SelectorSet
from ..politeness import RateLimiter, RobotsPolicy, build_url_guard, fetch_robots, origin_of
from .bridge import (
    ChapterState,
    ChapterUpdate,
    Phase,
    RobotsOverride,
    UiBridge,
    failure_to_update,
    record_to_update,
)


@dataclass(frozen=True, slots=True)
class RunRequest:
    """Everything the user chose, captured before the run starts."""

    toc_url: str
    selectors: SelectorSet
    output_dir: Path
    formats: tuple[str, ...]
    options: FetchOptions
    allow_private_hosts: bool = False
    force: bool = False
    profile_dir: Path | None = None


class AsyncLoopThread:
    """An asyncio loop living on its own thread."""

    def __init__(self) -> None:
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="toc-extractor-loop", daemon=True)
        self._ready = threading.Event()

    def _run(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._loop.call_soon(self._ready.set)
        self._loop.run_forever()

    def start(self) -> None:
        self._thread.start()
        self._ready.wait(timeout=5)

    def submit(self, coro: Any) -> Future[Any]:
        return asyncio.run_coroutine_threadsafe(coro, self._loop)

    def stop(self, timeout: float = 5.0) -> None:
        """Cancel outstanding work and let it unwind before killing the loop.

        Stopping the loop outright is not enough. A cancelled task raises
        CancelledError into its own except clause, and that clause is what
        saves the checkpoint and reports the outcome - tearing the loop down
        first leaves the run stuck mid-stop with nothing published.
        """
        if self._thread.is_alive():
            with contextlib.suppress(Exception):
                asyncio.run_coroutine_threadsafe(_cancel_pending(timeout), self._loop).result(
                    timeout=timeout + 1
                )
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
        if not self._loop.is_closed():
            self._loop.close()

    @property
    def running(self) -> bool:
        return self._thread.is_alive()


async def _cancel_pending(timeout: float) -> None:
    tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.wait(tasks, timeout=timeout)


class ExtractionWorker:
    """Drives one GUI session: launch, wait for the human, extract, stop.

    Publishes to a UiBridge and touches nothing else. It holds no widget and
    has no way to acquire one, which is the structural answer to v1 calling Tk
    from a worker thread.
    """

    def __init__(
        self,
        bridge: UiBridge,
        *,
        loop: AsyncLoopThread | None = None,
        source_factory: Callable[..., Any] | None = None,
        robots_fetcher: Callable[[str], str | None] | None = None,
    ) -> None:
        self._bridge = bridge
        self._loop = loop or AsyncLoopThread()
        self._owns_loop = loop is None
        self._source_factory = source_factory
        self._robots_fetcher = robots_fetcher

        self._source: Any = None
        self._robots: RobotsPolicy | None = None
        self._request: RunRequest | None = None
        self._task: Future[Any] | None = None
        self._cancelled = threading.Event()
        # Set only by confirm(). The extract path waits on it, so no code path
        # can start fetching without a human action having occurred.
        self._confirmed = threading.Event()

    # -- lifecycle ----------------------------------------------------------

    def launch(self, request: RunRequest) -> Future[Any]:
        """Open the browser at the TOC and stop, so the user can act."""
        self._request = request
        self._cancelled.clear()
        self._confirmed.clear()
        if self._owns_loop and not self._loop.running:
            self._loop.start()
        self._bridge.phase(Phase.LAUNCHING)
        return self._loop.submit(self._launch(request))

    def confirm(self) -> None:
        """The human gate. Only a user action reaches this."""
        self._confirmed.set()
        self._bridge.phase(Phase.CONFIRMED)
        self._bridge.log("Ready acknowledged. Extraction can start.")

    def extract(self) -> Future[Any]:
        if self._request is None:
            raise RuntimeError("launch() must run before extract()")
        if not self._confirmed.is_set():
            raise RuntimeError("extract() before the human gate was confirmed")
        self._bridge.phase(Phase.EXTRACTING)
        task = self._loop.submit(self._extract(self._request))
        self._task = task
        return task

    def stop(self) -> None:
        """Cooperative cancellation, the same path the fetch loop already has."""
        self._cancelled.set()
        self._bridge.phase(Phase.STOPPING)
        self._bridge.log("Stopping after the chapters already in flight.", level="warning")
        if self._task is not None:
            self._task.cancel()

    def shutdown(self) -> None:
        if self._source is not None:
            # Teardown must not raise over whatever caused the shutdown.
            with contextlib.suppress(Exception):
                self._loop.submit(self._source.aclose()).result(timeout=15)
            self._source = None
        if self._owns_loop and self._loop.running:
            self._loop.stop()

    # -- coroutines, all on the worker loop ---------------------------------

    async def _launch(self, request: RunRequest) -> None:
        try:
            guard = build_url_guard(allow_private_hosts=request.allow_private_hosts)
            verdict = guard.check(request.toc_url)
            if not verdict.allowed:
                raise PageError(f"refusing to open {request.toc_url}: {verdict.reason}")

            self._robots = fetch_robots(request.toc_url, fetcher=self._robots_fetcher)

            if self._source_factory is not None:
                self._source = self._source_factory()
            else:
                self._source = BrowserPageSource(
                    guard=guard,
                    headless=False,
                    user_data_dir=request.profile_dir,
                    navigation_timeout_ms=int(request.options.timeout * 1000),
                    max_pages=request.options.concurrency,
                )
                await self._source.start()

            # Actually navigate. The log used to claim the page was open
            # while the browser sat on a blank tab, which left the user nothing
            # to sign in to and made the human gate impossible to satisfy.
            final_url = await self._source.open_page(request.toc_url)
            self._bridge.log(f"Opened {final_url}.")
            self._bridge.log(
                "Sign in or solve any challenge in the browser window, then press I'm Ready."
            )
            self._bridge.phase(Phase.BROWSING)
        except Exception as exc:
            self._bridge.log(str(exc), level="error")
            self._bridge.finished(error=str(exc))

    async def _extract(self, request: RunRequest) -> None:
        try:
            await self._run_extraction(request)
        except asyncio.CancelledError:
            # Chapters already written stayed written and the checkpoint was
            # saved after each one, so a resume picks up exactly here.
            self._bridge.log("Stopped. Rerun to resume from where this left off.", "warning")
            self._bridge.finished(cancelled=True)
        except Exception as exc:
            self._bridge.log(str(exc), level="error")
            self._bridge.finished(error=str(exc))

    async def _run_extraction(self, request: RunRequest) -> None:
        guard = build_url_guard(allow_private_hosts=request.allow_private_hosts)
        # The post-gate downgrade is keyed on evidence of a session, not on
        # the confirm button. Pressing Ready without signing in is not an
        # authenticated session, and treating it as one would make the override
        # routine rather than deliberate - the failure the absent
        # --ignore-robots flag exists to avoid. Without this set at all the
        # downgrade never happened and a site disallowing exactly the paths a
        # login unlocks fetched nothing.
        authenticated = await self._source.has_session_cookies()
        options = replace(request.options, session_authenticated=authenticated)
        if authenticated:
            self._bridge.log("Signed-in session detected.")
        else:
            self._bridge.log(
                "No signed-in session; robots.txt rules will be enforced strictly.",
            )
        limiter = RateLimiter(min_interval=options.min_delay)
        if self._robots is not None and self._robots.crawl_delay is not None:
            host = origin_of(request.toc_url).split("//", 1)[-1]
            limiter.set_host_interval(host, self._robots.crawl_delay)

        checkpoint: Checkpoint | None = None
        text_sink: Any = None
        total = 0

        def on_record(record: ChapterRecord) -> None:
            self._bridge.chapter(record_to_update(record))
            if checkpoint is not None:
                path = text_sink.written.get(record.index) if text_sink is not None else None
                name = path.name if path is not None else ""
                digest = _digest(path)
                checkpoint.record(record, name, digest)
                checkpoint.save()
            self._bridge.progress(len(checkpoint.completed) if checkpoint else 0, total)

        def on_failure(failure: FailedChapter) -> None:
            self._bridge.chapter(failure_to_update(failure))

        fetcher = Fetcher(
            self._source,
            guard=guard,
            sink=build_sink([], request.output_dir),
            options=options,
            limiter=limiter,
            robots=self._robots,
            on_record=on_record,
            on_failure=on_failure,
        )

        collected = await fetcher.collect(request.toc_url, request.selectors)
        total = len(collected.collection.kept)

        for reason, count in sorted(collected.collection.reason_counts().items()):
            self._bridge.log(f"Skipped {count} link(s): {reason}", level="warning")

        # The post-gate robots downgrade. Anonymous runs never reach here with
        # a disallowed link kept; a signed-in session does, and every one of
        # them is announced with the rule that was overridden.
        for decision in collected.decisions:
            if decision is not None and decision.authenticated_override:
                self._bridge.publish(
                    RobotsOverride(rule_description=decision.rule_description or "")
                )

        for position, url in enumerate(collected.collection.kept, start=1):
            self._bridge.chapter(ChapterUpdate(index=position, url=url, state=ChapterState.PENDING))

        plan = plan_resume(
            request.output_dir,
            toc_url=request.toc_url,
            selectors=request.selectors,
            current_links=collected.kept,
            force=request.force,
        )
        already_done = None
        if plan is not None:
            if not plan.usable:
                raise PageError(plan.refusal)
            checkpoint = plan.checkpoint
            already_done = checkpoint.is_done
            self._bridge.log(f"Resuming: {plan.already_done} chapter(s) already fetched.")
            if plan.renumbering:
                self._bridge.log(
                    "New chapters were added to the start of the table of contents; "
                    "file numbering now reflects fetch order, not table-of-contents "
                    "order.",
                    level="warning",
                )

        if checkpoint is None:
            checkpoint = Checkpoint(
                path=Checkpoint.path_for(request.output_dir),
                toc_url=request.toc_url,
                fingerprint=fingerprint(request.toc_url, request.selectors),
                selectors={
                    "link": request.selectors.link,
                    "title": request.selectors.title,
                    "content": request.selectors.content,
                },
                link_set=collected.kept,
            )
        else:
            checkpoint.link_set = collected.kept
            for url in collected.collection.kept:
                if checkpoint.is_done(url):
                    entry = checkpoint.completed[url]
                    self._bridge.chapter(
                        ChapterUpdate(
                            index=entry.index,
                            url=url,
                            state=ChapterState.SKIPPED,
                            title=entry.title,
                        )
                    )

        sink = build_sink(
            list(request.formats) or [],
            request.output_dir,
            include_links=options.include_links,
            resumed={
                entry.index: PriorChapter(
                    index=entry.index,
                    url=entry.url,
                    output_name=entry.output_name,
                    title=entry.title,
                    bytes=entry.bytes,
                    sha256=entry.sha256,
                    stripped_urls=entry.stripped_urls,
                    fetched_at=entry.fetched_at,
                    output_sha256=entry.output_sha256,
                )
                for entry in checkpoint.completed.values()
            },
        )
        text_sink = text_exporter_of(sink)
        fetcher.set_sink(sink)

        self._bridge.progress(len(checkpoint.completed), total)
        result = await fetcher.fetch(collected, request.selectors, already_done=already_done)
        checkpoint.save()

        self._bridge.log(f"Wrote {len(result.completed)} chapter(s) to {request.output_dir}.")
        if result.total_stripped_urls:
            self._bridge.log(f"Removed {result.total_stripped_urls} URL(s) from chapter text.")
        self._bridge.finished(result=result)


def _digest(path: Path | None) -> str:
    if path is None or not path.exists():
        return ""
    return hashlib.sha256(path.read_bytes()).hexdigest()
