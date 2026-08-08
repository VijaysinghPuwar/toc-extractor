"""Tkinter front end.

`bridge` holds the run lifecycle and the worker-to-UI queue and imports no Tk.
`app` is the window: it renders `bridge.RunState` and owns every widget call.
Importing this package does not import Tk, so a headless environment can use
the bridge without a display.
"""

from __future__ import annotations

__all__ = ["bridge"]
