"""Terminal utilities: input guard against buffered keystrokes and live status display."""

from __future__ import annotations

import io
import os
import select
import sys
import termios
import threading
import time
import tty
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

    from rich.console import Console

_CLEAR_LINE = "\r\033[K"
_BUSY_WARNING = (
    "\nAgent is working — please wait for the current turn to finish (Ctrl-C to cancel)."
)


def _clear_status_line() -> None:
    if sys.stderr.isatty():
        sys.stderr.write(_CLEAR_LINE)
        sys.stderr.flush()


class InputGuard:
    """Disallows buffered input during agent turns, draining keystrokes and warning user."""

    def __init__(
        self,
        console: Console | None = None,
        fd: int | None = None,
        warning_interval: float = 3.0,
    ) -> None:
        self.console = console
        self._fd = fd
        self._warning_interval = warning_interval
        self._stop_event = threading.Event()
        self._paused = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_warned = 0.0
        # Serialises everything written to the terminal while the guard is up, so the
        # status line, the busy warning and the REPL's own output never interleave.
        self._output_lock = threading.Lock()
        self.old_settings: list[Any] | None = None

    def __enter__(self) -> InputGuard:
        self._stop_event.clear()
        self._paused.clear()
        fd = self._guarded_fd()
        if fd is None:
            return self
        self._enter_cbreak(fd)
        self._thread = threading.Thread(target=self._watch, args=(fd,), daemon=True)
        self._thread.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.suspend()
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=0.2)

    def _guarded_fd(self) -> int | None:
        if self._fd is None and sys.stdin.isatty():
            self._fd = sys.stdin.fileno()
        return self._fd

    def _enter_cbreak(self, fd: int) -> None:
        try:
            self.old_settings = termios.tcgetattr(fd)
            tty.setcbreak(fd)
        except (termios.error, io.UnsupportedOperation):
            self.old_settings = None

    def suspend(self) -> None:
        self._paused.set()
        if self._fd is None or self.old_settings is None:
            return
        try:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self.old_settings)
            termios.tcflush(self._fd, termios.TCIFLUSH)
        except (termios.error, io.UnsupportedOperation):
            pass

    def resume(self) -> None:
        # Only a terminal we put into cbreak goes back into it; anything else just unpauses.
        if self._fd is not None and self.old_settings is not None:
            self._enter_cbreak(self._fd)
        self._paused.clear()

    @contextmanager
    def printing(self) -> Iterator[None]:
        """Holds the terminal for a caller's output, with the status line cleared first."""
        with self._output_lock:
            _clear_status_line()
            yield

    def _watch(self, fd: int) -> None:
        while not self._stop_event.is_set():
            if self._paused.is_set():
                time.sleep(0.05)
                continue
            try:
                self._drain(fd)
            except (OSError, ValueError):
                break

    def _drain(self, fd: int) -> None:
        ready, _, _ = select.select([fd], [], [], 0.05)
        if not ready or self._paused.is_set():
            return
        os.read(fd, 1024)
        now = time.monotonic()
        if now - self._last_warned > self._warning_interval:
            self._last_warned = now
            self._warn()

    def _warn(self) -> None:
        with self.printing():
            if self.console is not None:
                self.console.print(_BUSY_WARNING, markup=False, highlight=False)
                return
            sys.stderr.write(_BUSY_WARNING + "\n")
            sys.stderr.flush()


class TurnStatus(InputGuard):
    """Renders a live status bar with elapsed timer and guards input during turns."""

    def __init__(
        self,
        console: Console | None = None,
        badge: str = "[area] package",
        initial_status: str = "Processing...",
        fd: int | None = None,
    ) -> None:
        super().__init__(console=console, fd=fd)
        self.badge = badge
        self.status_msg = initial_status
        # The last model call's context usage, like "logic ctx 3.2k/32.8k (10%)"; empty for none.
        self.usage = ""
        self._status_stop = threading.Event()
        self._status_thread: threading.Thread | None = None
        self.start_time = time.monotonic()

    def update(self, msg: str) -> None:
        self.status_msg = msg

    def update_usage(self, usage: str) -> None:
        self.usage = usage

    def format_line(self, elapsed: int | None = None) -> str:
        secs_total = int(time.monotonic() - self.start_time) if elapsed is None else elapsed
        mins, secs = divmod(secs_total, 60)
        timer = f"[{mins}:{secs:02d}]"
        waiting = (
            " (waiting for model...)"
            if secs_total >= 5 and "model" in self.status_msg.lower()
            else ""
        )
        usage = f" · {self.usage}" if self.usage else ""
        return f"{self.badge}{usage} — {self.status_msg} {timer}{waiting}"

    def __enter__(self) -> TurnStatus:
        self.start_time = time.monotonic()
        super().__enter__()
        self._status_stop.clear()
        self._status_thread = threading.Thread(target=self._live_display, daemon=True)
        self._status_thread.start()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._status_stop.set()
        if self._status_thread is not None:
            self._status_thread.join(timeout=0.2)
        super().__exit__(exc_type, exc_val, exc_tb)
        _clear_status_line()

    def _live_display(self) -> None:
        while not self._status_stop.is_set():
            if not self._paused.is_set():
                self._redraw()
            time.sleep(0.1)

    def _redraw(self) -> None:
        if not sys.stderr.isatty():
            return
        with self._output_lock:
            sys.stderr.write(f"{_CLEAR_LINE}{self.format_line()}")
            sys.stderr.flush()
