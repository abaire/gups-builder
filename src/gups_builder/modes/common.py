"""The helpers every mode shares: context, API-driven validation, staged checks.

A mode's build callback turns a plan into a Proposal; ``finalize`` stages it
against a copy of the world, runs lint and test, and sends only lint errors
back to the model for repair. A failing generated test is a builder bug, not
something the model can fix: it is reported, not repaired.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import BaseModel

from gups_builder.engine.lint import LintFinding  # noqa: TC001
from gups_builder.engine.staging import staged_world
from gups_builder.prompting import estimate_tokens, fit_lines, load_prompt
from gups_builder.star.writer import apply_ops

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from gups_builder.config import Config
    from gups_builder.engine.api import ApiEntry, ApiModel
    from gups_builder.engine.gups import TestRun
    from gups_builder.engine.world import WorldDescription
    from gups_builder.llm.client import LlmClient, Message
    from gups_builder.star.ops import StarOp
    from gups_builder.star.reader import PackageView


class Engine(Protocol):
    """What the modes and the REPL need from engine.gups.Gups; tests pass fakes."""

    def describe_world(self, world: Path | None = None) -> WorldDescription: ...

    def api(self) -> ApiModel: ...

    def lint(self, package: str, world: Path | None = None) -> list[LintFinding]: ...

    def test(self, package: str, world: Path | None = None) -> TestRun: ...


@dataclass
class Context:
    """One turn's inputs: the world, the package's current files and the models."""

    world_dir: Path
    world: WorldDescription
    api: ApiModel
    package: str
    view: PackageView
    files: dict[str, str]
    llm: LlmClient
    config: Config
    engine: Engine
    status: Callable[[str], None] = field(default_factory=lambda: lambda _: None)


@dataclass
class Proposal:
    """A mode's answer to a request: ops, new files, what to show the user."""

    ops: list[StarOp]
    new_files: dict[str, str]
    summary: str
    prose_preview: list[tuple[str, str, str]]
    notes: list[str] = field(default_factory=list)
    grid: dict[str, tuple[int, int, int]] | None = None
    link_ops: list[StarOp] = field(default_factory=list)
    link_package: str | None = None  # the package link_ops edit


class Check(BaseModel):
    """What stage_and_check found in the staged world."""

    lint_errors: list[LintFinding] = []
    lint_warnings: list[LintFinding] = []
    test_failure: str | None = None


class Turn(BaseModel):
    """How a mode answers a follow-up message.

    A turn is one of four kinds. An `edit` carries the mode's edits and becomes
    a proposal; a `question` or `chat` returns only its reply; an `out_of_scope`
    reply names the other mode (`other_mode`) and keeps the request (`note`) so
    the user can switch.
    """

    kind: Literal["edit", "question", "out_of_scope", "chat"]
    reply: str
    other_mode: Literal["area", "mob", "skill"] | None = None
    note: str | None = None


def brief_request(request: str, earlier: Sequence[tuple[str, str]] = ()) -> str:
    """The conversation a brief is drafted from, ending with the latest message.

    ``earlier`` is the brief's own conversation as (role, text) pairs, the user's
    request first, then each of the builder's questions and the user's answer.
    Each turn's text is the last turn's with lines added at the end, so a server's
    prefix cache keeps all of it.
    """
    turns = [*earlier, ("user", request)]
    lines = [f"{'You' if role == 'builder' else 'User'}: {text}" for role, text in turns]
    return (
        "The conversation, oldest first; the last User line is the latest message. Everything "
        "answered here is settled: keep it, and never ask about it again.\n" + "\n".join(lines)
    )


@dataclass
class TurnPrompt:
    """A follow-up turn's prompt parts.

    ``head`` (the rules, the catalogue and the package as it stands) and ``middle``
    (droppable context) change only when the package does; the history only grows.
    """

    head: str
    history: list[Message]
    message: str
    middle: Sequence[tuple[int, str]] = ()


def turn_sections(prompt: TurnPrompt, budget: int) -> list[tuple[int, str]]:
    """The turn's sections: what stays the same first, so a prefix cache keeps it, then
    the conversation, which gets all the room the rest leaves (``middle`` included, so a
    middle section drops only when the fixed parts alone overflow)."""
    bare = load_prompt("turn_tail", history="", message=prompt.message)
    fixed = prompt.head + bare + "".join(text for _, text in prompt.middle)
    lines = [f"{m.role}: {m.content}" for m in prompt.history]
    history = fit_lines(lines, budget - estimate_tokens(fixed))
    tail = load_prompt("turn_tail", history=history, message=prompt.message)
    return [(0, prompt.head), *prompt.middle, (0, tail)]


def one_question[B: BaseModel](brief: B) -> B:
    """A brief asks at most one question; the user answers it before the next is asked."""
    questions: list[str] = getattr(brief, "questions", [])
    if len(questions) <= 1:
        return brief
    return brief.model_copy(update={"questions": questions[:1]})


def _entry(api: ApiModel, decl: str) -> ApiEntry | None:
    try:
        return api.declaration(decl)
    except KeyError:
        return None


def allowed_values(api: ApiModel, _world: WorldDescription, decl: str, param: str) -> list[str]:
    """The param's allowed values; empty when the declaration or param is absent."""
    entry = _entry(api, decl)
    if entry is None:
        return []
    for p in entry.params:
        if p.name == param:
            return list(p.allowed_values)
    return []


def param_allowed(api: ApiModel, world: WorldDescription, decl: str, param: str) -> bool:
    """True when the param exists and its feature is on (or it has none)."""
    entry = _entry(api, decl)
    if entry is None:
        return False
    for p in entry.params:
        if p.name == param:
            return p.feature is None or world.has_feature(p.feature)
    return False


_ID_RUN = re.compile(r"[^a-z0-9]+")


def new_id(existing: set[str], wanted: str) -> str:
    """A snake_case id, [a-z][a-z0-9_]*, with a _2, _3 suffix on clashes."""
    base = _ID_RUN.sub("_", wanted.lower()).strip("_")
    if not base:
        base = "decl"
    if base[0].isdigit():
        base = "id_" + base
    if base not in existing:
        return base
    n = 2
    while f"{base}_{n}" in existing:
        n += 1
    return f"{base}_{n}"


def _is_zone(ctx: Context) -> bool:
    return any(p.path == ctx.package and p.kind == "zone" for p in ctx.world.packages)


def stage_and_check(ctx: Context, proposal: Proposal, builder_test: str | None) -> Check:
    """Apply the proposal to a copy of the world and run lint, then test.

    The copy is deleted when the check is done. The test runs when lint is
    clean and the package is a zone or the mode generated a test for it.
    """
    result = apply_ops(ctx.files, ctx.view, proposal.ops)
    with staged_world(ctx.world_dir) as staged:
        pkg = staged / ctx.package
        pkg.mkdir(parents=True, exist_ok=True)
        for name, text in {**result.files, **proposal.new_files}.items():
            (pkg / name).write_text(text, encoding="utf-8")
        if builder_test is not None:
            (pkg / "builder_test.star").write_text(builder_test, encoding="utf-8")
        ctx.status("Validating in staged world (gups lint)...")
        findings = ctx.engine.lint(ctx.package, staged)
        errors = [f for f in findings if f.severity == "error"]
        warnings = [f for f in findings if f.severity == "warning"]
        if errors:
            return Check(lint_errors=errors, lint_warnings=warnings)
        if _is_zone(ctx) or builder_test is not None:
            ctx.status("Running package tests (gups test)...")
            run = ctx.engine.test(ctx.package, staged)
            return Check(
                lint_errors=[], lint_warnings=warnings, test_failure=None if run.ok else run.output
            )
        return Check(lint_errors=[], lint_warnings=warnings)


def _file_in_package(file: str, package: str) -> str:
    prefix = package + "/"
    return file.removeprefix(prefix)


def _render(finding: LintFinding, contents: dict[str, str], package: str) -> str:
    """A finding as `<file>:<line>: <message>` plus the three lines around it."""
    header = (
        f"{finding.file}:{finding.line if finding.line is not None else '?'}: {finding.message}"
    )
    lines = contents.get(_file_in_package(finding.file, package), "").splitlines()
    if finding.line is None or not lines:
        return header
    parts = [header]
    for i in range(finding.line - 1, finding.line + 2):
        if 1 <= i <= len(lines):
            marker = ">" if i == finding.line else " "
            parts.append(f"{marker} {i}| {lines[i - 1]}")
    return "\n".join(parts)


def _render_errors(ctx: Context, proposal: Proposal, findings: list[LintFinding]) -> list[str]:
    result = apply_ops(ctx.files, ctx.view, proposal.ops)
    contents = {**ctx.files, **result.files, **proposal.new_files}
    return [_render(f, contents, ctx.package) for f in findings]


def finalize(
    ctx: Context,
    build: Callable[[list[str]], Proposal],
    builder_test: Callable[[Proposal], str | None] | None,
) -> Proposal:
    """Run build, stage and check, sending lint errors back to the model to repair.

    A first call is build([]); each lint failure calls build with the rendered
    errors, up to config.max_repairs repairs. After the last, the errors go
    into notes as NOT VALID. A failing test is a BUILDER BUG note, not a
    repair. Lint warnings always go into notes.
    """
    problems: list[str] = []
    for attempt in range(ctx.config.max_repairs + 1):
        proposal = build(problems)
        # A refused op would otherwise vanish from the staged files while staying in the
        # proposal; its reason is written for the user.
        proposal.notes.extend(
            refused.reason for refused in apply_ops(ctx.files, ctx.view, proposal.ops).refused
        )
        content = builder_test(proposal) if builder_test is not None else None
        check = stage_and_check(ctx, proposal, content)
        for warning in check.lint_warnings:
            proposal.notes.append(warning.message)
        if check.test_failure is not None:
            proposal.notes.append(
                "BUILDER BUG: the generated test failed; please report it:\n" + check.test_failure
            )
            return proposal
        if not check.lint_errors:
            return proposal
        rendered = _render_errors(ctx, proposal, check.lint_errors)
        if attempt == ctx.config.max_repairs:
            proposal.notes.extend(f"NOT VALID: {text}" for text in rendered)
            return proposal
        problems = rendered
    msg = "unreachable"
    raise AssertionError(msg)
