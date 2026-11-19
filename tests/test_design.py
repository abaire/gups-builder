"""Applying a design written outside the builder: checked, assembled and staged with no model."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from gups_builder.engine.lint import LintFinding
from gups_builder.llm.fake import FakeLlm
from gups_builder.modes.area import AreaBrief, build_from_brief
from gups_builder.modes.design import AreaDesign, apply_design, check_design, write_design
from gups_builder.star.reader import read_package
from gups_builder.star.writer import apply_ops
from tests.conftest import FakeEngine, PackageState, build_context, load_api, read_fixture

if TYPE_CHECKING:
    from gups_builder.engine.world import WorldDescription
    from gups_builder.modes.common import Context
    from gups_builder.star.reader import PackageView

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llm"
ROOMS = [
    "hall",
    "kitchen",
    "study",
    "library",
    "parlour",
    "master_bedroom",
    "nursery",
    "bedroom",
    "cellar",
]


def _room_prose(rid: str) -> dict[str, str]:
    return {
        "name": f"The {rid.replace('_', ' ').title()}",
        "desc": f"Dust lies thick across the {rid.replace('_', ' ')}.",
    }


def _key_prose() -> dict[str, object]:
    return json.loads(read_fixture(FIXTURES / "area_mansion", "prose_item.json"))  # type: ignore[no-any-return]


def _design_data(*, mobs: bool = False) -> dict[str, object]:
    data: dict[str, object] = {
        "brief": json.loads(read_fixture(FIXTURES / "area_mansion", "brief.json")),
        "graph": json.loads(read_fixture(FIXTURES / "area_mansion", "plan.json")),
        "room_prose": {rid: _room_prose(rid) for rid in ROOMS},
        "key_prose": {"silver_key": _key_prose()},
    }
    if mobs:
        plan = json.loads(read_fixture(FIXTURES / "mob_create", "plan.json"))
        data["mobs"] = {"mobs": plan["mobs"][:1]}
        data["mob_prose"] = {
            "goblin": {"name": "a goblin", "keywords": ["goblin"], "desc": "A goblin leers."}
        }
        data["item_prose"] = {
            "goblin_idol": {
                "name": "a goblin idol",
                "keywords": ["goblin", "idol"],
                "desc": "A crude wooden idol.",
            }
        }
    return data


def _design(**changes: object) -> AreaDesign:
    data = _design_data(mobs=bool(changes.pop("mobs", False)))
    data.update(changes)
    return AreaDesign.model_validate(data)


def _ctx(
    tmp_path: Path,
    world: WorldDescription,
    lint: list[list[LintFinding]] | None = None,
    llm: FakeLlm | None = None,
) -> tuple[Context, FakeEngine]:
    engine = FakeEngine(world, load_api(), lint)
    ctx = build_context(tmp_path, llm or FakeLlm({}), engine, PackageState("zones/mansion"))
    return ctx, engine


# -- checking ----------------------------------------------------------------------


def test_a_clean_design_has_no_problems_and_shows_neighbours(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    ctx, _ = _ctx(tmp_path, world_min)
    report = check_design(ctx, _design())
    assert report.problems == []
    assert "east: a cold kitchen (locked door)" in report.neighbours["hall"].splitlines()
    assert set(report.maps) == {-1, 0, 1}
    assert "kitchen" in report.maps[0]


def test_missing_and_extra_prose_are_problems(tmp_path: Path, world_min: WorldDescription) -> None:
    ctx, _ = _ctx(tmp_path, world_min)
    prose = {rid: _room_prose(rid) for rid in ROOMS if rid != "cellar"}
    prose["attic"] = _room_prose("attic")
    report = check_design(ctx, _design(room_prose=prose, key_prose={}))
    assert "room_prose has no entry for the room 'cellar'" in report.problems
    assert "room_prose names 'attic', which is not a room of the graph" in report.problems
    assert "key_prose has no entry for the key 'silver_key'" in report.problems


def test_prose_is_checked(tmp_path: Path, world_min: WorldDescription) -> None:
    ctx, _ = _ctx(tmp_path, world_min)
    prose = {rid: _room_prose(rid) for rid in ROOMS}
    prose["hall"] = {"name": "x" * 70, "desc": "Two\nlines."}
    report = check_design(ctx, _design(room_prose=prose))
    assert "hall: name is 70 characters, the limit is 60" in report.problems
    assert "hall: desc must be one paragraph" in report.problems


def test_a_locked_door_without_a_key_is_a_problem(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    ctx, _ = _ctx(tmp_path, world_min)
    graph = json.loads(read_fixture(FIXTURES / "area_mansion", "plan.json"))
    graph["passages"][0]["door"] = {"state": "locked", "key": None, "tags": []}
    graph["keys"] = []
    report = check_design(ctx, _design(graph=graph, key_prose={}))
    assert any("has no key" in p for p in report.problems)


def test_no_prose_is_checked_while_the_graph_has_problems(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    ctx, _ = _ctx(tmp_path, world_min)
    graph = json.loads(read_fixture(FIXTURES / "area_mansion", "plan.json"))
    graph["passages"].append({"a": "hall", "b": "attic", "direction": "west"})
    report = check_design(ctx, _design(graph=graph, room_prose={}))
    assert any("attic" in p for p in report.problems)
    assert not any("room_prose" in p for p in report.problems)


def test_a_package_with_rooms_is_refused(tmp_path: Path, world_mansion: WorldDescription) -> None:
    ctx, _ = _ctx(tmp_path, world_mansion)
    report = check_design(ctx, _design())
    assert report.problems == [
        "zones/mansion already has rooms; apply builds new areas only, so refine this one "
        "with: gups-builder area zones/mansion"
    ]


def test_mob_problems_are_reported_against_the_designed_rooms(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    ctx, _ = _ctx(tmp_path, world_min)
    data = _design_data(mobs=True)
    mobs = data["mobs"]
    assert isinstance(mobs, dict)
    mobs["mobs"][0]["spawns"] = [{"room": "attic", "max": 1}, {"room": "cellar", "max": 1}]
    report = check_design(ctx, AreaDesign.model_validate(data))
    assert report.problems == ["the spawn names the unknown room 'attic'"]


def test_missing_mob_and_loot_prose_are_problems(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    ctx, _ = _ctx(tmp_path, world_min)
    report = check_design(ctx, _design(mobs=True, mob_prose={}, item_prose={}))
    assert "mob_prose has no entry for the mob 'goblin'" in report.problems
    assert "item_prose has no entry for the new item 'goblin_idol'" in report.problems


# -- applying ----------------------------------------------------------------------


def test_apply_matches_the_model_pipeline(tmp_path: Path, world_min: WorldDescription) -> None:
    replies = [json.dumps(_room_prose(rid)) for rid in ROOMS]
    llm = FakeLlm(
        {
            "logic": [read_fixture(FIXTURES / "area_mansion", "plan.json")],
            "prose": [*replies, read_fixture(FIXTURES / "area_mansion", "prose_item.json")],
        }
    )
    model_ctx, _ = _ctx(tmp_path, world_min, llm=llm)
    brief = AreaBrief.model_validate_json(read_fixture(FIXTURES / "area_mansion", "brief.json"))
    proposal = build_from_brief(model_ctx, brief)
    expected = {
        **apply_ops(model_ctx.files, model_ctx.view, proposal.ops).files,
        **proposal.new_files,
    }

    ctx, engine = _ctx(tmp_path, world_min)
    result = apply_design(ctx, _design())
    assert result.ok
    assert {k: v for k, v in result.files.items() if k != "builder_test.star"} == expected
    assert result.summary == proposal.summary
    assert "builder_test.star" in result.files
    assert engine.test_calls == 1


def test_apply_with_mobs_spawns_them_in_the_new_rooms(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    ctx, _ = _ctx(tmp_path, world_min)
    result = apply_design(ctx, _design(mobs=True))
    assert result.ok, result.notes
    assert "goblin_idol" in result.files["items.star"]
    assert 'spawn(":goblin"' in result.files["rooms.star"]
    assert result.summary.endswith("; +1 mob, +1 item, 1 spawn")
    view = read_package_from(result.files, tmp_path)
    assert {d.id for d in view.decls if d.kind == "mob"} == {"goblin"}


def read_package_from(files: dict[str, str], tmp_path: Path) -> PackageView:
    pkg = tmp_path / "read" / "zones" / "mansion"
    pkg.mkdir(parents=True)
    for name, text in files.items():
        (pkg / name).write_text(text, encoding="utf-8")
    return read_package(pkg, "zones/mansion")


def test_lint_errors_are_rendered_and_not_ok(tmp_path: Path, world_min: WorldDescription) -> None:
    finding = LintFinding("error", "zones/mansion/rooms.star", 1, "something is wrong")
    ctx, engine = _ctx(tmp_path, world_min, lint=[[finding]])
    result = apply_design(ctx, _design())
    assert not result.ok
    assert result.lint_errors[0].startswith("zones/mansion/rooms.star:1: something is wrong\n> 1|")
    assert engine.test_calls == 0


def test_apply_with_problems_stages_nothing(tmp_path: Path, world_min: WorldDescription) -> None:
    ctx, engine = _ctx(tmp_path, world_min)
    result = apply_design(ctx, _design(room_prose={}))
    assert not result.ok
    assert result.report.problems
    assert result.files == {}
    assert engine.test_calls == 0


# -- writing -----------------------------------------------------------------------


def test_write_writes_the_package(tmp_path: Path, world_min: WorldDescription) -> None:
    ctx, _ = _ctx(tmp_path, world_min)
    result = apply_design(ctx, _design())
    written = write_design(ctx, result, with_link=False)
    pkg = tmp_path / "zones" / "mansion"
    assert sorted(written.files) == sorted(f"zones/mansion/{name}" for name in result.files)
    assert (pkg / "package.star").read_text().startswith("package(")
    assert (pkg / "rooms.star").read_text() == result.files["rooms.star"]
    assert written.lint == []


def test_write_refuses_a_result_that_is_not_ok(tmp_path: Path, world_min: WorldDescription) -> None:
    ctx, _ = _ctx(tmp_path, world_min)
    result = apply_design(ctx, _design(room_prose={}))
    written = write_design(ctx, result, with_link=False)
    assert written.files == []
    assert not (tmp_path / "zones" / "mansion").exists()


def test_the_link_return_exit_is_written_only_when_asked(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    tutorial = tmp_path / "zones" / "tutorial"
    tutorial.mkdir(parents=True)
    rooms = 'room("gate",\n    name = "The Gate",\n    desc = "A gate.",\n    exits = [],\n)\n'
    (tutorial / "rooms.star").write_text(rooms)
    brief = json.loads(read_fixture(FIXTURES / "area_mansion", "brief.json"))
    brief["link_to"] = "//zones/tutorial:gate"
    graph = json.loads(read_fixture(FIXTURES / "area_mansion", "plan.json"))
    graph["link"] = {"room": "hall", "exit": "south", "external_label": "//zones/tutorial:gate"}

    ctx, _ = _ctx(tmp_path, world_min)
    result = apply_design(ctx, _design(brief=brief, graph=graph))
    assert result.ok, result.notes
    assert result.link_package == "zones/tutorial"
    assert "zones/tutorial/rooms.star" not in write_design(ctx, result, with_link=False).files
    assert (tutorial / "rooms.star").read_text() == rooms

    ctx, _ = _ctx(tmp_path, world_min)
    result = apply_design(ctx, _design(brief=brief, graph=graph))
    written = write_design(ctx, result, with_link=True)
    assert "zones/tutorial/rooms.star" in written.files
    assert (
        'exits = [exit(DIR.north, to = "//zones/mansion:hall")],'
        in (tutorial / "rooms.star").read_text()
    )


def test_a_new_floor_item_needs_item_prose_and_is_placed(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    graph = json.loads(read_fixture(FIXTURES / "area_mansion", "plan.json"))
    graph["items"] = [{"room": "hall", "item": "umbrella", "concept": "a black umbrella"}]
    ctx, _ = _ctx(tmp_path, world_min)
    report = check_design(ctx, _design(graph=graph))
    assert report.new_items == ["umbrella"]
    assert "item_prose has no entry for the new item 'umbrella'" in report.problems
    prose = {"umbrella": {"name": "a black umbrella", "keywords": ["umbrella"], "desc": "Wet."}}
    result = apply_design(ctx, _design(graph=graph, item_prose=prose))
    assert result.ok, result.report.problems
    assert 'item("umbrella"' in result.files["items.star"]
    assert 'spawns = [spawn(":umbrella", respawn = "reset")]' in result.files["rooms.star"]
