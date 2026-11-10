"""Refining an area: a follow-up message becomes a turn, and an edit turn becomes ops.

The tests start from the Phase 8 mansion, read as real .star files, and drive
``area_turn`` with scripted replies. Each scenario checks the turn kind, the ops
it produced, the notes it carried, and — where the edit changes the map — that
the new room's coordinate is recorded for the next turn.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from gups_builder.config import Config
from gups_builder.engine.api import ApiModel
from gups_builder.engine.gups import Gups, TestRun
from gups_builder.engine.lint import LintFinding
from gups_builder.engine.world import WorldDescription
from gups_builder.llm.fake import FakeLlm
from gups_builder.modes.area import AreaTurn, area_turn
from gups_builder.modes.common import Context
from gups_builder.star.ops import (
    AddDecl,
    AppendItem,
    ItemMatch,
    RemoveDecl,
    RemoveItem,
    SetArg,
)
from gups_builder.star.reader import read_package

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llm" / "area_refine"
MANSION = Path(__file__).resolve().parent / "fixtures" / "mansion"
REPO = Path(__file__).resolve().parent.parent

# The Phase 8 mansion's coordinates; the cellar sits under the parlour so the
# kitchen's z-1 is free for the wine cellar.
GRID = {
    "hall": (0, 0, 0),
    "kitchen": (1, 0, 0),
    "study": (0, 1, 0),
    "library": (1, 1, 0),
    "parlour": (2, 0, 0),
    "master_bedroom": (0, 0, 1),
    "nursery": (1, 1, 1),
    "bedroom": (2, 0, 1),
    "cellar": (2, 0, -1),
    "vault": (-1, 0, 0),
}


class FakeEngine:
    def __init__(
        self, world: WorldDescription, api: ApiModel, lint: list[list[LintFinding]] | None = None
    ) -> None:
        self._world = world
        self._api = api
        self._lint = list(lint or [])

    def describe_world(self, world: Path | None = None) -> WorldDescription:
        return self._world

    def api(self) -> ApiModel:
        return self._api

    def lint(self, package: str, world: Path | None = None) -> list[LintFinding]:
        return self._lint.pop(0) if self._lint else []

    def test(self, package: str, world: Path | None = None) -> TestRun:
        return TestRun(ok=True, output="")


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


def _ctx(
    tmp_path: Path,
    llm: FakeLlm,
    world: WorldDescription,
    lint: list[list[LintFinding]] | None = None,
) -> tuple[Context, FakeEngine]:
    api = ApiModel.model_validate_json((REPO / "tests" / "fixtures" / "api_min.json").read_text())
    engine = FakeEngine(world, api, lint)
    files = {name: (MANSION / name).read_text() for name in ("rooms.star", "items.star")}
    ctx = Context(
        world_dir=tmp_path,
        world=world,
        api=api,
        package="zones/mansion",
        view=read_package(MANSION, "zones/mansion"),
        files=files,
        llm=llm,
        config=Config(max_repairs=1),
        engine=engine,
    )
    return ctx, engine


DOOR_LOCKED: dict[str, object] = {
    "$call": "door",
    "args": [],
    "kwargs": {"state": {"$const": "DOOR.locked"}, "key": ":study_key"},
}


def _exit(direction: str, to: str, door: dict[str, object] | None = None) -> dict[str, object]:
    kwargs: dict[str, object] = {"to": to}
    if door is not None:
        kwargs["door"] = door
    return {"$call": "exit", "args": [{"$const": f"DIR.{direction}"}], "kwargs": kwargs}


def test_add_a_wine_cellar_under_the_kitchen(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm(
        {"logic": [_load("turn_add_cellar.json")], "prose": [_load("prose_wine_cellar.json")]}
    )
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    coords = dict(GRID)
    turn, proposal = area_turn(ctx, coords, "add a wine cellar under the kitchen", [])
    assert isinstance(turn, AreaTurn)
    assert turn.kind == "edit"
    assert proposal is not None
    cellar = next(op for op in proposal.ops if isinstance(op, AddDecl) and op.id == "wine_cellar")
    assert cellar.file == "rooms.star"
    assert cellar.args["name"] == "The Wine Cellar"
    assert cellar.args["exits"] == [_exit("up", ":kitchen")]
    appends = [
        op
        for op in proposal.ops
        if isinstance(op, AppendItem) and op.id == "kitchen" and op.name == "exits"
    ]
    assert [op.value for op in appends] == [_exit("down", ":wine_cellar")]
    assert coords["wine_cellar"] == (1, 0, -1)


def test_lock_the_study_puts_doors_on_both_sides(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm(
        {"logic": [_load("turn_lock_study.json")], "prose": [_load("prose_study_key.json")]}
    )
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    turn, proposal = area_turn(ctx, dict(GRID), "lock the study", [])
    assert turn.kind == "edit"
    assert proposal is not None
    hall = [
        op
        for op in proposal.ops
        if isinstance(op, AppendItem) and op.id == "hall" and op.name == "exits"
    ]
    assert [op.value for op in hall] == [_exit("north", ":study", DOOR_LOCKED)]
    study = [
        op
        for op in proposal.ops
        if isinstance(op, AppendItem) and op.id == "study" and op.name == "exits"
    ]
    assert [op.value for op in study] == [_exit("south", ":hall", DOOR_LOCKED)]
    exit_ops = [
        (type(op).__name__, op.id)
        for op in proposal.ops
        if isinstance(op, (RemoveItem, AppendItem)) and op.name == "exits"
    ]
    assert exit_ops == [
        ("RemoveItem", "hall"),
        ("AppendItem", "hall"),
        ("RemoveItem", "study"),
        ("AppendItem", "study"),
    ]
    removes = [op for op in proposal.ops if isinstance(op, RemoveItem)]
    assert [op.match for op in removes] == [
        ItemMatch(call="exit", first_arg={"$const": "DIR.north"}),
        ItemMatch(call="exit", first_arg={"$const": "DIR.south"}),
    ]
    key = next(op for op in proposal.ops if isinstance(op, AddDecl) and op.id == "study_key")
    assert key.file == "items.star"
    assert key.args["kind"] == "key"
    assert key.args["name"] == "a heavy brass key"
    assert key.args["desc"] == "A heavy brass key, worn smooth by old hands."


def test_make_the_library_creepier_changes_only_its_desc(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm(
        {"logic": [_load("turn_rewrite_library.json")], "prose": [_load("prose_library.json")]}
    )
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    turn, proposal = area_turn(ctx, dict(GRID), "make the library creepier", [])
    assert turn.kind == "edit"
    assert proposal is not None
    set_args = [
        op
        for op in proposal.ops
        if isinstance(op, SetArg) and op.id == "library" and op.name == "desc"
    ]
    assert [op.value for op in set_args] == ["The books whisper when the candle gutters."]
    touched = [op for op in proposal.ops if isinstance(op, (SetArg, RemoveItem, AppendItem))]
    assert all(op.id == "library" and op.name == "desc" for op in touched)


def test_add_a_ghost_is_out_of_scope_for_mobs(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load("turn_add_ghost.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    turn, proposal = area_turn(ctx, dict(GRID), "add a ghost", [])
    assert turn.kind == "out_of_scope"
    assert turn.other_mode == "mob"
    assert turn.note == "add a ghost"
    assert proposal is None


def test_an_edit_onto_a_taken_coordinate_is_repaired(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [_load("turn_bad_coord.json"), _load("turn_good_coord.json")],
            "prose": [_load("prose_new_room.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    coords = dict(GRID)
    turn, proposal = area_turn(ctx, coords, "add a room", [])
    assert turn.kind == "edit"
    retry = [m.content for m in llm.calls[1][1] if m.role == "user"][-1]
    assert "share the coordinate" in retry
    assert proposal is not None
    new = next(op for op in proposal.ops if isinstance(op, AddDecl) and op.id == "new_room")
    assert new.args["exits"] == [_exit("west", ":parlour")]
    assert coords["new_room"] == (3, 0, 0)


def test_a_room_with_opaque_exits_is_refused_and_the_rest_stands(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load("turn_door_vault.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    turn, proposal = area_turn(ctx, dict(GRID), "put a door on the vault", [])
    assert turn.kind == "edit"
    assert proposal is not None
    door_closed: dict[str, object] = {
        "$call": "door",
        "args": [],
        "kwargs": {"state": {"$const": "DOOR.closed"}},
    }
    hall = [
        op
        for op in proposal.ops
        if isinstance(op, AppendItem) and op.id == "hall" and op.name == "exits"
    ]
    assert [op.value for op in hall] == [_exit("west", ":vault", door_closed)]
    assert any(
        "vault" in note and "computed by code" in note and "rooms.star" in note
        for note in proposal.notes
    )


def test_connect_onto_a_one_way_exit_adds_only_the_missing_side(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    # The vault already has its exit east to the hall; only the hall's way back is new.
    llm = FakeLlm({"logic": [_load("turn_connect_vault.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    turn, proposal = area_turn(ctx, dict(GRID), "connect the vault to the hall", [])
    assert proposal is not None
    exit_ops = [op for op in proposal.ops if isinstance(op, (RemoveItem, AppendItem))]
    assert [(type(op).__name__, op.id) for op in exit_ops] == [("AppendItem", "hall")]
    assert not proposal.notes


def test_connect_with_the_door_already_there_changes_nothing(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    # The kitchen door's key is a full label in describe-world and a bare id in the edit.
    llm = FakeLlm({"logic": [_load("turn_reconnect_kitchen.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    turn, proposal = area_turn(ctx, dict(GRID), "lock the kitchen with the silver key", [])
    assert proposal is not None
    assert proposal.ops == []


def test_a_door_on_rooms_with_no_passage_is_repaired(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [_load("turn_door_no_passage.json"), _load("turn_lock_study.json")],
            "prose": [_load("prose_study_key.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    area_turn(ctx, dict(GRID), "lock the study", [])
    retry = [m.content for m in llm.calls[1][1] if m.role == "user"][-1]
    assert "no passage between 'study' and 'kitchen'" in retry


def test_unknown_mobs_and_rooms_are_repaired(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load("turn_spawn_unknown.json"), _load("turn_spawns.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    area_turn(ctx, dict(GRID), "add a banshee", [])
    retry = [m.content for m in llm.calls[1][1] if m.role == "user"][-1]
    assert "no mob '//zones/mansion:banshee'" in retry
    assert "no room 'attic'" in retry


def test_an_existing_room_id_is_repaired(tmp_path: Path, world_mansion: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [_load("turn_add_existing_id.json"), _load("turn_good_coord.json")],
            "prose": [_load("prose_new_room.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    area_turn(ctx, dict(GRID), "add a room", [])
    retry = [m.content for m in llm.calls[1][1] if m.role == "user"][-1]
    assert "room 'hall' already exists" in retry


def test_spawns_use_the_package_label_form_in_the_file(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load("turn_spawns.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    turn, proposal = area_turn(ctx, dict(GRID), "move the ghost to the hall", [])
    assert proposal is not None
    add = next(op for op in proposal.ops if isinstance(op, AppendItem) and op.name == "spawns")
    assert add.id == "hall"
    assert add.value == {"$call": "spawn", "args": [":ghost"], "kwargs": {"count": 1}}
    remove = next(op for op in proposal.ops if isinstance(op, RemoveItem) and op.name == "spawns")
    assert remove.id == "cellar"
    assert remove.match == ItemMatch(call="spawn", first_arg=":ghost")
    assert not proposal.notes


def test_the_turn_prompt_shows_spawns_code_fields_and_mobs(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load("turn_add_ghost.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    area_turn(ctx, dict(GRID), "add a ghost", [])
    prompt = [m.content for m in llm.calls[0][1] if m.role == "user"][-1]
    cellar = next(line for line in prompt.splitlines() if line.startswith("cellar:"))
    assert "spawns: :ghost x1" in cellar
    vault = next(line for line in prompt.splitlines() if line.startswith("vault:"))
    assert "computed by code: exits" in vault
    hall = next(line for line in prompt.splitlines() if line.startswith("hall:"))
    assert "east->kitchen [locked door, key //zones/mansion:silver_key]" in hall
    assert "//zones/mansion:ghost: the ghost (level 3)" in prompt


def test_a_rewrite_keeps_the_room_name_in_the_prompt(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm(
        {"logic": [_load("turn_rewrite_library.json")], "prose": [_load("prose_library.json")]}
    )
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    area_turn(ctx, dict(GRID), "make the library creepier", [])
    prose_prompt = [m.content for m in llm.calls[1][1] if m.role == "user"][-1]
    assert "Name: The Library" in prose_prompt
    assert "Dust lies thick across the library." in prose_prompt


def test_a_new_room_hears_its_neighbours_by_name(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm(
        {"logic": [_load("turn_add_cellar.json")], "prose": [_load("prose_wine_cellar.json")]}
    )
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    area_turn(ctx, dict(GRID), "add a wine cellar under the kitchen", [])
    prose_prompt = [m.content for m in llm.calls[1][1] if m.role == "user"][-1]
    assert "up: The Kitchen" in prose_prompt


def test_a_lint_repair_sends_the_previous_turn_and_keeps_only_the_final_coordinates(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    lint = [[LintFinding("error", "zones/mansion/rooms.star", 3, "bad")], []]
    llm = FakeLlm(
        {
            "logic": [_load("turn_add_cellar.json"), _load("turn_good_coord.json")],
            "prose": [_load("prose_wine_cellar.json"), _load("prose_new_room.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_mansion, lint)
    coords = dict(GRID)
    turn, proposal = area_turn(ctx, coords, "add a room", [])
    repair = [m.content for m in llm.calls[2][1] if m.role == "user"][-1]
    assert '"wine_cellar"' in repair
    assert turn.reply == "Added a room east of the parlour."
    assert proposal is not None
    assert proposal.grid is not None
    assert proposal.grid["new_room"] == (3, 0, 0)
    assert "wine_cellar" not in proposal.grid
    assert coords["new_room"] == (3, 0, 0)
    assert "wine_cellar" not in coords


def test_remove_room_takes_the_room_and_the_exits_pointing_to_it(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load("turn_remove_study.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    coords = dict(GRID)
    turn, proposal = area_turn(ctx, coords, "remove the study", [])
    assert turn.kind == "edit"
    assert proposal is not None
    assert any(isinstance(op, RemoveDecl) and op.id == "study" for op in proposal.ops)
    removed = [op for op in proposal.ops if isinstance(op, RemoveItem) and op.name == "exits"]
    assert len(removed) == 2
    assert sorted(((op.id, op.match) for op in removed), key=lambda pair: pair[0]) == [
        ("hall", ItemMatch(call="exit", first_arg={"$const": "DIR.north"})),
        ("library", ItemMatch(call="exit", first_arg={"$const": "DIR.west"})),
    ]
    assert "study" not in coords


def test_untouched_rooms_keep_their_coordinates_across_two_turns(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [_load("turn_add_cellar.json"), _load("turn_rewrite_library.json")],
            "prose": [_load("prose_wine_cellar.json"), _load("prose_library.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    coords = dict(GRID)
    area_turn(ctx, coords, "add a wine cellar under the kitchen", [])
    area_turn(ctx, coords, "make the library creepier", [])
    assert coords["wine_cellar"] == (1, 0, -1)
    for rid, c in GRID.items():
        assert coords[rid] == c


@pytest.mark.engine
def test_a_refined_room_passes_the_real_engine(
    tmp_path: Path, gups_bin: Path, template_world: Path
) -> None:
    # A prose rewrite of the template world's tutorial hall is staged and run
    # through the real engine; the opaque fields of that room stay untouched.
    gups = Gups(gups_bin, template_world)
    world = gups.describe_world()
    api = gups.api()
    llm = FakeLlm({"logic": [_load("turn_rewrite_hall.json")], "prose": [_load("prose_hall.json")]})
    ctx = Context(
        world_dir=template_world,
        world=world,
        api=api,
        package="zones/tutorial",
        view=read_package(template_world / "zones" / "tutorial", "zones/tutorial"),
        files={
            name: (template_world / "zones" / "tutorial" / name).read_text()
            for name in ("rooms.star",)
        },
        llm=llm,
        config=Config(max_repairs=3),
        engine=gups,
    )
    turn, proposal = area_turn(ctx, {}, "make the hall creepier", [])
    assert turn.kind == "edit"
    assert proposal is not None
    assert not any(note.startswith("NOT VALID:") for note in proposal.notes)
    assert not any(note.startswith("BUILDER BUG:") for note in proposal.notes)
