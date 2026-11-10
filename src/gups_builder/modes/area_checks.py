"""The area mode's plan and turn shapes, and the checks run on them before any prose is spent.

A plan's grid is validated, along with its link to another package, its ids
and its TODOs. A turn's edits are applied to the package's grid and only the
problems they add are reported, alongside what is wrong with each edit on its
own, checked in order so an edit may name a room an earlier edit added.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Annotated, Literal

from pydantic import BaseModel, Field

from gups_builder.engine.world import DoorInfo, ExitInfo, RoomInfo
from gups_builder.grid.derive import DIRECTION_DELTAS, OPPOSITES, validate
from gups_builder.grid.model import Connection, Coord, DoorSpec, Grid, GridRoom, Portal
from gups_builder.grid.place import room_id
from gups_builder.modes.common import Context, Turn, allowed_values, param_allowed


class XYZ(BaseModel):
    x: int
    y: int
    z: int


class RoomCount(BaseModel):
    min: int
    max: int


class AreaBrief(BaseModel):
    title: str
    theme: str
    style_notes: str
    room_count: RoomCount
    room_concepts: list[str]
    link_to: str | None = None
    questions: list[str] = []


class PlannedRoom(BaseModel):
    id: str
    concept: str
    at: XYZ
    size: Literal["cramped", "normal", "large"] = "normal"
    tags: list[str] = []


class PlannedKey(BaseModel):
    id: str
    concept: str


class PlannedLink(BaseModel):
    room: str
    exit: str
    external_label: str
    return_exit: str | None = None


class RoomTodo(BaseModel):
    room: str
    text: str


class AreaPlan(BaseModel):
    rooms: list[PlannedRoom]
    connections: list[Connection]
    portals: list[Portal] = []
    keys: list[PlannedKey] = []
    entry: str
    link: PlannedLink | None = None
    todos: list[RoomTodo] = []


class RoomProse(BaseModel):
    name: str
    desc: str


class ItemProse(BaseModel):
    name: str
    keywords: list[str]
    desc: str


class AddRoom(BaseModel):
    edit: Literal["add_room"] = "add_room"
    id: str
    concept: str
    at: XYZ
    size: Literal["cramped", "normal", "large"] = "normal"
    tags: list[str] = []


class RemoveRoom(BaseModel):
    edit: Literal["remove_room"] = "remove_room"
    id: str


class Connect(BaseModel):
    edit: Literal["connect"] = "connect"
    a: str
    b: str
    door: DoorSpec | None = None


class Disconnect(BaseModel):
    edit: Literal["disconnect"] = "disconnect"
    a: str
    b: str


class SetDoor(BaseModel):
    edit: Literal["set_door"] = "set_door"
    a: str
    b: str
    door: DoorSpec | None = None  # None removes the door


class AddPortal(BaseModel):
    edit: Literal["add_portal"] = "add_portal"
    portal: Portal


class SetRoom(BaseModel):
    edit: Literal["set_room"] = "set_room"
    id: str
    size: str | None = None
    tags: list[str] | None = None
    regions: list[str] | None = None


class Rewrite(BaseModel):
    edit: Literal["rewrite"] = "rewrite"
    ids: list[str]
    guidance: str


class AddKey(BaseModel):
    edit: Literal["add_key"] = "add_key"
    id: str
    concept: str


class AddSpawn(BaseModel):
    edit: Literal["add_spawn"] = "add_spawn"
    room: str
    mob: str
    count: int = 1
    respawn_pulses: int | None = None


class RemoveSpawn(BaseModel):
    edit: Literal["remove_spawn"] = "remove_spawn"
    room: str
    mob: str


class AddTodoEdit(BaseModel):
    edit: Literal["add_todo"] = "add_todo"
    room: str
    text: str


AreaEdit = Annotated[
    AddRoom
    | RemoveRoom
    | Connect
    | Disconnect
    | SetDoor
    | AddPortal
    | SetRoom
    | Rewrite
    | AddKey
    | AddSpawn
    | RemoveSpawn
    | AddTodoEdit,
    Field(discriminator="edit"),
]


class AreaTurn(Turn):
    edits: list[AreaEdit] = []


_ID = re.compile(r"[a-z][a-z0-9_]*")

_DOOR_STATES: dict[str, Literal["open", "closed", "locked"]] = {
    "open": "open",
    "closed": "closed",
    "locked": "locked",
}


# -- labels ----------------------------------------------------------------------


def local_id(label: str, package: str) -> str | None:
    """The id when the label names something in this package; None otherwise."""
    if label.startswith(":"):
        return label[1:]
    prefix = f"//{package}:"
    return label[len(prefix) :] if label.startswith(prefix) else None


def full_label(label: str, package: str) -> str:
    """A label in full: a bare id or ``:id`` is taken to be in this package."""
    if label.startswith("//"):
        return label
    return f"//{package}:{label.removeprefix(':')}"


def _known_items(ctx: Context, new_ids: Iterable[str]) -> set[str]:
    """The items a locked door may name: new ones, the package's, and the world's."""
    known = set(new_ids)
    known.update(d.id for d in ctx.view.decls if d.kind == "item")
    known.update(item.label for item in ctx.world.items)
    return known


# -- the plan --------------------------------------------------------------------


def plan_to_grid(plan: AreaPlan) -> Grid:
    return Grid(
        rooms=[
            GridRoom(id=room.id, coord=(room.at.x, room.at.y, room.at.z)) for room in plan.rooms
        ],
        connections=list(plan.connections),
        portals=list(plan.portals),
    )


def _checked_grid(plan: AreaPlan) -> Grid:
    """The plan's grid, with its link as a portal out of the room it starts from."""
    grid = plan_to_grid(plan)
    link = plan.link
    if link is None or link.room not in {room.id for room in plan.rooms}:
        return grid
    # The link's exit counts among the room's exits, so a clash with a derived one shows.
    # Its return exit is checked by _link_problems, in the words of a link, not a portal.
    grid.portals.append(
        Portal(a=link.room, a_exit=link.exit, b=link.external_label, b_exit=link.exit)
    )
    grid.rooms.append(GridRoom(id=link.external_label, coord=None, external=True))
    return grid


def _decl_id_problems(decl_id: str, seen: set[str], existing: set[str]) -> list[str]:
    problems = (
        []
        if _ID.fullmatch(decl_id)
        else [f"the id '{decl_id}' is not snake_case ([a-z][a-z0-9_]*)"]
    )
    if decl_id in seen:
        problems.append(f"the id '{decl_id}' is used twice in the plan")
    elif decl_id in existing:
        problems.append(f"the id '{decl_id}' is already declared in the package")
    return problems


def _plan_id_problems(ctx: Context, plan: AreaPlan) -> list[str]:
    existing = {d.id for d in ctx.view.decls}
    seen: set[str] = set()
    problems: list[str] = []
    for decl_id in [room.id for room in plan.rooms] + [key.id for key in plan.keys]:
        problems.extend(_decl_id_problems(decl_id, seen, existing))
        seen.add(decl_id)
    return problems


def _todo_problems(plan: AreaPlan) -> list[str]:
    planned = {room.id for room in plan.rooms}
    return [
        f"the todo names the unknown room '{todo.room}'"
        for todo in plan.todos
        if todo.room not in planned
    ]


def plan_problems(ctx: Context, brief: AreaBrief, plan: AreaPlan) -> list[str]:
    """Every problem with the plan, so a bad plan is repaired before any prose is spent."""
    problems = validate(
        _checked_grid(plan),
        entry=plan.entry,
        room_range=(brief.room_count.min, brief.room_count.max),
        known_items=_known_items(ctx, (key.id for key in plan.keys)),
    )
    if plan.link is not None:
        problems.extend(_link_problems(ctx, plan, plan.link))
    problems.extend(_plan_id_problems(ctx, plan))
    problems.extend(_todo_problems(plan))
    return problems


def _external_room_problems(ctx: Context, label: str, external: RoomInfo | None) -> list[str]:
    if external is None:
        return [f"the link's external room '{label}' does not exist"]
    if label.startswith(f"//{ctx.package}:"):
        return [f"the link's external room '{label}' is in this package"]
    return []


def _return_exit_problems(link: PlannedLink, external: RoomInfo | None) -> list[str]:
    return_exit = link.return_exit or OPPOSITES.get(link.exit)
    if return_exit is None:
        return [
            f"the link from '{link.room}' has the non-standard exit '{link.exit}' "
            "and no return exit"
        ]
    if external is None or not any(e.dir == return_exit for e in external.exits):
        return []
    # link_ops are applied to the other package only at /write, so a clash there would
    # surface after the user confirmed; catch it while the model can still choose.
    return [
        f"the link's return exit '{return_exit}' is already an exit of "
        f"'{link.external_label}'; choose another return_exit"
    ]


def _link_problems(ctx: Context, plan: AreaPlan, link: PlannedLink) -> list[str]:
    problems: list[str] = []
    if link.room not in {room.id for room in plan.rooms}:
        problems.append(f"the link starts from the unknown room '{link.room}'")
    label = link.external_label
    external = next((room for room in ctx.world.rooms if room.label == label), None)
    problems.extend(_external_room_problems(ctx, label, external))
    problems.extend(_return_exit_problems(link, external))
    return problems


# -- prose -----------------------------------------------------------------------


def prose_check(what: str) -> Callable[[RoomProse], list[str]]:
    def check(prose: RoomProse) -> list[str]:
        problems: list[str] = []
        if not prose.name.strip():
            problems.append(f"{what}: name is empty")
        if "\n" in prose.name:
            problems.append(f"{what}: name must be one line")
        if len(prose.name) > 60:
            problems.append(f"{what}: name is {len(prose.name)} characters, the limit is 60")
        if not prose.desc.strip():
            problems.append(f"{what}: desc is empty")
        if "\n" in prose.desc:
            problems.append(f"{what}: desc must be one paragraph")
        return problems

    return check


def item_prose_check(what: str) -> Callable[[ItemProse], list[str]]:
    def check(prose: ItemProse) -> list[str]:
        problems = prose_check(what)(RoomProse(name=prose.name, desc=prose.desc))
        if not prose.keywords:
            problems.append(f"{what}: keywords is empty")
        return problems

    return check


# -- the package's grid ----------------------------------------------------------


def _door_spec(door: DoorInfo | None) -> DoorSpec | None:
    if door is None:
        return None
    return DoorSpec(
        state=_DOOR_STATES.get(door.state, "closed"), key=door.key, tags=list(door.tags)
    )


def _passage(package: str, rid: str, exit: ExitInfo) -> Connection | None:
    """A standard-direction exit to another room of the package, as a connection."""
    target = local_id(exit.to, package)
    if exit.dir not in DIRECTION_DELTAS or target is None:
        return None
    return Connection(a=rid, b=target, door=_door_spec(exit.door))


def _passages(package: str, room: RoomInfo) -> list[Connection]:
    rid = room_id(room.label)
    found = (_passage(package, rid, e) for e in room.exits)
    return [c for c in found if c is not None]


def grid_from_coords(ctx: Context, coords: dict[str, Coord]) -> Grid:
    """The package's rooms and their standard-direction passages within it."""
    rooms = ctx.world.rooms_in(ctx.package)
    grid_rooms = [
        GridRoom(id=room_id(room.label), coord=coords.get(room_id(room.label))) for room in rooms
    ]
    connections: list[Connection] = []
    seen: set[frozenset[str]] = set()
    for passage in (c for room in rooms for c in _passages(ctx.package, room)):
        pair = frozenset((passage.a, passage.b))
        if pair not in seen:
            seen.add(pair)
            connections.append(passage)
    return Grid(rooms=grid_rooms, connections=connections, portals=[])


def _remove_from_grid(grid: Grid, rid: str) -> None:
    grid.rooms = [room for room in grid.rooms if room.id != rid]
    grid.connections = [c for c in grid.connections if rid not in (c.a, c.b)]
    grid.portals = [p for p in grid.portals if rid not in (p.a, p.b)]


def _set_passage(grid: Grid, edit: Connect | SetDoor | Disconnect) -> None:
    pair = frozenset((edit.a, edit.b))
    grid.connections = [c for c in grid.connections if frozenset((c.a, c.b)) != pair]
    if not isinstance(edit, Disconnect):
        grid.connections.append(Connection(a=edit.a, b=edit.b, door=edit.door))


def apply_edit_to_grid(grid: Grid, edit: AreaEdit) -> None:
    if isinstance(edit, AddRoom):
        grid.rooms.append(GridRoom(id=edit.id, coord=(edit.at.x, edit.at.y, edit.at.z)))
    elif isinstance(edit, RemoveRoom):
        _remove_from_grid(grid, edit.id)
    elif isinstance(edit, (Connect, SetDoor, Disconnect)):
        _set_passage(grid, edit)
    elif isinstance(edit, AddPortal):
        grid.portals.append(edit.portal)


# -- turns -----------------------------------------------------------------------


def _edit_room_ids(edit: AreaEdit) -> list[str]:
    if isinstance(edit, (AddRoom, RemoveRoom, SetRoom)):
        return [edit.id]
    if isinstance(edit, (Connect, Disconnect, SetDoor)):
        return [edit.a, edit.b]
    if isinstance(edit, AddPortal):
        return [edit.portal.a, edit.portal.b]
    if isinstance(edit, (AddSpawn, RemoveSpawn, AddTodoEdit)):
        return [edit.room]
    if isinstance(edit, Rewrite):
        return list(edit.ids)
    return []


@dataclass
class _TurnScope:
    """What a turn's edits are checked against; edits update it in the order they apply."""

    package: str
    rooms: set[str]
    opaque: set[str]
    items: set[str]
    mobs: set[str]
    passages: set[frozenset[str]]
    sizes: list[str]


def _opaque_rooms(ctx: Context) -> set[str]:
    return {o.id for o in ctx.view.opaque if o.kind == "room" and o.id is not None}


def _package_rooms(ctx: Context) -> set[str]:
    """Every room of the package: the world's, the files', and those the reader can't parse."""
    rooms = {room_id(r.label) for r in ctx.world.rooms_in(ctx.package)}
    return rooms | {d.id for d in ctx.view.decls if d.kind == "room"} | _opaque_rooms(ctx)


def _package_items(ctx: Context) -> set[str]:
    items = {d.id for d in ctx.view.decls if d.kind == "item"}
    world_ids = (local_id(item.label, ctx.package) for item in ctx.world.items)
    return items | {i for i in world_ids if i}


def _room_sizes(ctx: Context) -> list[str]:
    if not param_allowed(ctx.api, ctx.world, "room", "size"):
        return []
    return allowed_values(ctx.api, ctx.world, "room", "size")


def _turn_scope(ctx: Context, coords: dict[str, Coord]) -> _TurnScope:
    return _TurnScope(
        package=ctx.package,
        rooms=_package_rooms(ctx),
        opaque=_opaque_rooms(ctx),
        items=_package_items(ctx),
        mobs={m.label for m in ctx.world.mobs},
        passages={frozenset((c.a, c.b)) for c in grid_from_coords(ctx, coords).connections},
        sizes=_room_sizes(ctx),
    )


def _missing_rooms(scope: _TurnScope, edit: AreaEdit) -> list[str]:
    return [
        f"there is no room '{rid}' in this package"
        for rid in _edit_room_ids(edit)
        if rid not in scope.rooms
    ]


def _add_room_problems(scope: _TurnScope, edit: AddRoom) -> list[str]:
    problems: list[str] = []
    if not _ID.fullmatch(edit.id):
        problems.append(
            f"'{edit.id}' is not a valid room id; use lower-case letters, digits and "
            "underscores, starting with a letter"
        )
    elif edit.id in scope.rooms:
        problems.append(f"room '{edit.id}' already exists; pick another id")
    scope.rooms.add(edit.id)
    return problems


def _remove_room_problems(scope: _TurnScope, edit: RemoveRoom) -> list[str]:
    problems = _missing_rooms(scope, edit)
    scope.rooms.discard(edit.id)
    return problems


def _is_unknown_size(scope: _TurnScope, size: str | None) -> bool:
    """Whether the size is set but not one of the world's sizes (when it lists any)."""
    if size is None or not scope.sizes:
        return False
    return size not in scope.sizes


def _set_room_problems(scope: _TurnScope, edit: SetRoom) -> list[str]:
    problems = _missing_rooms(scope, edit)
    if _is_unknown_size(scope, edit.size):
        problems.append(f"'{edit.size}' is not a room size; use one of: {', '.join(scope.sizes)}")
    return problems


def _spawn_problems(scope: _TurnScope, edit: AddSpawn | RemoveSpawn) -> list[str]:
    problems = _missing_rooms(scope, edit)
    if full_label(edit.mob, scope.package) not in scope.mobs:
        problems.append(f"there is no mob '{edit.mob}'; spawn one of the listed mobs")
    if isinstance(edit, AddSpawn) and edit.count < 1:
        problems.append(f"a spawn of '{edit.mob}' in '{edit.room}' needs a count of 1 or more")
    return problems


def _passage_problems(scope: _TurnScope, edit: Connect | SetDoor | Disconnect) -> list[str]:
    """A door or disconnect needs a passage; connect and disconnect add and take one."""
    pair = frozenset((edit.a, edit.b))
    if isinstance(edit, Connect):
        scope.passages.add(pair)
        return []
    if pair not in scope.passages:
        return [f"there is no passage between '{edit.a}' and '{edit.b}'; use connect to make one"]
    if isinstance(edit, Disconnect):
        scope.passages.discard(pair)
    return []


def _add_key_problems(scope: _TurnScope, edit: AddKey) -> list[str]:
    problems = (
        [f"item '{edit.id}' already exists; pick another id"] if edit.id in scope.items else []
    )
    scope.items.add(edit.id)
    return problems


def _one_edit_problems(scope: _TurnScope, edit: AreaEdit) -> list[str]:
    """One edit's problems; edits that add or remove something update ``scope``."""
    if isinstance(edit, AddRoom):
        return _add_room_problems(scope, edit)
    if isinstance(edit, RemoveRoom):
        return _remove_room_problems(scope, edit)
    if isinstance(edit, SetRoom):
        return _set_room_problems(scope, edit)
    if isinstance(edit, (AddSpawn, RemoveSpawn)):
        return _spawn_problems(scope, edit)
    if isinstance(edit, (Connect, SetDoor, Disconnect)):
        return _passage_problems(scope, edit)
    if isinstance(edit, AddKey):
        return _add_key_problems(scope, edit)
    if isinstance(edit, (Rewrite, AddTodoEdit)):
        return _missing_rooms(scope, edit)
    return []


def _edit_problems(ctx: Context, coords: dict[str, Coord], turn: AreaTurn) -> list[str]:
    """What is wrong with each edit on its own, in the order the edits apply."""
    scope = _turn_scope(ctx, coords)
    problems: list[str] = []
    for edit in turn.edits:
        problems.extend(_one_edit_problems(scope, edit))
        problems.extend(
            f"'{rid}' is not a plain declaration in this package; it cannot be edited"
            for rid in _edit_room_ids(edit)
            if rid in scope.opaque
        )
    return problems


def entry_room(ctx: Context, coords: dict[str, Coord]) -> str:
    """The first placed room of the package; reachability is judged from it."""
    for room in ctx.world.rooms_in(ctx.package):
        if room_id(room.label) in coords:
            return room_id(room.label)
    return next(iter(coords), "")


def turn_problems(ctx: Context, coords: dict[str, Coord], turn: AreaTurn) -> list[str]:
    if turn.kind != "edit":
        return []
    entry = entry_room(ctx, coords)
    known_items = _known_items(ctx, (edit.id for edit in turn.edits if isinstance(edit, AddKey)))
    grid_before = grid_from_coords(ctx, coords)
    baseline = set(validate(grid_before, entry=entry, room_range=None, known_items=known_items))
    grid_after = grid_from_coords(ctx, coords)
    for edit in turn.edits:
        apply_edit_to_grid(grid_after, edit)
    after = validate(grid_after, entry=entry, room_range=None, known_items=known_items)
    return [p for p in after if p not in baseline] + _edit_problems(ctx, coords, turn)
