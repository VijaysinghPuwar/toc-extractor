"""Output formats, addressed by name.

A plain dict rather than entry points or an ABC. Three formats in a
single-purpose tool do not need a plugin system, and one would be more code
than the exporters it dispatches to.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from ..models import PriorChapter
from ..sinks import MultiSink, Sink
from .jsonl import JsonlExporter
from .markdown import MarkdownExporter
from .text import TextExporter

ExporterFactory = Callable[..., Sink]

EXPORTERS: dict[str, ExporterFactory] = {
    TextExporter.name: TextExporter,
    MarkdownExporter.name: MarkdownExporter,
    JsonlExporter.name: JsonlExporter,
}

DEFAULT_FORMAT = TextExporter.name


def available() -> list[str]:
    return sorted(EXPORTERS)


def build_sink(
    formats: Sequence[str],
    output_dir: Path,
    *,
    include_links: bool = False,
    resumed: Mapping[int, PriorChapter] | None = None,
) -> Sink:
    """One sink for the requested formats, deduplicated, in a stable order."""
    chosen: list[str] = []
    for name in formats or [DEFAULT_FORMAT]:
        if name not in EXPORTERS:
            raise KeyError(f"unknown format {name!r}; available: {', '.join(available())}")
        if name not in chosen:
            chosen.append(name)

    sinks = [
        EXPORTERS[name](output_dir, include_links=include_links, resumed=resumed) for name in chosen
    ]
    return sinks[0] if len(sinks) == 1 else MultiSink(sinks)


def text_exporter_of(sink: Sink) -> TextExporter | None:
    """The TextExporter inside a sink, if there is one.

    The checkpoint has to record the name the exporter actually wrote. Building
    it by formatting the title, as the CLI did, produced "007 - A/B.txt" for a
    file really called "007 - A_B (2).txt" - unsanitised and unaware of
    collision dedup - so the next resume hard-failed looking for a file that
    never existed.
    """
    if isinstance(sink, TextExporter):
        return sink
    if isinstance(sink, MultiSink):
        for inner in sink.sinks:
            if isinstance(inner, TextExporter):
                return inner
    return None


__all__ = [
    "DEFAULT_FORMAT",
    "EXPORTERS",
    "JsonlExporter",
    "MarkdownExporter",
    "TextExporter",
    "available",
    "build_sink",
    "text_exporter_of",
]
