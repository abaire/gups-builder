"""Shared fixtures: the gups binary, a template-world copy, and the minimal world."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from gups_builder.engine.gups import EngineError, Gups
from gups_builder.engine.world import WorldDescription

REPO_ROOT = Path(__file__).resolve().parent.parent

_SKIP_DIRS = (".git", ".gups", ".builder", "local-world-test")


@pytest.fixture(scope="session")
def gups_bin() -> Path:
    try:
        return Gups.find(None, os.environ)
    except EngineError as error:
        pytest.skip(f"gups binary not found ({error}); set GUPS_BIN or add gups to PATH")


def _template_source() -> Path:
    """The template world: $GUPS_TEMPLATE_WORLD, else the sibling checkout."""
    env_path = os.environ.get("GUPS_TEMPLATE_WORLD")
    return Path(env_path) if env_path else REPO_ROOT.parent / "gups-engine" / "template-world"


def _copy_template_world(dest: Path) -> Path:
    source = _template_source()
    if not (source / "MODULE.gups").is_file():
        pytest.skip(f"no template world at {source}; set GUPS_TEMPLATE_WORLD")
    shutil.copytree(source, dest, ignore=lambda _, names: [n for n in names if n in _SKIP_DIRS])
    return dest


@pytest.fixture(scope="session")
def template_world(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return _copy_template_world(tmp_path_factory.mktemp("template-world") / "world")


@pytest.fixture()
def fresh_template_world(tmp_path: Path) -> Path:
    """A world copy alone for a test that rewrites a package another engine test creates.

    The shared ``template_world`` copy is session-scoped: the end-to-end test rewrites
    ``zones/mansion`` there, and the REPL engine test (test_repl.py, which runs later in
    file order) creates that same package expecting it absent.
    """
    return _copy_template_world(tmp_path / "world")


@pytest.fixture(scope="session")
def world_min() -> WorldDescription:
    fixture = REPO_ROOT / "tests" / "fixtures" / "describe_world_min.json"
    return WorldDescription.model_validate_json(fixture.read_text())


@pytest.fixture(scope="session")
def world_mansion() -> WorldDescription:
    fixture = REPO_ROOT / "tests" / "fixtures" / "describe_world_mansion.json"
    return WorldDescription.model_validate_json(fixture.read_text())
