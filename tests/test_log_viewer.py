"""The call-log viewer: reading the JSONL log, rendering it, and paging through it."""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from gups_builder.log_viewer import LogViewer, read_log, render_log

if TYPE_CHECKING:
    from pathlib import Path


def _entry(reply: str, error: str | None = None, **changes: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {
        "ts": "2026-11-17T17:10:00.591625+00:00",
        "role": "logic",
        "model": "m1",
        "messages": [
            {"role": "system", "content": "You are the builder."},
            {"role": "user", "content": "Plan the area."},
        ],
        "reply": reply,
        "seconds": 85.24,
        "error": error,
    }
    entry.update(changes)
    return entry


def _write_log(path: Path, entries: list[dict[str, Any]], extra: str = "") -> Path:
    path.write_text("".join(json.dumps(e) + "\n" for e in entries) + extra)
    return path


def test_read_log_keeps_every_call_and_reports_an_unreadable_line(tmp_path: Path) -> None:
    log = _write_log(tmp_path / "s.jsonl", [_entry("one"), _entry("two")], extra="{oops\n")
    entries = read_log(log)
    assert [e.get("reply") for e in entries[:2]] == ["one", "two"]
    assert entries[2]["error"].startswith("line 3 is not JSON")


def test_read_log_of_a_missing_file_is_empty(tmp_path: Path) -> None:
    assert read_log(tmp_path / "none.jsonl") == []


def test_render_log_shows_each_message_and_the_reply(tmp_path: Path) -> None:
    entries = [_entry("{}"), _entry("", error="logic request failed: HTTP 500")]
    text, starts = render_log(entries)
    lines = text.splitlines()
    assert lines[starts[0]] == (
        "=== call 1/2 | 2026-11-17T17:10:00.591625+00:00 | logic (m1) | 85.2 s ==="
    )
    assert lines[starts[1]].startswith("=== call 2/2 |")
    assert "--- system ---\nYou are the builder." in text
    assert "--- user ---\nPlan the area." in text
    assert "--- reply ---\n{}" in text
    assert "error: logic request failed: HTTP 500" in text


def _drive(viewer: LogViewer, keys: str, pipe: Any) -> None:
    pipe.send_text(keys + "q")
    # Safety net so a broken key sequence cannot block the test suite forever.
    timer = threading.Timer(2.0, pipe.send_text, args=("\x03",))
    timer.daemon = True
    timer.start()
    try:
        viewer.run()
    finally:
        timer.cancel()


def test_the_viewer_opens_on_the_newest_call_and_steps_back(tmp_path: Path) -> None:
    log = _write_log(tmp_path / "s.jsonl", [_entry("one"), _entry("two"), _entry("three")])
    with create_pipe_input() as pipe:
        viewer = LogViewer(log, input_=pipe, output=DummyOutput())
        assert viewer.current_call() == 2
        _drive(viewer, "[[", pipe)
        assert viewer.current_call() == 0
        _drive(viewer, "]G", pipe)
        assert viewer.current_call() == 2


def test_the_viewer_quits_on_q(tmp_path: Path) -> None:
    log = _write_log(tmp_path / "s.jsonl", [_entry("one")])
    with create_pipe_input() as pipe:
        viewer = LogViewer(log, input_=pipe, output=DummyOutput())
        _drive(viewer, "", pipe)
        assert viewer.current_call() == 0
