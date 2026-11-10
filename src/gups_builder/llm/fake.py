"""A scripted stand-in for LlmClient: one reply per call, matched by role, in order."""

from __future__ import annotations

from typing import Any

from .client import Message, Role


class FakeLlm:
    """Replays scripted replies. Each reply is matched by role, in order."""

    def __init__(self, replies: dict[Role, list[str]]) -> None:
        self.replies = {role: list(items) for role, items in replies.items()}
        self.calls: list[tuple[Role, list[Message], dict[str, Any] | None]] = []

    def complete(
        self, role: Role, messages: list[Message], json_schema: dict[str, Any] | None
    ) -> str:
        self.calls.append((role, list(messages), json_schema))
        if not self.replies.get(role):
            raise AssertionError(f"{role} ran out of scripted replies")
        return self.replies[role].pop(0)
