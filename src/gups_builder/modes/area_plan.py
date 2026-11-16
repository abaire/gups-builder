"""Area planning mode: one conversational turn on a draft brief, and folding old turns.

A planning conversation can outgrow the logic model's context. Its messages are
never simply dropped: when the part not yet folded no longer fits, a separate
call reads the oldest of them and returns the decisions they settled that the
brief's ``decisions`` list does not hold yet. Code appends those, and the folded
messages leave the prompt for good (the session keeps a cursor past them).

Each prompt puts what stays the same first (rules, rooms, link candidates) and
what changes last (history, draft brief, message), so a server's prefix cache
keeps the start; a fold moves the history's start once, then it holds again.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic import BaseModel

from gups_builder.llm.client import Message
from gups_builder.llm.structured import call_structured
from gups_builder.modes.area import _candidate_links, _planning_rooms, _size_words_text
from gups_builder.modes.area_checks import (
    AreaBrief,
    AreaPlanTurn,
    brief_problems,
    normalize_brief,
)
from gups_builder.modes.common import one_question
from gups_builder.prompting import estimate_tokens, fit, fit_lines, load_prompt

if TYPE_CHECKING:
    from gups_builder.grid.model import Coord
    from gups_builder.modes.common import Context

_FOLDED_NOTE = "(earlier messages are folded into the brief's decisions)"


class FoldedDecisions(BaseModel):
    """The decisions settled in the folded messages that the list does not hold yet."""

    decisions: list[str] = []


@dataclass
class PlanTurnResult:
    """A planning turn, the messages folded before it, and the decisions it lost."""

    turn: AreaPlanTurn
    folded: int = 0
    dropped: list[str] = field(default_factory=list)


@dataclass
class PlanRequest:
    """A planning turn's input; ``history`` starts after the messages already folded."""

    message: str
    history: list[Message]
    current_brief: AreaBrief | None
    grid_coords: dict[str, Coord] | None = None
    folded_before: bool = False


def _lines(history: list[Message]) -> list[str]:
    return [f"{m.role}: {m.content}" for m in history]


def _draft_text(brief: AreaBrief | None, decisions: list[str]) -> str:
    if brief is not None:
        return brief.model_dump_json(indent=2)
    return FoldedDecisions(decisions=decisions).model_dump_json(indent=2) if decisions else "none"


def _fold_count(lines: list[str], room: int) -> int:
    """How many of the oldest lines to fold so the rest fills at most half of ``room``.

    Folding down to half, not just under the limit, makes the next fold many turns away.
    """
    if sum(estimate_tokens(line) for line in lines) <= room:
        return 0
    kept = 0
    used = 0
    for line in reversed(lines):
        used += estimate_tokens(line)
        if used > room // 2 and kept:
            break
        kept += 1
    return len(lines) - kept


def fold_decisions(ctx: Context, decisions: list[str], messages: list[Message]) -> list[str]:
    """The decisions ``messages`` settle that ``decisions`` does not hold yet."""
    prompt = load_prompt(
        "area_plan_fold",
        decisions="\n".join(f"- {d}" for d in decisions) or "none",
        messages="\n".join(_lines(messages)),
    )
    folded = call_structured(
        ctx.llm,
        "logic",
        [Message("system", load_prompt("system_logic")), Message("user", prompt)],
        FoldedDecisions,
        ctx.config.max_repairs,
        status=ctx.status,
        step="folding old planning messages into decisions",
    )
    return [d for d in folded.decisions if d not in decisions]


@dataclass
class _Prompt:
    head: str
    links: str
    room: int


def _prompt_parts(ctx: Context, request: PlanRequest, budget: int) -> _Prompt:
    head = load_prompt(
        "area_plan_turn",
        size_words=_size_words_text(),
        existing_rooms=_planning_rooms(ctx, request.grid_coords),
    )
    links = "Rooms in other packages that could be linked:\n" + _candidate_links(ctx)
    bare_tail = load_prompt(
        "area_plan_turn_tail",
        history=_FOLDED_NOTE,
        current_brief=_draft_text(request.current_brief, []),
        message=request.message,
    )
    # The links count against the history's room, so they drop only when the fixed parts
    # alone overflow, not back and forth as the history grows and folds.
    return _Prompt(head, links, budget - estimate_tokens(head + links + bare_tail))


def _with_decisions(brief: AreaBrief | None, added: list[str]) -> AreaBrief | None:
    if brief is None or not added:
        return brief
    return brief.model_copy(update={"decisions": [*brief.decisions, *added]})


def plan_area_turn(ctx: Context, request: PlanRequest) -> PlanTurnResult:
    """One conversational planning turn: fold old messages if need be, then update the brief."""
    budget = ctx.config.logic.context_tokens - ctx.config.logic.max_tokens
    parts = _prompt_parts(ctx, request, budget)
    history = request.history
    folded = _fold_count(_lines(history), parts.room)
    added = fold_decisions(ctx, _known_decisions(request), history[:folded]) if folded else []
    brief = _with_decisions(request.current_brief, added)
    history_lines = _lines(history[folded:])
    if folded or request.folded_before:
        history_lines = [_FOLDED_NOTE, *history_lines]
    tail = load_prompt(
        "area_plan_turn_tail",
        history=fit_lines(history_lines, parts.room),
        current_brief=_draft_text(brief, added),
        message=request.message,
    )
    turn = call_structured(
        ctx.llm,
        "logic",
        [
            Message("system", load_prompt("system_logic")),
            Message("user", fit([(0, parts.head), (2, parts.links), (0, tail)], budget)),
        ],
        AreaPlanTurn,
        ctx.config.max_repairs,
        extra_check=lambda t: brief_problems(ctx, t.brief),
        status=ctx.status,
        step="updating area plan",
        normalize=lambda t: t.model_copy(
            update={"brief": one_question(normalize_brief(ctx, t.brief))}
        ),
    )
    before = [*(brief.decisions if brief is not None else []), *added]
    dropped = [d for d in dict.fromkeys(before) if d not in turn.brief.decisions]
    return PlanTurnResult(turn, folded, dropped)


def _known_decisions(request: PlanRequest) -> list[str]:
    return list(request.current_brief.decisions) if request.current_brief is not None else []
