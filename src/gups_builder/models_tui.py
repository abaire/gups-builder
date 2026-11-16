"""Interactive TUI for inspecting and modifying `gups-builder` model settings."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

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
from pydantic import BaseModel, ValidationError

from gups_builder.config import (
    Config,
    load_config,
    save_config_value,
    user_config_path,
    world_config_path,
)
from gups_builder.llm.client import HttpLlm, LlmError, Message, Role

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping
    from pathlib import Path

    from prompt_toolkit.formatted_text import StyleAndTextTuples
    from prompt_toolkit.input import Input
    from prompt_toolkit.output import Output

SettingKind = Literal["model", "structured", "text", "secret", "float", "int"]
ViewMode = Literal["main", "list", "edit"]

_STRUCTURED_CHOICES = ("json_schema", "json_object", "none")
_CUSTOM_MODEL_CHOICE = "[Enter custom model name...]"
_CANCEL_CHOICE = "[Cancel / Back]"

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
class SettingSpec:
    """One editable row in the models TUI."""

    role: Role | None
    field: str
    kind: SettingKind
    label: str
    hint: str = ""


def _build_specs() -> tuple[SettingSpec, ...]:
    specs: list[SettingSpec] = []
    for role in ("logic", "prose"):
        role_literal: Role = role
        specs.extend(
            [
                SettingSpec(role_literal, "model", "model", f"{role}.model"),
                SettingSpec(role_literal, "base_url", "text", f"{role}.base_url"),
                SettingSpec(role_literal, "api_key", "secret", f"{role}.api_key"),
                SettingSpec(role_literal, "structured", "structured", f"{role}.structured"),
                SettingSpec(role_literal, "temperature", "float", f"{role}.temperature"),
                SettingSpec(role_literal, "max_tokens", "int", f"{role}.max_tokens"),
                SettingSpec(role_literal, "context_tokens", "int", f"{role}.context_tokens"),
                SettingSpec(role_literal, "timeout_s", "float", f"{role}.timeout_s"),
            ]
        )
    specs.extend(
        [
            SettingSpec(None, "gups", "text", "gups", "path to gups binary"),
            SettingSpec(None, "max_repairs", "int", "max_repairs"),
        ]
    )
    return tuple(specs)


_SETTINGS = _build_specs()


class _WordReply(BaseModel):
    word: str


def _raw_value(config: Config, spec: SettingSpec) -> Any:
    if spec.role is None:
        return getattr(config, spec.field)
    rc = config.logic if spec.role == "logic" else config.prose
    return getattr(rc, spec.field)


def _display_value(config: Config, spec: SettingSpec) -> str:
    value = _raw_value(config, spec)
    if spec.kind == "secret":
        return "set" if value else "(not set)"
    if value is None or value == "":
        return "(not set)"
    return str(value)


def _parse_input(spec: SettingSpec, raw: str) -> Any:
    text = raw.strip()
    if spec.kind in ("secret", "text") and spec.field in ("api_key", "gups") and not text:
        return None
    if spec.kind in ("text", "model") and not text:
        msg = f"{spec.label} cannot be empty"
        raise ValueError(msg)
    if spec.kind == "float":
        try:
            return float(text)
        except ValueError:
            msg = f"Invalid number for {spec.label}: {text!r}"
            raise ValueError(msg) from None
    if spec.kind == "int":
        try:
            return int(text)
        except ValueError:
            msg = f"Invalid integer for {spec.label}: {text!r}"
            raise ValueError(msg) from None
    return text


def _probe_role(llm: HttpLlm, config: Config, role: Role) -> str:
    rc = config.logic if role == "logic" else config.prose
    started = time.monotonic()
    try:
        llm.complete(role, [Message("system", "Reply with the single word OK.")], None)
    except LlmError as error:
        return f"{role}: FAILED ({error})"
    elapsed = time.monotonic() - started
    if rc.structured != "json_schema":
        return f"{role}: OK ({elapsed:.1f} s)"
    try:
        reply = llm.complete(
            role,
            [Message("user", "Reply with the word OK.")],
            _WordReply.model_json_schema(),
        )
        if llm.last_fallback:
            return f"{role}: OK ({elapsed:.1f} s), json_schema rejected"
        _WordReply.model_validate_json(reply)
    except (LlmError, ValidationError):
        return f"{role}: OK ({elapsed:.1f} s), json_schema not honoured"
    return f"{role}: OK ({elapsed:.1f} s), json_schema OK"


class ModelsTui:
    """Prompt-toolkit application for viewing and editing model configuration."""

    def __init__(
        self,
        world: Path,
        cli: dict[str, str],
        env: Mapping[str, str],
        home: Path,
        client_factory: Callable[[Config], HttpLlm],
        *,
        input_: Input | None = None,
        output: Output | None = None,
    ) -> None:
        self._world = world
        self._cli = cli
        self._env = env
        self._home = home
        self._client_factory = client_factory
        self._config = load_config(world, cli, env, home)
        self._save_to_world = world_config_path(world).exists()

        self.view: ViewMode = "main"
        self._selected = 0
        self._list_title = ""
        self._list_choices: list[str] = []
        self._list_selected = 0
        self._active_spec: SettingSpec | None = None
        self._status = ""
        self._status_is_error = False

        self._edit_buffer = Buffer(multiline=False)
        self._main_window = Window(
            content=FormattedTextControl(
                self._render_main,
                get_cursor_position=self._main_cursor_position,
                focusable=True,
            ),
            wrap_lines=False,
            right_margins=[ScrollbarMargin(display_arrows=True)],
        )
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
        self._app: Application[int] = Application(
            layout=self._build_layout(),
            key_bindings=self._build_key_bindings(),
            style=_STYLE,
            full_screen=True,
            input=input_,
            output=output,
        )

    def _target_path(self) -> Path:
        if self._save_to_world:
            return world_config_path(self._world)
        return user_config_path(self._home)

    def _target_label(self) -> str:
        kind = "world" if self._save_to_world else "user"
        return f"{kind} ({self._target_path()})"

    def _set_status(self, text: str, *, error: bool = False) -> None:
        self._status = text
        self._status_is_error = error

    def _build_layout(self) -> Layout:
        header = Window(
            content=FormattedTextControl(self._render_header),
            height=2,
        )
        main_pane = ConditionalContainer(
            content=Frame(self._main_window, title="Settings"),
            filter=Condition(lambda: self.view == "main"),
        )
        list_pane = ConditionalContainer(
            content=Frame(self._list_window, title=lambda: self._list_title),
            filter=Condition(lambda: self.view == "list"),
        )
        edit_pane = ConditionalContainer(
            content=Frame(
                ScrollablePane(
                    HSplit(
                        [
                            Window(
                                content=FormattedTextControl(self._render_edit_prompt),
                                height=1,
                            ),
                            self._edit_window,
                            self._cancel_button,
                        ]
                    )
                ),
                title="Edit Setting",
            ),
            filter=Condition(lambda: self.view == "edit"),
        )
        status_bar = Window(content=FormattedTextControl(self._render_status), height=1)
        footer = Window(content=FormattedTextControl(self._render_footer), height=1)
        root = HSplit([header, main_pane, list_pane, edit_pane, status_bar, footer])
        return Layout(root, focused_element=self._main_window)

    def _render_header(self) -> StyleAndTextTuples:
        return [
            ("class:header", "gups-builder models\n"),
            ("class:hint", f"Save target: {self._target_label()}"),
        ]

    def _main_cursor_position(self) -> Point:
        line = 0
        last_group: str | None = None
        for idx, spec in enumerate(_SETTINGS):
            group = spec.role if spec.role is not None else "general"
            if group != last_group:
                if last_group is not None:
                    line += 1
                line += 1
                last_group = group
            if idx == self._selected:
                return Point(x=0, y=line)
            line += 1
        return Point(x=0, y=0)

    def _list_cursor_position(self) -> Point:
        return Point(x=0, y=self._list_selected)

    def _render_main(self) -> StyleAndTextTuples:
        lines: StyleAndTextTuples = []
        last_group: str | None = None
        for idx, spec in enumerate(_SETTINGS):
            group = spec.role if spec.role is not None else "general"
            if group != last_group:
                if last_group is not None:
                    lines.append(("", "\n"))
                lines.append(("class:section", f"[{group}]\n"))
                last_group = group
            prefix = "> " if idx == self._selected else "  "
            style = "class:selected" if idx == self._selected else ""
            hint = f"  ({spec.hint})" if spec.hint else ""
            field_text = f"{prefix}{spec.field:<16} {_display_value(self._config, spec)}"
            lines.append((style, field_text))
            if hint:
                lines.append((style or "class:hint", hint))
            lines.append((style, "\n"))
        return lines

    def _render_list(self) -> StyleAndTextTuples:
        lines: StyleAndTextTuples = []
        for idx, choice in enumerate(self._list_choices):
            prefix = "> " if idx == self._list_selected else "  "
            style = "class:selected" if idx == self._list_selected else ""
            lines.append((style, f"{prefix}{choice}\n"))
        return lines

    def _render_edit_prompt(self) -> StyleAndTextTuples:
        if self._active_spec is None:
            return [("class:section", ":")]
        if self._active_spec.hint:
            return [
                ("class:section", f"{self._active_spec.label} "),
                ("class:hint", f"({self._active_spec.hint})"),
                ("class:section", ":"),
            ]
        return [("class:section", f"{self._active_spec.label}:")]

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
        if self.view == "main":
            text = "↑/↓ Move  Enter Edit  c Check models  t Toggle user/world  q Quit"
        elif self.view == "list":
            text = "↑/↓ Move  Enter Select  q/Esc Cancel"
        else:
            text = "Enter Save  Tab/↓ Cancel button  Esc/Ctrl-G Cancel"
        return [("class:hint", text)]

    def _enter_main(self) -> None:
        self.view = "main"
        self._active_spec = None
        self._app.layout.focus(self._main_window)

    def _enter_list(self, spec: SettingSpec, title: str, choices: list[str]) -> None:
        self._active_spec = spec
        self._list_title = title
        self._list_choices = [*choices, _CANCEL_CHOICE]
        current = str(_raw_value(self._config, spec) or "")
        self._list_selected = (
            self._list_choices.index(current) if current in self._list_choices else 0
        )
        self.view = "list"
        self._app.layout.focus(self._list_window)

    def _enter_edit(self, spec: SettingSpec) -> None:
        self._active_spec = spec
        raw = _raw_value(self._config, spec)
        initial = "" if spec.kind == "secret" or raw is None else str(raw)
        self._edit_buffer.text = initial
        self._edit_buffer.cursor_position = len(initial)
        self.view = "edit"
        self._app.layout.focus(self._edit_window)

    def _save_and_reload(self, spec: SettingSpec, value: Any) -> None:
        save_config_value(self._target_path(), spec.role, spec.field, value)
        self._config = load_config(self._world, self._cli, self._env, self._home)
        shown = "(not set)" if value is None else ("set" if spec.kind == "secret" else str(value))
        self._set_status(f"Saved {spec.label} = {shown} to {self._target_label()}")

    def _activate_selected(self) -> None:
        spec = _SETTINGS[self._selected]
        if spec.kind == "model" and spec.role is not None:
            self._activate_model_picker(spec, spec.role)
            return
        if spec.kind == "structured":
            self._enter_list(spec, f"Select {spec.label}", list(_STRUCTURED_CHOICES))
            return
        self._enter_edit(spec)

    def _activate_model_picker(self, spec: SettingSpec, role: Role) -> None:
        llm = self._client_factory(self._config)
        try:
            models = llm.list_models(role)
        except LlmError as error:
            self._set_status(f"Model query failed ({error}); enter name manually", error=True)
            self._enter_edit(spec)
            return
        if not models:
            self._set_status("Server returned no models; enter name manually", error=True)
            self._enter_edit(spec)
            return
        self._set_status(f"Found {len(models)} models for {role}")
        self._enter_list(spec, f"Select {spec.label}", [*models, _CUSTOM_MODEL_CHOICE])

    def _confirm_list(self) -> None:
        spec = self._active_spec
        if spec is None or not self._list_choices:
            self._enter_main()
            return
        choice = self._list_choices[self._list_selected]
        if choice == _CANCEL_CHOICE:
            self._set_status("Cancelled")
            self._enter_main()
            return
        if spec.kind == "model" and choice == _CUSTOM_MODEL_CHOICE:
            self._enter_edit(spec)
            return
        self._save_and_reload(spec, choice)
        self._enter_main()

    def _confirm_edit(self) -> None:
        spec = self._active_spec
        if spec is None:
            self._enter_main()
            return
        try:
            parsed = _parse_input(spec, self._edit_buffer.text)
        except ValueError as error:
            self._set_status(str(error), error=True)
            return
        self._save_and_reload(spec, parsed)
        self._enter_main()

    def _run_probe(self) -> None:
        llm = self._client_factory(self._config)
        results = [_probe_role(llm, self._config, role) for role in ("logic", "prose")]
        has_failure = any("FAILED" in r for r in results)
        self._set_status(" | ".join(results), error=has_failure)

    def _build_key_bindings(self) -> KeyBindings:
        kb = KeyBindings()
        in_main = Condition(lambda: self.view == "main")
        in_list = Condition(lambda: self.view == "list")
        in_edit = Condition(lambda: self.view == "edit")

        @kb.add("c-c")
        def _quit_ctrl_c(event: Any) -> None:
            event.app.exit(result=0)

        @kb.add("q", filter=in_main)
        @kb.add("escape", filter=in_main)
        def _quit_main(event: Any) -> None:
            event.app.exit(result=0)

        @kb.add("up", filter=in_main)
        @kb.add("k", filter=in_main)
        def _main_up(_event: Any) -> None:
            self._selected = (self._selected - 1) % len(_SETTINGS)

        @kb.add("down", filter=in_main)
        @kb.add("j", filter=in_main)
        def _main_down(_event: Any) -> None:
            self._selected = (self._selected + 1) % len(_SETTINGS)

        @kb.add("enter", filter=in_main)
        def _main_enter(_event: Any) -> None:
            self._activate_selected()

        @kb.add("t", filter=in_main)
        def _toggle_target(_event: Any) -> None:
            self._save_to_world = not self._save_to_world
            self._set_status(f"Save target: {self._target_label()}")

        @kb.add("c", filter=in_main)
        def _check_models(_event: Any) -> None:
            self._run_probe()

        @kb.add("up", filter=in_list)
        @kb.add("k", filter=in_list)
        def _list_up(_event: Any) -> None:
            if self._list_choices:
                self._list_selected = (self._list_selected - 1) % len(self._list_choices)

        @kb.add("down", filter=in_list)
        @kb.add("j", filter=in_list)
        def _list_down(_event: Any) -> None:
            if self._list_choices:
                self._list_selected = (self._list_selected + 1) % len(self._list_choices)

        @kb.add("enter", filter=in_list)
        def _list_enter(_event: Any) -> None:
            self._confirm_list()

        @kb.add("escape", filter=in_list)
        @kb.add("q", filter=in_list)
        def _list_cancel(_event: Any) -> None:
            self._set_status("Cancelled")
            self._enter_main()

        @kb.add("enter", filter=in_edit)
        def _edit_enter(_event: Any) -> None:
            if self._app.layout.has_focus(self._cancel_button):
                self._set_status("Cancelled")
                self._enter_main()
                return
            self._confirm_edit()

        @kb.add("tab", filter=in_edit)
        @kb.add("s-tab", filter=in_edit)
        def _edit_toggle_focus(_event: Any) -> None:
            target = (
                self._edit_window
                if self._app.layout.has_focus(self._cancel_button)
                else self._cancel_button
            )
            self._app.layout.focus(target)

        @kb.add("down", filter=in_edit)
        def _edit_down(_event: Any) -> None:
            self._app.layout.focus(self._cancel_button)

        @kb.add("up", filter=in_edit)
        def _edit_up(_event: Any) -> None:
            self._app.layout.focus(self._edit_window)

        @kb.add("escape", filter=in_edit)
        @kb.add("c-g", filter=in_edit)
        def _edit_cancel(_event: Any) -> None:
            self._set_status("Cancelled")
            self._enter_main()

        return kb

    def select_row(self, index: int) -> None:
        """Select a setting row by index, clamped to the valid range."""
        self._selected = max(0, min(index, len(_SETTINGS) - 1))

    def open_list(self, title: str, choices: list[str], selected: int = 0) -> None:
        """Open the list subwindow with the given choices and selected index."""
        self._list_title = title
        self._list_choices = list(choices)
        self._list_selected = max(0, min(selected, len(self._list_choices) - 1)) if choices else 0
        self.view = "list"

    def active_vertical_scroll(self, width: int, height: int) -> int:
        """Render the active scrollable window at (width, height) and return vertical_scroll."""
        window = self._list_window if self.view == "list" else self._main_window
        window.write_to_screen(
            Screen(),
            mouse_handlers=MouseHandlers(),
            write_position=WritePosition(xpos=0, ypos=0, width=width, height=height),
            parent_style="",
            erase_bg=False,
            z_index=None,
        )
        return window.vertical_scroll

    def run(self) -> int:
        """Run the TUI until the user exits."""
        result = self._app.run()
        return int(result) if result is not None else 0


def run_models_tui(
    world: Path,
    cli: dict[str, str],
    env: Mapping[str, str],
    home: Path,
    client_factory: Callable[[Config], HttpLlm],
    *,
    input_: Input | None = None,
    output: Output | None = None,
) -> int:
    """Launch the interactive `gups-builder models` configuration TUI."""
    return ModelsTui(
        world,
        cli,
        env,
        home,
        client_factory,
        input_=input_,
        output=output,
    ).run()
