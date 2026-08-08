"""Where extracted chapters go.

The fetch loop never touches the filesystem. This is the seam Phase 4's
exporter registry plugs into; only the seam and one text sink exist now.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .models import ChapterRecord, RunResult
from .text import FilenameAllocator

COMBINED_NAME = "combined.txt"
SEPARATOR = "-" * 80


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
        combined = self._dir / COMBINED_NAME
        ordered = (self._chunks[index] for index in sorted(self._chunks))
        combined.write_text("".join(ordered), encoding="utf-8")


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
