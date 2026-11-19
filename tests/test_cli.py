import json
from pathlib import Path

import pytest

from gups_builder import cli
from gups_builder.config import Config
from gups_builder.engine.gups import Gups
from gups_builder.session import PendingChange, Session


def test_sessions_lists_sessions(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    world = tmp_path / "world"
    world.mkdir()
    Session.create(world, "area", "zones/mansion")
    rc = cli.main(["--world", str(world), "sessions"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "mansion" in out
    assert "area" in out
    assert "zones/mansion" in out


def test_models_prints_roles_and_hides_key(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GUPS_BUILDER_LOGIC_API_KEY", "super-secret")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    rc: int = cli.main(["--world", str(tmp_path / "world"), "models"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "logic" in out
    assert "prose" in out
    assert "super-secret" not in out
    assert "logic: model=(not set) url=http://127.0.0.1:11434/v1 api_key=set " in out
    assert "prose: model=(not set) url=http://127.0.0.1:11434/v1 api_key=not set " in out
    assert "logic: FAILED: the model is not configured" in out


def test_models_empty_model_shown_as_not_set(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    rc = cli.main(["--world", str(tmp_path / "world"), "models"])
    out = capsys.readouterr().out
    assert rc == 1
    assert "(not set)" in out
    assert "logic: FAILED: the model is not configured" in out
    assert "prose: FAILED: the model is not configured" in out


def test_skill_starts_a_skill_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started = _capture_start(monkeypatch)
    (tmp_path / "zones" / "caves").mkdir(parents=True)
    (tmp_path / "zones" / "caves" / "package.star").write_text('package(kind = "zone")\n')
    assert cli.main(["--world", str(tmp_path), "skill", "zones/caves"]) == 0
    assert started[0].state.mode == "skill"
    assert started[0].state.package == "zones/caves"


def test_skill_allows_a_missing_package_under_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The first write creates the package's package.star with kind = "skills".
    started = _capture_start(monkeypatch)
    assert cli.main(["--world", str(tmp_path), "skill", "skills/arcane"]) == 0
    assert started[0].state.mode == "skill"
    assert started[0].state.package == "skills/arcane"


def test_skill_refuses_a_missing_package_outside_skills(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started = _capture_start(monkeypatch)
    assert cli.main(["--world", str(tmp_path), "skill", "zones/caves"]) == 1
    assert started == []
    assert "zones/caves does not exist; skill mode needs an existing package" in (
        capsys.readouterr().out
    )


def test_mob_starts_a_mob_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    started = _capture_start(monkeypatch)
    (tmp_path / "zones" / "caves").mkdir(parents=True)
    (tmp_path / "zones" / "caves" / "package.star").write_text('package(kind = "zone")\n')
    assert cli.main(["--world", str(tmp_path), "mob", "zones/caves"]) == 0
    assert started[0].state.mode == "mob"
    assert started[0].state.package == "zones/caves"


def test_mob_refuses_a_missing_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    started = _capture_start(monkeypatch)
    assert cli.main(["--world", str(tmp_path), "mob", "zones/caves"]) == 1
    assert started == []
    assert "zones/caves does not exist; mob mode needs an existing package" in (
        capsys.readouterr().out
    )


def test_models_empty_api_key_is_not_set(
    capsys: pytest.CaptureFixture[str], tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("GUPS_BUILDER_LOGIC_API_KEY", "")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")
    cli.main(["--world", str(tmp_path / "world"), "models"])
    assert "logic: model=(not set) url=http://127.0.0.1:11434/v1 api_key=not set " in (
        capsys.readouterr().out
    )


def _fake_tty(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)


def test_resume_of_a_missing_session_fails_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _fake_tty(monkeypatch)
    rc = cli.main(["--world", str(tmp_path), "resume", "nope"])
    assert rc == 1
    assert "no session named 'nope'" in capsys.readouterr().out


def _capture_start(monkeypatch: pytest.MonkeyPatch) -> list[Session]:
    _fake_tty(monkeypatch)
    started: list[Session] = []

    def start(_world: Path, _config: object, session: Session) -> int:
        started.append(session)
        return 0

    monkeypatch.setattr(cli, "_start_repl", start)
    return started


def test_area_offers_to_resume_the_default_named_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    Session.create(tmp_path, "area", "zones/mansion")
    started = _capture_start(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: "y")
    assert cli.main(["--world", str(tmp_path), "area", "zones/mansion"]) == 0
    assert started[0].state.name == "mansion"


def test_area_declining_resume_names_the_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Session.create(tmp_path, "area", "zones/mansion", name="foo")
    started = _capture_start(monkeypatch)
    monkeypatch.setattr("builtins.input", lambda _: "n")
    assert cli.main(["--world", str(tmp_path), "area", "zones/mansion", "--name", "foo"]) == 0
    assert started[0].state.name == "foo-2"
    assert "foo-2" in capsys.readouterr().out


def test_area_does_not_offer_a_session_on_another_package(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    Session.create(tmp_path, "area", "zones/a/mansion")
    started = _capture_start(monkeypatch)

    def no_input(prompt: str) -> str:
        msg = f"should not ask: {prompt}"
        raise AssertionError(msg)

    monkeypatch.setattr("builtins.input", no_input)
    assert cli.main(["--world", str(tmp_path), "area", "zones/b/mansion"]) == 0
    assert started[0].state.name == "mansion-2"
    assert started[0].state.package == "zones/b/mansion"
    assert "mansion-2" in capsys.readouterr().out


def test_models_tty_launches_tui_and_check_bypasses_tui(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    tui_calls: list[Path] = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "home")

    def fake_tui(world: Path, *_args: object, **_kwargs: object) -> int:
        tui_calls.append(world)
        return 0

    monkeypatch.setattr(cli, "run_models_tui", fake_tui)

    assert cli.main(["--world", str(tmp_path / "world"), "models"]) == 0
    assert tui_calls == [tmp_path / "world"]

    assert cli.main(["--world", str(tmp_path / "world"), "models", "--check"]) == 1
    assert len(tui_calls) == 1
    assert "logic: FAILED: the model is not configured" in capsys.readouterr().out


@pytest.mark.parametrize(
    "argv",
    [
        ["area"],
        ["area", "zones/mansion"],
        ["mob", "zones/caves"],
        ["skill", "skills/arcane"],
        ["resume", "nope"],
    ],
)
def test_interactive_commands_require_terminal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: list[str],
) -> None:
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    monkeypatch.setattr("sys.stdout.isatty", lambda: False)

    def fail_if_called(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("should have exited before prompting or starting")

    monkeypatch.setattr("builtins.input", fail_if_called)
    monkeypatch.setattr(cli, "_start_repl", fail_if_called)
    monkeypatch.setattr(cli, "run_area_tui", fail_if_called)

    assert cli.main(["--world", str(tmp_path), *argv]) == 1
    assert "terminal" in capsys.readouterr().out.lower()
    assert not (tmp_path / ".builder" / "sessions").exists()


def test_area_without_package_launches_tui_and_starts_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _capture_start(monkeypatch)
    seen: list[Path] = []

    def fake_area_tui(world: Path) -> str:
        seen.append(world)
        return "zones/mansion"

    monkeypatch.setattr(cli, "run_area_tui", fake_area_tui)
    assert cli.main(["--world", str(tmp_path), "area"]) == 0
    assert seen == [tmp_path]
    assert started[0].state.mode == "area"
    assert started[0].state.package == "zones/mansion"


def test_area_without_package_tui_cancelled_exits_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = _capture_start(monkeypatch)
    monkeypatch.setattr(cli, "run_area_tui", lambda _world: None)
    assert cli.main(["--world", str(tmp_path), "area"]) == 0
    assert started == []


def test_toolbar_shows_the_pending_summary_and_nothing_when_none(tmp_path: Path) -> None:
    session = Session.create(tmp_path, "area", "zones/mansion")
    session.state.planning = True
    config = Config()
    config.logic.model = "big"

    idle = cli.toolbar_text(session.state, config)
    assert "mode: area (plan)" in idle
    assert "logic: big" in idle
    assert "prose: (not set)" in idle
    assert "idle" not in idle
    assert idle.rstrip().endswith("prose: (not set)")

    session.state.pending = PendingChange(
        ops=[], new_files={}, base_hashes={}, summary="3 rooms, 2 items"
    )
    assert cli.toolbar_text(session.state, config).rstrip().endswith("| 3 rooms, 2 items")


# -- the model-free design commands --------------------------------------------------

DESIGN = Path(__file__).resolve().parent / "fixtures" / "design" / "crypt.json"


def _json_out(capsys: pytest.CaptureFixture[str]) -> dict[str, object]:
    out: dict[str, object] = json.loads(capsys.readouterr().out)
    return out


def test_design_schema_prints_the_schema_and_rules(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["design-schema"]) == 0
    out = _json_out(capsys)
    schema = out["schema"]
    assert isinstance(schema, dict)
    assert schema["title"] == "AreaDesign"
    prompts = out["prompts"]
    assert isinstance(prompts, dict)
    assert "Do not list the exits." in prompts["room_prose"]


def test_apply_reports_a_design_it_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "design.json"
    bad.write_text('{"brief": {}}')
    assert cli.main(["--world", str(tmp_path), "apply", "zones/crypt", str(bad)]) == 2
    error = _json_out(capsys)["error"]
    assert isinstance(error, str)
    assert error.startswith(f"{bad} is not a valid design:")
    assert (tmp_path / ".builder" / ".gitignore").read_text() == "*\n"


@pytest.mark.engine
def test_world_info_describes_the_package(
    gups_bin: Path, fresh_template_world: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = ["--gups", str(gups_bin), "--world", str(fresh_template_world)]
    assert cli.main([*args, "world-info", "zones/crypt"]) == 0
    out = _json_out(capsys)
    assert out["has_rooms"] is False
    area = out["area"]
    assert isinstance(area, dict)
    assert "//zones/tutorial:gate" in area["linkable_rooms"]
    assert area["room_sizes"] == "cramped, normal, large"


@pytest.mark.engine
def test_apply_writes_a_design_that_lints_and_tests_clean(
    gups_bin: Path, fresh_template_world: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    world = fresh_template_world
    args = ["--gups", str(gups_bin), "--world", str(world)]
    assert cli.main([*args, "apply", "zones/crypt", str(DESIGN), "--check-only"]) == 0
    assert _json_out(capsys)["problems"] == []

    assert cli.main([*args, "apply", "zones/crypt", str(DESIGN)]) == 0
    dry = _json_out(capsys)
    assert dry["ok"] is True, dry
    assert not (world / "zones" / "crypt").exists()

    assert cli.main([*args, "apply", "zones/crypt", str(DESIGN), "--write"]) == 0
    out = _json_out(capsys)
    assert out["lint_after_write"] == []
    pkg = world / "zones" / "crypt"
    names = {"package.star", "rooms.star", "items.star", "mobs.star", "builder_test.star"}
    assert {p.name for p in pkg.iterdir()} == names
    assert Gups(gups_bin, world).test("zones/crypt").ok

    assert cli.main([*args, "apply", "zones/crypt", str(DESIGN), "--check-only"]) == 1
    problems = _json_out(capsys)["problems"]
    assert isinstance(problems, list)
    assert problems[0].startswith("zones/crypt already has rooms")
