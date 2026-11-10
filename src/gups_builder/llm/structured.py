"""One model call whose reply must validate as a pydantic model, with repair on failure."""

from __future__ import annotations

import re
from collections.abc import Callable

from pydantic import BaseModel, ValidationError

from .client import LlmClient, Message, Role

_FENCE = re.compile(r"^\s*```[a-zA-Z0-9_-]*\s*\n(.*?)\n```\s*$", re.DOTALL)


def _strip_fence(reply: str) -> str:
    match = _FENCE.match(reply)
    return match.group(1) if match else reply


class StructuredCallFailed(Exception):
    """Every attempt of a structured call failed; `attempts` and `errors` explain why."""

    def __init__(self, attempts: list[str], errors: list[str]) -> None:
        super().__init__(
            f"model failed to produce a valid reply after {len(attempts)} attempts: {errors[-1]}"
        )
        self.attempts = attempts
        self.errors = errors


def _try_parse[T: BaseModel](
    reply: str, model: type[T], extra_check: Callable[[T], list[str]] | None
) -> tuple[T | None, str | None]:
    """The parsed model, or a message telling the model what to fix."""
    try:
        parsed = model.model_validate_json(_strip_fence(reply))
    except ValidationError as error:
        lines = [f"- {err['loc']}: {err['msg']}" for err in error.errors()]
        return None, "\n".join(lines)
    if extra_check is not None:
        complaints = extra_check(parsed)
        if complaints:
            return None, "\n".join(f"- {item}" for item in complaints)
    return parsed, None


def call_structured[T: BaseModel](
    llm: LlmClient,
    role: Role,
    messages: list[Message],
    model: type[T],
    max_repairs: int,
    extra_check: Callable[[T], list[str]] | None = None,
) -> T:
    history = list(messages)
    attempts: list[str] = []
    errors: list[str] = []
    for _ in range(max_repairs + 1):
        reply = llm.complete(role, history, model.model_json_schema())
        attempts.append(reply)
        parsed, error = _try_parse(reply, model, extra_check)
        if parsed is not None:
            return parsed
        assert error is not None
        errors.append(error)
        history = history + [
            Message("assistant", reply),
            Message(
                "user",
                f"That reply was not valid:\n{error}\nReply again with the corrected JSON only.",
            ),
        ]
    raise StructuredCallFailed(attempts, errors)
