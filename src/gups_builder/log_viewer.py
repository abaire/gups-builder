"""A full-screen pager over a session's call log, for working out what a model was told.

Every call is shown whole, each message and the reply in full, with the newest call
first in view: that is the one a failure usually needs. ``]`` and ``[`` step between
calls, leaving ``n`` and ``N`` to the read-only buffer's search, where they step
between matches; ``q`` leaves.
"""

from __future__ import annotations

import bisect
import json
from typing import TYPE_CHECKING, Any

from prompt_toolkit.application import Application
from prompt_toolkit.document import Document
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import HSplit, Layout, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import SearchToolbar, TextArea

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from prompt_toolkit.formatted_text import StyleAndTextTuples
    from prompt_toolkit.input import Input
    from prompt_toolkit.key_binding import KeyPressEvent
    from prompt_toolkit.output import Output

LogEntry = dict[str, Any]

_STYLE = Style.from_dict({"header": "bold cyan", "hint": "dim"})


def read_log(path: Path) -> list[LogEntry]:
    """Every call in the log, in order; a line that isn't JSON becomes an entry saying so."""
    if not path.is_file():
        return []
    entries: list[LogEntry] = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            entries.append(_parse_line(number, line))
    return entries


def _parse_line(number: int, line: str) -> LogEntry:
    try:
        entry = json.loads(line)
    except json.JSONDecodeError as error:
        return {"error": f"line {number} is not JSON ({error.msg})", "reply": line}
    if isinstance(entry, dict):
        return entry
    return {"error": f"line {number} is not a JSON object", "reply": line}


def _header(entry: LogEntry, index: int, total: int) -> str:
    model = entry.get("model")
    who = f"{entry.get('role', '?')} ({model})" if model else str(entry.get("role", "?"))
    seconds = entry.get("seconds")
    took = f"{seconds:.1f} s" if isinstance(seconds, (int, float)) else "? s"
    return f"=== call {index}/{total} | {entry.get('ts', '?')} | {who} | {took} ==="


def _render_entry(entry: LogEntry, index: int, total: int) -> list[str]:
    lines = [_header(entry, index, total)]
    if entry.get("error"):
        lines.append(f"error: {entry['error']}")
    for message in entry.get("messages") or []:
        if isinstance(message, dict):
            lines.append(f"--- {message.get('role', '?')} ---")
            lines.extend(str(message.get("content", "")).splitlines())
    lines.append("--- reply ---")
    lines.extend(str(entry.get("reply", "")).splitlines() or ["(empty)"])
    lines.append("")
    return lines


def render_log(entries: list[LogEntry]) -> tuple[str, list[int]]:
    """The log as plain text, and the line each call's header is on."""
    lines: list[str] = []
    starts: list[int] = []
    for index, entry in enumerate(entries, 1):
        starts.append(len(lines))
        lines.extend(_render_entry(entry, index, len(entries)))
    return "\n".join(lines), starts


class LogViewer:
    """A read-only, searchable pager over one call log."""

    def __init__(
        self, path: Path, *, input_: Input | None = None, output: Output | None = None
    ) -> None:
        self._path = path
        text, self._starts = render_log(read_log(path))
        search = SearchToolbar()
        self._area = TextArea(
            text=text, read_only=True, scrollbar=True, search_field=search, focus_on_click=True
        )
        self._jump(len(self._starts) - 1)
        root = HSplit(
            [
                self._area,
                search,
                Window(FormattedTextControl(self._footer), height=1),
            ]
        )
        self._app: Application[None] = Application(
            layout=Layout(root, focused_element=self._area),
            key_bindings=self._bindings(),
            style=_STYLE,
            full_screen=True,
            mouse_support=True,
            input=input_,
            output=output,
        )

    def current_call(self) -> int:
        """The index of the call the cursor is in (0 for an empty log)."""
        row = self._area.document.cursor_position_row
        return max(bisect.bisect_right(self._starts, row) - 1, 0)

    def _jump(self, call: int) -> None:
        if not self._starts:
            return
        call = max(0, min(call, len(self._starts) - 1))
        doc = self._area.document
        position = doc.translate_row_col_to_index(self._starts[call], 0)
        self._area.buffer.set_document(Document(doc.text, position), bypass_readonly=True)

    def _footer(self) -> StyleAndTextTuples:
        where = f"call {self.current_call() + 1}/{len(self._starts)}" if self._starts else "empty"
        keys = "]/[ next/prev call  g/G first/last  / search  n/N next/prev match  q quit"
        return [("class:header", f" {self._path.name} | {where} "), ("class:hint", keys)]

    def _bindings(self) -> KeyBindings:
        kb = KeyBindings()
        not_searching = Condition(lambda: self._app.layout.has_focus(self._area))
        moves = {
            "]": lambda: self.current_call() + 1,
            "[": lambda: self.current_call() - 1,
            "g": lambda: 0,
            "G": lambda: len(self._starts) - 1,
        }
        for key, target in moves.items():
            kb.add(key, filter=not_searching)(self._mover(target))

        @kb.add("q", filter=not_searching)
        @kb.add("escape", filter=not_searching)
        @kb.add("c-c")
        def _quit(event: KeyPressEvent) -> None:
            event.app.exit()

        return kb

    def _mover(self, target: Callable[[], int]) -> Callable[[KeyPressEvent], None]:
        def move(_event: KeyPressEvent) -> None:
            self._jump(target())

        return move

    def run(self) -> None:
        self._app.run()


def run_log_viewer(path: Path) -> None:
    """Page through the call log at ``path`` until the user quits."""
    LogViewer(path).run()
