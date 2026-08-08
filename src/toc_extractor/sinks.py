"""Where extracted chapters go.

The fetch loop never touches the filesystem. This is the seam Phase 4's
exporter registry plugs into; only the seam and one text sink exist now.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from .models import ChapterRecord, RunResult
from .text import FilenameAllocator

COMBINED_NAME = "combined.txt"
_INDEX_RE = re.compile(r"^(\d{3}) - ")
SEPARATOR = "-" * 80


def _index_of(name: str) -> int | None:
    """The NNN prefix a chapter file was written with, if it has one."""
    match = _INDEX_RE.match(name)
    return int(match.group(1)) if match else None


class Sink(Protocol):
    """Receives chapters as they complete, then a final accounting."""

    async def open(self) -> None: ...

    async def write(self, record: ChapterRecord) -> None: ...

    async def close(self, result: RunResult) -> None: ...


class TextSink:
    """v1's output: one file per chapter plus combined.txt.

    Per-chapter files land as chapters complete, which with concurrency is out
    of order. combined.txt is assembled at close in index order, because v1
    wrote it sequentially and the golden byte-identity claim is about its
    contents, not about when the bytes were written.
    """

    def __init__(self, output_dir: Path, *, include_links: bool = False) -> None:
        self._dir = output_dir
        self._include_links = include_links
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

        header = f"{record.title}\n\n"
        if self._include_links:
            header += f"Source: {record.final_url}\n\n"

        path = self._dir / allocated.name
        path.write_text(header + record.text + "\n", encoding="utf-8")

        self.written[record.index] = path
        self._chunks[record.index] = header + record.text + "\n\n" + SEPARATOR + "\n\n"

    async def close(self, result: RunResult) -> None:
        """Rebuild combined.txt from everything on disk, not just this run.

        A resumed run only holds chunks for the chapters it fetched. Writing
        those alone truncated combined.txt to the new chapters and silently
        discarded the earlier ones - the per-chapter files were intact, so
        nothing looked wrong until you opened the merged file. Chapters from a
        previous run are reconstructed from their files, which is exact: a
        chapter file is the combined entry minus the separator.
        """
        chunks = dict(self._chunks)
        for path in self._dir.glob("*.txt"):
            if path.name == COMBINED_NAME:
                continue
            index = _index_of(path.name)
            if index is None or index in chunks:
                continue
            chunks[index] = path.read_text(encoding="utf-8") + "\n" + SEPARATOR + "\n\n"

        combined = self._dir / COMBINED_NAME
        combined.write_text("".join(chunks[index] for index in sorted(chunks)), encoding="utf-8")


class NullSink:
    """Discards everything. Used by --dry-run, which must not write."""

    def __init__(self) -> None:
        self.records: list[ChapterRecord] = []

    async def open(self) -> None:
        return None

    async def write(self, record: ChapterRecord) -> None:
        self.records.append(record)

    async def close(self, result: RunResult) -> None:
        return None
