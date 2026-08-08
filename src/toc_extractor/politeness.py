"""robots.txt policy, per-host rate limiting, and URL safety.

Everything here is pure or injectable, so it is fully tested before the async
fetcher composes it. No module in this file imports Playwright.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
import time
import urllib.robotparser
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from urllib.parse import urlparse, urlunparse

ALLOWED_SCHEMES = frozenset({"http", "https"})

DEFAULT_USER_AGENT = "TOCExtractor"

# Resolver signature. Injected so tests never touch DNS; the default is
# socket.getaddrinfo.
Resolver = Callable[[str], Sequence[str]]

# Sleep signature. Injected so rate-limiter tests run against a fake clock
# instead of spending real seconds.
Sleeper = Callable[[float], Awaitable[None]]


async def _sleep_seconds(seconds: float) -> None:
    await asyncio.sleep(seconds)


class RejectionReason(StrEnum):
    """Why a candidate URL was not fetched.

    Values are stable strings because they are written into the JSONL manifest.
    Nothing in the pipeline discards a link without recording one of these.
    """

    NOT_A_STRING = "not_a_string"
    EMPTY = "empty"
    MALFORMED = "malformed"
    DISALLOWED_SCHEME = "disallowed_scheme"
    MISSING_HOST = "missing_host"
    UNRESOLVABLE_HOST = "unresolvable_host"
    PRIVATE_ADDRESS = "private_address"
    ROBOTS_DISALLOWED = "robots_disallowed"
    DUPLICATE = "duplicate"


@dataclass(frozen=True, slots=True)
class UrlVerdict:
    """The outcome of checking one URL."""

    url: str
    allowed: bool
    reason: RejectionReason | None = None
    detail: str = ""

    def __bool__(self) -> bool:
        return self.allowed


def default_resolver(host: str) -> Sequence[str]:
    infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    # sockaddr[0] is the address string for both AF_INET and AF_INET6.
    return [str(info[4][0]) for info in infos]


def _is_private(address: str) -> bool:
    """True for anything that is not a routable public address.

    Covers loopback, link-local (including the 169.254.169.254 metadata
    endpoint), RFC1918, unique-local IPv6, and the reserved ranges. IPv4-mapped
    IPv6 is unwrapped first so ::ffff:127.0.0.1 cannot slip past.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False

    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped

    return (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


class UrlGuard:
    """Scheme allowlist plus private-address rejection.

    This is the string-level check. Phase 3 additionally enforces the same
    policy at the request layer, because a permitted host can redirect to one
    that was never pre-flighted.
    """

    def __init__(
        self,
        *,
        allow_private_hosts: bool = False,
        resolver: Resolver = default_resolver,
    ) -> None:
        self._allow_private_hosts = allow_private_hosts
        self._resolver = resolver

    def check(self, candidate: object) -> UrlVerdict:
        if not isinstance(candidate, str):
            # SVG anchors hand back an SVGAnimatedString, which crosses into
            # Python as {}. v1 filtered it out as falsy and lost the chapter
            # with no error; here it is a counted rejection.
            return UrlVerdict(
                url=repr(candidate),
                allowed=False,
                reason=RejectionReason.NOT_A_STRING,
                detail=f"got {type(candidate).__name__}",
            )

        raw = candidate.strip()
        if not raw:
            return UrlVerdict(url=raw, allowed=False, reason=RejectionReason.EMPTY)

        try:
            parsed = urlparse(raw)
        except ValueError as exc:
            return UrlVerdict(
                url=raw, allowed=False, reason=RejectionReason.MALFORMED, detail=str(exc)
            )

        if parsed.scheme.lower() not in ALLOWED_SCHEMES:
            # The reason defect 5 needed a scheme allowlist rather than a better
            # absolute-URL test: urljoin(base, "file:///etc/passwd") returns the
            # file URL untouched, so no amount of care in the join step helps.
            return UrlVerdict(
                url=raw,
                allowed=False,
                reason=RejectionReason.DISALLOWED_SCHEME,
                detail=parsed.scheme or "(none)",
            )

        try:
            host = parsed.hostname
        except ValueError as exc:
            return UrlVerdict(
                url=raw, allowed=False, reason=RejectionReason.MALFORMED, detail=str(exc)
            )

        if not host:
            return UrlVerdict(url=raw, allowed=False, reason=RejectionReason.MISSING_HOST)

        if self._allow_private_hosts:
            return UrlVerdict(url=raw, allowed=True)

        return self._check_address(raw, host)

    def _check_address(self, raw: str, host: str) -> UrlVerdict:
        # A bare IP literal needs no DNS round trip.
        try:
            ipaddress.ip_address(host)
        except ValueError:
            pass
        else:
            if _is_private(host):
                return UrlVerdict(
                    url=raw,
                    allowed=False,
                    reason=RejectionReason.PRIVATE_ADDRESS,
                    detail=host,
                )
            return UrlVerdict(url=raw, allowed=True)

        try:
            addresses = self._resolver(host)
        except OSError as exc:
            return UrlVerdict(
                url=raw,
                allowed=False,
                reason=RejectionReason.UNRESOLVABLE_HOST,
                detail=f"{host}: {exc}",
            )

        if not addresses:
            return UrlVerdict(
                url=raw, allowed=False, reason=RejectionReason.UNRESOLVABLE_HOST, detail=host
            )

        # Every resolved address must be public. One private answer is enough to
        # reject, because which address the browser picks is not ours to choose.
        for address in addresses:
            if _is_private(address):
                return UrlVerdict(
                    url=raw,
                    allowed=False,
                    reason=RejectionReason.PRIVATE_ADDRESS,
                    detail=f"{host} -> {address}",
                )
        return UrlVerdict(url=raw, allowed=True)


def build_url_guard(*, allow_private_hosts: bool = False) -> UrlGuard:
    """The one construction path for a UrlGuard outside tests.

    Deliberately narrow. The only exemption the guard understands is the
    all-or-nothing --allow-private-hosts switch: there is no per-origin
    allowlist, no config key, and no constructor argument that could grow into
    one. Tests that need to exempt a fixture server do it by subclassing in the
    test module, which keeps the bypass unreachable from anything a user can
    configure. test_guard_construction.py asserts that stays true.
    """
    return UrlGuard(allow_private_hosts=allow_private_hosts)


@dataclass(frozen=True, slots=True)
class RobotsRule:
    """One directive from robots.txt, with the line it came from."""

    directive: str
    value: str
    line_number: int
    user_agent: str

    def describe(self) -> str:
        return (
            f'"{self.directive}: {self.value}" at robots.txt line '
            f"{self.line_number} (User-agent: {self.user_agent})"
        )


@dataclass(frozen=True, slots=True)
class RobotsPolicy:
    """A parsed robots.txt for one origin.

    The allow/deny decision comes from urllib.robotparser, which handles the
    precedence rules. The matching rule is located separately, because
    robotparser exposes no way to ask which directive decided an answer, and
    the post-gate warning has to name it.
    """

    origin: str
    user_agent: str
    crawl_delay: float | None
    _parser: urllib.robotparser.RobotFileParser
    _rules: tuple[RobotsRule, ...] = field(default=())
    fetched: bool = True

    def can_fetch(self, url: str) -> bool:
        if not self.fetched:
            # An origin with no reachable robots.txt is treated as permitted,
            # which is what RFC 9309 specifies for a 404.
            return True
        return self._parser.can_fetch(self.user_agent, url)

    def matched_rule(self, url: str) -> RobotsRule | None:
        """The most specific Disallow directive covering `url`, if any."""
        path = urlparse(url).path or "/"
        best: RobotsRule | None = None
        for rule in self._rules:
            if rule.directive != "disallow" or not rule.value:
                continue
            if path.startswith(rule.value) and (best is None or len(rule.value) > len(best.value)):
                best = rule
        return best


@dataclass(slots=True)
class _Group:
    agents: list[str]
    rules: list[RobotsRule]


def _parse_groups(content: str) -> list[_Group]:
    """Split robots.txt into agent groups, remembering line numbers."""
    groups: list[_Group] = []
    current: _Group | None = None
    previous_was_agent = False

    for number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.split("#", 1)[0].strip()
        if not line or ":" not in line:
            continue

        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()

        if key == "user-agent":
            # Consecutive User-agent lines share one group; a User-agent after
            # any rule line starts a new one.
            if current is None or not previous_was_agent:
                current = _Group(agents=[], rules=[])
                groups.append(current)
            current.agents.append(value.lower())
            previous_was_agent = True
            continue

        previous_was_agent = False
        if key not in {"disallow", "allow"} or current is None:
            continue
        current.rules.append(
            RobotsRule(
                directive=key,
                value=value,
                line_number=number,
                user_agent=current.agents[-1] if current.agents else "*",
            )
        )

    return groups


def _applicable_group(groups: list[_Group], user_agent: str) -> _Group | None:
    """The one group that governs `user_agent`.

    robots.txt precedence is winner-takes-all: if a group names this agent, the
    wildcard group does not apply at all. Collecting rules from both would let
    matched_rule() name a directive that can_fetch() correctly ignored.
    """
    wanted = user_agent.lower()
    for group in groups:
        if any(agent != "*" and wanted.startswith(agent) for agent in group.agents):
            return group
    for group in groups:
        if "*" in group.agents:
            return group
    return None


def parse_robots(
    content: str,
    *,
    origin: str,
    user_agent: str = DEFAULT_USER_AGENT,
) -> RobotsPolicy:
    """Parse robots.txt text, keeping line numbers for the rules that apply."""
    parser = urllib.robotparser.RobotFileParser()
    parser.parse(content.splitlines())

    group = _applicable_group(_parse_groups(content), user_agent)
    rules = tuple(group.rules) if group is not None else ()

    delay = parser.crawl_delay(user_agent)
    crawl_delay = float(delay) if delay is not None else None

    return RobotsPolicy(
        origin=origin,
        user_agent=user_agent,
        crawl_delay=crawl_delay,
        _parser=parser,
        _rules=rules,
    )


def missing_robots(origin: str, *, user_agent: str = DEFAULT_USER_AGENT) -> RobotsPolicy:
    """Policy for an origin whose robots.txt could not be fetched."""
    parser = urllib.robotparser.RobotFileParser()
    parser.parse([])
    return RobotsPolicy(
        origin=origin,
        user_agent=user_agent,
        crawl_delay=None,
        _parser=parser,
        _rules=(),
        fetched=False,
    )


def origin_of(url: str) -> str:
    parsed = urlparse(url)
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", ""))


class RateLimiter:
    """Per-host minimum interval that concurrency cannot shorten.

    The per-host lock is held across the sleep, not just around the timestamp
    update. That is the whole point: if the lock were released before sleeping,
    N concurrent workers would each observe the same "last request" time and
    fire together, and the delay the user configured would silently become a
    delay divided by N.
    """

    def __init__(
        self,
        *,
        min_interval: float = 1.2,
        clock: Callable[[], float] = time.monotonic,
        sleep: Sleeper | None = None,
    ) -> None:
        self._min_interval = max(0.0, min_interval)
        self._clock = clock
        self._sleep: Sleeper = sleep or _sleep_seconds
        self._locks: dict[str, asyncio.Lock] = {}
        self._last: dict[str, float] = {}
        self._overrides: dict[str, float] = {}

    def set_host_interval(self, host: str, interval: float) -> None:
        """Raise the interval for one host, e.g. from a Crawl-delay directive.

        Only ever raises. A robots.txt asking for a shorter delay than the user
        configured does not get to speed us up.
        """
        self._overrides[host] = max(self._overrides.get(host, 0.0), interval)

    def interval_for(self, host: str) -> float:
        return max(self._min_interval, self._overrides.get(host, 0.0))

    async def acquire(self, host: str) -> float:
        """Block until this host may be hit again. Returns seconds waited."""
        lock = self._locks.setdefault(host, asyncio.Lock())
        async with lock:
            interval = self.interval_for(host)
            now = self._clock()
            last = self._last.get(host)
            waited = 0.0
            if last is not None:
                remaining = interval - (now - last)
                if remaining > 0:
                    await self._sleep(remaining)
                    waited = remaining
            self._last[host] = self._clock()
            return waited


def dedupe_preserving_order(urls: Iterable[str]) -> tuple[list[str], list[str]]:
    """Split into first-seen order and the duplicates that were dropped."""
    seen: set[str] = set()
    kept: list[str] = []
    duplicates: list[str] = []
    for url in urls:
        if url in seen:
            duplicates.append(url)
        else:
            seen.add(url)
            kept.append(url)
    return kept, duplicates
