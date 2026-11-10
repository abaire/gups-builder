"""The mob mode's plan and turn shapes, and the checks run on them before any prose is spent.

A planned mob is checked against the world's schema, features, skills,
factions, plugin traits and the package's rooms. A turn's edits are checked in
order against the package's mobs, so an edit may name a mob an earlier edit of
the same turn added.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal, TypeGuard

from pydantic import BaseModel, Field

from gups_builder.engine.api import ApiEntry
from gups_builder.engine.world import WorldDescription
from gups_builder.grid.place import room_id
from gups_builder.modes.area_checks import ItemProse
from gups_builder.modes.common import Context, Turn, param_allowed
from gups_builder.star.reader import DeclView
from gups_builder.star.values import Value, is_editable

_ID = re.compile(r"[a-z][a-z0-9_]*")


class BehaviorSpec(BaseModel):
    name: str
    args: dict[str, Value] = {}


class LootSpec(BaseModel):
    item: str
    chance: int = 100
    concept: str | None = None
    kind: str = "misc"


class SpawnSpec(BaseModel):
    room: str
    count: int = 1
    respawn_pulses: int | None = None


class PlannedMob(BaseModel):
    id: str
    concept: str
    level: int | None = None
    stats: dict[str, int] = {}
    vitals: dict[str, int] = {}
    resists: dict[str, int] = {}
    skills: dict[str, int] = {}
    senses: dict[str, int] = {}
    behaviors: list[BehaviorSpec] = []
    factions: list[str] = []
    traits: list[str] = []
    loot: list[LootSpec] = []
    spawns: list[SpawnSpec] = []
    todos: list[str] = []


class MobPlan(BaseModel):
    mobs: list[PlannedMob]


class MobProse(BaseModel):
    name: str
    keywords: list[str]
    desc: str


class AddMob(BaseModel):
    edit: Literal["add_mob"] = "add_mob"
    mob: PlannedMob


class RemoveMob(BaseModel):
    edit: Literal["remove_mob"] = "remove_mob"
    id: str


class SetMob(BaseModel):
    edit: Literal["set_mob"] = "set_mob"
    id: str
    fields: dict[str, Value] = {}


class AddLoot(BaseModel):
    edit: Literal["add_loot"] = "add_loot"
    id: str
    loot: LootSpec


class RemoveLoot(BaseModel):
    edit: Literal["remove_loot"] = "remove_loot"
    id: str
    item: str


class Place(BaseModel):
    edit: Literal["place"] = "place"
    spawn: SpawnSpec
    mob: str


class Unplace(BaseModel):
    edit: Literal["unplace"] = "unplace"
    room: str
    mob: str


class Rewrite(BaseModel):
    edit: Literal["rewrite"] = "rewrite"
    ids: list[str]
    guidance: str


MobEdit = Annotated[
    AddMob | RemoveMob | SetMob | AddLoot | RemoveLoot | Place | Unplace | Rewrite,
    Field(discriminator="edit"),
]


class MobTurn(Turn):
    edits: list[MobEdit] = []


# -- labels ----------------------------------------------------------------------


def full_label(label: str, package: str) -> str:
    """A label in full: a bare id or ``:id`` is taken to be in this package."""
    if label.startswith("//"):
        return label
    return f"//{package}:{label.removeprefix(':')}"


def local_id(label: str, package: str) -> str | None:
    """The id of a label in this package, or None for another package's label."""
    full = full_label(label, package)
    prefix = f"//{package}:"
    return full.removeprefix(prefix) if full.startswith(prefix) else None


def _package_labels(ctx: Context, kind: str, world_labels: list[str]) -> set[str]:
    """Full labels of one kind: the world's, plus the package's files, which may be newer."""
    labels = set(world_labels)
    labels |= {f"//{ctx.package}:{d.id}" for d in ctx.view.decls if d.kind == kind}
    return labels


def _item_labels(ctx: Context) -> set[str]:
    return _package_labels(ctx, "item", [i.label for i in ctx.world.items])


def _skill_labels(ctx: Context) -> set[str]:
    return _package_labels(ctx, "skill", [s.label for s in ctx.world.skills])


def _mob_labels(ctx: Context) -> set[str]:
    return _package_labels(ctx, "mob", [m.label for m in ctx.world.mobs])


def _faction_labels(ctx: Context) -> set[str]:
    return _package_labels(ctx, "faction", list(ctx.world.factions))


def declared_ids(ctx: Context) -> set[str]:
    """Every id the package declares; labels are package-wide, whatever the kind."""
    ids = {d.id for d in ctx.view.decls}
    ids |= {o.id for o in ctx.view.opaque if o.id is not None}
    return ids


def room_ids(ctx: Context) -> set[str]:
    rooms = {room_id(r.label) for r in ctx.world.rooms_in(ctx.package)}
    rooms |= {d.id for d in ctx.view.decls if d.kind == "room"}
    return rooms


# -- prose -----------------------------------------------------------------------


def prose_check(what: str) -> Callable[[MobProse | ItemProse], list[str]]:
    def check(prose: MobProse | ItemProse) -> list[str]:
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
        if not prose.keywords:
            problems.append(f"{what}: keywords is empty")
        return problems

    return check


# -- loot ------------------------------------------------------------------------


def is_new_item(ctx: Context, loot: LootSpec) -> bool:
    """A loot entry that creates its item: it has a concept and names no existing item."""
    return loot.concept is not None and full_label(loot.item, ctx.package) not in _item_labels(ctx)


def new_item_id(ctx: Context, loot: LootSpec) -> str | None:
    """The id of the item a loot entry creates in this package, or None when it creates none."""
    return local_id(loot.item, ctx.package) if is_new_item(ctx, loot) else None


def new_items(ctx: Context, mobs: list[PlannedMob]) -> dict[str, LootSpec]:
    """The items the mobs' loot creates, by id; a shared item is created once."""
    items: dict[str, LootSpec] = {}
    for mob in mobs:
        for loot in mob.loot:
            new_id = new_item_id(ctx, loot)
            if new_id is not None:
                items.setdefault(new_id, loot)
    return items


def _new_item_id_problems(
    item: str, new_id: str | None, taken: set[str], plan_mobs: set[str]
) -> list[str]:
    if new_id is None or not _ID.fullmatch(new_id):
        return [
            f"the loot item '{item}' is not an item of the world and not a "
            "valid new id; use lower-case letters, digits and underscores"
        ]
    if new_id in plan_mobs:
        return [f"the new item id '{new_id}' is also a mob of the plan"]
    if new_id in taken:
        return [f"the new item id '{new_id}' is already declared in the package"]
    return []


def _loot_item_problems(
    ctx: Context, loot: LootSpec, taken: set[str], plan_mobs: set[str]
) -> list[str]:
    """The loot's item: an existing item, or a new one with a free, valid id."""
    if loot.concept is None:
        if full_label(loot.item, ctx.package) in _item_labels(ctx):
            return []
        return [
            f"the loot item '{loot.item}' is not an item of the world; "
            "give it a concept to create it"
        ]
    if not is_new_item(ctx, loot):
        return []
    return _new_item_id_problems(loot.item, local_id(loot.item, ctx.package), taken, plan_mobs)


def loot_problems(ctx: Context, loot: LootSpec, taken: set[str], plan_mobs: set[str]) -> list[str]:
    """Problems with one loot entry; ``taken`` holds the ids a new item may not use."""
    problems = _loot_item_problems(ctx, loot, taken, plan_mobs)
    if not 1 <= loot.chance <= 100:
        problems.append(f"the loot chance {loot.chance} for '{loot.item}' is outside 1-100")
    return problems


# -- a mob's fields --------------------------------------------------------------


def _feature_off(world: WorldDescription, feature: str | None) -> bool:
    return feature is not None and not world.has_feature(feature)


def _behavior_arg_problems(
    world: WorldDescription, behavior: str, entry: ApiEntry, arg: str
) -> list[str]:
    param = next((p for p in entry.params if p.name == arg), None)
    if param is None:
        return [f"the behavior '{behavior}' has no argument '{arg}'"]
    if _feature_off(world, param.feature):
        return [
            f"the behavior '{behavior}' argument '{arg}' needs the "
            f"{param.feature} feature, which this world has off"
        ]
    return []


def _behavior_spec_problems(ctx: Context, spec: BehaviorSpec) -> list[str]:
    entry = ctx.api.behavior(spec.name)
    if entry is None:
        return [f"the behavior '{spec.name}' is not in this world's API"]
    problems: list[str] = []
    if _feature_off(ctx.world, entry.feature):
        problems.append(
            f"the behavior '{spec.name}' needs the {entry.feature} feature, "
            "which this world has off"
        )
    for arg in spec.args:
        problems.extend(_behavior_arg_problems(ctx.world, spec.name, entry, arg))
    return problems


def _behavior_problems(ctx: Context, mob: PlannedMob) -> list[str]:
    return [p for spec in mob.behaviors for p in _behavior_spec_problems(ctx, spec)]


def _schema_problems(ctx: Context, mob: PlannedMob) -> list[str]:
    """Stat, vital, resist and sense names the world's schema does not have."""
    schema = ctx.world.schema
    checks: list[tuple[dict[str, int], list[str], str, str]] = [
        (mob.stats, [s.name for s in schema.stats], "stat", "stats"),
        (mob.vitals, [v.name for v in schema.vitals], "vital", "vitals"),
        (mob.resists, [d.name for d in schema.damage_types], "damage type", "damage types"),
        (mob.senses, list(schema.channels), "sense channel", "channels"),
    ]
    return [
        f"'{name}' is not a {singular} of this world's schema; the {plural} are "
        f"{', '.join(allowed) or 'none'}"
        for used, allowed, singular, plural in checks
        for name in used
        if name not in allowed
    ]


def _level_problems(ctx: Context, mob: PlannedMob) -> list[str]:
    if mob.level is not None and not ctx.world.has_feature("leveling"):
        return ["the world has the leveling feature off; the mob may not have a level"]
    return []


def _known_skill_problems(ctx: Context, mob: PlannedMob) -> list[str]:
    if not mob.skills:
        return []
    if not ctx.world.has_feature("skills"):
        return ["the world has the skills feature off; the mob may not know skills"]
    labels = _skill_labels(ctx)
    return [
        f"the skill '{label}' is not a skill of this world"
        for label in mob.skills
        if full_label(label, ctx.package) not in labels
    ]


def _proficiency_problems(ctx: Context, mob: PlannedMob) -> list[str]:
    return [
        f"the proficiency {prof} for '{label}' is outside 0-100"
        for label, prof in mob.skills.items()
        if not 0 <= prof <= 100
    ]


def _faction_problems(ctx: Context, mob: PlannedMob) -> list[str]:
    if not mob.factions:
        return []
    if not param_allowed(ctx.api, ctx.world, "mob", "factions"):
        return ["the world has the standing feature off; the mob may not have factions"]
    factions = _faction_labels(ctx)
    return [
        f"the faction '{faction}' is not a faction of this world"
        for faction in mob.factions
        if full_label(faction, ctx.package) not in factions
    ]


def _trait_problems(ctx: Context, mob: PlannedMob) -> list[str]:
    plugin_traits = {t for p in ctx.world.plugins for t in p.traits}
    return [
        f"the trait '{trait}' is declared by no plugin of this world"
        for trait in mob.traits
        if trait not in plugin_traits
    ]


def _spawn_problems(ctx: Context, mob: PlannedMob) -> list[str]:
    rooms = room_ids(ctx)
    problems = [
        f"the spawn names the unknown room '{spawn.room}'"
        for spawn in mob.spawns
        if spawn.room not in rooms
    ]
    problems.extend(
        f"the spawn in '{spawn.room}' needs a count of 1 or more"
        for spawn in mob.spawns
        if spawn.count < 1
    )
    return problems


# The field checks, in the order their problems are reported.
_FIELD_CHECKS: tuple[Callable[[Context, PlannedMob], list[str]], ...] = (
    _schema_problems,
    _level_problems,
    _known_skill_problems,
    _proficiency_problems,
    _behavior_problems,
    _faction_problems,
    _trait_problems,
    _spawn_problems,
)


def field_problems(ctx: Context, mob: PlannedMob) -> list[str]:
    """Problems with a mob's declaration fields against the world; not its id or loot."""
    return [p for check in _FIELD_CHECKS for p in check(ctx, mob)]


# -- the plan --------------------------------------------------------------------


def _mob_id_problems(mob_id: str, seen: set[str], taken: set[str]) -> list[str]:
    if not _ID.fullmatch(mob_id):
        return [f"the id '{mob_id}' is not snake_case ([a-z][a-z0-9_]*)"]
    if mob_id in seen:
        return [f"the id '{mob_id}' is used twice in the plan"]
    if mob_id in taken:
        return [f"the id '{mob_id}' is already declared in the package"]
    return []


def plan_problems(ctx: Context, plan: MobPlan, taken: set[str]) -> list[str]:
    """Every problem with the plan, so a bad plan is repaired before any prose is spent.

    ``taken`` is every id the package already declares: a new mob or item may
    use none of them.
    """
    plan_mobs = {m.id for m in plan.mobs}
    seen: set[str] = set()
    problems: list[str] = [] if plan.mobs else ["the plan has no mobs"]
    for mob in plan.mobs:
        problems.extend(_mob_id_problems(mob.id, seen, taken))
        seen.add(mob.id)
        problems.extend(field_problems(ctx, mob))
        for loot in mob.loot:
            problems.extend(loot_problems(ctx, loot, taken, plan_mobs))
    return problems


# -- set_mob field shapes --------------------------------------------------------


def _is_whole_number(value: Value) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_bare_call(value: Value, callee: str) -> TypeGuard[dict[str, Value]]:
    """``callee(...)`` with no positional arguments."""
    return isinstance(value, dict) and value.get("$call") == callee and not value.get("args")


def _int_kwargs(value: Value, callee: str) -> dict[str, int] | None:
    """``callee(name = <int>, ...)`` as a dict, or None for any other shape."""
    if not _is_bare_call(value, callee):
        return None
    kwargs = value.get("kwargs")
    if not isinstance(kwargs, dict):
        return None
    numbers = {k: v for k, v in kwargs.items() if _is_whole_number(v)}
    return numbers if len(numbers) == len(kwargs) else None


def _int_pair(pair: Value) -> tuple[str, int] | None:
    """A ``[key, <int>]`` pair of a ``$dict``, or None for any other shape."""
    if not isinstance(pair, list) or len(pair) != 2:
        return None
    key, number = pair
    if isinstance(key, str) and _is_whole_number(number):
        return key, number
    return None


def _int_dict(value: Value) -> dict[str, int] | None:
    """``{"key": <int>, ...}`` as a dict, or None for any other shape."""
    pairs = value.get("$dict") if isinstance(value, dict) else None
    if not isinstance(pairs, list):
        return None
    out: dict[str, int] = {}
    for pair in pairs:
        entry = _int_pair(pair)
        if entry is None:
            return None
        out[entry[0]] = entry[1]
    return out


def _behavior_spec(item: Value) -> BehaviorSpec | None:
    """A behavior call as a spec, or None for anything that is not a named call."""
    if not isinstance(item, dict):
        return None
    name = item.get("$call")
    if not isinstance(name, str):
        return None
    kwargs = item.get("kwargs")
    return BehaviorSpec(name=name, args=dict(kwargs) if isinstance(kwargs, dict) else {})


# A field's checkable form (None to leave it out) and the problems with its shape.
_Shaped = tuple[object | None, list[str]]


def _level_shape(name: str, value: Value) -> _Shaped:
    if _is_whole_number(value):
        return value, []
    return None, ["the mob field 'level' must be a whole number"]


def _kwargs_shape(name: str, value: Value) -> _Shaped:
    numbers = _int_kwargs(value, name)
    if numbers is None:
        return None, [f"the mob field '{name}' must be written as {name}(<name> = <number>, ...)"]
    return numbers, []


def _dict_shape(name: str, value: Value) -> _Shaped:
    numbers = _int_dict(value)
    if numbers is None:
        return None, [f"the mob field '{name}' must be a dict of name to number"]
    return numbers, []


def _behaviors_shape(name: str, value: Value) -> _Shaped:
    if not isinstance(value, list):
        return None, []
    specs = [_behavior_spec(item) for item in value]
    return [s for s in specs if s is not None], []


def _strings_shape(name: str, value: Value) -> _Shaped:
    if not isinstance(value, list):
        return None, []
    return [v for v in value if isinstance(v, str)], []


# The set_mob fields the plan checks cover; any other (name, desc, ...) is left to lint.
_SET_MOB_SHAPES: dict[str, Callable[[str, Value], _Shaped]] = {
    "level": _level_shape,
    "stats": _kwargs_shape,
    "vitals": _kwargs_shape,
    "resists": _kwargs_shape,
    "skills": _dict_shape,
    "senses": _dict_shape,
    "behaviors": _behaviors_shape,
    "factions": _strings_shape,
    "traits": _strings_shape,
}


def _set_mob_as_plan(edit: SetMob) -> tuple[PlannedMob, list[str]]:
    """The checkable fields of a set_mob as a plan entry, plus problems with their shape.

    The fields go through the same world checks as a planned mob; a field the
    checks don't cover (name, desc, ...) is left to lint.
    """
    fields: dict[str, object] = {}
    problems: list[str] = []
    for name, value in edit.fields.items():
        shape = _SET_MOB_SHAPES.get(name)
        if shape is None:
            continue
        checkable, shape_problems = shape(name, value)
        problems.extend(shape_problems)
        if checkable is not None:
            fields[name] = checkable
    mob = PlannedMob.model_validate({"id": edit.id, "concept": "", **fields})
    return mob, problems


# -- list entries in the files ---------------------------------------------------


def _first_arg_label(entry: Value, callee: str) -> str | None:
    """The label ``callee(<label>, ...)`` starts with, or None for any other shape."""
    if not isinstance(entry, dict) or entry.get("$call") != callee:
        return None
    args = entry.get("args")
    if not isinstance(args, list) or not args:
        return None
    first = args[0]
    return first if isinstance(first, str) else None


def _list_entry(entries: Value, callee: str, label: str, package: str) -> str | None:
    """The label of the ``callee(...)`` entry of the list that names ``label``, as written."""
    if not isinstance(entries, list):
        return None
    wanted = full_label(label, package)
    for entry in entries:
        first = _first_arg_label(entry, callee)
        if first is not None and full_label(first, package) == wanted:
            return first
    return None


def loot_entry(decl: DeclView | None, item: str, package: str) -> str | None:
    """The label of the mob's loot entry for the item, as the file writes it."""
    return _list_entry(decl.args.get("loot") if decl is not None else None, "loot", item, package)


def spawn_entry(decl: DeclView | None, mob: str, package: str) -> str | None:
    """The label of the room's spawn of the mob, as the file writes it."""
    spawns = decl.args.get("spawns") if decl is not None else None
    return _list_entry(spawns, "spawn", mob, package)


def _lacks_entry(decl: DeclView | None, name: str, entry: str | None) -> bool:
    """Whether a plain list ``name`` of ``decl`` is known to hold no such entry."""
    if decl is None or not is_editable(decl.args.get(name)):
        return False
    return entry is None


# -- turns -----------------------------------------------------------------------


def _mob_field_features(ctx: Context) -> dict[str, str | None]:
    """Each mob declaration field to the feature it needs, or None."""
    try:
        entry = ctx.api.declaration("mob")
    except KeyError:
        return {}
    return {p.name: p.feature for p in entry.params}


@dataclass
class _TurnScope:
    """What a turn's edits are checked against; add and remove edits update it in order."""

    mobs: set[str]
    opaque: set[str]
    taken: set[str]
    rooms: set[str]
    fields: dict[str, str | None]
    known: set[str]


def _missing_mob(scope: _TurnScope, mob_id: str) -> list[str]:
    if mob_id in scope.mobs:
        return []
    return [f"there is no mob '{mob_id}' in this package"]


def _add_mob_problems(ctx: Context, scope: _TurnScope, edit: AddMob) -> list[str]:
    problems = plan_problems(ctx, MobPlan(mobs=[edit.mob]), scope.taken)
    scope.mobs.add(edit.mob.id)
    scope.taken.add(edit.mob.id)
    scope.known.add(full_label(edit.mob.id, ctx.package))
    scope.taken |= set(new_items(ctx, [edit.mob]))
    return problems


def _remove_mob_problems(scope: _TurnScope, edit: RemoveMob) -> list[str]:
    problems = _missing_mob(scope, edit.id)
    scope.mobs.discard(edit.id)
    return problems


def _set_field_problems(ctx: Context, scope: _TurnScope, name: str, value: Value) -> list[str]:
    if name not in scope.fields:
        return [f"'{name}' is not a field of the mob declaration"]
    problems: list[str] = []
    feature = scope.fields[name]
    if _feature_off(ctx.world, feature):
        problems.append(
            f"the mob field '{name}' needs the {feature} feature, which this world has off"
        )
    if not is_editable(value):
        problems.append(f"the mob field '{name}' is computed by code; it cannot be set")
    return problems


def _set_mob_problems(ctx: Context, scope: _TurnScope, edit: SetMob) -> list[str]:
    problems = _missing_mob(scope, edit.id)
    for name, value in edit.fields.items():
        problems.extend(_set_field_problems(ctx, scope, name, value))
    planned, shape_problems = _set_mob_as_plan(edit)
    return problems + shape_problems + field_problems(ctx, planned)


def _add_loot_problems(ctx: Context, scope: _TurnScope, edit: AddLoot) -> list[str]:
    problems = _missing_mob(scope, edit.id) + loot_problems(ctx, edit.loot, scope.taken, set())
    new_id = new_item_id(ctx, edit.loot)
    if new_id is not None:
        scope.taken.add(new_id)
    return problems


def _remove_loot_problems(ctx: Context, scope: _TurnScope, edit: RemoveLoot) -> list[str]:
    if edit.id not in scope.mobs:
        return _missing_mob(scope, edit.id)
    decl = ctx.view.get("mob", edit.id)
    if _lacks_entry(decl, "loot", loot_entry(decl, edit.item, ctx.package)):
        return [f"the mob '{edit.id}' does not drop '{edit.item}'"]
    return []


def _spawn_target_problems(ctx: Context, scope: _TurnScope, room: str, mob: str) -> list[str]:
    problems: list[str] = []
    if room not in scope.rooms:
        problems.append(f"there is no room '{room}' in this package")
    if full_label(mob, ctx.package) not in scope.known:
        problems.append(f"there is no mob '{mob}' in this package or the world")
    return problems


def _place_problems(ctx: Context, scope: _TurnScope, edit: Place) -> list[str]:
    problems = _spawn_target_problems(ctx, scope, edit.spawn.room, edit.mob)
    if edit.spawn.count < 1:
        problems.append(f"a spawn in '{edit.spawn.room}' needs a count of 1 or more")
    return problems


def _unplace_problems(ctx: Context, scope: _TurnScope, edit: Unplace) -> list[str]:
    problems = _spawn_target_problems(ctx, scope, edit.room, edit.mob)
    if edit.room not in scope.rooms:
        return problems
    decl = ctx.view.get("room", edit.room)
    if _lacks_entry(decl, "spawns", spawn_entry(decl, edit.mob, ctx.package)):
        problems.append(f"the room '{edit.room}' does not spawn '{edit.mob}'")
    return problems


def _rewrite_problems(scope: _TurnScope, edit: Rewrite) -> list[str]:
    return [p for rid in edit.ids for p in _missing_mob(scope, rid)]


def _edit_problems(ctx: Context, scope: _TurnScope, edit: MobEdit) -> list[str]:
    """One edit's problems; an add or remove updates ``scope`` for the edits after it."""
    if isinstance(edit, AddMob):
        return _add_mob_problems(ctx, scope, edit)
    if isinstance(edit, RemoveMob):
        return _remove_mob_problems(scope, edit)
    if isinstance(edit, SetMob):
        return _set_mob_problems(ctx, scope, edit)
    if isinstance(edit, AddLoot):
        return _add_loot_problems(ctx, scope, edit)
    if isinstance(edit, RemoveLoot):
        return _remove_loot_problems(ctx, scope, edit)
    if isinstance(edit, Place):
        return _place_problems(ctx, scope, edit)
    if isinstance(edit, Unplace):
        return _unplace_problems(ctx, scope, edit)
    return _rewrite_problems(scope, edit)


def _edit_mob_ids(edit: MobEdit) -> list[str]:
    if isinstance(edit, (RemoveMob, SetMob, AddLoot, RemoveLoot)):
        return [edit.id]
    if isinstance(edit, Rewrite):
        return list(edit.ids)
    return []


def turn_problems(ctx: Context, turn: MobTurn) -> list[str]:
    if turn.kind != "edit":
        return []
    scope = _TurnScope(
        mobs={d.id for d in ctx.view.decls if d.kind == "mob"},
        opaque={o.id for o in ctx.view.opaque if o.kind == "mob" and o.id is not None},
        taken=declared_ids(ctx),
        rooms=room_ids(ctx),
        fields=_mob_field_features(ctx),
        known=_mob_labels(ctx),
    )
    problems: list[str] = []
    for edit in turn.edits:
        problems.extend(_edit_problems(ctx, scope, edit))
        problems.extend(
            f"'{rid}' is not a plain declaration in this package; it cannot be edited"
            for rid in _edit_mob_ids(edit)
            if rid in scope.opaque
        )
    return problems
