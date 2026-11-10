"""Engine bridge tests: models of describe-world and api.json, lint parsing, staging, Gups."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from gups_builder.engine.api import ApiModel
from gups_builder.engine.gups import EngineError, Gups
from gups_builder.engine.lint import LintFinding, parse_github
from gups_builder.engine.staging import staged_world
from gups_builder.engine.world import WorldDescription, parse_describe_world

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def test_world_min_parses() -> None:
    world = WorldDescription.model_validate_json((FIXTURES / "describe_world_min.json").read_text())
    assert world.format == 1
    assert [s.name for s in world.schema.stats] == ["str", "dex", "con", "int", "wis", "armor"]
    assert [v.name for v in world.schema.vitals] == ["hp", "mana"]
    assert [t.name for t in world.schema.damage_types] == ["bludgeon", "slash", "fire"]
    assert world.schema.unarmed_damage_type == "bludgeon"
    assert world.schema.channels == ["sight", "smell", "sound", "spell"]
    assert world.features == ["combat", "leveling", "skills", "track"]
    assert world.commands == {"cast": "invoke"}


def test_world_min_failed_package_and_nulls(world_min: WorldDescription) -> None:
    broken = world_min.packages[0]
    assert broken.path == "zones/broken"
    assert broken.ok is False
    assert broken.kind is None
    gate = next(r for r in world_min.rooms if r.label == "//zones/tutorial:gate")
    assert gate.exits[0].door is None
    guide = next(m for m in world_min.mobs if m.label == "//zones/tutorial:guide")
    assert guide.factions is None
    assert world_min.errors[0].line == 12


def test_world_min_helpers(world_min: WorldDescription) -> None:
    assert world_min.has_feature("combat")
    assert not world_min.has_feature("trade")
    assert [r.label for r in world_min.rooms_in("zones/tutorial")] == [
        "//zones/tutorial:hall",
        "//zones/tutorial:gate",
    ]
    assert [m.label for m in world_min.mobs_in("zones/tutorial")] == ["//zones/tutorial:guide"]
    assert [i.label for i in world_min.items_in("zones/tutorial")] == ["//zones/tutorial:brass_key"]
    assert [s.label for s in world_min.skills_in("zones/tutorial")] == ["//zones/tutorial:fireball"]
    assert world_min.label_exists("//zones/tutorial:brass_key")
    assert world_min.label_exists("//zones/tutorial:hall")
    assert not world_min.label_exists("//zones/other:hall")


def test_world_min_ignores_unknown_keys() -> None:
    doc = json.loads((FIXTURES / "describe_world_min.json").read_text())
    doc["new_engine_field"] = 1
    doc["rooms"][0]["new_room_field"] = "x"
    world = WorldDescription.model_validate(doc)
    assert world.format == 1


def test_format_2_is_refused() -> None:
    doc = json.loads((FIXTURES / "describe_world_min.json").read_text())
    doc["format"] = 2
    with pytest.raises(
        EngineError,
        match="gups describe-world format 2 is not supported; this gups-builder needs format 1",
    ):
        parse_describe_world(json.dumps(doc))


def test_parse_github_with_line() -> None:
    line = "::error file=zones/mansion/rooms.star,line=12,title=gups lint::room door: unknown state"
    [finding] = parse_github(line)
    assert finding == LintFinding(
        "error", "zones/mansion/rooms.star", 12, "room door: unknown state"
    )


def test_parse_github_without_line() -> None:
    line = "::error file=package.star,title=gups lint:://zones/tutorial failed to load"
    [finding] = parse_github(line)
    assert finding.severity == "error"
    assert finding.file == "package.star"
    assert finding.line is None
    assert finding.message == "//zones/tutorial failed to load"


def test_parse_github_warning_and_escapes() -> None:
    line = "::warning file=a.star,title=gups lint::100%25 done%0Asecond%0D"
    [finding] = parse_github(line)
    assert finding.severity == "warning"
    assert finding.message == "100% done\nsecond\r"


def test_parse_github_ignores_other_lines() -> None:
    output = "\n".join(
        [
            "::group=Loading",
            "0 errors, 1 warning",
            "::error file=x.star,line=1,title=something else::not ours",
            "",
            "::warning file=y.star,title=gups lint::kept",
        ]
    )
    [finding] = parse_github(output)
    assert finding.file == "y.star"


def test_api_model_parses_fixture() -> None:
    api = ApiModel.model_validate_json((FIXTURES / "api_min.json").read_text())
    assert api.api_version == 1
    names = [entry.name for entry in api.declarations]
    assert names == ["door", "exit", "item", "loot", "mob", "package", "room", "skill", "spawn"]

    room = api.declaration("room")
    size = next(p for p in room.params if p.name == "size")
    assert size.allowed_values == ["cramped", "normal", "large"]
    assert size.type == "str"

    with pytest.raises(KeyError):
        api.declaration("nope")
    assert api.effect("damage") is not None
    assert api.effect("nope") is None
    assert api.behavior("wander") is not None
    assert api.behavior("nope") is None


def test_staged_world_skips_and_cleans(tmp_path: Path) -> None:
    world = tmp_path / "world"
    for name in ("zones", "plugins", ".git", ".gups", ".builder", "local-world-test"):
        (world / name).mkdir(parents=True)
        (world / name / "file.star").write_text("x")
    with staged_world(world) as staged:
        assert (staged / "zones" / "file.star").is_file()
        assert (staged / "plugins" / "file.star").is_file()
        assert not (staged / ".git").exists()
        assert not (staged / ".gups").exists()
        assert not (staged / ".builder").exists()
        assert not (staged / "local-world-test").exists()
        holder = staged.parent
    assert not holder.exists()


def _make_fake_bin(path: Path, contents: str = "#!/bin/sh\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_find_prefers_config_path(tmp_path: Path) -> None:
    binary = _make_fake_bin(tmp_path / "bin-a")
    other = _make_fake_bin(tmp_path / "bin-b")
    found = Gups.find(str(binary), {"GUPS_BIN": str(other), "PATH": ""})
    assert found == binary


def test_find_uses_gups_bin_env(tmp_path: Path) -> None:
    binary = _make_fake_bin(tmp_path / "gups")
    found = Gups.find(None, {"GUPS_BIN": str(binary), "PATH": ""})
    assert found == binary


def test_find_uses_path(tmp_path: Path) -> None:
    binary = _make_fake_bin(tmp_path / "dir" / "gups")
    found = Gups.find(None, {"PATH": str(tmp_path / "dir")})
    assert found == binary


def test_find_raises_when_nothing(tmp_path: Path) -> None:
    with pytest.raises(EngineError):
        Gups.find(None, {"PATH": ""})
    with pytest.raises(EngineError):
        Gups.find(str(tmp_path / "missing"), {"PATH": ""})


@pytest.mark.engine
def test_describe_world_template(gups_bin: Path, template_world: Path) -> None:
    world = Gups(gups_bin, template_world).describe_world()
    assert world.format == 1
    hall = next(r for r in world.rooms_in("zones/tutorial") if r.label == "//zones/tutorial:hall")
    east = next(e for e in hall.exits if e.dir == "east")
    assert east.to == "//zones/tutorial:armory"
    assert east.door is not None
    assert east.door.state == "locked"
    assert east.door.key == "//zones/tutorial:brass_key"


@pytest.mark.engine
def test_lint_reports_broken_file(gups_bin: Path, template_world: Path) -> None:
    with staged_world(template_world) as staged:
        (staged / "zones" / "tutorial" / "broken.star").write_text("room(:)")
        findings = Gups(gups_bin, staged).lint("zones/tutorial")
    broken = [f for f in findings if f.file == "zones/tutorial/broken.star"]
    assert broken
    assert any(f.line == 1 for f in broken)
    assert all(f.severity == "error" for f in broken)


@pytest.mark.engine
def test_test_command_passes(gups_bin: Path, template_world: Path) -> None:
    run = Gups(gups_bin, template_world).test("zones/tutorial")
    assert run.ok
    assert run.output.strip() != ""


@pytest.mark.engine
def test_api_model_from_engine(gups_bin: Path, template_world: Path) -> None:
    api = Gups(gups_bin, template_world).api()
    size = next(p for p in api.declaration("room").params if p.name == "size")
    assert size.allowed_values == ["cramped", "normal", "large"]
    assert api.effect("damage") is not None
    assert api.behavior("wander") is not None


def test_describe_world_error_on_bad_exit(tmp_path: Path) -> None:
    binary = _make_fake_bin(tmp_path / "gups", "#!/bin/sh\nexit 1\n")
    gups = Gups(binary, tmp_path)
    with pytest.raises(EngineError, match="describe-world"):
        gups.describe_world()


def test_describe_world_format_check(tmp_path: Path) -> None:
    doc = json.loads((FIXTURES / "describe_world_min.json").read_text())
    doc["format"] = 3
    binary = _make_fake_bin(tmp_path / "gups", f"#!/bin/sh\ncat <<'EOF'\n{json.dumps(doc)}\nEOF\n")
    gups = Gups(binary, tmp_path)
    with pytest.raises(EngineError, match="format 3 is not supported"):
        gups.describe_world()


def test_find_refuses_a_configured_path_that_is_not_executable(tmp_path: Path) -> None:
    on_path = _make_fake_bin(tmp_path / "dir" / "gups")
    path_env = str(on_path.parent)
    with pytest.raises(EngineError, match="missing"):
        Gups.find(str(tmp_path / "missing"), {"PATH": path_env})
    with pytest.raises(EngineError, match="GUPS_BIN"):
        Gups.find(None, {"GUPS_BIN": str(tmp_path / "missing"), "PATH": path_env})


def test_describe_world_non_json_is_engine_error(tmp_path: Path) -> None:
    binary = _make_fake_bin(tmp_path / "gups", "#!/bin/sh\necho 'not json'\n")
    with pytest.raises(EngineError, match="describe-world"):
        Gups(binary, tmp_path).describe_world()


def test_api_non_json_is_engine_error(tmp_path: Path) -> None:
    binary = _make_fake_bin(tmp_path / "gups", '#!/bin/sh\necho \'{"api_version": "x"\'\n')
    with pytest.raises(EngineError, match="docs --json"):
        Gups(binary, tmp_path).api()


def test_lint_that_fails_without_findings_is_engine_error(tmp_path: Path) -> None:
    binary = _make_fake_bin(
        tmp_path / "gups", "#!/bin/sh\necho 'Error: no such option --bogus.' >&2\nexit 1\n"
    )
    with pytest.raises(EngineError, match="no such option"):
        Gups(binary, tmp_path).lint("zones/x")


def test_lint_findings_are_returned_whatever_the_exit_code(tmp_path: Path) -> None:
    script = "#!/bin/sh\necho '::error file=zones/x/a.star,line=2,title=gups lint::bad'\nexit 1\n"
    binary = _make_fake_bin(tmp_path / "gups", script)
    [finding] = Gups(binary, tmp_path).lint("zones/x")
    assert finding.line == 2
