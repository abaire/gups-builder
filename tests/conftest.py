"""Shared fixtures: the gups binary, a template-world copy, and the minimal world."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from gups_builder.config import Config
from gups_builder.engine.api import ApiModel
from gups_builder.engine.gups import EngineError, Gups, TestRun
from gups_builder.engine.lint import LintFinding
from gups_builder.engine.world import WorldDescription
from gups_builder.llm.fake import FakeLlm
from gups_builder.modes.common import Context
from gups_builder.star.reader import PackageView

REPO_ROOT = Path(__file__).resolve().parent.parent

_SKIP_DIRS = (".git", ".gups", ".builder", "local-world-test")


def read_fixture(base: Path, name: str) -> str:
    return (base / name).read_text()


def load_api() -> ApiModel:
    return ApiModel.model_validate_json(
        read_fixture(REPO_ROOT / "tests" / "fixtures", "api_min.json")
    )


class FakeEngine:
    """An engine double: fixed world and API, scripted lint runs, counted test runs."""

    def __init__(
        self, world: WorldDescription, api: ApiModel, lint: list[list[LintFinding]] | None = None
    ) -> None:
        self._world = world
        self._api = api
        self._lint = list(lint or [])
        self.test_calls = 0

    def describe_world(self, world: Path | None = None) -> WorldDescription:
        return self._world

    def api(self) -> ApiModel:
        return self._api

    def lint(self, package: str, world: Path | None = None) -> list[LintFinding]:
        return self._lint.pop(0) if self._lint else []

    def test(self, package: str, world: Path | None = None) -> TestRun:
        self.test_calls += 1
        return TestRun(ok=True, output="")


@dataclass
class PackageState:
    """The package under edit: its name, the files read so far, and the parsed view."""

    package: str
    files: dict[str, str] = field(default_factory=dict)
    view: PackageView | None = None


def build_context(tmp_path: Path, llm: FakeLlm, engine: FakeEngine, state: PackageState) -> Context:
    """A creation context over ``engine``; the view defaults to an empty package."""
    empty = PackageView(path=state.package, files={}, decls=[], opaque=[])
    return Context(
        world_dir=tmp_path,
        world=engine.describe_world(),
        api=engine.api(),
        package=state.package,
        view=state.view or empty,
        files=dict(state.files),
        llm=llm,
        config=Config(max_repairs=1),
        engine=engine,
    )


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
