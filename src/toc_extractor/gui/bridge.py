"""The GUI's run lifecycle and worker-to-UI messaging. No Tk import.

Two reasons this is a separate module and not methods on the window.

The original defect: v1 called Tk widget methods from a worker thread. Tk is
not thread-safe, and macOS is stricter than Linux about it, so it surfaces as
a hang or a crash rather than a warning. Keeping every decision here, with
widgets as a thin rendering layer, means there is no worker-side code holding
a widget reference to misuse.

The practical reason: CI cannot open a window. Anything that needs a display
is untestable, so the state machine, the queue protocol, and the per-chapter
bookkeeping live where a test can reach them without one.
"""

from __future__ import annotations

import queue
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from enum import StrEnum, auto

from ..models import ChapterRecord, FailedChapter, RunResult


class Phase(StrEnum):
    """Where a run is in its lifecycle.

    The human gate is two states, not one flag: BROWSING is "a browser is open
    and the user is doing whatever they need to", CONFIRMED is "the user said
    go". Nothing may fetch chapters until CONFIRMED, and only the user can
    cause that transition.
    """

    IDLE = auto()
    LAUNCHING = auto()
    BROWSING = auto()
    CONFIRMED = auto()
    EXTRACTING = auto()
    STOPPING = auto()
    DONE = auto()
    FAILED = auto()


# Which phases may follow which. Written out rather than implied by scattered
# checks so an illegal transition is a single assertion, not a bug that shows
# up as a button being clickable when it should not be.
_ALLOWED: dict[Phase, frozenset[Phase]] = {
    Phase.IDLE: frozenset({Phase.LAUNCHING}),
    Phase.LAUNCHING: frozenset({Phase.BROWSING, Phase.FAILED, Phase.IDLE}),
    Phase.BROWSING: frozenset({Phase.CONFIRMED, Phase.FAILED, Phase.IDLE}),
    Phase.CONFIRMED: frozenset({Phase.EXTRACTING, Phase.FAILED, Phase.IDLE}),
    Phase.EXTRACTING: frozenset({Phase.STOPPING, Phase.DONE, Phase.FAILED}),
    Phase.STOPPING: frozenset({Phase.DONE, Phase.FAILED}),
    Phase.DONE: frozenset({Phase.IDLE}),
    Phase.FAILED: frozenset({Phase.IDLE}),
}


class ChapterState(StrEnum):
    PENDING = auto()
    FETCHING = auto()
    RETRYING = auto()
    DONE = auto()
    FAILED = auto()
    SKIPPED = auto()


class IllegalTransition(RuntimeError):
    """A phase change the lifecycle does not permit."""


# -- messages ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LogLine:
    text: str
    level: str = "info"


@dataclass(frozen=True, slots=True)
class PhaseChanged:
    phase: Phase


@dataclass(frozen=True, slots=True)
class ChapterUpdate:
    index: int
    url: str
    state: ChapterState
    title: str = ""
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Progress:
    done: int
    total: int

    @property
    def fraction(self) -> float:
        return 0.0 if self.total <= 0 else min(1.0, self.done / self.total)


@dataclass(frozen=True, slots=True)
class RobotsOverride:
    """Emitted once when an authenticated session proceeds past a Disallow.

    Carried as its own message type rather than a log line because the UI must
    render it somewhere the user cannot scroll past or dismiss into silence.
    """

    rule_description: str


@dataclass(frozen=True, slots=True)
class Finished:
    result: RunResult | None
    error: str = ""
    cancelled: bool = False


Message = LogLine | PhaseChanged | ChapterUpdate | Progress | RobotsOverride | Finished


# -- state ------------------------------------------------------------------


@dataclass
class ChapterRow:
    index: int
    url: str
    state: ChapterState = ChapterState.PENDING
    title: str = ""
    detail: str = ""


@dataclass
class RunState:
    """Everything the window renders. Mutated only on the main thread."""

    phase: Phase = Phase.IDLE
    rows: dict[int, ChapterRow] = field(default_factory=dict)
    log: list[LogLine] = field(default_factory=list)
    robots_overrides: list[str] = field(default_factory=list)
    done: int = 0
    total: int = 0
    error: str = ""
    cancelled: bool = False

    @property
    def can_launch(self) -> bool:
        return self.phase in {Phase.IDLE, Phase.DONE, Phase.FAILED}

    @property
    def can_confirm(self) -> bool:
        return self.phase is Phase.BROWSING

    @property
    def can_extract(self) -> bool:
        return self.phase is Phase.CONFIRMED

    @property
    def can_stop(self) -> bool:
        return self.phase is Phase.EXTRACTING

    def ordered_rows(self) -> list[ChapterRow]:
        return [self.rows[index] for index in sorted(self.rows)]

    def count(self, state: ChapterState) -> int:
        return sum(1 for row in self.rows.values() if row.state is state)


def transition(current: Phase, target: Phase) -> Phase:
    """Advance the lifecycle, refusing a move the machine does not allow."""
    if target not in _ALLOWED[current]:
        raise IllegalTransition(f"{current.value} -> {target.value}")
    return target


class UiBridge:
    """The one channel from the worker thread to the UI.

    Workers only ever call publish(). The main thread only ever calls drain().
    That split is the whole point: there is no path by which a worker can touch
    a widget, because a worker cannot reach one.
    """

    def __init__(self, maxsize: int = 0) -> None:
        self._queue: queue.Queue[Message] = queue.Queue(maxsize=maxsize)

    # worker side

    def publish(self, message: Message) -> None:
        self._queue.put(message)

    def log(self, text: str, level: str = "info") -> None:
        self.publish(LogLine(text=text, level=level))

    def phase(self, phase: Phase) -> None:
        self.publish(PhaseChanged(phase=phase))

    def chapter(self, update: ChapterUpdate) -> None:
        self.publish(update)

    def progress(self, done: int, total: int) -> None:
        self.publish(Progress(done=done, total=total))

    def finished(
        self,
        result: RunResult | None = None,
        *,
        error: str = "",
        cancelled: bool = False,
    ) -> None:
        self.publish(Finished(result=result, error=error, cancelled=cancelled))

    # main-thread side

    def drain(self, limit: int = 200) -> list[Message]:
        """Take up to `limit` messages without blocking.

        Bounded so a fast run cannot starve the Tk event loop: draining
        everything available on each tick would let a 200-chapter burst hold
        the main thread long enough for the window to stop redrawing.
        """
        taken: list[Message] = []
        for _ in range(limit):
            try:
                taken.append(self._queue.get_nowait())
            except queue.Empty:
                break
        return taken

    def pending(self) -> int:
        return self._queue.qsize()


def apply(state: RunState, messages: Iterable[Message]) -> RunState:
    """Fold messages into the state. Pure bookkeeping, no widgets."""
    for message in messages:
        match message:
            case PhaseChanged(phase=phase):
                state.phase = transition(state.phase, phase)
            case LogLine():
                state.log.append(message)
            case ChapterUpdate():
                row = state.rows.setdefault(
                    message.index, ChapterRow(index=message.index, url=message.url)
                )
                row.state = message.state
                row.url = message.url or row.url
                if message.title:
                    row.title = message.title
                row.detail = message.detail
            case Progress(done=done, total=total):
                state.done, state.total = done, total
            case RobotsOverride(rule_description=rule):
                # Never cleared by later messages: the user has to see this.
                # Deduplicated because one Disallow typically covers every
                # chapter, and printing the same rule forty times turns a
                # warning that must be read into noise that will not be.
                if rule in state.robots_overrides:
                    continue
                state.robots_overrides.append(rule)
                state.log.append(
                    LogLine(
                        text=(
                            f"Proceeding past a robots.txt rule because you are signed in: {rule}"
                        ),
                        level="warning",
                    )
                )
            case Finished(result=_, error=error, cancelled=cancelled):
                state.error = error
                state.cancelled = cancelled
                target = Phase.FAILED if error else Phase.DONE
                state.phase = transition(state.phase, target)
    return state


def record_to_update(record: ChapterRecord) -> ChapterUpdate:
    return ChapterUpdate(
        index=record.index,
        url=record.requested_url,
        state=ChapterState.DONE,
        title=record.title,
    )


def failure_to_update(failure: FailedChapter) -> ChapterUpdate:
    return ChapterUpdate(
        index=failure.index,
        url=failure.url,
        state=ChapterState.FAILED,
        detail=f"{failure.reason} after {failure.attempts} attempt(s)",
    )


ProgressCallback = Callable[[int, int], None]
