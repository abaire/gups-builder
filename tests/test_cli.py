from pathlib import Path

import pytest

from gups_builder import cli
from gups_builder.session import Session


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


def test_resume_of_a_missing_session_fails_cleanly(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rc = cli.main(["--world", str(tmp_path), "resume", "nope"])
    assert rc == 1
    assert "no session named 'nope'" in capsys.readouterr().out


def _capture_start(monkeypatch: pytest.MonkeyPatch) -> list[Session]:
    started: list[Session] = []

    def start(world: Path, config: object, session: Session) -> int:
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
        raise AssertionError(f"should not ask: {prompt}")

    monkeypatch.setattr("builtins.input", no_input)
    assert cli.main(["--world", str(tmp_path), "area", "zones/b/mansion"]) == 0
    assert started[0].state.name == "mansion-2"
    assert started[0].state.package == "zones/b/mansion"
    assert "mansion-2" in capsys.readouterr().out
