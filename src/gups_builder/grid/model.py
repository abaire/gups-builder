"""Grid types: rooms on integer coordinates, connections, portals and derived exits."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field

Coord = tuple[int, int, int]

Direction = Literal[
    "north",
    "south",
    "east",
    "west",
    "northeast",
    "northwest",
    "southeast",
    "southwest",
    "up",
    "down",
]


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


class Passage(BaseModel):
    """Two rooms of a planned graph joined in a direction; the builder places them from it."""

    a: str
    b: str
    direction: Direction = Field(description="the way from a to b: b lies this way from a")
    door: DoorSpec | None = None


class Portal(BaseModel):
    a: str
    a_exit: str
    b: str
    b_exit: str | None = None
    door: DoorSpec | None = None
    one_way: bool = False  # only a -> b; b_exit must then be None


class RandomExit(BaseModel):
    """An exit that sends whoever takes it to one of ``to``, picked anew each crossing."""

    room: str
    exit: str
    to: list[str]


class Grid(BaseModel):
    rooms: list[GridRoom] = []
    connections: list[Connection] = []
    portals: list[Portal] = []
    random_exits: list[RandomExit] = []


@dataclass(frozen=True)
class DerivedExit:
    room: str
    direction: str
    to: str
    door: DoorSpec | None
    # The engine pairs an exit with its way back only by opposite directions; any other
    # exit must be declared one_way, or lint fails it for having no reverse.
    one_way: bool = False
    # Every room a random exit may send the player to; ``to`` is then the first.
    candidates: tuple[str, ...] = ()

    @property
    def destinations(self) -> tuple[str, ...]:
        return self.candidates or (self.to,)
