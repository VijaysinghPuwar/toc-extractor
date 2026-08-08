"""Tests for link vetting, with the accounting invariant front and centre."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from toc_extractor.parser import (
    LinkCollection,
    RejectedLink,
    SelectorSet,
    collect_links,
)
from toc_extractor.politeness import RejectionReason, UrlGuard, parse_robots


def resolver(host: str) -> Sequence[str]:
    if host.endswith(".invalid"):
        raise OSError("no such host")
    return ["93.184.216.34"]


GUARD = UrlGuard(resolver=resolver)


# ---------------------------------------------------------------------------
# The invariant
# ---------------------------------------------------------------------------


def test_every_candidate_is_kept_or_rejected() -> None:
    raw: list[object] = [
        "https://example.com/ch/1",
        "https://example.com/ch/2",
        {},  # SVG anchor
        None,  # element with no href
        "file:///etc/passwd",
        "javascript:alert(1)",
        "",
        "https://example.com/ch/1",  # duplicate
        "http://127.0.0.1/admin",
    ]
    collection, _ = collect_links(raw, guard=GUARD)

    assert collection.raw_count == len(raw)
    assert len(collection.kept) + len(collection.rejected) == len(raw)
    assert collection.kept == ("https://example.com/ch/1", "https://example.com/ch/2")


def test_accounting_failure_is_an_error_not_a_silent_drop() -> None:
    """Constructing an unbalanced collection must be impossible."""
    with pytest.raises(AssertionError, match="link accounting lost"):
        LinkCollection(raw_count=5, kept=("a",), rejected=())


def test_truncation_is_accounted_separately_from_rejection() -> None:
    raw: list[object] = [f"https://example.com/ch/{i}" for i in range(10)]
    collection, _ = collect_links(raw, guard=GUARD, max_links=3)

    assert len(collection.kept) == 3
    assert collection.truncated == 7
    assert collection.rejected == ()
    assert len(collection.kept) + collection.truncated == collection.raw_count


def test_reason_counts_are_manifest_ready() -> None:
    raw: list[object] = [{}, {}, "file:///x", "", "https://example.com/ok"]
    collection, _ = collect_links(raw, guard=GUARD)
    assert collection.reason_counts() == {
        "not_a_string": 2,
        "disallowed_scheme": 1,
        "empty": 1,
    }


# ---------------------------------------------------------------------------
# Individual rejection reasons
# ---------------------------------------------------------------------------


def test_svg_anchor_is_rejected_with_a_reason() -> None:
    collection, _ = collect_links([{}], guard=GUARD)
    assert collection.kept == ()
    assert collection.rejected[0].reason is RejectionReason.NOT_A_STRING


def test_duplicates_are_counted() -> None:
    raw: list[object] = ["https://example.com/a", "https://example.com/a"]
    collection, _ = collect_links(raw, guard=GUARD)
    assert collection.kept == ("https://example.com/a",)
    assert collection.rejected[0].reason is RejectionReason.DUPLICATE


def test_rejected_link_describes_itself() -> None:
    item = RejectedLink(value="file:///x", reason=RejectionReason.DISALLOWED_SCHEME, detail="file")
    assert item.describe() == "file:///x: disallowed_scheme (file)"


# ---------------------------------------------------------------------------
# robots integration
# ---------------------------------------------------------------------------

ROBOTS = "User-agent: *\nDisallow: /members/\n"


def test_robots_disallow_is_a_hard_rejection_when_anonymous() -> None:
    policy = parse_robots(ROBOTS, origin="https://example.com")
    raw: list[object] = ["https://example.com/members/1", "https://example.com/public/1"]
    collection, decisions = collect_links(raw, guard=GUARD, robots=policy)

    assert collection.kept == ("https://example.com/public/1",)
    assert collection.rejected[0].reason is RejectionReason.ROBOTS_DISALLOWED
    assert "line 2" in collection.rejected[0].detail
    assert len(decisions) == 1


def test_authenticated_session_overrides_and_records_the_rule() -> None:
    """The escape hatch is a human action, not a flag.

    A site routinely disallows the paths a login unlocks, so refusing hard here
    would fail the GUI's main flow and teach users to disable the check.
    """
    policy = parse_robots(ROBOTS, origin="https://example.com")
    collection, decisions = collect_links(
        ["https://example.com/members/1"],
        guard=GUARD,
        robots=policy,
        session_authenticated=True,
    )

    assert collection.kept == ("https://example.com/members/1",)
    assert collection.rejected == ()

    decision = decisions[0]
    assert decision.authenticated_override
    assert decision.rule_description is not None
    assert "line 2" in decision.rule_description

    entry = decision.as_manifest_entry()
    assert entry["robots_authenticated_override"] is True
    assert entry["robots_rule"] is not None


def test_no_robots_policy_permits_everything() -> None:
    collection, decisions = collect_links(["https://example.com/x"], guard=GUARD, robots=None)
    assert collection.kept == ("https://example.com/x",)
    assert decisions[0].authenticated_override is False


def test_authenticated_override_is_not_recorded_when_robots_permits() -> None:
    """An override marker on a permitted URL would poison the audit trail."""
    policy = parse_robots(ROBOTS, origin="https://example.com")
    _, decisions = collect_links(
        ["https://example.com/public/1"],
        guard=GUARD,
        robots=policy,
        session_authenticated=True,
    )
    assert decisions[0].authenticated_override is False
    assert decisions[0].rule_description is None


# ---------------------------------------------------------------------------
# SelectorSet
# ---------------------------------------------------------------------------


@pytest.mark.browser
def test_link_collector_js_returns_strings_for_every_element_kind() -> None:
    """Prove the JS fix against a real DOM, not by reasoning about the spec.

    v1 used `a.href || a.getAttribute('href')`. For SVG anchors that yields an
    SVGAnimatedString, which reaches Python as {} and was silently dropped.
    Resolving against document.baseURI in the page returns a plain string for
    every element kind.
    """
    from playwright.sync_api import sync_playwright

    from toc_extractor.parser import LINK_COLLECTOR_JS

    html = """<!doctype html><html><body>
    <a class="c" href="/rel/ch1">anchor relative</a>
    <a class="c" href="chapter/ch2">anchor relative no slash</a>
    <a class="c" href="httpd-docs/ch3">anchor http-prefixed relative</a>
    <div class="c" href="/div/ch4">div with href</div>
    <my-link class="c" href="httpd-docs/ch5">custom element</my-link>
    <svg xmlns="http://www.w3.org/2000/svg"><a class="c" href="/svg/ch6"><text>s</text></a></svg>
    </body></html>"""

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page()
        page.goto("https://example.com/book/toc", wait_until="commit")
        page.set_content(html)
        raw = page.eval_on_selector_all(".c", LINK_COLLECTOR_JS)
        browser.close()

    assert all(isinstance(value, str) for value in raw), raw
    assert raw == [
        "https://example.com/rel/ch1",
        "https://example.com/book/chapter/ch2",
        "https://example.com/book/httpd-docs/ch3",
        "https://example.com/div/ch4",
        "https://example.com/book/httpd-docs/ch5",
        "https://example.com/svg/ch6",
    ]

    collection, _ = collect_links(raw, guard=GUARD)
    assert len(collection.kept) == 6
    assert collection.rejected == ()


def test_selector_set_reports_what_is_missing() -> None:
    selectors = SelectorSet.create(link="a.ch", title="", content="  ")
    assert not selectors.complete
    assert selectors.missing == ("title", "content")


def test_complete_selector_set_is_stripped() -> None:
    selectors = SelectorSet.create(link="  a.ch  ", title="h1", content="article")
    assert selectors.complete
    assert selectors.link == "a.ch"
