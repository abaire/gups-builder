"""Laying a planned room graph out on the grid from each passage's direction.

A small model asked for coordinates puts rooms on one cell and joins rooms that are
not neighbours; asked only which way each passage runs, it has nothing to get wrong
but the graph. The entry room goes to the origin and each passage places the room it
reaches one step away. A passage whose room is already placed elsewhere, or whose cell
another room holds, keeps its direction as the exit name and becomes a portal: the
player still goes that way, and only the map cannot draw it as a neighbour.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import TYPE_CHECKING

from gups_builder.grid.derive import DIRECTION_DELTAS, OPPOSITES
from gups_builder.grid.model import Connection, Coord, Direction, Passage, Portal

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator


_COMPASS: list[Direction] = [
    "north",
    "northeast",
    "east",
    "southeast",
    "south",
    "southwest",
    "west",
    "northwest",
]


def _alternatives(direction: Direction) -> list[Direction]:
    """The other compass directions, nearest first, alternating clockwise and back."""
    if direction not in _COMPASS:
        return []
    i = _COMPASS.index(direction)
    steps = [1, -1, 2, -2, 3, -3, 4]
    return [_COMPASS[(i + step) % len(_COMPASS)] for step in steps]


def spread_directions(passages: list[Passage], taken: Iterable[tuple[str, str]]) -> list[Passage]:
    """The passages with each one that reuses a direction at either end turned aside.

    Two passages leaving a room the same way are one exit name used twice, which a small
    model asked to fix sends back unchanged; any free direction will do, and the nearest
    one keeps the layout close to what the model meant. ``taken`` holds the exits a room
    already has from portals or a link. Up and down have no near alternative, so a clash
    there is left for validation to report.
    """
    used = set(taken)
    spread: list[Passage] = []
    for passage in passages:
        direction = _free_direction(passage, used)
        used.add((passage.a, direction))
        used.add((passage.b, OPPOSITES[direction]))
        spread.append(
            passage
            if direction == passage.direction
            else passage.model_copy(update={"direction": direction})
        )
    return spread


def _free_direction(passage: Passage, used: set[tuple[str, str]]) -> Direction:
    for direction in [passage.direction, *_alternatives(passage.direction)]:
        if _free_at_both_ends(passage, direction, used):
            return direction
    return passage.direction


def _free_at_both_ends(passage: Passage, direction: str, used: set[tuple[str, str]]) -> bool:
    if (passage.a, direction) in used:
        return False
    return (passage.b, OPPOSITES[direction]) not in used


@dataclass
class Layout:
    coords: dict[str, Coord] = field(default_factory=dict)
    connections: list[Connection] = field(default_factory=list)
    portals: list[Portal] = field(default_factory=list)


def _step(coord: Coord, direction: str) -> Coord:
    delta = DIRECTION_DELTAS[direction]
    return (coord[0] + delta[0], coord[1] + delta[1], coord[2] + delta[2])


@dataclass(frozen=True)
class _Leg:
    """One way through a passage: from ``start`` toward ``end``, going ``direction``."""

    start: str
    end: str
    direction: str


def _legs(passages: list[Passage]) -> list[_Leg]:
    legs: list[_Leg] = []
    for p in passages:
        legs.append(_Leg(p.a, p.b, p.direction))
        legs.append(_Leg(p.b, p.a, OPPOSITES[p.direction]))
    return legs


@dataclass
class _Placer:
    room_ids: list[str]
    legs: list[_Leg]
    coords: dict[str, Coord] = field(default_factory=dict)

    def put(self, rid: str, coord: Coord) -> None:
        self.coords[rid] = coord

    def free(self, coord: Coord) -> bool:
        return coord not in self.coords.values()

    def grow(self) -> None:
        """Place every room a passage reaches from a placed room onto a free cell."""
        progress = True
        while progress:
            progress = False
            for leg in self.legs:
                if self._placeable(leg):
                    self.put(leg.end, _step(self.coords[leg.start], leg.direction))
                    progress = True

    def _placeable(self, leg: _Leg) -> bool:
        if leg.start not in self.coords or leg.end in self.coords:
            return False
        return self.free(_step(self.coords[leg.start], leg.direction))

    def place_one_more(self) -> None:
        """Place the first unplaced room near where a passage wanted it, or off to the east."""
        for leg in self.legs:
            if leg.start in self.coords and leg.end not in self.coords:
                self.put(leg.end, self._nearest_free(_step(self.coords[leg.start], leg.direction)))
                return
        rid = next(rid for rid in self.room_ids if rid not in self.coords)
        east = max(coord[0] for coord in self.coords.values()) + 2
        self.put(rid, self._nearest_free((east, 0, 0)))

    def _nearest_free(self, target: Coord) -> Coord:
        return next(coord for coord in _rings(target) if self.free(coord))

    def unplaced(self) -> bool:
        return len(self.coords) < len(self.room_ids)


def _rings(centre: Coord) -> Iterator[Coord]:
    """Cells on centre's level, ring by ring outward, starting with centre itself."""
    yield centre
    radius = 1
    while True:
        for dx, dy in product(range(-radius, radius + 1), repeat=2):
            if max(abs(dx), abs(dy)) == radius:
                yield (centre[0] + dx, centre[1] + dy, centre[2])
        radius += 1


def _start(room_ids: list[str], entry: str) -> str:
    return entry if entry in room_ids else room_ids[0]


def lay_out(room_ids: list[str], passages: list[Passage], entry: str) -> Layout:
    """Coordinates for every room, and each passage as a connection or a portal.

    A passage naming a room that is not in ``room_ids`` stays a connection, so
    validation reports the unknown room in the words it uses for connections. A
    repeated id is one room here; the caller reports the repeat.
    """
    room_ids = list(dict.fromkeys(room_ids))
    if not room_ids:
        return Layout(connections=[Connection(a=p.a, b=p.b, door=p.door) for p in passages])
    known = set(room_ids)
    placer = _Placer(room_ids, [leg for leg in _legs(passages) if {leg.start, leg.end} <= known])
    placer.put(_start(room_ids, entry), (0, 0, 0))
    placer.grow()
    while placer.unplaced():
        placer.place_one_more()
        placer.grow()
    layout = Layout(coords={rid: placer.coords[rid] for rid in room_ids})
    for passage in passages:
        _add_passage(layout, passage)
    return layout


def _add_passage(layout: Layout, p: Passage) -> None:
    if _on_the_grid(layout.coords, p):
        layout.connections.append(Connection(a=p.a, b=p.b, door=p.door))
        return
    layout.portals.append(
        Portal(a=p.a, a_exit=p.direction, b=p.b, b_exit=OPPOSITES[p.direction], door=p.door)
    )


def _on_the_grid(coords: dict[str, Coord], p: Passage) -> bool:
    """Whether the map shows the passage as it is: b one step from a in its direction."""
    if p.a not in coords or p.b not in coords:
        return True
    return _step(coords[p.a], p.direction) == coords[p.b]
