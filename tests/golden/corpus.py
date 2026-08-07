"""Input corpus for the v1 golden capture.

Shared by capture_v1.py (which records what v1 does) and test_text.py (which
asserts what v2 does). Keeping one corpus means the two can never drift apart.

Cases are split by contract, not by topic:

PINNED    v1 behaviour is the contract. v2 must reproduce it byte for byte.
CHANGING  v2 deliberately differs, for reasons recorded in `reason`. The golden
          still records v1's output so the change is visible in review rather
          than silent, but the test asserts the new documented behaviour.

Splitting them is what stops the golden from either rubber-stamping v2 or
blocking the macOS fixes it was written to enable.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Case:
    id: str
    value: str
    reason: str = ""
    tags: frozenset[str] = field(default_factory=frozenset)


# --------------------------------------------------------------------------
# clean_text
# --------------------------------------------------------------------------

TEXT_PINNED: list[Case] = [
    Case("empty", ""),
    Case("whitespace_only", "   \n\t  \n  "),
    Case("plain", "A quiet sentence with nothing to strip."),
    # Ad markers: v1 matches "Ads by\s+\w+" and "Sponsored\s+Content", both
    # case-insensitively. \w+ stops at punctuation, which is why the trailing
    # period survives in ad_marker_trailing_punct.
    Case("ad_marker_simple", "Chapter text.\nAds by PubRev\nMore text."),
    Case("ad_marker_case", "Text.\nADS BY SomeVendor\nsponsored   content\nEnd."),
    Case("ad_marker_trailing_punct", "Before. Ads by Vendor. After."),
    Case("ad_marker_multiword_vendor", "Ads by Big Ad Network here"),
    Case("ad_marker_no_space", "AdsbyVendor stays intact"),
    Case("sponsored_newline_between", "Sponsored\nContent removed"),
    # URL stripping. Note v1 uses \S+, so a URL followed by punctuation takes
    # the punctuation with it.
    Case("url_bare", "See https://example.com/page for details."),
    Case("url_trailing_period", "See https://example.com/page. Next sentence."),
    Case("url_http", "Visit http://example.org now"),
    Case("url_in_parens", "(https://example.com/x) after"),
    Case("url_multiple", "a https://a.com b http://b.com c"),
    Case("url_not_a_scheme", "ftp://example.com survives"),
    # Whitespace normalisation: \s+\n -> \n, \n\s+ -> \n, \n{3,} -> \n\n
    Case("ws_trailing_before_newline", "line one   \nline two"),
    Case("ws_leading_after_newline", "line one\n    line two"),
    Case("ws_triple_newline", "a\n\n\nb"),
    Case("ws_many_newlines", "a\n\n\n\n\n\nb"),
    Case("ws_double_newline_kept", "a\n\nb"),
    Case("ws_crlf", "a\r\nb\r\n\r\n\r\nc"),
    Case("ws_tabs", "a\tb\n\tc"),
    Case("ws_nbsp", "a\u00a0b\n\u00a0c"),
    # Interaction: stripping a URL can leave whitespace that then collapses.
    Case("combined_url_then_ws", "Read this:\n\nhttps://example.com/a\n\n\nThen this."),
    Case("combined_ad_then_ws", "Para one.\n\nAds by X\n\n\nPara two."),
    Case("unicode_text", "Ünïcödé — em dash, “smart quotes”, 中文字符, emoji 🙂"),
    Case("long_paragraph", ("word " * 400).strip()),
]

TEXT_CHANGING: list[Case] = []
"""v2 changes no clean_text behaviour.

Per the ruling on defect 6, the silent URL stripping stays exactly as it is;
what v2 adds is a count of how many URLs were removed, reported in the log and
the JSONL manifest. A count is new output, not changed output, so every
clean_text case above is PINNED.
"""


# --------------------------------------------------------------------------
# safe_filename
# --------------------------------------------------------------------------

_NFC = unicodedata.normalize("NFC", "Café Chapter")
_NFD = unicodedata.normalize("NFD", "Café Chapter")
assert _NFC != _NFD, "NFC/NFD fixture must actually differ"

FILENAME_PINNED: list[Case] = [
    Case("empty", ""),
    Case("whitespace_only", "   "),
    Case("plain", "Chapter One"),
    Case("already_clean", "Chapter 12 - The Long Road"),
    Case("tabs_newlines", "Chapter\tOne\nPart\rTwo"),
    Case("collapse_spaces", "Chapter    One     Again"),
    Case("leading_trailing_space", "   Chapter One   "),
    # The drifted regex. v1 GUI uses [...] (one _ per char), v1 CLI uses [...]+
    # (one _ per run). Single forbidden chars agree; runs do not.
    Case("forbidden_single", "Chapter/One"),
    Case("forbidden_each_once", 'a\\b/c:d*e?f"g<h>i|j'),
    Case("forbidden_run_two", "Chapter//One"),
    Case("forbidden_run_mixed", "Chapter<<>>One"),
    Case("forbidden_run_long", "a////////b"),
    Case("forbidden_only", "///"),
    Case("windows_path", r"C:\Users\me\file.txt"),
    Case("url_as_title", "https://example.com/chapter/1"),
    Case("length_149", "x" * 149),
    Case("length_150", "x" * 150),
    Case("length_151", "x" * 151),
    Case("length_300", "y" * 300),
]

FILENAME_CHANGING: list[Case] = [
    Case(
        "nfd_input",
        _NFD,
        reason=(
            "macOS hands back NFD from the filesystem while the DOM supplies NFC. "
            "v1 passes both through unchanged, so a title read from disk stops "
            "matching the same title read from the page. v2 normalises to NFC."
        ),
        tags=frozenset({"macos", "nfc"}),
    ),
    Case(
        "nfc_input",
        _NFC,
        reason="Paired with nfd_input: both must land on the same NFC output in v2.",
        tags=frozenset({"macos", "nfc"}),
    ),
    Case(
        "colon_macos",
        "Chapter 1: The Beginning",
        reason=(
            "v1 already maps ':' to '_' via the forbidden class, so this is "
            "pinned in practice. Listed here because Finder displays ':' as '/' "
            "and the README needs to explain why titles with colons look odd."
        ),
        tags=frozenset({"macos"}),
    ),
    Case(
        "leading_dot",
        ".hidden chapter",
        reason="v1 keeps the dot, which hides the file in Finder and ls. v2 strips it.",
        tags=frozenset({"macos"}),
    ),
    Case(
        "leading_dots_multiple",
        "...three dots",
        reason="Same as leading_dot; v2 strips the whole leading run.",
        tags=frozenset({"macos"}),
    ),
    Case(
        "cjk_over_255_bytes",
        "章" * 120,
        reason=(
            "120 CJK chars is 360 UTF-8 bytes. v1 caps at 150 characters (450 "
            "bytes), which overruns the 255-byte APFS limit on a name that v1 "
            "considers short enough. v2 caps on encoded bytes."
        ),
        tags=frozenset({"macos", "bytes"}),
    ),
    Case(
        "emoji_over_255_bytes",
        "🙂" * 100,
        reason="100 emoji is 400 UTF-8 bytes; v2 must not split a character mid-sequence.",
        tags=frozenset({"macos", "bytes"}),
    ),
    Case(
        "case_collision_upper",
        "Chapter One",
        reason=(
            "APFS is case-insensitive by default, so this and "
            "case_collision_lower resolve to the same file. v1 silently "
            "overwrites; v2 deduplicates with a numeric suffix and logs it."
        ),
        tags=frozenset({"macos", "collision"}),
    ),
    Case(
        "case_collision_lower",
        "chapter one",
        reason="Paired with case_collision_upper.",
        tags=frozenset({"macos", "collision"}),
    ),
]


TEXT_CASES = TEXT_PINNED + TEXT_CHANGING
FILENAME_CASES = FILENAME_PINNED + FILENAME_CHANGING

# clean_text flag combinations. The first is what every v1 caller uses with no
# CLI flags set, and is what the byte-identity claim in the README rests on.
CLEAN_TEXT_COMBOS: list[tuple[str, bool, bool]] = [
    ("default", True, True),
    ("include_links", False, True),
    ("no_strip_ads", True, False),
    ("include_links_no_strip_ads", False, False),
]
