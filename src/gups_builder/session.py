"""The persisted session: state on disk, created and loaded by name.

A session is one interactive conversation about one package in one mode. Its
state is a JSON document at ``<world>/.builder/sessions/<name>.json``, written
atomically so a crash never leaves a half-written file. The first use of the
builder in a world drops a ``.builder/.gitignore`` holding ``*`` so nothing in
``.builder/`` is committed by accident.
"""

from __future__ import annotations

import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel

from gups_builder.star.ops import StarOp

Mode = Literal["area", "mob", "skill"]

_GITIGNORE = "*\n"


def _now() -> str:
    return datetime.now(UTC).isoformat()


class PendingChange(BaseModel):
    """The proposal the user has not yet written, and what it was built against."""

    ops: list[StarOp]
    new_files: dict[str, str]
    link_ops: list[StarOp] = []
    link_package: str | None = None  # the package link_ops edit; a StarOp carries no package
    base_hashes: dict[str, str]  # file name -> sha256 the ops were built against
    summary: str
    notes: list[str] = []
    # Room coordinates after the change; they become the session's grid only on /write,
    # so a discarded proposal leaves no coordinates behind.
    grid: dict[str, tuple[int, int, int]] | None = None


class SessionState(BaseModel):
    """Everything the builder remembers about one conversation."""

    version: Literal[1] = 1
    name: str
    mode: Mode
    package: str
    created: str
    updated: str
    history: list[tuple[Literal["user", "builder"], str]] = []
    grid: dict[str, tuple[int, int, int]] = {}
    notes: list[tuple[str, str]] = []  # (mode, request) of out-of-scope turns
    pending: PendingChange | None = None
    builder_test_hash: str | None = None  # sha256 of builder_test.star as last written
    # Files are keyed by their path in the world ("zones/mansion/rooms.star"), because a write
    # can also touch the linked package's files.
    undo: dict[str, str | None] | None = None  # file -> previous content (None: absent) for /undo
    undo_grid: dict[str, tuple[int, int, int]] | None = None  # the grid before the last /write
    written_hashes: dict[str, str] = {}  # file -> sha256 right after the last /write


class Session:
    """A loaded session and the world it lives in; saves are atomic."""

    def __init__(self, world: Path, state: SessionState) -> None:
        self.world = world
        self.state = state

    @staticmethod
    def path(world: Path, name: str) -> Path:
        return world / ".builder" / "sessions" / f"{name}.json"

    @classmethod
    def create(cls, world: Path, mode: Mode, package: str, name: str | None = None) -> Session:
        """A new session; the name defaults to the package's last segment, then -2, -3."""
        base = name if name is not None else package.rsplit("/", 1)[-1]
        chosen = base
        n = 2
        while cls.path(world, chosen).exists():
            chosen = f"{base}-{n}"
            n += 1
        builder = world / ".builder"
        builder.mkdir(parents=True, exist_ok=True)
        gitignore = builder / ".gitignore"
        if not gitignore.exists():
            gitignore.write_text(_GITIGNORE, encoding="utf-8")
        state = SessionState(
            name=chosen, mode=mode, package=package, created=_now(), updated=_now()
        )
        session = cls(world, state)
        session.save()
        return session

    @classmethod
    def load(cls, world: Path, name: str) -> Session:
        text = cls.path(world, name).read_text(encoding="utf-8")
        return cls(world, SessionState.model_validate_json(text))

    def save(self) -> None:
        """Write the state to a temp file, then replace the session file atomically."""
        self.state.updated = _now()
        path = self.path(self.world, self.state.name)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(self.state.model_dump_json())
            os.replace(tmp, path)
        except BaseException:
            if os.path.exists(tmp):
                os.unlink(tmp)
            raise
