"""The skill mode's plan and turn shapes, and the checks run on them before any prose is spent.

A planned skill is checked against the world's schema, features, effect
primitives and plugin effects, and its unrecognized lines against the
placeholders the engine fills. A turn's edits are checked in order against the
package's skills, so an edit may name a skill an earlier edit of the turn added.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal, TypeGuard

from pydantic import BaseModel, Field

from gups_builder.engine.api import ApiEntry, ApiModel
from gups_builder.engine.world import WorldDescription
from gups_builder.grid.place import room_id
from gups_builder.modes.common import Context, Turn, allowed_values
from gups_builder.star.values import Value, is_editable

_ID = re.compile(r"[a-z][a-z0-9_]*")
_PLACEHOLDER = re.compile(r"\{(\w+)\}")
_ALLOWED_PLACEHOLDERS = ("actor", "target")
_LINES = ("noun", "begin", "room", "victim", "resisted_room", "resisted_victim")
_TRACK_RANGES = ("melee", "near", "far")


class EffectSpec(BaseModel):
    primitive: str
    args: dict[str, Value] = {}
    effects: list[EffectSpec] = []


class Teach(BaseModel):
    mob: str
    proficiency: int


class PlannedSkill(BaseModel):
    id: str
    concept: str
    kind: str
    cost: dict[str, int] = {}
    warmup: int = 0
    lag: int = 4
    range: str | None = None
    target: str
    channels: list[str] = []
    tags: list[str] = []
    interrupt: dict[str, Value] = {}
    effect: EffectSpec | None = None
    teach: list[Teach] = []
    todos: list[str] = []


class UnrecognizedProse(BaseModel):
    noun: str | None = None
    begin: str | None = None
    room: str | None = None
    victim: str | None = None
    resisted_room: str | None = None
    resisted_victim: str | None = None


class AddSkill(BaseModel):
    edit: Literal["add_skill"] = "add_skill"
    skill: PlannedSkill


class RemoveSkill(BaseModel):
    edit: Literal["remove_skill"] = "remove_skill"
    id: str


class SetSkill(BaseModel):
    edit: Literal["set_skill"] = "set_skill"
    id: str
    fields: dict[str, Value] = {}


class SetEffect(BaseModel):
    edit: Literal["set_effect"] = "set_effect"
    id: str
    effect: EffectSpec


class TeachEdit(BaseModel):
    edit: Literal["teach"] = "teach"
    id: str
    mob: str
    proficiency: int


class Rewrite(BaseModel):
    edit: Literal["rewrite"] = "rewrite"
    ids: list[str]
    guidance: str


SkillEdit = Annotated[
    AddSkill | RemoveSkill | SetSkill | SetEffect | TeachEdit | Rewrite,
    Field(discriminator="edit"),
]


class SkillTurn(Turn):
    edits: list[SkillEdit] = []


# -- labels --------------------------------------------------------------------


def full_label(label: str, package: str) -> str:
    """A label in full: a bare id or ``:id`` is taken to be in this package."""
    if label.startswith("//"):
        return label
    return f"//{package}:{label.removeprefix(':')}"


def local_id(label: str, package: str) -> str | None:
    """The id of a label in this package, or None for another package's label.

    A bare id and a ``:id`` are taken to be in this package, the same way
    ``full_label`` resolves them.
    """
    if label.startswith("//"):
        prefix = f"//{package}:"
        return label[len(prefix) :] if label.startswith(prefix) else None
    return label.removeprefix(":")


def declared_ids(ctx: Context) -> set[str]:
    """Every id the package declares; labels are package-wide, whatever the kind."""
    ids = {d.id for d in ctx.view.decls}
    ids |= {o.id for o in ctx.view.opaque if o.id is not None}
    return ids


def _mob_ids(ctx: Context) -> set[str]:
    """The ids of the package's mobs, from its files and from describe-world."""
    ids = {d.id for d in ctx.view.decls if d.kind == "mob"}
    ids |= {room_id(m.label) for m in ctx.world.mobs_in(ctx.package)}
    return ids


# -- checks --------------------------------------------------------------------


def _effect_entry(api: ApiModel, primitive: str) -> ApiEntry | None:
    return None if "." in primitive else api.effect(primitive)


def _is_plugin_effect(world: WorldDescription, primitive: str) -> bool:
    if "." not in primitive:
        return False
    plugin_id, name = primitive.split(".", 1)
    plugin = next((p for p in world.plugins if p.id == plugin_id), None)
    return plugin is not None and name in plugin.effects


def _damage_type_name(value: Value) -> str | None:
    """The damage type of a ``type`` argument, whatever form the model wrote it in."""
    if isinstance(value, dict):
        const = value.get("$const")
        if isinstance(const, str) and const.startswith("DAMAGE."):
            return const.removeprefix("DAMAGE.")
        return None
    if isinstance(value, str):
        return value
    return None


# The primitives each target kind's effect may use, besides plugin effects, and
# the problem a primitive outside them is reported as.
_TARGET_PRIMITIVES = {
    "room": (
        ("apply_affect", "dispel", "sequence"),
        "a room skill's effect may use only apply_affect, dispel, sequence and plugin effects",
    ),
    "item": (
        ("apply_affect", "sequence"),
        "an item skill's effect may use only apply_affect, sequence and plugin effects",
    ),
}


def _effect_problems(ctx: Context, effect: EffectSpec, target: str | None) -> list[str]:
    """Problems with one effect, recursively, against the world's API and schema."""
    entry = _effect_entry(ctx.api, effect.primitive)
    plugin = _is_plugin_effect(ctx.world, effect.primitive)
    if entry is None and not plugin:
        return [f"the effect '{effect.primitive}' is not an effect of this world"]
    problems = [
        f"the effect '{effect.primitive}' argument '{arg}' must be a plain value, not code"
        for arg, value in effect.args.items()
        if not is_editable(value)
    ]
    if entry is not None:
        problems.extend(_effect_api_problems(ctx.world, effect, entry))
    problems.extend(_sequence_problems(effect))
    for sub in effect.effects:
        problems.extend(_effect_problems(ctx, sub, target))
    if not plugin:
        problems.extend(_target_primitive_problems(effect.primitive, target))
    if effect.primitive == "damage":
        problems.extend(_damage_type_problems(ctx.world, effect.args.get("type")))
    return problems


def _target_primitive_problems(primitive: str, target: str | None) -> list[str]:
    """A room or item skill's effect may use only some primitives (plugin ones aside)."""
    allowed, problem = _TARGET_PRIMITIVES.get(target or "", (None, ""))
    return [problem] if allowed is not None and primitive not in allowed else []


def _sequence_problems(effect: EffectSpec) -> list[str]:
    """Only sequence combines effects, and it needs at least one."""
    if effect.primitive == "sequence":
        return [] if effect.effects else ["the 'sequence' effect needs at least one effect"]
    if effect.effects:
        return [f"the '{effect.primitive}' effect takes no effects; only sequence combines effects"]
    return []


def _effect_api_problems(world: WorldDescription, effect: EffectSpec, entry: ApiEntry) -> list[str]:
    """The effect's feature and argument names against its api.json entry."""
    problems: list[str] = []
    if entry.feature is not None and not world.has_feature(entry.feature):
        problems.append(
            f"the effect '{effect.primitive}' needs the {entry.feature} feature, "
            "which this world has off"
        )
    params = {p.name: p for p in entry.params}
    for arg in effect.args:
        param = params.get(arg)
        if param is None:
            problems.append(f"the effect '{effect.primitive}' has no argument '{arg}'")
        elif param.feature is not None and not world.has_feature(param.feature):
            problems.append(
                f"the effect '{effect.primitive}' argument '{arg}' needs the "
                f"{param.feature} feature, which this world has off"
            )
    return problems


def _damage_type_problems(world: WorldDescription, value: Value) -> list[str]:
    type_name = _damage_type_name(value)
    damage_names = [d.name for d in world.schema.damage_types]
    if type_name is None or type_name in damage_names:
        return []
    return [
        f"'{type_name}' is not a damage type of this world's schema; the damage "
        f"types are {', '.join(damage_names) or 'none'}"
    ]


def ranges(ctx: Context) -> list[str]:
    """The ranges this world accepts: the API's, less the track bands when track is off."""
    allowed = allowed_values(ctx.api, ctx.world, "skill", "range")
    if ctx.world.has_feature("track"):
        return allowed
    return [r for r in allowed if r not in _TRACK_RANGES]


def _range_problems(ctx: Context, range_name: str | None) -> list[str]:
    if range_name is None:
        return []
    if range_name in _TRACK_RANGES and not ctx.world.has_feature("track"):
        return [f"the world has the track feature off; the range '{range_name}' needs it"]
    allowed = ranges(ctx)
    if range_name not in allowed:
        return [
            f"the range '{range_name}' is not an allowed range; "
            f"the allowed ranges are {', '.join(allowed) or 'none'}"
        ]
    return []


def _target_problems(ctx: Context, target: str) -> list[str]:
    allowed = allowed_values(ctx.api, ctx.world, "skill", "target")
    if target not in allowed:
        return [
            f"the target '{target}' is not an allowed target; "
            f"the allowed targets are {', '.join(allowed) or 'none'}"
        ]
    return []


def _cost_problems(ctx: Context, cost: dict[str, int]) -> list[str]:
    world = ctx.world
    vital_names = [v.name for v in world.schema.vitals]
    return [
        f"'{vital}' is not a vital of this world's schema; "
        f"the vitals are {', '.join(vital_names) or 'none'}"
        for vital in cost
        if vital not in vital_names
    ]


def skill_problems(ctx: Context, skill: PlannedSkill, taken: set[str]) -> list[str]:
    """Every problem with the skill, so a bad plan is repaired before any prose is spent.

    ``taken`` is every id the package already declares: a new skill may use none
    of them, since labels are package-wide.
    """
    problems: list[str] = []
    if not _ID.fullmatch(skill.id):
        problems.append(f"the id '{skill.id}' is not snake_case ([a-z][a-z0-9_]*)")
    elif skill.id in taken:
        problems.append(f"the id '{skill.id}' is already declared in the package")
    problems.extend(_range_problems(ctx, skill.range))
    problems.extend(_target_problems(ctx, skill.target))
    problems.extend(_cost_problems(ctx, skill.cost))
    problems.extend(
        f"the interrupt entry '{name}' must be a plain value, not code"
        for name, value in skill.interrupt.items()
        if not is_editable(value)
    )
    if skill.effect is not None:
        problems.extend(_effect_problems(ctx, skill.effect, skill.target))
    mob_ids = _mob_ids(ctx)
    for teach in skill.teach:
        problems.extend(_teach_problems(ctx, mob_ids, teach.mob, teach.proficiency))
    return problems


def _teach_problems(ctx: Context, mob_ids: set[str], mob: str, proficiency: int) -> list[str]:
    problems: list[str] = []
    local = local_id(mob, ctx.package)
    if local is None:
        problems.append(f"the teach's mob '{mob}' is not a mob of this package")
    elif local not in mob_ids:
        problems.append(f"the teach names the unknown mob '{mob}'")
    if not 0 <= proficiency <= 100:
        problems.append(f"the proficiency {proficiency} for '{mob}' is outside 0-100")
    return problems


def _target_placeholder_problems(skill: PlannedSkill, line: str) -> list[str]:
    """Where ``{target}`` cannot appear: a skill with no single target."""
    if skill.target in ("self", "none", "room"):
        return [
            f"the unrecognized line '{line}' uses {{target}}, "
            f"but the skill's target is '{skill.target}'"
        ]
    if line == "room" and skill.range == "room":
        return [
            "the unrecognized line 'room' uses {target}, "
            "but a room-range skill has no single target"
        ]
    return []


def _line_problems(skill: PlannedSkill, line: str, value: str) -> list[str]:
    """One unrecognized line: one non-empty line, with only the known placeholders."""
    if not value.strip():
        return [f"the unrecognized line '{line}' is empty"]
    if "\n" in value:
        return [f"the unrecognized line '{line}' must be one line"]
    problems = [
        f"the unrecognized line '{line}' has the unknown placeholder '{{{ph}}}'"
        for ph in _PLACEHOLDER.findall(value)
        if ph not in _ALLOWED_PLACEHOLDERS
    ]
    if "{target}" in value:
        problems.extend(_target_placeholder_problems(skill, line))
    return problems


def prose_problems(skill: PlannedSkill, prose: UnrecognizedProse) -> list[str]:
    problems = [p for line, text in prose_lines(prose) for p in _line_problems(skill, line, text)]
    if skill.target != "enemy":
        problems.extend(
            f"the unrecognized line '{line}' is set, "
            "but only an enemy skill may have resisted lines"
            for line in ("resisted_room", "resisted_victim")
            if getattr(prose, line) is not None
        )
    return problems


def prose_lines(prose: UnrecognizedProse) -> list[tuple[str, str]]:
    """The unrecognized lines the prose has, in declaration order."""
    lines = [(line, getattr(prose, line)) for line in _LINES]
    return [(line, text) for line, text in lines if isinstance(text, str)]


def _skill_field_features(ctx: Context) -> dict[str, str | None]:
    """Each skill declaration field to the feature it needs, or None."""
    try:
        entry = ctx.api.declaration("skill")
    except KeyError:
        return {}
    return {p.name: p.feature for p in entry.params}


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


def _whole_number_problems(ctx: Context, name: str, value: Value) -> list[str]:
    if _is_whole_number(value):
        return []
    return [f"the skill field '{name}' must be a whole number"]


def _cost_field_problems(ctx: Context, name: str, value: Value) -> list[str]:
    cost = _int_kwargs(value, "vitals")
    if cost is None:
        return ["the skill field 'cost' must be written as vitals(<vital> = <number>, ...)"]
    return _cost_problems(ctx, cost)


def _range_field_problems(ctx: Context, name: str, value: Value) -> list[str]:
    if not isinstance(value, str):
        return ["the skill field 'range' must be a string"]
    return _range_problems(ctx, value)


def _target_field_problems(ctx: Context, name: str, value: Value) -> list[str]:
    if not isinstance(value, str):
        return ["the skill field 'target' must be a string"]
    return _target_problems(ctx, value)


def _string_list_problems(ctx: Context, name: str, value: Value) -> list[str]:
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return []
    return [f"the skill field '{name}' must be a list of strings"]


def _dict_problems(ctx: Context, name: str, value: Value) -> list[str]:
    if isinstance(value, dict) and isinstance(value.get("$dict"), list):
        return []
    return [f"the skill field '{name}' must be a dict"]


def _effect_field_problems(ctx: Context, name: str, value: Value) -> list[str]:
    if isinstance(value, dict) and isinstance(value.get("$call"), str):
        return []
    return ["the skill field 'effect' must be an effect call"]


# The shape each set_skill field's value must have; fields not listed are left to lint.
_FIELD_SHAPES: dict[str, Callable[[Context, str, Value], list[str]]] = {
    "warmup": _whole_number_problems,
    "lag": _whole_number_problems,
    "cost": _cost_field_problems,
    "range": _range_field_problems,
    "target": _target_field_problems,
    "channels": _string_list_problems,
    "tags": _string_list_problems,
    "interrupt": _dict_problems,
    "unrecognized": _dict_problems,
    "effect": _effect_field_problems,
}


@dataclass
class _TurnScope:
    """What a turn's edits are checked against; add and remove edits update it in order."""

    skills: set[str]
    opaque: set[str]
    taken: set[str]
    mob_ids: set[str]
    fields: dict[str, str | None]


def _missing_skill(scope: _TurnScope, skill_id: str) -> list[str]:
    if skill_id in scope.skills:
        return []
    return [f"there is no skill '{skill_id}' in this package"]


def _set_skill_problems(ctx: Context, scope: _TurnScope, edit: SetSkill) -> list[str]:
    problems = _missing_skill(scope, edit.id)
    for name, value in edit.fields.items():
        if name not in scope.fields:
            problems.append(f"'{name}' is not a field of the skill declaration")
            continue
        feature = scope.fields[name]
        if feature is not None and not ctx.world.has_feature(feature):
            problems.append(
                f"the skill field '{name}' needs the {feature} feature, which this world has off"
            )
        if not is_editable(value):
            problems.append(f"the skill field '{name}' is computed by code; it cannot be set")
        shape = _FIELD_SHAPES.get(name)
        if shape is not None:
            problems.extend(shape(ctx, name, value))
    return problems


def _edit_problems(ctx: Context, scope: _TurnScope, edit: SkillEdit) -> list[str]:
    """One edit's problems; an add or remove updates ``scope`` for the edits after it."""
    if isinstance(edit, AddSkill):
        problems = skill_problems(ctx, edit.skill, scope.taken)
        scope.skills.add(edit.skill.id)
        scope.taken.add(edit.skill.id)
        return problems
    if isinstance(edit, RemoveSkill):
        problems = _missing_skill(scope, edit.id)
        scope.skills.discard(edit.id)
        return problems
    if isinstance(edit, SetSkill):
        return _set_skill_problems(ctx, scope, edit)
    if isinstance(edit, SetEffect):
        return _missing_skill(scope, edit.id) + _effect_problems(
            ctx, edit.effect, str_arg(ctx, edit.id, "target")
        )
    if isinstance(edit, TeachEdit):
        # The skill itself is not edited, so it need not be declared here: the
        # teach adds a label to the mob's skills dict and the staged lint decides
        # whether that label resolves.
        return _teach_problems(ctx, scope.mob_ids, edit.mob, edit.proficiency)
    return [p for rid in edit.ids for p in _missing_skill(scope, rid)]


def turn_problems(ctx: Context, turn: SkillTurn) -> list[str]:
    if turn.kind != "edit":
        return []
    scope = _TurnScope(
        skills={d.id for d in ctx.view.decls if d.kind == "skill"},
        opaque={o.id for o in ctx.view.opaque if o.kind == "skill" and o.id is not None},
        taken=declared_ids(ctx),
        mob_ids=_mob_ids(ctx),
        fields=_skill_field_features(ctx),
    )
    problems: list[str] = []
    for edit in turn.edits:
        problems.extend(_edit_problems(ctx, scope, edit))
        problems.extend(
            f"'{rid}' is not a plain declaration in this package; it cannot be edited"
            for rid in _edit_skill_ids(edit)
            if rid in scope.opaque
        )
    return problems


def _edit_skill_ids(edit: SkillEdit) -> list[str]:
    if isinstance(edit, (RemoveSkill, SetSkill, SetEffect)):
        return [edit.id]
    if isinstance(edit, Rewrite):
        return list(edit.ids)
    return []


def str_arg(ctx: Context, skill_id: str, name: str) -> str | None:
    """A skill's argument from the file, when it is a plain string."""
    decl = ctx.view.get("skill", skill_id)
    value = decl.args.get(name) if decl is not None else None
    return value if isinstance(value, str) else None
