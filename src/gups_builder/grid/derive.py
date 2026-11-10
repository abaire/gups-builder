"""Deriving exits from a grid and validating a planned grid.

Each connection yields its two exits with the door on both sides; a portal
yields its two named exits, deriving the return name from a standard one.
``validate`` returns every problem it finds, each a full sentence naming the
rooms, so the model can repair its plan.
"""

from __future__ import annotations

from collections import Counter, deque
from collections.abc import Mapping

from .model import Coord, DerivedExit, Grid

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
        a, b = by_id.get(conn.a), by_id.get(conn.b)
        if a is None or b is None or a.coord is None or b.coord is None:
            continue
        d_ab = direction_between(a.coord, b.coord)
        if d_ab is None:
            continue
        exits.append(DerivedExit(room=conn.a, direction=d_ab, to=conn.b, door=conn.door))
        exits.append(DerivedExit(room=conn.b, direction=OPPOSITES[d_ab], to=conn.a, door=conn.door))
    for portal in grid.portals:
        a, b = by_id.get(portal.a), by_id.get(portal.b)
        if a is None or b is None:
            continue
        exits.append(
            DerivedExit(room=portal.a, direction=portal.a_exit, to=portal.b, door=portal.door)
        )
        if portal.b_exit is not None:
            exits.append(
                DerivedExit(room=portal.b, direction=portal.b_exit, to=portal.a, door=portal.door)
            )
        elif portal.a_exit in OPPOSITES:
            exits.append(
                DerivedExit(
                    room=portal.b,
                    direction=OPPOSITES[portal.a_exit],
                    to=portal.a,
                    door=portal.door,
                )
            )
    return exits


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
    problems: list[str] = []
    by_id = {room.id: room for room in grid.rooms}

    seen: dict[Coord, str] = {}
    for room in grid.rooms:
        if room.external or room.coord is None:
            continue
        if room.coord in seen:
            problems.append(
                f"'{seen[room.coord]}' and '{room.id}' share the coordinate "
                + _coord_text(room.coord)
            )
        else:
            seen[room.coord] = room.id

    if entry not in by_id:
        problems.append(f"the entry room '{entry}' is not in the plan")

    for conn in grid.connections:
        a, b = by_id.get(conn.a), by_id.get(conn.b)
        if a is None or b is None:
            problems.extend(_unknown_ends("connection", conn.a, conn.b, by_id))
            continue
        if (
            a.coord is not None
            and b.coord is not None
            and direction_between(a.coord, b.coord) is None
        ):
            problems.append(
                f"'{conn.a}' {_coord_text(a.coord)} and '{conn.b}' {_coord_text(b.coord)} "
                "are not neighbours; use a portal"
            )
    for portal in grid.portals:
        problems.extend(_unknown_ends("portal", portal.a, portal.b, by_id))

    exits = derive_exits(grid)
    usage: Counter[tuple[str, str]] = Counter((e.room, e.direction) for e in exits)
    for (room_id, exit_name), count in usage.items():
        if count > 1:
            problems.append(f"'{room_id}' uses the exit name '{exit_name}' twice")

    adjacency: dict[str, list[str]] = {}
    for e in exits:
        adjacency.setdefault(e.room, []).append(e.to)
        adjacency.setdefault(e.to, []).append(e.room)
    if entry in by_id:
        reachable = {entry}
        queue = deque([entry])
        while queue:
            current = queue.popleft()
            for neighbour in adjacency.get(current, []):
                if neighbour in reachable or neighbour not in by_id or by_id[neighbour].external:
                    continue
                reachable.add(neighbour)
                queue.append(neighbour)
        for room in grid.rooms:
            if not room.external and room.id not in reachable:
                problems.append(f"'{room.id}' is not reachable from '{entry}'")

    if room_range is not None:
        count = sum(1 for room in grid.rooms if not room.external)
        if not room_range[0] <= count <= room_range[1]:
            lo, hi = room_range
            problems.append(f"the plan has {count} rooms; the request calls for {lo} to {hi}")

    reported: set[tuple[str, str]] = set()
    for e in exits:
        if e.door is None:
            continue
        pair = (e.room, e.to) if e.room < e.to else (e.to, e.room)
        if pair in reported:
            continue
        reported.add(pair)
        if e.door.state == "locked" and e.door.key is None:
            problems.append(f"the locked door between '{e.room}' and '{e.to}' has no key")
        elif e.door.key is not None and e.door.key not in known_items:
            problems.append(
                f"the door between '{e.room}' and '{e.to}' has the key '{e.door.key}', "
                "which is not an item of the plan"
            )

    # A room without a coordinate is off the map, so only portals may reach it:
    # a connection needs both coordinates to derive a direction.
    for room in grid.rooms:
        if room.external or room.coord is not None:
            continue
        if any(room.id in (conn.a, conn.b) for conn in grid.connections):
            problems.append(
                f"'{room.id}' has no coordinate but a connection joins it; give it a coordinate "
                "or reach it only by portals"
            )

    for portal in grid.portals:
        if portal.b_exit is None and portal.a_exit not in OPPOSITES:
            problems.append(
                f"the portal from '{portal.a}' has the non-standard exit '{portal.a_exit}' "
                "and no return exit"
            )

    return problems
