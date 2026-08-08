"""Resume support.

Keyed on URL, never on output filename. The v2 safe_filename change means a v1
run and a v2 run produce different names for the same chapter, so a
filename-keyed resume would decide nothing had been done and refetch the lot -
the exact outcome the politeness machinery exists to avoid.

Resume is the default. The failure this exists for is a 200-chapter run dying
at 180; making the user opt in would mean the common recovery sends 180
redundant requests to someone else's server.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from urllib.parse import urlparse, urlunparse

from .logging import get_logger
from .models import ChapterRecord
from .parser import SelectorSet

log = get_logger("checkpoint")

STATE_FILENAME = ".toc_extractor_state.json"
SCHEMA_VERSION = 1


class TocComparison(StrEnum):
    """How a stored link set relates to the one just collected."""

    IDENTICAL = "identical"
    GREW_AT_END = "grew_at_end"
    GREW_AT_START = "grew_at_start"
    DIVERGED = "diverged"


def normalize_url(url: str) -> str:
    """Canonicalise for comparison only.

    Trailing slash and fragment are cosmetic and must not read as a reordered
    TOC. Query parameters are left alone: on plenty of TOCs they carry the
    chapter identity.
    """
    parsed = urlparse(url)
    path = parsed.path.rstrip("/") or "/"
    return urlunparse((parsed.scheme.lower(), parsed.netloc.lower(), path, "", parsed.query, ""))


def fingerprint(toc_url: str, selectors: SelectorSet) -> str:
    """Identify the *extraction*, not the link set.

    Deliberately excludes the URLs. A serial gaining a chapter overnight is the
    normal case on the sites this tool targets, and folding the link set in here
    would invalidate a completed run every time that happened. Changing a
    selector is different in kind: same book, different text.
    """
    payload = json.dumps(
        {
            "toc_url": normalize_url(toc_url),
            "link": selectors.link,
            "title": selectors.title,
            "content": selectors.content,
        },
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CompletedChapter:
    """One chapter already written, as recorded on disk."""

    url: str
    index: int
    output_name: str
    title: str
    bytes: int
    sha256: str
    stripped_urls: int
    fetched_at: str

    @classmethod
    def from_record(cls, record: ChapterRecord, output_name: str) -> CompletedChapter:
        return cls(
            url=record.requested_url,
            index=record.index,
            output_name=output_name,
            title=record.title,
            bytes=record.byte_count,
            sha256=record.sha256,
            stripped_urls=record.stripped_urls,
            fetched_at=record.fetched_at.isoformat(),
        )

    def to_json(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "output_name": self.output_name,
            "title": self.title,
            "bytes": self.bytes,
            "sha256": self.sha256,
            "stripped_urls": self.stripped_urls,
            "fetched_at": self.fetched_at,
        }

    @classmethod
    def from_json(cls, url: str, payload: dict[str, Any]) -> CompletedChapter:
        return cls(
            url=url,
            index=int(payload["index"]),
            output_name=str(payload["output_name"]),
            title=str(payload.get("title", "")),
            bytes=int(payload.get("bytes", 0)),
            sha256=str(payload.get("sha256", "")),
            stripped_urls=int(payload.get("stripped_urls", 0)),
            fetched_at=str(payload.get("fetched_at", "")),
        )


@dataclass
class Checkpoint:
    """On-disk resume state for one output directory."""

    path: Path
    toc_url: str
    fingerprint: str
    selectors: dict[str, str] = field(default_factory=dict)
    link_set: list[str] = field(default_factory=list)
    completed: dict[str, CompletedChapter] = field(default_factory=dict)
    failed: dict[str, dict[str, Any]] = field(default_factory=dict)

    # -- persistence --------------------------------------------------------

    @staticmethod
    def path_for(output_dir: Path) -> Path:
        return output_dir / STATE_FILENAME

    @classmethod
    def load(cls, output_dir: Path) -> Checkpoint | None:
        path = cls.path_for(output_dir)
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, json.JSONDecodeError) as exc:
            # A corrupt state file must not abort a run that can simply start
            # over, but it must not be silent either.
            log.warning("ignoring unreadable checkpoint at %s: %s", path, exc)
            return None

        if payload.get("version") != SCHEMA_VERSION:
            log.warning(
                "ignoring checkpoint at %s written by schema version %s",
                path,
                payload.get("version"),
            )
            return None

        return cls(
            path=path,
            toc_url=str(payload.get("toc_url", "")),
            fingerprint=str(payload.get("fingerprint", "")),
            selectors={k: str(v) for k, v in payload.get("selectors", {}).items()},
            link_set=[str(url) for url in payload.get("link_set", [])],
            completed={
                url: CompletedChapter.from_json(url, entry)
                for url, entry in payload.get("completed", {}).items()
            },
            failed=dict(payload.get("failed", {})),
        )

    def save(self) -> None:
        """Write atomically: temp file, fsync, rename.

        A run interrupted mid-write must leave either the previous state or the
        new one, never a truncated file. rename is atomic within a filesystem,
        so the temp file is created in the same directory rather than /tmp.
        """
        payload = {
            "version": SCHEMA_VERSION,
            "toc_url": self.toc_url,
            "fingerprint": self.fingerprint,
            "selectors": self.selectors,
            "link_count": len(self.link_set),
            "link_set": self.link_set,
            "updated_at": datetime.now(UTC).isoformat(),
            "completed": {url: entry.to_json() for url, entry in self.completed.items()},
            "failed": self.failed,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)

        fd, temp_name = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        except BaseException:
            # Includes Ctrl-C: an abandoned temp file in the output directory
            # would be the visible residue of a crash nobody needs to see.
            Path(temp_name).unlink(missing_ok=True)
            raise

    def discard(self) -> None:
        self.path.unlink(missing_ok=True)

    # -- recording ----------------------------------------------------------

    def record(self, record: ChapterRecord, output_name: str) -> None:
        self.completed[record.requested_url] = CompletedChapter.from_record(record, output_name)
        self.failed.pop(record.requested_url, None)

    def record_failure(self, url: str, reason: str, attempts: int) -> None:
        self.failed[url] = {"reason": reason, "attempts": attempts}

    def is_done(self, url: str) -> bool:
        return url in self.completed

    def next_index(self) -> int:
        return max((entry.index for entry in self.completed.values()), default=0) + 1


@dataclass(frozen=True, slots=True)
class ResumePlan:
    """What a checkpoint means for the run about to start."""

    checkpoint: Checkpoint
    comparison: TocComparison
    already_done: int
    appended: tuple[str, ...] = ()
    usable: bool = True
    refusal: str = ""
    renumbering: bool = False


def compare_link_sets(stored: list[str], current: list[str]) -> TocComparison:
    """Ordered comparison that accepts growth at either end.

    Reverse-chronological serials prepend new chapters, and treating that as a
    reorder would demand --force for the single most common update pattern on
    the sites this tool targets. Removals and interior reordering stay
    ambiguous, because resuming into a re-indexed TOC would mismatch chapter
    numbers against filenames already on disk.
    """
    if not stored:
        return TocComparison.IDENTICAL

    normal_stored = [normalize_url(url) for url in stored]
    normal_current = [normalize_url(url) for url in current]

    if normal_stored == normal_current:
        return TocComparison.IDENTICAL
    if len(normal_current) < len(normal_stored):
        return TocComparison.DIVERGED
    if normal_current[: len(normal_stored)] == normal_stored:
        return TocComparison.GREW_AT_END
    if normal_current[-len(normal_stored) :] == normal_stored:
        return TocComparison.GREW_AT_START
    return TocComparison.DIVERGED


def plan_resume(
    output_dir: Path,
    *,
    toc_url: str,
    selectors: SelectorSet,
    current_links: list[str],
    force: bool = False,
) -> ResumePlan | None:
    """Decide what an existing checkpoint means. None when there is nothing to resume."""
    existing = Checkpoint.load(output_dir)
    if existing is None:
        return None

    if force:
        log.info("--force: discarding checkpoint at %s", existing.path)
        existing.discard()
        return None

    expected = fingerprint(toc_url, selectors)
    if existing.fingerprint != expected:
        return ResumePlan(
            checkpoint=existing,
            comparison=TocComparison.DIVERGED,
            already_done=len(existing.completed),
            usable=False,
            refusal=_describe_extraction_change(existing, toc_url, selectors),
        )

    comparison = compare_link_sets(existing.link_set, current_links)
    stored_normal = {normalize_url(url) for url in existing.link_set}
    appended = tuple(url for url in current_links if normalize_url(url) not in stored_normal)

    if comparison is TocComparison.DIVERGED:
        return ResumePlan(
            checkpoint=existing,
            comparison=comparison,
            already_done=len(existing.completed),
            usable=False,
            refusal=_describe_toc_change(existing.link_set, current_links),
        )

    return ResumePlan(
        checkpoint=existing,
        comparison=comparison,
        already_done=len(existing.completed),
        appended=appended,
        renumbering=comparison is TocComparison.GREW_AT_START and bool(existing.completed),
    )


def _describe_extraction_change(existing: Checkpoint, toc_url: str, selectors: SelectorSet) -> str:
    """Say which input changed, not that one of several might have.

    The first version listed the possibilities and left the reader to work it
    out. Storing the selectors alongside their hash costs nothing and turns a
    refusal into an instruction.
    """
    changes: list[str] = []
    if normalize_url(existing.toc_url) != normalize_url(toc_url):
        changes.append(f"TOC URL was {existing.toc_url!r}, now {toc_url!r}")

    stored = existing.selectors
    for name, current in (
        ("link", selectors.link),
        ("title", selectors.title),
        ("content", selectors.content),
    ):
        was = stored.get(name)
        if was is not None and was != current:
            changes.append(f"--{name} was {was!r}, now {current!r}")

    if not changes:
        # Older checkpoints predate the stored selectors; say so rather than
        # inventing a diff.
        changes.append("the stored run used a different TOC URL or different selectors")

    return (
        "this is not the same extraction (" + "; ".join(changes) + "), so the "
        "files already written do not match what this run would produce; "
        "pass --force to start over"
    )


def _describe_toc_change(stored: list[str], current: list[str]) -> str:
    """Say what moved, so the reader can decide whether --force is safe."""
    stored_normal = [normalize_url(url) for url in stored]
    current_normal = [normalize_url(url) for url in current]
    missing = [url for url in stored_normal if url not in set(current_normal)]
    added = [url for url in current_normal if url not in set(stored_normal)]

    parts = [f"stored {len(stored)} link(s), found {len(current)}"]
    if missing:
        parts.append(f"{len(missing)} no longer present, first {missing[0]}")
    if added:
        parts.append(f"{len(added)} new")
    if not missing and not added:
        parts.append("same links in a different order")

    return (
        "the table of contents changed in a way that is not simple growth ("
        + "; ".join(parts)
        + "); chapter numbers would no longer line up with the files already "
        "written, so pass --force to start over"
    )


def announce(plan: ResumePlan) -> None:
    """Say what is about to happen, prominently enough that nobody is surprised."""
    if not plan.usable:
        log.warning("refusing to resume: %s", plan.refusal)
        log.warning("checkpoint: %s", plan.checkpoint.path)
        return

    log.info(
        "Resuming: %d chapter(s) already fetched, state file %s",
        plan.already_done,
        plan.checkpoint.path,
    )
    if plan.appended:
        log.info("The table of contents grew by %d chapter(s) since that run.", len(plan.appended))
    if plan.renumbering:
        # The trap in accepting a prepend. Output names come from index, and
        # already-fetched chapters keep the name recorded in the checkpoint, so
        # after a prepend the numbering reflects fetch order rather than TOC
        # order. Better said here than discovered in the output folder.
        log.warning(
            "New chapters were added to the start of the table of contents. "
            "Files already written keep their original numbers, so numbering now "
            "reflects the order chapters were fetched, not their order in the "
            "table of contents. Use --force for a run numbered by the current "
            "table of contents."
        )
