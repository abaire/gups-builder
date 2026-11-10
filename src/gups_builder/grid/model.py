"""Grid types: rooms on integer coordinates, connections, portals and derived exits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel

Coord = tuple[int, int, int]


class DoorSpec(BaseModel):
    state: Literal["open", "closed", "locked"] = "closed"
    key: str | None = None  # an item id in the target package, or a full label
    tags: list[str] = []


class GridRoom(BaseModel):
    id: str
    coord: Coord | None
    external: bool = False  # a label outside the package, shown for reference only


class Connection(BaseModel):
    a: str
    b: str
    door: DoorSpec | None = None


class Portal(BaseModel):
    a: str
    a_exit: str
    b: str
    b_exit: str | None = None
    door: DoorSpec | None = None


class Grid(BaseModel):
    rooms: list[GridRoom] = []
    connections: list[Connection] = []
    portals: list[Portal] = []


@dataclass(frozen=True)
class DerivedExit:
    room: str
    direction: str
    to: str
    door: DoorSpec | None
