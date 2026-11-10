"""The area grid: directions, derived exits, validation, placement and the ASCII map."""

from __future__ import annotations

import pytest

from gups_builder.engine.world import ExitInfo, RoomInfo, WorldDescription
from gups_builder.grid.ascii import render_map
from gups_builder.grid.derive import derive_exits, direction_between, validate
from gups_builder.grid.model import Connection, DoorSpec, Grid, GridRoom, Portal
from gups_builder.grid.place import place_existing


def _exit(d: str, to: str) -> ExitInfo:
    return ExitInfo(dir=d, to=to)


def test_all_directions() -> None:
    assert direction_between((0, 0, 0), (0, 1, 0)) == "north"
    assert direction_between((0, 0, 0), (0, -1, 0)) == "south"
    assert direction_between((0, 0, 0), (1, 0, 0)) == "east"
    assert direction_between((0, 0, 0), (-1, 0, 0)) == "west"
    assert direction_between((0, 0, 0), (1, 1, 0)) == "northeast"
    assert direction_between((0, 0, 0), (-1, 1, 0)) == "northwest"
    assert direction_between((0, 0, 0), (1, -1, 0)) == "southeast"
    assert direction_between((0, 0, 0), (-1, -1, 0)) == "southwest"
    assert direction_between((0, 0, 0), (0, 0, 1)) == "up"
    assert direction_between((0, 0, 0), (0, 0, -1)) == "down"


def test_non_adjacent_pairs_have_no_direction() -> None:
    assert direction_between((0, 0, 0), (0, 0, 0)) is None
    assert direction_between((0, 0, 0), (0, 2, 0)) is None
    assert direction_between((0, 0, 0), (1, 1, 1)) is None
    assert direction_between((0, 0, 0), (2, 1, 0)) is None


def test_connection_yields_paired_exits_with_the_door_on_both_sides() -> None:
    grid = Grid(
        rooms=[GridRoom(id="hall", coord=(0, 0, 0)), GridRoom(id="armory", coord=(1, 0, 0))],
        connections=[
            Connection(a="hall", b="armory", door=DoorSpec(state="locked", key="brass_key"))
        ],
    )
    exits = derive_exits(grid)
    assert len(exits) == 2
    assert (exits[0].room, exits[0].direction, exits[0].to) == ("hall", "east", "armory")
    assert (exits[1].room, exits[1].direction, exits[1].to) == ("armory", "west", "hall")
    assert exits[0].door == exits[1].door
    assert exits[0].door is not None
    assert exits[0].door.state == "locked"


def test_derive_exit_order_is_connections_then_portals() -> None:
    grid = Grid(
        rooms=[
            GridRoom(id="hall", coord=(0, 0, 0)),
            GridRoom(id="gate", coord=(1, 0, 0)),
            GridRoom(id="attic", coord=(0, 1, 1)),
        ],
        connections=[Connection(a="hall", b="gate")],
        portals=[Portal(a="hall", a_exit="up", b="attic")],
    )
    exits = derive_exits(grid)
    assert [(e.room, e.direction, e.to) for e in exits] == [
        ("hall", "east", "gate"),
        ("gate", "west", "hall"),
        ("hall", "up", "attic"),
        ("attic", "down", "hall"),
    ]


def test_portal_without_b_exit_uses_the_opposite_of_a_standard_a_exit() -> None:
    grid = Grid(
        rooms=[GridRoom(id="hall", coord=(0, 0, 0)), GridRoom(id="attic", coord=None)],
        connections=[],
        portals=[Portal(a="hall", a_exit="north", b="attic")],
    )
    exits = derive_exits(grid)
    assert [(e.room, e.direction, e.to) for e in exits] == [
        ("hall", "north", "attic"),
        ("attic", "south", "hall"),
    ]


def test_validate_is_clean_for_a_valid_3x3_mansion_with_a_cellar() -> None:
    rooms = [
        GridRoom(id=f"r{i}", coord=(x, y, 0))
        for i, (x, y) in enumerate(
            [(0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (2, 1), (0, 2), (1, 2), (2, 2)]
        )
    ]
    rooms.append(GridRoom(id="cellar", coord=(1, 1, -1)))
    connections = [
        Connection(a="r0", b="r1"),
        Connection(a="r1", b="r2"),
        Connection(a="r3", b="r4"),
        Connection(a="r4", b="r5"),
        Connection(a="r6", b="r7"),
        Connection(a="r7", b="r8"),
        Connection(a="r0", b="r3"),
        Connection(a="r3", b="r6"),
        Connection(a="r1", b="r4"),
        Connection(a="r4", b="r7"),
        Connection(a="r2", b="r5"),
        Connection(a="r5", b="r8"),
        Connection(a="r4", b="cellar"),
    ]
    grid = Grid(rooms=rooms, connections=connections, portals=[])
    assert validate(grid, entry="r0", room_range=(8, 12), known_items=set()) == []


def test_validate_reports_two_rooms_on_one_coordinate() -> None:
    grid = Grid(
        rooms=[GridRoom(id="hall", coord=(0, 0, 0)), GridRoom(id="attic", coord=(0, 0, 0))],
        connections=[],
        portals=[],
    )
    problems = validate(grid, entry="hall", room_range=None, known_items=set())
    assert "'hall' and 'attic' share the coordinate (0,0,0)" in problems


def test_validate_reports_a_non_adjacent_connection() -> None:
    grid = Grid(
        rooms=[GridRoom(id="hall", coord=(0, 0, 0)), GridRoom(id="attic", coord=(0, 2, 1))],
        connections=[Connection(a="hall", b="attic")],
        portals=[],
    )
    problems = validate(grid, entry="hall", room_range=None, known_items=set())
    assert "'hall' (0,0,0) and 'attic' (0,2,1) are not neighbours; use a portal" in problems


def test_validate_reports_a_room_using_the_same_exit_name_twice() -> None:
    grid = Grid(
        rooms=[
            GridRoom(id="hall", coord=(0, 0, 0)),
            GridRoom(id="a", coord=(1, 0, 0)),
            GridRoom(id="b", coord=(0, 1, 0)),
        ],
        connections=[Connection(a="hall", b="a"), Connection(a="hall", b="b")],
        portals=[Portal(a="hall", a_exit="north", b="a")],
    )
    problems = validate(grid, entry="hall", room_range=None, known_items=set())
    assert "'hall' uses the exit name 'north' twice" in problems


def test_validate_reports_a_connection_naming_an_unknown_room() -> None:
    grid = Grid(
        rooms=[GridRoom(id="hall", coord=(0, 0, 0))],
        connections=[Connection(a="hall", b="ghost")],
        portals=[],
    )
    problems = validate(grid, entry="hall", room_range=None, known_items=set())
    assert "the connection from 'hall' names the unknown room 'ghost'" in problems


def test_validate_reports_a_portal_naming_an_unknown_room() -> None:
    grid = Grid(
        rooms=[GridRoom(id="hall", coord=(0, 0, 0))],
        connections=[],
        portals=[Portal(a="hall", a_exit="escape", b="ghost")],
    )
    problems = validate(grid, entry="hall", room_range=None, known_items=set())
    assert "the portal from 'hall' names the unknown room 'ghost'" in problems


def test_validate_reports_an_unreachable_room() -> None:
    grid = Grid(
        rooms=[
            GridRoom(id="hall", coord=(0, 0, 0)),
            GridRoom(id="gate", coord=(1, 0, 0)),
            GridRoom(id="cellar", coord=(0, 0, 1)),
        ],
        connections=[Connection(a="hall", b="gate")],
        portals=[],
    )
    problems = validate(grid, entry="hall", room_range=None, known_items=set())
    assert "'cellar' is not reachable from 'hall'" in problems


def test_validate_counts_doors_as_passable_for_reachability() -> None:
    grid = Grid(
        rooms=[GridRoom(id="hall", coord=(0, 0, 0)), GridRoom(id="armory", coord=(1, 0, 0))],
        connections=[Connection(a="hall", b="armory", door=DoorSpec(state="locked", key="key"))],
        portals=[],
    )
    assert validate(grid, entry="hall", room_range=None, known_items={"key"}) == []


def test_validate_reports_the_room_count_when_out_of_range() -> None:
    grid = Grid(
        rooms=[GridRoom(id="hall", coord=(0, 0, 0)), GridRoom(id="gate", coord=(1, 0, 0))],
        connections=[Connection(a="hall", b="gate")],
        portals=[],
    )
    problems = validate(grid, entry="hall", room_range=(4, 8), known_items=set())
    assert "the plan has 2 rooms; the request calls for 4 to 8" in problems


def test_validate_reports_a_locked_door_without_a_key() -> None:
    grid = Grid(
        rooms=[GridRoom(id="hall", coord=(0, 0, 0)), GridRoom(id="armory", coord=(1, 0, 0))],
        connections=[Connection(a="hall", b="armory", door=DoorSpec(state="locked"))],
        portals=[],
    )
    problems = validate(grid, entry="hall", room_range=None, known_items={"brass_key"})
    assert "the locked door between 'hall' and 'armory' has no key" in problems


def test_validate_reports_a_key_that_is_not_an_item_of_the_plan() -> None:
    grid = Grid(
        rooms=[GridRoom(id="hall", coord=(0, 0, 0)), GridRoom(id="armory", coord=(1, 0, 0))],
        connections=[Connection(a="hall", b="armory", door=DoorSpec(state="closed", key="ghost"))],
        portals=[],
    )
    problems = validate(grid, entry="hall", room_range=None, known_items={"brass_key"})
    assert (
        "the door between 'hall' and 'armory' has the key 'ghost', which is not an item of the plan"
        in problems
    )


def test_validate_reports_a_portal_without_a_return_exit_when_a_exit_is_not_standard() -> None:
    grid = Grid(
        rooms=[GridRoom(id="hall", coord=(0, 0, 0)), GridRoom(id="attic", coord=None)],
        connections=[],
        portals=[Portal(a="hall", a_exit="escape", b="attic")],
    )
    problems = validate(grid, entry="hall", room_range=None, known_items=set())
    assert (
        "the portal from 'hall' has the non-standard exit 'escape' and no return exit" in problems
    )


def test_validate_accepts_a_coordinateless_room_reached_only_by_portals() -> None:
    grid = Grid(
        rooms=[GridRoom(id="hall", coord=(0, 0, 0)), GridRoom(id="attic", coord=None)],
        connections=[],
        portals=[Portal(a="hall", a_exit="north", b="attic")],
    )
    assert validate(grid, entry="hall", room_range=None, known_items=set()) == []


def test_validate_reports_a_coordinateless_room_joined_by_a_connection() -> None:
    grid = Grid(
        rooms=[GridRoom(id="hall", coord=(0, 0, 0)), GridRoom(id="attic", coord=None)],
        connections=[Connection(a="hall", b="attic")],
        portals=[],
    )
    problems = validate(grid, entry="hall", room_range=None, known_items=set())
    assert (
        "'attic' has no coordinate but a connection joins it; give it a coordinate "
        "or reach it only by portals"
    ) in problems


def test_validate_names_the_missing_end_of_a_connection() -> None:
    grid = Grid(
        rooms=[GridRoom(id="hall", coord=(0, 0, 0))],
        connections=[Connection(a="ghost", b="hall")],
        portals=[Portal(a="ghost", a_exit="escape", b="hall", b_exit="back")],
    )
    problems = validate(grid, entry="hall", room_range=None, known_items=set())
    assert "the connection to 'hall' names the unknown room 'ghost'" in problems
    assert "the portal to 'hall' names the unknown room 'ghost'" in problems
    assert not any("unknown room 'hall'" in problem for problem in problems)


def test_validate_reports_an_entry_room_that_is_not_in_the_plan() -> None:
    grid = Grid(rooms=[GridRoom(id="hall", coord=(0, 0, 0))], connections=[], portals=[])
    problems = validate(grid, entry="foyer", room_range=None, known_items=set())
    assert problems == ["the entry room 'foyer' is not in the plan"]


def test_place_existing_tutorial_rooms_from_describe_world(world_min: WorldDescription) -> None:
    coords, off_map = place_existing(world_min.rooms_in("zones/tutorial"), "zones/tutorial", {})
    assert coords == {"gate": (0, 0, 0), "hall": (-1, 0, 0)}
    assert off_map == []


def test_place_existing_of_no_rooms_is_empty() -> None:
    assert place_existing([], "zones/empty", {}) == ({}, [])


def test_place_existing_tutorial_rooms_start_from_the_first_label() -> None:
    rooms = [
        RoomInfo(
            label="//zones/tutorial:hall",
            name="The Hall",
            exits=[_exit("east", "//zones/tutorial:gate")],
        ),
        RoomInfo(
            label="//zones/tutorial:gate",
            name="The Gate",
            exits=[_exit("west", "//zones/tutorial:hall")],
        ),
    ]
    coords, off_map = place_existing(rooms, "zones/tutorial", {})
    assert coords == {"gate": (0, 0, 0), "hall": (-1, 0, 0)}
    assert off_map == []


def test_place_existing_honours_known_coordinates() -> None:
    rooms = [
        RoomInfo(
            label="//zones/tutorial:hall",
            name="The Hall",
            exits=[_exit("east", "//zones/tutorial:gate")],
        ),
        RoomInfo(
            label="//zones/tutorial:gate",
            name="The Gate",
            exits=[_exit("west", "//zones/tutorial:hall")],
        ),
    ]
    coords, off_map = place_existing(rooms, "zones/tutorial", {"hall": (5, 0, 0)})
    assert coords == {"hall": (5, 0, 0), "gate": (6, 0, 0)}
    assert off_map == []


def test_place_existing_leaves_a_room_off_map_on_contradictory_exits() -> None:
    rooms = [
        RoomInfo(
            label="//pkg:w",
            name="w",
            exits=[_exit("east", "//pkg:x"), _exit("north", "//pkg:y")],
        ),
        RoomInfo(
            label="//pkg:x",
            name="x",
            exits=[_exit("west", "//pkg:w"), _exit("north", "//pkg:y")],
        ),
        RoomInfo(label="//pkg:y", name="y", exits=[]),
    ]
    coords, off_map = place_existing(rooms, "pkg", {})
    assert coords == {"w": (0, 0, 0), "x": (1, 0, 0)}
    assert off_map == ["y"]


def test_place_existing_ignores_exits_to_other_packages() -> None:
    rooms = [
        RoomInfo(
            label="//pkg:hall",
            name="Hall",
            exits=[_exit("east", "//other:elsewhere")],
        )
    ]
    coords, off_map = place_existing(rooms, "pkg", {})
    assert coords == {"hall": (0, 0, 0)}
    assert off_map == []


def test_render_map_of_a_crossing_diagonal_grid() -> None:
    # a-b runs northeast, c-d runs southwest: the two diagonals cross.
    grid = Grid(
        rooms=[
            GridRoom(id="a", coord=(0, 0, 0)),
            GridRoom(id="b", coord=(1, 1, 0)),
            GridRoom(id="c", coord=(0, 1, 0)),
            GridRoom(id="d", coord=(1, 0, 0)),
        ],
        connections=[
            Connection(a="a", b="b"),
            Connection(a="c", b="d"),
            Connection(a="a", b="d"),
            Connection(a="c", b="b"),
        ],
        portals=[],
    )
    names = {"a": "A Room", "b": "B Room", "c": "C Room", "d": "D Room"}
    assert render_map(grid, 0, names) == (
        "[C]----[B]\n     X\n[A]----[D]\nA a: A Room\nB b: B Room\nC c: C Room\nD d: D Room"
    )


def test_render_map_of_a_single_diagonal() -> None:
    grid = Grid(
        rooms=[GridRoom(id="a", coord=(0, 0, 0)), GridRoom(id="b", coord=(1, 1, 0))],
        connections=[Connection(a="a", b="b")],
        portals=[],
    )
    assert render_map(grid, 0, {}) == "       [B]\n     /\n[A]\nA a\nB b"


def test_render_map_draws_connections_and_letters_in_id_order() -> None:
    grid = Grid(
        rooms=[
            GridRoom(id="b", coord=(0, 1, 0)),
            GridRoom(id="a", coord=(0, 0, 0)),
        ],
        connections=[Connection(a="a", b="b")],
        portals=[],
    )
    names = {"a": "A Room", "b": "B Room"}
    assert render_map(grid, 0, names) == ("[B]\n |\n[A]\nA a: A Room\nB b: B Room")


def test_render_map_marks_down_and_portals() -> None:
    grid = Grid(
        rooms=[
            GridRoom(id="hall", coord=(0, 0, 0)),
            GridRoom(id="cellar", coord=(0, 0, -1)),
            GridRoom(id="attic", coord=None),
        ],
        connections=[Connection(a="hall", b="cellar")],
        portals=[Portal(a="hall", a_exit="north", b="attic")],
    )
    names = {"hall": "Hall"}
    assert render_map(grid, 0, names) == "[A] v*\nA hall: Hall"


def test_render_map_marks_up_down_and_portal_together_and_keeps_the_connector() -> None:
    grid = Grid(
        rooms=[
            GridRoom(id="hall", coord=(0, 0, 0)),
            GridRoom(id="kitchen", coord=(1, 0, 0)),
            GridRoom(id="attic", coord=(0, 0, 1)),
            GridRoom(id="cellar", coord=(0, 0, -1)),
            GridRoom(id="vault", coord=None),
        ],
        connections=[
            Connection(a="hall", b="kitchen"),
            Connection(a="hall", b="attic"),
            Connection(a="hall", b="cellar"),
        ],
        portals=[Portal(a="hall", a_exit="north", b="vault")],
    )
    assert render_map(grid, 0, {}) == "[A]^v*-[B]\nA hall\nB kitchen"


def test_render_map_letters_continue_past_z() -> None:
    grid = Grid(
        rooms=[GridRoom(id=f"r{i:02}", coord=(i, 0, 0)) for i in range(28)],
        connections=[],
        portals=[],
    )
    legend = render_map(grid, 0, {}).splitlines()[1:]
    assert legend[25] == "Z r25"
    assert legend[26] == "a r26"
    assert legend[27] == "b r27"


def test_render_map_of_an_empty_level() -> None:
    grid = Grid(rooms=[GridRoom(id="hall", coord=(0, 0, 1))], connections=[], portals=[])
    assert render_map(grid, 0, {}) == "no rooms on this level"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
