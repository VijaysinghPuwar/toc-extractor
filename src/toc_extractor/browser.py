"""The Playwright-backed PageSource.

The only module that imports Playwright. Everything it can raise is
translated into the pagesource vocabulary before it leaves.

The route handling here is more involved than a reader would expect, for a
measured reason. `page.route` fires **once per navigation**, not once per
redirect hop: Chromium follows redirects internally, so a handler that only
inspects the first request never sees where the chain actually ended. Neither
`route.fetch(...)` followed by `route.fulfill(...)` nor `page.on("request")`
fixes it — the former does not re-enter the handler, the latter observes every
hop but cannot block. So the redirect loop lives in the handler: fetch with
`max_redirects=0`, validate the Location target, repeat, and abort the moment
a hop is disallowed.

Two consequences fall out of that:

- `page.url` becomes wrong. The final body is fulfilled at the originally
  requested URL, so the browser never learns a redirect happened. The final
  URL is tracked in the handler and reported from there.
- Only navigations are fulfilled. Proxying every subresource would make
  cookie, encoding, and cache fidelity our problem for no security gain.
  Non-navigation requests are screened and aborted if disallowed, which needs
  no proxying at all.
"""

from __future__ import annotations

import contextlib
from collections.abc import Sequence
from pathlib import Path
from types import TracebackType
from typing import Any
from urllib.parse import urljoin

from playwright.async_api import Browser, BrowserContext, Page, Route, async_playwright
from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as PlaywrightTimeout

from .pagesource import (
    ChapterPage,
    PageBlocked,
    PageError,
    PageTimeout,
    SelectorNotFound,
    TocPage,
)
from .parser import LINK_COLLECTOR_JS
from .politeness import RejectionReason, UrlGuard

MAX_REDIRECT_HOPS = 20


class _GuardedNavigation:
    """Tracks one navigation's redirect chain and the verdict on each hop."""

    def __init__(self) -> None:
        self.final_url: str | None = None
        self.blocked: PageBlocked | None = None
        self.hops: list[str] = []


class BrowserPageSource:
    """Loads pages in a real Chromium context, with the URL guard at the request layer."""

    def __init__(
        self,
        *,
        guard: UrlGuard,
        headless: bool = True,
        user_agent: str | None = None,
        storage_state: Path | None = None,
        user_data_dir: Path | None = None,
        navigation_timeout_ms: int = 25_000,
    ) -> None:
        self._guard = guard
        self._headless = headless
        self._user_agent = user_agent
        self._storage_state = storage_state
        self._user_data_dir = user_data_dir
        self._navigation_timeout_ms = navigation_timeout_ms

        self._playwright: Any = None
        self._browser: Browser | None = None
        self._context: BrowserContext | None = None
        self._page: Page | None = None
        self._nav: _GuardedNavigation | None = None
        # One resolution per host per run. A page of forty images must not mean
        # forty DNS lookups in the screening path.
        self._verdicts: dict[str, bool] = {}

    async def __aenter__(self) -> BrowserPageSource:
        await self.start()
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    async def start(self) -> None:
        self._playwright = await async_playwright().start()
        chromium = self._playwright.chromium

        if self._user_data_dir is not None:
            # Persistent profile: what the GUI uses so a manual login survives
            # between runs. Playwright returns a context directly, with no
            # separate Browser object to close.
            self._context = await chromium.launch_persistent_context(
                user_data_dir=str(self._user_data_dir),
                headless=self._headless,
                user_agent=self._user_agent,
            )
        else:
            self._browser = await chromium.launch(headless=self._headless)
            self._context = await self._browser.new_context(
                user_agent=self._user_agent,
                storage_state=str(self._storage_state) if self._storage_state else None,
            )

        self._context.set_default_navigation_timeout(self._navigation_timeout_ms)
        self._context.set_default_timeout(self._navigation_timeout_ms)
        self._page = await self._context.new_page()
        await self._page.route("**/*", self._handle_route)

    async def aclose(self) -> None:
        for closer in (self._context, self._browser):
            if closer is not None:
                # Teardown must not mask the error that caused it.
                with contextlib.suppress(PlaywrightError):
                    await closer.close()
        if self._playwright is not None:
            await self._playwright.stop()
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None

    # -- routing ------------------------------------------------------------

    def _screen(self, url: str) -> tuple[bool, RejectionReason | None, str]:
        cached = self._verdicts.get(url)
        if cached is not None:
            return cached, None, ""
        verdict = self._guard.check(url)
        self._verdicts[url] = verdict.allowed
        return verdict.allowed, verdict.reason, verdict.detail

    async def _handle_route(self, route: Route) -> None:
        request = route.request
        allowed, reason, detail = self._screen(request.url)

        if request.resource_type != "document":
            # Screen but never proxy. A scraped page carrying
            # <img src="http://192.168.1.1/..."> would otherwise fire a blind
            # request into the user's LAN; aborting needs no fulfilment.
            if allowed:
                await route.continue_()
            else:
                await route.abort("blockedbyclient")
            return

        nav = self._nav
        if not allowed:
            if nav is not None:
                nav.blocked = PageBlocked(request.url, reason or RejectionReason.MALFORMED, detail)
            await route.abort("blockedbyclient")
            return

        await self._follow_redirects(route, request.url, nav)

    async def _follow_redirects(
        self, route: Route, url: str, nav: _GuardedNavigation | None
    ) -> None:
        for _ in range(MAX_REDIRECT_HOPS):
            if nav is not None:
                nav.hops.append(url)
            try:
                response = await route.fetch(url=url, max_redirects=0)
            except PlaywrightError as exc:
                if nav is not None:
                    nav.blocked = PageBlocked(url, RejectionReason.MALFORMED, str(exc))
                await route.abort("failed")
                return

            location = response.headers.get("location")
            if 300 <= response.status < 400 and location:
                url = urljoin(url, location)
                allowed, reason, detail = self._screen(url)
                if not allowed:
                    # The case a pre-flight string check cannot catch: a
                    # permitted host redirecting somewhere that was never vetted.
                    if nav is not None:
                        nav.blocked = PageBlocked(url, reason or RejectionReason.MALFORMED, detail)
                    await route.abort("blockedbyclient")
                    return
                continue

            if nav is not None:
                nav.final_url = url
            await route.fulfill(response=response)
            return

        if nav is not None:
            nav.blocked = PageBlocked(url, RejectionReason.MALFORMED, "too many redirects")
        await route.abort("failed")

    # -- navigation ---------------------------------------------------------

    async def _goto(self, url: str) -> str:
        allowed, reason, detail = self._screen(url)
        if not allowed:
            raise PageBlocked(url, reason or RejectionReason.MALFORMED, detail)

        page = self._require_page()
        nav = _GuardedNavigation()
        self._nav = nav
        try:
            await page.goto(url, wait_until="domcontentloaded")
        except PlaywrightTimeout as exc:
            raise PageTimeout(f"{url}: navigation timed out") from exc
        except PlaywrightError as exc:
            if nav.blocked is not None:
                raise nav.blocked from exc
            raise PageError(f"{url}: {exc}") from exc
        finally:
            self._nav = None

        if nav.blocked is not None:
            raise nav.blocked
        return nav.final_url or url

    def _require_page(self) -> Page:
        if self._page is None:
            raise PageError("page source is not started; call start() first")
        return self._page

    # -- PageSource ---------------------------------------------------------

    async def load_toc(
        self,
        url: str,
        *,
        link_selector: str,
        capture_html: bool = False,
        screenshot_path: Path | None = None,
    ) -> TocPage:
        final_url = await self._goto(url)
        page = self._require_page()

        html = await page.content() if capture_html else None
        if screenshot_path is not None:
            screenshot_path.parent.mkdir(parents=True, exist_ok=True)
            await page.screenshot(path=str(screenshot_path), full_page=True)

        raw: Sequence[object] = await page.eval_on_selector_all(link_selector, LINK_COLLECTOR_JS)
        return TocPage(
            requested_url=url,
            final_url=final_url,
            raw_links=tuple(raw),
            html=html,
        )

    async def load_chapter(
        self,
        url: str,
        *,
        title_selector: str,
        content_selector: str,
    ) -> ChapterPage:
        final_url = await self._goto(url)
        page = self._require_page()

        title = await self._read_field(page, title_selector, final_url)
        body = await self._read_field(page, content_selector, final_url)
        return ChapterPage(
            requested_url=url,
            final_url=final_url,
            title=title,
            body=body,
        )

    async def _read_field(self, page: Page, selector: str, url: str) -> str:
        # wait_for_selector, not a bare read. On a JS-hydrated page the element
        # is legitimately absent for a moment after domcontentloaded, and a
        # bare read would raise SelectorNotFound - which the fetch loop is
        # explicitly told never to retry. Without the wait, "do not retry"
        # would be encoding a permanent verdict on a transient condition.
        try:
            element = await page.wait_for_selector(selector, state="attached")
        except PlaywrightTimeout as exc:
            raise SelectorNotFound(f"{selector} matched nothing on {url}") from exc
        except PlaywrightError as exc:
            raise PageError(f"{url}: {exc}") from exc

        if element is None:
            raise SelectorNotFound(f"{selector} matched nothing on {url}")
        try:
            return await element.inner_text()
        except PlaywrightError as exc:
            raise PageError(f"{url}: reading {selector}: {exc}") from exc


async def open_browser_source(**kwargs: Any) -> BrowserPageSource:
    """Construct and start a BrowserPageSource."""
    source = BrowserPageSource(**kwargs)
    await source.start()
    return source


__all__ = ["BrowserPageSource", "open_browser_source"]
