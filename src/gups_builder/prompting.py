"""Prompt templates and context budgeting.

A prompt is a Markdown file in ``prompts/`` with ``{field}`` placeholders. A
model prompt is built from ``(priority, text)`` sections, kept in order, with
the least important sections dropped until the token estimate fits the role's
budget; the request, the rules and the current target declarations are
priority 0 and are never dropped.
"""

from __future__ import annotations

from pathlib import Path

_PROMPTS = Path(__file__).parent / "prompts"


def prompt_template(name: str) -> str:
    """The text of ``prompts/<name>.md`` as written, its placeholders left in."""
    try:
        return (_PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        msg = f"no prompt template named '{name}'"
        raise FileNotFoundError(msg) from None


def load_prompt(name: str, **fields: str) -> str:
    """The text of ``prompts/<name>.md`` with every field substituted."""
    text = prompt_template(name)
    try:
        return text.format_map(fields)
    except KeyError as error:
        msg = f"prompt '{name}' is missing the field '{error.args[0]}'"
        raise ValueError(msg) from error


def estimate_tokens(text: str) -> int:
    """A cheap estimate: a token is about four characters."""
    return len(text) // 4 + 1


def fit(sections: list[tuple[int, str]], budget: int) -> str:
    """The sections joined in order, dropping from the highest priority number down.

    Priority 0 is never dropped, even when it alone exceeds the budget.
    """
    kept = list(sections)
    while sum(estimate_tokens(text) for _, text in kept) > budget:
        droppable = [priority for priority, _ in kept if priority > 0]
        if not droppable:
            break
        worst = max(droppable)
        for i in range(len(kept) - 1, -1, -1):
            if kept[i][0] == worst:
                del kept[i]
                break
    return "\n\n".join(text for _, text in kept)


_DROP_STEP = 8  # lines left out at a time, so the kept text starts at the same line for a while


def fit_lines(lines: list[str], budget: int, *, empty: str = "none") -> str:
    """The newest lines whose estimate fits ``budget``, oldest first.

    Lines are left out from the oldest, in steps of eight: a window that slid by one
    line each turn would change the text's start every turn and miss a server's
    prefix cache every time. A first line says how many were left out. The newest
    line is always kept.
    """
    if not lines:
        return empty
    used = 0
    fits = 0
    for line in reversed(lines):
        cost = estimate_tokens(line)
        if fits and used + cost > budget:
            break
        used += cost
        fits += 1
    dropped = len(lines) - fits
    if dropped:
        dropped = min(len(lines) - 1, -(-dropped // _DROP_STEP) * _DROP_STEP)
    head = [f"({dropped} earlier message(s) left out for space)"] if dropped else []
    return "\n".join(head + lines[dropped:])
