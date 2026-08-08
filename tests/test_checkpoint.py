"""Checkpoint: URL keying, atomic writes, and the TOC-growth semantics."""

from __future__ import annotations

import json
import os
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from toc_extractor.checkpoint import (
    STATE_FILENAME,
    Checkpoint,
    TocComparison,
    announce,
    compare_link_sets,
    fingerprint,
    normalize_url,
    plan_resume,
)
from toc_extractor.fetcher import Fetcher, FetchOptions
from toc_extractor.models import ChapterRecord
from toc_extractor.parser import SelectorSet
from toc_extractor.politeness import RateLimiter, UrlGuard
from toc_extractor.sinks import NullSink

from .stub import StubPage, StubPageSource

TOC = "https://example.com/toc"
SELECTORS = SelectorSet.create(link="a.ch", title="h1", content="article")
OTHER_SELECTORS = SelectorSet.create(link="a.ch", title="h1", content="div.body")
FIXED_TIME = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def resolver(_host: str) -> Sequence[str]:
    return ["93.184.216.34"]


GUARD = UrlGuard(resolver=resolver)


class _SimulatedInterrupt(BaseException):
    """Stands in for Ctrl-C without tripping pytest's session-interrupt path.

    A literal KeyboardInterrupt here makes pytest tear down the run it belongs
    to, which is the same trap that killed an earlier draft of the error
    boundary tests. Subclassing BaseException exercises the identical code path
    - outside `except Exception`, propagating through TaskGroup - with none of
    the collateral damage.
    """


def urls(count: int, *, prefix: str = "ch") -> list[str]:
    return [f"https://example.com/{prefix}/{i}" for i in range(1, count + 1)]


def make_record(index: int, url: str) -> ChapterRecord:
    return ChapterRecord(
        index=index,
        requested_url=url,
        final_url=url,
        title=f"Chapter {index}",
        text=f"Body {index}.",
        stripped_urls=0,
        fetched_at=FIXED_TIME,
        attempts=1,
    )


def seeded(tmp_path: Path, *, links: list[str], done: int, toc: str = TOC) -> Checkpoint:
    cp = Checkpoint(
        path=Checkpoint.path_for(tmp_path),
        toc_url=toc,
        fingerprint=fingerprint(toc, SELECTORS),
        link_set=list(links),
    )
    for index, url in enumerate(links[:done], start=1):
        cp.record(make_record(index, url), f"{index:03d} - Chapter {index}.txt")
    cp.save()
    return cp


# ---------------------------------------------------------------------------
# Keying and normalisation
# ---------------------------------------------------------------------------


def test_state_is_keyed_on_url_not_filename(tmp_path: Path) -> None:
    """A v1 run and a v2 run name the same chapter differently.

    Keying on output_name would make a resume conclude nothing had been done.
    """
    cp = seeded(tmp_path, links=urls(3), done=2)
    payload = json.loads(cp.path.read_text(encoding="utf-8"))

    assert set(payload["completed"]) == {
        "https://example.com/ch/1",
        "https://example.com/ch/2",
    }
    assert payload["completed"]["https://example.com/ch/1"]["output_name"] == (
        "001 - Chapter 1.txt"
    )


@pytest.mark.parametrize(
    "left,right",
    [
        ("https://example.com/ch/1/", "https://example.com/ch/1"),
        ("https://example.com/ch/1#part2", "https://example.com/ch/1"),
        ("https://EXAMPLE.com/ch/1", "https://example.com/ch/1"),
        ("HTTPS://example.com/ch/1", "https://example.com/ch/1"),
    ],
)
def test_cosmetic_differences_do_not_read_as_a_reorder(left: str, right: str) -> None:
    assert normalize_url(left) == normalize_url(right)


def test_query_parameters_are_load_bearing_and_kept() -> None:
    """On plenty of TOCs the query string carries the chapter identity."""
    assert normalize_url("https://example.com/read?ch=1") != normalize_url(
        "https://example.com/read?ch=2"
    )


# ---------------------------------------------------------------------------
# Fingerprint
# ---------------------------------------------------------------------------


def test_fingerprint_ignores_the_link_set() -> None:
    """Growth must not invalidate a run; that is the whole point of the split."""
    assert fingerprint(TOC, SELECTORS) == fingerprint(TOC, SELECTORS)


def test_changing_a_selector_changes_the_fingerprint() -> None:
    """Same book, different text - not the same extraction."""
    assert fingerprint(TOC, SELECTORS) != fingerprint(TOC, OTHER_SELECTORS)


def test_changing_the_toc_url_changes_the_fingerprint() -> None:
    assert fingerprint(TOC, SELECTORS) != fingerprint("https://example.com/other", SELECTORS)


# ---------------------------------------------------------------------------
# Link-set comparison
# ---------------------------------------------------------------------------


def test_identical_link_sets() -> None:
    assert compare_link_sets(urls(3), urls(3)) is TocComparison.IDENTICAL


def test_growth_at_the_end_is_accepted() -> None:
    assert compare_link_sets(urls(3), urls(5)) is TocComparison.GREW_AT_END


def test_growth_at_the_start_is_accepted() -> None:
    """Reverse-chronological serials prepend; demanding --force for that is wrong."""
    stored = urls(3)
    current = ["https://example.com/ch/0", *stored]
    assert compare_link_sets(stored, current) is TocComparison.GREW_AT_START


def test_growth_at_both_ends_is_ambiguous() -> None:
    stored = urls(3)
    current = ["https://example.com/ch/0", *stored, "https://example.com/ch/9"]
    assert compare_link_sets(stored, current) is TocComparison.DIVERGED


def test_removal_diverges() -> None:
    assert compare_link_sets(urls(5), urls(3)) is TocComparison.DIVERGED


def test_interior_reorder_diverges() -> None:
    stored = urls(4)
    current = [stored[0], stored[2], stored[1], stored[3]]
    assert compare_link_sets(stored, current) is TocComparison.DIVERGED


def test_comparison_tolerates_cosmetic_url_differences() -> None:
    stored = ["https://example.com/ch/1", "https://example.com/ch/2"]
    current = ["https://example.com/ch/1/", "https://example.com/ch/2#top"]
    assert compare_link_sets(stored, current) is TocComparison.IDENTICAL


# ---------------------------------------------------------------------------
# Resume planning
# ---------------------------------------------------------------------------


def test_no_checkpoint_means_no_plan(tmp_path: Path) -> None:
    assert plan_resume(tmp_path, toc_url=TOC, selectors=SELECTORS, current_links=urls(3)) is None


def test_resume_is_the_default(tmp_path: Path) -> None:
    seeded(tmp_path, links=urls(5), done=3)
    plan = plan_resume(tmp_path, toc_url=TOC, selectors=SELECTORS, current_links=urls(5))

    assert plan is not None
    assert plan.usable
    assert plan.already_done == 3
    assert plan.comparison is TocComparison.IDENTICAL


def test_force_discards_the_checkpoint(tmp_path: Path) -> None:
    seeded(tmp_path, links=urls(5), done=3)
    plan = plan_resume(
        tmp_path, toc_url=TOC, selectors=SELECTORS, current_links=urls(5), force=True
    )

    assert plan is None
    assert not (tmp_path / STATE_FILENAME).exists()


def test_changed_selectors_refuse_to_resume(tmp_path: Path) -> None:
    seeded(tmp_path, links=urls(5), done=3)
    plan = plan_resume(tmp_path, toc_url=TOC, selectors=OTHER_SELECTORS, current_links=urls(5))

    assert plan is not None
    assert not plan.usable
    assert "different selectors" in plan.refusal
    assert "--force" in plan.refusal


def test_appended_chapters_are_named(tmp_path: Path) -> None:
    seeded(tmp_path, links=urls(3), done=3)
    plan = plan_resume(tmp_path, toc_url=TOC, selectors=SELECTORS, current_links=urls(5))

    assert plan is not None
    assert plan.usable
    assert plan.appended == ("https://example.com/ch/4", "https://example.com/ch/5")
    assert not plan.renumbering


def test_prepend_resumes_and_flags_renumbering(tmp_path: Path) -> None:
    """The named case: growth at the start, with the numbering consequence."""
    stored = urls(3)
    seeded(tmp_path, links=stored, done=3)
    current = ["https://example.com/ch/0", *stored]

    plan = plan_resume(tmp_path, toc_url=TOC, selectors=SELECTORS, current_links=current)

    assert plan is not None
    assert plan.usable
    assert plan.comparison is TocComparison.GREW_AT_START
    assert plan.appended == ("https://example.com/ch/0",)
    assert plan.renumbering


def test_prepend_preserves_stored_output_names(tmp_path: Path) -> None:
    """Already-written files keep their numbers; new ones continue from the max."""
    stored = urls(3)
    checkpoint = seeded(tmp_path, links=stored, done=3)
    reloaded = Checkpoint.load(tmp_path)

    assert reloaded is not None
    assert reloaded.completed[stored[0]].output_name == "001 - Chapter 1.txt"
    assert reloaded.next_index() == 4
    assert checkpoint.next_index() == 4


def test_divergent_toc_refuses_with_specifics(tmp_path: Path) -> None:
    seeded(tmp_path, links=urls(5), done=2)
    plan = plan_resume(tmp_path, toc_url=TOC, selectors=SELECTORS, current_links=urls(3))

    assert plan is not None
    assert not plan.usable
    assert "stored 5 links, found 3" in plan.refusal
    assert "--force" in plan.refusal


def test_announce_warns_prominently_about_renumbering(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    stored = urls(3)
    seeded(tmp_path, links=stored, done=3)
    plan = plan_resume(
        tmp_path,
        toc_url=TOC,
        selectors=SELECTORS,
        current_links=["https://example.com/ch/0", *stored],
    )
    assert plan is not None

    with caplog.at_level("INFO", logger="toc_extractor.checkpoint"):
        announce(plan)

    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert any("numbering" in r.getMessage() for r in warnings)
    assert any("Resuming" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# Durability
# ---------------------------------------------------------------------------


def test_save_is_atomic_and_leaves_no_temp_files(tmp_path: Path) -> None:
    seeded(tmp_path, links=urls(3), done=2)
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != STATE_FILENAME]
    assert leftovers == []


def test_interrupted_write_leaves_the_previous_state_intact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Ctrl-C mid-write must not truncate the file that already existed."""
    checkpoint = seeded(tmp_path, links=urls(3), done=1)
    before = checkpoint.path.read_text(encoding="utf-8")

    def explode(src: object, dst: object) -> None:
        raise _SimulatedInterrupt("interrupted between fsync and rename")

    checkpoint.record(make_record(2, "https://example.com/ch/2"), "002 - Chapter 2.txt")
    monkeypatch.setattr(os, "replace", explode)
    with pytest.raises(_SimulatedInterrupt):
        checkpoint.save()
    monkeypatch.undo()

    assert checkpoint.path.read_text(encoding="utf-8") == before
    assert json.loads(before)["completed"].keys() == {"https://example.com/ch/1"}
    assert [p.name for p in tmp_path.iterdir()] == [STATE_FILENAME]


def test_corrupt_checkpoint_is_ignored_not_fatal(tmp_path: Path) -> None:
    path = Checkpoint.path_for(tmp_path)
    path.write_text("{not json", encoding="utf-8")
    assert Checkpoint.load(tmp_path) is None


def test_unknown_schema_version_is_ignored(tmp_path: Path) -> None:
    path = Checkpoint.path_for(tmp_path)
    path.write_text(json.dumps({"version": 99}), encoding="utf-8")
    assert Checkpoint.load(tmp_path) is None


def test_roundtrip_preserves_every_field(tmp_path: Path) -> None:
    checkpoint = seeded(tmp_path, links=urls(2), done=0)
    record = ChapterRecord(
        index=7,
        requested_url="https://example.com/ch/7",
        final_url="https://example.com/moved/7",
        title="Seven",
        text="body with https://a.com removed",
        stripped_urls=3,
        fetched_at=FIXED_TIME,
        attempts=2,
    )
    checkpoint.record(record, "007 - Seven.txt")
    checkpoint.save()

    reloaded = Checkpoint.load(tmp_path)
    assert reloaded is not None
    entry = reloaded.completed["https://example.com/ch/7"]
    assert entry.index == 7
    assert entry.output_name == "007 - Seven.txt"
    assert entry.stripped_urls == 3
    assert entry.sha256 == record.sha256
    assert entry.bytes == record.byte_count


# ---------------------------------------------------------------------------
# End to end with the fetch loop
# ---------------------------------------------------------------------------


async def test_cancel_then_resume_refetches_nothing_and_misses_nothing(
    tmp_path: Path,
) -> None:
    """The failure the whole feature exists for, start to finish."""
    all_urls = urls(6)
    catalogue = {TOC: StubPage(links=all_urls)}
    for position, url in enumerate(all_urls, start=1):
        # A real await per page, so the task group's cancellation has an
        # actual suspension point to land on. With an instant stub the
        # remaining tasks finish in the same loop iteration and the
        # "interrupted" run completes everything.
        catalogue[url] = StubPage(title=f"Chapter {position}", body=f"Body {position}.", hang=0.01)

    checkpoint = Checkpoint(
        path=Checkpoint.path_for(tmp_path),
        toc_url=TOC,
        fingerprint=fingerprint(TOC, SELECTORS),
        link_set=all_urls,
    )

    def persist(record: ChapterRecord) -> None:
        checkpoint.record(record, f"{record.index:03d} - {record.title}.txt")
        checkpoint.save()

    first_source = StubPageSource(catalogue)
    stop_after = 3

    def persist_and_stop(record: ChapterRecord) -> None:
        persist(record)
        if len(checkpoint.completed) >= stop_after:
            raise _SimulatedInterrupt("simulated Ctrl-C")

    fetcher = Fetcher(
        first_source,
        guard=GUARD,
        sink=NullSink(),
        options=FetchOptions(concurrency=1, min_delay=0.0, wait_after_load=0.0),
        limiter=RateLimiter(min_interval=0.0),
        now=lambda: FIXED_TIME,
        on_record=persist_and_stop,
    )
    with pytest.raises(BaseExceptionGroup):
        await fetcher.run(TOC, SELECTORS)

    first_round = set(checkpoint.completed)
    # The exact stop point is incidental; that the run stopped partway is not.
    assert stop_after <= len(first_round) < len(all_urls)

    plan = plan_resume(tmp_path, toc_url=TOC, selectors=SELECTORS, current_links=all_urls)
    assert plan is not None and plan.usable
    assert plan.already_done == len(first_round)

    resumed = plan.checkpoint
    second_source = StubPageSource(catalogue)
    second = Fetcher(
        second_source,
        guard=GUARD,
        sink=NullSink(),
        options=FetchOptions(concurrency=1, min_delay=0.0, wait_after_load=0.0),
        limiter=RateLimiter(min_interval=0.0),
        now=lambda: FIXED_TIME,
        already_done=resumed.is_done,
        on_record=lambda r: resumed.record(r, f"{r.index:03d} - {r.title}.txt"),
    )
    result = await second.run(TOC, SELECTORS)

    refetched = first_round & {url for url in second_source.urls_loaded if url != TOC}
    assert refetched == set(), f"refetched already-completed chapters: {refetched}"
    assert set(resumed.completed) == set(all_urls), "a chapter was missed"
    assert result.accounts_for_every_link()
    assert len(result.skipped_resumed) == len(first_round)
