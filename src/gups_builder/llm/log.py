"""The JSONL call log: one line per model call, readable later by a human or a script."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .client import Message, Role


class CallLog:
    def __init__(self, path: Path) -> None:
        self.path = path

    def record(
        self,
        role: Role,
        model: str,
        messages: list[Message],
        reply: str,
        seconds: float,
        error: str | None,
    ) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "ts": datetime.now(UTC).isoformat(),
            "role": role,
            "model": model,
            "messages": [asdict(m) for m in messages],
            "reply": reply,
            "seconds": seconds,
            "error": error,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry) + "\n")
