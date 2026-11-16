"""Tests for the interactive `gups-builder area` zone-selection TUI."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from gups_builder.area_tui import AreaTui, ZoneInfo, discover_zones, run_area_tui

if TYPE_CHECKING:
    from pathlib import Path


def _drive(world: Path, keys: str) -> str | None:
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        # Safety net so a broken key sequence cannot block the test suite forever.
        timer = threading.Timer(2.0, pipe.send_text, args=("\x03",))
        timer.daemon = True
        timer.start()
        try:
            return run_area_tui(world, input_=pipe, output=DummyOutput())
        finally:
            timer.cancel()


def _write_package(world: Path, rel: str, kind: str = "zone", title: str | None = None) -> None:
    pkg = world / rel
    pkg.mkdir(parents=True, exist_ok=True)
    title_line = f'    title = "{title}",\n' if title is not None else ""
    (pkg / "package.star").write_text(f'package(\n    kind = "{kind}",\n{title_line})\n')


def test_discover_zones_finds_zone_packages_and_skips_skills(tmp_path: Path) -> None:
    _write_package(tmp_path, "zones/mansion", kind="zone", title="The Mansion")
    _write_package(tmp_path, "zones/caves", kind="zone")
    _write_package(tmp_path, "skills/arcane", kind="skills", title="Arcane")
    _write_package(tmp_path, ".builder/ignored", kind="zone")

    assert discover_zones(tmp_path) == [
        ZoneInfo(path="zones/caves", title=None),
        ZoneInfo(path="zones/mansion", title="The Mansion"),
    ]


def test_tui_cancels_with_q_returns_none(tmp_path: Path) -> None:
    _write_package(tmp_path, "zones/caves")
    assert _drive(tmp_path, "q") is None


def test_tui_selects_existing_zone(tmp_path: Path) -> None:
    _write_package(tmp_path, "zones/caves")
    _write_package(tmp_path, "zones/mansion")
    # Down to zones/mansion, Enter.
    assert _drive(tmp_path, "\x1b[B\r") == "zones/mansion"


def test_tui_creates_new_zone_via_list_choice(tmp_path: Path) -> None:
    _write_package(tmp_path, "zones/caves")
    # Down to [Create new zone...], Enter, type "haunted" after pre-filled "zones/", Enter.
    assert _drive(tmp_path, "\x1b[B\rhaunted\r") == "zones/haunted"


def test_tui_creates_new_zone_via_n_shortcut(tmp_path: Path) -> None:
    _write_package(tmp_path, "zones/caves")
    assert _drive(tmp_path, "nhaunted\r") == "zones/haunted"


def test_tui_new_zone_rejects_empty_and_invalid_paths(tmp_path: Path) -> None:
    # No existing zones: row 0 is [Create new zone...].
    # Enter -> pre-filled "zones/" -> Enter (rejected) ->
    # backspace 6 ("zones/") -> Enter (rejected) ->
    # type "/abs" + Enter (rejected) ->
    # backspace 4 -> type "zones/valid" + Enter.
    bs6 = "\x7f" * 6
    bs4 = "\x7f" * 4
    keys = f"\r\r{bs6}\r/abs\r{bs4}zones/valid\r"
    assert _drive(tmp_path, keys) == "zones/valid"


def test_tui_new_zone_cancel_button_returns_to_list(tmp_path: Path) -> None:
    _write_package(tmp_path, "zones/caves")
    # 'n' -> type "foo" -> Tab to [Cancel / Back] -> Enter -> 'q' quits.
    assert _drive(tmp_path, "nfoo\t\rq") is None


def test_tui_scrolls_list_in_small_window(tmp_path: Path) -> None:
    for i in range(25):
        _write_package(tmp_path, f"zones/zone_{i:02d}")
    with create_pipe_input() as pipe:
        tui = AreaTui(tmp_path, input_=pipe, output=DummyOutput())
        tui.select_row(20)
        assert tui.active_vertical_scroll(80, 8) > 0
