"""Exit names and exit kinds on the grid: renaming, snake_case, one-way and random exits."""

from __future__ import annotations

from gups_builder.grid.derive import (
    derive_exits,
    name_clashing_exits,
    snake_case_exits,
    validate,
)
from gups_builder.grid.model import Connection, Grid, GridRoom, Portal, RandomExit


def _maze(*portals: Portal) -> Grid:
    return Grid(
        rooms=[
            GridRoom(id="maze_1", coord=(0, 0, 0)),
            GridRoom(id="maze_2", coord=(0, 1, 0)),
            GridRoom(id="kitchen", coord=(5, 5, 0)),
        ],
        connections=[Connection(a="maze_1", b="maze_2")],
        portals=list(portals),
    )


def test_clashing_portal_exits_are_told_apart_by_where_they_lead() -> None:
    grid = _maze(
        Portal(a="maze_2", a_exit="trick_door", b="maze_1", b_exit="hidden_door"),
        Portal(a="maze_2", a_exit="trick_door", b="kitchen", b_exit="hidden_door"),
    )
    named = name_clashing_exits(grid)
    assert [p.a_exit for p in named.portals] == ["trick_door_to_maze_1", "trick_door_to_kitchen"]
    assert [p.b_exit for p in named.portals] == ["hidden_door", "hidden_door"]
    assert validate(named, entry="maze_1", room_range=None, known_items=set()) == []


def test_clashing_return_exits_are_renamed_too() -> None:
    grid = _maze(
        Portal(a="maze_2", a_exit="crack", b="kitchen", b_exit="trapdoor"),
        Portal(a="maze_1", a_exit="crevice", b="kitchen", b_exit="trapdoor"),
    )
    named = name_clashing_exits(grid)
    assert [p.b_exit for p in named.portals] == ["trapdoor_to_maze_2", "trapdoor_to_maze_1"]


def test_clashing_exits_to_one_room_are_numbered() -> None:
    grid = _maze(
        Portal(a="maze_2", a_exit="trick_door", b="kitchen", b_exit="back"),
        Portal(a="maze_2", a_exit="trick_door", b="kitchen", b_exit="way_back"),
    )
    named = name_clashing_exits(grid)
    assert [p.a_exit for p in named.portals] == ["trick_door_to_kitchen", "trick_door_to_kitchen_2"]
    assert validate(named, entry="maze_1", room_range=None, known_items=set()) == []


def test_a_standard_direction_is_never_renamed() -> None:
    # Renaming 'north' would also lose the return exit derived from it; the model must
    # choose, and validation tells it so.
    grid = _maze(Portal(a="maze_1", a_exit="north", b="kitchen"))
    assert name_clashing_exits(grid) == grid


def test_a_name_held_elsewhere_makes_the_portal_give_way() -> None:
    grid = _maze(Portal(a="maze_1", a_exit="gate", b="kitchen", b_exit="back"))
    named = name_clashing_exits(grid, held={("maze_1", "gate")})
    assert named.portals[0].a_exit == "gate_to_kitchen"


def test_a_plan_without_clashes_is_unchanged() -> None:
    grid = _maze(Portal(a="maze_2", a_exit="trick_door", b="kitchen", b_exit="back"))
    assert name_clashing_exits(grid) == grid


def test_custom_exit_names_become_snake_case() -> None:
    grid = _maze(
        Portal(a="maze_2", a_exit="One-way Trick Door", b="kitchen", b_exit="hidden  passage!"),
        Portal(a="maze_1", a_exit="north", b="kitchen"),
    )
    named = snake_case_exits(grid)
    assert [(p.a_exit, p.b_exit) for p in named.portals] == [
        ("one_way_trick_door", "hidden_passage"),
        ("north", None),
    ]


def test_validate_reports_an_exit_name_the_engine_rejects() -> None:
    grid = _maze(Portal(a="maze_2", a_exit="2nd_door", b="kitchen", b_exit="back"))
    problems = validate(grid, entry="maze_1", room_range=None, known_items=set())
    assert problems == [
        "'maze_2' has the exit name '2nd_door', which is not a valid exit name; "
        "use snake_case starting with a letter ([a-z][a-z0-9_]*)"
    ]


def test_a_portal_is_one_way_unless_its_names_are_opposite_directions() -> None:
    # The engine pairs an exit with its reverse only by opposite directions, so any
    # other portal is two exits each declared one_way.
    grid = _maze(
        Portal(a="maze_1", a_exit="west", b="kitchen"),
        Portal(a="maze_2", a_exit="trapdoor", b="kitchen", b_exit="ladder"),
        Portal(a="maze_2", a_exit="east", b="kitchen", b_exit="hatch"),
    )
    portal_exits = derive_exits(grid)[2:]
    assert [(e.room, e.direction, e.one_way) for e in portal_exits] == [
        ("maze_1", "west", False),
        ("kitchen", "east", False),
        ("maze_2", "trapdoor", True),
        ("kitchen", "ladder", True),
        ("maze_2", "east", True),
        ("kitchen", "hatch", True),
    ]
    assert not any(e.one_way for e in derive_exits(grid)[:2])


def _with_exits(
    *, portals: list[Portal] | None = None, random_exits: list[RandomExit] | None = None
) -> Grid:
    return Grid(
        rooms=[
            GridRoom(id="maze_1", coord=(0, 0, 0)),
            GridRoom(id="maze_2", coord=(0, 1, 0)),
            GridRoom(id="kitchen", coord=(5, 5, 0)),
            GridRoom(id="rift", coord=(9, 9, 0)),
        ],
        connections=[Connection(a="maze_1", b="maze_2")],
        portals=portals or [],
        random_exits=random_exits or [],
    )


def _problems(grid: Grid) -> list[str]:
    return validate(grid, entry="maze_1", room_range=None, known_items=set())


def test_a_one_way_portal_has_no_way_back() -> None:
    grid = _with_exits(
        portals=[
            Portal(a="maze_2", a_exit="chute", b="kitchen", one_way=True),
            Portal(a="kitchen", a_exit="ladder", b="rift", b_exit="ladder"),
        ]
    )
    exits = [(e.room, e.direction, e.to, e.one_way) for e in derive_exits(grid)[2:]]
    assert exits[0] == ("maze_2", "chute", "kitchen", True)
    assert ("kitchen", "chute", "maze_2", True) not in exits
    assert _problems(grid) == []


def test_a_one_way_portal_with_a_return_exit_is_reported() -> None:
    grid = _with_exits(
        portals=[
            Portal(a="maze_2", a_exit="chute", b="kitchen", b_exit="ladder", one_way=True),
            Portal(a="kitchen", a_exit="stair", b="rift", b_exit="stair"),
        ]
    )
    assert _problems(grid) == [
        "the one-way portal from 'maze_2' to 'kitchen' has the return exit 'ladder'; "
        "drop b_exit, or make the portal two-way"
    ]


def test_reachability_follows_the_direction_of_one_way_exits() -> None:
    # The rift leads to the entry, but nothing leads to the rift.
    grid = _with_exits(
        portals=[
            Portal(a="rift", a_exit="chute", b="maze_1", one_way=True),
            Portal(a="maze_1", a_exit="stair", b="kitchen", b_exit="stair"),
        ]
    )
    assert _problems(grid) == [
        "'rift' is not reachable from 'maze_1'; add a connection or portal that leads to it"
    ]


def test_a_room_with_no_way_out_is_reported() -> None:
    grid = _with_exits(
        portals=[
            Portal(a="maze_1", a_exit="chute", b="rift", one_way=True),
            Portal(a="maze_1", a_exit="stair", b="kitchen", b_exit="stair"),
        ]
    )
    assert _problems(grid) == ["'rift' has no exits; add a way out of it"]


def test_a_random_exit_leads_to_each_of_its_rooms() -> None:
    grid = _with_exits(
        random_exits=[RandomExit(room="maze_2", exit="trick_door", to=["maze_1", "kitchen"])],
        portals=[Portal(a="kitchen", a_exit="ladder", b="rift", b_exit="ladder")],
    )
    trick = [e for e in derive_exits(grid) if e.direction == "trick_door"]
    assert [(e.room, e.candidates, e.one_way) for e in trick] == [
        ("maze_2", ("maze_1", "kitchen"), True)
    ]
    assert _problems(grid) == []


def test_random_exit_problems_are_reported() -> None:
    grid = _with_exits(
        random_exits=[
            RandomExit(room="maze_2", exit="trick_door", to=["kitchen", "kitchen"]),
            RandomExit(room="maze_1", exit="west", to=["kitchen", "rift"]),
            RandomExit(room="maze_1", exit="crack", to=["rift", "ghost"]),
        ],
        portals=[Portal(a="kitchen", a_exit="ladder", b="rift", b_exit="ladder")],
    )
    assert _problems(grid) == [
        "the random exit 'trick_door' from 'maze_2' has fewer than two distinct rooms to pick "
        "from; list at least two, or use a portal",
        "the random exit 'west' from 'maze_1' uses a standard direction; give it a name of its "
        "own, like trick_door",
        "the random exit 'crack' from 'maze_1' names the unknown room 'ghost'; use a room id of "
        "the plan",
    ]


def test_a_portal_gives_way_to_a_random_exits_name() -> None:
    grid = _with_exits(
        random_exits=[RandomExit(room="maze_2", exit="trick_door", to=["maze_1", "kitchen"])],
        portals=[Portal(a="maze_2", a_exit="trick_door", b="rift", b_exit="back")],
    )
    named = name_clashing_exits(grid)
    assert named.portals[0].a_exit == "trick_door_to_rift"
    assert named.random_exits == grid.random_exits


def test_random_exit_names_become_snake_case() -> None:
    grid = _with_exits(random_exits=[RandomExit(room="maze_2", exit="Trick Door", to=["a", "b"])])
    assert snake_case_exits(grid).random_exits[0].exit == "trick_door"
