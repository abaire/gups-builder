"""Deriving exits from a grid and validating a planned grid.

Each connection yields its two exits with the door on both sides; a portal
yields its two named exits, deriving the return name from a standard one.
``validate`` returns every problem it finds, each a full sentence naming the
rooms, so the model can repair its plan.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Mapping

from .model import Connection, Coord, DerivedExit, DoorSpec, Grid, GridRoom, Portal

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
    exits = [DerivedExit(room=portal.a, direction=portal.a_exit, to=portal.b, door=portal.door)]
    direction = _portal_return_direction(portal)
    if direction is not None:
        exits.append(DerivedExit(room=portal.b, direction=direction, to=portal.a, door=portal.door))
    return exits


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
        problems.append(f"the {what} from '{a}' names the unknown room '{b}'")
    if a not in by_id and a != b:
        problems.append(f"the {what} to '{b}' names the unknown room '{a}'")
    return problems


def validate(
    grid: Grid, entry: str, room_range: tuple[int, int] | None, known_items: set[str]
) -> list[str]:
    """Every problem in the plan, each a full sentence the model can act on."""
    by_id = {room.id: room for room in grid.rooms}
    exits = derive_exits(grid)
    problems = _shared_coordinates(grid)
    if entry not in by_id:
        problems.append(f"the entry room '{entry}' is not in the plan")
    problems.extend(_bad_connections(grid, by_id))
    for portal in grid.portals:
        problems.extend(_unknown_ends("portal", portal.a, portal.b, by_id))
    problems.extend(_duplicate_exit_names(exits))
    problems.extend(_unreachable_rooms(grid, exits, entry, by_id))
    problems.extend(_room_count(grid, room_range))
    problems.extend(_door_problems(exits, known_items))
    problems.extend(_unmapped_rooms(grid))
    problems.extend(_portals_without_return(grid))
    return problems


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
                + _coord_text(room.coord)
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


def _duplicate_exit_names(exits: list[DerivedExit]) -> list[str]:
    usage: Counter[tuple[str, str]] = Counter((e.room, e.direction) for e in exits)
    return [
        f"'{room_id}' uses the exit name '{exit_name}' twice"
        for (room_id, exit_name), count in usage.items()
        if count > 1
    ]


def _unreachable_rooms(
    grid: Grid, exits: list[DerivedExit], entry: str, by_id: Mapping[str, GridRoom]
) -> list[str]:
    if entry not in by_id:
        return []
    neighbours: dict[str, list[str]] = {}
    for e in exits:
        neighbours.setdefault(e.room, []).append(e.to)
        neighbours.setdefault(e.to, []).append(e.room)
    reached = _walk_from(entry, neighbours, by_id)
    return [
        f"'{room.id}' is not reachable from '{entry}'"
        for room in grid.rooms
        if _unreached(reached, room)
    ]


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
    return [f"the plan has {count} rooms; the request calls for {lo} to {hi}"]


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
            problems.append(f"the locked door between '{e.room}' and '{e.to}' has no key")
            continue
        if _key_not_in_plan(e.door, known_items):
            problems.append(
                f"the door between '{e.room}' and '{e.to}' has the key '{e.door.key}', "
                "which is not an item of the plan"
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
    problems: list[str] = []
    for portal in grid.portals:
        if _missing_return(portal):
            problems.append(
                f"the portal from '{portal.a}' has the non-standard exit '{portal.a_exit}' "
                "and no return exit"
            )
    return problems


def _missing_return(portal: Portal) -> bool:
    if portal.b_exit is not None:
        return False
    return portal.a_exit not in OPPOSITES
