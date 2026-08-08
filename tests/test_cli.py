"""CLI parity with cli_runner.py, one test per flag.

The flag list is read out of the v1 script rather than typed here, so a flag
that existed in v1 and was forgotten in v2 fails this file instead of being
discovered by someone whose script stopped working.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from toc_extractor.checkpoint import STATE_FILENAME, Checkpoint
from toc_extractor.cli import EXIT_OK, EXIT_USAGE, build_parser, options_from, run

from .stub import StubPage, StubPageSource

REPO_ROOT = Path(__file__).resolve().parents[1]
V1_SCRIPT = REPO_ROOT / "cli_runner.py"

TOC = "https://example.com/toc"
BASE = ["--toc", TOC, "--link", "a.ch", "--title", "h1", "--content", "article"]


def v1_flags() -> set[str]:
    """Every long option cli_runner.py accepted."""
    if not V1_SCRIPT.exists():
        pytest.skip("v1 script has been removed; parity is now historical")
    return set(re.findall(r'ap\.add_argument\(\s*"(--[a-z-]+)"', V1_SCRIPT.read_text()))


def v2_flags() -> set[str]:
    found: set[str] = set()
    for action in build_parser()._actions:
        found.update(option for option in action.option_strings if option.startswith("--"))
    return found


def catalogue(count: int = 3, **overrides: StubPage) -> dict[str, StubPage]:
    urls = [f"https://example.com/ch/{i}" for i in range(1, count + 1)]
    pages = {TOC: StubPage(links=urls)}
    for position, url in enumerate(urls, start=1):
        pages[url] = StubPage(title=f"Chapter {position}", body=f"Body {position}.")
    pages.update(overrides)
    return pages


def parse(*extra: str):  # type: ignore[no-untyped-def]
    return build_parser().parse_args([*BASE, *extra])


async def invoke(tmp_path: Path, *extra: str, pages: dict[str, StubPage] | None = None):  # type: ignore[no-untyped-def]
    """Run the CLI against the stub, never a browser and never the network."""
    source = StubPageSource(pages or catalogue())
    args = build_parser().parse_args(
        [
            *BASE,
            "--out",
            str(tmp_path),
            "--min-delay",
            "0",
            "--max-delay",
            "0",
            # v1 defaulted to a 500ms settle per page; leaving it on would make
            # this file the slowest in the suite for no added coverage.
            "--wait-after-load",
            "0",
            *extra,
        ]
    )
    code = await run(
        args,
        source_factory=lambda: source,
        robots_fetcher=lambda _url: None,
    )
    return code, source


# ---------------------------------------------------------------------------
# Parity
# ---------------------------------------------------------------------------


def test_every_v1_flag_still_exists() -> None:
    missing = v1_flags() - v2_flags()
    assert missing == set(), f"v1 flags dropped in v2: {sorted(missing)}"


def test_v1_flag_count_is_what_we_think_it_is() -> None:
    """Guards against the parity test passing because the regex found nothing."""
    assert len(v1_flags()) == 19


@pytest.mark.parametrize(
    "flag,attribute,default",
    [
        ("--max", "max", 20),
        ("--out", "out", "downloads"),
        ("--include-links", "include_links", False),
        ("--no-strip-ads", "no_strip_ads", False),
        ("--dry-run", "dry_run", False),
        ("--dump-html", "dump_html", False),
        ("--screenshot", "screenshot", False),
        ("--ua", "ua", None),
        ("--storage-state", "storage_state", None),
        ("--headful", "headful", False),
        ("--timeout", "timeout", 25000),
        ("--min-delay", "min_delay", 1.2),
        ("--max-delay", "max_delay", 2.5),
        ("--retries", "retries", 2),
        ("--wait-after-load", "wait_after_load", 500),
    ],
)
def test_v1_defaults_are_unchanged(flag: str, attribute: str, default: object) -> None:
    """A rewrite that quietly changes a default is worse than one that drops the flag."""
    assert getattr(parse(), attribute) == default, flag


@pytest.mark.parametrize("flag", ["--toc", "--link", "--title", "--content"])
def test_required_flags_are_required(flag: str) -> None:
    argv = list(BASE)
    index = argv.index(flag)
    del argv[index : index + 2]
    with pytest.raises(SystemExit):
        build_parser().parse_args(argv)


# ---------------------------------------------------------------------------
# Each flag changes behaviour
# ---------------------------------------------------------------------------


def test_max_limits_links() -> None:
    assert options_from(parse("--max", "7")).max_links == 7


def test_retries_reaches_options() -> None:
    assert options_from(parse("--retries", "5")).retries == 5


def test_concurrency_reaches_options() -> None:
    assert options_from(parse("--concurrency", "9")).concurrency == 9


def test_timeout_converts_milliseconds_to_seconds() -> None:
    """v1 took ms and v2 works in seconds; the flag keeps its v1 units."""
    assert options_from(parse("--timeout", "4000")).timeout == pytest.approx(4.0)


def test_wait_after_load_converts_milliseconds_to_seconds() -> None:
    assert options_from(parse("--wait-after-load", "250")).wait_after_load == pytest.approx(0.25)


def test_no_strip_ads_inverts_strip_ads() -> None:
    assert options_from(parse()).strip_ads is True
    assert options_from(parse("--no-strip-ads")).strip_ads is False


def test_include_links_reaches_options() -> None:
    assert options_from(parse("--include-links")).include_links is True


def test_max_delay_cannot_be_below_min_delay() -> None:
    options = options_from(parse("--min-delay", "5", "--max-delay", "1"))
    assert options.max_delay == 5.0


def test_screenshot_sets_a_path_under_out() -> None:
    options = options_from(parse("--out", "somewhere", "--screenshot"))
    assert options.screenshot_path == Path("somewhere/toc.png")


def test_dump_html_sets_capture() -> None:
    assert options_from(parse("--dump-html")).capture_html is True


async def test_dry_run_lists_urls_and_writes_nothing(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    code, source = await invoke(tmp_path, "--dry-run")
    captured = capsys.readouterr()

    assert code == EXIT_OK
    assert "001  https://example.com/ch/1" in captured.out
    assert source.urls_loaded == [TOC]
    assert list(tmp_path.iterdir()) == []


async def test_include_links_writes_source_lines(tmp_path: Path) -> None:
    await invoke(tmp_path, "--include-links")
    body = (tmp_path / "001 - Chapter 1.txt").read_text(encoding="utf-8")
    assert "Source: https://example.com/ch/1" in body


async def test_out_directory_is_honoured(tmp_path: Path) -> None:
    target = tmp_path / "nested" / "place"
    await invoke(target)
    assert (target / "combined.txt").exists()


async def test_max_truncates_the_run(tmp_path: Path) -> None:
    await invoke(tmp_path, "--max", "2", pages=catalogue(5))
    written = sorted(p.name for p in tmp_path.glob("0*.txt"))
    assert written == ["001 - Chapter 1.txt", "002 - Chapter 2.txt"]


async def test_dump_html_writes_the_toc(tmp_path: Path) -> None:
    pages = catalogue()
    source = StubPageSource(pages, supports_capture=True)
    args = build_parser().parse_args(
        [*BASE, "--out", str(tmp_path), "--min-delay", "0", "--wait-after-load", "0", "--dump-html"]
    )
    await run(args, source_factory=lambda: source, robots_fetcher=lambda _u: None)
    assert (tmp_path / "toc.html").read_text(encoding="utf-8") == "<html></html>"


# ---------------------------------------------------------------------------
# Guard and robots wiring
# ---------------------------------------------------------------------------


async def test_a_private_toc_url_is_refused(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        ["--toc", "http://127.0.0.1/toc", "--link", "a", "--title", "h1", "--content", "p"]
    )
    assert await run(args, source_factory=lambda: StubPageSource({})) == EXIT_USAGE


async def test_a_file_toc_url_is_refused(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        ["--toc", "file:///etc/passwd", "--link", "a", "--title", "h1", "--content", "p"]
    )
    assert await run(args, source_factory=lambda: StubPageSource({})) == EXIT_USAGE


async def test_robots_disallow_blocks_chapters(tmp_path: Path) -> None:
    code, _ = await invoke(tmp_path, pages=catalogue(3))
    assert code == EXIT_OK

    source2 = StubPageSource(catalogue(3))
    args = build_parser().parse_args(
        [*BASE, "--out", str(tmp_path / "b"), "--min-delay", "0", "--wait-after-load", "0"]
    )
    await run(
        args,
        source_factory=lambda: source2,
        robots_fetcher=lambda _u: "User-agent: *\nDisallow: /ch/\n",
    )
    assert [u for u in source2.urls_loaded if u != TOC] == []


# ---------------------------------------------------------------------------
# Checkpoint wiring
# ---------------------------------------------------------------------------


async def test_a_completed_run_writes_a_checkpoint(tmp_path: Path) -> None:
    await invoke(tmp_path)
    payload = json.loads((tmp_path / STATE_FILENAME).read_text(encoding="utf-8"))
    assert set(payload["completed"]) == {
        "https://example.com/ch/1",
        "https://example.com/ch/2",
        "https://example.com/ch/3",
    }


async def test_rerunning_resumes_and_refetches_nothing(tmp_path: Path) -> None:
    """Resume is the default; a second run must not hammer the site again."""
    await invoke(tmp_path)
    _, second = await invoke(tmp_path)
    assert [u for u in second.urls_loaded if u != TOC] == []


async def test_force_refetches_everything(tmp_path: Path) -> None:
    await invoke(tmp_path)
    _, second = await invoke(tmp_path, "--force")
    assert len([u for u in second.urls_loaded if u != TOC]) == 3


async def test_growth_at_the_end_fetches_only_the_new_chapters(tmp_path: Path) -> None:
    await invoke(tmp_path, pages=catalogue(3))
    _, second = await invoke(tmp_path, pages=catalogue(5))
    fetched = [u for u in second.urls_loaded if u != TOC]
    assert fetched == ["https://example.com/ch/4", "https://example.com/ch/5"]


async def test_diverged_toc_refuses_with_usage_exit(tmp_path: Path) -> None:
    await invoke(tmp_path, pages=catalogue(5))
    code, second = await invoke(tmp_path, pages=catalogue(3))
    assert code == EXIT_USAGE
    assert [u for u in second.urls_loaded if u != TOC] == []


async def test_changing_a_selector_refuses_to_resume(tmp_path: Path) -> None:
    await invoke(tmp_path)
    source = StubPageSource(catalogue())
    args = build_parser().parse_args(
        [
            "--toc",
            TOC,
            "--link",
            "a.ch",
            "--title",
            "h1",
            "--content",
            "div.other",
            "--out",
            str(tmp_path),
            "--min-delay",
            "0",
            "--wait-after-load",
            "0",
        ]
    )
    code = await run(args, source_factory=lambda: source, robots_fetcher=lambda _u: None)
    assert code == EXIT_USAGE


async def test_checkpoint_records_the_output_name_not_just_the_url(tmp_path: Path) -> None:
    await invoke(tmp_path)
    checkpoint = Checkpoint.load(tmp_path)
    assert checkpoint is not None
    entry = checkpoint.completed["https://example.com/ch/2"]
    assert entry.output_name == "002 - Chapter 2.txt"
    assert entry.index == 2
