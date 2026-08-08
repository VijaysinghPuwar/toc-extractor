"""Command line entry point.

Every flag cli_runner.py had behaves identically, because a rewrite that
quietly changes what a flag means is worse than one that keeps the flag.
argparse rather than a third-party library: the parity requirement is written
against argparse's semantics, and build_parser() is importable so the README's
CLI reference is generated from it and cannot drift.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .browser import BrowserPageSource
from .checkpoint import Checkpoint, announce, fingerprint, plan_resume
from .fetcher import Fetcher, FetchOptions
from .logging import configure, get_logger
from .models import ChapterRecord, RunResult
from .pagesource import PageError
from .parser import SelectorSet
from .politeness import RateLimiter, RobotsFetcher, build_url_guard, fetch_robots, origin_of
from .sinks import NullSink, Sink, TextSink

log = get_logger("cli")

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FAILED = 1


def build_parser() -> argparse.ArgumentParser:
    """The single source of truth for the CLI surface.

    Importable on purpose: the README's reference section is generated from
    format_help() and a test asserts the two agree.
    """
    parser = argparse.ArgumentParser(
        prog="toc-extractor",
        description=(
            "Extract chapter text from a table-of-contents page using CSS "
            "selectors you supply. Use only on content you own or are "
            "permitted to access."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    source = parser.add_argument_group("source and selectors")
    source.add_argument("--toc", required=True, help="TOC URL (must start with http/https)")
    source.add_argument(
        "--link", required=True, help="CSS selector for chapter links on the TOC page"
    )
    source.add_argument("--title", required=True, help="CSS selector for title on a chapter page")
    source.add_argument(
        "--content", required=True, help="CSS selector for content on a chapter page"
    )

    output = parser.add_argument_group("output")
    output.add_argument("--max", type=int, default=20, help="Max chapters to fetch (default: 20)")
    output.add_argument("--out", default="downloads", help="Output folder (default: downloads)")
    output.add_argument(
        "--include-links", action="store_true", help="Include source URL in saved files"
    )
    output.add_argument(
        "--no-strip-ads",
        action="store_true",
        help="Do NOT strip common ad markers from text",
    )

    behaviour = parser.add_argument_group("behaviour")
    behaviour.add_argument(
        "--dry-run", action="store_true", help="List discovered chapter URLs and exit"
    )
    behaviour.add_argument(
        "--force",
        action="store_true",
        help="Discard any saved progress and start over (resume is the default)",
    )

    debug = parser.add_argument_group("debugging")
    debug.add_argument("--dump-html", action="store_true", help="Save TOC HTML to out/toc.html")
    debug.add_argument(
        "--screenshot", action="store_true", help="Save TOC screenshot to out/toc.png"
    )
    debug.add_argument("-v", "--verbose", action="store_true", help="Show debug output")
    debug.add_argument("-q", "--quiet", action="store_true", help="Show warnings and errors only")

    browser = parser.add_argument_group("browser")
    browser.add_argument("--ua", default=None, help="Custom User-Agent string")
    browser.add_argument(
        "--storage-state",
        default=None,
        help="Path to Playwright storage state JSON (reuses login)",
    )
    browser.add_argument(
        "--headful", action="store_true", help="Run headed (GUI). Default is headless."
    )
    browser.add_argument(
        "--timeout", type=int, default=25000, help="Navigation timeout ms (default: 25000)"
    )

    politeness = parser.add_argument_group("politeness")
    politeness.add_argument(
        "--min-delay", type=float, default=1.2, help="Min delay between chapters (s)"
    )
    politeness.add_argument(
        "--max-delay", type=float, default=2.5, help="Max delay between chapters (s)"
    )
    politeness.add_argument(
        "--retries", type=int, default=2, help="Retries per chapter on errors (default: 2)"
    )
    politeness.add_argument(
        "--wait-after-load", type=int, default=500, help="Extra settle wait per page (ms)"
    )
    politeness.add_argument(
        "--concurrency",
        type=int,
        default=3,
        help="Chapters fetched at once (default: 3). The per-host delay still applies.",
    )
    politeness.add_argument(
        "--allow-private-hosts",
        action="store_true",
        help="Permit hosts resolving to loopback or private ranges (for local testing)",
    )

    return parser


def options_from(args: argparse.Namespace) -> FetchOptions:
    """Translate parsed arguments into FetchOptions.

    --timeout and --wait-after-load stay milliseconds on the command line
    because that is what v1 accepted; they become seconds here, where
    everything else is seconds.
    """
    out = Path(args.out)
    return FetchOptions(
        concurrency=max(1, args.concurrency),
        retries=max(0, args.retries),
        timeout=max(0.001, args.timeout / 1000.0),
        min_delay=max(0.0, args.min_delay),
        max_delay=max(max(0.0, args.min_delay), args.max_delay),
        wait_after_load=max(0.0, args.wait_after_load / 1000.0),
        include_links=args.include_links,
        strip_ads=not args.no_strip_ads,
        max_links=max(1, args.max),
        dry_run=args.dry_run,
        capture_html=args.dump_html,
        screenshot_path=(out / "toc.png") if args.screenshot else None,
    )


async def run(
    args: argparse.Namespace,
    *,
    source_factory: object | None = None,
    robots_fetcher: RobotsFetcher | None = None,
) -> int:
    """Execute one extraction. Returns the process exit code."""
    selectors = SelectorSet.create(link=args.link, title=args.title, content=args.content)
    if not selectors.complete:
        log.error("missing selectors: %s", ", ".join(selectors.missing))
        return EXIT_USAGE

    output_dir = Path(args.out)
    options = options_from(args)
    guard = build_url_guard(allow_private_hosts=args.allow_private_hosts)

    verdict = guard.check(args.toc)
    if not verdict.allowed:
        log.error("refusing to fetch %s: %s %s", args.toc, verdict.reason, verdict.detail)
        return EXIT_USAGE

    robots = fetch_robots(args.toc, fetcher=robots_fetcher)
    if not robots.fetched:
        log.debug("no robots.txt at %s; proceeding", origin_of(args.toc))

    limiter = RateLimiter(min_interval=options.min_delay)
    if robots.crawl_delay is not None:
        host = origin_of(args.toc).split("//", 1)[-1]
        limiter.set_host_interval(host, robots.crawl_delay)
        log.info("robots.txt requests a %.1fs crawl delay; honouring it", robots.crawl_delay)

    if source_factory is None:
        source: object = BrowserPageSource(
            guard=guard,
            headless=not args.headful,
            user_agent=args.ua,
            storage_state=Path(args.storage_state) if args.storage_state else None,
            navigation_timeout_ms=args.timeout,
            # One page per worker. Two concurrent goto() calls on a shared page
            # abort each other, which the stub cannot model and only a live
            # server exposed.
            max_pages=options.concurrency,
        )
        await source.start()  # type: ignore[attr-defined]
    else:
        source = source_factory()  # type: ignore[operator]

    sink: Sink = (
        NullSink() if args.dry_run else TextSink(output_dir, include_links=args.include_links)
    )

    try:
        return await _extract(
            args,
            source=source,
            sink=sink,
            guard=guard,
            robots=robots,
            limiter=limiter,
            options=options,
            selectors=selectors,
            output_dir=output_dir,
        )
    finally:
        await source.aclose()  # type: ignore[attr-defined]


async def _extract(
    args: argparse.Namespace,
    *,
    source: object,
    sink: Sink,
    guard: object,
    robots: object,
    limiter: RateLimiter,
    options: FetchOptions,
    selectors: SelectorSet,
    output_dir: Path,
) -> int:
    checkpoint: Checkpoint | None = None

    def persist(record: ChapterRecord) -> None:
        if checkpoint is None:
            return
        checkpoint.record(record, f"{record.index:03d} - {record.title}.txt")
        checkpoint.save()

    fetcher = Fetcher(
        source,  # type: ignore[arg-type]
        guard=guard,  # type: ignore[arg-type]
        sink=sink,
        options=options,
        limiter=limiter,
        robots=robots,  # type: ignore[arg-type]
        on_record=persist,
    )

    try:
        collected = await fetcher.collect(args.toc, selectors)
    except PageError as exc:
        log.error("could not read the table of contents: %s", exc)
        return EXIT_FAILED

    _report_collection(collected)

    if options.dry_run:
        for position, url in enumerate(collected.kept, start=1):
            print(f"{position:03d}  {url}")
        return EXIT_OK

    if args.dump_html and collected.toc.html is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "toc.html").write_text(collected.toc.html, encoding="utf-8")

    plan = plan_resume(
        output_dir,
        toc_url=args.toc,
        selectors=selectors,
        current_links=collected.kept,
        force=args.force,
    )
    already_done = None
    if plan is not None:
        announce(plan)
        if not plan.usable:
            return EXIT_USAGE
        checkpoint = plan.checkpoint
        already_done = checkpoint.is_done

    if checkpoint is None:
        checkpoint = Checkpoint(
            path=Checkpoint.path_for(output_dir),
            toc_url=args.toc,
            fingerprint=fingerprint(args.toc, selectors),
            selectors={
                "link": selectors.link,
                "title": selectors.title,
                "content": selectors.content,
            },
            link_set=collected.kept,
        )
    else:
        checkpoint.link_set = collected.kept

    result = await fetcher.fetch(collected, selectors, already_done=already_done)
    checkpoint.save()
    _report_result(result, output_dir)
    return EXIT_FAILED if result.failed else EXIT_OK


def _report_collection(collected: object) -> None:
    collection = collected.collection  # type: ignore[attr-defined]
    log.info("Found %d chapter link(s).", len(collection.kept))
    for reason, count in sorted(collection.reason_counts().items()):
        log.warning("skipped %d link(s): %s", count, reason)
    if collection.truncated:
        log.info("Ignoring %d link(s) beyond --max.", collection.truncated)


def _report_result(result: RunResult, output_dir: Path) -> None:
    log.info("Wrote %d chapter(s) to %s", len(result.completed), output_dir)
    if result.total_stripped_urls:
        log.info(
            "Removed %d URL(s) from chapter text. Pass --include-links to keep them.",
            result.total_stripped_urls,
        )
    for failure in result.failed:
        log.warning(
            "chapter %d failed after %d attempt(s): %s",
            failure.index,
            failure.attempts,
            failure.detail,
        )


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    configure(verbose=args.verbose, quiet=args.quiet)
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        # Progress up to this point is already on disk: the checkpoint is
        # written after every chapter, not at the end.
        log.warning("interrupted; rerun the same command to resume")
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
