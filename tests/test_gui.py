"""GUI behaviour, tested without a display.

Everything here exercises `gui.bridge` and `gui.worker`, which import no Tk.
That split exists so CI can test the parts that used to be broken: the
lifecycle, the queue protocol, and the worker thread. `gui.app` is a thin
rendering layer over RunState, and a window cannot be opened headless.
"""

from __future__ import annotations

import contextlib
import threading
import time
from collections.abc import Sequence
from pathlib import Path

import pytest

from toc_extractor.checkpoint import Checkpoint
from toc_extractor.fetcher import FetchOptions
from toc_extractor.gui.bridge import (
    ChapterState,
    ChapterUpdate,
    Finished,
    IllegalTransition,
    LogLine,
    Phase,
    PhaseChanged,
    Progress,
    RobotsOverride,
    RunState,
    UiBridge,
    apply,
    transition,
)
from toc_extractor.gui.worker import AsyncLoopThread, ExtractionWorker, RunRequest
from toc_extractor.parser import SelectorSet

from .stub import StubPage, StubPageSource

TOC = "https://example.com/toc"
SELECTORS = SelectorSet.create(link="a.ch", title="h1", content="article")


def catalogue(count: int = 4, **extra: StubPage) -> dict[str, StubPage]:
    urls = [f"https://example.com/ch/{i}" for i in range(1, count + 1)]
    pages = {TOC: StubPage(links=urls)}
    for position, url in enumerate(urls, start=1):
        pages[url] = StubPage(title=f"Chapter {position}", body=f"Body {position}.")
    pages.update(extra)
    return pages


def request_for(tmp_path: Path, **overrides: object) -> RunRequest:
    options = FetchOptions(
        concurrency=int(overrides.pop("concurrency", 2)),  # type: ignore[arg-type]
        retries=0,
        min_delay=0.0,
        max_delay=0.0,
        wait_after_load=0.0,
        max_links=int(overrides.pop("max_links", 100)),  # type: ignore[arg-type]
    )
    return RunRequest(
        toc_url=TOC,
        selectors=SELECTORS,
        output_dir=tmp_path,
        formats=("text", "jsonl"),
        options=options,
        allow_private_hosts=True,
        **overrides,  # type: ignore[arg-type]
    )


def run_session(
    tmp_path: Path,
    *,
    pages: dict[str, StubPage] | None = None,
    stop_after: int | None = None,
    **overrides: object,
) -> tuple[RunState, StubPageSource]:
    """Drive launch -> confirm -> extract the way the buttons do."""
    catalogue_pages = pages or catalogue()
    concurrency = int(overrides.get("concurrency", 2))  # type: ignore[arg-type]
    source = StubPageSource(catalogue_pages, max_concurrent=concurrency)

    bridge = UiBridge()
    loop = AsyncLoopThread()
    loop.start()
    worker = ExtractionWorker(
        bridge,
        loop=loop,
        source_factory=lambda: source,
        robots_fetcher=lambda _url: None,
    )
    try:
        worker.launch(request_for(tmp_path, **overrides)).result(timeout=10)
        worker.confirm()
        future = worker.extract()
        if stop_after is not None:
            _wait_for_chapters(tmp_path, stop_after)
            worker.stop()
        with contextlib.suppress(Exception):
            # Cancelling raises here the instant the future is cancelled, well
            # before the coroutine's handler has run, so the outcome is taken
            # from the queue rather than from this call.
            future.result(timeout=15)
        _wait_for_finish(bridge)
    finally:
        loop.stop()

    state = RunState()
    apply(state, bridge.drain(limit=10_000))
    return state, source


def _wait_for_finish(bridge: UiBridge, timeout: float = 10.0) -> None:
    """Wait for the Finished message, which is what the UI actually reacts to."""
    waiter = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(isinstance(m, Finished) for m in _peek(bridge)):
            return
        waiter.wait(0.01)


def _peek(bridge: UiBridge) -> list[object]:
    taken = bridge.drain(limit=10_000)
    for message in taken:
        bridge.publish(message)  # type: ignore[arg-type]
    return list(taken)


def _wait_for_chapters(output_dir: Path, count: int, timeout: float = 10.0) -> None:
    """Wait until `count` chapters are durably recorded.

    Counting queued messages was wrong: log and phase messages arrive first, so
    the stop fired before a single chapter had been fetched and the test passed
    while proving nothing. The checkpoint is the real observable.
    """
    waiter = threading.Event()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        checkpoint = Checkpoint.load(output_dir)
        if checkpoint is not None and len(checkpoint.completed) >= count:
            return
        waiter.wait(0.01)


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


def test_the_human_gate_is_two_states_not_a_flag() -> None:
    """Nothing may fetch until a human has confirmed, and only they can."""
    phase = transition(Phase.IDLE, Phase.LAUNCHING)
    phase = transition(phase, Phase.BROWSING)
    with pytest.raises(IllegalTransition):
        transition(phase, Phase.EXTRACTING)
    phase = transition(phase, Phase.CONFIRMED)
    assert transition(phase, Phase.EXTRACTING) is Phase.EXTRACTING


@pytest.mark.parametrize(
    "current,target",
    [
        (Phase.IDLE, Phase.EXTRACTING),
        (Phase.BROWSING, Phase.EXTRACTING),
        (Phase.LAUNCHING, Phase.CONFIRMED),
        (Phase.DONE, Phase.EXTRACTING),
        (Phase.STOPPING, Phase.EXTRACTING),
    ],
)
def test_illegal_transitions_are_refused(current: Phase, target: Phase) -> None:
    with pytest.raises(IllegalTransition):
        transition(current, target)


def test_button_availability_follows_the_phase() -> None:
    state = RunState()
    assert state.can_launch and not state.can_confirm and not state.can_extract

    apply(state, [PhaseChanged(Phase.LAUNCHING)])
    assert not state.can_launch

    apply(state, [PhaseChanged(Phase.BROWSING)])
    assert state.can_confirm and not state.can_extract


def test_extract_before_confirm_is_refused(tmp_path: Path) -> None:
    """The gate is enforced in the worker, not only by a disabled button."""
    bridge = UiBridge()
    loop = AsyncLoopThread()
    loop.start()
    worker = ExtractionWorker(bridge, loop=loop, source_factory=lambda: StubPageSource(catalogue()))
    try:
        worker.launch(request_for(tmp_path)).result(timeout=10)
        with pytest.raises(RuntimeError, match="human gate"):
            worker.extract()
    finally:
        loop.stop()


def test_extract_before_launch_is_refused() -> None:
    worker = ExtractionWorker(UiBridge())
    with pytest.raises(RuntimeError, match="launch"):
        worker.extract()


# ---------------------------------------------------------------------------
# The queue
# ---------------------------------------------------------------------------


def test_drain_is_bounded_so_a_burst_cannot_starve_the_ui() -> None:
    """Draining everything on a tick would hold the main thread through a burst."""
    bridge = UiBridge()
    for index in range(500):
        bridge.log(f"line {index}")

    first = bridge.drain(limit=200)
    assert len(first) == 200
    assert bridge.pending() == 300


def test_drain_returns_empty_rather_than_blocking() -> None:
    assert UiBridge().drain() == []


def test_publish_is_safe_from_another_thread() -> None:
    bridge = UiBridge()
    errors: list[BaseException] = []

    def publish() -> None:
        try:
            for index in range(100):
                bridge.log(f"worker {index}")
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=publish) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert errors == []
    assert bridge.pending() == 400


# ---------------------------------------------------------------------------
# Full sessions
# ---------------------------------------------------------------------------


def test_a_full_session_fetches_and_reports(tmp_path: Path) -> None:
    state, source = run_session(tmp_path)

    assert state.phase is Phase.DONE
    assert state.count(ChapterState.DONE) == 4
    assert state.total == 4
    assert (tmp_path / "combined.txt").exists()
    assert (tmp_path / "manifest.jsonl").exists()
    assert len([url for url in source.urls_loaded if url != TOC]) == 4


def test_per_chapter_rows_reach_the_state(tmp_path: Path) -> None:
    state, _ = run_session(tmp_path)
    rows = state.ordered_rows()
    assert [row.index for row in rows] == [1, 2, 3, 4]
    assert all(row.state is ChapterState.DONE for row in rows)
    assert rows[0].title == "Chapter 1"


def test_a_failing_chapter_is_marked_with_its_reason(tmp_path: Path) -> None:
    pages = catalogue()
    pages["https://example.com/ch/2"] = StubPage(fail_times=99)
    state, _ = run_session(tmp_path, pages=pages)

    failed = [row for row in state.ordered_rows() if row.state is ChapterState.FAILED]
    assert [row.index for row in failed] == [2]
    assert "timeout" in failed[0].detail


def test_rejected_links_are_logged_not_silently_dropped(tmp_path: Path) -> None:
    pages = catalogue(2)
    pages[TOC] = StubPage(
        links=["https://example.com/ch/1", "https://example.com/ch/2", {}, "file:///etc/passwd"]
    )
    state, _ = run_session(tmp_path, pages=pages)

    text = " ".join(line.text for line in state.log)
    assert "not_a_string" in text
    assert "disallowed_scheme" in text


# ---------------------------------------------------------------------------
# Stop and resume
# ---------------------------------------------------------------------------


def test_stopping_leaves_a_resumable_checkpoint(tmp_path: Path) -> None:
    """Stop must not just set a flag: the next run has to pick up cleanly."""
    slow = catalogue(8)
    for url, page in slow.items():
        if url != TOC:
            page.hang = 0.05

    state, _ = run_session(tmp_path, pages=slow, stop_after=2, concurrency=1)
    assert state.phase in {Phase.DONE, Phase.FAILED}

    checkpoint = Checkpoint.load(tmp_path)
    assert checkpoint is not None
    done_first = set(checkpoint.completed)
    assert 0 < len(done_first) < 8, "the run should have stopped partway"

    # A second session resumes rather than refetching.
    second_state, second_source = run_session(tmp_path, pages=catalogue(8))
    refetched = done_first & {url for url in second_source.urls_loaded if url != TOC}

    assert refetched == set(), f"refetched already-complete chapters: {refetched}"
    assert second_state.phase is Phase.DONE

    final = Checkpoint.load(tmp_path)
    assert final is not None
    assert len(final.completed) == 8


def test_resumed_chapters_are_shown_as_already_done(tmp_path: Path) -> None:
    run_session(tmp_path, pages=catalogue(4))
    state, source = run_session(tmp_path, pages=catalogue(4))

    assert state.count(ChapterState.SKIPPED) == 4
    assert [url for url in source.urls_loaded if url != TOC] == []


# ---------------------------------------------------------------------------
# Robots
# ---------------------------------------------------------------------------


def test_robots_disallow_blocks_an_anonymous_run(tmp_path: Path) -> None:
    """Hard refusal: the GUI has no flag that turns this off."""
    bridge = UiBridge()
    loop = AsyncLoopThread()
    loop.start()
    source = StubPageSource(catalogue(3), max_concurrent=2)
    worker = ExtractionWorker(
        bridge,
        loop=loop,
        source_factory=lambda: source,
        robots_fetcher=lambda _url: "User-agent: *\nDisallow: /ch/\n",
    )
    try:
        worker.launch(request_for(tmp_path)).result(timeout=10)
        worker.confirm()
        worker.extract().result(timeout=15)
    finally:
        loop.stop()

    state = RunState()
    apply(state, bridge.drain(limit=10_000))
    assert [url for url in source.urls_loaded if url != TOC] == []
    assert any("robots_disallowed" in line.text for line in state.log)


def test_an_override_is_announced_and_cannot_be_cleared() -> None:
    """The post-gate downgrade is visible and not dismissible into silence."""
    state = RunState()
    rule = '"disallow: /members/" at robots.txt line 7 (User-agent: *)'
    apply(state, [RobotsOverride(rule_description=rule)])

    assert state.robots_overrides == [rule]
    assert any(line.level == "warning" and "line 7" in line.text for line in state.log)

    # Later traffic must not clear it.
    apply(state, [LogLine(text="something else"), Progress(done=1, total=2)])
    assert state.robots_overrides == [rule]


# ---------------------------------------------------------------------------
# Message folding
# ---------------------------------------------------------------------------


def test_progress_fraction_is_bounded() -> None:
    assert Progress(done=0, total=0).fraction == 0.0
    assert Progress(done=3, total=6).fraction == 0.5
    assert Progress(done=9, total=6).fraction == 1.0


def test_chapter_updates_replace_rather_than_duplicate() -> None:
    state = RunState()
    url = "https://example.com/ch/1"
    apply(state, [ChapterUpdate(index=1, url=url, state=ChapterState.PENDING)])
    apply(state, [ChapterUpdate(index=1, url=url, state=ChapterState.DONE, title="One")])

    assert len(state.rows) == 1
    assert state.rows[1].state is ChapterState.DONE
    assert state.rows[1].title == "One"


def test_finished_with_an_error_moves_to_failed() -> None:
    state = RunState()
    apply(state, [PhaseChanged(Phase.LAUNCHING)])
    apply(state, [Finished(result=None, error="boom")])
    assert state.phase is Phase.FAILED
    assert state.error == "boom"


def test_a_missing_selector_never_reaches_the_worker(tmp_path: Path) -> None:
    incomplete = SelectorSet.create(link="a", title="", content="article")
    assert not incomplete.complete
    assert incomplete.missing == ("title",)


def resolver(_host: str) -> Sequence[str]:
    return ["93.184.216.34"]


def test_one_rule_is_announced_once_however_many_chapters_it_covers() -> None:
    """A Disallow usually covers the whole book.

    A live run printed the identical rule six times for six chapters. Repeating
    a warning that must be read is how it stops being read.
    """
    state = RunState()
    rule = '"disallow: /members/" at robots.txt line 2 (User-agent: *)'
    apply(state, [RobotsOverride(rule_description=rule) for _ in range(6)])

    assert state.robots_overrides == [rule]
    assert sum(1 for line in state.log if "line 2" in line.text) == 1


def test_distinct_rules_are_each_announced() -> None:
    state = RunState()
    apply(
        state,
        [
            RobotsOverride(rule_description="rule A"),
            RobotsOverride(rule_description="rule B"),
            RobotsOverride(rule_description="rule A"),
        ],
    )
    assert state.robots_overrides == ["rule A", "rule B"]
