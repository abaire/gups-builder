"""Interactive TUI for selecting an existing zone or entering a new one."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import libcst as cst
from prompt_toolkit.application import Application
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout import ConditionalContainer, HSplit, Layout, ScrollablePane, Window
from prompt_toolkit.layout.controls import BufferControl, FormattedTextControl
from prompt_toolkit.layout.margins import ScrollbarMargin
from prompt_toolkit.layout.mouse_handlers import MouseHandlers
from prompt_toolkit.layout.screen import Screen, WritePosition
from prompt_toolkit.styles import Style
from prompt_toolkit.widgets import Frame

if TYPE_CHECKING:
    from prompt_toolkit.formatted_text import StyleAndTextTuples
    from prompt_toolkit.input import Input
    from prompt_toolkit.output import Output

ViewMode = Literal["list", "new"]

_NEW_ZONE_CHOICE = "[Create new zone...]"
_CANCEL_CHOICE = "[Cancel / Back]"
_DEFAULT_NEW_PREFIX = "zones/"

_STYLE = Style.from_dict(
    {
        "header": "bold cyan",
        "section": "bold green",
        "selected": "reverse bold",
        "status": "green",
        "error": "bold red",
        "hint": "dim",
    }
)


@dataclass(frozen=True)
class ZoneInfo:
    """An existing zone package discovered in the world."""

    path: str
    title: str | None = None


def _read_package_metadata(path: Path) -> tuple[str | None, str | None]:
    try:
        module = cst.parse_module(path.read_text(encoding="utf-8"))
    except (OSError, cst.ParserSyntaxError):
        return None, None
    for stmt in module.body:
        if not isinstance(stmt, cst.SimpleStatementLine):
            continue
        for item in stmt.body:
            if not isinstance(item, cst.Expr) or not isinstance(item.value, cst.Call):
                continue
            call = item.value
            if not isinstance(call.func, cst.Name) or call.func.value != "package":
                continue
            return _extract_package_args(call)
    return None, None


def _extract_package_args(call: cst.Call) -> tuple[str | None, str | None]:
    kind: str | None = None
    title: str | None = None
    for arg in call.args:
        if not isinstance(arg.keyword, cst.Name) or not isinstance(arg.value, cst.SimpleString):
            continue
        val = arg.value.evaluated_value
        if not isinstance(val, str):
            continue
        if arg.keyword.value == "kind":
            kind = val
        elif arg.keyword.value == "title":
            title = val
    return kind, title


def discover_zones(world: Path) -> list[ZoneInfo]:
    """Return all zone packages under ``world``, sorted by world-relative path."""
    if not world.is_dir():
        return []
    zones: list[ZoneInfo] = []
    for pkg_file in sorted(world.rglob("package.star")):
        rel_dir = pkg_file.parent.relative_to(world)
        if any(part.startswith(".") for part in rel_dir.parts):
            continue
        rel = rel_dir.as_posix()
        kind, title = _read_package_metadata(pkg_file)
        if kind == "zone" or (kind is None and rel.startswith("zones/")):
            zones.append(ZoneInfo(path=rel, title=title))
    zones.sort(key=lambda z: z.path)
    return zones


def _validate_zone_path(raw: str) -> str:
    text = raw.strip()
    if not text or text.endswith("/"):
        msg = "Enter a zone path (e.g. zones/mansion)"
        raise ValueError(msg)
    if text.startswith("/") or ".." in Path(text).parts:
        msg = "Zone path must be relative to the world root"
        raise ValueError(msg)
    return text


class AreaTui:
    """Prompt-toolkit application for selecting or naming a zone package."""

    def __init__(
        self,
        world: Path,
        *,
        input_: Input | None = None,
        output: Output | None = None,
    ) -> None:
        self._world = world
        self._zones = discover_zones(world)
        self._choices = [
            *(z.path for z in self._zones),
            _NEW_ZONE_CHOICE,
            _CANCEL_CHOICE,
        ]
        self._titles = {z.path: z.title for z in self._zones if z.title}

        self.view: ViewMode = "list"
        self._selected = 0
        self._status = ""
        self._status_is_error = False

        self._edit_buffer = Buffer(multiline=False)
        self._list_window = Window(
            content=FormattedTextControl(
                self._render_list,
                get_cursor_position=self._list_cursor_position,
                focusable=True,
            ),
            wrap_lines=False,
            right_margins=[ScrollbarMargin(display_arrows=True)],
        )
        self._edit_window = Window(
            content=BufferControl(buffer=self._edit_buffer),
            height=1,
        )
        self._cancel_button = Window(
            content=FormattedTextControl(self._render_cancel_button, focusable=True),
            height=1,
        )
        self._app: Application[str | None] = Application(
            layout=self._build_layout(),
            key_bindings=self._build_key_bindings(),
            style=_STYLE,
            full_screen=True,
            input=input_,
            output=output,
        )

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status = text
        self._status_is_error = error

    def _build_layout(self) -> Layout:
        header = Window(
            content=FormattedTextControl(self._render_header),
            height=2,
        )
        list_pane = ConditionalContainer(
            content=Frame(self._list_window, title="Select Zone"),
            filter=Condition(lambda: self.view == "list"),
        )
        new_pane = ConditionalContainer(
            content=Frame(
                ScrollablePane(
                    HSplit(
                        [
                            Window(
                                content=FormattedTextControl(self._render_new_prompt),
                                height=1,
                            ),
                            self._edit_window,
                            self._cancel_button,
                        ]
                    )
                ),
                title="New Zone",
            ),
            filter=Condition(lambda: self.view == "new"),
        )
        status_bar = Window(content=FormattedTextControl(self._render_status), height=1)
        footer = Window(content=FormattedTextControl(self._render_footer), height=1)
        root = HSplit([header, list_pane, new_pane, status_bar, footer])
        return Layout(root, focused_element=self._list_window)

    def _render_header(self) -> StyleAndTextTuples:
        return [
            ("class:header", "gups-builder area\n"),
            ("class:hint", f"World: {self._world}"),
        ]

    def _list_cursor_position(self) -> Point:
        return Point(x=0, y=self._selected)

    def _render_list(self) -> StyleAndTextTuples:
        lines: StyleAndTextTuples = []
        for idx, choice in enumerate(self._choices):
            prefix = "> " if idx == self._selected else "  "
            style = "class:selected" if idx == self._selected else ""
            title = self._titles.get(choice)
            lines.append((style, f"{prefix}{choice}"))
            if title:
                lines.append((style or "class:hint", f"  ({title})"))
            lines.append((style, "\n"))
        return lines

    def _render_new_prompt(self) -> StyleAndTextTuples:
        return [
            ("class:section", "New zone path "),
            ("class:hint", "(e.g. zones/mansion)"),
            ("class:section", ":"),
        ]

    def _render_cancel_button(self) -> StyleAndTextTuples:
        focused = self._app.layout.has_focus(self._cancel_button)
        style = "class:selected" if focused else "class:hint"
        return [(style, "[ Cancel / Back ]")]

    def _render_status(self) -> StyleAndTextTuples:
        if not self._status:
            return [("", "")]
        style = "class:error" if self._status_is_error else "class:status"
        return [(style, self._status)]

    def _render_footer(self) -> StyleAndTextTuples:
        if self.view == "list":
            text = "↑/↓ Move  Enter Select  n New zone  q/Esc Cancel"
        else:
            text = "Enter Confirm  Tab/↓ Cancel button  Esc/Ctrl-G Back"
        return [("class:hint", text)]

    def _enter_list(self) -> None:
        self.view = "list"
        self._app.layout.focus(self._list_window)

    def _enter_new(self) -> None:
        self._edit_buffer.text = _DEFAULT_NEW_PREFIX
        self._edit_buffer.cursor_position = len(_DEFAULT_NEW_PREFIX)
        self.view = "new"
        self._app.layout.focus(self._edit_window)

    def _confirm_list(self, event: Any) -> None:
        choice = self._choices[self._selected]
        if choice == _CANCEL_CHOICE:
            event.app.exit(result=None)
            return
        if choice == _NEW_ZONE_CHOICE:
            self._enter_new()
            return
        event.app.exit(result=choice)

    def _confirm_new(self, event: Any) -> None:
        try:
            path = _validate_zone_path(self._edit_buffer.text)
        except ValueError as error:
            self._set_status(str(error), error=True)
            return
        event.app.exit(result=path)

    def _build_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()
        in_list = Condition(lambda: self.view == "list")
        in_new = Condition(lambda: self.view == "new")

        @kb.add("c-c")
        def _quit_ctrl_c(event: Any) -> None:
            event.app.exit(result=None)

        @kb.add("q", filter=in_list)
        @kb.add("escape", filter=in_list)
        def _quit_list(event: Any) -> None:
            event.app.exit(result=None)

        @kb.add("up", filter=in_list)
        @kb.add("k", filter=in_list)
        def _list_up(_event: Any) -> None:
            self._selected = (self._selected - 1) % len(self._choices)

        @kb.add("down", filter=in_list)
        @kb.add("j", filter=in_list)
        def _list_down(_event: Any) -> None:
            self._selected = (self._selected + 1) % len(self._choices)

        @kb.add("enter", filter=in_list)
        def _list_enter(event: Any) -> None:
            self._confirm_list(event)

        @kb.add("n", filter=in_list)
        def _list_new(_event: Any) -> None:
            self._enter_new()

        @kb.add("enter", filter=in_new)
        def _new_enter(event: Any) -> None:
            if self._app.layout.has_focus(self._cancel_button):
                self._set_status("Cancelled")
                self._enter_list()
                return
            self._confirm_new(event)

        @kb.add("tab", filter=in_new)
        @kb.add("s-tab", filter=in_new)
        def _new_toggle_focus(_event: Any) -> None:
            target = (
                self._edit_window
                if self._app.layout.has_focus(self._cancel_button)
                else self._cancel_button
            )
            self._app.layout.focus(target)

        @kb.add("down", filter=in_new)
        def _new_down(_event: Any) -> None:
            self._app.layout.focus(self._cancel_button)

        @kb.add("up", filter=in_new)
        def _new_up(_event: Any) -> None:
            self._app.layout.focus(self._edit_window)

        @kb.add("escape", filter=in_new)
        @kb.add("c-g", filter=in_new)
        def _new_cancel(_event: Any) -> None:
            self._set_status("Cancelled")
            self._enter_list()

        return kb

    def select_row(self, index: int) -> None:
        """Select a row by index, clamped to the valid range."""
        self._selected = max(0, min(index, len(self._choices) - 1))

    def active_vertical_scroll(self, width: int, height: int) -> int:
        """Render the list window at (width, height) and return vertical_scroll."""
        self._list_window.write_to_screen(
            Screen(),
            mouse_handlers=MouseHandlers(),
            write_position=WritePosition(xpos=0, ypos=0, width=width, height=height),
            parent_style="",
            erase_bg=False,
            z_index=None,
        )
        return self._list_window.vertical_scroll

    def run(self) -> str | None:
        """Run the TUI until the user selects a zone or cancels."""
        return self._app.run()


def run_area_tui(
    world: Path,
    *,
    input_: Input | None = None,
    output: Output | None = None,
) -> str | None:
    """Launch the interactive `gups-builder area` zone-selection TUI."""
    return AreaTui(world, input_=input_, output=output).run()
