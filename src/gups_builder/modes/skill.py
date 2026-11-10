"""The skill mode: creating one skill with a declarative effect, and refining it.

The logic model briefs the request, then plans the skill against the world's
schema, the API's effect primitives and the world's skills as balance
references. The plan is checked before any prose is spent. A follow-up message
becomes a turn whose edits are checked against the package's skills and the
world, then turned into ops. Like every mode, the build runs inside the staged
lint loop; a skills package has no generated test, so only lint is run.
The plan and turn shapes and their checks are in ``skill_checks``.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from pydantic import BaseModel

from gups_builder.engine.api import ApiModel
from gups_builder.engine.world import WorldDescription
from gups_builder.grid.place import room_id
from gups_builder.llm.client import Message
from gups_builder.llm.structured import call_structured
from gups_builder.modes.common import Context, Proposal, allowed_values, finalize
from gups_builder.modes.skill_checks import (
    AddSkill,
    EffectSpec,
    PlannedSkill,
    RemoveSkill,
    SetEffect,
    SetSkill,
    SkillEdit,
    SkillTurn,
    TeachEdit,
    UnrecognizedProse,
    declared_ids,
    full_label,
    local_id,
    prose_lines,
    prose_problems,
    ranges,
    skill_problems,
    str_arg,
    turn_problems,
)
from gups_builder.prompting import fit, load_prompt
from gups_builder.star.ops import AddDecl, AddTodo, RemoveDecl, SetArg, StarOp
from gups_builder.star.reader import DeclView
from gups_builder.star.render import render_string
from gups_builder.star.values import Value, is_editable

_BALANCE_OTHERS = 10


class SkillBrief(BaseModel):
    concept: str
    kind: str
    notes: str = ""
    questions: list[str] = []


# -- prompts -------------------------------------------------------------------


def _existing_skills_text(ctx: Context) -> str:
    ids = [d.id for d in ctx.view.decls if d.kind == "skill"]
    return ", ".join(ids) if ids else "none"


def _existing_mobs_text(ctx: Context) -> str:
    names: dict[str, str] = {}
    for d in ctx.view.decls:
        if d.kind == "mob":
            name = d.args.get("name")
            names[d.id] = name if isinstance(name, str) else "(no name)"
    for m in ctx.world.mobs_in(ctx.package):
        names.setdefault(room_id(m.label), m.name)
    return "\n".join(f"{mid}: {name}" for mid, name in names.items()) or "none"


def _numbers(d: dict[str, int]) -> str:
    return ", ".join(f"{k} {v}" for k, v in d.items()) or "none"


def _effects_text(api: ApiModel) -> str:
    lines = [
        f"{entry.name} — {entry.doc} [{', '.join(p.name for p in entry.params)}]"
        for entry in api.effects
    ]
    body = "\n".join(lines) if lines else "none"
    return f"Effect primitives (name, doc, its arguments):\n{body}"


def _plugin_effects_text(world: WorldDescription) -> str:
    lines = [f"{p.id}.{name}" for p in world.plugins for name in p.effects]
    body = "\n".join(lines) if lines else "none"
    return f"Plugin effects (name):\n{body}"


def _call_text(value: dict[str, Value]) -> str:
    args = value.get("args")
    kwargs = value.get("kwargs")
    parts: list[str] = []
    if isinstance(args, list):
        parts.extend(_value_text(a) for a in args)
    if isinstance(kwargs, dict):
        parts.extend(f"{k} = {_value_text(v)}" for k, v in kwargs.items())
    return f"{value['$call']}({', '.join(parts)})"


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


def _own_balance_line(decl: DeclView) -> str:
    """One of the package's skills as a balance reference, from its file."""
    kind = decl.args.get("kind")
    parts = [f"{decl.id}: kind {kind if isinstance(kind, str) else '?'}"]
    cost = decl.args.get("cost")
    if cost is not None:
        parts.append(f"cost {_value_text(cost)}")
    parts.extend(
        f"{name} {decl.args[name]}"
        for name in ("warmup", "lag")
        if isinstance(decl.args.get(name), int)
    )
    effect = decl.args.get("effect")
    if effect is not None and is_editable(effect):
        parts.append(f"effect {_value_text(effect)}")
    return "; ".join(parts)


def _balance_text(ctx: Context) -> str:
    """The package's own skills, then skills of other packages, for balance.

    The package's own skills show the effect as the file writes it, when the
    reader can hold it as a plain value; the others are the world's, which the
    builder cannot read the files of.
    """
    lines = [_own_balance_line(d) for d in ctx.view.decls if d.kind == "skill"]
    prefix = f"//{ctx.package}:"
    others = [s for s in ctx.world.skills if not s.label.startswith(prefix)]
    lines.extend(
        f"{s.label}: kind {s.kind}, cost {_numbers(s.cost)}, warmup {s.warmup}, lag {s.lag}"
        for s in others[:_BALANCE_OTHERS]
    )
    body = "\n".join(lines) if lines else "none"
    return f"Balance references (id or label, kind, cost, warmup, lag, effect):\n{body}"


# -- creation ------------------------------------------------------------------


@dataclass
class _CreateState:
    """What a build already produced, so a repair re-requests only what changed."""

    plan: PlannedSkill | None = None
    prose: UnrecognizedProse | None = None
    fingerprint: str | None = None


def create_skill(ctx: Context, request: str) -> SkillBrief | Proposal:
    """A new skill: the brief, then the plan, prose and ops inside the repair loop."""
    logic_budget = ctx.config.logic.context_tokens - ctx.config.logic.max_tokens
    brief_prompt = load_prompt(
        "skill_brief",
        existing_skills=_existing_skills_text(ctx),
        existing_mobs=_existing_mobs_text(ctx),
    )
    sections = [
        (0, request),
        (0, brief_prompt),
    ]
    brief = call_structured(
        ctx.llm,
        "logic",
        [
            Message("system", load_prompt("system_logic")),
            Message("user", fit(sections, logic_budget)),
        ],
        SkillBrief,
        ctx.config.max_repairs,
    )
    if brief.questions:
        return brief
    state = _CreateState()
    # No generated test: a skills package's own tests are its business.
    return finalize(ctx, lambda problems: _build(ctx, brief, state, problems), None)


def _fingerprint(skill: PlannedSkill, notes: str) -> str:
    """Everything the skill's prose prompt depends on that the plan can change."""
    return json.dumps([skill.concept, skill.kind, skill.target, skill.range, notes])


def _prose_call(ctx: Context, skill: PlannedSkill, notes: str) -> UnrecognizedProse:
    prompt = load_prompt(
        "skill_prose",
        notes=notes,
        concept=skill.concept,
        kind=skill.kind,
        target=skill.target,
        range=skill.range or "unspecified",
    )
    budget = ctx.config.prose.context_tokens - ctx.config.prose.max_tokens
    return call_structured(
        ctx.llm,
        "prose",
        [
            Message("system", load_prompt("system_prose")),
            Message("user", fit([(0, prompt)], budget)),
        ],
        UnrecognizedProse,
        ctx.config.max_repairs,
        extra_check=lambda prose: prose_problems(skill, prose),
    )


def _listed(items: Iterable[str]) -> str:
    """Items for a prompt, comma-separated, or ``none``."""
    return ", ".join(items) or "none"


def _plan_sections(
    ctx: Context, brief: SkillBrief, state: _CreateState, problems: list[str]
) -> list[tuple[int, str]]:
    """The plan prompt's sections, with the repair request when lint found problems."""
    world = ctx.world
    plan_prompt = load_prompt(
        "skill_plan",
        brief=brief.model_dump_json(indent=2),
        vitals=_listed(f"{v.name} ({v.label})" for v in world.schema.vitals),
        damage_types=_listed(f"{d.name} ({d.label})" for d in world.schema.damage_types),
        channels=_listed(world.schema.channels),
        track="on" if world.has_feature("track") else "off",
        ranges=_listed(ranges(ctx)),
        targets=_listed(allowed_values(ctx.api, world, "skill", "target")),
        existing_ids=_listed(sorted(declared_ids(ctx))),
    )
    sections = [
        (0, plan_prompt),
        (1, _effects_text(ctx.api)),
        (1, _plugin_effects_text(world)),
        (2, _balance_text(ctx)),
    ]
    if problems and state.plan is not None:
        repair = load_prompt(
            "repair_lint", problems="\n".join(problems), previous=state.plan.model_dump_json()
        )
        sections.append((0, repair))
    return sections


def _plan_call(ctx: Context, sections: list[tuple[int, str]]) -> PlannedSkill:
    logic_budget = ctx.config.logic.context_tokens - ctx.config.logic.max_tokens
    taken = declared_ids(ctx)
    return call_structured(
        ctx.llm,
        "logic",
        [
            Message("system", load_prompt("system_logic")),
            Message("user", fit(sections, logic_budget)),
        ],
        PlannedSkill,
        ctx.config.max_repairs,
        extra_check=lambda s: skill_problems(ctx, s, taken),
    )


def _new_package_files(ctx: Context, brief: SkillBrief) -> dict[str, str]:
    """package.star for a package that has none yet."""
    # The reader skips package.star, so its absence from ctx.files says nothing; the disk does.
    if (ctx.world_dir / ctx.package / "package.star").exists():
        return {}
    return {
        "package.star": (
            f'package(api_version = {ctx.world.api_version}, kind = "skills", '
            f"title = {render_string(brief.concept)})\n"
        )
    }


def _create_summary(plan: PlannedSkill) -> str:
    if not plan.teach:
        return "+1 skill"
    n = len(plan.teach)
    return f"+1 skill, {n} mob{'s' if n != 1 else ''} taught"


def _build(ctx: Context, brief: SkillBrief, state: _CreateState, problems: list[str]) -> Proposal:
    plan = _plan_call(ctx, _plan_sections(ctx, brief, state, problems))
    state.plan = plan
    fingerprint = _fingerprint(plan, brief.notes)
    if state.prose is None or state.fingerprint != fingerprint:
        state.prose = _prose_call(ctx, plan, brief.notes)
        state.fingerprint = fingerprint
    return Proposal(
        ops=_new_skill_ops(ctx, plan, state.prose),
        new_files=_new_package_files(ctx, brief),
        summary=_create_summary(plan),
        prose_preview=_preview(plan.id, state.prose),
    )


def _preview(skill_id: str, prose: UnrecognizedProse) -> list[tuple[str, str, str]]:
    return [(skill_id, f"unrecognized.{line}", text) for line, text in prose_lines(prose)]


def _unrecognized_value(prose: UnrecognizedProse) -> Value:
    pairs: list[Value] = [[line, text] for line, text in prose_lines(prose)]
    return {"$dict": pairs}


def _new_skill_ops(ctx: Context, skill: PlannedSkill, prose: UnrecognizedProse) -> list[StarOp]:
    """The declaration of a new skill, then its teaches and its todos."""
    ops: list[StarOp] = [
        AddDecl(
            file=_skills_file(ctx),
            kind="skill",
            id=skill.id,
            args=_skill_args(skill, prose),
        )
    ]
    for teach in skill.teach:
        mob_id = local_id(teach.mob, ctx.package)
        if mob_id is not None:
            ops.append(_teach_op(ctx, skill.id, mob_id, teach.proficiency))
    ops.extend(AddTodo(kind="skill", id=skill.id, text=todo) for todo in skill.todos)
    return ops


def _skills_file(ctx: Context) -> str:
    """The package's one skills file when it has one, else skills.star."""
    files = {d.file for d in ctx.view.decls if d.kind == "skill"}
    return files.pop() if len(files) == 1 else "skills.star"


def _skill_args(skill: PlannedSkill, prose: UnrecognizedProse) -> dict[str, Value]:
    """The declaration's arguments in the template's order; empty optional ones left out."""
    candidates: list[tuple[str, Value]] = [
        ("kind", skill.kind),
        (
            "cost",
            {"$call": "vitals", "args": [], "kwargs": dict(skill.cost)} if skill.cost else None,
        ),
        ("warmup", skill.warmup or None),
        ("lag", skill.lag),
        ("range", skill.range),
        ("target", skill.target),
        ("channels", list(skill.channels) or None),
        (
            "interrupt",
            {"$dict": [[k, v] for k, v in skill.interrupt.items()]} if skill.interrupt else None,
        ),
        ("tags", list(skill.tags) or None),
        ("unrecognized", _unrecognized_value(prose) if prose_lines(prose) else None),
        ("effect", _effect_value(skill.effect) if skill.effect is not None else None),
    ]
    return {name: value for name, value in candidates if value is not None}


def _effect_value(effect: EffectSpec) -> Value:
    """The effect as nested $call values; sequence's effects as positional args."""
    name, args = effect.primitive, dict(effect.args)
    if name == "damage":
        type_value = args.get("type")
        if isinstance(type_value, str):
            args["type"] = {"$const": f"DAMAGE.{type_value}"}
    if name == "sequence":
        return {
            "$call": "sequence",
            "args": [_effect_value(sub) for sub in effect.effects],
            "kwargs": {},
        }
    return {"$call": name, "args": [], "kwargs": args}


def _skill_key(skill: str) -> str:
    """The key a mob's skills dict holds a skill under: its label, or ``:id`` for this package."""
    return skill if skill.startswith("//") else f":{skill.removeprefix(':')}"


def _skills_value(ctx: Context, skill: str, mob_id: str, proficiency: int) -> Value:
    """The mob's skills dict from its file, with the skill at the new proficiency.

    A Starlark dict literal may not repeat a key, so a skill the mob already
    knows, under any spelling of its label, has its entry replaced in place.
    Other entries are kept as the file writes them. A skills value the file
    computes by code is not merged: the writer refuses the op instead.
    """
    decl = ctx.view.get("mob", mob_id)
    current = decl.args.get("skills") if decl is not None else None
    entries = current.get("$dict") if isinstance(current, dict) else None
    pairs: list[Value] = list(entries) if isinstance(entries, list) else []
    key = _skill_key(skill)
    wanted = full_label(key, ctx.package)
    for i, pair in enumerate(pairs):
        pair_key = _pair_key(pair)
        if pair_key is not None and full_label(pair_key, ctx.package) == wanted:
            pairs[i] = [pair_key, proficiency]
            break
    else:
        pairs.append([key, proficiency])
    return {"$dict": pairs}


def _pair_key(pair: Value) -> str | None:
    """The string key of a ``$dict`` pair, or None for any other shape."""
    if not isinstance(pair, list) or len(pair) != 2:
        return None
    key = pair[0]
    return key if isinstance(key, str) else None


def _teach_op(ctx: Context, skill: str, mob_id: str, proficiency: int) -> SetArg:
    return SetArg(
        kind="mob",
        id=mob_id,
        name="skills",
        value=_skills_value(ctx, skill, mob_id, proficiency),
    )


# -- refinement ------------------------------------------------------------------


_CATALOGUE = """add_skill skill {id, concept, kind, cost, warmup, lag, range, target, channels,
  tags, interrupt, effect, teach, todos} — add a skill with its unrecognized lines
remove_skill id — remove a skill
set_skill id, fields — set named fields of a skill to new values (Value form)
set_effect id, effect {primitive, args, effects} — replace a skill's effect
teach id, mob, proficiency — add a skill to a mob's skills
rewrite ids, guidance — rewrite the unrecognized lines of existing skills"""


_SHOWN_FIELDS = (
    "cost",
    "warmup",
    "lag",
    "range",
    "target",
    "channels",
    "interrupt",
    "tags",
    "unrecognized",
    "effect",
)


def _skill_line(decl: DeclView) -> str:
    """One of the package's skills for the turn prompt, its computed fields marked."""
    kind = decl.args.get("kind")
    parts = [f"{decl.id}: {kind if isinstance(kind, str) else '(no kind)'}"]
    parts.extend(
        f"{fieldname}: {_value_text(decl.args[fieldname])}"
        for fieldname in _SHOWN_FIELDS
        if fieldname in decl.args
    )
    computed = [name for name, value in decl.args.items() if not is_editable(value)]
    if computed:
        parts.append(f"computed by code: {', '.join(computed)} (do not edit these)")
    return "; ".join(parts)


def _skills_text(ctx: Context) -> str:
    lines = [_skill_line(d) for d in ctx.view.decls if d.kind == "skill"]
    lines.extend(
        f"{o.id}: (not a plain declaration; do not edit this skill)"
        for o in ctx.view.opaque
        if o.kind == "skill" and o.id
    )
    return "\n".join(lines) if lines else "none"


def _history_text(history: list[Message]) -> str:
    if not history:
        return "none"
    return "\n".join(f"{m.role}: {m.content}" for m in history[-6:])


def _turn_call(
    ctx: Context,
    message: str,
    history: list[Message],
    repair: tuple[list[str], SkillTurn] | None = None,
) -> SkillTurn:
    """The turn for the message; ``repair`` is lint's problems and the turn they were in."""
    budget = ctx.config.logic.context_tokens - ctx.config.logic.max_tokens
    turn_prompt = load_prompt(
        "skill_turn",
        catalogue=_CATALOGUE,
        skills=_skills_text(ctx),
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
        SkillTurn,
        ctx.config.max_repairs,
        extra_check=lambda turn: turn_problems(ctx, turn),
    )


def skill_turn(
    ctx: Context, message: str, history: list[Message]
) -> tuple[SkillTurn, Proposal | None]:
    """Turn a follow-up message in skill mode into a turn and, for an edit, a proposal."""
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

    proposal = finalize(ctx, build, None)
    if last.kind != "edit":
        return last, None
    return last, proposal


def _current_lines(ctx: Context, skill_id: str) -> list[str]:
    """The skill's unrecognized lines as the file writes them, one ``key: text`` each."""
    decl = ctx.view.get("skill", skill_id)
    value = decl.args.get("unrecognized") if decl is not None else None
    entries = value.get("$dict") if isinstance(value, dict) else None
    if not isinstance(entries, list):
        return []
    lines: list[str] = []
    for pair in entries:
        key = _pair_key(pair)
        if key is not None:
            assert isinstance(pair, list)
            lines.append(f"{key}: {pair[1] if isinstance(pair[1], str) else '(computed)'}")
    return lines


def _rewrite_call(ctx: Context, skill_id: str, guidance: str) -> UnrecognizedProse:
    """A prose call for one skill's unrecognized lines, from the file's current ones."""
    target = str_arg(ctx, skill_id, "target")
    range_name = str_arg(ctx, skill_id, "range")
    old = _current_lines(ctx, skill_id)
    prompt = load_prompt(
        "skill_rewrite",
        notes="keep to the package's existing style",
        skill_id=skill_id,
        target=target or "unknown",
        range=range_name or "unspecified",
        old_text="\n".join(old) if old else "(none)",
        guidance=guidance,
    )
    # An unknown target is checked as the most permissive one, enemy.
    checked = PlannedSkill(
        id=skill_id, concept="", kind="", target=target or "enemy", range=range_name
    )
    budget = ctx.config.prose.context_tokens - ctx.config.prose.max_tokens
    return call_structured(
        ctx.llm,
        "prose",
        [
            Message("system", load_prompt("system_prose")),
            Message("user", fit([(0, prompt)], budget)),
        ],
        UnrecognizedProse,
        ctx.config.max_repairs,
        extra_check=lambda prose: prose_problems(checked, prose),
    )


def _edit_ops(
    ctx: Context, edit: SkillEdit, message: str, preview: list[tuple[str, str, str]]
) -> list[StarOp]:
    """The ops for one edit; new prose goes into ``preview``."""
    if isinstance(edit, AddSkill):
        # The user's request is the only style guidance a turn has.
        prose = _prose_call(ctx, edit.skill, message)
        preview.extend(_preview(edit.skill.id, prose))
        return _new_skill_ops(ctx, edit.skill, prose)
    if isinstance(edit, RemoveSkill):
        return [RemoveDecl(kind="skill", id=edit.id)]
    if isinstance(edit, SetSkill):
        return [
            SetArg(kind="skill", id=edit.id, name=name, value=value)
            for name, value in edit.fields.items()
        ]
    if isinstance(edit, SetEffect):
        return [SetArg(kind="skill", id=edit.id, name="effect", value=_effect_value(edit.effect))]
    if isinstance(edit, TeachEdit):
        mob_id = local_id(edit.mob, ctx.package)
        return [] if mob_id is None else [_teach_op(ctx, edit.id, mob_id, edit.proficiency)]
    ops: list[StarOp] = []
    for rid in edit.ids:
        prose = _rewrite_call(ctx, rid, edit.guidance)
        ops.append(
            SetArg(kind="skill", id=rid, name="unrecognized", value=_unrecognized_value(prose))
        )
        preview.extend(_preview(rid, prose))
    return ops


def _edit_to_proposal(ctx: Context, turn: SkillTurn, message: str) -> Proposal:
    ops: list[StarOp] = []
    prose_preview: list[tuple[str, str, str]] = []
    for edit in turn.edits:
        ops.extend(_edit_ops(ctx, edit, message, prose_preview))
    added = sum(isinstance(edit, AddSkill) for edit in turn.edits)
    removed = sum(isinstance(edit, RemoveSkill) for edit in turn.edits)
    return Proposal(
        ops=ops,
        new_files={},
        summary=_refine_summary(added, removed, len(ops)),
        prose_preview=prose_preview,
    )


def _refine_summary(added: int, removed: int, n_ops: int) -> str:
    parts = []
    if added:
        parts.append(f"+{added} skill{'s' if added != 1 else ''}")
    if removed:
        parts.append(f"-{removed} skill{'s' if removed != 1 else ''}")
    if not parts:
        parts.append(f"{n_ops} change{'s' if n_ops != 1 else ''}")
    return ", ".join(parts)
