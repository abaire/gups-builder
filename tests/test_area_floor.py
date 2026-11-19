"""Area mode's items on room floors: keys and floor items in a created area and in edit turns."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING

import pytest

from gups_builder.engine.gups import Gups
from gups_builder.llm.fake import FakeLlm
from gups_builder.modes.area import AreaTurn, area_turn, create_area
from gups_builder.modes.area_checks import turn_problems
from gups_builder.modes.area_floor import item_labels, room_floor
from gups_builder.modes.common import Context, Proposal
from gups_builder.star.ops import AddDecl, AppendItem, ItemMatch, RemoveDecl, RemoveItem
from gups_builder.star.reader import read_contents
from gups_builder.star.writer import apply_ops
from tests.conftest import read_fixture
from tests.test_area_create import _ctx, _decl, _load, _prose_replies, _real_mansion
from tests.test_area_refine import FIXTURES, GRID
from tests.test_area_refine import _ctx as _refine_ctx

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from gups_builder.engine.world import WorldDescription
    from gups_builder.star.values import Value


def _load_refine(name: str) -> str:
    return read_fixture(FIXTURES, name)


def _floor_spawn(label: str) -> Value:
    return {"$call": "spawn", "args": [label], "kwargs": {"respawn": "reset"}}


def test_floor_items_are_created_and_spawned_with_the_keys(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    plan = json.loads(_load("plan.json"))
    plan["items"] = [
        {"room": "kitchen", "item": "ladle", "concept": "a dented copper ladle", "kind": "misc"},
        {"room": "study", "item": "ladle"},
    ]
    ladle = {"name": "a copper ladle", "keywords": ["ladle", "copper"], "desc": "Dented."}
    llm = FakeLlm(
        {
            "logic": [_load("brief.json"), json.dumps(plan)],
            "prose": [*_prose_replies(), json.dumps(ladle)],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    assert proposal.summary == "+9 rooms, +2 items, 20 exits, 3 on the floor"
    assert sum(isinstance(op, AddDecl) and op.id == "ladle" for op in proposal.ops) == 1
    decl = _decl(proposal, "ladle")
    assert decl.file == "items.star"
    assert decl.args["kind"] == "misc"
    assert _decl(proposal, "kitchen").args["spawns"] == [_floor_spawn(":ladle")]
    assert _decl(proposal, "study").args["spawns"] == [
        _floor_spawn(":silver_key"),
        _floor_spawn(":ladle"),
    ]
    assert "spawns" not in _decl(proposal, "hall").args
    assert ("ladle", "name", "a copper ladle") in proposal.prose_preview


def test_an_item_of_another_package_may_lie_on_a_floor(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    plan = json.loads(_load("plan.json"))
    plan["items"] = [{"room": "hall", "item": "//zones/tutorial:brass_key"}]
    llm = FakeLlm({"logic": [_load("brief.json"), json.dumps(plan)], "prose": _prose_replies()})
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    assert _decl(proposal, "hall").args["spawns"] == [_floor_spawn("//zones/tutorial:brass_key")]
    assert not any(isinstance(op, AddDecl) and op.id == "brass_key" for op in proposal.ops)


def test_plan_problems_catch_keys_and_items_lying_nowhere(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    plan = json.loads(_load("plan.json"))
    del plan["keys"][0]["room"]
    plan["items"] = [
        {"room": "attic", "item": ":silver_key"},
        {"room": "hall", "item": "ghost_lamp"},
        {"room": "hall", "item": "Lamp", "concept": "a lamp"},
        {"room": "cellar", "item": ":silver_key"},
    ]
    llm = FakeLlm(
        {
            "logic": [_load("brief.json"), json.dumps(plan), _load("plan.json")],
            "prose": _prose_replies(),
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_area(ctx, "a medium sized creepy mansion")
    retry = [m.content for m in llm.calls[2][1] if m.role == "user"][-1]
    assert "the key 'silver_key' lies in no room" in retry
    assert "the item ':silver_key' lies in the unknown room 'attic'" in retry
    assert "the item 'ghost_lamp' on the floor of 'hall' is not an item of the world" in retry
    assert "the id 'Lamp' is not snake_case" in retry
    assert "the item ':silver_key' on the floor of 'cellar'" not in retry


def test_a_key_behind_its_own_door_is_sent_back(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    plan = json.loads(_load("plan.json"))
    # The cellar's only way in is locked with the very key it would hold.
    cellar = next(p for p in plan["passages"] if p["b"] == "cellar")
    cellar["door"] = {"state": "locked", "key": "silver_key", "tags": []}
    plan["keys"][0]["room"] = "cellar"
    llm = FakeLlm(
        {
            "logic": [_load("brief.json"), json.dumps(plan), _load("plan.json")],
            "prose": _prose_replies(),
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_area(ctx, "a medium sized creepy mansion")
    retry = [m.content for m in llm.calls[2][1] if m.role == "user"][-1]
    assert "the key 'silver_key' lies in 'cellar', which the player reaches only through" in retry


# -- edit turns ---------------------------------------------------------------------


def _edit_turn(*edits: Mapping[str, object]) -> str:
    return json.dumps({"kind": "edit", "reply": "Done.", "edits": list(edits)})


def _written(ctx: Context, proposal: Proposal) -> dict[str, str]:
    result = apply_ops(ctx.files, ctx.view, proposal.ops)
    assert [r.reason for r in result.refused] == []
    return {**ctx.files, **result.files}


def _floor_after(ctx: Context, files: dict[str, str], room: str) -> list[str]:
    view = read_contents(files, ctx.package)
    decl = view.get("room", room)
    assert decl is not None
    return room_floor(decl, item_labels(ctx), ctx.package)


def _with_files(ctx: Context, files: dict[str, str]) -> Context:
    return replace(ctx, files=files, view=read_contents(files, ctx.package))


def test_place_item_creates_a_new_item_and_spawns_it_on_the_floor(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    place = {"edit": "place_item", "room": "kitchen", "item": "ladle", "concept": "a ladle"}
    existing = {"edit": "place_item", "room": "kitchen", "item": ":silver_key"}
    ladle = {"name": "a copper ladle", "keywords": ["ladle"], "desc": "Dented."}
    llm = FakeLlm({"logic": [_edit_turn(place, existing)], "prose": [json.dumps(ladle)]})
    ctx, _ = _refine_ctx(tmp_path, llm, world_mansion)
    _, proposal = area_turn(ctx, dict(GRID), "put a ladle and the key in the kitchen", [])
    assert proposal is not None
    decl = next(op for op in proposal.ops if isinstance(op, AddDecl))
    assert (decl.id, decl.file, decl.args["kind"]) == ("ladle", "items.star", "misc")
    assert proposal.ops[-2:] == [
        AppendItem(kind="room", id="kitchen", name="spawns", value=_floor_spawn(":ladle")),
        AppendItem(kind="room", id="kitchen", name="spawns", value=_floor_spawn(":silver_key")),
    ]
    written = _written(ctx, proposal)
    floor = _floor_after(_with_files(ctx, written), written, "kitchen")
    assert floor == ["//zones/mansion:ladle", "//zones/mansion:silver_key"]
    assert 'spawn(":silver_key", respawn = "reset")' in written["rooms.star"]


def test_a_key_in_a_room_the_turn_adds_lies_on_its_floor(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    turn = json.loads(_load_refine("turn_add_cellar.json"))
    turn["edits"].append(
        {"edit": "add_key", "id": "cellar_key", "concept": "a key", "room": "wine_cellar"}
    )
    llm = FakeLlm(
        {
            "logic": [json.dumps(turn)],
            "prose": [_load_refine("prose_wine_cellar.json"), _load_refine("prose_study_key.json")],
        }
    )
    ctx, _ = _refine_ctx(tmp_path, llm, world_mansion)
    _, proposal = area_turn(ctx, dict(GRID), "add a wine cellar with a key in it", [])
    assert proposal is not None
    written = _written(ctx, proposal)
    assert _floor_after(_with_files(ctx, written), written, "wine_cellar") == [
        "//zones/mansion:cellar_key"
    ]


def test_unplace_item_takes_the_rule_off_and_remove_room_takes_it_away(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    place = {"edit": "place_item", "room": "kitchen", "item": ":silver_key"}
    llm = FakeLlm({"logic": [_edit_turn(place)]})
    ctx, _ = _refine_ctx(tmp_path, llm, world_mansion)
    _, proposal = area_turn(ctx, dict(GRID), "put the key in the kitchen", [])
    assert proposal is not None
    placed = _written(ctx, proposal)

    unplace = {"edit": "unplace_item", "room": "kitchen", "item": "//zones/mansion:silver_key"}
    llm = FakeLlm({"logic": [_edit_turn(unplace)]})
    ctx, _ = _refine_ctx(tmp_path, llm, world_mansion)
    ctx = _with_files(ctx, placed)
    _, proposal = area_turn(ctx, dict(GRID), "take the key away", [])
    assert proposal is not None
    assert proposal.ops == [
        RemoveItem(
            kind="room",
            id="kitchen",
            name="spawns",
            match=ItemMatch(call="spawn", first_arg=":silver_key"),
        )
    ]
    assert _floor_after(ctx, _written(ctx, proposal), "kitchen") == []

    remove = {"edit": "remove_room", "id": "kitchen"}
    llm = FakeLlm({"logic": [_edit_turn(remove)]})
    ctx, _ = _refine_ctx(tmp_path, llm, world_mansion)
    ctx = _with_files(ctx, placed)
    _, proposal = area_turn(ctx, dict(GRID), "remove the kitchen", [])
    assert proposal is not None
    assert proposal.ops[0] == RemoveDecl(kind="room", id="kitchen")
    assert not any(
        isinstance(op, (AppendItem, RemoveItem)) and op.id == "kitchen" for op in proposal.ops
    )


def test_floor_edits_are_checked(tmp_path: Path, world_mansion: WorldDescription) -> None:
    ctx, _ = _refine_ctx(tmp_path, FakeLlm({}), world_mansion)
    turn = AreaTurn.model_validate_json(
        _edit_turn(
            {"edit": "place_item", "room": "attic", "item": ":silver_key"},
            {"edit": "place_item", "room": "hall", "item": "ghost_lamp"},
            {"edit": "place_item", "room": "hall", "item": ":ghost"},
            {"edit": "place_item", "room": "hall", "item": "kitchen", "concept": "a pot"},
            {"edit": "unplace_item", "room": "hall", "item": ":silver_key"},
            {"edit": "add_key", "id": "cellar_key", "concept": "a key"},
        )
    )
    problems = turn_problems(ctx, dict(GRID), turn)
    assert "there is no room 'attic' in this package" in problems
    assert any(p.startswith("there is no item 'ghost_lamp'") for p in problems)
    # A mob is not an item, so it can't lie on a floor.
    assert any(p.startswith("there is no item ':ghost'") for p in problems)
    assert "the id 'kitchen' is already declared in the package; choose a fresh id" in problems
    assert "':silver_key' is not on the floor of 'hall'" in problems
    assert any(p.startswith("the key 'cellar_key' lies in no room") for p in problems)


def test_the_turn_prompt_shows_each_floor_apart_from_its_mobs(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    place = {"edit": "place_item", "room": "kitchen", "item": ":silver_key"}
    llm = FakeLlm({"logic": [_edit_turn(place)]})
    ctx, _ = _refine_ctx(tmp_path, llm, world_mansion)
    _, proposal = area_turn(ctx, dict(GRID), "put the key in the kitchen", [])
    assert proposal is not None
    first = "\n".join(m.content for m in llm.calls[0][1] if m.role == "user")
    assert ":silver_key: a small silver key" in first
    placed = _written(ctx, proposal)

    llm = FakeLlm(
        {"logic": [_edit_turn({"edit": "unplace_item", "room": "kitchen", "item": ":silver_key"})]}
    )
    ctx, _ = _refine_ctx(tmp_path, llm, world_mansion)
    ctx = _with_files(ctx, placed)
    area_turn(ctx, dict(GRID), "take the key away", [])
    prompt = "\n".join(m.content for m in llm.calls[0][1] if m.role == "user")
    kitchen = next(line for line in prompt.splitlines() if line.startswith("kitchen:"))
    assert "; spawns: none; floor: :silver_key" in kitchen
    assert "computed by code" not in kitchen


def test_a_key_added_behind_its_own_door_is_a_problem(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    ctx, _ = _refine_ctx(tmp_path, FakeLlm({}), world_mansion)
    door = {"state": "locked", "key": "study_key", "tags": []}
    turn = AreaTurn.model_validate_json(
        _edit_turn(
            {"edit": "set_door", "a": "hall", "b": "master_bedroom", "door": door},
            {"edit": "add_key", "id": "study_key", "concept": "a key", "room": "master_bedroom"},
        )
    )
    problems = turn_problems(ctx, dict(GRID), turn)
    assert any(p.startswith("the key 'study_key' lies in 'master_bedroom'") for p in problems)


# -- the real engine ----------------------------------------------------------------

_KEY_TEST = """\
def test_the_key_is_not_put_back_while_a_player_holds_it(t):
    w = t.world(seed = 1)
    study = w.room(":study")
    keys = [i for i in study.items() if i.label == "//zones/mansion:silver_key"]
    t.eq(len(keys), 1)
    p = w.spawn_player("Tess", at = ":study")
    p.do("get key")
    t.true(p.has_item(":silver_key"))
    p.do("south")
    w.run(pulses = 120)
    w.reset()
    t.eq([i for i in study.items() if i.label == "//zones/mansion:silver_key"], [])

def test_a_destroyed_key_comes_back_at_the_next_reset(t):
    w = t.world(seed = 1)
    study = w.room(":study")
    c = w.ctx()
    c.destroy([i for i in study.items() if i.label == "//zones/mansion:silver_key"][0])
    w.reset()
    keys = [i for i in study.items() if i.label == "//zones/mansion:silver_key"]
    t.eq(len(keys), 1)
"""


@pytest.mark.engine
def test_a_floor_key_stays_taken_until_it_leaves_the_world(
    gups_bin: Path, fresh_template_world: Path
) -> None:
    proposal = _real_mansion(gups_bin, fresh_template_world, _load("plan.json"))
    assert not any(note.startswith(("NOT VALID:", "BUILDER BUG:")) for note in proposal.notes)
    result = apply_ops({}, read_contents({}, "zones/mansion"), proposal.ops)
    assert result.refused == []
    package = fresh_template_world / "zones" / "mansion"
    package.mkdir(parents=True, exist_ok=True)
    for name, text in {**result.files, **proposal.new_files}.items():
        (package / name).write_text(text, encoding="utf-8")
    (package / "floor_test.star").write_text(_KEY_TEST, encoding="utf-8")
    run = Gups(gups_bin, fresh_template_world).test("zones/mansion")
    assert run.ok, run.output
