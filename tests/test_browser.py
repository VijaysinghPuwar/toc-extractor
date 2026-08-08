"""Browser-backed PageSource tests.

Deliberately few. Everything decidable without a browser is decided against
the stub; these cover only what a stub cannot model - real navigation, real
redirect handling by Chromium, and real DOM behaviour.

The fixture server is real HTTP because Chromium has to speak to something,
and because a redirect that Chromium follows internally is the whole point of
the guard tests.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator, Sequence
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

import pytest

from toc_extractor.browser import BrowserPageSource
from toc_extractor.pagesource import PageBlocked, SelectorNotFound
from toc_extractor.politeness import RejectionReason, UrlGuard, UrlVerdict

pytestmark = pytest.mark.browser

CHAPTER_HTML = """<!doctype html><html><body>
<h1 class="t">Chapter Title</h1>
<article class="c">Chapter body text.</article>
</body></html>"""

SVG_TOC_HTML = """<!doctype html><html><body>
<a class="ch" href="/ch/1">plain anchor</a>
<div class="ch" href="/ch/2">div with href</div>
<svg xmlns="http://www.w3.org/2000/svg"><a class="ch" href="/ch/3"><text>svg</text></a></svg>
</body></html>"""

HYDRATED_HTML = """<!doctype html><html><body>
<h1 class="t">Late Title</h1>
<div id="mount"></div>
<script>
setTimeout(function () {
  var el = document.createElement('article');
  el.className = 'c';
  el.textContent = 'Hydrated body.';
  document.getElementById('mount').appendChild(el);
}, 300);
</script>
</body></html>"""


class Handler(BaseHTTPRequestHandler):
    # Shared across instances on purpose: the server makes one handler per
    # request, so per-instance state could not record a redirect chain.
    reached: ClassVar[list[str]] = []

    def do_GET(self) -> None:
        Handler.reached.append(self.path)
        if self.path == "/redirect/in-policy":
            self._redirect("/redirect/hop2")
        elif self.path == "/redirect/hop2":
            self._redirect("/chapter")
        elif self.path == "/redirect/to-private":
            self._redirect("http://169.254.169.254/latest/meta-data/")
        elif self.path == "/redirect/to-file":
            self._redirect("file:///etc/passwd")
        elif self.path == "/toc/svg":
            self._html(SVG_TOC_HTML)
        elif self.path == "/hydrated":
            self._html(HYDRATED_HTML)
        else:
            self._html(CHAPTER_HTML)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _html(self, body: str) -> None:
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args: object) -> None:
        return None


@pytest.fixture(scope="module")
def server() -> Iterator[str]:
    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


@pytest.fixture(autouse=True)
def clear_reached() -> None:
    Handler.reached.clear()


def loopback_permitted() -> UrlGuard:
    """The fixture server is on 127.0.0.1, which the guard rejects by default.

    Permitting private hosts here is what makes the scheme and redirect tests
    meaningful: they must block for their own reasons, not because the whole
    fixture server is unreachable.
    """
    return UrlGuard(allow_private_hosts=True)


def only_the_fixture_origin(origin: str) -> UrlGuard:
    """Permit the fixture server, apply the real policy to everything else.

    The fixture server lives on 127.0.0.1, which the guard rejects as a private
    literal before any redirect is followed. Blanket allow_private_hosts=True
    would also permit the redirect target and prove nothing, so the origin gets
    an explicit exemption and every other host faces the ordinary check.
    """

    class FixtureGuard(UrlGuard):
        def check(self, candidate: object) -> UrlVerdict:
            if isinstance(candidate, str) and candidate.startswith(origin):
                return UrlVerdict(url=candidate, allowed=True)
            return super().check(candidate)

    def resolver(host: str) -> Sequence[str]:
        return ["93.184.216.34"]

    return FixtureGuard(allow_private_hosts=False, resolver=resolver)


async def test_loads_a_chapter_and_reads_both_fields(server: str) -> None:
    async with BrowserPageSource(guard=loopback_permitted()) as source:
        page = await source.load_chapter(
            f"{server}/chapter", title_selector="h1.t", content_selector="article.c"
        )
    assert page.title == "Chapter Title"
    assert page.body == "Chapter body text."


async def test_file_url_as_a_chapter_link_is_refused(server: str) -> None:
    """v1 read this off disk and wrote it into the output folder as chapter text."""
    async with BrowserPageSource(guard=loopback_permitted()) as source:
        with pytest.raises(PageBlocked) as caught:
            await source.load_chapter(
                "file:///etc/passwd", title_selector="h1", content_selector="article"
            )
    assert caught.value.reason is RejectionReason.DISALLOWED_SCHEME


async def test_in_policy_redirect_chain_loads_and_reports_the_final_url(server: str) -> None:
    """Ordinary redirects must keep working; the guard is not allowed to break them."""
    async with BrowserPageSource(guard=loopback_permitted()) as source:
        page = await source.load_chapter(
            f"{server}/redirect/in-policy",
            title_selector="h1.t",
            content_selector="article.c",
        )

    assert page.requested_url == f"{server}/redirect/in-policy"
    assert page.final_url == f"{server}/chapter"
    assert page.title == "Chapter Title"
    assert Handler.reached == ["/redirect/in-policy", "/redirect/hop2", "/chapter"]


async def test_redirect_to_a_private_address_never_reaches_the_target(server: str) -> None:
    """The case a pre-flight string check cannot catch.

    The requested URL is permitted; the third hop is not. Only a check at the
    request layer sees it.
    """
    async with BrowserPageSource(guard=only_the_fixture_origin(server)) as source:
        with pytest.raises(PageBlocked) as caught:
            await source.load_chapter(
                f"{server}/redirect/to-private",
                title_selector="h1.t",
                content_selector="article.c",
            )

    assert caught.value.reason is RejectionReason.PRIVATE_ADDRESS
    assert "169.254.169.254" in caught.value.url
    assert "/latest/meta-data/" not in "".join(Handler.reached)


async def test_redirect_to_a_file_url_is_refused(server: str) -> None:
    async with BrowserPageSource(guard=loopback_permitted()) as source:
        with pytest.raises(PageBlocked) as caught:
            await source.load_chapter(
                f"{server}/redirect/to-file",
                title_selector="h1.t",
                content_selector="article.c",
            )
    assert caught.value.reason is RejectionReason.DISALLOWED_SCHEME


async def test_svg_and_div_links_survive_in_page_resolution(server: str) -> None:
    """The v1 silent-drop bug, end to end through the real collector."""
    async with BrowserPageSource(guard=loopback_permitted()) as source:
        toc = await source.load_toc(f"{server}/toc/svg", link_selector=".ch")

    assert all(isinstance(value, str) for value in toc.raw_links)
    assert toc.raw_links == (
        f"{server}/ch/1",
        f"{server}/ch/2",
        f"{server}/ch/3",
    )


async def test_wait_for_selector_survives_late_hydration(server: str) -> None:
    """Why SelectorNotFound is correct to never retry.

    The content element appears 300ms after domcontentloaded. A bare read
    would raise SelectorNotFound on a page that was about to be fine, and the
    fetch loop is told never to retry that.
    """
    async with BrowserPageSource(guard=loopback_permitted()) as source:
        page = await source.load_chapter(
            f"{server}/hydrated", title_selector="h1.t", content_selector="article.c"
        )
    assert page.body == "Hydrated body."


async def test_genuinely_absent_selector_raises_after_the_timeout(server: str) -> None:
    async with BrowserPageSource(guard=loopback_permitted(), navigation_timeout_ms=1_000) as source:
        with pytest.raises(SelectorNotFound):
            await source.load_chapter(
                f"{server}/chapter",
                title_selector="h1.t",
                content_selector="div.nothing-matches-this",
            )


async def test_capture_writes_html_and_screenshot(server: str, tmp_path: Path) -> None:
    shot = tmp_path / "toc.png"
    async with BrowserPageSource(guard=loopback_permitted()) as source:
        toc = await source.load_toc(
            f"{server}/toc/svg", link_selector=".ch", capture_html=True, screenshot_path=shot
        )

    assert toc.html is not None
    assert "svg" in toc.html
    assert shot.exists() and shot.stat().st_size > 0


async def test_custom_user_agent_is_applied(server: str) -> None:
    async with (
        BrowserPageSource(guard=loopback_permitted(), user_agent="TOCExtractor/test") as source,
        source._acquire() as slot,
    ):
        agent = await slot.page.evaluate("() => navigator.userAgent")
    assert agent == "TOCExtractor/test"


async def test_concurrent_chapter_loads_do_not_abort_each_other(server: str) -> None:
    """Two goto() calls on one page abort each other with net::ERR_ABORTED.

    Found by an end-to-end run, not by the suite: the stub serves from a dict
    and has no notion of a page being busy, so it cannot model this at all.
    One page per worker is the fix.
    """
    import asyncio

    async with BrowserPageSource(guard=loopback_permitted(), max_pages=4) as source:
        pages = await asyncio.gather(
            *(
                source.load_chapter(
                    f"{server}/chapter", title_selector="h1.t", content_selector="article.c"
                )
                for _ in range(8)
            )
        )

    assert len(pages) == 8
    assert all(page.title == "Chapter Title" for page in pages)


async def test_a_single_page_pool_still_serialises_correctly(server: str) -> None:
    import asyncio

    async with BrowserPageSource(guard=loopback_permitted(), max_pages=1) as source:
        pages = await asyncio.gather(
            *(
                source.load_chapter(
                    f"{server}/chapter", title_selector="h1.t", content_selector="article.c"
                )
                for _ in range(4)
            )
        )
    assert all(page.body == "Chapter body text." for page in pages)
