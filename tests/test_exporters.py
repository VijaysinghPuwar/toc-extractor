"""Exporters, the registry, and the fourth enforcement of the accounting rule."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path

import pytest

from toc_extractor.exporters import DEFAULT_FORMAT, available, build_sink
from toc_extractor.exporters.jsonl import MANIFEST_NAME, JsonlExporter
from toc_extractor.exporters.markdown import BOOK_NAME, MarkdownExporter
from toc_extractor.exporters.text import COMBINED_NAME, SEPARATOR, TextExporter
from toc_extractor.models import ChapterRecord, FailedChapter, PriorChapter, RunResult
from toc_extractor.parser import LinkCollection, RejectedLink, RobotsDecision
from toc_extractor.politeness import RejectionReason
from toc_extractor.sinks import MultiSink

FIXED_TIME = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)


def record(index: int, *, title: str | None = None, stripped: int = 0) -> ChapterRecord:
    return ChapterRecord(
        index=index,
        requested_url=f"https://example.com/ch/{index}",
        final_url=f"https://example.com/ch/{index}",
        title=title or f"Chapter {index}",
        text=f"Body {index}.",
        stripped_urls=stripped,
        fetched_at=FIXED_TIME,
        attempts=1,
    )


def prior(index: int, *, output_sha256: str | None = None) -> PriorChapter:
    """What the checkpoint recalls about a chapter an earlier run wrote."""
    source = record(index)
    if output_sha256 is None:
        # The bytes TextExporter writes for this record.
        output_sha256 = hashlib.sha256(f"{source.title}\n\n{source.text}\n".encode()).hexdigest()
    return PriorChapter(
        index=index,
        url=source.requested_url,
        output_name=f"{index:03d} - Chapter {index}.txt",
        title=source.title,
        bytes=source.byte_count,
        sha256=source.sha256,
        stripped_urls=source.stripped_urls,
        fetched_at=FIXED_TIME.isoformat(),
        output_sha256=output_sha256,
    )


def result_for(records: Sequence[ChapterRecord], **kwargs: object) -> RunResult:
    kept = tuple(r.requested_url for r in records)
    collection = LinkCollection(raw_count=len(kept), kept=kept)
    return RunResult(
        toc_url="https://example.com/toc",
        collection=kwargs.pop("collection", collection),  # type: ignore[arg-type]
        completed=tuple(records),
        **kwargs,  # type: ignore[arg-type]
    )


async def export(sink: object, records: Sequence[ChapterRecord], **kwargs: object) -> RunResult:
    await sink.open()  # type: ignore[attr-defined]
    for item in records:
        await sink.write(item)  # type: ignore[attr-defined]
    outcome = result_for(records, **kwargs)
    await sink.close(outcome)  # type: ignore[attr-defined]
    return outcome


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


def test_available_formats() -> None:
    assert available() == ["jsonl", "markdown", "text"]
    assert DEFAULT_FORMAT == "text"


def test_default_is_text_when_nothing_is_asked_for(tmp_path: Path) -> None:
    assert isinstance(build_sink([], tmp_path), TextExporter)


def test_several_formats_fan_out(tmp_path: Path) -> None:
    assert isinstance(build_sink(["text", "jsonl"], tmp_path), MultiSink)


def test_repeated_format_is_deduplicated(tmp_path: Path) -> None:
    assert isinstance(build_sink(["text", "text"], tmp_path), TextExporter)


def test_unknown_format_names_the_alternatives(tmp_path: Path) -> None:
    with pytest.raises(KeyError, match="jsonl, markdown, text"):
        build_sink(["epub"], tmp_path)


async def test_multi_sink_writes_every_format(tmp_path: Path) -> None:
    await export(build_sink(["text", "markdown", "jsonl"], tmp_path), [record(1), record(2)])
    names = sorted(p.name for p in tmp_path.iterdir())
    assert COMBINED_NAME in names
    assert BOOK_NAME in names
    assert MANIFEST_NAME in names


# ---------------------------------------------------------------------------
# Text: v1 layout
# ---------------------------------------------------------------------------


async def test_chapter_file_matches_v1_layout(tmp_path: Path) -> None:
    await export(TextExporter(tmp_path), [record(1)])
    assert (tmp_path / "001 - Chapter 1.txt").read_text(encoding="utf-8") == (
        "Chapter 1\n\nBody 1.\n"
    )


async def test_combined_matches_v1_layout(tmp_path: Path) -> None:
    await export(TextExporter(tmp_path), [record(1), record(2)])
    assert (tmp_path / COMBINED_NAME).read_text(encoding="utf-8") == (
        f"Chapter 1\n\nBody 1.\n\n{SEPARATOR}\n\nChapter 2\n\nBody 2.\n\n{SEPARATOR}\n\n"
    )


async def test_include_links_adds_a_source_line(tmp_path: Path) -> None:
    await export(TextExporter(tmp_path, include_links=True), [record(1)])
    body = (tmp_path / "001 - Chapter 1.txt").read_text(encoding="utf-8")
    assert body == "Chapter 1\n\nSource: https://example.com/ch/1\n\nBody 1.\n"


async def test_combined_is_index_ordered_regardless_of_write_order(tmp_path: Path) -> None:
    sink = TextExporter(tmp_path)
    await sink.open()
    for item in (record(3), record(1), record(2)):
        await sink.write(item)
    await sink.close(result_for([record(1), record(2), record(3)]))

    combined = (tmp_path / COMBINED_NAME).read_text(encoding="utf-8")
    assert [combined.index(f"Chapter {i}") for i in (1, 2, 3)] == sorted(
        combined.index(f"Chapter {i}") for i in (1, 2, 3)
    )


# ---------------------------------------------------------------------------
# Text: resume, and the two ways this can go wrong
# ---------------------------------------------------------------------------


async def test_resumed_chapters_are_merged_from_recorded_names(tmp_path: Path) -> None:
    await export(TextExporter(tmp_path), [record(1), record(2)])

    resumed = {index: prior(index) for index in (1, 2)}
    await export(TextExporter(tmp_path, resumed=resumed), [record(3)])

    combined = (tmp_path / COMBINED_NAME).read_text(encoding="utf-8")
    assert [f"Chapter {i}" in combined for i in (1, 2, 3)] == [True, True, True]


async def test_stray_files_are_never_merged(tmp_path: Path) -> None:
    """The bug the fix could have introduced.

    Rebuilding combined.txt by globbing the output directory would sweep in
    anything shaped like a chapter file - a leftover from another book sharing
    the folder, or a hand-edited copy. Only what the checkpoint recorded is
    merged, so a stray file is ignored rather than silently published.
    """
    await export(TextExporter(tmp_path), [record(1)])
    (tmp_path / "002 - Someone Elses Book.txt").write_text("Not ours.\n", encoding="utf-8")

    await export(TextExporter(tmp_path, resumed={1: prior(1)}), [record(3)])

    combined = (tmp_path / COMBINED_NAME).read_text(encoding="utf-8")
    assert "Someone Elses Book" not in combined
    assert "Not ours." not in combined
    assert "Chapter 1" in combined and "Chapter 3" in combined


async def test_a_deleted_chapter_file_is_refused_not_quietly_dropped(tmp_path: Path) -> None:
    await export(TextExporter(tmp_path), [record(1), record(2)])
    (tmp_path / "001 - Chapter 1.txt").unlink()

    resumed = {index: prior(index) for index in (1, 2)}
    with pytest.raises(AssertionError, match="their files are gone"):
        await export(TextExporter(tmp_path, resumed=resumed), [record(3)])


async def test_merged_output_must_match_the_completed_set(tmp_path: Path) -> None:
    """The fourth enforcement point.

    accounts_for_every_link() was true throughout the run that silently
    truncated combined.txt, because the links were all accounted for - the loss
    was downstream, in the merge.
    """
    sink = TextExporter(tmp_path)
    await sink.open()
    await sink.write(record(1))

    # A result claiming a chapter the sink never saw.
    outcome = result_for([record(1), record(2)])
    with pytest.raises(AssertionError, match="merged output does not match"):
        await sink.close(outcome)


async def test_case_colliding_titles_get_distinct_files(tmp_path: Path) -> None:
    await export(
        TextExporter(tmp_path),
        [record(1, title="Chapter One"), record(2, title="chapter one")],
    )
    names = sorted(p.name for p in tmp_path.glob("*.txt") if p.name != COMBINED_NAME)
    assert names == ["001 - Chapter One.txt", "002 - chapter one.txt"]


# ---------------------------------------------------------------------------
# Markdown
# ---------------------------------------------------------------------------


async def test_markdown_writes_a_heading_and_a_book(tmp_path: Path) -> None:
    await export(MarkdownExporter(tmp_path), [record(1), record(2)])
    assert (tmp_path / "001 - Chapter 1.md").read_text(encoding="utf-8") == (
        "# Chapter 1\n\nBody 1.\n"
    )
    book = (tmp_path / BOOK_NAME).read_text(encoding="utf-8")
    assert book.startswith("# Chapter 1")
    assert "# Chapter 2" in book


async def test_markdown_title_cannot_reopen_the_document_structure(tmp_path: Path) -> None:
    """A chapter genuinely titled "# Prologue" must not produce two headings."""
    await export(MarkdownExporter(tmp_path), [record(1, title="## Prologue")])
    # '#' is not a forbidden path character, so the filename keeps it; only the
    # heading is normalised, which is where the structural damage would be.
    body = (tmp_path / "001 - ## Prologue.md").read_text(encoding="utf-8")
    assert body.startswith("# Prologue\n")
    assert "## Prologue" not in body


async def test_markdown_include_links(tmp_path: Path) -> None:
    await export(MarkdownExporter(tmp_path, include_links=True), [record(1)])
    body = (tmp_path / "001 - Chapter 1.md").read_text(encoding="utf-8")
    assert "[Source](https://example.com/ch/1)" in body


# ---------------------------------------------------------------------------
# JSONL manifest
# ---------------------------------------------------------------------------


def manifest_lines(tmp_path: Path) -> list[dict[str, object]]:
    text = (tmp_path / MANIFEST_NAME).read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines()]


async def test_manifest_carries_every_required_field(tmp_path: Path) -> None:
    await export(JsonlExporter(tmp_path), [record(1, stripped=2)])
    chapter = manifest_lines(tmp_path)[0]

    for field in (
        "url",
        "final_url",
        "fetched_at",
        "bytes",
        "sha256",
        "stripped_urls",
        "attempts",
        "index",
        "title",
    ):
        assert field in chapter, field
    assert chapter["stripped_urls"] == 2
    assert chapter["sha256"] == record(1).sha256


async def test_manifest_records_a_redirect(tmp_path: Path) -> None:
    moved = ChapterRecord(
        index=1,
        requested_url="https://example.com/old",
        final_url="https://example.com/new",
        title="Moved",
        text="Body.",
        stripped_urls=0,
        fetched_at=FIXED_TIME,
        attempts=1,
    )
    await export(JsonlExporter(tmp_path), [moved])
    chapter = manifest_lines(tmp_path)[0]
    assert chapter["final_url"] == "https://example.com/new"
    assert chapter["redirected"] is True


async def test_manifest_records_a_robots_override(tmp_path: Path) -> None:
    """The audit trail for the one way past a Disallow."""
    overridden = ChapterRecord(
        index=1,
        requested_url="https://example.com/members/1",
        final_url="https://example.com/members/1",
        title="Members",
        text="Body.",
        stripped_urls=0,
        fetched_at=FIXED_TIME,
        attempts=1,
        robots=RobotsDecision(
            allowed=True,
            rule_description='"disallow: /members/" at robots.txt line 7 (User-agent: *)',
            authenticated_override=True,
        ),
    )
    await export(JsonlExporter(tmp_path), [overridden])
    chapter = manifest_lines(tmp_path)[0]

    assert chapter["robots_authenticated_override"] is True
    assert "line 7" in str(chapter["robots_rule"])


async def test_manifest_summary_carries_rejections(tmp_path: Path) -> None:
    collection = LinkCollection(
        raw_count=4,
        kept=("https://example.com/ch/1",),
        rejected=(
            RejectedLink(value="{}", reason=RejectionReason.NOT_A_STRING, detail="got dict"),
            RejectedLink(
                value="file:///etc/passwd",
                reason=RejectionReason.DISALLOWED_SCHEME,
                detail="file",
            ),
        ),
        truncated=1,
    )
    await export(
        JsonlExporter(tmp_path),
        [record(1)],
        collection=collection,
        failed=(FailedChapter(index=9, url="u", reason="timeout", detail="d", attempts=3),),
    )

    summary = manifest_lines(tmp_path)[-1]
    assert summary["type"] == "summary"
    assert summary["raw_links"] == 4
    assert summary["truncated"] == 1
    assert summary["rejected"] == {"not_a_string": 1, "disallowed_scheme": 1}
    assert [item["reason"] for item in summary["rejected_detail"]] == [  # type: ignore[index,union-attr]
        "not_a_string",
        "disallowed_scheme",
    ]
    assert summary["failed"][0]["reason"] == "timeout"  # type: ignore[index]


async def test_manifest_is_index_ordered_with_summary_last(tmp_path: Path) -> None:
    sink = JsonlExporter(tmp_path)
    await sink.open()
    for item in (record(3), record(1), record(2)):
        await sink.write(item)
    await sink.close(result_for([record(1), record(2), record(3)]))

    lines = manifest_lines(tmp_path)
    assert [line["index"] for line in lines[:-1]] == [1, 2, 3]
    assert lines[-1]["type"] == "summary"


async def test_every_manifest_line_is_valid_json(tmp_path: Path) -> None:
    await export(JsonlExporter(tmp_path), [record(1, title="Ünïcödé — 中文 🙂")])
    for line in (tmp_path / MANIFEST_NAME).read_text(encoding="utf-8").splitlines():
        assert json.loads(line)


async def test_an_edited_chapter_file_is_refused(tmp_path: Path) -> None:
    """The gap the missing-file check left open.

    Refusing a deleted chapter but accepting a truncated one would let modified
    content into the merged output with no signal - the same silent-loss class,
    one boundary over.
    """
    await export(TextExporter(tmp_path), [record(1), record(2)])
    (tmp_path / "001 - Chapter 1.txt").write_text("Tampered.\n", encoding="utf-8")

    resumed = {index: prior(index) for index in (1, 2)}
    with pytest.raises(AssertionError, match="changed since they were written"):
        await export(TextExporter(tmp_path, resumed=resumed), [record(3)])


async def test_an_unchanged_chapter_file_passes_verification(tmp_path: Path) -> None:
    await export(TextExporter(tmp_path), [record(1), record(2)])
    resumed = {index: prior(index) for index in (1, 2)}
    await export(TextExporter(tmp_path, resumed=resumed), [record(3)])

    combined = (tmp_path / COMBINED_NAME).read_text(encoding="utf-8")
    assert all(f"Chapter {i}" in combined for i in (1, 2, 3))


async def test_a_checkpoint_without_a_hash_still_resumes(tmp_path: Path) -> None:
    """Checkpoints written before the hash existed must not become unusable."""
    await export(TextExporter(tmp_path), [record(1)])
    resumed = {1: prior(1, output_sha256="")}
    await export(TextExporter(tmp_path, resumed=resumed), [record(2)])

    combined = (tmp_path / COMBINED_NAME).read_text(encoding="utf-8")
    assert "Chapter 1" in combined and "Chapter 2" in combined
