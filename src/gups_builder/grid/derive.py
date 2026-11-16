"""Deriving exits from a grid and validating a planned grid.

Each connection yields its two exits with the door on both sides; a portal
yields its two named exits, deriving the return name from a standard one.
``validate`` returns every problem it finds, each a full sentence naming the
rooms and saying what to change, so the model can repair its plan.
``snake_case_exits`` and ``name_clashing_exits`` settle what the builder can fix
alone: a custom exit name the engine would reject, and two that clash in one room.
"""

from __future__ import annotations

import re
from collections import Counter, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING

from gups_builder.grid.model import (
    Connection,
    Coord,
    DerivedExit,
    DoorSpec,
    Grid,
    GridRoom,
    Portal,
    RandomExit,
)

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

DIRECTION_DELTAS: dict[str, Coord] = {
    "north": (0, 1, 0),
    "south": (0, -1, 0),
    "east": (1, 0, 0),
    "west": (-1, 0, 0),
    "northeast": (1, 1, 0),
    "northwest": (-1, 1, 0),
    "southeast": (1, -1, 0),
    "southwest": (-1, -1, 0),
    "up": (0, 0, 1),
    "down": (0, 0, -1),
}

_DELTAS = {delta: name for name, delta in DIRECTION_DELTAS.items()}

OPPOSITES = {
    "north": "south",
    "south": "north",
    "east": "west",
    "west": "east",
    "northeast": "southwest",
    "northwest": "southeast",
    "southeast": "northwest",
    "southwest": "northeast",
    "up": "down",
    "down": "up",
}


_USE_AN_ID = "; use a room id of the plan"


def direction_between(a: Coord, b: Coord) -> str | None:
    """The direction name when the rooms are adjacent; None otherwise."""
    return _DELTAS.get((b[0] - a[0], b[1] - a[1], b[2] - a[2]))


def derive_exits(grid: Grid) -> list[DerivedExit]:
    """Every exit the plan produces, in the order of connections then portals."""
    by_id = {room.id: room for room in grid.rooms}
    exits: list[DerivedExit] = []
    for conn in grid.connections:
        exits.extend(_connection_exits(conn, by_id))
    for portal in grid.portals:
        exits.extend(_portal_exits(portal, by_id))
    for random_exit in grid.random_exits:
        exits.extend(_random_exit(random_exit, by_id))
    return exits


def _connection_exits(conn: Connection, by_id: Mapping[str, GridRoom]) -> list[DerivedExit]:
    """Both exits of a connection, or none when either end is absent or off the map."""
    a = by_id.get(conn.a)
    b = by_id.get(conn.b)
    if a is None:
        return []
    if b is None:
        return []
    if a.coord is None:
        return []
    if b.coord is None:
        return []
    direction = direction_between(a.coord, b.coord)
    if direction is None:
        return []
    return [
        DerivedExit(room=conn.a, direction=direction, to=conn.b, door=conn.door),
        DerivedExit(room=conn.b, direction=OPPOSITES[direction], to=conn.a, door=conn.door),
    ]


def _portal_exits(portal: Portal, by_id: Mapping[str, GridRoom]) -> list[DerivedExit]:
    """Both exits of a portal, or none when either end is unknown."""
    if portal.a not in by_id:
        return []
    if portal.b not in by_id:
        return []
    if portal.one_way:
        return [DerivedExit(portal.a, portal.a_exit, portal.b, portal.door, one_way=True)]
    direction = _portal_return_direction(portal)
    one_way = OPPOSITES.get(portal.a_exit) != direction
    exits = [DerivedExit(portal.a, portal.a_exit, portal.b, portal.door, one_way)]
    if direction is not None:
        exits.append(DerivedExit(portal.b, direction, portal.a, portal.door, one_way))
    return exits


def _random_exit(random_exit: RandomExit, by_id: Mapping[str, GridRoom]) -> list[DerivedExit]:
    """The exit, toward the candidates the plan has; none when it has none of them."""
    candidates = tuple(dict.fromkeys(to for to in random_exit.to if to in by_id))
    if random_exit.room not in by_id or not candidates:
        return []
    return [
        DerivedExit(
            random_exit.room, random_exit.exit, candidates[0], None, True, candidates=candidates
        )
    ]


def _portal_return_direction(portal: Portal) -> str | None:
    """The return direction of a portal, or None when the plan fixes no b_exit."""
    if portal.b_exit is not None:
        return portal.b_exit
    return OPPOSITES.get(portal.a_exit)


def _coord_text(coord: Coord) -> str:
    return f"({coord[0]},{coord[1]},{coord[2]})"


def _unknown_ends(what: str, a: str, b: str, by_id: Mapping[str, object]) -> list[str]:
    """One sentence per end of a connection or portal that names no room of the plan."""
    problems = []
    if b not in by_id:
        problems.append(f"the {what} from '{a}' names the unknown room '{b}'{_USE_AN_ID}")
    if a not in by_id and a != b:
        problems.append(f"the {what} to '{b}' names the unknown room '{a}'{_USE_AN_ID}")
    return problems


def validate(
    grid: Grid, entry: str, room_range: tuple[int, int] | None, known_items: set[str]
) -> list[str]:
    """Every problem in the plan, each a full sentence the model can act on."""
    by_id = {room.id: room for room in grid.rooms}
    exits = derive_exits(grid)
    broken = _broken_links(grid, by_id)
    problems = list(broken)
    if entry not in by_id:
        problems.append(f"the entry room '{entry}' is not in the plan{_USE_AN_ID}")
    problems.extend(_duplicate_exit_names(exits))
    problems.extend(_invalid_exit_names(exits))
    # A broken link drops its exits, and every room past it would be reported as cut off
    # too: dozens of lines that all go away with the one fix the model is already asked for.
    if not broken:
        problems.extend(_unjoined_rooms(grid, exits, entry, by_id))
    problems.extend(_room_count(grid, room_range))
    problems.extend(_door_problems(exits, known_items))
    problems.extend(_unmapped_rooms(grid))
    problems.extend(_portals_without_return(grid))
    problems.extend(_one_way_returns(grid))
    for random_exit in grid.random_exits:
        problems.extend(_random_exit_problems(random_exit, by_id))
    return problems


def _broken_links(grid: Grid, by_id: Mapping[str, GridRoom]) -> list[str]:
    """Rooms on one cell, and connections or portals that cannot yield their exits."""
    problems = _shared_coordinates(grid)
    problems.extend(_bad_connections(grid, by_id))
    for portal in grid.portals:
        problems.extend(_unknown_ends("portal", portal.a, portal.b, by_id))
    return problems


def _unjoined_rooms(
    grid: Grid, exits: list[DerivedExit], entry: str, by_id: Mapping[str, GridRoom]
) -> list[str]:
    """Rooms the entry cannot reach, then reached rooms with no way out.

    A room joined to nothing is reported once, as unreachable, since one passage to it
    settles both.
    """
    cut_off = _unreachable_rooms(grid, exits, entry, by_id)
    unreachable = [
        f"'{rid}' is not reachable from '{entry}'; add a connection or portal that leads to it"
        for rid in cut_off
    ]
    return unreachable + _rooms_without_exits(grid, exits, set(cut_off))


def _shared_coordinates(grid: Grid) -> list[str]:
    problems: list[str] = []
    seen: dict[Coord, str] = {}
    for room in grid.rooms:
        if room.external:
            continue
        if room.coord is None:
            continue
        if room.coord in seen:
            problems.append(
                f"'{seen[room.coord]}' and '{room.id}' share the coordinate "
                f"{_coord_text(room.coord)}; move one of them to a free coordinate"
            )
            continue
        seen[room.coord] = room.id
    return problems


def _bad_connections(grid: Grid, by_id: Mapping[str, GridRoom]) -> list[str]:
    problems: list[str] = []
    for conn in grid.connections:
        if conn.a not in by_id:
            problems.extend(_unknown_ends("connection", conn.a, conn.b, by_id))
            continue
        if conn.b not in by_id:
            problems.extend(_unknown_ends("connection", conn.a, conn.b, by_id))
            continue
        a = by_id[conn.a]
        b = by_id[conn.b]
        if a.coord is None:
            continue
        if b.coord is None:
            continue
        if direction_between(a.coord, b.coord) is None:
            problems.append(
                f"'{conn.a}' {_coord_text(a.coord)} and '{conn.b}' {_coord_text(b.coord)} "
                "are not neighbours; use a portal"
            )
    return problems


def _target_text(e: DerivedExit) -> str:
    if e.candidates:
        return "a random one of " + ", ".join(f"'{to}'" for to in e.candidates)
    return f"'{e.to}'"


def _duplicate_exit_names(exits: list[DerivedExit]) -> list[str]:
    targets: dict[tuple[str, str], list[str]] = {}
    for e in exits:
        targets.setdefault((e.room, e.direction), []).append(_target_text(e))
    return [
        f"'{room_id}' uses the exit name '{exit_name}' for both "
        + " and ".join(tos)
        + "; give one of them another direction or exit name"
        for (room_id, exit_name), tos in targets.items()
        if len(tos) > 1
    ]


# The engine's own rule for an exit's direction or custom name.
EXIT_NAME = re.compile(r"[a-z][a-z0-9_]*")


def _invalid_exit_names(exits: list[DerivedExit]) -> list[str]:
    bad = dict.fromkeys(
        (e.room, e.direction) for e in exits if not EXIT_NAME.fullmatch(e.direction)
    )
    return [
        f"'{room_id}' has the exit name '{name}', which is not a valid exit name; "
        "use snake_case starting with a letter ([a-z][a-z0-9_]*)"
        for room_id, name in bad
    ]


def _unreachable_rooms(
    grid: Grid, exits: list[DerivedExit], entry: str, by_id: Mapping[str, GridRoom]
) -> list[str]:
    if entry not in by_id:
        return []
    # Exits are followed in their own direction, so a room that only a one-way exit
    # leaves is not reached through it.
    neighbours: dict[str, list[str]] = {}
    for e in exits:
        neighbours.setdefault(e.room, []).extend(e.destinations)
    reached = _walk_from(entry, neighbours, by_id)
    return [room.id for room in grid.rooms if _unreached(reached, room)]


def _walk_from(
    entry: str, neighbours: dict[str, list[str]], by_id: Mapping[str, GridRoom]
) -> set[str]:
    """Every room the entry reaches, counting each door as passable."""
    reached: set[str] = {entry}
    queue = deque([entry])
    while queue:
        for neighbour in neighbours.get(queue.popleft(), []):
            if _blocked(reached, by_id, neighbour):
                continue
            reached.add(neighbour)
            queue.append(neighbour)
    return reached


def _blocked(reached: set[str], by_id: Mapping[str, GridRoom], neighbour: str) -> bool:
    if neighbour in reached:
        return True
    if neighbour not in by_id:
        return True
    return by_id[neighbour].external


def _unreached(reached: set[str], room: GridRoom) -> bool:
    """A room of the plan the walk never reached; external labels are not walked."""
    if room.external:
        return False
    return room.id not in reached


def _room_count(grid: Grid, room_range: tuple[int, int] | None) -> list[str]:
    if room_range is None:
        return []
    count = sum(1 for room in grid.rooms if not room.external)
    lo, hi = room_range
    if _in_range(count, lo, hi):
        return []
    change = f"add {lo - count}" if count < lo else f"remove {count - hi}"
    return [f"the plan has {count} rooms; the request calls for {lo} to {hi}, so {change}"]


def _in_range(count: int, lo: int, hi: int) -> bool:
    if count < lo:
        return False
    return count <= hi


def _door_problems(exits: list[DerivedExit], known_items: set[str]) -> list[str]:
    problems: list[str] = []
    reported: set[tuple[str, str]] = set()
    for e in exits:
        if e.door is None:
            continue
        pair = (e.room, e.to) if e.room < e.to else (e.to, e.room)
        if pair in reported:
            continue
        reported.add(pair)
        if _locked_without_key(e.door):
            problems.append(
                f"the locked door between '{e.room}' and '{e.to}' has no key; "
                "name its key and plan the key"
            )
            continue
        if _key_not_in_plan(e.door, known_items):
            problems.append(
                f"the door between '{e.room}' and '{e.to}' has the key '{e.door.key}', "
                "which is not an item of the plan; plan the key or name a planned one"
            )
    return problems


def _locked_without_key(door: DoorSpec) -> bool:
    if door.state != "locked":
        return False
    return door.key is None


def _key_not_in_plan(door: DoorSpec, known_items: set[str]) -> bool:
    if door.key is None:
        return False
    return door.key not in known_items


# A room without a coordinate is off the map, so only portals may reach it:
# a connection needs both coordinates to derive a direction.
def _unmapped_rooms(grid: Grid) -> list[str]:
    problems: list[str] = []
    for room in grid.rooms:
        if room.external:
            continue
        if room.coord is not None:
            continue
        if _joined_by_connection(room.id, grid):
            problems.append(
                f"'{room.id}' has no coordinate but a connection joins it; give it a coordinate "
                "or reach it only by portals"
            )
    return problems


def _joined_by_connection(room_id: str, grid: Grid) -> bool:
    return any(room_id in (conn.a, conn.b) for conn in grid.connections)


def _portals_without_return(grid: Grid) -> list[str]:
    return [
        f"the portal from '{portal.a}' has the non-standard exit '{portal.a_exit}' "
        "and no return exit; set b_exit, or make the portal one_way"
        for portal in grid.portals
        if _missing_return(portal)
    ]


def _missing_return(portal: Portal) -> bool:
    if portal.one_way or portal.b_exit is not None:
        return False
    return portal.a_exit not in OPPOSITES


def _one_way_returns(grid: Grid) -> list[str]:
    return [
        f"the one-way portal from '{portal.a}' to '{portal.b}' has the return exit "
        f"'{portal.b_exit}'; drop b_exit, or make the portal two-way"
        for portal in grid.portals
        if portal.one_way and portal.b_exit is not None
    ]


# The engine fails a room with no exits at all; one-way exits make that possible.
def _rooms_without_exits(grid: Grid, exits: list[DerivedExit], skip: set[str]) -> list[str]:
    exiting = {e.room for e in exits} | skip
    return [
        f"'{room.id}' has no exits; add a way out of it"
        for room in grid.rooms
        if not room.external and room.id not in exiting
    ]


def _random_exit_problems(random_exit: RandomExit, by_id: Mapping[str, GridRoom]) -> list[str]:
    what = f"the random exit '{random_exit.exit}' from '{random_exit.room}'"
    problems = []
    if len(set(random_exit.to)) < 2:
        problems.append(
            f"{what} has fewer than two distinct rooms to pick from; list at least two, "
            "or use a portal"
        )
    if random_exit.exit in OPPOSITES:
        # A standard direction promises the room next door, which a pick is not.
        problems.append(
            f"{what} uses a standard direction; give it a name of its own, like trick_door"
        )
    problems.extend(
        f"{what} names the unknown room '{name}'{_USE_AN_ID}"
        for name in dict.fromkeys([random_exit.room, *random_exit.to])
        if name not in by_id
    )
    return problems


# -- clashing exit names -----------------------------------------------------------


ExitKey = tuple[str, str]


def name_clashing_exits(grid: Grid, held: Iterable[ExitKey] = ()) -> Grid:
    """The grid with each clashing custom portal exit named after the room it leads to.

    Two portals named 'trick door' in one room are one mistake the builder can fix on
    its own, and a small model asked to fix it tends to send the same plan back. Only
    custom names change: a standard direction also fixes the return exit, so a clash
    there is left for validation to report. ``held`` names exits outside the grid, such
    as a link's, that keep their name, so a portal clashing with one gives way.
    """
    usage: Counter[ExitKey] = Counter((e.room, e.direction) for e in derive_exits(grid))
    usage.update(held)
    names = _ExitNames(usage, {key for key in usage if usage[key] == 1})
    portals = [_renamed_portal(portal, names) for portal in grid.portals]
    if portals == grid.portals:
        return grid
    return grid.model_copy(update={"portals": portals})


def _renamed_portal(portal: Portal, names: _ExitNames) -> Portal:
    a_exit = names.distinct(portal.a, portal.a_exit, portal.b)
    b_exit = portal.b_exit
    if b_exit is not None:
        b_exit = names.distinct(portal.b, b_exit, portal.a)
    if (a_exit, b_exit) == (portal.a_exit, portal.b_exit):
        return portal
    return portal.model_copy(update={"a_exit": a_exit, "b_exit": b_exit})


@dataclass
class _ExitNames:
    """How often each exit name is used in its room, and the names already settled."""

    usage: Counter[ExitKey]
    taken: set[ExitKey]

    def distinct(self, room: str, name: str, to: str) -> str:
        """``name`` when it is unique in ``room``; otherwise one that says where it leads."""
        if name in OPPOSITES or self.usage[(room, name)] < 2:
            return name
        base = f"{name}_to_{to}"
        candidate, number = base, 2
        while (room, candidate) in self.taken:
            candidate, number = f"{base}_{number}", number + 1
        self.taken.add((room, candidate))
        return candidate


def snake_case(name: str) -> str:
    """``name`` as an exit name the engine accepts, when it has letters or digits to keep.

    Models write exit names as players would say them ('One-way Trick Door'); the
    engine wants 'one_way_trick_door'. A name that starts with a digit stays invalid,
    for validation to report, since no spelling of it is obviously right.
    """
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or name


def snake_case_exits(grid: Grid) -> Grid:
    """The grid with every portal and random exit name in the engine's snake_case."""
    portals = [_snake_case_portal(portal) for portal in grid.portals]
    random_exits = [r.model_copy(update={"exit": snake_case(r.exit)}) for r in grid.random_exits]
    if (portals, random_exits) == (grid.portals, grid.random_exits):
        return grid
    return grid.model_copy(update={"portals": portals, "random_exits": random_exits})


def _snake_case_portal(portal: Portal) -> Portal:
    b_exit = None if portal.b_exit is None else snake_case(portal.b_exit)
    a_exit = snake_case(portal.a_exit)
    if (a_exit, b_exit) == (portal.a_exit, portal.b_exit):
        return portal
    return portal.model_copy(update={"a_exit": a_exit, "b_exit": b_exit})
