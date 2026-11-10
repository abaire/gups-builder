"""The area mode: creating a new zone from a request, and refining it turn by turn.

The logic model briefs the request, plans the rooms on the grid and keys, and
the prose model writes each room and key. The plan's grid is validated before
any prose is requested, and the whole build is staged and checked before the
user sees the proposal. A follow-up message becomes a turn whose edits are
checked on the grid, then turned into ops against the current files. The plan
and turn shapes and their checks are in ``area_checks``.
"""

from __future__ import annotations

import json
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from gups_builder.engine.world import DoorInfo, ExitInfo, RoomInfo
from gups_builder.grid.derive import DIRECTION_DELTAS, OPPOSITES, derive_exits
from gups_builder.grid.model import Coord, DerivedExit, DoorSpec, Grid
from gups_builder.grid.place import place_existing, room_id
from gups_builder.llm.client import Message
from gups_builder.llm.structured import call_structured
from gups_builder.modes import testgen
from gups_builder.modes.area_checks import (
    AddKey,
    AddPortal,
    AddRoom,
    AddSpawn,
    AddTodoEdit,
    AreaBrief,
    AreaEdit,
    AreaPlan,
    AreaTurn,
    Connect,
    Disconnect,
    ItemProse,
    PlannedKey,
    PlannedLink,
    PlannedRoom,
    RemoveRoom,
    RemoveSpawn,
    Rewrite,
    RoomProse,
    SetDoor,
    SetRoom,
    apply_edit_to_grid,
    full_label,
    grid_from_coords,
    item_prose_check,
    local_id,
    plan_problems,
    plan_to_grid,
    prose_check,
    turn_problems,
)
from gups_builder.modes.common import (
    Context,
    Proposal,
    allowed_values,
    finalize,
    param_allowed,
)
from gups_builder.prompting import fit, load_prompt
from gups_builder.star.ops import (
    AddDecl,
    AddTodo,
    AppendItem,
    ItemMatch,
    RemoveDecl,
    RemoveItem,
    SetArg,
    StarOp,
)
from gups_builder.star.reader import DeclView, read_package
from gups_builder.star.render import render_string
from gups_builder.star.values import Value, is_editable
from gups_builder.star.writer import apply_ops

# The mode's entry points, and the plan and turn shapes its callers name.
__all__ = ["AreaBrief", "AreaPlan", "AreaTurn", "area_turn", "create_area", "plan_to_grid"]

SIZE_WORDS = {"tiny": (2, 4), "small": (4, 8), "medium": (8, 15), "large": (15, 30)}

COORDINATE_RULES = (
    "Rooms sit on an integer grid (x, y, z): +y is north, +x is east, +z is up. "
    "The eight horizontal neighbours, and straight up and down, are adjacent. "
    "Connect adjacent rooms with a Connection; use a Portal for anything else. "
    "No two rooms share a coordinate, and every room must be reachable from the entry room. "
    "A locked door names its key."
)


@dataclass
class _CreateState:
    """The brief, and what a build already produced, so a repair re-requests only what changed."""

    brief: AreaBrief
    plan: AreaPlan | None = None
    prose: dict[str, RoomProse] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)
    key_prose: dict[str, ItemProse] = field(default_factory=dict)
    key_concepts: dict[str, str] = field(default_factory=dict)


def create_area(ctx: Context, request: str) -> AreaBrief | Proposal:
    """A new area: the brief, then the plan, prose and ops inside the repair loop."""
    logic_budget = ctx.config.logic.context_tokens - ctx.config.logic.max_tokens
    brief_prompt = load_prompt(
        "area_brief", size_words=_size_words_text(), existing_rooms=_existing_rooms(ctx)
    )
    sections = [
        (0, request),
        (0, brief_prompt),
        (2, "Rooms in other packages that could be linked:\n" + _candidate_links(ctx)),
    ]
    brief = call_structured(
        ctx.llm,
        "logic",
        [
            Message("system", load_prompt("system_logic")),
            Message("user", fit(sections, logic_budget)),
        ],
        AreaBrief,
        ctx.config.max_repairs,
    )
    if brief.questions:
        return brief
    state = _CreateState(brief)
    return finalize(ctx, lambda problems: _build(ctx, state, problems), _builder_test(ctx))


def _counted(n: int, noun: str) -> str:
    return f"{n} {noun}{'s' if n != 1 else ''}"


def _size_words_text() -> str:
    return ", ".join(f"{word} {lo}-{hi} rooms" for word, (lo, hi) in SIZE_WORDS.items())


def _existing_rooms(ctx: Context) -> str:
    rooms = [d for d in ctx.view.decls if d.kind == "room"]
    if not rooms:
        return "none"
    return "\n".join(f"{d.id}: {d.args.get('name')}" for d in rooms)


def _candidate_links(ctx: Context) -> str:
    candidates = [
        f"{room.label}: {room.name}"
        for room in ctx.world.rooms
        if not room.label.startswith(f"//{ctx.package}:")
    ]
    return "\n".join(candidates) if candidates else "none"


def _size_options(ctx: Context) -> str:
    if not param_allowed(ctx.api, ctx.world, "room", "size"):
        return "none (the world has the track feature off)"
    return ", ".join(allowed_values(ctx.api, ctx.world, "room", "size"))


def _existing_ids(ctx: Context) -> str:
    ids = [d.id for d in ctx.view.decls]
    return ", ".join(ids) if ids else "none"


def _exits_by_room(grid: Grid) -> dict[str, list[DerivedExit]]:
    by_room: dict[str, list[DerivedExit]] = {}
    for exit in derive_exits(grid):
        by_room.setdefault(exit.room, []).append(exit)
    return by_room


def _fingerprint(room: PlannedRoom, neighbours: str) -> str:
    """Everything the room's prose prompt depends on that the plan can change."""
    return json.dumps([room.concept, room.size, [room.at.x, room.at.y, room.at.z], neighbours])


# -- creation -------------------------------------------------------------------


def _plan_sections(ctx: Context, state: _CreateState, problems: list[str]) -> list[tuple[int, str]]:
    """The plan prompt's sections, with the repair request when lint found problems."""
    graph_prompt = load_prompt(
        "area_graph",
        brief=state.brief.model_dump_json(indent=2),
        coordinate_rules=COORDINATE_RULES,
        sizes=_size_options(ctx),
        existing_ids=_existing_ids(ctx),
    )
    sections = [(0, graph_prompt)]
    if problems and state.plan is not None:
        # The repair keeps the brief and rules in front of the model, not only the errors.
        repair = load_prompt(
            "repair_lint", problems="\n".join(problems), previous=state.plan.model_dump_json()
        )
        sections.append((0, repair))
    return sections


def _plan_call(ctx: Context, brief: AreaBrief, sections: list[tuple[int, str]]) -> AreaPlan:
    logic_budget = ctx.config.logic.context_tokens - ctx.config.logic.max_tokens
    return call_structured(
        ctx.llm,
        "logic",
        [
            Message("system", load_prompt("system_logic")),
            Message("user", fit(sections, logic_budget)),
        ],
        AreaPlan,
        ctx.config.max_repairs,
        extra_check=lambda p: plan_problems(ctx, brief, p),
    )


def _planned_room_prose(
    ctx: Context, state: _CreateState, room: PlannedRoom, neighbours: str
) -> None:
    """The room's prose into ``state``, requested again only when what it depends on changed."""
    fingerprint = _fingerprint(room, neighbours)
    if room.id in state.prose and state.fingerprints.get(room.id) == fingerprint:
        return
    prompt = load_prompt(
        "room_prose",
        theme=state.brief.theme,
        style_notes=state.brief.style_notes,
        concept=room.concept,
        size=room.size,
        neighbours=neighbours,
    )
    state.prose[room.id] = _prose_call(ctx, prompt, RoomProse, prose_check(room.id))
    state.fingerprints[room.id] = fingerprint


def _planned_key_prose(ctx: Context, state: _CreateState, key: PlannedKey) -> ItemProse:
    """The key's prose, requested again only when its concept changed."""
    if key.id in state.key_prose and state.key_concepts.get(key.id) == key.concept:
        return state.key_prose[key.id]
    prompt = load_prompt("item_prose", theme=state.brief.theme, concept=key.concept)
    key_prose = _prose_call(ctx, prompt, ItemProse, item_prose_check(key.id))
    state.key_prose[key.id] = key_prose
    state.key_concepts[key.id] = key.concept
    return key_prose


def _room_decls(
    ctx: Context, plan: AreaPlan, state: _CreateState, exits_by_room: dict[str, list[DerivedExit]]
) -> list[StarOp]:
    planned = {room.id for room in plan.rooms}
    size_ok = param_allowed(ctx.api, ctx.world, "room", "size")
    ops: list[StarOp] = []
    link = plan.link
    for room in plan.rooms:
        exits = [_exit_value(e, planned) for e in exits_by_room.get(room.id, [])]
        if link is not None and link.room == room.id:
            exits.append(_link_exit(link.exit, link.external_label))
        args = _room_args(room, state.prose[room.id], exits, size_ok)
        ops.append(AddDecl(file="rooms.star", kind="room", id=room.id, args=args))
    return ops


def _key_decl(key_id: str, prose: ItemProse) -> AddDecl:
    args: dict[str, Value] = {
        "name": prose.name,
        "keywords": list(prose.keywords),
        "desc": prose.desc,
        "kind": "key",
    }
    return AddDecl(file="items.star", kind="item", id=key_id, args=args)


def _link_ops(ctx: Context, link: PlannedLink | None) -> tuple[list[StarOp], str | None]:
    """The return exit in the linked package's room, and that package."""
    if link is None:
        return [], None
    link_package, external_id = link.external_label.removeprefix("//").rsplit(":", 1)
    return_exit = link.return_exit or OPPOSITES[link.exit]
    value = _link_exit(return_exit, f"//{ctx.package}:{link.room}")
    return [AppendItem(kind="room", id=external_id, name="exits", value=value)], link_package


def _new_package_files(ctx: Context, brief: AreaBrief) -> dict[str, str]:
    """package.star for a package that has none yet."""
    # The reader skips package.star, so its absence from ctx.files says nothing; the disk does.
    if (ctx.world_dir / ctx.package / "package.star").exists():
        return {}
    return {
        "package.star": (
            "package(\n"
            f"    api_version = {ctx.world.api_version},\n"
            '    kind = "zone",\n'
            f"    title = {render_string(brief.title)},\n"
            "    reset_world_ticks = 10,\n"
            ")\n"
        )
    }


def _create_summary(plan: AreaPlan, exits_by_room: dict[str, list[DerivedExit]]) -> str:
    n_exits = sum(len(v) for v in exits_by_room.values()) + (1 if plan.link is not None else 0)
    summary = f"+{len(plan.rooms)} rooms, +{_counted(len(plan.keys), 'item')}, {n_exits} exits"
    if plan.link is not None:
        summary += f", 1 link to {plan.link.external_label} (needs confirmation)"
    return summary


def _preview(decl_id: str, prose: RoomProse | ItemProse) -> list[tuple[str, str, str]]:
    return [(decl_id, "name", prose.name), (decl_id, "desc", prose.desc)]


def _create_preview(
    plan: AreaPlan, state: _CreateState, keys: list[tuple[str, ItemProse]]
) -> list[tuple[str, str, str]]:
    """The rooms' prose, then the keys'."""
    preview = [p for room in plan.rooms for p in _preview(room.id, state.prose[room.id])]
    preview.extend(p for key_id, key_prose in keys for p in _preview(key_id, key_prose))
    return preview


def _build(ctx: Context, state: _CreateState, problems: list[str]) -> Proposal:
    plan = _plan_call(ctx, state.brief, _plan_sections(ctx, state, problems))
    state.plan = plan
    exits_by_room = _exits_by_room(plan_to_grid(plan))
    concepts = {room.id: room.concept for room in plan.rooms}
    for room in plan.rooms:
        neighbours = _neighbours_text(exits_by_room.get(room.id, []), concepts)
        _planned_room_prose(ctx, state, room, neighbours)
    keys = [(key.id, _planned_key_prose(ctx, state, key)) for key in plan.keys]

    ops = _room_decls(ctx, plan, state, exits_by_room)
    ops.extend(_key_decl(key_id, key_prose) for key_id, key_prose in keys)
    ops.extend(AddTodo(kind="room", id=todo.room, text=todo.text) for todo in plan.todos)
    link_ops, link_package = _link_ops(ctx, plan.link)
    return Proposal(
        ops=ops,
        new_files=_new_package_files(ctx, state.brief),
        summary=_create_summary(plan, exits_by_room),
        prose_preview=_create_preview(plan, state, keys),
        grid={room.id: (room.at.x, room.at.y, room.at.z) for room in plan.rooms},
        link_ops=link_ops,
        link_package=link_package,
    )


def _direction_value(direction: str) -> Value:
    """A standard direction as its DIR constant; a custom exit name as a plain string."""
    return {"$const": f"DIR.{direction}"} if direction in DIRECTION_DELTAS else direction


def _link_exit(direction: str, to: str) -> Value:
    return {"$call": "exit", "args": [_direction_value(direction)], "kwargs": {"to": to}}


def _door_value(door: DoorSpec) -> Value:
    door_kwargs: dict[str, Value] = {"state": {"$const": f"DOOR.{door.state}"}}
    if door.key is not None:
        door_kwargs["key"] = door.key if door.key[0] in (":", "/") else f":{door.key}"
    if door.tags:
        door_kwargs["tags"] = list(door.tags)
    return {"$call": "door", "args": [], "kwargs": door_kwargs}


def _exit_value(e: DerivedExit, room_ids: set[str]) -> Value:
    kwargs: dict[str, Value] = {"to": f":{e.to}" if e.to in room_ids else e.to}
    if e.door is not None:
        kwargs["door"] = _door_value(e.door)
    return {"$call": "exit", "args": [_direction_value(e.direction)], "kwargs": kwargs}


def _builder_test(ctx: Context) -> Callable[[Proposal], str | None]:
    def callback(proposal: Proposal) -> str | None:
        result = apply_ops(ctx.files, ctx.view, proposal.ops)
        contents = {**ctx.files, **result.files, **proposal.new_files}
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "pkg"
            pkg.mkdir()
            for name, text in contents.items():
                (pkg / name).write_text(text, encoding="utf-8")
            view_after = read_package(pkg, ctx.package)
        return testgen.builder_test(ctx.package, ctx.world, view_after) or None

    return callback


# -- refinement ------------------------------------------------------------------


_CATALOGUE = """add_room id, concept, at {x, y, z}, size, tags — add a room and its prose
remove_room id — remove a room and the exits that point at it
connect a, b, door {state, key} — join two adjacent rooms (add or change the door)
disconnect a, b — remove the passage between two rooms
set_door a, b, door | none — put a door on, or take one off, an existing passage
add_portal portal {a, a_exit, b, b_exit, door} — join two non-adjacent rooms
set_room id, size, tags, regions — change a room's size, tags or regions
rewrite ids, guidance — rewrite the prose of existing rooms
add_key id, concept — add a key item, with its prose
add_spawn room, mob, count, respawn_pulses — spawn a mob into a room
remove_spawn room, mob — take a spawn out of a room
add_todo room, text — leave a note on a room for a function to write"""

# A door compared across the file and the edit: state, key as a full label, tags.
_DoorKey = tuple[str, str | None, tuple[str, ...]]


def _short_label(label: str, package: str) -> str:
    """The ``:id`` form for this package's labels, as the files write them."""
    local = local_id(label, package)
    return f":{local}" if local is not None else label


def _door_key(state: str, key: str | None, tags: list[str], package: str) -> _DoorKey:
    return (state, full_label(key, package) if key else None, tuple(sorted(tags)))


def _spec_door_key(door: DoorSpec | None, package: str) -> _DoorKey | None:
    return None if door is None else _door_key(door.state, door.key, door.tags, package)


def _info_door_key(door: DoorInfo | None, package: str) -> _DoorKey | None:
    return None if door is None else _door_key(door.state, door.key, door.tags, package)


def _spawn_calls(value: Value | None) -> list[dict[str, Value]]:
    """The ``spawn(...)`` calls of a room's spawns list."""
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict) and item.get("$call") == "spawn"]


def _spawn_label(call: dict[str, Value]) -> str | None:
    """The mob label a spawn call starts with, or None when it starts with something else."""
    args = call.get("args")
    if not isinstance(args, list) or not args:
        return None
    first = args[0]
    return first if isinstance(first, str) else None


def _spawn_text(call: dict[str, Value]) -> str:
    label = _spawn_label(call)
    kwargs = call.get("kwargs")
    count = kwargs.get("count", 1) if isinstance(kwargs, dict) else 1
    return f"{label if label is not None else '?'} x{count}"


def _spawns_text(value: Value | None) -> str:
    return ", ".join(_spawn_text(call) for call in _spawn_calls(value)) or "none"


def _where(coords: dict[str, Coord], rid: str) -> str:
    if rid not in coords:
        return "off-map"
    x, y, z = coords[rid]
    return f"({x},{y},{z})"


def _exit_text(e: ExitInfo, package: str) -> str:
    target = local_id(e.to, package) or e.to
    if e.door is None:
        return f"{e.dir}->{target}"
    key = f", key {e.door.key}" if e.door.key else ""
    return f"{e.dir}->{target} [{e.door.state} door{key}]"


def _decl_text(decl: DeclView) -> str:
    """A plain room's spawns, and the fields its file computes in code."""
    text = f"; spawns: {_spawns_text(decl.args.get('spawns'))}"
    computed = [name for name, value in decl.args.items() if not is_editable(value)]
    if computed:
        text += f"; computed by code: {', '.join(computed)} (do not edit these)"
    return text


def _room_line(ctx: Context, room: RoomInfo, coords: dict[str, Coord], opaque: set[str]) -> str:
    rid = room_id(room.label)
    exits = ", ".join(_exit_text(e, ctx.package) for e in room.exits) or "none"
    line = f"{rid}: {room.name} at {_where(coords, rid)}; exits: {exits}"
    decl = ctx.view.get("room", rid)
    if decl is not None:
        return line + _decl_text(decl)
    if rid in opaque:
        return line + "; not a plain declaration (do not edit this room)"
    return line


def _rooms_text(ctx: Context, coords: dict[str, Coord], off_map: list[str]) -> str:
    opaque = {o.id for o in ctx.view.opaque if o.kind == "room" and o.id is not None}
    lines = [_room_line(ctx, room, coords, opaque) for room in ctx.world.rooms_in(ctx.package)]
    if off_map:
        lines.append(
            f"Off-map (no coordinate): {', '.join(off_map)}. Only a portal may reach an "
            "off-map room."
        )
    return "\n".join(lines) if lines else "none"


def _mobs_text(ctx: Context) -> str:
    lines = [f"{m.label}: {m.name} (level {m.level})" for m in ctx.world.mobs]
    return "Mobs that may be spawned into a room (label, name, level):\n" + (
        "\n".join(lines) if lines else "none"
    )


def _history_text(history: list[Message]) -> str:
    if not history:
        return "none"
    return "\n".join(f"{m.role}: {m.content}" for m in history[-6:])


@dataclass
class _TurnRequest:
    """A follow-up message, with the conversation and grid it is read against."""

    coords: dict[str, Coord]
    off_map: list[str]
    message: str
    history: list[Message]


def _turn_call(
    ctx: Context,
    request: _TurnRequest,
    repair: tuple[list[str], AreaTurn] | None = None,
) -> AreaTurn:
    """The turn for the request; ``repair`` is lint's problems and the turn they were in."""
    budget = ctx.config.logic.context_tokens - ctx.config.logic.max_tokens
    turn_prompt = load_prompt(
        "area_turn",
        catalogue=_CATALOGUE,
        rooms=_rooms_text(ctx, request.coords, request.off_map),
        history=_history_text(request.history),
        message=request.message,
    )
    sections = [(0, turn_prompt), (2, _mobs_text(ctx))]
    if repair is not None:
        problems, previous = repair
        sections.append(
            (
                0,
                load_prompt(
                    "repair_lint", problems="\n".join(problems), previous=previous.model_dump_json()
                ),
            )
        )
    return call_structured(
        ctx.llm,
        "logic",
        [
            Message("system", load_prompt("system_logic")),
            Message("user", fit(sections, budget)),
        ],
        AreaTurn,
        ctx.config.max_repairs,
        extra_check=lambda turn: turn_problems(ctx, request.coords, turn),
    )


def _spawn_value(ctx: Context, edit: AddSpawn) -> Value:
    kwargs: dict[str, Value] = {"count": edit.count}
    if edit.respawn_pulses is not None:
        kwargs["respawn_pulses"] = edit.respawn_pulses
    label = _short_label(full_label(edit.mob, ctx.package), ctx.package)
    return {"$call": "spawn", "args": [label], "kwargs": kwargs}


def _spawn_match(ctx: Context, edit: RemoveSpawn) -> ItemMatch:
    """Match the spawn by the label as the file writes it, short or full."""
    wanted = full_label(edit.mob, ctx.package)
    decl = ctx.view.get("room", edit.room)
    spawns = decl.args.get("spawns") if decl is not None else None
    for call in _spawn_calls(spawns):
        label = _spawn_label(call)
        if label is not None and full_label(label, ctx.package) == wanted:
            return ItemMatch(call="spawn", first_arg=label)
    return ItemMatch(call="spawn", first_arg=_short_label(wanted, ctx.package))


def _room_name(ctx: Context, rid: str) -> str:
    decl = ctx.view.get("room", rid)
    value = decl.args.get("name") if decl is not None else None
    if isinstance(value, str):
        return value
    for room in ctx.world.rooms_in(ctx.package):
        if room_id(room.label) == rid:
            return room.name
    return rid


def _current_desc(ctx: Context, rid: str) -> str:
    decl = ctx.view.get("room", rid)
    if decl is None:
        return ""
    value = decl.args.get("desc")
    return value if isinstance(value, str) else ""


def _prose_call[T: BaseModel](
    ctx: Context, prompt: str, model: type[T], check: Callable[[T], list[str]]
) -> T:
    budget = ctx.config.prose.context_tokens - ctx.config.prose.max_tokens
    return call_structured(
        ctx.llm,
        "prose",
        [
            Message("system", load_prompt("system_prose")),
            Message("user", fit([(0, prompt)], budget)),
        ],
        model,
        ctx.config.max_repairs,
        extra_check=check,
    )


def _rewrite_call(ctx: Context, rid: str, guidance: str) -> str:
    prompt = load_prompt(
        "room_rewrite",
        theme="the existing area",
        room_name=_room_name(ctx, rid),
        old_text=_current_desc(ctx, rid),
        guidance=guidance,
    )
    return _prose_call(ctx, prompt, RoomProse, prose_check(rid)).desc


def _new_room_prose(ctx: Context, edit: AddRoom, neighbours: str) -> RoomProse:
    prompt = load_prompt(
        "room_prose",
        theme="the existing area",
        style_notes="match the existing rooms",
        concept=edit.concept,
        size=edit.size,
        neighbours=neighbours,
    )
    return _prose_call(ctx, prompt, RoomProse, prose_check(edit.id))


def _key_prose(ctx: Context, edit: AddKey) -> ItemProse:
    prompt = load_prompt("item_prose", theme="the existing area", concept=edit.concept)
    return _prose_call(ctx, prompt, ItemProse, item_prose_check(edit.id))


def _neighbours_text(exits: list[DerivedExit], names: dict[str, str]) -> str:
    lines = [
        f"{e.direction}: {names.get(e.to, e.to)}"
        + (f" ({e.door.state} door)" if e.door is not None else "")
        for e in exits
    ]
    return "\n".join(lines) or "none"


def _room_args(
    room: PlannedRoom | AddRoom, prose: RoomProse, exits: list[Value], size_ok: bool
) -> dict[str, Value]:
    args: dict[str, Value] = {"name": prose.name, "desc": prose.desc}
    if size_ok and room.size != "normal":
        args["size"] = {"$const": f"SIZE.{room.size}"}
    if room.tags:
        args["tags"] = list(room.tags)
    args["exits"] = exits
    return args


def _rooms_exiting_to(ctx: Context, rid: str) -> list[str]:
    """The package's rooms with an exit to the room."""
    return [
        room_id(room.label)
        for room in ctx.world.rooms_in(ctx.package)
        if any(local_id(e.to, ctx.package) == rid for e in room.exits)
    ]


def _edit_pairs(ctx: Context, edit: AreaEdit) -> list[tuple[str, str]]:
    """The pairs of rooms whose passage one edit draws, changes or takes away."""
    if isinstance(edit, (Connect, Disconnect, SetDoor)):
        return [(edit.a, edit.b)]
    if isinstance(edit, AddPortal):
        return [(edit.portal.a, edit.portal.b)]
    if isinstance(edit, RemoveRoom):
        return [(other, edit.id) for other in _rooms_exiting_to(ctx, edit.id)]
    return []


def _touched_pairs(ctx: Context, edits: list[AreaEdit]) -> list[tuple[str, str]]:
    """Every pair of rooms whose passage an edit draws, changes or takes away."""
    pairs: list[tuple[str, str]] = []
    seen: set[frozenset[str]] = set()
    for a, b in (pair for edit in edits for pair in _edit_pairs(ctx, edit)):
        if frozenset((a, b)) not in seen:
            seen.add(frozenset((a, b)))
            pairs.append((a, b))
    return pairs


@dataclass
class _TurnBuild:
    """What a turn's ops are built against, and the notes and prose they leave."""

    current: dict[str, list[ExitInfo]]
    exits_after: dict[str, list[DerivedExit]]
    room_ids: set[str]
    names: dict[str, str]
    size_ok: bool
    new_coords: dict[str, Coord]
    skip: set[str]
    notes: list[str] = field(default_factory=list)
    preview: list[tuple[str, str, str]] = field(default_factory=list)


@dataclass
class _SideExits:
    """The exit ops on one room, in order; each direction is removed at most once."""

    side: str
    ops: list[StarOp] = field(default_factory=list)
    gone: set[str] = field(default_factory=set)

    def remove(self, direction: str) -> None:
        if direction not in self.gone:
            self.gone.add(direction)
            match = ItemMatch(call="exit", first_arg=_direction_value(direction))
            self.ops.append(RemoveItem(kind="room", id=self.side, name="exits", match=match))

    def add(self, value: Value) -> None:
        self.ops.append(AppendItem(kind="room", id=self.side, name="exits", value=value))


def _exits_between(
    build: _TurnBuild, package: str, side: str, other: str
) -> tuple[dict[str, _DoorKey | None], dict[str, DerivedExit]]:
    """The exits from ``side`` to ``other`` the files have now, and those wanted after."""
    actual = {
        e.dir: _info_door_key(e.door, package)
        for e in build.current.get(side, [])
        if local_id(e.to, package) == other
    }
    desired = {e.direction: e for e in build.exits_after.get(side, []) if e.to == other}
    return actual, desired


def _door_changed(wanted: DerivedExit | None, door: _DoorKey | None, package: str) -> bool:
    return wanted is None or _spec_door_key(wanted.door, package) != door


def _replace_exit(build: _TurnBuild, edits: _SideExits, e: DerivedExit) -> None:
    # Another room's exit in the same direction would clash with the new one.
    if any(x.dir == e.direction for x in build.current.get(edits.side, [])):
        edits.remove(e.direction)
    edits.add(_exit_value(e, build.room_ids))


def _side_exit_ops(ctx: Context, build: _TurnBuild, side: str, other: str) -> list[StarOp]:
    """The exit changes on ``side`` for its passage to ``other``."""
    pkg = ctx.package
    actual, desired = _exits_between(build, pkg, side, other)
    stale = [d for d, door in actual.items() if _door_changed(desired.get(d), door, pkg)]
    wanted = [e for d, e in desired.items() if d not in actual or _door_changed(e, actual[d], pkg)]
    edits = _SideExits(side)
    for direction in stale:
        edits.remove(direction)
    for e in wanted:
        _replace_exit(build, edits, e)
    return edits.ops


def _exit_ops(ctx: Context, edits: list[AreaEdit], build: _TurnBuild) -> list[StarOp]:
    """Exit changes on rooms that stay, diffed against the exits the files have now.

    Only the passages an edit touches are diffed, so an untouched room never
    changes; a new room's exits are in its AddDecl and a removed room's go with
    its RemoveDecl. Exits are matched by direction, which is unique in a room.
    """
    return [
        op
        for a, b in _touched_pairs(ctx, edits)
        for side, other in ((a, b), (b, a))
        if side not in build.skip
        for op in _side_exit_ops(ctx, build, side, other)
    ]


def _set_room_ops(edit: SetRoom, build: _TurnBuild) -> list[StarOp]:
    ops: list[StarOp] = []
    if edit.size is not None and not build.size_ok:
        build.notes.append(
            f"room '{edit.id}': the world has the track feature off; size left unchanged"
        )
    elif edit.size is not None:
        ops.append(
            SetArg(kind="room", id=edit.id, name="size", value={"$const": f"SIZE.{edit.size}"})
        )
    if edit.tags is not None:
        ops.append(SetArg(kind="room", id=edit.id, name="tags", value=list(edit.tags)))
    if edit.regions is not None:
        ops.append(SetArg(kind="room", id=edit.id, name="regions", value=list(edit.regions)))
    return ops


def _add_room_ops(ctx: Context, build: _TurnBuild, edit: AddRoom) -> list[StarOp]:
    room_exits = build.exits_after.get(edit.id, [])
    prose = _new_room_prose(ctx, edit, _neighbours_text(room_exits, build.names))
    exits = [_exit_value(e, build.room_ids) for e in room_exits]
    args = _room_args(edit, prose, exits, build.size_ok)
    build.new_coords[edit.id] = (edit.at.x, edit.at.y, edit.at.z)
    build.preview.extend(_preview(edit.id, prose))
    return [AddDecl(file="rooms.star", kind="room", id=edit.id, args=args)]


def _rewrite_ops(ctx: Context, build: _TurnBuild, edit: Rewrite) -> list[StarOp]:
    ops: list[StarOp] = []
    for rid in edit.ids:
        new_desc = _rewrite_call(ctx, rid, edit.guidance)
        ops.append(SetArg(kind="room", id=rid, name="desc", value=new_desc))
        build.preview.append((rid, "desc", new_desc))
    return ops


def _add_key_ops(ctx: Context, build: _TurnBuild, edit: AddKey) -> list[StarOp]:
    key_prose = _key_prose(ctx, edit)
    build.preview.extend(_preview(edit.id, key_prose))
    return [_key_decl(edit.id, key_prose)]


def _plain_edit_ops(ctx: Context, edit: AreaEdit) -> list[StarOp]:
    """The ops for an edit that writes no prose; passages are diffed after all edits."""
    if isinstance(edit, RemoveRoom):
        return [RemoveDecl(kind="room", id=edit.id)]
    if isinstance(edit, AddSpawn):
        return [AppendItem(kind="room", id=edit.room, name="spawns", value=_spawn_value(ctx, edit))]
    if isinstance(edit, RemoveSpawn):
        match = _spawn_match(ctx, edit)
        return [RemoveItem(kind="room", id=edit.room, name="spawns", match=match)]
    if isinstance(edit, AddTodoEdit):
        return [AddTodo(kind="room", id=edit.room, text=edit.text)]
    return []


def _edit_ops(ctx: Context, build: _TurnBuild, edit: AreaEdit) -> list[StarOp]:
    """The ops for one edit; new prose and notes go into ``build``."""
    if isinstance(edit, AddRoom):
        return _add_room_ops(ctx, build, edit)
    if isinstance(edit, SetRoom):
        return _set_room_ops(edit, build)
    if isinstance(edit, Rewrite):
        return _rewrite_ops(ctx, build, edit)
    if isinstance(edit, AddKey):
        return _add_key_ops(ctx, build, edit)
    return _plain_edit_ops(ctx, edit)


def _turn_build(ctx: Context, turn: AreaTurn, coords: dict[str, Coord]) -> _TurnBuild:
    grid_after = grid_from_coords(ctx, coords)
    for edit in turn.edits:
        apply_edit_to_grid(grid_after, edit)
    rooms = ctx.world.rooms_in(ctx.package)
    existing = {room_id(r.label) for r in rooms}
    added = {edit.id: edit.concept for edit in turn.edits if isinstance(edit, AddRoom)}
    removed = {edit.id for edit in turn.edits if isinstance(edit, RemoveRoom)}
    names = {rid: _room_name(ctx, rid) for rid in existing}
    names.update(added)
    return _TurnBuild(
        current={room_id(r.label): r.exits for r in rooms},
        exits_after=_exits_by_room(grid_after),
        # A removed room's label is still ":<id>" in the exits that point at it.
        room_ids=existing | set(added),
        names=names,
        size_ok=param_allowed(ctx.api, ctx.world, "room", "size"),
        new_coords={rid: c for rid, c in coords.items() if rid not in removed},
        skip=set(added) | removed,
    )


def _edit_to_proposal(ctx: Context, turn: AreaTurn, coords: dict[str, Coord]) -> Proposal:
    build = _turn_build(ctx, turn, coords)
    ops = [op for edit in turn.edits for op in _edit_ops(ctx, build, edit)]
    ops.extend(_exit_ops(ctx, turn.edits, build))
    added = sum(isinstance(edit, AddRoom) for edit in turn.edits)
    removed = {edit.id for edit in turn.edits if isinstance(edit, RemoveRoom)}
    return Proposal(
        ops=ops,
        new_files={},
        summary=_refine_summary(added, len(removed), len(ops)),
        prose_preview=build.preview,
        notes=build.notes,
        grid=build.new_coords,
    )


def _refine_summary(new_rooms: int, removed_rooms: int, n_ops: int) -> str:
    parts = []
    if new_rooms:
        parts.append(f"+{_counted(new_rooms, 'room')}")
    if removed_rooms:
        parts.append(f"-{_counted(removed_rooms, 'room')}")
    if not parts:
        parts.append(_counted(n_ops, "change"))
    return ", ".join(parts)


def _is_zone(ctx: Context) -> bool:
    return any(p.path == ctx.package and p.kind == "zone" for p in ctx.world.packages)


def _update_coords(grid_coords: dict[str, Coord], turn: AreaTurn, grid: dict[str, Coord]) -> None:
    """The caller's coordinates after the turn: removed rooms dropped, the rest replaced."""
    for edit in turn.edits:
        if isinstance(edit, RemoveRoom):
            grid_coords.pop(edit.id, None)
    grid_coords.update(grid)


def area_turn(
    ctx: Context,
    grid_coords: dict[str, Coord],
    message: str,
    history: list[Message],
) -> tuple[AreaTurn, Proposal | None]:
    """Turn a follow-up message in area mode into a turn and, for an edit, a proposal.

    ``grid_coords`` is updated in place with the coordinates after the edit, but
    only from the turn the proposal was finally built from; a turn rejected by a
    lint repair leaves no coordinates behind.
    """
    coords, off_map = place_existing(ctx.world.rooms_in(ctx.package), ctx.package, grid_coords)
    request = _TurnRequest(coords=coords, off_map=off_map, message=message, history=history)
    turn = _turn_call(ctx, request)
    if turn.kind != "edit":
        return turn, None
    last = turn

    def build(problems: list[str]) -> Proposal:
        nonlocal last
        if problems:
            last = _turn_call(ctx, request, (problems, last))
        if last.kind != "edit":
            return Proposal(
                ops=[],
                new_files={},
                summary="no changes",
                prose_preview=[],
                notes=[f"The repair answered with a {last.kind} turn: {last.reply}"],
                grid=dict(coords),
            )
        return _edit_to_proposal(ctx, last, coords)

    proposal = finalize(ctx, build, _builder_test(ctx) if _is_zone(ctx) else None)
    if last.kind != "edit":
        return last, None
    if proposal.grid is not None:
        _update_coords(grid_coords, last, proposal.grid)
    return last, proposal
