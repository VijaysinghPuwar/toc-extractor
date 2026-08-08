"""The stub must honour the PageSource contract, or every test using it lies."""

from __future__ import annotations

from pathlib import Path

import pytest

from toc_extractor.pagesource import (
    CaptureUnsupported,
    PageBlocked,
    PageError,
    PageSource,
    PageTimeout,
    SelectorNotFound,
)
from toc_extractor.politeness import RejectionReason

from .stub import StubPage, StubPageSource

TOC = "https://example.com/toc"
CH1 = "https://example.com/ch/1"


def make_source(**kwargs: object) -> StubPageSource:
    pages = {
        TOC: StubPage(links=[CH1]),
        CH1: StubPage(title="First", body="Body one."),
    }
    return StubPageSource(pages, **kwargs)  # type: ignore[arg-type]


def test_stub_satisfies_the_protocol() -> None:
    source: PageSource = make_source()
    assert source is not None


async def test_load_toc_returns_raw_links_untouched() -> None:
    """Non-string values must survive to parser.py, which does the counting."""
    source = StubPageSource({TOC: StubPage(links=[CH1, {}, None, ""])})
    page = await source.load_toc(TOC, link_selector="a")
    assert page.raw_links == (CH1, {}, None, "")


async def test_load_chapter_returns_fields() -> None:
    source = make_source()
    page = await source.load_chapter(CH1, title_selector="h1", content_selector="article")
    assert page.title == "First"
    assert page.body == "Body one."
    assert page.final_url == CH1


async def test_redirects_are_followed_and_final_url_reported() -> None:
    source = StubPageSource(
        {
            "https://example.com/old": StubPage(redirect_to="https://example.com/new"),
            "https://example.com/new": StubPage(title="Moved", body="Here."),
        }
    )
    page = await source.load_chapter(
        "https://example.com/old", title_selector="h1", content_selector="article"
    )
    assert page.requested_url == "https://example.com/old"
    assert page.final_url == "https://example.com/new"


async def test_redirect_loop_is_an_error_not_a_hang() -> None:
    source = StubPageSource(
        {
            "https://example.com/a": StubPage(redirect_to="https://example.com/b"),
            "https://example.com/b": StubPage(redirect_to="https://example.com/a"),
        }
    )
    with pytest.raises(PageError, match="redirect loop"):
        await source.load_chapter(
            "https://example.com/a", title_selector="h1", content_selector="article"
        )


async def test_fail_times_exhausts_then_succeeds() -> None:
    source = StubPageSource({CH1: StubPage(fail_times=2, title="Late")})
    for _ in range(2):
        with pytest.raises(PageTimeout):
            await source.load_chapter(CH1, title_selector="h1", content_selector="article")
    page = await source.load_chapter(CH1, title_selector="h1", content_selector="article")
    assert page.title == "Late"
    assert source.attempts_for(CH1) == 3


async def test_missing_selector_is_distinct_from_timeout() -> None:
    """A page that loaded but lacks the selector must not burn retries."""
    source = StubPageSource({CH1: StubPage(missing_selector=True)})
    with pytest.raises(SelectorNotFound):
        await source.load_chapter(CH1, title_selector="h1", content_selector="article")


async def test_capture_is_refused_with_a_stated_reason() -> None:
    source = make_source()
    with pytest.raises(CaptureUnsupported, match="browser-backed"):
        await source.load_toc(TOC, link_selector="a", capture_html=True)
    with pytest.raises(CaptureUnsupported):
        await source.load_toc(TOC, link_selector="a", screenshot_path=Path("/tmp/x.png"))


async def test_capture_works_when_the_source_claims_support() -> None:
    source = make_source(supports_capture=True)
    page = await source.load_toc(TOC, link_selector="a", capture_html=True)
    assert page.html == "<html></html>"


async def test_unregistered_url_is_an_error() -> None:
    source = make_source()
    with pytest.raises(PageError, match="no stub page registered"):
        await source.load_chapter(
            "https://example.com/nope", title_selector="h1", content_selector="article"
        )


async def test_loads_are_timestamped_against_the_injected_clock() -> None:
    now = [0.0]
    source = StubPageSource({CH1: StubPage()}, clock=lambda: now[0])
    await source.load_chapter(CH1, title_selector="h1", content_selector="article")
    now[0] = 5.0
    await source.load_chapter(CH1, title_selector="h1", content_selector="article")
    assert source.load_times_for(CH1) == [0.0, 5.0]


async def test_aclose_is_recorded() -> None:
    source = make_source()
    await source.aclose()
    assert source.closed


def test_page_blocked_names_the_offending_hop() -> None:
    """With a redirect chain the requested URL is not the actionable one."""
    error = PageBlocked(
        "http://169.254.169.254/latest/",
        RejectionReason.PRIVATE_ADDRESS,
        "link-local",
    )
    assert "169.254.169.254" in str(error)
    assert "private_address" in str(error)
    assert error.reason is RejectionReason.PRIVATE_ADDRESS


# ---------------------------------------------------------------------------
# Exclusivity: the constraint that made the page-sharing bug invisible
# ---------------------------------------------------------------------------


async def test_stub_refuses_concurrent_loads_on_one_page() -> None:
    """A real page aborts the earlier navigation when a second goto() starts.

    The stub had no notion of a busy resource, so BrowserPageSource driving one
    shared page from three workers passed 447 tests and failed on the first
    live run with net::ERR_ABORTED. Modelling the limit makes the stub able to
    fail the way the real thing does.
    """
    import asyncio

    source = StubPageSource({CH1: StubPage(hang=0.02)}, max_concurrent=1)
    results = await asyncio.gather(
        *(
            source.load_chapter(CH1, title_selector="h1", content_selector="article")
            for _ in range(3)
        ),
        return_exceptions=True,
    )

    failures = [r for r in results if isinstance(r, PageError)]
    assert failures, "one page must not serve three concurrent loads"
    assert "concurrent loads" in str(failures[0])


async def test_stub_permits_concurrency_up_to_its_page_count() -> None:
    import asyncio

    source = StubPageSource({CH1: StubPage(hang=0.02)}, max_concurrent=3)
    results = await asyncio.gather(
        *(
            source.load_chapter(CH1, title_selector="h1", content_selector="article")
            for _ in range(3)
        )
    )
    assert len(results) == 3
    assert source.max_observed_concurrency == 3


async def test_fetcher_over_a_single_page_stub_surfaces_the_conflict() -> None:
    """End to end: the shape of the bug that reached production.

    Concurrency 3 against a source that only has one page must not quietly
    succeed.
    """
    import asyncio

    from toc_extractor.fetcher import Fetcher, FetchOptions
    from toc_extractor.parser import SelectorSet
    from toc_extractor.politeness import RateLimiter, UrlGuard
    from toc_extractor.sinks import NullSink

    urls = [f"https://example.com/ch/{i}" for i in range(1, 5)]
    pages = {TOC: StubPage(links=urls)}
    for url in urls:
        pages[url] = StubPage(hang=0.02)

    source = StubPageSource(pages, max_concurrent=1)
    fetcher = Fetcher(
        source,
        guard=UrlGuard(resolver=lambda _h: ["93.184.216.34"]),
        sink=NullSink(),
        options=FetchOptions(concurrency=3, retries=0, min_delay=0.0, wait_after_load=0.0),
        limiter=RateLimiter(min_interval=0.0),
    )
    result = await fetcher.run(TOC, SelectorSet.create(link="a", title="h1", content="article"))

    assert result.failed, "driving one page from three workers must not look healthy"
    assert asyncio is not None
