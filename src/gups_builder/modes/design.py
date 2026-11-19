"""A new area designed outside the builder, applied with no model.

An agent such as Claude Code plays both models: it writes the brief, the room graph,
the prose and, optionally, the mobs and their loot as one JSON design. This module
checks that design with the same checks the area and mob modes hold their models'
replies to, assembles it with the same code, and stages and checks the result. A
problem is reported, never repaired: the agent edits the design and applies it again.

The mobs are planned against the package as the area leaves it, so they may spawn in
the rooms the same design adds.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from gups_builder.grid.ascii import render_map
from gups_builder.modes import testgen
from gups_builder.modes.area import (
    SIZE_WORDS,
    AreaProse,
    area_facts,
    assemble_area,
    room_neighbours,
)
from gups_builder.modes.area_checks import (
    AreaBrief,
    AreaGraph,
    AreaPlan,
    ItemProse,
    RoomProse,
    brief_problems,
    item_prose_check,
    new_floor_items,
    normalize_brief,
    normalize_plan,
    place_graph,
    plan_problems,
    plan_to_grid,
    prose_check,
)
from gups_builder.modes.common import Context, Proposal, render_errors, stage_and_check
from gups_builder.modes.mob import assemble_mobs, mob_facts
from gups_builder.modes.mob_checks import MobPlan, MobProse, declared_ids, new_items
from gups_builder.modes.mob_checks import plan_problems as mob_plan_problems
from gups_builder.modes.mob_checks import prose_check as mob_prose_check
from gups_builder.prompting import prompt_template
from gups_builder.session import write_atomic
from gups_builder.star.diff import unified_diff
from gups_builder.star.reader import StarParseError, read_contents, read_package
from gups_builder.star.writer import apply_ops

if TYPE_CHECKING:
    from collections.abc import Callable

    from gups_builder.engine.lint import LintFinding
    from gups_builder.star.ops import StarOp


class AreaDesign(BaseModel):
    """The design file: what the logic and prose models would have replied, in one place."""

    brief: AreaBrief
    graph: AreaGraph
    room_prose: dict[str, RoomProse] = {}
    key_prose: dict[str, ItemProse] = {}
    mobs: MobPlan | None = None
    mob_prose: dict[str, MobProse] = {}
    item_prose: dict[str, ItemProse] = {}


@dataclass
class DesignReport:
    """What a check found, and what the agent needs to write the prose."""

    problems: list[str] = field(default_factory=list)
    # The placed rooms, one ASCII map per level.
    maps: dict[int, str] = field(default_factory=dict)
    # Each room's neighbours as the prose prompt would show them.
    neighbours: dict[str, str] = field(default_factory=dict)
    # The items the floors and the mobs' loot create, which need item_prose.
    new_items: list[str] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "problems": self.problems,
            "maps": {str(z): text for z, text in sorted(self.maps.items())},
            "neighbours": self.neighbours,
            "new_items": self.new_items,
        }


@dataclass
class DesignResult:
    """An applied design: the package's files after it, and what the staged check found."""

    report: DesignReport
    files: dict[str, str] = field(default_factory=dict)
    diff: str = ""
    summary: str = ""
    prose_preview: list[tuple[str, str, str]] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    lint_errors: list[str] = field(default_factory=list)
    test_failure: str | None = None
    link_ops: list[StarOp] = field(default_factory=list)
    link_package: str | None = None

    @property
    def ok(self) -> bool:
        return not (self.report.problems or self.lint_errors or self.test_failure is not None)

    def to_json(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            **self.report.to_json(),
            "summary": self.summary,
            "notes": self.notes,
            "lint_errors": self.lint_errors,
            "test_failure": self.test_failure,
            "link_package": self.link_package,
            "prose": [{"id": i, "field": f, "text": t} for i, f, t in self.prose_preview],
            "diff": self.diff,
        }


@dataclass
class WriteResult:
    files: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    lint: list[LintFinding] = field(default_factory=list)


# -- what a designer is told ----------------------------------------------------

# The prompts that carry the rules the models are held to, in the order a design is made.
RULE_PROMPTS = (
    "area_plan_turn",
    "area_graph",
    "system_prose",
    "room_prose",
    "item_prose",
    "mob_plan",
    "mob_prose",
)


def design_rules() -> dict[str, Any]:
    """The design file's schema and the rules the builder's models are given, as written."""
    return {
        "schema": AreaDesign.model_json_schema(),
        "size_words": {word: list(bounds) for word, bounds in SIZE_WORDS.items()},
        "prompts": {name: prompt_template(name) for name in RULE_PROMPTS},
    }


def world_facts(ctx: Context) -> dict[str, Any]:
    """What the area and mob prompts would tell the models about this world and package."""
    return {
        "package": ctx.package,
        "has_rooms": _has_rooms(ctx),
        "area": area_facts(ctx),
        "mobs": mob_facts(ctx),
    }


# -- checking ----------------------------------------------------------------------


@dataclass
class _Checked:
    """A design taken as far as its checks allow; the plan and mob context once they pass."""

    report: DesignReport
    brief: AreaBrief
    plan: AreaPlan | None = None
    area: Proposal | None = None
    mob_ctx: Context | None = None


def _has_rooms(ctx: Context) -> bool:
    return bool(ctx.world.rooms_in(ctx.package)) or any(d.kind == "room" for d in ctx.view.decls)


def _prose_problems[P: BaseModel](
    what: str,
    prose: dict[str, P],
    wanted: list[str],
    check: Callable[[str], Callable[[P], list[str]]],
) -> list[str]:
    """Every wanted id has prose that passes its check, and no prose names another id."""
    noun, not_a = _PROSE_FIELDS[what]
    problems = [f"{what} has no entry for the {noun} '{i}'" for i in wanted if i not in prose]
    problems.extend(f"{what} names '{i}', which is not {not_a}" for i in prose if i not in wanted)
    for i in wanted:
        if i in prose:
            problems.extend(check(i)(prose[i]))
    return problems


# Each prose field of the design: what its ids name, and what an id it should not hold is not.
_PROSE_FIELDS = {
    "room_prose": ("room", "a room of the graph"),
    "key_prose": ("key", "a key of the graph"),
    "mob_prose": ("mob", "a mob of the plan"),
    "item_prose": ("new item", "an item the floors or the mobs' loot create"),
}


def _area_report(plan: AreaPlan) -> DesignReport:
    grid = plan_to_grid(plan)
    names = {room.id: room.concept for room in plan.rooms}
    levels = sorted({room.at.z for room in plan.rooms})
    return DesignReport(
        maps={z: render_map(grid, z, names) for z in levels},
        neighbours=room_neighbours(plan),
    )


def _check_area(ctx: Context, design: AreaDesign) -> _Checked:
    brief = normalize_brief(ctx, design.brief)
    if _has_rooms(ctx):
        message = (
            f"{ctx.package} already has rooms; apply builds new areas only, so refine this one "
            f"with: gups-builder area {ctx.package}"
        )
        return _Checked(DesignReport(problems=[message]), brief)
    plan = normalize_plan(brief, place_graph(design.graph))
    report = _area_report(plan)
    report.problems = brief_problems(ctx, brief) + plan_problems(ctx, brief, plan)
    if report.problems:
        return _Checked(report, brief, plan)
    report.problems = _prose_problems(
        "room_prose", design.room_prose, [r.id for r in plan.rooms], prose_check
    ) + _prose_problems("key_prose", design.key_prose, [k.id for k in plan.keys], item_prose_check)
    floor_new = [item.item for item in new_floor_items(ctx, plan)]
    report.new_items = floor_new
    # The mobs' loot may create items too, so other ids in item_prose are judged with the mobs.
    floor_prose = {i: p for i, p in design.item_prose.items() if i in floor_new}
    report.problems += _prose_problems("item_prose", floor_prose, floor_new, item_prose_check)
    return _Checked(report, brief, plan)


def _contents_after(ctx: Context, proposal: Proposal) -> dict[str, str]:
    result = apply_ops(ctx.files, ctx.view, proposal.ops)
    return {**ctx.files, **result.files, **proposal.new_files}


def _with_contents(ctx: Context, contents: dict[str, str]) -> Context:
    """The context as the package stands with these contents; package.star is not read."""
    files = {name: text for name, text in contents.items() if name != "package.star"}
    return replace(ctx, files=files, view=read_contents(files, ctx.package))


def _check_mobs(checked: _Checked, design: AreaDesign) -> None:
    """The mobs' problems against the package as the area leaves it."""
    mob_ctx, plan = checked.mob_ctx, design.mobs
    if mob_ctx is None or plan is None:
        return
    report = checked.report
    report.problems = mob_plan_problems(mob_ctx, plan, declared_ids(mob_ctx))
    if report.problems:
        return
    created = [*report.new_items, *new_items(mob_ctx, plan.mobs)]
    report.new_items = created
    report.problems = _prose_problems(
        "mob_prose", design.mob_prose, [m.id for m in plan.mobs], mob_prose_check
    ) + _prose_problems("item_prose", design.item_prose, created, mob_prose_check)


def _checked(ctx: Context, design: AreaDesign) -> _Checked:
    checked = _check_area(ctx, design)
    if checked.report.problems or checked.plan is None:
        return checked
    floor_new = set(checked.report.new_items)
    item_prose = {i: p for i, p in design.item_prose.items() if i in floor_new}
    prose = AreaProse(design.room_prose, design.key_prose, item_prose)
    area = assemble_area(ctx, checked.brief, checked.plan, prose)
    checked.area = area
    checked.mob_ctx = _with_contents(ctx, _contents_after(ctx, area))
    _check_mobs(checked, design)
    return checked


def check_design(ctx: Context, design: AreaDesign) -> DesignReport:
    """Every problem with the design, checked in the order the modes check their replies.

    The graph is checked before the prose, and the area before the mobs, so a
    problem upstream is not buried under the ones it causes.
    """
    return _checked(ctx, design).report


# -- applying ----------------------------------------------------------------------


def _mob_proposal(checked: _Checked, design: AreaDesign) -> Proposal | None:
    if checked.mob_ctx is None or design.mobs is None:
        return None
    return assemble_mobs(checked.mob_ctx, design.mobs, design.mob_prose, design.item_prose)


def _refusals(ctx: Context, proposal: Proposal) -> list[str]:
    return [r.reason for r in apply_ops(ctx.files, ctx.view, proposal.ops).refused]


def _final_contents(
    ctx: Context, checked: _Checked, mobs: Proposal | None
) -> tuple[dict[str, str], list[str]]:
    """The package's files after the area and the mobs, and the ops the writer refused."""
    assert checked.area is not None and checked.mob_ctx is not None
    contents = _contents_after(ctx, checked.area)
    notes = _refusals(ctx, checked.area)
    if mobs is not None:
        mob_ctx = checked.mob_ctx
        notes += _refusals(mob_ctx, mobs)
        contents = {**contents, **apply_ops(mob_ctx.files, mob_ctx.view, mobs.ops).files}
    return contents, notes


def _builder_test(ctx: Context, contents: dict[str, str]) -> str | None:
    files = {name: text for name, text in contents.items() if name != "package.star"}
    return testgen.builder_test(ctx.package, ctx.world, read_contents(files, ctx.package)) or None


def _changed(ctx: Context, contents: dict[str, str]) -> dict[str, str]:
    return {name: text for name, text in contents.items() if ctx.files.get(name) != text}


def apply_design(ctx: Context, design: AreaDesign) -> DesignResult:
    """The checked design assembled, staged, linted and tested; nothing is written."""
    checked = _checked(ctx, design)
    result = DesignResult(report=checked.report)
    if checked.report.problems or checked.area is None:
        return result
    mobs = _mob_proposal(checked, design)
    contents, result.notes = _final_contents(ctx, checked, mobs)
    test = _builder_test(ctx, contents)
    files = _changed(ctx, contents)
    staged = Proposal(ops=[], new_files=dict(files), summary="", prose_preview=[])
    check = stage_and_check(ctx, staged, test)
    if test is not None:
        files["builder_test.star"] = test
    result.files = files
    result.diff = unified_diff(dict(ctx.files), {**ctx.files, **files})
    result.summary = checked.area.summary + (f"; {mobs.summary}" if mobs is not None else "")
    result.prose_preview = checked.area.prose_preview + (mobs.prose_preview if mobs else [])
    result.notes.extend(w.message for w in check.lint_warnings)
    result.lint_errors = render_errors(ctx, staged, check.lint_errors)
    result.test_failure = check.test_failure
    result.link_ops = checked.area.link_ops
    result.link_package = checked.area.link_package
    return result


# -- writing -----------------------------------------------------------------------


def _read_texts(directory: Path) -> dict[str, str]:
    if not directory.is_dir():
        return {}
    return {p.name: p.read_bytes().decode("utf-8") for p in sorted(directory.glob("*.star"))}


def _link_writes(ctx: Context, result: DesignResult, notes: list[str]) -> dict[str, str]:
    """The linked package's changed files, keyed by their path in the world."""
    link_package = result.link_package
    if link_package is None or not result.link_ops:
        return {}
    link_dir = ctx.world_dir / link_package
    try:
        view = read_package(link_dir, link_package)
    except (StarParseError, OSError) as error:
        notes.append(f"refused (link): {error}")
        return {}
    applied = apply_ops(_read_texts(link_dir), view, result.link_ops)
    notes.extend(f"refused (link): {r.reason}" for r in applied.refused)
    return {f"{link_package}/{name}": text for name, text in applied.files.items()}


def _test_path(ctx: Context, result: DesignResult, notes: list[str]) -> dict[str, str]:
    """builder_test.star, or builder_test.star.new beside one that was edited by hand."""
    test = result.files.get("builder_test.star")
    if test is None:
        return {}
    path = ctx.world_dir / ctx.package / "builder_test.star"
    if path.is_file() and path.read_bytes().decode("utf-8") != test:
        notes.append("builder_test.star already exists; wrote builder_test.star.new")
        return {f"{ctx.package}/builder_test.star.new": test}
    return {f"{ctx.package}/builder_test.star": test}


def write_design(ctx: Context, result: DesignResult, *, with_link: bool) -> WriteResult:
    """Write an applied design that checked clean, then lint the package as written.

    The linked package's return exit is written only with ``with_link``.
    """
    if not result.ok:
        return WriteResult(notes=["the design did not check clean; nothing was written"])
    notes: list[str] = []
    writes = {
        f"{ctx.package}/{name}": text
        for name, text in result.files.items()
        if name != "builder_test.star"
    }
    writes.update(_test_path(ctx, result, notes))
    if with_link:
        writes.update(_link_writes(ctx, result, notes))
    for path, text in writes.items():
        write_atomic(ctx.world_dir / path, text)
    return WriteResult(files=sorted(writes), notes=notes, lint=ctx.engine.lint(ctx.package))
