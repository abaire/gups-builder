"""The mob mode: creating mobs with loot items and placement spawns, and refining them.

The logic model briefs the request, then plans the mobs against the world's
schema, the API's behaviors and the world's mobs as balance references. The
plan is checked before any prose is spent. A follow-up message becomes a turn
whose edits are checked against the package's mobs and the world, then turned
into ops. Like every mode, the build runs inside the staged lint-and-test loop.
The plan and turn shapes and their checks are in ``mob_checks``.
"""

from __future__ import annotations

import json
import statistics
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from gups_builder.engine.api import ApiModel
from gups_builder.engine.world import MobInfo
from gups_builder.grid.place import room_id
from gups_builder.llm.client import Message
from gups_builder.llm.structured import call_structured
from gups_builder.modes import testgen
from gups_builder.modes.area_checks import ItemProse
from gups_builder.modes.common import Context, Proposal, finalize, param_allowed
from gups_builder.modes.mob_checks import (
    AddLoot,
    AddMob,
    LootSpec,
    MobEdit,
    MobPlan,
    MobProse,
    MobTurn,
    Place,
    PlannedMob,
    RemoveLoot,
    RemoveMob,
    SetMob,
    SpawnSpec,
    Unplace,
    declared_ids,
    full_label,
    loot_entry,
    new_item_id,
    new_items,
    plan_problems,
    prose_check,
    room_ids,
    spawn_entry,
    turn_problems,
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
from gups_builder.star.values import Value, is_editable
from gups_builder.star.writer import apply_ops


class MobBrief(BaseModel):
    concepts: list[str]
    notes: str = ""
    questions: list[str] = []


@dataclass
class _CreateState:
    """The brief, and what a build already produced, so a repair re-requests only what changed."""

    brief: MobBrief
    plan: MobPlan | None = None
    prose: dict[str, MobProse] = field(default_factory=dict)
    fingerprints: dict[str, str] = field(default_factory=dict)
    item_prose: dict[str, ItemProse] = field(default_factory=dict)


def create_mobs(ctx: Context, request: str) -> MobBrief | Proposal:
    """A new mob: the brief, then the plan, prose and ops inside the repair loop."""
    logic_budget = ctx.config.logic.context_tokens - ctx.config.logic.max_tokens
    brief_prompt = load_prompt(
        "mob_brief", rooms=_rooms_text(ctx), existing_mobs=_existing_mobs_text(ctx)
    )
    sections = [
        (0, request),
        (0, brief_prompt),
        (2, _balance_text(ctx)),
    ]
    brief = call_structured(
        ctx.llm,
        "logic",
        [
            Message("system", load_prompt("system_logic")),
            Message("user", fit(sections, logic_budget)),
        ],
        MobBrief,
        ctx.config.max_repairs,
    )
    if brief.questions:
        return brief
    state = _CreateState(brief)
    return finalize(ctx, lambda problems: _build(ctx, state, problems), _builder_test(ctx))


def mob_turn(ctx: Context, message: str, history: list[Message]) -> tuple[MobTurn, Proposal | None]:
    """Turn a follow-up message in mob mode into a turn and, for an edit, a proposal."""
    turn = _turn_call(ctx, message, history)
    if turn.kind != "edit":
        return turn, None
    last = turn

    def build(problems: list[str]) -> Proposal:
        nonlocal last
        if problems:
            last = _turn_call(ctx, message, history, (problems, last))
        if last.kind != "edit":
            return Proposal(
                ops=[],
                new_files={},
                summary="no changes",
                prose_preview=[],
                notes=[f"The repair answered with a {last.kind} turn: {last.reply}"],
            )
        return _edit_to_proposal(ctx, last, message)

    proposal = finalize(ctx, build, _builder_test(ctx))
    if last.kind != "edit":
        return last, None
    return last, proposal


def _short_label(label: str, package: str) -> str:
    """A label as the files write it: ``:id`` in this package, in full otherwise."""
    full = full_label(label, package)
    prefix = f"//{package}:"
    return f":{full.removeprefix(prefix)}" if full.startswith(prefix) else full


def _counted(n: int, noun: str) -> str:
    return f"{n} {noun}{'s' if n != 1 else ''}"


def _listed(items: Iterable[str]) -> str:
    """Items for a prompt, comma-separated, or ``none``."""
    return ", ".join(items) or "none"


# -- prompts -------------------------------------------------------------------


def _rooms_text(ctx: Context) -> str:
    return _listed(sorted(room_ids(ctx)))


def _existing_mobs_text(ctx: Context) -> str:
    """The package's mobs, from the files first: they may be newer than describe-world."""
    names: dict[str, str] = {}
    for d in ctx.view.decls:
        if d.kind == "mob":
            name = d.args.get("name")
            names[d.id] = name if isinstance(name, str) else "(no name)"
    for m in ctx.world.mobs_in(ctx.package):
        names.setdefault(room_id(m.label), m.name)
    return "\n".join(f"{mid}: {name}" for mid, name in names.items()) or "none"


def _numbers(d: dict[str, int]) -> str:
    return _listed(f"{k} {v}" for k, v in d.items())


def _behaviors_text(api: ApiModel) -> str:
    lines = [
        f"{entry.name} — {entry.doc} [{', '.join(p.name for p in entry.params)}]"
        for entry in api.behaviors
    ]
    body = "\n".join(lines) if lines else "none"
    return f"Behaviors available (name, doc, its arguments):\n{body}"


def _skills_text(ctx: Context) -> str:
    if not ctx.world.has_feature("skills"):
        return "none (the world has the skills feature off)"
    skills = [f"{s.label}: {s.kind}, target {s.target}" for s in ctx.world.skills]
    return "\n".join(skills) if skills else "none"


def _factions_text(ctx: Context) -> str:
    if not param_allowed(ctx.api, ctx.world, "mob", "factions"):
        return "none (the world has the standing feature off)"
    return _listed(ctx.world.factions)


def _traits_text(ctx: Context) -> str:
    return _listed(sorted(t for p in ctx.world.plugins for t in p.traits))


def _mob_line(m: MobInfo) -> str:
    return f"{m.label}: level {m.level}, stats {_numbers(m.stats)}, vitals {_numbers(m.vitals)}"


_BALANCE_OTHERS = 10


def _balance_text(ctx: Context) -> str:
    """The package's mobs and the other mobs closest to the package's level, for balance.

    Nothing is planned yet when this is built, so "closest" is measured from the
    median level of the package's mobs, or of the whole world when the package
    has none yet.
    """
    world = ctx.world
    own = world.mobs_in(ctx.package)
    others = [m for m in world.mobs if not m.label.startswith(f"//{ctx.package}:")]
    lines = [_mob_line(m) for m in own]
    reference = own or others
    if reference:
        level = statistics.median_low(m.level for m in reference)
        others.sort(key=lambda m: (abs(m.level - level), m.level, m.label))
        lines += [_mob_line(m) for m in others[:_BALANCE_OTHERS]]
    body = "\n".join(lines) if lines else "none"
    return f"Balance references (label, level, stats, vitals):\n{body}"


# -- creation -------------------------------------------------------------------


def _fingerprint(mob: PlannedMob) -> str:
    """Everything the mob's prose prompt depends on that the plan can change."""
    return json.dumps(
        [
            mob.concept,
            mob.level,
            mob.stats,
            mob.vitals,
            [(b.name, b.args) for b in mob.behaviors],
        ]
    )


def _mob_prose_call(ctx: Context, mob: PlannedMob, notes: str) -> MobProse:
    prompt = load_prompt(
        "mob_prose",
        notes=notes,
        concept=mob.concept,
        level=str(mob.level) if mob.level is not None else "none",
        stats=_numbers(mob.stats),
        vitals=_numbers(mob.vitals),
        behaviors=_listed(b.name for b in mob.behaviors),
    )
    budget = ctx.config.prose.context_tokens - ctx.config.prose.max_tokens
    return call_structured(
        ctx.llm,
        "prose",
        [
            Message("system", load_prompt("system_prose")),
            Message("user", fit([(0, prompt)], budget)),
        ],
        MobProse,
        ctx.config.max_repairs,
        extra_check=prose_check(mob.id),
    )


def _item_prose_call(ctx: Context, item_id: str, theme: str, concept: str) -> ItemProse:
    prompt = load_prompt("item_prose", theme=theme, concept=concept)
    budget = ctx.config.prose.context_tokens - ctx.config.prose.max_tokens
    return call_structured(
        ctx.llm,
        "prose",
        [
            Message("system", load_prompt("system_prose")),
            Message("user", fit([(0, prompt)], budget)),
        ],
        ItemProse,
        ctx.config.max_repairs,
        extra_check=prose_check(item_id),
    )


def _item_decl(item: ItemProse, item_id: str, kind: str) -> AddDecl:
    return AddDecl(
        file="items.star",
        kind="item",
        id=item_id,
        args={
            "name": item.name,
            "keywords": list(item.keywords),
            "desc": item.desc,
            "kind": kind,
        },
    )


def _mob_decl(ctx: Context, mob: PlannedMob, prose: MobProse) -> AddDecl:
    return AddDecl(file="mobs.star", kind="mob", id=mob.id, args=_mob_args(ctx, mob, prose))


def _preview(decl_id: str, prose: MobProse | ItemProse) -> list[tuple[str, str, str]]:
    return [(decl_id, "name", prose.name), (decl_id, "desc", prose.desc)]


def _placement_ops(ctx: Context, mob: PlannedMob) -> list[StarOp]:
    """The mob's spawns in their rooms, then its TODOs."""
    ops: list[StarOp] = [
        AppendItem(
            kind="room", id=spawn.room, name="spawns", value=_spawn_value(ctx, mob.id, spawn)
        )
        for spawn in mob.spawns
    ]
    ops.extend(AddTodo(kind="mob", id=mob.id, text=todo) for todo in mob.todos)
    return ops


def _plan_sections(ctx: Context, state: _CreateState, problems: list[str]) -> list[tuple[int, str]]:
    """The plan prompt's sections, with the repair request when lint found problems."""
    world = ctx.world
    plan_prompt = load_prompt(
        "mob_plan",
        brief=state.brief.model_dump_json(indent=2),
        stats=_listed(f"{s.name} ({s.label})" for s in world.schema.stats),
        vitals=_listed(f"{v.name} ({v.label})" for v in world.schema.vitals),
        damage_types=_listed(f"{d.name} ({d.label})" for d in world.schema.damage_types),
        channels=_listed(world.schema.channels),
        features=_listed(world.features),
        skills=_skills_text(ctx),
        factions=_factions_text(ctx),
        traits=_traits_text(ctx),
        existing_ids=_listed(sorted(declared_ids(ctx))),
    )
    sections = [
        (0, plan_prompt),
        (1, _behaviors_text(ctx.api)),
        (2, _balance_text(ctx)),
    ]
    if problems and state.plan is not None:
        repair = load_prompt(
            "repair_lint", problems="\n".join(problems), previous=state.plan.model_dump_json()
        )
        sections.append((0, repair))
    return sections


def _plan_call(ctx: Context, sections: list[tuple[int, str]]) -> MobPlan:
    logic_budget = ctx.config.logic.context_tokens - ctx.config.logic.max_tokens
    taken = declared_ids(ctx)
    return call_structured(
        ctx.llm,
        "logic",
        [
            Message("system", load_prompt("system_logic")),
            Message("user", fit(sections, logic_budget)),
        ],
        MobPlan,
        ctx.config.max_repairs,
        extra_check=lambda p: plan_problems(ctx, p, taken),
    )


def _planned_mob_prose(ctx: Context, state: _CreateState, mob: PlannedMob) -> MobProse:
    """The mob's prose, requested again only when what it depends on changed."""
    fingerprint = _fingerprint(mob)
    current = mob.id in state.prose and state.fingerprints.get(mob.id) == fingerprint
    if not current:
        state.prose[mob.id] = _mob_prose_call(ctx, mob, state.brief.notes)
        state.fingerprints[mob.id] = fingerprint
    return state.prose[mob.id]


def _planned_item_prose(
    ctx: Context, state: _CreateState, item_id: str, loot: LootSpec
) -> ItemProse:
    """A new loot item's prose, requested again only when its concept changed."""
    concept = loot.concept or ""
    # The concept is part of the key so a repair that changes it gets new prose.
    key = f"{item_id}\n{concept}"
    if key not in state.item_prose:
        state.item_prose[key] = _item_prose_call(ctx, item_id, state.brief.notes, concept)
    return state.item_prose[key]


def _create_summary(plan: MobPlan, n_items: int) -> str:
    n_spawns = sum(len(m.spawns) for m in plan.mobs)
    return (
        f"+{_counted(len(plan.mobs), 'mob')}, +{_counted(n_items, 'item')}, "
        f"{_counted(n_spawns, 'spawn')}"
    )


def _build(ctx: Context, state: _CreateState, problems: list[str]) -> Proposal:
    plan = _plan_call(ctx, _plan_sections(ctx, state, problems))
    state.plan = plan
    ops: list[StarOp] = []
    prose_preview: list[tuple[str, str, str]] = []
    for mob in plan.mobs:
        mob_prose = _planned_mob_prose(ctx, state, mob)
        ops.append(_mob_decl(ctx, mob, mob_prose))
        prose_preview.extend(_preview(mob.id, mob_prose))
    created = new_items(ctx, plan.mobs)
    for item_id, loot in created.items():
        item = _planned_item_prose(ctx, state, item_id, loot)
        ops.append(_item_decl(item, item_id, loot.kind))
        prose_preview.extend(_preview(item_id, item))
    for mob in plan.mobs:
        ops.extend(_placement_ops(ctx, mob))
    return Proposal(
        ops=ops,
        new_files={},
        summary=_create_summary(plan, len(created)),
        prose_preview=prose_preview,
    )


def _numbers_call(callee: str, numbers: dict[str, int]) -> Value:
    """``callee(name = number, ...)``, or None when there are no numbers."""
    return {"$call": callee, "args": [], "kwargs": dict(numbers)} if numbers else None


def _numbers_dict(numbers: dict[str, int]) -> Value:
    """``{"name": number, ...}``, or None when there are no numbers."""
    return {"$dict": [[k, v] for k, v in numbers.items()]} if numbers else None


def _mob_args(ctx: Context, mob: PlannedMob, prose: MobProse) -> dict[str, Value]:
    """The declaration's arguments in the template's order; empty optional ones left out."""
    behaviors: list[Value] = [
        {"$call": b.name, "args": [], "kwargs": dict(b.args)} for b in mob.behaviors
    ]
    loot: list[Value] = [_loot_value(ctx, item) for item in mob.loot]
    candidates: list[tuple[str, Value]] = [
        ("name", prose.name),
        ("keywords", list(prose.keywords)),
        ("desc", prose.desc),
        ("level", mob.level),
        ("stats", _numbers_call("stats", mob.stats)),
        ("vitals", _numbers_call("vitals", mob.vitals)),
        ("resists", _numbers_call("resists", mob.resists)),
        ("skills", _numbers_dict(mob.skills)),
        ("senses", _numbers_dict(mob.senses)),
        ("behaviors", behaviors or None),
        ("factions", list(mob.factions) or None),
        ("traits", list(mob.traits) or None),
        ("loot", loot or None),
    ]
    return {name: value for name, value in candidates if value is not None}


def _loot_value(ctx: Context, loot: LootSpec) -> Value:
    kwargs: dict[str, Value] = {}
    if loot.chance != 100:
        kwargs["chance"] = loot.chance
    return {"$call": "loot", "args": [_short_label(loot.item, ctx.package)], "kwargs": kwargs}


def _spawn_value(ctx: Context, mob: str, spawn: SpawnSpec) -> Value:
    kwargs: dict[str, Value] = {"count": spawn.count}
    if spawn.respawn_pulses is not None:
        kwargs["respawn_pulses"] = spawn.respawn_pulses
    return {"$call": "spawn", "args": [_short_label(mob, ctx.package)], "kwargs": kwargs}


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


_CATALOGUE = """add_mob mob — add a mob (id, concept, level, stats, vitals, resists, skills,
  senses, behaviors, factions, traits, loot, spawns, todos) with its prose
remove_mob id — remove a mob and the spawns that place it
set_mob id, fields — set named fields of a mob to new values (Value form)
add_loot id, loot {item, chance, concept, kind} — add a loot item to a mob
remove_loot id, item — take a loot item out of a mob
place spawn {room, count, respawn_pulses}, mob — place a mob in a room
unplace room, mob — take a spawn out of a room
rewrite ids, guidance — rewrite the prose of existing mobs"""


def _call_text(value: dict[str, Value]) -> str:
    args = value.get("args")
    kwargs = value.get("kwargs")
    parts = [
        ", ".join(_value_text(a) for a in args) if isinstance(args, list) else "",
        ", ".join(f"{k} = {_value_text(v)}" for k, v in kwargs.items())
        if isinstance(kwargs, dict)
        else "",
    ]
    return f"{value['$call']}({', '.join(p for p in parts if p)})"


def _dict_text(pairs: Value) -> str:
    if not isinstance(pairs, list):
        return "{}"
    inner = ", ".join(
        f"{_value_text(pair[0])}: {_value_text(pair[1])}"
        for pair in pairs
        if isinstance(pair, list) and len(pair) == 2
    )
    return "{" + inner + "}"


# How the prompt shows each tagged Value form, by its tag.
_TAGGED_TEXT: dict[str, Callable[[dict[str, Value]], str]] = {
    "$const": lambda v: str(v["$const"]),
    "$ref": lambda v: f"<{v['$ref']}>",
    "$opaque": lambda v: f"<{v['$opaque']}>",
    "$call": lambda v: _call_text(v),
    "$dict": lambda v: _dict_text(v["$dict"]),
}


def _value_text(value: Value) -> str:
    """A Value as the prompt shows it: calls named, constants bare, code marked."""
    if isinstance(value, list):
        return "[" + ", ".join(_value_text(v) for v in value) + "]"
    if not isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    tag = next((t for t in _TAGGED_TEXT if t in value), None)
    if tag is not None:
        return _TAGGED_TEXT[tag](value)
    return "{" + ", ".join(f"{k}: {_value_text(v)}" for k, v in value.items()) + "}"


_SHOWN_FIELDS = (
    "stats",
    "vitals",
    "resists",
    "skills",
    "senses",
    "factions",
    "traits",
    "behaviors",
    "loot",
)


def _decl_line(decl: DeclView) -> str:
    """One of the package's mobs as the turn prompt shows it."""
    name = decl.args.get("name")
    parts = [f"{decl.id}: {name if isinstance(name, str) else '(no name)'}"]
    level = decl.args.get("level")
    if isinstance(level, int):
        parts.append(f"level {level}")
    parts.extend(
        f"{fieldname}: {_value_text(decl.args[fieldname])}"
        for fieldname in _SHOWN_FIELDS
        if fieldname in decl.args
    )
    computed = [name for name, value in decl.args.items() if not is_editable(value)]
    if computed:
        parts.append(f"computed by code: {', '.join(computed)} (do not edit these)")
    return "; ".join(parts)


def _mobs_text(ctx: Context) -> str:
    lines = [_decl_line(decl) for decl in ctx.view.decls if decl.kind == "mob"]
    opaque = [o for o in ctx.view.opaque if o.kind == "mob"]
    lines.extend(f"{o.id}: (not a plain declaration; do not edit this mob)" for o in opaque if o.id)
    return "\n".join(lines) if lines else "none"


def _history_text(history: list[Message]) -> str:
    if not history:
        return "none"
    return "\n".join(f"{m.role}: {m.content}" for m in history[-6:])


def _turn_call(
    ctx: Context,
    message: str,
    history: list[Message],
    repair: tuple[list[str], MobTurn] | None = None,
) -> MobTurn:
    """The turn for the message; ``repair`` is lint's problems and the turn they were in."""
    budget = ctx.config.logic.context_tokens - ctx.config.logic.max_tokens
    turn_prompt = load_prompt(
        "mob_turn",
        catalogue=_CATALOGUE,
        mobs=_mobs_text(ctx),
        history=_history_text(history),
        message=message,
    )
    sections = [(0, turn_prompt)]
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
        MobTurn,
        ctx.config.max_repairs,
        extra_check=lambda turn: turn_problems(ctx, turn),
    )


def _rooms_with_spawn(ctx: Context, mob: str) -> list[tuple[str, str]]:
    """The package's rooms that spawn the mob, with the label each file writes."""
    found: list[tuple[str, str]] = []
    for decl in ctx.view.decls:
        label = spawn_entry(decl, mob, ctx.package) if decl.kind == "room" else None
        if label is not None:
            found.append((decl.id, label))
    return found


def _rewrite_call(ctx: Context, rid: str, guidance: str) -> str:
    decl = ctx.view.get("mob", rid)
    name = decl.args.get("name") if decl is not None else None
    old = decl.args.get("desc") if decl is not None else None
    prompt = load_prompt(
        "mob_rewrite",
        notes="keep to the package's existing style",
        mob_name=name if isinstance(name, str) else rid,
        old_text=old if isinstance(old, str) else "",
        guidance=guidance,
    )
    budget = ctx.config.prose.context_tokens - ctx.config.prose.max_tokens
    return call_structured(
        ctx.llm,
        "prose",
        [
            Message("system", load_prompt("system_prose")),
            Message("user", fit([(0, prompt)], budget)),
        ],
        MobProse,
        ctx.config.max_repairs,
        extra_check=prose_check(rid),
    ).desc


@dataclass
class _TurnProse:
    """The prose a turn's edits wrote, and the new items made so far.

    A loot item two edits of the turn share is created once.
    """

    preview: list[tuple[str, str, str]] = field(default_factory=list)
    created: set[str] = field(default_factory=set)


def _new_item_ops(ctx: Context, prose: _TurnProse, loot: LootSpec, theme: str) -> list[StarOp]:
    new_id = new_item_id(ctx, loot)
    if new_id is None or new_id in prose.created:
        return []
    prose.created.add(new_id)
    item = _item_prose_call(ctx, new_id, theme, loot.concept or "")
    prose.preview.extend(_preview(new_id, item))
    return [_item_decl(item, new_id, loot.kind)]


def _add_mob_ops(ctx: Context, prose: _TurnProse, edit: AddMob, message: str) -> list[StarOp]:
    # The user's request is the only style guidance a turn has.
    mob_prose = _mob_prose_call(ctx, edit.mob, message)
    prose.preview.extend(_preview(edit.mob.id, mob_prose))
    ops: list[StarOp] = [_mob_decl(ctx, edit.mob, mob_prose)]
    for loot in edit.mob.loot:
        ops.extend(_new_item_ops(ctx, prose, loot, edit.mob.concept))
    return ops + _placement_ops(ctx, edit.mob)


def _remove_mob_ops(ctx: Context, edit: RemoveMob) -> list[StarOp]:
    ops: list[StarOp] = [RemoveDecl(kind="mob", id=edit.id)]
    ops.extend(
        RemoveItem(
            kind="room",
            id=room,
            name="spawns",
            match=ItemMatch(call="spawn", first_arg=label),
        )
        for room, label in _rooms_with_spawn(ctx, edit.id)
    )
    return ops


def _set_mob_ops(edit: SetMob) -> list[StarOp]:
    return [
        SetArg(kind="mob", id=edit.id, name=name, value=value)
        for name, value in edit.fields.items()
    ]


def _add_loot_ops(ctx: Context, prose: _TurnProse, edit: AddLoot, message: str) -> list[StarOp]:
    ops: list[StarOp] = [
        AppendItem(kind="mob", id=edit.id, name="loot", value=_loot_value(ctx, edit.loot))
    ]
    return ops + _new_item_ops(ctx, prose, edit.loot, message)


def _remove_loot_ops(ctx: Context, edit: RemoveLoot) -> list[StarOp]:
    written = loot_entry(ctx.view.get("mob", edit.id), edit.item, ctx.package)
    match = ItemMatch(call="loot", first_arg=written or _short_label(edit.item, ctx.package))
    return [RemoveItem(kind="mob", id=edit.id, name="loot", match=match)]


def _place_ops(ctx: Context, edit: Place) -> list[StarOp]:
    value = _spawn_value(ctx, edit.mob, edit.spawn)
    return [AppendItem(kind="room", id=edit.spawn.room, name="spawns", value=value)]


def _unplace_ops(ctx: Context, edit: Unplace) -> list[StarOp]:
    written = spawn_entry(ctx.view.get("room", edit.room), edit.mob, ctx.package)
    match = ItemMatch(call="spawn", first_arg=written or _short_label(edit.mob, ctx.package))
    return [RemoveItem(kind="room", id=edit.room, name="spawns", match=match)]


def _rewrite_ops(ctx: Context, prose: _TurnProse, ids: list[str], guidance: str) -> list[StarOp]:
    ops: list[StarOp] = []
    for rid in ids:
        new_desc = _rewrite_call(ctx, rid, guidance)
        ops.append(SetArg(kind="mob", id=rid, name="desc", value=new_desc))
        prose.preview.append((rid, "desc", new_desc))
    return ops


def _edit_ops(ctx: Context, prose: _TurnProse, edit: MobEdit, message: str) -> list[StarOp]:
    """The ops for one edit; new prose goes into ``prose``."""
    if isinstance(edit, AddMob):
        return _add_mob_ops(ctx, prose, edit, message)
    if isinstance(edit, RemoveMob):
        return _remove_mob_ops(ctx, edit)
    if isinstance(edit, SetMob):
        return _set_mob_ops(edit)
    if isinstance(edit, AddLoot):
        return _add_loot_ops(ctx, prose, edit, message)
    if isinstance(edit, RemoveLoot):
        return _remove_loot_ops(ctx, edit)
    if isinstance(edit, Place):
        return _place_ops(ctx, edit)
    if isinstance(edit, Unplace):
        return _unplace_ops(ctx, edit)
    return _rewrite_ops(ctx, prose, edit.ids, edit.guidance)


def _edit_to_proposal(ctx: Context, turn: MobTurn, message: str) -> Proposal:
    prose = _TurnProse()
    ops: list[StarOp] = []
    for edit in turn.edits:
        ops.extend(_edit_ops(ctx, prose, edit, message))
    added = sum(isinstance(edit, AddMob) for edit in turn.edits)
    removed = sum(isinstance(edit, RemoveMob) for edit in turn.edits)
    return Proposal(
        ops=ops,
        new_files={},
        summary=_refine_summary(added, removed, len(ops)),
        prose_preview=prose.preview,
    )


def _refine_summary(added_mobs: int, removed_mobs: int, n_ops: int) -> str:
    parts = []
    if added_mobs:
        parts.append(f"+{_counted(added_mobs, 'mob')}")
    if removed_mobs:
        parts.append(f"-{_counted(removed_mobs, 'mob')}")
    if not parts:
        parts.append(_counted(n_ops, "change"))
    return ", ".join(parts)
