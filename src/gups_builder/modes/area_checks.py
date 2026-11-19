"""The area mode's plan and turn shapes, and the checks run on them before any prose is spent.

A plan's grid is validated, along with its link to another package, its ids
and its TODOs. A turn's edits are applied to the package's grid and only the
problems they add are reported, alongside what is wrong with each edit on its
own, checked in order so an edit may name a room an earlier edit added.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import count
from typing import TYPE_CHECKING, Annotated, Any, Literal

from pydantic import BaseModel, Field

from gups_builder.grid.derive import (
    DIRECTION_DELTAS,
    OPPOSITES,
    name_clashing_exits,
    snake_case,
    snake_case_exits,
    validate,
)
from gups_builder.grid.layout import lay_out, spread_directions
from gups_builder.grid.model import (
    Connection,
    Coord,
    DoorSpec,
    Grid,
    GridRoom,
    Passage,
    Portal,
    RandomExit,
)
from gups_builder.grid.place import room_id
from gups_builder.modes.area_floor import (
    floor_labels,
    full_label,
    key_reach_problem,
    key_reach_problems,
    known_items,
    local_id,
    names_item,
    new_floor_items,
    package_floors,
    plan_floor_problems,
)
from gups_builder.modes.common import Context, Turn, allowed_values, param_allowed

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from gups_builder.engine.world import DoorInfo, ExitInfo, RoomInfo


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
    # Every settled decision, one short line each: what /build must honour, kept whole
    # while the conversation that settled it is folded away.
    decisions: list[str] = []
    questions: list[str] = []


class AreaPlanTurn(BaseModel):
    reply: str
    brief: AreaBrief


class PlannedRoom(BaseModel):
    id: str
    concept: str
    at: XYZ
    size: Literal["cramped", "normal", "large"] = "normal"
    tags: list[str] = []


class PlannedKey(BaseModel):
    id: str
    concept: str
    room: str | None = None  # the room whose floor the key lies on


class FloorItem(BaseModel):
    """An item kept on a room's floor: an existing item, or a new id with a concept."""

    room: str
    item: str
    concept: str | None = None
    kind: str = "misc"


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
    random_exits: list[RandomExit] = []
    keys: list[PlannedKey] = []
    items: list[FloorItem] = []
    entry: str
    link: PlannedLink | None = None
    todos: list[RoomTodo] = []


class RoomConcepts(BaseModel):
    room_concepts: list[str]


class GraphRoom(BaseModel):
    id: str
    concept: str
    size: Literal["cramped", "normal", "large"] = "normal"
    tags: list[str] = []


class AreaGraph(BaseModel):
    """The plan as the model writes it: passages say which way they run, not where rooms are.

    ``place_graph`` lays it out into an ``AreaPlan``, which the rest of the mode uses.
    """

    rooms: list[GraphRoom]
    passages: list[Passage]
    portals: list[Portal] = []
    random_exits: list[RandomExit] = []
    keys: list[PlannedKey] = []
    items: list[FloorItem] = []
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


class AddRandomExit(BaseModel):
    edit: Literal["add_random_exit"] = "add_random_exit"
    random_exit: RandomExit


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
    room: str | None = None  # the room whose floor the key lies on


class PlaceItem(BaseModel):
    """Keep an item on a room's floor: an existing item, or a new id with a concept."""

    edit: Literal["place_item"] = "place_item"
    room: str
    item: str
    concept: str | None = None
    kind: str = "misc"


class UnplaceItem(BaseModel):
    edit: Literal["unplace_item"] = "unplace_item"
    room: str
    item: str


class AddSpawn(BaseModel):
    edit: Literal["add_spawn"] = "add_spawn"
    room: str
    mob: str
    max: int = 1
    respawn: int | None = None  # pulses; None keeps the engine's default timer


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
    | AddRandomExit
    | SetRoom
    | Rewrite
    | AddKey
    | PlaceItem
    | UnplaceItem
    | AddSpawn
    | RemoveSpawn
    | AddTodoEdit,
    Field(discriminator="edit"),
]


class AreaTurn(Turn):
    edits: list[AreaEdit] = Field(default_factory=list)


# Re-exported: the area mode and design apply name these here.
__all__ = ["floor_labels", "full_label", "local_id", "new_floor_items"]

_ID = re.compile(r"[a-z][a-z0-9_]*")

_DOOR_STATES: dict[str, Literal["open", "closed", "locked"]] = {
    "open": "open",
    "closed": "closed",
    "locked": "locked",
}


# -- labels ----------------------------------------------------------------------


# -- the link --------------------------------------------------------------------


def _is_external(ctx: Context, label: str) -> bool:
    return not label.startswith(f"//{ctx.package}:")


def resolve_external_label(ctx: Context, label: str) -> str:
    """A bare room id naming exactly one room of another package, as that room's label.

    Anything else is returned as it is, for the checks to judge.
    """
    if label.startswith(("//", ":")):
        return label
    matches = [
        room.label
        for room in ctx.world.rooms
        if _is_external(ctx, room.label) and room_id(room.label) == label
    ]
    return matches[0] if len(matches) == 1 else label


def normalize_brief(ctx: Context, brief: AreaBrief) -> AreaBrief:
    if brief.link_to is None:
        return brief
    return brief.model_copy(update={"link_to": resolve_external_label(ctx, brief.link_to)})


def brief_problems(ctx: Context, brief: AreaBrief) -> list[str]:
    """A brief may only link to a room that exists in another package."""
    label = brief.link_to
    if label is None:
        return []
    exists = any(room.label == label for room in ctx.world.rooms)
    if exists and _is_external(ctx, label):
        return []
    return [
        f"link_to '{label}' is not a room of another package; use the full label of one of "
        "the rooms listed as linkable, or null"
    ]


def concepts_fit(brief: AreaBrief) -> bool:
    return brief.room_count.min <= len(brief.room_concepts) <= brief.room_count.max


def at_most_max_concepts(brief: AreaBrief, concepts: RoomConcepts) -> RoomConcepts:
    """The list cut to the room count's maximum; the rooms already agreed come first."""
    return RoomConcepts(room_concepts=concepts.room_concepts[: brief.room_count.max])


def concept_count_problems(brief: AreaBrief, concepts: RoomConcepts) -> list[str]:
    """The list must reach the room count's minimum; a longer one has been cut already."""
    n, lo, hi = len(concepts.room_concepts), brief.room_count.min, brief.room_count.max
    if lo <= n <= hi:
        return []
    return [f"the list has {n} room concepts; the area needs {lo} to {hi}, so add {lo - n}"]


def bind_link(brief: AreaBrief, plan: AreaPlan) -> AreaPlan:
    """The plan with its link held to the brief, the design the user agreed to.

    A link the brief does not name is dropped rather than repaired: nobody asked for it.
    The external room is always the brief's, so a model that echoes only the room's id,
    or picks another room, cannot fail the plan over it.
    """
    if plan.link is None:
        return plan
    if brief.link_to is None:
        return plan.model_copy(update={"link": None})
    link = plan.link.model_copy(update={"external_label": brief.link_to})
    return plan.model_copy(update={"link": link})


def normalize_plan(brief: AreaBrief, plan: AreaPlan) -> AreaPlan:
    """The plan with what the builder can settle alone settled: the link, then exit names.

    Exit names are put in the engine's snake_case first, since that can make two clash.
    The link's exit keeps its name, since the user saw it in the brief; a portal that
    clashes with it is the one renamed.
    """
    plan = _snake_case_link(bind_link(brief, plan))
    held = [] if plan.link is None else [(plan.link.room, plan.link.exit)]
    grid = name_clashing_exits(snake_case_exits(plan_to_grid(plan)), held)
    if (grid.portals, grid.random_exits) == (plan.portals, plan.random_exits):
        return plan
    return plan.model_copy(update={"portals": grid.portals, "random_exits": grid.random_exits})


def _snake_case_link(plan: AreaPlan) -> AreaPlan:
    link = plan.link
    if link is None:
        return plan
    names = {"exit": snake_case(link.exit)}
    if link.return_exit is not None:
        names["return_exit"] = snake_case(link.return_exit)
    return plan.model_copy(update={"link": link.model_copy(update=names)})


# -- the plan --------------------------------------------------------------------


def place_graph(graph: AreaGraph) -> AreaPlan:
    """The graph laid out on the grid, its passages become connections or portals."""
    ids = [room.id for room in graph.rooms]
    passages = spread_directions(graph.passages, _named_exits(graph))
    layout = lay_out(ids, passages, graph.entry)
    spare = _spare_cells(layout.coords)
    rooms: list[PlannedRoom] = []
    seen: set[str] = set()
    for room in graph.rooms:
        # A repeated id is reported as such; on its own cell, it adds no coordinate clash.
        coord = next(spare) if room.id in seen else layout.coords[room.id]
        seen.add(room.id)
        rooms.append(
            PlannedRoom(
                id=room.id,
                concept=room.concept,
                at=XYZ(x=coord[0], y=coord[1], z=coord[2]),
                size=room.size,
                tags=room.tags,
            )
        )
    return AreaPlan(
        rooms=rooms,
        connections=layout.connections,
        portals=[*graph.portals, *layout.portals],
        random_exits=graph.random_exits,
        keys=graph.keys,
        items=graph.items,
        entry=graph.entry,
        link=graph.link,
        todos=graph.todos,
    )


def _named_exits(graph: AreaGraph) -> list[tuple[str, str]]:
    """The exits a graph's portals and link give its rooms, which passages must not reuse."""
    exits = [(p.a, p.a_exit) for p in graph.portals]
    exits.extend((p.b, p.b_exit) for p in graph.portals if p.b_exit is not None)
    if graph.link is not None:
        exits.append((graph.link.room, graph.link.exit))
    return exits


def _spare_cells(coords: dict[str, Coord]) -> Iterator[Coord]:
    east = max((coord[0] for coord in coords.values()), default=0) + 2
    return ((x, 0, 0) for x in count(east, 2))


def plan_to_grid(plan: AreaPlan) -> Grid:
    return Grid(
        rooms=[
            GridRoom(id=room.id, coord=(room.at.x, room.at.y, room.at.z)) for room in plan.rooms
        ],
        connections=list(plan.connections),
        portals=list(plan.portals),
        random_exits=list(plan.random_exits),
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
        problems.append(f"the id '{decl_id}' is used twice in the plan; give one another id")
    elif decl_id in existing:
        problems.append(f"the id '{decl_id}' is already declared in the package; choose a fresh id")
    return problems


def _plan_id_problems(ctx: Context, plan: AreaPlan) -> list[str]:
    existing = {d.id for d in ctx.view.decls}
    seen: set[str] = set()
    problems: list[str] = []
    ids = [room.id for room in plan.rooms] + [key.id for key in plan.keys]
    ids.extend(item.item for item in new_floor_items(ctx, plan))
    for decl_id in ids:
        problems.extend(_decl_id_problems(decl_id, seen, existing))
        seen.add(decl_id)
    return problems


def _todo_problems(plan: AreaPlan) -> list[str]:
    planned = {room.id for room in plan.rooms}
    return [
        f"the todo names the unknown room '{todo.room}'; use a room id of the plan"
        for todo in plan.todos
        if todo.room not in planned
    ]


def plan_problems(ctx: Context, brief: AreaBrief, plan: AreaPlan) -> list[str]:
    """Every problem with the plan, so a bad plan is repaired before any prose is spent."""
    problems = validate(
        _checked_grid(plan),
        entry=plan.entry,
        room_range=(brief.room_count.min, brief.room_count.max),
        known_items=known_items(ctx, (key.id for key in plan.keys)),
    )
    if plan.link is not None:
        problems.extend(_link_problems(ctx, plan, plan.link))
    problems.extend(_plan_id_problems(ctx, plan))
    problems.extend(_todo_problems(plan))
    problems.extend(plan_floor_problems(ctx, plan))
    problems.extend(key_reach_problems(plan_to_grid(plan), plan, ctx.package))
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
        problems.append(
            f"the link starts from the unknown room '{link.room}'; use a room id of the plan"
        )
    label = link.external_label
    external = next((room for room in ctx.world.rooms if room.label == label), None)
    problems.extend(_external_room_problems(ctx, label, external))
    problems.extend(_return_exit_problems(link, external))
    return problems


# -- prose -----------------------------------------------------------------------


_MAX_NAME_LEN = 60


def prose_check(what: str) -> Callable[[RoomProse], list[str]]:
    def check(prose: RoomProse) -> list[str]:
        problems: list[str] = []
        if not prose.name.strip():
            problems.append(f"{what}: name is empty")
        if "\n" in prose.name:
            problems.append(f"{what}: name must be one line")
        if len(prose.name) > _MAX_NAME_LEN:
            problems.append(
                f"{what}: name is {len(prose.name)} characters, the limit is {_MAX_NAME_LEN}"
            )
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


def _passage(package: str, rid: str, room_exit: ExitInfo) -> Connection | None:
    """A standard-direction exit to another room of the package, as a connection."""
    target = local_id(room_exit.to, package)
    if room_exit.dir not in DIRECTION_DELTAS or target is None:
        return None
    return Connection(a=rid, b=target, door=_door_spec(room_exit.door))


def _passages(package: str, room: RoomInfo) -> list[Connection]:
    rid = room_id(room.label)
    found = (_passage(package, rid, e) for e in room.exits)
    return [c for c in found if c is not None]


def _random_exit(package: str, rid: str, room_exit: ExitInfo) -> RandomExit | None:
    """A random exit whose rooms are all in the package; None for any other exit."""
    targets = [local_id(label, package) for label in room_exit.candidates]
    if not targets or None in targets:
        return None
    return RandomExit(room=rid, exit=room_exit.dir, to=[t for t in targets if t is not None])


def _random_exits(package: str, rooms: list[RoomInfo]) -> list[RandomExit]:
    found = (_random_exit(package, room_id(r.label), e) for r in rooms for e in r.exits)
    return [r for r in found if r is not None]


def grid_from_coords(ctx: Context, coords: dict[str, Coord]) -> Grid:
    """The package's rooms, their standard-direction passages and their random exits."""
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
    return Grid(
        rooms=grid_rooms,
        connections=connections,
        portals=[],
        random_exits=_random_exits(ctx.package, rooms),
    )


def _remove_from_grid(grid: Grid, rid: str) -> None:
    grid.rooms = [room for room in grid.rooms if room.id != rid]
    grid.connections = [c for c in grid.connections if rid not in (c.a, c.b)]
    grid.portals = [p for p in grid.portals if rid not in (p.a, p.b)]
    # A random exit that still picks the removed room is left for validation to report.
    grid.random_exits = [r for r in grid.random_exits if r.room != rid]


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
    elif isinstance(edit, AddRandomExit):
        grid.random_exits.append(_snake_cased(edit.random_exit))


def _snake_cased(random_exit: RandomExit) -> RandomExit:
    return random_exit.model_copy(update={"exit": snake_case(random_exit.exit)})


# -- turns -----------------------------------------------------------------------


def _edit_room_ids(edit: AreaEdit) -> list[str]:
    """The rooms of the package an edit names; a random exit edits only its own room."""
    getter = _EDIT_ROOMS.get(type(edit))
    return getter(edit) if getter is not None else []


def _key_room(edit: AddKey) -> list[str]:
    return [edit.room] if edit.room is not None else []


# The rooms each kind of edit names; the rooms a random exit picks among are validation's to check.
_EDIT_ROOMS: dict[type, Callable[[Any], list[str]]] = {
    AddRoom: lambda e: [e.id],
    RemoveRoom: lambda e: [e.id],
    SetRoom: lambda e: [e.id],
    Connect: lambda e: [e.a, e.b],
    Disconnect: lambda e: [e.a, e.b],
    SetDoor: lambda e: [e.a, e.b],
    AddPortal: lambda e: [e.portal.a, e.portal.b],
    AddRandomExit: lambda e: [e.random_exit.room],
    AddSpawn: lambda e: [e.room],
    RemoveSpawn: lambda e: [e.room],
    AddTodoEdit: lambda e: [e.room],
    PlaceItem: lambda e: [e.room],
    UnplaceItem: lambda e: [e.room],
    AddKey: _key_room,
    Rewrite: lambda e: list(e.ids),
}


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
    exit_names: dict[str, set[str]]
    world_items: set[str] = field(default_factory=set)
    floors: dict[str, set[str]] = field(default_factory=dict)


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
        exit_names={
            room_id(r.label): {e.dir for e in r.exits} for r in ctx.world.rooms_in(ctx.package)
        },
        world_items={item.label for item in ctx.world.items},
        floors=package_floors(ctx),
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
    if isinstance(edit, AddSpawn) and edit.max < 1:
        problems.append(f"a spawn of '{edit.mob}' in '{edit.room}' needs a max of 1 or more")
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
    if edit.room is None:
        problems.append(
            f"the key '{edit.id}' lies in no room; give it the room whose floor it lies on"
        )
    else:
        problems.extend(_missing_rooms(scope, edit))
        scope.floors.setdefault(edit.room, set()).add(full_label(edit.id, scope.package))
    scope.items.add(edit.id)
    return problems


def _scope_knows_item(scope: _TurnScope, label: str) -> bool:
    return names_item(scope.items | scope.world_items, label, scope.package)


def _new_item_problems(scope: _TurnScope, edit: PlaceItem) -> list[str]:
    """A placed item the turn creates: a fresh id of this package."""
    item_id = local_id(full_label(edit.item, scope.package), scope.package) or edit.item
    scope.items.add(item_id)
    if not _ID.fullmatch(item_id):
        return [f"the item id '{item_id}' is not snake_case ([a-z][a-z0-9_]*)"]
    if _declared_elsewhere(scope, item_id):
        return [f"the id '{item_id}' is already declared in the package; choose a fresh id"]
    return []


def _declared_elsewhere(scope: _TurnScope, item_id: str) -> bool:
    """Whether a room or mob of the package already has the id a new item wants."""
    return item_id in scope.rooms or full_label(item_id, scope.package) in scope.mobs


def _place_item_problems(scope: _TurnScope, edit: PlaceItem) -> list[str]:
    problems = _missing_rooms(scope, edit)
    if not _scope_knows_item(scope, edit.item):
        if edit.concept is None:
            problems.append(
                f"there is no item '{edit.item}'; place an item of the world, or give a "
                "concept to create it"
            )
        else:
            problems.extend(_new_item_problems(scope, edit))
    scope.floors.setdefault(edit.room, set()).add(full_label(edit.item, scope.package))
    return problems


def _unplace_item_problems(scope: _TurnScope, edit: UnplaceItem) -> list[str]:
    problems = _missing_rooms(scope, edit)
    floor = scope.floors.get(edit.room, set())
    label = full_label(edit.item, scope.package)
    if label not in floor:
        problems.append(f"'{edit.item}' is not on the floor of '{edit.room}'")
    floor.discard(label)
    return problems


def _random_exit_name_problems(scope: _TurnScope, edit: AddRandomExit) -> list[str]:
    """An exit name is unique in its room; the grid's checks do not see exits the files hold."""
    room, name = edit.random_exit.room, snake_case(edit.random_exit.exit)
    taken = scope.exit_names.setdefault(room, set())
    if name in taken:
        return [f"'{room}' already has an exit named '{name}'; give the random exit another name"]
    taken.add(name)
    return []


def _catalogue_problems(scope: _TurnScope, edit: AreaEdit) -> list[str]:
    """The problems of an edit about a thing the package gains: a key or a random exit."""
    if isinstance(edit, AddKey):
        return _add_key_problems(scope, edit)
    if isinstance(edit, AddRandomExit):
        return _random_exit_name_problems(scope, edit)
    if isinstance(edit, PlaceItem):
        return _place_item_problems(scope, edit)
    if isinstance(edit, UnplaceItem):
        return _unplace_item_problems(scope, edit)
    return []


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
    if isinstance(edit, (Rewrite, AddTodoEdit)):
        return _missing_rooms(scope, edit)
    return _catalogue_problems(scope, edit)


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
    known = known_items(ctx, (edit.id for edit in turn.edits if isinstance(edit, AddKey)))
    grid_before = grid_from_coords(ctx, coords)
    baseline = set(validate(grid_before, entry=entry, room_range=None, known_items=known))
    grid_after = grid_from_coords(ctx, coords)
    for edit in turn.edits:
        apply_edit_to_grid(grid_after, edit)
    after = validate(grid_after, entry=entry, room_range=None, known_items=known)
    reach = [
        key_reach_problem(grid_after, entry, (edit.id, edit.room), ctx.package)
        for edit in turn.edits
        if isinstance(edit, AddKey) and edit.room is not None
    ]
    new = [p for p in after if p not in baseline] + [p for p in reach if p]
    return new + _edit_problems(ctx, coords, turn)
