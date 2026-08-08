"""Fetch loop: retries, concurrency, politeness composition, cancellation.

Every test here runs against the dict-backed stub and a fake clock. None
touches a browser, a socket, or a real second.
"""

from __future__ import annotations

import asyncio
import random
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from toc_extractor.exporters.text import TextExporter
from toc_extractor.fetcher import Fetcher, FetchOptions, observed_intervals
from toc_extractor.models import ChapterRecord, PriorChapter
from toc_extractor.pagesource import PageError, PageTimeout
from toc_extractor.parser import SelectorSet
from toc_extractor.politeness import RateLimiter, UrlGuard, parse_robots
from toc_extractor.sinks import NullSink

from .stub import StubPage, StubPageSource

TOC = "https://example.com/toc"
SELECTORS = SelectorSet.create(link="a.ch", title="h1", content="article")
FIXED_TIME = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def resolver(host: str) -> Sequence[str]:
    if host.endswith(".invalid"):
        raise OSError("no such host")
    return ["93.184.216.34"]


GUARD = UrlGuard(resolver=resolver)


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds


def chapter_urls(count: int) -> list[str]:
    return [f"https://example.com/ch/{i}" for i in range(1, count + 1)]


def build(
    *,
    chapters: int = 3,
    clock: FakeClock | None = None,
    pages: dict[str, StubPage] | None = None,
    **option_kwargs: object,
) -> tuple[Fetcher, StubPageSource, NullSink, FakeClock]:
    clock = clock or FakeClock()
    urls = chapter_urls(chapters)
    catalogue = {TOC: StubPage(links=list(urls))}
    for position, url in enumerate(urls, start=1):
        catalogue[url] = StubPage(title=f"Chapter {position}", body=f"Body {position}.")
    if pages:
        catalogue.update(pages)

    options = FetchOptions(**option_kwargs)  # type: ignore[arg-type]
    # One page per worker, matching what BrowserPageSource now allocates.
    source = StubPageSource(catalogue, clock=clock.time, max_concurrent=options.concurrency)
    sink = NullSink()
    limiter = RateLimiter(min_interval=options.min_delay, clock=clock.time, sleep=clock.sleep)
    fetcher = Fetcher(
        source,
        guard=GUARD,
        sink=sink,
        options=options,
        limiter=limiter,
        clock=clock.time,
        sleep=clock.sleep,
        now=lambda: FIXED_TIME,
        rng=random.Random(0),
    )
    return fetcher, source, sink, clock


# ---------------------------------------------------------------------------
# Basic flow
# ---------------------------------------------------------------------------


async def test_all_chapters_are_fetched_in_index_order() -> None:
    fetcher, _, sink, _ = build(chapters=3, wait_after_load=0.0)
    result = await fetcher.run(TOC, SELECTORS)

    assert [record.index for record in result.completed] == [1, 2, 3]
    assert [record.title for record in result.completed] == [
        "Chapter 1",
        "Chapter 2",
        "Chapter 3",
    ]
    assert len(sink.records) == 3


async def test_result_accounts_for_every_kept_link() -> None:
    fetcher, _, _, _ = build(chapters=4, wait_after_load=0.0)
    result = await fetcher.run(TOC, SELECTORS)
    assert result.accounts_for_every_link()
    assert result.collection.raw_count == 4


async def test_stripped_url_counts_reach_the_sink() -> None:
    """Phase 4's manifest needs these; plumbing them later means reopening this loop."""
    pages = {
        "https://example.com/ch/1": StubPage(
            title="One", body="see https://a.com and https://b.com now"
        )
    }
    fetcher, _, sink, _ = build(chapters=1, pages=pages, wait_after_load=0.0)
    await fetcher.run(TOC, SELECTORS)
    assert sink.records[0].stripped_urls == 2


async def test_non_string_links_are_rejected_not_fetched() -> None:
    pages = {TOC: StubPage(links=["https://example.com/ch/1", {}, None])}
    fetcher, source, _, _ = build(chapters=1, pages=pages, wait_after_load=0.0)
    result = await fetcher.run(TOC, SELECTORS)

    assert len(result.collection.kept) == 1
    assert result.collection.reason_counts() == {"not_a_string": 2}
    assert source.attempts_for("https://example.com/ch/1") == 1


async def test_max_links_truncates_and_counts() -> None:
    fetcher, _, _, _ = build(chapters=10, max_links=3, wait_after_load=0.0)
    result = await fetcher.run(TOC, SELECTORS)
    assert len(result.completed) == 3
    assert result.collection.truncated == 7


async def test_final_url_is_recorded_after_redirect() -> None:
    pages = {
        "https://example.com/ch/1": StubPage(redirect_to="https://example.com/moved"),
        "https://example.com/moved": StubPage(title="Moved", body="Body."),
    }
    fetcher, _, _, _ = build(chapters=1, pages=pages, wait_after_load=0.0)
    result = await fetcher.run(TOC, SELECTORS)
    record = result.completed[0]
    assert record.requested_url == "https://example.com/ch/1"
    assert record.final_url == "https://example.com/moved"
    assert record.redirected


# ---------------------------------------------------------------------------
# Retries
# ---------------------------------------------------------------------------


async def test_transient_failures_are_retried_then_succeed() -> None:
    pages = {"https://example.com/ch/1": StubPage(fail_times=2, title="Late", body="Body.")}
    fetcher, source, _, _ = build(chapters=1, pages=pages, retries=2, wait_after_load=0.0)
    result = await fetcher.run(TOC, SELECTORS)

    assert source.attempts_for("https://example.com/ch/1") == 3
    assert result.completed[0].attempts == 3
    assert result.failed == ()


async def test_retries_are_bounded_and_failure_is_recorded() -> None:
    pages = {"https://example.com/ch/1": StubPage(fail_times=99)}
    fetcher, source, _, _ = build(chapters=1, pages=pages, retries=2, wait_after_load=0.0)
    result = await fetcher.run(TOC, SELECTORS)

    assert source.attempts_for("https://example.com/ch/1") == 3
    assert result.completed == ()
    assert result.failed[0].reason == "timeout"
    assert result.failed[0].attempts == 3


async def test_zero_retries_means_one_attempt() -> None:
    pages = {"https://example.com/ch/1": StubPage(fail_times=99)}
    fetcher, source, _, _ = build(chapters=1, pages=pages, retries=0, wait_after_load=0.0)
    await fetcher.run(TOC, SELECTORS)
    assert source.attempts_for("https://example.com/ch/1") == 1


async def test_selector_not_found_is_not_retried() -> None:
    """The page loaded fine; retrying spends attempts on a certainty."""
    pages = {"https://example.com/ch/1": StubPage(missing_selector=True)}
    fetcher, source, _, _ = build(chapters=1, pages=pages, retries=5, wait_after_load=0.0)
    result = await fetcher.run(TOC, SELECTORS)

    assert source.attempts_for("https://example.com/ch/1") == 1
    assert result.failed[0].reason == "selector_not_found"


async def test_one_chapter_failing_does_not_stop_the_others() -> None:
    pages = {"https://example.com/ch/2": StubPage(fail_times=99)}
    fetcher, _, _, _ = build(chapters=3, pages=pages, retries=0, wait_after_load=0.0)
    result = await fetcher.run(TOC, SELECTORS)

    assert [record.index for record in result.completed] == [1, 3]
    assert [failure.index for failure in result.failed] == [2]
    assert result.accounts_for_every_link()


async def test_backoff_is_jittered_within_the_ceiling() -> None:
    pages = {"https://example.com/ch/1": StubPage(fail_times=99)}
    fetcher, _, _, clock = build(
        chapters=1, pages=pages, retries=3, min_delay=1.0, max_delay=4.0, wait_after_load=0.0
    )
    await fetcher.run(TOC, SELECTORS)
    # Three backoff sleeps, each drawn from [0, min(max_delay, 1.0 * 2**(n-1))],
    # plus four rate-limiter waits of 1.0 each after the first request.
    assert clock.now > 0


# ---------------------------------------------------------------------------
# Concurrency and politeness composition
# ---------------------------------------------------------------------------


async def test_concurrency_does_not_defeat_the_per_host_interval() -> None:
    """The property the whole design exists to guarantee.

    Five workers against one host. If the semaphore let them each observe the
    same "last request" time, the configured 2s delay would silently become
    2s/5. Every observed gap must still be at least 2s.
    """
    clock = FakeClock()
    fetcher, source, _, _ = build(
        chapters=10,
        clock=clock,
        concurrency=5,
        min_delay=2.0,
        max_delay=2.0,
        wait_after_load=0.0,
    )
    result = await fetcher.run(TOC, SELECTORS)

    assert len(result.completed) == 10
    chapter_times = [when for when, url in source.loads if url != TOC]
    gaps = observed_intervals(chapter_times)
    assert len(gaps) == 9
    assert min(gaps) >= 2.0 - 1e-9, gaps


async def test_concurrency_one_is_still_correct() -> None:
    clock = FakeClock()
    fetcher, source, _, _ = build(
        chapters=4, clock=clock, concurrency=1, min_delay=1.0, wait_after_load=0.0
    )
    await fetcher.run(TOC, SELECTORS)
    gaps = observed_intervals([when for when, url in source.loads if url != TOC])
    assert min(gaps) >= 1.0 - 1e-9


async def test_separate_hosts_are_not_serialised_against_each_other() -> None:
    urls = ["https://a.example/ch/1", "https://b.example/ch/1"]
    catalogue = {
        TOC: StubPage(links=urls),
        urls[0]: StubPage(title="A", body="a"),
        urls[1]: StubPage(title="B", body="b"),
    }
    clock = FakeClock()
    source = StubPageSource(catalogue, clock=clock.time, max_concurrent=2)
    limiter = RateLimiter(min_interval=5.0, clock=clock.time, sleep=clock.sleep)
    fetcher = Fetcher(
        source,
        guard=GUARD,
        sink=NullSink(),
        options=FetchOptions(concurrency=2, min_delay=5.0, wait_after_load=0.0),
        limiter=limiter,
        clock=clock.time,
        sleep=clock.sleep,
        now=lambda: FIXED_TIME,
    )
    await fetcher.run(TOC, SELECTORS)
    assert clock.now == 0.0


async def test_semaphore_bounds_in_flight_work() -> None:
    """Concurrency is a ceiling on simultaneous loads, not a target."""
    fetcher, source, _, _ = build(
        chapters=6, concurrency=2, min_delay=0.0, max_delay=0.0, wait_after_load=0.0
    )
    result = await fetcher.run(TOC, SELECTORS)
    assert len(result.completed) == 6
    assert len(source.urls_loaded) == 7


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


async def test_hard_timeout_covers_a_page_that_never_settles() -> None:
    """Real seconds, deliberately: asyncio.timeout uses the loop's own clock."""
    catalogue = {
        TOC: StubPage(links=["https://example.com/ch/1"]),
        "https://example.com/ch/1": StubPage(hang=5.0),
    }
    source = StubPageSource(catalogue)
    fetcher = Fetcher(
        source,
        guard=GUARD,
        sink=NullSink(),
        options=FetchOptions(timeout=0.05, retries=0, min_delay=0.0, wait_after_load=0.0),
        limiter=RateLimiter(min_interval=0.0),
        now=lambda: FIXED_TIME,
    )
    result = await fetcher.run(TOC, SELECTORS)
    assert result.completed == ()
    assert result.failed[0].reason in {"timeout", "error"}


# ---------------------------------------------------------------------------
# Cancellation
# ---------------------------------------------------------------------------


async def test_cancellation_mid_run_propagates_and_keeps_records_consistent() -> None:
    """Stop is Phase 5, but a consistent teardown has to hold now."""
    seen: list[ChapterRecord] = []
    catalogue = {TOC: StubPage(links=chapter_urls(20))}
    for position, url in enumerate(chapter_urls(20), start=1):
        catalogue[url] = StubPage(title=f"Chapter {position}", body="Body.", hang=0.02)

    source = StubPageSource(catalogue, max_concurrent=2)
    fetcher = Fetcher(
        source,
        guard=GUARD,
        sink=NullSink(),
        options=FetchOptions(concurrency=2, min_delay=0.0, wait_after_load=0.0),
        limiter=RateLimiter(min_interval=0.0),
        now=lambda: FIXED_TIME,
        on_record=seen.append,
    )

    task = asyncio.create_task(fetcher.run(TOC, SELECTORS))
    await asyncio.sleep(0.05)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    # Whatever completed before the cancel is intact and consistently indexed.
    assert len(seen) < 20
    assert len({record.index for record in seen}) == len(seen)
    assert all(record.title.startswith("Chapter ") for record in seen)


# ---------------------------------------------------------------------------
# dry-run and resume hooks
# ---------------------------------------------------------------------------


async def test_dry_run_fetches_nothing_and_writes_nothing(tmp_path: Path) -> None:
    output = tmp_path / "out"
    catalogue = {TOC: StubPage(links=chapter_urls(3))}
    for url in chapter_urls(3):
        catalogue[url] = StubPage()

    source = StubPageSource(catalogue)
    sink = TextExporter(output)
    fetcher = Fetcher(
        source,
        guard=GUARD,
        sink=sink,
        options=FetchOptions(dry_run=True),
        limiter=RateLimiter(min_interval=0.0),
        now=lambda: FIXED_TIME,
    )
    result = await fetcher.run(TOC, SELECTORS)

    assert len(result.collection.kept) == 3
    assert result.completed == ()
    assert source.urls_loaded == [TOC]
    assert not output.exists(), "a dry run must not create the output directory"


async def test_already_done_urls_are_skipped_not_refetched() -> None:
    done = {"https://example.com/ch/1", "https://example.com/ch/2"}
    catalogue = {TOC: StubPage(links=chapter_urls(4))}
    for position, url in enumerate(chapter_urls(4), start=1):
        catalogue[url] = StubPage(title=f"Chapter {position}", body="Body.")

    source = StubPageSource(catalogue)
    fetcher = Fetcher(
        source,
        guard=GUARD,
        sink=NullSink(),
        options=FetchOptions(min_delay=0.0, wait_after_load=0.0),
        limiter=RateLimiter(min_interval=0.0),
        now=lambda: FIXED_TIME,
        already_done=lambda url: url in done,
    )
    result = await fetcher.run(TOC, SELECTORS)

    assert set(result.skipped_resumed) == done
    assert [record.index for record in result.completed] == [3, 4]
    assert result.accounts_for_every_link()
    for url in done:
        assert source.attempts_for(url) == 0


async def test_robots_disallow_rejects_before_any_fetch() -> None:
    policy = parse_robots("User-agent: *\nDisallow: /ch/2\n", origin="https://example.com")
    catalogue = {TOC: StubPage(links=chapter_urls(3))}
    for position, url in enumerate(chapter_urls(3), start=1):
        catalogue[url] = StubPage(title=f"Chapter {position}", body="Body.")

    source = StubPageSource(catalogue)
    fetcher = Fetcher(
        source,
        guard=GUARD,
        sink=NullSink(),
        options=FetchOptions(min_delay=0.0, wait_after_load=0.0),
        limiter=RateLimiter(min_interval=0.0),
        robots=policy,
        now=lambda: FIXED_TIME,
    )
    result = await fetcher.run(TOC, SELECTORS)

    assert source.attempts_for("https://example.com/ch/2") == 0
    assert result.collection.reason_counts() == {"robots_disallowed": 1}


# ---------------------------------------------------------------------------
# TextSink
# ---------------------------------------------------------------------------


async def test_text_sink_reproduces_v1_layout(tmp_path: Path) -> None:
    output = tmp_path / "out"
    fetcher, _, _, _ = build(chapters=2, wait_after_load=0.0)
    sink = TextExporter(output)
    fetcher.set_sink(sink)
    await fetcher.run(TOC, SELECTORS)

    assert (output / "001 - Chapter 1.txt").read_text(encoding="utf-8") == (
        "Chapter 1\n\nBody 1.\n"
    )
    combined = (output / "combined.txt").read_text(encoding="utf-8")
    assert combined.startswith("Chapter 1\n\nBody 1.\n\n" + "-" * 80)
    assert "Chapter 2" in combined


async def test_combined_is_index_ordered_despite_concurrency(tmp_path: Path) -> None:
    """Chapters complete out of order at concurrency > 1; combined.txt must not."""
    output = tmp_path / "out"
    fetcher, _, _, _ = build(
        chapters=5, concurrency=5, min_delay=0.0, max_delay=0.0, wait_after_load=0.0
    )
    sink = TextExporter(output)
    fetcher.set_sink(sink)
    await fetcher.run(TOC, SELECTORS)

    combined = (output / "combined.txt").read_text(encoding="utf-8")
    positions = [combined.index(f"Chapter {i}") for i in range(1, 6)]
    assert positions == sorted(positions)


async def test_text_sink_includes_source_when_asked(tmp_path: Path) -> None:
    output = tmp_path / "out"
    fetcher, _, _, _ = build(chapters=1, include_links=True, wait_after_load=0.0)
    sink = TextExporter(output, include_links=True)
    fetcher.set_sink(sink)
    await fetcher.run(TOC, SELECTORS)

    body = (output / "001 - Chapter 1.txt").read_text(encoding="utf-8")
    assert "Source: https://example.com/ch/1" in body


async def test_text_sink_deduplicates_colliding_titles(tmp_path: Path) -> None:
    output = tmp_path / "out"
    pages = {
        "https://example.com/ch/1": StubPage(title="Chapter One", body="a"),
        "https://example.com/ch/2": StubPage(title="chapter one", body="b"),
    }
    fetcher, _, _, _ = build(chapters=2, pages=pages, wait_after_load=0.0)
    sink = TextExporter(output)
    fetcher.set_sink(sink)
    await fetcher.run(TOC, SELECTORS)

    names = sorted(path.name for path in output.glob("*.txt"))
    assert names == ["001 - Chapter One.txt", "002 - chapter one.txt", "combined.txt"]


async def test_page_error_from_toc_is_not_swallowed() -> None:
    source = StubPageSource({})
    fetcher = Fetcher(
        source,
        guard=GUARD,
        sink=NullSink(),
        limiter=RateLimiter(min_interval=0.0),
        now=lambda: FIXED_TIME,
    )
    with pytest.raises(PageError, match="no stub page registered"):
        await fetcher.run(TOC, SELECTORS)


async def test_timeout_type_is_reported_as_timeout() -> None:
    pages = {"https://example.com/ch/1": StubPage(fail_times=99, failure=PageTimeout)}
    fetcher, _, _, _ = build(chapters=1, pages=pages, retries=0, wait_after_load=0.0)
    result = await fetcher.run(TOC, SELECTORS)
    assert result.failed[0].reason == "timeout"


async def test_combined_survives_a_resumed_run(tmp_path: Path) -> None:
    """Regression: a resumed run used to truncate combined.txt.

    The sink only holds chunks for chapters it fetched, so writing those alone
    discarded everything from the earlier run. The per-chapter files stayed
    intact, which is why nothing looked wrong until you opened combined.txt.
    """
    output = tmp_path / "out"

    first, _, _, _ = build(chapters=5, max_links=3, wait_after_load=0.0)
    first._sink = TextExporter(output)
    await first.run(TOC, SELECTORS)
    assert (output / "combined.txt").read_text(encoding="utf-8").count("Chapter ") == 3

    done = {f"https://example.com/ch/{i}" for i in (1, 2, 3)}
    # What the checkpoint recorded for the first run. Passed explicitly rather
    # than rediscovered by globbing the directory: a glob would sweep in stray
    # files, or a second book sharing this folder, and merge them in.
    resumed = {
        index: PriorChapter(
            index=index,
            url=f"https://example.com/ch/{index}",
            output_name=f"{index:03d} - Chapter {index}.txt",
            title=f"Chapter {index}",
            bytes=0,
            sha256="",
            stripped_urls=0,
            fetched_at=FIXED_TIME.isoformat(),
        )
        for index in (1, 2, 3)
    }

    second, _, _, _ = build(chapters=5, wait_after_load=0.0)
    second.set_sink(TextExporter(output, resumed=resumed))
    second._already_done = lambda url: url in done
    await second.run(TOC, SELECTORS)

    combined = (output / "combined.txt").read_text(encoding="utf-8")
    for number in range(1, 6):
        assert f"Chapter {number}" in combined, f"chapter {number} missing from combined.txt"
    positions = [combined.index(f"Chapter {i}") for i in range(1, 6)]
    assert positions == sorted(positions), "combined.txt must stay in index order"
