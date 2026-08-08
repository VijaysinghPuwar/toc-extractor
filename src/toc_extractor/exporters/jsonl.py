"""A machine-readable record of what the run actually did.

One JSON object per line. Chapters first, then a single summary object, so a
consumer can stream chapters without buffering and still find the accounting
at the end.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from ..models import ChapterRecord, PriorChapter, RunResult

MANIFEST_NAME = "manifest.jsonl"


class JsonlExporter:
    name = "jsonl"

    def __init__(
        self,
        output_dir: Path,
        *,
        resumed: Mapping[int, PriorChapter] | None = None,
        **_: object,
    ) -> None:
        self._dir = output_dir
        self._resumed = dict(resumed or {})
        self._lines: list[dict[str, object]] = []

    async def open(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

    async def write(self, record: ChapterRecord) -> None:
        entry: dict[str, object] = {
            "type": "chapter",
            "index": record.index,
            "url": record.requested_url,
            "final_url": record.final_url,
            "redirected": record.redirected,
            "title": record.title,
            "fetched_at": record.fetched_at.isoformat(),
            "attempts": record.attempts,
            "bytes": record.byte_count,
            "sha256": record.sha256,
            # The count exists because v1 deleted URLs from prose with no
            # record. The deletion is unchanged; the silence is not.
            "stripped_urls": record.stripped_urls,
        }
        if record.robots is not None:
            entry.update(record.robots.as_manifest_entry())
        self._lines.append(entry)

    async def close(self, result: RunResult) -> None:
        # Chapters an earlier run wrote. Included so the manifest describes the
        # whole book rather than only the tail a resume happened to fetch; they
        # carry from_checkpoint so the thinner record is visible, not implied.
        seen = {int(line["index"]) for line in self._lines}  # type: ignore[call-overload]
        for index, prior in self._resumed.items():
            if index in seen:
                continue
            self._lines.append(
                {
                    "type": "chapter",
                    "from_checkpoint": True,
                    "index": prior.index,
                    "url": prior.url,
                    "final_url": prior.url,
                    "title": prior.title,
                    "fetched_at": prior.fetched_at,
                    "bytes": prior.bytes,
                    "sha256": prior.sha256,
                    "stripped_urls": prior.stripped_urls,
                }
            )

        summary: dict[str, object] = {
            "type": "summary",
            "toc_url": result.toc_url,
            "raw_links": result.collection.raw_count,
            "kept": len(result.collection.kept),
            "truncated": result.collection.truncated,
            "completed": len(result.completed),
            "chapters_total": len(self._lines),
            "skipped_resumed": len(result.skipped_resumed),
            "rejected": result.collection.reason_counts(),
            "rejected_detail": [
                {"url": item.value, "reason": item.reason.value, "detail": item.detail}
                for item in result.collection.rejected
            ],
            "failed": [
                {
                    "index": failure.index,
                    "url": failure.url,
                    "reason": failure.reason,
                    "attempts": failure.attempts,
                }
                for failure in result.failed
            ],
            "total_stripped_urls": result.total_stripped_urls,
        }

        self._lines.sort(key=lambda entry: entry.get("index", 0))  # type: ignore[arg-type,return-value]
        path = self._dir / MANIFEST_NAME
        with path.open("w", encoding="utf-8") as handle:
            for line in [*self._lines, summary]:
                handle.write(json.dumps(line, ensure_ascii=False) + "\n")
