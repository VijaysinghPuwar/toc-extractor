"""The Tk window. The only module in the package that imports tkinter.

Everything here runs on the main thread. Worker output arrives solely through
UiBridge.drain(), called from root.after, so no widget method is ever reached
from another thread — the v1 defect this rewrite exists to remove.
"""

from __future__ import annotations

import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..exporters import DEFAULT_FORMAT, available
from ..fetcher import FetchOptions
from ..parser import SelectorSet
from .bridge import ChapterState, Phase, RunState, UiBridge, apply
from .worker import ExtractionWorker, RunRequest

TITLE = "TOC Extractor"
DRAIN_INTERVAL_MS = 100
PROFILE_DIR = Path("./.pw_profile").resolve()

_STATE_LABEL = {
    ChapterState.PENDING: "pending",
    ChapterState.FETCHING: "fetching",
    ChapterState.RETRYING: "retrying",
    ChapterState.DONE: "done",
    ChapterState.FAILED: "failed",
    ChapterState.SKIPPED: "already done",
}


class ExtractorWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.title(TITLE)
        self.root.geometry("980x760")
        self.root.minsize(900, 700)

        self.bridge = UiBridge()
        self.state = RunState()
        self.worker = ExtractionWorker(self.bridge)

        self.toc_url = tk.StringVar()
        self.link_selector = tk.StringVar()
        self.title_selector = tk.StringVar()
        self.content_selector = tk.StringVar()
        self.output_dir = tk.StringVar(value=str(Path("downloads").resolve()))
        self.max_chapters = tk.IntVar(value=20)
        self.concurrency = tk.IntVar(value=3)
        self.retries = tk.IntVar(value=2)
        self.min_delay = tk.DoubleVar(value=1.2)
        self.max_delay = tk.DoubleVar(value=2.5)
        self.include_links = tk.BooleanVar(value=False)
        self.strip_ads = tk.BooleanVar(value=True)
        self.force = tk.BooleanVar(value=False)
        self.formats = {name: tk.BooleanVar(value=name == DEFAULT_FORMAT) for name in available()}

        self._build()
        self._render()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(DRAIN_INTERVAL_MS, self._tick)

    # -- layout -------------------------------------------------------------

    def _build(self) -> None:
        source = ttk.LabelFrame(self.root, text="Source and selectors")
        source.pack(fill="x", padx=8, pady=4)
        source.columnconfigure(1, weight=1)

        for row, (label, var) in enumerate(
            (
                ("Table of contents URL:", self.toc_url),
                ("Chapter link selector:", self.link_selector),
                ("Title selector:", self.title_selector),
                ("Content selector:", self.content_selector),
            )
        ):
            ttk.Label(source, text=label).grid(row=row, column=0, sticky="e", padx=8, pady=4)
            ttk.Entry(source, textvariable=var).grid(row=row, column=1, sticky="we", padx=8, pady=4)

        options = ttk.LabelFrame(self.root, text="Options")
        options.pack(fill="x", padx=8, pady=4)
        options.columnconfigure(1, weight=1)

        ttk.Label(options, text="Output folder:").grid(row=0, column=0, sticky="e", padx=8, pady=4)
        ttk.Entry(options, textvariable=self.output_dir).grid(
            row=0, column=1, sticky="we", padx=8, pady=4
        )
        ttk.Button(options, text="Browse", command=self._choose_dir).grid(
            row=0, column=2, padx=8, pady=4
        )

        numbers = ttk.Frame(options)
        numbers.grid(row=1, column=0, columnspan=3, sticky="w", padx=8, pady=4)
        for position, (label, number_var, width) in enumerate(
            (
                ("Max chapters", self.max_chapters, 6),
                ("Concurrency", self.concurrency, 4),
                ("Retries", self.retries, 4),
                ("Min delay (s)", self.min_delay, 6),
                ("Max delay (s)", self.max_delay, 6),
            )
        ):
            ttk.Label(numbers, text=label).grid(
                row=0, column=position * 2, sticky="e", padx=8, pady=2
            )
            ttk.Entry(numbers, textvariable=number_var, width=width).grid(
                row=0, column=position * 2 + 1
            )

        toggles = ttk.Frame(options)
        toggles.grid(row=2, column=0, columnspan=3, sticky="w", padx=8, pady=4)
        ttk.Checkbutton(toggles, text="Include source URLs", variable=self.include_links).pack(
            side="left", padx=6
        )
        ttk.Checkbutton(toggles, text="Strip ad markers", variable=self.strip_ads).pack(
            side="left", padx=6
        )
        ttk.Checkbutton(toggles, text="Ignore saved progress", variable=self.force).pack(
            side="left", padx=6
        )

        formats = ttk.Frame(options)
        formats.grid(row=3, column=0, columnspan=3, sticky="w", padx=8, pady=4)
        ttk.Label(formats, text="Formats:").pack(side="left", padx=(8, 2))
        for name, format_var in self.formats.items():
            ttk.Checkbutton(formats, text=name, variable=format_var).pack(side="left", padx=4)

        controls = ttk.LabelFrame(self.root, text="Run")
        controls.pack(fill="x", padx=8, pady=4)
        self.btn_launch = ttk.Button(controls, text="1. Launch browser", command=self._on_launch)
        self.btn_launch.pack(side="left", padx=8, pady=4)
        self.btn_confirm = ttk.Button(
            controls, text="2. I'm Ready", command=self._on_confirm, state="disabled"
        )
        self.btn_confirm.pack(side="left", padx=8, pady=4)
        self.btn_extract = ttk.Button(
            controls, text="3. Start extraction", command=self._on_extract, state="disabled"
        )
        self.btn_extract.pack(side="left", padx=8, pady=4)
        self.btn_stop = ttk.Button(controls, text="Stop", command=self._on_stop, state="disabled")
        self.btn_stop.pack(side="left", padx=8, pady=4)

        self.progress = ttk.Progressbar(self.root, mode="determinate", maximum=100)
        self.progress.pack(fill="x", padx=8)
        self.status = ttk.Label(self.root, text="Idle.")
        self.status.pack(anchor="w", padx=10, pady=2)

        # A dedicated strip for robots overrides. Not the log, because the log
        # scrolls and this must not be scrollable into silence.
        self.robots_banner = tk.Label(
            self.root, text="", anchor="w", justify="left", wraplength=940, fg="#7a3b00"
        )

        panes = ttk.PanedWindow(self.root, orient="vertical")
        panes.pack(fill="both", expand=True, padx=8, pady=4)

        chapters = ttk.Frame(panes)
        self.tree = ttk.Treeview(
            chapters, columns=("state", "title", "detail"), show="headings", height=10
        )
        for name, heading, width in (
            ("state", "State", 110),
            ("title", "Title", 380),
            ("detail", "Detail", 340),
        ):
            self.tree.heading(name, text=heading)
            self.tree.column(name, width=width, anchor="w")
        self.tree.pack(fill="both", expand=True, side="left")
        ttk.Scrollbar(chapters, orient="vertical", command=self.tree.yview).pack(
            fill="y", side="right"
        )
        panes.add(chapters, weight=3)

        logs = ttk.Frame(panes)
        self.log = tk.Text(logs, height=10, wrap="word")
        self.log.pack(fill="both", expand=True)
        panes.add(logs, weight=2)

    # -- user actions -------------------------------------------------------

    def _choose_dir(self) -> None:
        chosen = filedialog.askdirectory(initialdir=self.output_dir.get() or ".")
        if chosen:
            self.output_dir.set(chosen)

    def _request(self) -> RunRequest | None:
        selectors = SelectorSet.create(
            link=self.link_selector.get(),
            title=self.title_selector.get(),
            content=self.content_selector.get(),
        )
        if not selectors.complete:
            messagebox.showerror(TITLE, "Missing selectors: " + ", ".join(selectors.missing))
            return None
        if not self.toc_url.get().strip():
            messagebox.showerror(TITLE, "A table-of-contents URL is required.")
            return None

        chosen = tuple(name for name, var in self.formats.items() if var.get())
        options = FetchOptions(
            concurrency=max(1, self.concurrency.get()),
            retries=max(0, self.retries.get()),
            min_delay=max(0.0, self.min_delay.get()),
            max_delay=max(self.min_delay.get(), self.max_delay.get()),
            include_links=self.include_links.get(),
            strip_ads=self.strip_ads.get(),
            max_links=max(1, self.max_chapters.get()),
        )
        return RunRequest(
            toc_url=self.toc_url.get().strip(),
            selectors=selectors,
            output_dir=Path(self.output_dir.get()),
            formats=chosen or (DEFAULT_FORMAT,),
            options=options,
            force=self.force.get(),
            profile_dir=PROFILE_DIR,
        )

    def _on_launch(self) -> None:
        request = self._request()
        if request is not None:
            self.worker.launch(request)

    def _on_confirm(self) -> None:
        self.worker.confirm()

    def _on_extract(self) -> None:
        self.worker.extract()

    def _on_stop(self) -> None:
        self.worker.stop()

    def _on_close(self) -> None:
        self.worker.shutdown()
        self.root.destroy()

    # -- the one place worker output enters the UI --------------------------

    def _tick(self) -> None:
        messages = self.bridge.drain()
        if messages:
            apply(self.state, messages)
            self._render()
        self.root.after(DRAIN_INTERVAL_MS, self._tick)

    def _render(self) -> None:
        state = self.state
        self.btn_launch.config(state="normal" if state.can_launch else "disabled")
        self.btn_confirm.config(state="normal" if state.can_confirm else "disabled")
        self.btn_extract.config(state="normal" if state.can_extract else "disabled")
        self.btn_stop.config(state="normal" if state.can_stop else "disabled")

        self.progress.config(value=100 * _fraction(state.done, state.total))
        self.status.config(text=_status_text(state))

        if state.robots_overrides:
            self.robots_banner.config(
                text=(
                    "Signed-in session: proceeding past robots.txt rules that would "
                    "otherwise stop this run.\n"
                    + "\n".join(f"  {rule}" for rule in state.robots_overrides)
                )
            )
            self.robots_banner.pack(fill="x", padx=10, pady=4, before=self.progress)

        self.tree.delete(*self.tree.get_children())
        for row in state.ordered_rows():
            self.tree.insert(
                "",
                "end",
                iid=str(row.index),
                values=(_STATE_LABEL[row.state], row.title or row.url, row.detail),
            )

        self.log.delete("1.0", "end")
        for line in state.log[-500:]:
            prefix = "" if line.level == "info" else f"{line.level}: "
            self.log.insert("end", prefix + line.text + "\n")
        self.log.see("end")


def _fraction(done: int, total: int) -> float:
    return 0.0 if total <= 0 else min(1.0, done / total)


def _status_text(state: RunState) -> str:
    if state.phase is Phase.IDLE:
        return "Idle."
    if state.phase is Phase.LAUNCHING:
        return "Opening the browser."
    if state.phase is Phase.BROWSING:
        return "Sign in or solve any challenge in the browser, then press I'm Ready."
    if state.phase is Phase.CONFIRMED:
        return "Ready. Press Start extraction."
    if state.phase is Phase.EXTRACTING:
        return f"Extracting: {state.done} of {state.total}."
    if state.phase is Phase.STOPPING:
        return "Stopping."
    if state.phase is Phase.FAILED:
        return f"Failed: {state.error}"
    if state.cancelled:
        return "Stopped. Run again to resume."
    return (
        f"Done. {state.count(ChapterState.DONE)} fetched, "
        f"{state.count(ChapterState.FAILED)} failed."
    )


def main() -> int:
    from .tkcheck import require

    require()
    root = tk.Tk()
    ExtractorWindow(root)
    root.mainloop()
    return 0
