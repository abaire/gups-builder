"""One model call whose reply must validate as a pydantic model, with repair on failure."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from pydantic import BaseModel, ValidationError

from gups_builder.llm.client import LlmClient, Message, Role

if TYPE_CHECKING:
    from collections.abc import Callable

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n```\s*$", re.DOTALL)


def _strip_fence(reply: str) -> str:
    match = _FENCE.match(reply)
    return match.group(1) if match else reply


class StructuredCallError(Exception):
    """Every attempt of a structured call failed; `attempts` and `errors` explain why.

    ``errors`` holds one entry per attempt, so a caller can show how the repairs went,
    not only how the last one failed. ``note`` says why repairing stopped early, if it did.
    """

    def __init__(
        self,
        attempts: list[str],
        errors: list[str],
        step: str | None = None,
        note: str | None = None,
    ) -> None:
        what = f" for {step}" if step else ""
        why = f" ({note})" if note else ""
        super().__init__(
            f"model failed to produce a valid reply{what} after {len(attempts)} attempts: "
            f"{errors[-1]}{why}"
        )
        self.attempts = attempts
        self.errors = errors
        self.step = step
        self.note = note


@dataclass(frozen=True)
class _Checks[T: BaseModel]:
    """What a parsed reply goes through: ``normalize`` first, then ``extra_check``."""

    extra_check: Callable[[T], list[str]] | None = None
    normalize: Callable[[T], T] | None = None


def _try_parse[T: BaseModel](
    reply: str, model: type[T], checks: _Checks[T]
) -> tuple[T | None, str | None]:
    """The parsed model, or a message telling the model what to fix."""
    try:
        parsed = model.model_validate_json(_strip_fence(reply))
    except ValidationError as error:
        lines = [f"- {err['loc']}: {err['msg']}" for err in error.errors()]
        return None, "\n".join(lines)
    if checks.normalize is not None:
        parsed = checks.normalize(parsed)
    extra_check = checks.extra_check
    if extra_check is not None:
        complaints = extra_check(parsed)
        if complaints:
            return None, "\n".join(f"- {item}" for item in complaints)
    return parsed, None


def _same_reply(a: str, b: str) -> bool:
    """Whether two replies say the same thing, ignoring a fence and JSON whitespace."""
    a, b = _strip_fence(a).strip(), _strip_fence(b).strip()
    if a == b:
        return True
    try:
        return bool(json.loads(a) == json.loads(b))
    except json.JSONDecodeError:
        return False


def _repeat_note(attempts: list[str]) -> str:
    """Why repairing stopped: the model sent its last reply back unchanged."""
    n = len(attempts)
    return (
        f"attempt {n} sent back attempt {n - 1}'s reply unchanged, "
        "so the remaining repairs were skipped"
    )


def _model_label(llm: LlmClient, role: Role) -> str:
    name = llm.model_name(role)
    return f"{role} model ({name})" if name else f"{role} model"


def _progress(who: str, desc: str, attempt: int, max_repairs: int) -> str:
    if attempt == 0:
        return f"Waiting for {who} — {desc}..."
    return f"Repairing {desc} ({who}, attempt {attempt}/{max_repairs})..."


def _asking_for_repair(history: list[Message], reply: str, error: str) -> list[Message]:
    note = f"That reply was not valid:\n{error}\nReply again with the corrected JSON only."
    return [*history, Message("assistant", reply), Message("user", note)]


def _asking_afresh(messages: list[Message], reply: str, error: str) -> list[Message]:
    """The first request again, with the rejected reply and its errors folded into it.

    A model that sent its reply back unchanged would see the same conversation again
    and send it back once more; one new request, with the reply quoted as text rather
    than as its own turn, gives it a different prompt to answer.
    """
    note = (
        f"A previous reply to this request was:\n{reply}\n"
        f"It was not valid:\n{error}\n"
        "Reply with a corrected JSON reply only, changing what each problem asks."
    )
    *before, last = messages
    return [*before, Message(last.role, f"{last.content}\n\n{note}")]


@dataclass
class _Repairs:
    """Where a call's repairs stand: the next history, and whether it has started afresh."""

    messages: list[Message]
    history: list[Message]
    restarted: bool = False

    def next_history(self, attempts: list[str], error: str) -> list[Message] | None:
        """The history for the next attempt, or None when repairing should stop."""
        if not _same_reply_twice(attempts):
            self.history = _asking_for_repair(self.history, attempts[-1], error)
            return self.history
        if self.restarted:
            return None
        self.restarted = True
        self.history = _asking_afresh(self.messages, attempts[-1], error)
        return self.history


def _same_reply_twice(attempts: list[str]) -> bool:
    return len(attempts) >= 2 and _same_reply(attempts[-1], attempts[-2])


def call_structured[T: BaseModel](
    llm: LlmClient,
    role: Role,
    messages: list[Message],
    model: type[T],
    max_repairs: int,
    extra_check: Callable[[T], list[str]] | None = None,
    status: Callable[[str], None] | None = None,
    step: str | None = None,
    normalize: Callable[[T], T] | None = None,
) -> T:
    """The model's reply as ``model``, repaired up to ``max_repairs`` times.

    ``normalize`` puts a parsed reply into canonical form (filling in what the builder
    already knows) before ``extra_check`` judges it; the normalized reply is returned.
    A reply that repeats the one before it gets one fresh request instead of a repair;
    a second repeat ends the call.
    """
    checks = _Checks(extra_check, normalize)
    repairs = _Repairs(list(messages), list(messages))
    history = repairs.history
    attempts: list[str] = []
    errors: list[str] = []
    report = status or (lambda _: None)
    who = _model_label(llm, role)
    desc = step if step is not None else model.__name__
    for attempt in range(max_repairs + 1):
        report(_progress(who, desc, attempt, max_repairs))
        reply = llm.complete(role, history, model.model_json_schema())
        attempts.append(reply)
        parsed, error = _try_parse(reply, model, checks)
        if parsed is not None:
            return parsed
        assert error is not None  # noqa: S101
        errors.append(error)
        next_history = repairs.next_history(attempts, error)
        if next_history is None:
            raise StructuredCallError(attempts, errors, step, _repeat_note(attempts))
        history = next_history
    note = _repeat_note(attempts) if _same_reply_twice(attempts) else None
    raise StructuredCallError(attempts, errors, step, note)
