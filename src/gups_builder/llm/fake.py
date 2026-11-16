"""A scripted stand-in for LlmClient: one reply per call, matched by role, in order."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from gups_builder.llm.client import ContextUsage, estimate_message_tokens

if TYPE_CHECKING:
    from collections.abc import Callable

    from gups_builder.llm.client import Message, Role


class FakeLlm:
    """Replays scripted replies. Each reply is matched by role, in order."""

    def __init__(
        self, replies: dict[Role, list[str]], models: dict[Role, str] | None = None
    ) -> None:
        self.replies = {role: list(items) for role, items in replies.items()}
        self.models = dict(models or {})
        self.calls: list[tuple[Role, list[Message], dict[str, Any] | None]] = []
        self.window = 32768
        self._usage_listener: Callable[[ContextUsage], None] | None = None

    def set_usage_listener(self, listener: Callable[[ContextUsage], None] | None) -> None:
        self._usage_listener = listener

    def model_name(self, role: Role) -> str:
        return self.models.get(role, "")

    def complete(
        self, role: Role, messages: list[Message], json_schema: dict[str, Any] | None
    ) -> str:
        self.calls.append((role, list(messages), json_schema))
        if self._usage_listener is not None:
            tokens = estimate_message_tokens(messages, json_schema)
            self._usage_listener(ContextUsage(role, tokens, self.window, estimated=True))
        if not self.replies.get(role):
            msg = f"{role} ran out of scripted replies"
            raise AssertionError(msg)
        return self.replies[role].pop(0)
