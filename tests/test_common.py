"""The shared mode helpers: validation against the API, staged checks and finalize."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from gups_builder.config import Config
from gups_builder.engine.api import ApiModel
from gups_builder.engine.gups import TestRun
from gups_builder.engine.lint import LintFinding
from gups_builder.engine.world import Schema, WorldDescription
from gups_builder.llm.fake import FakeLlm
from gups_builder.modes.common import (
    Check,
    Context,
    Proposal,
    allowed_values,
    finalize,
    new_id,
    param_allowed,
    stage_and_check,
)
from gups_builder.star.ops import AddTodo
from gups_builder.star.reader import PackageView

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture
def api() -> ApiModel:
    return ApiModel.model_validate_json((FIXTURES / "api_min.json").read_text())


class FakeEngine:
    """Answers scripted lint and test runs, in order; records what it saw."""

    def __init__(
        self,
        world: WorldDescription,
        api: ApiModel,
        lint: list[list[LintFinding]],
        tests: list[TestRun] | None = None,
    ) -> None:
        self._world = world
        self._api = api
        self._lint = [list(run) for run in lint]
        self._tests = list(tests or [])
        self.lint_calls = 0
        self.test_calls = 0
        self.package_files: dict[str, str] = {}
        self.test_world: Path | None = None

    def describe_world(self, world: Path | None = None) -> WorldDescription:
        return self._world

    def api(self) -> ApiModel:
        return self._api

    def lint(self, package: str, world: Path | None = None) -> list[LintFinding]:
        self.lint_calls += 1
        if world is not None:
            pkg = world / package
            self.package_files = (
                {p.name: p.read_text() for p in pkg.iterdir() if p.is_file()}
                if pkg.is_dir()
                else {}
            )
        if self._lint:
            return self._lint.pop(0)
        return []

    def test(self, package: str, world: Path | None = None) -> TestRun:
        self.test_calls += 1
        self.test_world = world
        if self._tests:
            return self._tests.pop(0)
        return TestRun(ok=True, output="")


def _world() -> WorldDescription:
    return WorldDescription(
        format=1,
        api_version=1,
        engine_version="t",
        schema=Schema(unarmed_damage_type="bludgeon"),
    )


def _ctx(
    world_dir: Path,
    engine: FakeEngine,
    package: str = "zones/mansion",
    files: dict[str, str] | None = None,
) -> Context:
    return Context(
        world_dir=world_dir,
        world=engine.describe_world(),
        api=engine.api(),
        package=package,
        view=PackageView(path=package, files={}, decls=[], opaque=[]),
        files=files or {},
        llm=FakeLlm({}),
        config=Config(max_repairs=1),
        engine=engine,
    )


def _proposal(ops: list[Any] | None = None, new_files: dict[str, str] | None = None) -> Proposal:
    return Proposal(ops=ops or [], new_files=new_files or {}, summary="s", prose_preview=[])


def test_finalize_with_clean_lint_returns_the_first_proposal(tmp_path: Path, api: ApiModel) -> None:
    engine = FakeEngine(
        _world(),
        api,
        lint=[[]],
    )
    calls: list[list[str]] = []

    def build(problems: list[str]) -> Proposal:
        calls.append(problems)
        return _proposal()

    ctx = _ctx(tmp_path, engine)
    proposal = finalize(ctx, build, None)
    assert calls == [[]]
    assert proposal is not None
    assert proposal.notes == []
    assert engine.test_calls == 0  # not a zone, no generated test


def test_finalize_sends_rendered_lint_errors_back_and_gives_up(
    tmp_path: Path, api: ApiModel
) -> None:
    engine = FakeEngine(
        _world(),
        api,
        lint=[
            [LintFinding("error", "zones/mansion/rooms.star", 7, "unexpected token")],
            [LintFinding("error", "zones/mansion/rooms.star", 7, "unexpected token")],
        ],
    )
    calls: list[list[str]] = []

    def build(problems: list[str]) -> Proposal:
        calls.append(problems)
        return _proposal()

    ctx = _ctx(
        tmp_path,
        engine,
        files={"rooms.star": "\n".join(f"line {i}" for i in range(1, 11))},
    )
    proposal = finalize(ctx, build, None)
    assert len(calls) == 2
    assert calls[0] == []
    assert calls[1][0].startswith("zones/mansion/rooms.star:7: unexpected token")
    assert "line 7" in calls[1][0]
    assert calls[1][0].count("NOT VALID") == 0
    assert any(note.startswith("NOT VALID: zones/mansion/rooms.star:7:") for note in proposal.notes)
    assert engine.lint_calls == 2


def test_finalize_test_failure_is_a_builder_bug_not_a_repair(tmp_path: Path, api: ApiModel) -> None:
    engine = FakeEngine(
        _world(),
        api,
        lint=[[]],
        tests=[TestRun(ok=False, output="FAIL test_builder_hall_exits")],
    )
    calls: list[list[str]] = []

    def build(problems: list[str]) -> Proposal:
        calls.append(problems)
        return _proposal()

    ctx = _ctx(tmp_path, engine)
    proposal = finalize(ctx, build, lambda p: "def test_builder(t):\n    t.world(seed = 1)\n")
    assert calls == [[]]
    assert engine.test_calls == 1
    assert proposal.notes == [
        "BUILDER BUG: the generated test failed; please report it:\nFAIL test_builder_hall_exits"
    ]


def test_finalize_puts_refused_ops_in_the_notes(tmp_path: Path, api: ApiModel) -> None:
    engine = FakeEngine(_world(), api, lint=[[]])

    def build(problems: list[str]) -> Proposal:
        return _proposal(ops=[AddTodo(kind="room", id="ghost", text="haunt it")])

    proposal = finalize(_ctx(tmp_path, engine), build, None)
    assert proposal.notes == ["room 'ghost' is not a plain declaration in this package"]


def test_stage_and_check_writes_ops_new_files_and_test_into_the_stage(
    tmp_path: Path, api: ApiModel
) -> None:
    engine = FakeEngine(
        _world(),
        api,
        lint=[[]],
    )
    ctx = _ctx(tmp_path, engine)
    check = stage_and_check(ctx, _proposal(new_files={"rooms.star": "room('hall')\n"}), "TEST\n")
    assert check == Check(lint_errors=[], lint_warnings=[], test_failure=None)
    assert engine.package_files == {"rooms.star": "room('hall')\n", "builder_test.star": "TEST\n"}
    assert engine.test_calls == 1  # a generated test was given, so the test runs


def test_stage_and_check_runs_the_test_for_a_zone(
    tmp_path: Path, api: ApiModel, world_min: WorldDescription
) -> None:
    engine = FakeEngine(world_min, api, lint=[[]])
    ctx = _ctx(tmp_path, engine, package="zones/tutorial")
    stage_and_check(ctx, _proposal(), None)
    assert engine.test_calls == 1
    assert engine.test_world is not None
    assert engine.test_world != tmp_path


def test_stage_and_check_splits_lint_errors_and_warnings(tmp_path: Path, api: ApiModel) -> None:
    engine = FakeEngine(
        _world(),
        api,
        lint=[
            [
                LintFinding("error", "zones/mansion/rooms.star", 1, "bad"),
                LintFinding("warning", "zones/mansion/rooms.star", 2, "shady"),
            ]
        ],
    )
    ctx = _ctx(tmp_path, engine)
    check = stage_and_check(ctx, _proposal(), None)
    assert [f.message for f in check.lint_errors] == ["bad"]
    assert [f.message for f in check.lint_warnings] == ["shady"]


def test_allowed_values_and_param_allowed(api: ApiModel, world_min: WorldDescription) -> None:
    assert allowed_values(api, world_min, "room", "size") == ["cramped", "normal", "large"]
    assert param_allowed(api, world_min, "room", "size") is True
    assert param_allowed(api, world_min, "room", "no_such_param") is False
    assert param_allowed(api, world_min, "no_such_decl", "size") is False
    no_track = world_min.model_copy(
        update={"features": [f for f in world_min.features if f != "track"]}
    )
    assert param_allowed(api, no_track, "room", "size") is False
    assert allowed_values(api, no_track, "room", "size") == ["cramped", "normal", "large"]


def test_new_id_normalizes_and_counts_clashes() -> None:
    assert new_id(set(), "Wine Cellar") == "wine_cellar"
    assert new_id({"wine_cellar"}, "wine_cellar") == "wine_cellar_2"
    assert new_id({"wine_cellar", "wine_cellar_2"}, "Wine Cellar") == "wine_cellar_3"
    assert new_id(set(), "2fast") == "id_2fast"
    assert new_id(set(), "--") == "decl"
