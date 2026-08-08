"""The seam between the fetch loop and where chapters end up.

The fetch loop never touches the filesystem. Concrete writers live in
`exporters/`; this module holds only the protocol, the fan-out, and the sink
that discards everything.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from .models import ChapterRecord, RunResult


class Sink(Protocol):
    """Receives chapters as they complete, then a final accounting."""

    async def open(self) -> None: ...

    async def write(self, record: ChapterRecord) -> None: ...

    async def close(self, result: RunResult) -> None: ...


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


class MultiSink:
    """Fans one run out to several exporters.

    Sequential rather than gathered: the exporters all write into one
    directory, and a failure in the third should not race the first two into
    an inconsistent state.
    """

    def __init__(self, sinks: Sequence[Sink]) -> None:
        self._sinks = list(sinks)

    async def open(self) -> None:
        for sink in self._sinks:
            await sink.open()

    async def write(self, record: ChapterRecord) -> None:
        for sink in self._sinks:
            await sink.write(record)

    async def close(self, result: RunResult) -> None:
        for sink in self._sinks:
            await sink.close(result)
