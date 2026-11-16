"""Laying a planned room graph out on the grid from each passage's direction."""

from __future__ import annotations

from gups_builder.grid.derive import validate
from gups_builder.grid.layout import lay_out, spread_directions
from gups_builder.grid.model import Connection, DoorSpec, Grid, GridRoom, Passage, Portal


def _grid(ids: list[str], passages: list[Passage], entry: str) -> Grid:
    layout = lay_out(ids, passages, entry)
    return Grid(
        rooms=[GridRoom(id=rid, coord=layout.coords[rid]) for rid in ids],
        connections=layout.connections,
        portals=layout.portals,
    )


def test_passages_place_each_room_one_step_away_from_the_entry() -> None:
    passages = [
        Passage(a="hall", b="kitchen", direction="east"),
        Passage(a="hall", b="study", direction="north"),
        Passage(a="kitchen", b="cellar", direction="down"),
        Passage(a="attic", b="hall", direction="up"),
    ]
    layout = lay_out(["hall", "kitchen", "study", "cellar", "attic"], passages, "hall")
    assert layout.coords == {
        "hall": (0, 0, 0),
        "kitchen": (1, 0, 0),
        "study": (0, 1, 0),
        "cellar": (1, 0, -1),
        "attic": (0, 0, -1),
    }
    assert layout.connections == [
        Connection(a="hall", b="kitchen"),
        Connection(a="hall", b="study"),
        Connection(a="kitchen", b="cellar"),
        Connection(a="attic", b="hall"),
    ]
    assert layout.portals == []


def test_a_loop_that_closes_on_the_grid_stays_a_connection() -> None:
    passages = [
        Passage(a="a", b="b", direction="east"),
        Passage(a="b", b="c", direction="north"),
        Passage(a="c", b="d", direction="west"),
        Passage(a="d", b="a", direction="south"),
    ]
    layout = lay_out(["a", "b", "c", "d"], passages, "a")
    assert layout.coords["d"] == (0, 1, 0)
    assert len(layout.connections) == 4
    assert layout.portals == []


def test_a_passage_the_grid_cannot_honour_becomes_a_portal_named_by_its_direction() -> None:
    # Going east twice and then west once from the far room cannot land back on 'b'.
    door = DoorSpec(state="closed")
    passages = [
        Passage(a="a", b="b", direction="east"),
        Passage(a="b", b="c", direction="east"),
        Passage(a="c", b="a", direction="west", door=door),
    ]
    layout = lay_out(["a", "b", "c"], passages, "a")
    assert layout.coords == {"a": (0, 0, 0), "b": (1, 0, 0), "c": (2, 0, 0)}
    assert layout.connections == [Connection(a="a", b="b"), Connection(a="b", b="c")]
    assert layout.portals == [Portal(a="c", a_exit="west", b="a", b_exit="east", door=door)]


def test_a_room_whose_cell_is_taken_goes_to_the_nearest_free_cell() -> None:
    passages = [
        Passage(a="hall", b="kitchen", direction="east"),
        Passage(a="hall", b="pantry", direction="east"),
    ]
    layout = lay_out(["hall", "kitchen", "pantry"], passages, "hall")
    assert layout.coords["kitchen"] == (1, 0, 0)
    assert layout.coords["pantry"] not in {(0, 0, 0), (1, 0, 0)}
    assert layout.portals == [Portal(a="hall", a_exit="east", b="pantry", b_exit="west")]
    # Two passages east from one room are the model's mistake, reported in its own terms.
    problems = validate(
        _grid(["hall", "kitchen", "pantry"], passages, "hall"),
        entry="hall",
        room_range=None,
        known_items=set(),
    )
    assert (
        "'hall' uses the exit name 'east' for both 'kitchen' and 'pantry'; give one of them "
        "another direction or exit name"
    ) in problems


def test_rooms_no_passage_reaches_still_get_free_cells() -> None:
    passages = [Passage(a="hall", b="study", direction="north")]
    layout = lay_out(["hall", "study", "rift", "void"], passages, "hall")
    assert len(set(layout.coords.values())) == 4
    assert layout.coords["hall"] == (0, 0, 0)


def test_an_island_of_rooms_keeps_its_own_directions() -> None:
    passages = [
        Passage(a="hall", b="study", direction="north"),
        Passage(a="maze_1", b="maze_2", direction="east"),
    ]
    layout = lay_out(["hall", "study", "maze_1", "maze_2"], passages, "hall")
    x, y, z = layout.coords["maze_1"]
    assert layout.coords["maze_2"] == (x + 1, y, z)
    assert Connection(a="maze_1", b="maze_2") in layout.connections


def test_a_passage_naming_an_unknown_room_is_kept_for_validation_to_report() -> None:
    passages = [Passage(a="hall", b="ghost", direction="north")]
    grid = _grid(["hall"], passages, "hall")
    assert grid.connections == [Connection(a="hall", b="ghost")]
    problems = validate(grid, entry="hall", room_range=None, known_items=set())
    assert (
        "the connection from 'hall' names the unknown room 'ghost'; use a room id of the plan"
        in problems
    )


def test_an_unknown_entry_lays_out_from_the_first_room() -> None:
    passages = [Passage(a="hall", b="study", direction="north")]
    layout = lay_out(["hall", "study"], passages, "foyer")
    assert layout.coords == {"hall": (0, 0, 0), "study": (0, 1, 0)}


def test_a_second_passage_the_same_way_turns_to_the_nearest_free_direction() -> None:
    # Both ends count: the foyer is west of the hall, so the study cannot be west of it too.
    passages = [
        Passage(a="foyer", b="hall", direction="east"),
        Passage(a="hall", b="study", direction="west"),
        Passage(a="hall", b="pantry", direction="west"),
    ]
    spread = spread_directions(passages, taken=[])
    assert [p.direction for p in spread] == ["east", "northwest", "southwest"]


def test_spreading_leaves_room_for_exits_already_taken() -> None:
    passages = [Passage(a="hall", b="study", direction="north")]
    spread = spread_directions(passages, taken=[("hall", "north"), ("hall", "northeast")])
    assert spread[0].direction == "northwest"


def test_a_vertical_clash_is_left_for_validation() -> None:
    passages = [
        Passage(a="hall", b="attic", direction="up"),
        Passage(a="hall", b="loft", direction="up"),
    ]
    assert spread_directions(passages, taken=[]) == passages
