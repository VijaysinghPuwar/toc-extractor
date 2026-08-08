"""Markdown output, one file per chapter plus a merged book.md.

No EPUB exporter. It would need author, language, cover, and spine order,
none of which a selector-driven scraper has, so it could only invent them.
`pandoc book.md -o book.epub` covers the same ground honestly.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from ..models import ChapterRecord, PriorChapter, RunResult
from ..text import FilenameAllocator

BOOK_NAME = "book.md"


def _escape_heading(title: str) -> str:
    """Stop a title from re-opening the document structure.

    A chapter genuinely titled "# Prologue" would otherwise produce two
    headings and break every downstream table of contents.
    """
    return title.lstrip("#").strip() or "untitled"


class MarkdownExporter:
    name = "markdown"

    def __init__(
        self,
        output_dir: Path,
        *,
        include_links: bool = False,
        resumed: Mapping[int, PriorChapter] | None = None,
    ) -> None:
        self._dir = output_dir
        self._include_links = include_links
        self._resumed = dict(resumed or {})
        self._allocator = FilenameAllocator(suffix=".md")
        self._chunks: dict[int, str] = {}

    async def open(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    async def write(self, record: ChapterRecord) -> None:
        allocated = self._allocator.allocate(record.index, record.title)
        body = f"# {_escape_heading(record.title)}\n\n"
        if self._include_links:
            body += f"[Source]({record.final_url})\n\n"
        body += record.text + "\n"

        (self._dir / allocated.name).write_text(body, encoding="utf-8")
        self._chunks[record.index] = body + "\n"

    async def close(self, result: RunResult) -> None:
        # Markdown is a derived view: unlike combined.txt it is regenerated
        # wholly from the chapters this run knows about, so a resumed run
        # rebuilds it from the .md files recorded in the checkpoint.
        chunks = dict(self._chunks)
        for index, prior in self._resumed.items():
            if index in chunks:
                continue
            path = self._dir / (Path(prior.output_name).stem + ".md")
            if path.exists():
                chunks[index] = path.read_text(encoding="utf-8") + "\n"

        book = self._dir / BOOK_NAME
        book.write_text("".join(chunks[index] for index in sorted(chunks)), encoding="utf-8")
