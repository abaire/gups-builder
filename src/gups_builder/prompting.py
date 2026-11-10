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


def load_prompt(name: str, **fields: str) -> str:
    """The text of ``prompts/<name>.md`` with every field substituted."""
    try:
        text = (_PROMPTS / f"{name}.md").read_text(encoding="utf-8")
    except FileNotFoundError:
        raise FileNotFoundError(f"no prompt template named '{name}'") from None
    try:
        return text.format_map(fields)
    except KeyError as error:
        raise ValueError(f"prompt '{name}' is missing the field '{error.args[0]}'") from error


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
