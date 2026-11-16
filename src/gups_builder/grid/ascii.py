"""The ASCII map of one level of the grid.

Each room is a 3-character cell ``[A]`` with a letter code in id order, one
block of eight columns per grid column: the cell, then a five-column gap. The
first four gap columns hold the room's markers, ``^`` for an up exit, ``v``
for down, ``*`` for a portal and ``?`` for a random exit, each in its own
column so none hides another.
``-`` fills the rest of the gap between east-west neighbours, so the last gap
column always shows the link. ``|`` joins north-south neighbours under the
cell's centre, and ``/`` and ``\\`` the diagonals in the row between, with an
``X`` where both cross. North is at the top; a legend below names every cell.
"""

from __future__ import annotations

import string
from typing import TYPE_CHECKING

from gups_builder.grid.derive import derive_exits

if TYPE_CHECKING:
    from gups_builder.grid.model import DerivedExit, Grid

_BLOCK = 8  # cell (3) + marker columns (4) + a connector column that markers never use
_MARKERS = {"up": (3, "^"), "down": (4, "v")}
_PORTAL_COLUMN = 5
_RANDOM_COLUMN = 6
_DIAGONAL_COLUMN = 5
_CODES = string.ascii_uppercase + string.ascii_lowercase + string.digits


def _code(i: int) -> str:
    return _CODES[i] if i < len(_CODES) else "?"


def _room_markers(grid: Grid, exits: list[DerivedExit]) -> dict[str, list[tuple[int, str]]]:
    """Each room's markers, as (column offset in its block, mark)."""
    markers: dict[str, list[tuple[int, str]]] = {}
    for e in exits:
        if e.direction in _MARKERS:
            markers.setdefault(e.room, []).append(_MARKERS[e.direction])
    for end in (end for p in grid.portals for end in (p.a, p.b)):
        markers.setdefault(end, []).append((_PORTAL_COLUMN, "*"))
    for random_exit in grid.random_exits:
        markers.setdefault(random_exit.room, []).append((_RANDOM_COLUMN, "?"))
    return markers


def render_map(grid: Grid, z: int, names: dict[str, str]) -> str:
    placed = sorted(
        (
            (r.id, r.coord)
            for r in grid.rooms
            if not r.external and r.coord is not None and r.coord[2] == z
        ),
        key=lambda p: p[0],
    )
    if not placed:
        return "no rooms on this level"
    min_x = min(coord[0] for _, coord in placed)
    max_x = max(coord[0] for _, coord in placed)
    min_y = min(coord[1] for _, coord in placed)
    max_y = max(coord[1] for _, coord in placed)
    rows = 2 * (max_y - min_y) + 1
    cols = _BLOCK * (max_x - min_x + 1)
    cells: list[list[str]] = [[" "] * cols for _ in range(rows)]
    by_id = dict(placed)
    letters = {rid: _code(i) for i, (rid, _) in enumerate(placed)}

    def row_of(y: int) -> int:
        return 2 * (max_y - y)

    def col_of(x: int) -> int:
        return _BLOCK * (x - min_x)

    for rid, coord in placed:
        r, c = row_of(coord[1]), col_of(coord[0])
        cells[r][c : c + 3] = list(f"[{letters[rid]}]")

    exits = derive_exits(grid)
    markers = _room_markers(grid, exits)

    # Markers first, so the east-west connector fills only the gap columns they leave.
    for rid, coord in placed:
        r, c = row_of(coord[1]), col_of(coord[0])
        for offset, mark in markers.get(rid, []):
            cells[r][c + offset] = mark

    diagonals: dict[tuple[int, int], set[str]] = {}
    for e in exits:
        a, b = by_id.get(e.room), by_id.get(e.to)
        if a is None or b is None or b[2] != z:
            continue
        dx, dy = b[0] - a[0], b[1] - a[1]
        if (dx, dy) == (1, 0):
            r, c = row_of(a[1]), col_of(a[0])
            for gap in range(c + 3, c + _BLOCK):
                if cells[r][gap] == " ":
                    cells[r][gap] = "-"
        elif (dx, dy) == (0, 1):
            cells[row_of(a[1]) - 1][col_of(a[0]) + 1] = "|"
        elif abs(dx) == 1 and abs(dy) == 1:
            slot = (row_of(max(a[1], b[1])) + 1, col_of(min(a[0], b[0])) + _DIAGONAL_COLUMN)
            diagonals.setdefault(slot, set()).add("/" if dx == dy else "\\")
    for (r, c), marks in diagonals.items():
        cells[r][c] = "X" if len(marks) > 1 else next(iter(marks))

    map_lines = ["".join(line).rstrip() for line in cells]
    legend = [
        f"{letters[rid]} {rid}" + (f": {names[rid]}" if names.get(rid) else "") for rid, _ in placed
    ]
    return "\n".join(map_lines + legend)
