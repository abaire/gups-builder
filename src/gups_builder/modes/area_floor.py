"""Items on the floors of an area's rooms, and the labels that name them.

A plan's keys and floor items become item spawn rules in their rooms' spawns;
these are the checks that hold them to the plan and the package, and the
helpers that read a room's floor back from those rules.

Every floor item is written ``respawn = "reset"`` with the engine's default
``until = "gone"``: the rule keeps one copy in the world, wherever it is. A
player who picks it up keeps the only one, even in a logged-out pack, and a
copy that leaves the world (destroyed, used up) comes back at the next zone
reset, so a key is never lost for good and never duplicated.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from gups_builder.grid.derive import derive_exits
from gups_builder.modes.common import spawn_call

if TYPE_CHECKING:
    from collections.abc import Iterable

    from gups_builder.grid.model import DerivedExit, DoorSpec, Grid
    from gups_builder.modes.area_checks import AreaPlan, FloorItem
    from gups_builder.modes.common import Context
    from gups_builder.star.reader import DeclView
    from gups_builder.star.values import Value

FLOOR_RESPAWN = "reset"


def local_id(label: str | None, package: str) -> str | None:
    """The id when the label names something in this package; None otherwise."""
    if label is None:
        return None
    if label.startswith(":"):
        return label[1:]
    prefix = f"//{package}:"
    return label[len(prefix) :] if label.startswith(prefix) else None


def full_label(label: str, package: str) -> str:
    """A label in full: a bare id or ``:id`` is taken to be in this package."""
    if label.startswith("//"):
        return label
    return f"//{package}:{label.removeprefix(':')}"


def known_items(ctx: Context, new_ids: Iterable[str]) -> set[str]:
    """The items a locked door may name: new ones, the package's, and the world's."""
    known = set(new_ids)
    known.update(d.id for d in ctx.view.decls if d.kind == "item")
    known.update(item.label for item in ctx.world.items)
    return known


def short_label(label: str, package: str) -> str:
    """A label as the files write it: ``:id`` in this package, in full otherwise."""
    local = local_id(full_label(label, package), package)
    return f":{local}" if local is not None else label


def floor_spawn(label: str, package: str) -> Value:
    """The spawn rule that keeps one copy of the item on a room's floor."""
    return spawn_call(short_label(label, package), respawn=FLOOR_RESPAWN)


def item_labels(ctx: Context) -> set[str]:
    """Every item of the world and of the package, as full labels."""
    labels = {item.label for item in ctx.world.items}
    labels.update(full_label(d.id, ctx.package) for d in ctx.view.decls if d.kind == "item")
    return labels


def _spawn_label(entry: Value) -> str | None:
    """The label a ``spawn(...)`` entry starts with, or None for anything else."""
    if not isinstance(entry, dict) or entry.get("$call") != "spawn":
        return None
    args = entry.get("args")
    first = args[0] if isinstance(args, list) and args else None
    return first if isinstance(first, str) else None


def room_floor(decl: DeclView, items: set[str], package: str) -> list[str]:
    """The items a room's spawn rules place, as full labels; mob rules are left out."""
    spawns = decl.args.get("spawns")
    if not isinstance(spawns, list):
        return []
    labels = (_spawn_label(entry) for entry in spawns)
    full = [full_label(label, package) for label in labels if label is not None]
    return [label for label in full if label in items]


def package_floors(ctx: Context) -> dict[str, set[str]]:
    """The items the files keep on each room's floor, as full labels."""
    items = item_labels(ctx)
    return {
        decl.id: set(room_floor(decl, items, ctx.package))
        for decl in ctx.view.decls
        if decl.kind == "room"
    }


def names_item(known: set[str], label: str, package: str) -> bool:
    """Whether ``label``, bare, short or full, names one of ``known`` (ids and full labels)."""
    as_id = local_id(full_label(label, package), package)
    return full_label(label, package) in known or (as_id is not None and as_id in known)


def _is_new_item(known: set[str], item: FloorItem, package: str) -> bool:
    return item.concept is not None and not names_item(known, item.item, package)


def new_floor_items(ctx: Context, plan: AreaPlan) -> list[FloorItem]:
    """The floor items the plan creates, once each: a concept and an id nothing declares."""
    known = known_items(ctx, (key.id for key in plan.keys))
    created: dict[str, FloorItem] = {}
    for item in plan.items:
        if _is_new_item(known, item, ctx.package):
            created.setdefault(item.item, item)
    return list(created.values())


def floor_labels(plan: AreaPlan, package: str) -> dict[str, list[str]]:
    """Each room's floor as full item labels: its keys first, then its other items."""
    floors: dict[str, list[str]] = {}
    for key in plan.keys:
        if key.room is not None:
            floors.setdefault(key.room, []).append(full_label(key.id, package))
    for item in plan.items:
        floors.setdefault(item.room, []).append(full_label(item.item, package))
    return floors


def _key_room_problems(plan: AreaPlan) -> list[str]:
    planned = {room.id for room in plan.rooms}
    problems: list[str] = []
    for key in plan.keys:
        if key.room is None:
            problems.append(
                f"the key '{key.id}' lies in no room; give it the room whose floor it lies on"
            )
        elif key.room not in planned:
            problems.append(
                f"the key '{key.id}' lies in the unknown room '{key.room}'; "
                "use a room id of the plan"
            )
    return problems


def _floor_item_problem(known: set[str], item: FloorItem, planned: set[str], package: str) -> str:
    if item.room not in planned:
        return (
            f"the item '{item.item}' lies in the unknown room '{item.room}'; "
            "use a room id of the plan"
        )
    if item.concept is None and not names_item(known, item.item, package):
        return (
            f"the item '{item.item}' on the floor of '{item.room}' is not an item of the world "
            "or the plan; give it a concept to create it"
        )
    return ""


def _repeat_problems(plan: AreaPlan, package: str) -> list[str]:
    """An item listed twice on one floor."""
    problems: list[str] = []
    for room, labels in floor_labels(plan, package).items():
        repeated = sorted({label for label in labels if labels.count(label) > 1})
        problems.extend(
            f"the item '{label}' is on the floor of '{room}' twice; list it once"
            for label in repeated
        )
    return problems


def _opens_without(e: DerivedExit, key: str, package: str) -> bool:
    """Whether a player without ``key`` can go through the exit."""
    door = e.door
    if door is None or not _is_locked_with_a_key(door):
        return True
    return full_label(door.key or "", package) != full_label(key, package)


def _is_locked_with_a_key(door: DoorSpec) -> bool:
    return door.state == "locked" and door.key is not None


def _reachable_without(exits: list[DerivedExit], entry: str, key: str, package: str) -> set[str]:
    """The rooms a player reaches from the entry without the key."""
    seen = {entry}
    todo = [entry]
    while todo:
        room = todo.pop()
        for e in exits:
            if e.room == room and _opens_without(e, key, package):
                todo.extend(to for to in e.destinations if to not in seen)
                seen.update(e.destinations)
    return seen


def key_reach_problem(grid: Grid, entry: str, key: tuple[str, str], package: str) -> str:
    """A key, by (id, room), that lies only behind the doors it opens: no one could pick it up."""
    key_id, room = key
    if room in _reachable_without(derive_exits(grid), entry, key_id, package):
        return ""
    return (
        f"the key '{key_id}' lies in '{room}', which the player reaches only through a door "
        "it opens; put it in a room reachable without it"
    )


def key_reach_problems(grid: Grid, plan: AreaPlan, package: str) -> list[str]:
    """Every planned key that lies behind its own door."""
    planned = {room.id for room in plan.rooms}
    problems = [
        key_reach_problem(grid, plan.entry, (key.id, key.room), package)
        for key in plan.keys
        if key.room is not None and key.room in planned
    ]
    return [p for p in problems if p]


def plan_floor_problems(ctx: Context, plan: AreaPlan) -> list[str]:
    """Every problem with where the plan's keys and floor items lie."""
    planned = {room.id for room in plan.rooms}
    created = (item.item for item in new_floor_items(ctx, plan))
    known = known_items(ctx, [*(key.id for key in plan.keys), *created])
    problems = _key_room_problems(plan)
    problems.extend(_floor_item_problem(known, item, planned, ctx.package) for item in plan.items)
    problems.extend(_repeat_problems(plan, ctx.package))
    return [p for p in problems if p]
