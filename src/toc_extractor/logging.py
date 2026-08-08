"""Logging for humans reading a terminal.

One formatter, three levels. No JSON mode: nothing ingests this tool's output,
and a second format would be surface without a decision behind it.

Absolute imports mean `import logging` here is the stdlib module, not this one.
"""

from __future__ import annotations

import logging
import sys
from typing import TextIO

LOGGER_NAME = "toc_extractor"

QUIET = logging.WARNING
NORMAL = logging.INFO
VERBOSE = logging.DEBUG


class _Formatter(logging.Formatter):
    """`info` messages print bare; anything else is labelled.

    Progress lines are the common case and a LEVEL prefix on every one of them
    is noise. Warnings and errors need to stand out from that stream, which is
    the whole reason the resume notice and the renumbering warning are not
    print() calls.
    """

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        if record.levelno <= logging.INFO:
            return message
        return f"{record.levelname.lower()}: {message}"


def configure(*, verbose: bool = False, quiet: bool = False, stream: TextIO | None = None) -> None:
    """Attach one handler to this package's logger. Safe to call twice."""
    logger = logging.getLogger(LOGGER_NAME)
    logger.setLevel(VERBOSE if verbose else QUIET if quiet else NORMAL)
    logger.propagate = False

    for existing in list(logger.handlers):
        logger.removeHandler(existing)

    handler = logging.StreamHandler(stream if stream is not None else sys.stderr)
    handler.setFormatter(_Formatter())
    logger.addHandler(handler)


def get_logger(name: str | None = None) -> logging.Logger:
    return logging.getLogger(LOGGER_NAME if name is None else f"{LOGGER_NAME}.{name}")
