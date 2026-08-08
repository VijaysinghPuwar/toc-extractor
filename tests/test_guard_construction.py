"""The fixture-origin exemption must stay reachable only from test code.

test_browser.py subclasses UrlGuard to exempt the fixture server on 127.0.0.1,
because the real policy rejects it as a private literal before any redirect
runs. That subclass is the only permissive path in the codebase, and the
failure mode this file guards against is a later refactor exposing the same
capability through a constructor argument, a config key, or a CLI flag - at
which point the guard has a documented bypass nobody decided to add.

These assertions are deliberately structural rather than behavioural. A
behavioural test would pass right up until someone added the escape hatch.
"""

from __future__ import annotations

import inspect
from collections.abc import Sequence

import pytest

from toc_extractor.politeness import (
    RejectionReason,
    UrlGuard,
    UrlVerdict,
    build_url_guard,
)

# Anything matching one of these in a parameter name would be a per-origin
# exemption by another name.
SUSPICIOUS = ("allowlist", "allow_list", "whitelist", "exempt", "origin", "trusted", "bypass")


def test_build_url_guard_returns_the_real_class_not_a_subclass() -> None:
    guard = build_url_guard()
    assert type(guard) is UrlGuard


def test_build_url_guard_does_not_override_check() -> None:
    """A subclass could keep the type check honest and still change the answer."""
    guard = build_url_guard()
    assert type(guard).check is UrlGuard.check


def test_build_url_guard_exposes_only_the_all_or_nothing_switch() -> None:
    parameters = inspect.signature(build_url_guard).parameters
    assert set(parameters) == {"allow_private_hosts"}
    assert parameters["allow_private_hosts"].annotation == "bool"


def test_url_guard_constructor_has_no_per_origin_exemption() -> None:
    parameters = set(inspect.signature(UrlGuard.__init__).parameters) - {"self"}
    assert parameters == {"allow_private_hosts", "resolver"}

    for name in parameters:
        assert not any(token in name.lower() for token in SUSPICIOUS), name


def test_no_guard_entry_point_offers_an_exemption_hook() -> None:
    """Scoped to the guard on purpose.

    A first draft scanned every callable in politeness and flagged
    RobotsPolicy(origin=...), where an origin parameter is exactly right. The
    invariant is about the guard, so widening the net past it only produces
    noise a later reader will silence by deleting the test.
    """
    entry_points = [build_url_guard, UrlGuard.__init__, UrlGuard.check]

    for value in entry_points:
        for parameter in inspect.signature(value).parameters:
            assert not any(token in parameter.lower() for token in SUSPICIOUS), (
                f"{value.__qualname__}({parameter}) looks like a per-origin exemption"
            )


def test_the_construction_path_still_rejects_a_fixture_shaped_origin() -> None:
    """The exact host test_browser.py has to subclass its way around."""
    verdict = build_url_guard().check("http://127.0.0.1:65200/redirect/to-private")
    assert not verdict
    assert verdict.reason is RejectionReason.PRIVATE_ADDRESS


def test_allow_private_hosts_is_all_or_nothing_not_per_origin() -> None:
    guard = build_url_guard(allow_private_hosts=True)
    assert guard.check("http://127.0.0.1:8080/x")
    assert guard.check("http://10.0.0.1/x")
    assert guard.check("http://169.254.169.254/latest/")


def test_allow_private_hosts_still_refuses_disallowed_schemes() -> None:
    """The private-host switch must not double as a scheme exemption."""
    verdict = build_url_guard(allow_private_hosts=True).check("file:///etc/passwd")
    assert verdict.reason is RejectionReason.DISALLOWED_SCHEME


def test_the_test_only_subclass_is_what_grants_the_exemption() -> None:
    """Pin the mechanism, so removing it from tests fails here rather than silently."""

    class FixtureGuard(UrlGuard):
        def check(self, candidate: object) -> UrlVerdict:
            if isinstance(candidate, str) and candidate.startswith("http://127.0.0.1:"):
                return UrlVerdict(url=candidate, allowed=True)
            return super().check(candidate)

    def resolver(_host: str) -> Sequence[str]:
        return ["93.184.216.34"]

    exempting = FixtureGuard(allow_private_hosts=False, resolver=resolver)
    assert exempting.check("http://127.0.0.1:9999/x")
    assert not build_url_guard().check("http://127.0.0.1:9999/x")


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest/meta-data/",
        "http://127.0.0.1/admin",
        "file:///etc/passwd",
        "javascript:alert(1)",
    ],
)
def test_default_construction_refuses_the_usual_suspects(url: str) -> None:
    assert not build_url_guard().check(url)


# ---------------------------------------------------------------------------
# The path cli.py actually uses
# ---------------------------------------------------------------------------


def test_cli_constructs_the_guard_only_through_build_url_guard() -> None:
    """The exemption must not become reachable from a command line.

    Structural, because a behavioural test would keep passing right up until
    someone wired a flag to it.
    """
    import inspect

    from toc_extractor import cli

    source = inspect.getsource(cli)
    assert "build_url_guard(" in source
    assert "UrlGuard(" not in source, "cli.py must not construct UrlGuard directly"


def test_no_cli_flag_grants_a_per_origin_exemption() -> None:
    from toc_extractor.cli import build_parser

    for action in build_parser()._actions:
        for option in action.option_strings:
            assert not any(token in option.lower() for token in SUSPICIOUS), option


def test_the_only_guard_flag_is_the_all_or_nothing_switch() -> None:
    from toc_extractor.cli import build_parser

    guard_flags = {
        option
        for action in build_parser()._actions
        for option in action.option_strings
        if "host" in option or "private" in option
    }
    assert guard_flags == {"--allow-private-hosts"}
