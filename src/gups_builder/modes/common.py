"""The helpers every mode shares: context, API-driven validation, staged checks.

A mode's build callback turns a plan into a Proposal; ``finalize`` stages it
against a copy of the world, runs lint and test, and sends only lint errors
back to the model for repair. A failing generated test is a builder bug, not
something the model can fix: it is reported, not repaired.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

from pydantic import BaseModel

from gups_builder.config import Config
from gups_builder.engine.api import ApiEntry, ApiModel
from gups_builder.engine.gups import TestRun
from gups_builder.engine.lint import LintFinding
from gups_builder.engine.staging import staged_world
from gups_builder.engine.world import WorldDescription
from gups_builder.llm.client import LlmClient
from gups_builder.star.ops import StarOp
from gups_builder.star.reader import PackageView
from gups_builder.star.writer import apply_ops


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


def _entry(api: ApiModel, decl: str) -> ApiEntry | None:
    try:
        return api.declaration(decl)
    except KeyError:
        return None


def allowed_values(api: ApiModel, world: WorldDescription, decl: str, param: str) -> list[str]:
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
        findings = ctx.engine.lint(ctx.package, staged)
        errors = [f for f in findings if f.severity == "error"]
        warnings = [f for f in findings if f.severity == "warning"]
        if errors:
            return Check(lint_errors=errors, lint_warnings=warnings)
        if _is_zone(ctx) or builder_test is not None:
            run = ctx.engine.test(ctx.package, staged)
            return Check(
                lint_errors=[], lint_warnings=warnings, test_failure=None if run.ok else run.output
            )
        return Check(lint_errors=[], lint_warnings=warnings)


def _file_in_package(file: str, package: str) -> str:
    prefix = package + "/"
    return file[len(prefix) :] if file.startswith(prefix) else file


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
    raise AssertionError("unreachable")
