"""Tests for the URL guard, robots policy, and per-host rate limiter.

No test here touches DNS, the network, or the clock: the resolver and the
sleep function are injected.
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Sequence

import pytest

from toc_extractor.politeness import (
    RateLimiter,
    RejectionReason,
    UrlGuard,
    dedupe_preserving_order,
    missing_robots,
    origin_of,
    parse_robots,
)


def fixed_resolver(mapping: dict[str, Sequence[str]]):
    def resolve(host: str) -> Sequence[str]:
        if host not in mapping:
            raise OSError(f"no such host: {host}")
        return mapping[host]

    return resolve


PUBLIC = fixed_resolver({"example.com": ["93.184.216.34"], "cdn.example.com": ["93.184.216.35"]})


# ---------------------------------------------------------------------------
# UrlGuard: schemes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "file:///Users/someone/.ssh/id_ed25519",
        "javascript:alert(document.cookie)",
        "data:text/html,<script>1</script>",
        "ftp://example.com/x",
        "about:blank",
        "chrome://settings",
    ],
)
def test_non_http_schemes_are_rejected(url: str) -> None:
    """The v1 regression.

    v1 passed every one of these straight to page.goto, because
    urljoin(base, x) returns x untouched when x carries its own scheme. A
    file: URL was read off disk and written into the output directory as
    chapter text.
    """
    verdict = UrlGuard(resolver=PUBLIC).check(url)
    assert not verdict
    assert verdict.reason is RejectionReason.DISALLOWED_SCHEME


@pytest.mark.parametrize("url", ["https://example.com/ch/1", "http://example.com/ch/2"])
def test_http_schemes_are_allowed(url: str) -> None:
    assert UrlGuard(resolver=PUBLIC).check(url)


def test_scheme_check_is_case_insensitive() -> None:
    assert UrlGuard(resolver=PUBLIC).check("HTTPS://example.com/x")
    assert not UrlGuard(resolver=PUBLIC).check("FILE:///etc/passwd")


# ---------------------------------------------------------------------------
# UrlGuard: non-string input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [{}, None, 42, [], {"baseVal": "/x"}])
def test_non_string_candidates_are_counted_not_dropped(value: object) -> None:
    """The SVG-anchor bug.

    SVGAElement.href is an SVGAnimatedString, which crosses into Python as {}.
    It is truthy in JS and falsy in Python, so v1's `if l` filter discarded it
    with no error and the chapter vanished. Here it is a counted rejection.
    """
    verdict = UrlGuard(resolver=PUBLIC).check(value)
    assert not verdict
    assert verdict.reason is RejectionReason.NOT_A_STRING
    assert type(value).__name__ in verdict.detail


@pytest.mark.parametrize("value", ["", "   "])
def test_empty_candidates_are_counted(value: str) -> None:
    verdict = UrlGuard(resolver=PUBLIC).check(value)
    assert verdict.reason is RejectionReason.EMPTY


def test_relative_url_is_rejected_as_schemeless() -> None:
    """Resolution happens in parser.py; anything still relative here is a bug."""
    verdict = UrlGuard(resolver=PUBLIC).check("httpd-docs/ch1")
    assert verdict.reason is RejectionReason.DISALLOWED_SCHEME


# ---------------------------------------------------------------------------
# UrlGuard: private addresses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url,label",
    [
        ("http://127.0.0.1:8080/admin", "loopback"),
        ("http://localhost:3000/x", "loopback by name"),
        ("http://169.254.169.254/latest/meta-data/", "cloud metadata endpoint"),
        ("http://10.0.0.5/internal", "RFC1918 class A"),
        ("http://192.168.1.1/router", "RFC1918 class C"),
        ("http://172.16.0.9/x", "RFC1918 class B"),
        ("http://[::1]/x", "IPv6 loopback"),
        ("http://[fe80::1]/x", "IPv6 link-local"),
        ("http://[fd00::1]/x", "IPv6 unique-local"),
        ("http://0.0.0.0/x", "unspecified"),
    ],
)
def test_private_addresses_are_rejected(url: str, label: str) -> None:
    resolver = fixed_resolver({"localhost": ["127.0.0.1"]})
    verdict = UrlGuard(resolver=resolver).check(url)
    assert not verdict, label
    assert verdict.reason is RejectionReason.PRIVATE_ADDRESS


def test_ipv4_mapped_ipv6_loopback_is_rejected() -> None:
    """::ffff:127.0.0.1 is loopback wearing an IPv6 hat."""
    verdict = UrlGuard(resolver=PUBLIC).check("http://[::ffff:127.0.0.1]/x")
    assert verdict.reason is RejectionReason.PRIVATE_ADDRESS


def test_public_host_resolving_to_private_address_is_rejected() -> None:
    """A public name pointed at 127.0.0.1 is the usual SSRF shape."""
    resolver = fixed_resolver({"evil.example": ["127.0.0.1"]})
    verdict = UrlGuard(resolver=resolver).check("https://evil.example/x")
    assert verdict.reason is RejectionReason.PRIVATE_ADDRESS
    assert "evil.example -> 127.0.0.1" in verdict.detail


def test_any_private_answer_rejects_even_if_others_are_public() -> None:
    resolver = fixed_resolver({"mixed.example": ["93.184.216.34", "10.0.0.1"]})
    assert not UrlGuard(resolver=resolver).check("https://mixed.example/x")


def test_unresolvable_host_is_rejected_with_reason() -> None:
    verdict = UrlGuard(resolver=PUBLIC).check("https://nope.invalid/x")
    assert verdict.reason is RejectionReason.UNRESOLVABLE_HOST


def test_allow_private_hosts_opens_the_gate() -> None:
    guard = UrlGuard(allow_private_hosts=True, resolver=PUBLIC)
    assert guard.check("http://127.0.0.1:8080/admin")


def test_allow_private_hosts_does_not_open_the_scheme_gate() -> None:
    """The private-host escape hatch must not also permit file: URLs."""
    guard = UrlGuard(allow_private_hosts=True, resolver=PUBLIC)
    verdict = guard.check("file:///etc/passwd")
    assert not verdict
    assert verdict.reason is RejectionReason.DISALLOWED_SCHEME


# ---------------------------------------------------------------------------
# robots.txt
# ---------------------------------------------------------------------------

ROBOTS = """\
# comment line
User-agent: *
Disallow: /private/
Crawl-delay: 4

User-agent: TOCExtractor
Disallow: /members/
Allow: /members/public/
"""


def test_robots_allows_unlisted_path() -> None:
    policy = parse_robots(ROBOTS, origin="https://example.com")
    assert policy.can_fetch("https://example.com/chapter/1")


def test_robots_denies_listed_path() -> None:
    policy = parse_robots(ROBOTS, origin="https://example.com")
    assert not policy.can_fetch("https://example.com/members/secret")


def test_matched_rule_reports_line_number() -> None:
    """The post-gate warning has to name the rule and where it lives."""
    policy = parse_robots(ROBOTS, origin="https://example.com")
    rule = policy.matched_rule("https://example.com/members/secret")
    assert rule is not None
    assert rule.value == "/members/"
    assert rule.line_number == 7
    assert "line 7" in rule.describe()


def test_matched_rule_prefers_the_most_specific_directive() -> None:
    content = "User-agent: *\nDisallow: /a/\nDisallow: /a/b/\n"
    policy = parse_robots(content, origin="https://example.com")
    rule = policy.matched_rule("https://example.com/a/b/c")
    assert rule is not None
    assert rule.value == "/a/b/"
    assert rule.line_number == 3


def test_matched_rule_is_none_for_permitted_path() -> None:
    policy = parse_robots(ROBOTS, origin="https://example.com")
    assert policy.matched_rule("https://example.com/chapter/1") is None


def test_crawl_delay_is_read_from_the_applicable_group() -> None:
    policy = parse_robots(ROBOTS, origin="https://example.com", user_agent="SomeOtherBot")
    assert policy.crawl_delay == 4.0


def test_crawl_delay_does_not_leak_from_the_wildcard_group() -> None:
    """robots.txt precedence is winner-takes-all.

    TOCExtractor has its own group with no Crawl-delay, so the wildcard
    group's Crawl-delay: 4 does not apply to it.
    """
    policy = parse_robots(ROBOTS, origin="https://example.com", user_agent="TOCExtractor")
    assert policy.crawl_delay is None


def test_matched_rule_ignores_the_wildcard_group_when_a_specific_one_exists() -> None:
    """matched_rule and can_fetch must never disagree.

    /private/ is disallowed for * but not for TOCExtractor, which has its own
    group. If matched_rule reported the wildcard rule here, the post-gate
    warning would name a directive that never applied.
    """
    policy = parse_robots(ROBOTS, origin="https://example.com", user_agent="TOCExtractor")
    assert policy.can_fetch("https://example.com/private/x")
    assert policy.matched_rule("https://example.com/private/x") is None


def test_wildcard_group_applies_when_no_specific_group_matches() -> None:
    policy = parse_robots(ROBOTS, origin="https://example.com", user_agent="SomeOtherBot")
    assert not policy.can_fetch("https://example.com/private/x")
    rule = policy.matched_rule("https://example.com/private/x")
    assert rule is not None
    assert rule.line_number == 3


def test_missing_robots_permits_everything() -> None:
    """RFC 9309: an unreachable robots.txt means no restrictions."""
    policy = missing_robots("https://example.com")
    assert policy.can_fetch("https://example.com/anything")
    assert policy.fetched is False
    assert policy.crawl_delay is None


def test_comments_do_not_become_rules() -> None:
    policy = parse_robots("# Disallow: /everything\nUser-agent: *\nAllow: /\n", origin="x")
    assert policy.can_fetch("https://example.com/everything")


def test_origin_of_strips_path_and_query() -> None:
    assert origin_of("https://example.com/book/toc?page=2#x") == "https://example.com"


# ---------------------------------------------------------------------------
# RateLimiter
# ---------------------------------------------------------------------------


class FakeClock:
    """A clock that only advances when the fake sleep is awaited."""

    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def time(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_first_request_to_a_host_does_not_wait() -> None:
    clock = FakeClock()
    limiter = RateLimiter(min_interval=2.0, clock=clock.time, sleep=clock.sleep)
    assert await limiter.acquire("example.com") == 0.0
    assert clock.sleeps == []


async def test_second_request_waits_the_full_interval() -> None:
    clock = FakeClock()
    limiter = RateLimiter(min_interval=2.0, clock=clock.time, sleep=clock.sleep)
    await limiter.acquire("example.com")
    assert await limiter.acquire("example.com") == pytest.approx(2.0)


async def test_hosts_are_limited_independently() -> None:
    clock = FakeClock()
    limiter = RateLimiter(min_interval=2.0, clock=clock.time, sleep=clock.sleep)
    await limiter.acquire("example.com")
    assert await limiter.acquire("other.example") == 0.0


async def test_concurrency_cannot_shorten_the_interval() -> None:
    """The invariant the whole class exists for.

    Ten workers hit one host at once. If the lock were released before the
    sleep, they would all read the same "last request" timestamp and fire
    together, turning a 2s delay into 2s/10. Serialised, the tenth request
    must land no earlier than 18s after the first.
    """
    clock = FakeClock()
    limiter = RateLimiter(min_interval=2.0, clock=clock.time, sleep=clock.sleep)

    async def worker() -> float:
        await limiter.acquire("example.com")
        return clock.now

    stamps = await asyncio.gather(*(worker() for _ in range(10)))

    assert clock.now == pytest.approx(18.0)
    ordered = sorted(stamps)
    for earlier, later in itertools.pairwise(ordered):
        assert later - earlier >= 2.0 - 1e-9


async def test_crawl_delay_can_raise_the_interval() -> None:
    clock = FakeClock()
    limiter = RateLimiter(min_interval=1.0, clock=clock.time, sleep=clock.sleep)
    limiter.set_host_interval("example.com", 5.0)
    await limiter.acquire("example.com")
    assert await limiter.acquire("example.com") == pytest.approx(5.0)


async def test_crawl_delay_cannot_lower_the_configured_interval() -> None:
    """A site asking to be hit faster than the user chose does not get to."""
    clock = FakeClock()
    limiter = RateLimiter(min_interval=5.0, clock=clock.time, sleep=clock.sleep)
    limiter.set_host_interval("example.com", 0.1)
    assert limiter.interval_for("example.com") == 5.0


async def test_zero_interval_never_sleeps() -> None:
    clock = FakeClock()
    limiter = RateLimiter(min_interval=0.0, clock=clock.time, sleep=clock.sleep)
    await limiter.acquire("example.com")
    await limiter.acquire("example.com")
    assert clock.sleeps == []


# ---------------------------------------------------------------------------
# dedupe
# ---------------------------------------------------------------------------


def test_dedupe_preserves_order_and_reports_drops() -> None:
    kept, dropped = dedupe_preserving_order(["a", "b", "a", "c", "b"])
    assert kept == ["a", "b", "c"]
    assert dropped == ["a", "b"]


def test_dedupe_accounts_for_every_input() -> None:
    """raw == kept + rejected, the invariant that would have caught the SVG bug."""
    raw = ["a", "b", "a", "c", "b", "a"]
    kept, dropped = dedupe_preserving_order(raw)
    assert len(kept) + len(dropped) == len(raw)
