"""Regenerate the README's CLI reference from the argparse parser.

Run after changing a flag:

    ./.venv/bin/python scripts_gen_readme.py

A test asserts the block in the README matches what the parser produces, so a
flag added without running this fails the suite rather than leaving the
reference quietly wrong.
"""

from __future__ import annotations

import contextlib
import io
import pathlib
import re

from toc_extractor.cli import build_parser

README = pathlib.Path(__file__).resolve().parent / "README.md"
START = "<!-- cli-reference: generated, do not edit by hand -->"
END = "<!-- /cli-reference -->"


def cli_help() -> str:
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        build_parser().print_help()
    return buffer.getvalue().rstrip()


def block() -> str:
    return f"{START}\n\n```\n{cli_help()}\n```\n\n{END}"


def main() -> int:
    text = README.read_text(encoding="utf-8")
    pattern = re.compile(re.escape(START) + r".*?" + re.escape(END), re.DOTALL)
    if not pattern.search(text):
        raise SystemExit(f"no CLI reference markers in {README}")
    README.write_text(pattern.sub(lambda _: block(), text), encoding="utf-8")
    print(f"regenerated the CLI reference ({len(cli_help().splitlines())} lines)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
