"""Placing existing rooms on the grid, breadth-first through their exits.

Starting from the saved coordinates (or the first room by label at the
origin), each standard-direction exit to a room of the package places its
target one step away. A room the exits place on two different coordinates,
or a room they never reach, is returned off-map.
"""

from __future__ import annotations

from gups_builder.engine.world import RoomInfo

from .derive import DIRECTION_DELTAS
from .model import Coord


def room_id(label: str) -> str:
    return label.rsplit(":", 1)[1]


def place_existing(
    rooms: list[RoomInfo], package: str, known: dict[str, Coord]
) -> tuple[dict[str, Coord], list[str]]:
    by_id = {room_id(room.label): room for room in rooms}
    coords: dict[str, Coord] = {rid: c for rid, c in known.items() if rid in by_id}
    if not by_id:
        return {}, []
    if not coords:
        first = min(by_id, key=lambda rid: by_id[rid].label)
        coords[first] = (0, 0, 0)
    taken = set(coords.values())
    progress = True
    while progress:
        progress = False
        for rid in by_id:
            if rid in coords:
                continue
            candidates: set[Coord] = set()
            for other_rid, other in by_id.items():
                if other_rid not in coords:
                    continue
                for exit in other.exits:
                    if exit.to != f"//{package}:{rid}" or exit.dir not in DIRECTION_DELTAS:
                        continue
                    origin = coords[other_rid]
                    delta = DIRECTION_DELTAS[exit.dir]
                    candidates.add(
                        (origin[0] + delta[0], origin[1] + delta[1], origin[2] + delta[2])
                    )
            if len(candidates) == 1:
                candidate = next(iter(candidates))
                if candidate not in taken:
                    coords[rid] = candidate
                    taken.add(candidate)
                    progress = True
    off_map = sorted(rid for rid in by_id if rid not in coords)
    return coords, off_map
