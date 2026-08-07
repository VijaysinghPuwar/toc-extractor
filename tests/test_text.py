"""Check the canonical text.py against what v1 actually did.

The golden fixture is the authority for PINNED cases. CHANGING cases assert the
new documented behaviour and additionally assert that it *differs* from v1,
so a regression that quietly restored v1 semantics would fail rather than pass.
"""

from __future__ import annotations

import json
import unicodedata
from pathlib import Path
from typing import Any

import pytest

from toc_extractor.text import (
    NAME_MAX_BYTES,
    AllocatedName,
    FilenameAllocator,
    clean_text,
    safe_filename,
    truncate_utf8,
)

from .golden.corpus import (
    CLEAN_TEXT_COMBOS,
    FILENAME_CHANGING,
    FILENAME_PINNED,
    TEXT_PINNED,
    Case,
)

GOLDEN_PATH = Path(__file__).parent / "golden" / "v1_golden.json"

# The v1 CLI implementation is canonical for safe_filename: it is the newer of
# the two scripts, it collapses runs of forbidden characters, and the collapse
# is safe only because FilenameAllocator exists. See text.py.
CANONICAL_IMPL = "cli"


@pytest.fixture(scope="session")
def golden() -> dict[str, Any]:
    return json.loads(GOLDEN_PATH.read_text(encoding="utf-8"))


def test_golden_was_captured_from_untouched_v1(golden: dict[str, Any]) -> None:
    """Guard against a golden recaptured after consolidation.

    A fixture recorded from v2 would make every assertion below a tautology.
    """
    provenance = golden["_provenance"]
    assert provenance["git_dirty"] is False
    assert provenance["git_describe"].startswith("v1.0.0")
    assert set(provenance["source_sha256"]) == {"toc_playwright.py", "cli_runner.py"}


# ---------------------------------------------------------------------------
# clean_text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("combo,remove_links,strip_ads", CLEAN_TEXT_COMBOS)
@pytest.mark.parametrize("case", TEXT_PINNED, ids=lambda c: c.id)
def test_clean_text_matches_v1(
    case: Case, combo: str, remove_links: bool, strip_ads: bool, golden: dict[str, Any]
) -> None:
    expected = golden["clean_text"][CANONICAL_IMPL][combo][case.id]["value"]
    result = clean_text(case.value, remove_links=remove_links, strip_ads=strip_ads)
    assert result.text == expected


@pytest.mark.parametrize("case", TEXT_PINNED, ids=lambda c: c.id)
def test_clean_text_identical_across_both_v1_impls(case: Case, golden: dict[str, Any]) -> None:
    """The two v1 copies never disagreed on clean_text; keep it that way."""
    for combo, _, _ in CLEAN_TEXT_COMBOS:
        gui = golden["clean_text"]["gui"][combo][case.id]["sha256"]
        cli = golden["clean_text"]["cli"][combo][case.id]["sha256"]
        assert gui == cli, f"v1 copies diverged on {case.id}/{combo}"


def test_stripped_url_count_is_reported() -> None:
    """Defect 6: the deletion stays, the silence does not."""
    result = clean_text("a https://a.com b http://b.com c https://c.com/x d")
    assert result.stripped_urls == 3
    assert "https://" not in result.text


def test_stripped_url_count_is_zero_when_links_are_kept() -> None:
    result = clean_text("see https://example.com/x", remove_links=False)
    assert result.stripped_urls == 0
    assert "https://example.com/x" in result.text


def test_stripped_url_count_zero_when_no_urls() -> None:
    assert clean_text("no links here").stripped_urls == 0


# ---------------------------------------------------------------------------
# safe_filename: pinned
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("case", FILENAME_PINNED, ids=lambda c: c.id)
def test_safe_filename_matches_canonical_v1(case: Case, golden: dict[str, Any]) -> None:
    expected = golden["safe_filename"][CANONICAL_IMPL][case.id]["value"]
    assert safe_filename(case.value) == expected


@pytest.mark.parametrize(
    "case_id",
    [
        "forbidden_run_two",
        "forbidden_run_mixed",
        "forbidden_run_long",
        "forbidden_only",
        "windows_path",
        "url_as_title",
    ],
)
def test_safe_filename_takes_cli_side_of_the_v1_drift(case_id: str, golden: dict[str, Any]) -> None:
    """These six are where the v1 copies disagreed. Record which side won."""
    gui = golden["safe_filename"]["gui"][case_id]["value"]
    cli = golden["safe_filename"]["cli"][case_id]["value"]
    assert gui != cli, f"{case_id} is no longer a divergence; remove it from this test"

    source = next(c for c in FILENAME_PINNED if c.id == case_id)
    assert safe_filename(source.value) == cli


# ---------------------------------------------------------------------------
# safe_filename: deliberate macOS changes
# ---------------------------------------------------------------------------


def _changing(case_id: str) -> Case:
    return next(c for c in FILENAME_CHANGING if c.id == case_id)


def test_nfd_and_nfc_titles_collapse_to_one_name(golden: dict[str, Any]) -> None:
    nfd = _changing("nfd_input")
    nfc = _changing("nfc_input")

    assert (
        golden["safe_filename"][CANONICAL_IMPL][nfd.id]["value"]
        != (golden["safe_filename"][CANONICAL_IMPL][nfc.id]["value"])
    ), "v1 must have kept these distinct, or this test proves nothing"

    assert safe_filename(nfd.value) == safe_filename(nfc.value)
    assert unicodedata.is_normalized("NFC", safe_filename(nfd.value))


@pytest.mark.parametrize("case_id", ["leading_dot", "leading_dots_multiple"])
def test_leading_dots_are_stripped(case_id: str, golden: dict[str, Any]) -> None:
    case = _changing(case_id)
    assert golden["safe_filename"][CANONICAL_IMPL][case_id]["value"].startswith(".")
    assert not safe_filename(case.value).startswith(".")


@pytest.mark.parametrize("case_id", ["cjk_over_255_bytes", "emoji_over_255_bytes"])
def test_byte_cap_replaces_character_cap(case_id: str, golden: dict[str, Any]) -> None:
    case = _changing(case_id)
    v1_bytes = golden["safe_filename"][CANONICAL_IMPL][case_id]["utf8_bytes"]
    assert v1_bytes > NAME_MAX_BYTES, "v1 must overrun here, or this test proves nothing"

    result = safe_filename(case.value)
    assert len(result.encode("utf-8")) <= NAME_MAX_BYTES
    # Truncation must land on a character boundary, not mid-sequence.
    assert result == result.encode("utf-8").decode("utf-8")


def test_colon_becomes_underscore() -> None:
    case = _changing("colon_macos")
    assert ":" not in safe_filename(case.value)


def test_control_characters_are_removed() -> None:
    """Broader than v1's tab/newline/CR set; a NUL makes os.open fail."""
    assert safe_filename("Chapter\x00One\x07Two") == "ChapterOneTwo"


# ---------------------------------------------------------------------------
# truncate_utf8
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,limit,expected",
    [
        ("abc", 10, "abc"),
        ("abc", 3, "abc"),
        ("abc", 2, "ab"),
        ("章章章", 9, "章章章"),
        ("章章章", 8, "章章"),
        ("章章章", 3, "章"),
        ("章章章", 2, ""),
        ("🙂🙂", 7, "🙂"),
    ],
)
def test_truncate_utf8(value: str, limit: int, expected: str) -> None:
    assert truncate_utf8(value, limit) == expected


# ---------------------------------------------------------------------------
# FilenameAllocator
# ---------------------------------------------------------------------------


def test_case_collision_is_deduplicated() -> None:
    """APFS is case-insensitive: v1 let the second of these overwrite the first."""
    alloc = FilenameAllocator()
    first = alloc.allocate(1, _changing("case_collision_upper").value)
    second = alloc.allocate(1, _changing("case_collision_lower").value)

    assert first == AllocatedName(name="001 - Chapter One.txt")
    assert second.deduplicated
    assert second.collided_with == "001 - Chapter One.txt"
    assert second.name == "001 - chapter one (2).txt"
    assert first.name.casefold() != second.name.casefold()


def test_distinct_titles_are_not_deduplicated() -> None:
    alloc = FilenameAllocator()
    assert not alloc.allocate(1, "Alpha").deduplicated
    assert not alloc.allocate(2, "Beta").deduplicated


def test_index_alone_keeps_identical_titles_distinct() -> None:
    """The NNN prefix means the common case never reaches the dedup path."""
    alloc = FilenameAllocator()
    assert not alloc.allocate(1, "Chapter").deduplicated
    assert not alloc.allocate(2, "Chapter").deduplicated


def test_third_collision_gets_its_own_suffix() -> None:
    alloc = FilenameAllocator()
    names = [alloc.allocate(1, t).name for t in ("Ch", "CH", "ch")]
    assert names == ["001 - Ch.txt", "001 - CH (2).txt", "001 - ch (3).txt"]
    assert len({n.casefold() for n in names}) == 3


def test_collapsed_runs_can_collide_and_are_caught() -> None:
    """The reason the run-collapse in safe_filename is safe.

    "A//B" and "A/B" both sanitise to "A_B"; without the allocator the second
    would overwrite the first.
    """
    assert safe_filename("A//B") == safe_filename("A/B") == "A_B"

    alloc = FilenameAllocator()
    first = alloc.allocate(7, "A//B")
    second = alloc.allocate(7, "A/B")
    assert first.name == "007 - A_B.txt"
    assert second.deduplicated
    assert second.name == "007 - A_B (2).txt"


def test_allocated_component_respects_byte_limit() -> None:
    alloc = FilenameAllocator()
    result = alloc.allocate(1, "章" * 300)
    assert len(result.name.encode("utf-8")) <= NAME_MAX_BYTES
    assert result.name.startswith("001 - ")
    assert result.name.endswith(".txt")


def test_dedup_marker_does_not_push_component_over_byte_limit() -> None:
    alloc = FilenameAllocator()
    long_title = "章" * 300
    first = alloc.allocate(1, long_title)
    second = alloc.allocate(1, long_title)

    assert second.deduplicated
    for name in (first.name, second.name):
        assert len(name.encode("utf-8")) <= NAME_MAX_BYTES
