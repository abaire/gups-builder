"""Temporary copies of a world for validating proposals before touching the real files."""

from __future__ import annotations

import shutil
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

# Not world content: build artifacts, builder state and one-off test databases.
_SKIP_DIRS = frozenset({".git", ".gups", ".builder", "local-world-test"})


def _ignore(_dir: str, names: list[str]) -> list[str]:
    return [name for name in names if name in _SKIP_DIRS]


@contextmanager
def staged_world(world: Path) -> Iterator[Path]:
    """Copy [world] to a temporary directory; the copy is deleted on exit.

    `plugins/` is kept: the world's jar checksums must still match after staging.
    """
    with tempfile.TemporaryDirectory(prefix="gups-builder-staged-") as tmp:
        staged = Path(tmp) / "world"
        shutil.copytree(world, staged, ignore=_ignore)
        yield staged
