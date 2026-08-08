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
