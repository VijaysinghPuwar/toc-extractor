"""Selector profiles: parsing, precedence, and refusing what it cannot use."""

from __future__ import annotations

from pathlib import Path

import pytest

from toc_extractor.cli import build_parser
from toc_extractor.config import (
    OPTION_FLAGS,
    ProfileError,
    explicit_flags,
    load_profile,
    merge,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = REPO_ROOT / "profiles" / "example.toml"

MINIMAL = """
[selectors]
link = "ol.toc a"
title = "h1.title"
content = "article.reader"
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "profile.toml"
    path.write_text(text, encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# The shipped example
# ---------------------------------------------------------------------------


def test_the_shipped_example_parses() -> None:
    profile = load_profile(EXAMPLE)
    assert profile.selectors == {
        "link": "ol.toc a",
        "title": "h1.title",
        "content": "article.reader",
    }


def test_the_shipped_example_matches_the_shipped_fixture() -> None:
    """The example must be runnable, and must describe no real site.

    A profile written against a site nobody can reach is documentation that
    cannot be checked; one written against a real site would be the
    site-specific knowledge this design keeps out of the repository.
    """
    toc = (REPO_ROOT / "tests" / "fixtures" / "example_toc.html").read_text(encoding="utf-8")
    chapter = (REPO_ROOT / "tests" / "fixtures" / "example_chapter.html").read_text(
        encoding="utf-8"
    )

    assert 'class="toc"' in toc
    assert 'class="title"' in chapter
    assert 'class="reader"' in chapter


def test_the_shipped_example_names_no_real_host() -> None:
    text = EXAMPLE.read_text(encoding="utf-8")
    assert "https://" not in text.replace("https://example.com/toc", "")


def test_every_documented_option_maps_to_a_real_flag() -> None:
    """A profile key with no flag behind it is a described-but-unwired feature."""
    flags = {
        option
        for action in build_parser()._actions
        for option in action.option_strings
        if option.startswith("--")
    }
    assert set(OPTION_FLAGS.values()) <= flags


# ---------------------------------------------------------------------------
# Precedence
# ---------------------------------------------------------------------------


def test_a_profile_supplies_selectors(tmp_path: Path) -> None:
    profile = load_profile(write(tmp_path, MINIMAL))
    argv = ["--toc", "https://example.com/toc"]
    args = build_parser().parse_args(argv)

    merge(args, profile, argv)
    assert args.link == "ol.toc a"
    assert args.content == "article.reader"


def test_an_explicit_flag_beats_the_profile(tmp_path: Path) -> None:
    profile = load_profile(write(tmp_path, MINIMAL))
    argv = ["--toc", "https://example.com/toc", "--content", "div.mine"]
    args = build_parser().parse_args(argv)

    merge(args, profile, argv)
    assert args.content == "div.mine"
    assert args.link == "ol.toc a"


def test_a_flag_set_to_its_own_default_still_beats_the_profile(tmp_path: Path) -> None:
    """The reason precedence reads argv rather than comparing to defaults.

    Comparing a parsed value against the parser default cannot distinguish
    "not given" from "given, and equal to the default", so a profile would
    quietly override a flag the user did pass.
    """
    profile = load_profile(write(tmp_path, MINIMAL + "\n[options]\nconcurrency = 7\n"))
    argv = ["--toc", "https://example.com/toc", "--concurrency", "3"]
    args = build_parser().parse_args(argv)

    assert args.concurrency == 3, "3 is also the parser default, which is the point"
    merge(args, profile, argv)
    assert args.concurrency == 3


def test_equals_form_is_recognised_as_explicit(tmp_path: Path) -> None:
    profile = load_profile(write(tmp_path, MINIMAL + "\n[options]\nconcurrency = 7\n"))
    argv = ["--toc", "https://example.com/toc", "--concurrency=3"]
    args = build_parser().parse_args(argv)

    merge(args, profile, argv)
    assert args.concurrency == 3


def test_profile_options_reach_the_namespace(tmp_path: Path) -> None:
    text = (
        MINIMAL
        + """
[options]
min_delay = 2.5
max_delay = 4.0
concurrency = 2
max = 25
formats = ["text", "jsonl"]
include_links = true
"""
    )
    profile = load_profile(write(tmp_path, text))
    argv = ["--toc", "https://example.com/toc"]
    args = build_parser().parse_args(argv)

    merge(args, profile, argv)
    assert args.min_delay == 2.5
    assert args.max == 25
    assert args.formats == ["text", "jsonl"]
    assert args.include_links is True


def test_merge_reports_what_it_applied(tmp_path: Path) -> None:
    profile = load_profile(write(tmp_path, MINIMAL + "\n[options]\nmax = 9\n"))
    argv = ["--toc", "https://example.com/toc", "--link", "a.mine"]
    args = build_parser().parse_args(argv)

    applied = merge(args, profile, argv)
    assert set(applied) == {"title", "content", "max"}


@pytest.mark.parametrize(
    "argv,expected",
    [
        (["--toc", "x"], {"--toc"}),
        (["--toc=x", "--max=3"], {"--toc", "--max"}),
        (["-v", "--quiet"], {"--quiet"}),
        ([], set()),
    ],
)
def test_explicit_flags(argv: list[str], expected: set[str]) -> None:
    assert explicit_flags(argv) == expected


# ---------------------------------------------------------------------------
# Refusing what it cannot use
# ---------------------------------------------------------------------------


def test_a_missing_profile_is_named(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="no profile at"):
        load_profile(tmp_path / "nope.toml")


def test_invalid_toml_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="not valid TOML"):
        load_profile(write(tmp_path, "[selectors\nlink ="))


def test_an_unknown_selector_key_is_refused(tmp_path: Path) -> None:
    """A silently ignored typo is a profile that does not do what it says."""
    with pytest.raises(ProfileError, match="unknown key"):
        load_profile(write(tmp_path, '[selectors]\nlinks = "a"\n'))


def test_an_unknown_option_key_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="unknown key"):
        load_profile(write(tmp_path, MINIMAL + "\n[options]\nconcurency = 4\n"))


def test_the_refusal_lists_the_valid_keys(tmp_path: Path) -> None:
    with pytest.raises(ProfileError) as caught:
        load_profile(write(tmp_path, MINIMAL + "\n[options]\nnonsense = 1\n"))
    assert "concurrency" in str(caught.value)


def test_a_non_string_selector_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="must be a string"):
        load_profile(write(tmp_path, "[selectors]\nlink = 3\n"))


def test_formats_must_be_a_list_of_strings(tmp_path: Path) -> None:
    with pytest.raises(ProfileError, match="list of strings"):
        load_profile(write(tmp_path, MINIMAL + '\n[options]\nformats = "text"\n'))


def test_an_empty_profile_is_valid_and_supplies_nothing(tmp_path: Path) -> None:
    profile = load_profile(write(tmp_path, ""))
    assert profile.selectors == {}
    assert profile.options == {}


def test_a_partial_profile_supplies_only_what_it_has(tmp_path: Path) -> None:
    profile = load_profile(write(tmp_path, '[selectors]\ntitle = "h1"\n'))
    argv = ["--toc", "https://example.com/toc", "--link", "a", "--content", "article"]
    args = build_parser().parse_args(argv)

    merge(args, profile, argv)
    assert args.title == "h1"
    assert args.link == "a"
