"""Turn raw DOM output into a vetted list of chapter URLs.

The contract this module exists to enforce:

    len(raw) == len(kept) + len(rejected)

Every candidate is either kept or rejected with a reason. Nothing is dropped
because it happened to be falsy. v1's `if l` filter is what silently lost SVG
anchors, and no amount of care downstream can notice a link that was never
counted.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from .politeness import RejectionReason, RobotsPolicy, UrlGuard, UrlVerdict

# Evaluated in the page to collect chapter links.
#
# v1 used `a.href || a.getAttribute('href')`, which has two failure modes:
# HTMLAnchorElement.href is already absolute so the Python-side urljoin never
# ran for real anchors, and SVGAElement.href is an SVGAnimatedString object
# rather than a string. Resolving against document.baseURI in the page returns
# a plain absolute string for ordinary anchors, SVG anchors, and stray
# elements carrying an href attribute alike, and keeps resolution in the one
# place that knows the document's <base>.
LINK_COLLECTOR_JS = """
elements => elements.map(el => {
    const raw = el.getAttribute('href');
    if (raw === null) return null;
    try {
        return new URL(raw, document.baseURI).href;
    } catch (e) {
        return raw;
    }
})
"""


@dataclass(frozen=True, slots=True)
class RejectedLink:
    """One candidate that will not be fetched, and why."""

    value: str
    reason: RejectionReason
    detail: str = ""

    def describe(self) -> str:
        suffix = f" ({self.detail})" if self.detail else ""
        return f"{self.value}: {self.reason.value}{suffix}"


@dataclass(frozen=True, slots=True)
class LinkCollection:
    """The result of vetting one TOC page's links."""

    raw_count: int
    kept: tuple[str, ...] = ()
    rejected: tuple[RejectedLink, ...] = ()
    truncated: int = 0

    def __post_init__(self) -> None:
        # The invariant, asserted rather than logged. `truncated` counts links
        # dropped by --max, which are accounted for separately because they
        # were vetted successfully and simply fell outside the requested range.
        total = len(self.kept) + len(self.rejected) + self.truncated
        if total != self.raw_count:
            raise AssertionError(
                f"link accounting lost {self.raw_count - total} of {self.raw_count} "
                f"candidates: kept={len(self.kept)} rejected={len(self.rejected)} "
                f"truncated={self.truncated}"
            )

    def reason_counts(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for item in self.rejected:
            counts[item.reason.value] = counts.get(item.reason.value, 0) + 1
        return counts


@dataclass(frozen=True, slots=True)
class RobotsDecision:
    """What robots.txt said, and whether a human overrode it.

    `authenticated_override` is only ever set by the GUI's post-gate path.
    There is no flag that produces it, by design: overriding robots is a
    deliberate human action taken after logging in, not a string in a shell
    script that gets copied between runs.
    """

    allowed: bool
    rule_description: str | None = None
    authenticated_override: bool = False

    def as_manifest_entry(self) -> dict[str, object]:
        return {
            "robots_allowed": self.allowed,
            "robots_rule": self.rule_description,
            "robots_authenticated_override": self.authenticated_override,
        }


def collect_links(
    raw: Sequence[object],
    *,
    guard: UrlGuard,
    robots: RobotsPolicy | None = None,
    session_authenticated: bool = False,
    max_links: int | None = None,
) -> tuple[LinkCollection, list[RobotsDecision]]:
    """Vet raw DOM values into fetchable URLs.

    `session_authenticated` reflects the GUI's human gate having been passed.
    When it is False a robots Disallow is a hard rejection; when it is True the
    link is kept and the matched rule travels with it so the caller can emit
    the non-suppressible warning and record the override.
    """
    kept: list[str] = []
    rejected: list[RejectedLink] = []
    decisions: list[RobotsDecision] = []
    seen: set[str] = set()

    for candidate in raw:
        verdict: UrlVerdict = guard.check(candidate)
        if not verdict.allowed:
            assert verdict.reason is not None
            rejected.append(
                RejectedLink(value=verdict.url, reason=verdict.reason, detail=verdict.detail)
            )
            continue

        url = verdict.url
        if url in seen:
            rejected.append(RejectedLink(value=url, reason=RejectionReason.DUPLICATE))
            continue
        seen.add(url)

        decision = _apply_robots(url, robots, session_authenticated)
        if not decision.allowed:
            rejected.append(
                RejectedLink(
                    value=url,
                    reason=RejectionReason.ROBOTS_DISALLOWED,
                    detail=decision.rule_description or "",
                )
            )
            continue

        kept.append(url)
        decisions.append(decision)

    truncated = 0
    if max_links is not None and len(kept) > max_links:
        truncated = len(kept) - max_links
        kept = kept[:max_links]
        decisions = decisions[:max_links]

    return (
        LinkCollection(
            raw_count=len(raw),
            kept=tuple(kept),
            rejected=tuple(rejected),
            truncated=truncated,
        ),
        decisions,
    )


def _apply_robots(
    url: str,
    robots: RobotsPolicy | None,
    session_authenticated: bool,
) -> RobotsDecision:
    if robots is None or robots.can_fetch(url):
        return RobotsDecision(allowed=True)

    rule = robots.matched_rule(url)
    description = rule.describe() if rule is not None else "robots.txt disallows this path"

    if session_authenticated:
        # The escape hatch. A site frequently disallows the very paths a login
        # unlocks, so a hard refusal here would make the GUI's primary flow
        # fail on day one and train users to disable the check permanently.
        return RobotsDecision(
            allowed=True,
            rule_description=description,
            authenticated_override=True,
        )

    return RobotsDecision(allowed=False, rule_description=description)


@dataclass(frozen=True, slots=True)
class SelectorSet:
    """The three selectors the user supplies. No site defaults, ever."""

    link: str
    title: str
    content: str
    missing: tuple[str, ...] = field(default=())

    @classmethod
    def create(cls, link: str, title: str, content: str) -> SelectorSet:
        missing = tuple(
            name
            for name, value in (("link", link), ("title", title), ("content", content))
            if not value.strip()
        )
        return cls(link=link.strip(), title=title.strip(), content=content.strip(), missing=missing)

    @property
    def complete(self) -> bool:
        return not self.missing
