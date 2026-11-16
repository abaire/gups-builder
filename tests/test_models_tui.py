"""Tests for the interactive `gups-builder models` TUI."""

from __future__ import annotations

import json
import threading
from typing import TYPE_CHECKING, Any

import httpx
from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.output import DummyOutput

from gups_builder.config import load_config, user_config_path, world_config_path
from gups_builder.llm.client import HttpLlm
from gups_builder.models_tui import ModelsTui, run_models_tui

if TYPE_CHECKING:
    from pathlib import Path


def _drive(
    tmp_path: Path,
    keys: str,
    handler: Any,
    *,
    world_toml: str | None = None,
) -> tuple[int, Path, Path]:
    world = tmp_path / "world"
    home = tmp_path / "home"
    if world_toml is not None:
        wpath = world_config_path(world)
        wpath.parent.mkdir(parents=True, exist_ok=True)
        wpath.write_text(world_toml)

    transport = httpx.MockTransport(handler)
    with create_pipe_input() as pipe:
        pipe.send_text(keys)
        # Safety net so a broken key sequence cannot block the test suite forever.
        timer = threading.Timer(2.0, pipe.send_text, args=("\x03",))
        timer.daemon = True
        timer.start()
        try:
            rc = run_models_tui(
                world,
                {},
                {},
                home,
                lambda cfg: HttpLlm(cfg, transport=transport),
                input_=pipe,
                output=DummyOutput(),
            )
        finally:
            timer.cancel()
    return rc, world, home


def test_tui_quits_without_changes(tmp_path: Path) -> None:
    rc, world, home = _drive(
        tmp_path,
        "q",
        lambda _r: httpx.Response(200, json={"data": []}),
    )
    assert rc == 0
    assert not user_config_path(home).exists()
    assert not world_config_path(world).exists()


def test_tui_selects_logic_model_from_server_list(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/models")
        return httpx.Response(
            200,
            json={
                "data": [
                    {"id": "qwen2.5:14b"},
                    {"id": "llama3.2:latest"},
                ]
            },
        )

    # First row is logic.model: Enter opens model list (sorted: llama3.2:latest, qwen2.5:14b).
    # Down moves to qwen2.5:14b, Enter saves, q quits.
    rc, world, home = _drive(tmp_path, "\r\x1b[B\rq", handler)
    assert rc == 0
    cfg = load_config(world, {}, {}, home)
    assert cfg.logic.model == "qwen2.5:14b"
    assert user_config_path(home).is_file()


def test_tui_model_query_failure_falls_back_to_manual_entry(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="offline")

    # Enter on logic.model -> fails -> opens text input -> type "custom-m" + Enter -> q quits.
    rc, world, home = _drive(tmp_path, "\rcustom-m\rq", handler)
    assert rc == 0
    cfg = load_config(world, {}, {}, home)
    assert cfg.logic.model == "custom-m"


def test_tui_selects_structured_mode(tmp_path: Path) -> None:
    # Rows:
    # 0: logic.model, 1: logic.base_url, 2: logic.api_key, 3: logic.structured
    # Down x3 -> Enter -> Down (json_object) -> Enter -> q
    keys = "\x1b[B\x1b[B\x1b[B\r\x1b[B\rq"
    rc, world, home = _drive(
        tmp_path,
        keys,
        lambda _r: httpx.Response(200, json={"data": []}),
    )
    assert rc == 0
    cfg = load_config(world, {}, {}, home)
    assert cfg.logic.structured == "json_object"


def test_tui_edits_numeric_setting_and_rejects_bad_input(tmp_path: Path) -> None:
    # Row 4 is logic.temperature (default 0.2):
    # Down x4 -> Enter -> Backspace x3 -> type "nope" + Enter (rejected, stays in edit) ->
    # Backspace x4 -> type "0.45" + Enter -> q
    down4 = "\x1b[B" * 4
    bs3 = "\x7f" * 3
    bs4 = "\x7f" * 4
    keys = f"{down4}\r{bs3}nope\r{bs4}0.45\rq"
    rc, world, home = _drive(
        tmp_path,
        keys,
        lambda _r: httpx.Response(200, json={"data": []}),
    )
    assert rc == 0
    cfg = load_config(world, {}, {}, home)
    assert cfg.logic.temperature == 0.45


def test_tui_toggles_save_target_to_world_config(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "world-model"}]})

    # 't' toggles from user to world, Enter on logic.model, Enter selects world-model, q quits.
    rc, world, home = _drive(tmp_path, "t\r\rq", handler)
    assert rc == 0
    assert world_config_path(world).is_file()
    assert not user_config_path(home).exists()
    cfg = load_config(world, {}, {}, home)
    assert cfg.logic.model == "world-model"


def test_tui_check_action_probes_models(tmp_path: Path) -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        body = json.loads(request.content)
        if "response_format" in body:
            return httpx.Response(
                200,
                json={"choices": [{"message": {"role": "assistant", "content": '{"word":"ok"}'}}]},
            )
        return httpx.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
        )

    rc, _, _ = _drive(
        tmp_path,
        "cq",
        handler,
        world_toml='[logic]\nmodel = "m1"\n[prose]\nmodel = "m2"\n',
    )
    assert rc == 0
    assert len(seen) == 4


def test_tui_list_has_explicit_cancel_option(tmp_path: Path) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": [{"id": "m1"}]})

    # logic.model list has: m1, [Enter custom model name...], [Cancel / Back]
    # Enter opens list -> Down x2 -> Enter selects [Cancel / Back] -> q quits.
    rc, world, home = _drive(tmp_path, "\r\x1b[B\x1b[B\rq", handler)
    assert rc == 0
    assert not user_config_path(home).exists()
    assert not world_config_path(world).exists()


def test_tui_edit_has_explicit_cancel_button(tmp_path: Path) -> None:
    # Row 4 is logic.temperature: Down x4 -> Enter -> type "9.9" -> Tab to [Cancel] -> Enter -> q
    down4 = "\x1b[B" * 4
    keys = f"{down4}\r9.9\t\rq"
    rc, world, home = _drive(
        tmp_path,
        keys,
        lambda _r: httpx.Response(200, json={"data": []}),
    )
    assert rc == 0
    assert not user_config_path(home).exists()
    assert not world_config_path(world).exists()
    assert load_config(world, {}, {}, home).logic.temperature == 0.2


def test_tui_scrolls_main_and_list_in_small_window(tmp_path: Path) -> None:
    world = tmp_path / "world"
    home = tmp_path / "home"
    transport = httpx.MockTransport(lambda _r: httpx.Response(200, json={"data": []}))
    with create_pipe_input() as pipe:
        tui = ModelsTui(
            world,
            {},
            {},
            home,
            lambda cfg: HttpLlm(cfg, transport=transport),
            input_=pipe,
            output=DummyOutput(),
        )
        tui.select_row(17)
        assert tui.active_vertical_scroll(80, 8) > 0

        tui.open_list("Models", [f"model-{i}" for i in range(25)], selected=20)
        assert tui.active_vertical_scroll(80, 8) > 0
