"""v1's output: one file per chapter plus combined.txt."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..logging import get_logger
from ..models import ChapterRecord, PriorChapter, RunResult
from ..text import FilenameAllocator

log = get_logger("exporters.text")

COMBINED_NAME = "combined.txt"
SEPARATOR = "-" * 80


class TextExporter:
    """Per-chapter .txt files and a merged combined.txt.

    Chapters land as they complete, which with concurrency is out of order.
    combined.txt is assembled at close in index order, because v1 wrote it
    sequentially and the byte-identity claim is about its contents.
    """

    name = "text"

    def __init__(
        self,
        output_dir: Path,
        *,
        include_links: bool = False,
        resumed: Mapping[int, PriorChapter] | None = None,
    ) -> None:
        self._dir = output_dir
        self._include_links = include_links
        # Chapters completed by an *earlier* run, from the checkpoint.
        # Taken from the checkpoint rather than from a directory scan: a glob
        # would sweep in stray files, or a second book sharing this folder,
        # and merge them into the wrong book. Fixing a silent-loss bug by
        # introducing a silent-merge bug is not a fix.
        self._resumed = dict(resumed or {})
        self._allocator = FilenameAllocator()
        self._chunks: dict[int, str] = {}
        self.written: dict[int, Path] = {}
        self.deduplicated: list[tuple[str, str]] = []

    async def open(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    async def write(self, record: ChapterRecord) -> None:
        allocated = self._allocator.allocate(record.index, record.title)
        if allocated.collided_with is not None:
            self.deduplicated.append((allocated.collided_with, allocated.name))
            log.warning(
                "%r would overwrite %r on a case-insensitive filesystem; writing %r instead",
                record.title,
                allocated.collided_with,
                allocated.name,
            )

        header = f"{record.title}\n\n"
        if self._include_links:
            header += f"Source: {record.final_url}\n\n"

        path = self._dir / allocated.name
        path.write_text(header + record.text + "\n", encoding="utf-8")

        self.written[record.index] = path
        self._chunks[record.index] = header + record.text + "\n\n" + SEPARATOR + "\n\n"

    async def close(self, result: RunResult) -> None:
        chunks = dict(self._chunks)

        missing: list[str] = []
        for index, prior in self._resumed.items():
            if index in chunks:
                continue
            path = self._dir / prior.output_name
            if not path.exists():
                missing.append(prior.output_name)
                continue
            # Exact reconstruction: a chapter file is its combined entry minus
            # the separator, so no re-derivation of the header is needed.
            chunks[index] = path.read_text(encoding="utf-8") + "\n" + SEPARATOR + "\n\n"

        if missing:
            raise AssertionError(
                "combined.txt would omit chapters the checkpoint records as complete "
                f"because their files are gone: {', '.join(sorted(missing))}. "
                "Delete the output directory and rerun, or pass --force."
            )

        _assert_merged_output_is_complete(chunks, self._resumed, result)

        combined = self._dir / COMBINED_NAME
        combined.write_text("".join(chunks[index] for index in sorted(chunks)), encoding="utf-8")


def _assert_merged_output_is_complete(
    chunks: Mapping[int, str],
    resumed: Mapping[int, PriorChapter],
    result: RunResult,
) -> None:
    """The fourth enforcement point for one invariant: nothing is lost silently.

    The other three are LinkCollection's constructor check, the PageError
    translation boundary, and run()'s accounting assertion. Each was added
    after a real bug in which content disappeared without a trace, and this one
    is no different: a resumed run rebuilt combined.txt from only the chapters
    it had just fetched, silently dropping everything from the previous run.
    Every existing check passed while it happened, because
    accounts_for_every_link() was true - the links were all accounted for. The
    loss was downstream of every check that existed, in the merge itself.
    """
    expected = set(resumed) | {record.index for record in result.completed}
    written = set(chunks)
    if written != expected:
        raise AssertionError(
            "merged output does not match the completed chapters: "
            f"missing {sorted(expected - written)}, unexpected {sorted(written - expected)}"
        )
