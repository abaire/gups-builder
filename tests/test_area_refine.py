"""Refining an area: a follow-up message becomes a turn, and an edit turn becomes ops.

The tests start from the Phase 8 mansion, read as real .star files, and drive
``area_turn`` with scripted replies. Each scenario checks the turn kind, the ops
it produced, the notes it carried, and — where the edit changes the map — that
the new room's coordinate is recorded for the next turn.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from gups_builder.config import Config
from gups_builder.engine.gups import Gups
from gups_builder.engine.lint import LintFinding
from gups_builder.engine.world import ExitInfo
from gups_builder.grid.model import RandomExit
from gups_builder.llm.client import Message
from gups_builder.llm.fake import FakeLlm
from gups_builder.modes.area import (
    AreaBrief,
    AreaTurn,
    area_turn,
    build_from_plan,
)
from gups_builder.modes.area_checks import RoomCount, grid_from_coords, turn_problems
from gups_builder.modes.area_plan import PlanRequest, plan_area_turn
from gups_builder.modes.common import Context, Proposal
from gups_builder.star.ops import (
    AddDecl,
    AppendItem,
    ItemMatch,
    RemoveDecl,
    RemoveItem,
    SetArg,
)
from gups_builder.star.reader import read_package
from tests.conftest import FakeEngine, PackageState, build_context, load_api, read_fixture

if TYPE_CHECKING:
    from gups_builder.engine.world import WorldDescription

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llm" / "area_refine"
MANSION = Path(__file__).resolve().parent / "fixtures" / "mansion"

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


def _load(name: str) -> str:
    return read_fixture(FIXTURES, name)


def _ctx(
    tmp_path: Path,
    llm: FakeLlm,
    world: WorldDescription,
    lint: list[list[LintFinding]] | None = None,
) -> tuple[Context, FakeEngine]:
    files = {name: (MANSION / name).read_text() for name in ("rooms.star", "items.star")}
    state = PackageState("zones/mansion", files, read_package(MANSION, "zones/mansion"))
    engine = FakeEngine(world, load_api(), lint)
    return build_context(tmp_path, llm, engine, state), engine


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


def _with_random_exit(world: WorldDescription) -> WorldDescription:
    """The mansion with a random exit already in the hall's file, as describe-world shows it."""
    swirl = ExitInfo(
        dir="swirl",
        candidates=["//zones/mansion:study", "//zones/mansion:library"],
        one_way=True,
    )
    rooms = [
        r.model_copy(update={"exits": [*r.exits, swirl]}) if r.label.endswith(":hall") else r
        for r in world.rooms
    ]
    return world.model_copy(update={"rooms": rooms})


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


@pytest.mark.parametrize("random_exit", [False, True], ids=["plain", "with_random_exit"])
def test_connect_onto_a_one_way_exit_adds_only_the_missing_side(
    tmp_path: Path, world_mansion: WorldDescription, random_exit: bool
) -> None:
    # The vault already has its exit east to the hall; only the hall's way back is new.
    # A random exit the hall already has is left alone by the passage diff.
    llm = FakeLlm({"logic": [_load("turn_connect_vault.json")]})
    world = _with_random_exit(world_mansion) if random_exit else world_mansion
    ctx, _ = _ctx(tmp_path, llm, world)
    _turn, proposal = area_turn(ctx, dict(GRID), "connect the vault to the hall", [])
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
    _turn, proposal = area_turn(ctx, dict(GRID), "lock the kitchen with the silver key", [])
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
    _turn, proposal = area_turn(ctx, dict(GRID), "move the ghost to the hall", [])
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


def test_plan_and_build_on_an_existing_area_uses_brief_theme_and_style(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    brief = AreaBrief(
        title="The Manor Wine Wing",
        theme="Ancient subterranean wine vaults",
        style_notes="Cobwebbed oak casks and chilly stone",
        room_count=RoomCount(min=1, max=2),
        room_concepts=["wine_cellar: vaulted cellar beneath the kitchen"],
    )
    plan_reply = '{"reply": "We can add a wine cellar under the kitchen.", "brief": ' + (
        brief.model_dump_json() + "}"
    )
    llm = FakeLlm(
        {
            "logic": [plan_reply, _load("turn_add_cellar.json")],
            "prose": [_load("prose_wine_cellar.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    coords = dict(GRID)

    request = PlanRequest("let's plan a wine cellar under the kitchen", [], None, coords)
    planned = plan_area_turn(ctx, request).turn
    assert planned.brief.title == "The Manor Wine Wing"
    plan_prompt = [m.content for m in llm.calls[0][1] if m.role == "user"][-1]
    assert "kitchen: The Kitchen at (1,0,0)" in plan_prompt

    turn, proposal = build_from_plan(ctx, planned.brief, coords)
    assert turn is not None
    assert proposal is not None
    assert coords["wine_cellar"] == (1, 0, -1)
    prose_prompt = [m.content for m in llm.calls[2][1] if m.role == "user"][-1]
    assert "Ancient subterranean wine vaults" in prose_prompt
    assert "Cobwebbed oak casks and chilly stone" in prose_prompt


@pytest.mark.engine
def test_a_refined_room_passes_the_real_engine(gups_bin: Path, template_world: Path) -> None:
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


def _hall_exit_values(proposal: Proposal, room: str = "hall") -> list[object]:
    return [
        op.value
        for op in proposal.ops
        if isinstance(op, AppendItem) and op.id == room and op.name == "exits"
    ]


def test_a_one_way_portal_adds_only_the_exit_out_of_its_room(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load("turn_one_way_portal.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    _turn, proposal = area_turn(ctx, dict(GRID), "add a chute from the hall to the cellar", [])
    assert proposal is not None
    chute = {
        "$call": "exit",
        "args": ["chute"],
        "kwargs": {"to": ":cellar", "one_way": True},
    }
    assert _hall_exit_values(proposal) == [chute]
    assert [op.id for op in proposal.ops if isinstance(op, AppendItem)] == ["hall"]


def test_a_random_exit_is_added_with_every_room_it_picks_among(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load("turn_random_exit.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    turn, proposal = area_turn(ctx, dict(GRID), "add a trick door to the hall", [])
    assert turn.kind == "edit"
    assert proposal is not None
    # The name is put in snake_case; the engine makes a multi-room exit one-way, so no flag.
    trick_door = {
        "$call": "exit",
        "args": ["trick_door"],
        "kwargs": {"to": [":study", ":library", ":nursery"]},
    }
    assert _hall_exit_values(proposal) == [trick_door]
    assert [op.id for op in proposal.ops if isinstance(op, AppendItem)] == ["hall"]
    assert proposal.summary == "1 change"


def test_a_random_exit_does_not_change_the_passages_it_names(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    # The hall already has a passage to the study; the random exit's first room must not
    # make the diff rewrite it or add a second copy of the random exit.
    llm = FakeLlm({"logic": [_load("turn_random_exit.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    _turn, proposal = area_turn(ctx, dict(GRID), "add a trick door to the hall", [])
    assert proposal is not None
    assert not [op for op in proposal.ops if isinstance(op, RemoveItem)]
    assert len(_hall_exit_values(proposal)) == 1
    assert _hall_exit_values(proposal, "study") == []


def test_a_random_exit_on_a_new_room_is_written_with_the_room(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [_load("turn_random_exit_new_room.json")],
            "prose": [_load("prose_new_room.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    _turn, proposal = area_turn(ctx, dict(GRID), "add a funhouse", [])
    assert proposal is not None
    funhouse = next(op for op in proposal.ops if isinstance(op, AddDecl) and op.id == "funhouse")
    exits = funhouse.args["exits"]
    assert isinstance(exits, list)
    assert {
        "$call": "exit",
        "args": ["mirror_maze"],
        "kwargs": {"to": [":hall", ":study"]},
    } in exits
    assert _hall_exit_values(proposal, "funhouse") == []
    prose_prompt = [m.content for m in llm.calls[1][1] if m.role == "user"][-1]
    assert "mirror_maze: one of The Hall or The Study" in prose_prompt


def test_random_exit_problems_are_repaired(tmp_path: Path, world_mansion: WorldDescription) -> None:
    llm = FakeLlm({"logic": [_load("turn_random_exit_bad.json"), _load("turn_random_exit.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    area_turn(ctx, dict(GRID), "add trick doors", [])
    retry = [m.content for m in llm.calls[1][1] if m.role == "user"][-1]
    assert "the random exit 'trick_door' from 'hall' has fewer than two distinct rooms" in retry
    assert "the random exit 'north' from 'hall' uses a standard direction" in retry
    assert "the random exit 'hole' from 'ghost_room' names the unknown room 'ghost_room'" in retry
    assert "'hall' already has an exit named 'slide'" in retry


def test_the_turn_prompt_lists_the_random_exit_edit(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load("turn_random_exit.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    area_turn(ctx, dict(GRID), "add a trick door to the hall", [])
    prompt = [m.content for m in llm.calls[0][1] if m.role == "user"][-1]
    assert "add_random_exit random_exit {room, exit, to}" in prompt


def test_the_turn_prompt_shows_an_existing_random_exit_and_its_rooms(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load("turn_add_ghost.json")]})
    ctx, _ = _ctx(tmp_path, llm, _with_random_exit(world_mansion))
    area_turn(ctx, dict(GRID), "add a ghost", [])
    prompt = [m.content for m in llm.calls[0][1] if m.role == "user"][-1]
    hall = next(line for line in prompt.splitlines() if line.startswith("hall:"))
    assert "swirl->one of study or library" in hall
    assert "None" not in hall


def test_an_existing_random_exit_is_in_the_grid(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    ctx, _ = _ctx(tmp_path, FakeLlm({}), _with_random_exit(world_mansion))
    grid = grid_from_coords(ctx, dict(GRID))
    assert grid.random_exits == [RandomExit(room="hall", exit="swirl", to=["study", "library"])]


def test_removing_a_room_an_existing_random_exit_picks_is_reported(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    ctx, _ = _ctx(tmp_path, FakeLlm({}), _with_random_exit(world_mansion))
    turn = AreaTurn.model_validate_json(_load("turn_remove_study.json"))
    problems = turn_problems(ctx, dict(GRID), turn)
    assert any("swirl" in p and "study" in p for p in problems), problems


def test_a_refine_turn_hears_answers_older_than_six_messages_and_keeps_its_prefix(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load("turn_add_ghost.json")] * 2})
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    history = [Message("user", "the library is haunted")]
    history += [Message("assistant" if i % 2 else "user", f"turn {i}") for i in range(1, 9)]
    area_turn(ctx, dict(GRID), "add a ghost", history)
    longer = [*history, Message("user", "add a ghost"), Message("assistant", "1 change")]
    area_turn(ctx, dict(GRID), "and another", longer)
    first, second = ([m.content for m in llm.calls[i][1] if m.role == "user"][-1] for i in (0, 1))
    assert "user: the library is haunted" in second
    # Rules, rooms and mobs come first, then the history, which only grows at its end.
    assert second.startswith(first[: first.index("\n\nUser's message:")])
    assert second.index("(level 3)") < second.index("Conversation so far")


def test_build_of_a_planned_edit_sends_no_conversation(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    llm = FakeLlm(
        {"logic": [_load("turn_add_cellar.json")], "prose": [_load("prose_wine_cellar.json")]}
    )
    ctx, _ = _ctx(tmp_path, llm, world_mansion)
    brief = AreaBrief.model_validate_json(
        read_fixture(
            Path(__file__).resolve().parent / "fixtures" / "llm" / "area_mansion", "brief.json"
        )
    ).model_copy(update={"decisions": ["the cellar is under the kitchen"]})
    build_from_plan(ctx, brief, dict(GRID))
    prompt = [m.content for m in llm.calls[0][1] if m.role == "user"][-1]
    assert "Conversation so far (you, then the user):\nnone" in prompt
    assert "Decisions:\n  - the cellar is under the kitchen" in prompt
