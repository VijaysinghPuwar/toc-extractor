"""Typed records that travel from the fetch loop to a sink.

Everything a Phase 4 exporter could want is carried here, populated once by
the fetcher. The alternative — sinks reaching back for detail — would mean
reopening the fetch loop every time an exporter needs one more field.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime

from .parser import LinkCollection, RobotsDecision


@dataclass(frozen=True, slots=True)
class ChapterRecord:
    """One successfully extracted chapter."""

    index: int
    requested_url: str
    final_url: str
    title: str
    text: str
    stripped_urls: int
    fetched_at: datetime
    attempts: int
    robots: RobotsDecision | None = None

    @property
    def byte_count(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    @property
    def redirected(self) -> bool:
        return self.requested_url != self.final_url


@dataclass(frozen=True, slots=True)
class FailedChapter:
    """One chapter that could not be extracted, and why."""

    index: int
    url: str
    reason: str
    detail: str
    attempts: int


@dataclass(frozen=True, slots=True)
class RunResult:
    """End-of-run accounting.

    `collection` carries the raw/kept/rejected/truncated invariant from
    parser.py, so a caller can report exactly what happened to every link the
    TOC offered rather than only what succeeded.
    """

    toc_url: str
    collection: LinkCollection
    completed: tuple[ChapterRecord, ...] = ()
    failed: tuple[FailedChapter, ...] = ()
    skipped_resumed: tuple[str, ...] = ()
    appended_links: tuple[str, ...] = ()

    @property
    def attempted(self) -> int:
        return len(self.completed) + len(self.failed)

    @property
    def total_stripped_urls(self) -> int:
        return sum(record.stripped_urls for record in self.completed)

    def accounts_for_every_link(self) -> bool:
        """kept == completed + failed + resumed-skips."""
        return len(self.collection.kept) == (
            len(self.completed) + len(self.failed) + len(self.skipped_resumed)
        )
