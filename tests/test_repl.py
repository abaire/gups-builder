"""The interactive loop: turns, the review flow, write/undo/reload, hand-edit detection.

A scripted ``FakeLlm`` and a fake ``Engine`` serve the world; the test drives the
``Repl`` through ``handle`` lines with an injected ``read_line`` and inspects a
``Console(record=True)`` plus the files on disk and the session state.
"""

from __future__ import annotations

import hashlib
import json
import os
import select
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

import pytest
from rich.console import Console

from gups_builder.config import Config
from gups_builder.engine.gups import Gups, TestRun
from gups_builder.grid.model import RandomExit
from gups_builder.llm.fake import FakeLlm
from gups_builder.modes import testgen
from gups_builder.modes.area import AreaBrief, AreaGraph
from gups_builder.modes.area_checks import place_graph
from gups_builder.repl import Repl, _decl_random_exits, context_factory
from gups_builder.session import Session
from gups_builder.star.reader import read_package
from gups_builder.terminal import InputGuard, TurnStatus
from tests.conftest import load_api, read_fixture

if TYPE_CHECKING:
    from collections.abc import Callable

    from gups_builder.engine.api import ApiModel
    from gups_builder.engine.lint import LintFinding
    from gups_builder.engine.world import WorldDescription
    from gups_builder.llm.client import Message, Role
    from gups_builder.modes.common import Context

REPO = Path(__file__).resolve().parent.parent
AREA = Path(__file__).resolve().parent / "fixtures" / "llm" / "area_mansion"
REFINE = Path(__file__).resolve().parent / "fixtures" / "llm" / "area_refine"
MOB_CREATE = Path(__file__).resolve().parent / "fixtures" / "llm" / "mob_create"
SKILL_CREATE = Path(__file__).resolve().parent / "fixtures" / "llm" / "skill_create"
GOBLINS = Path(__file__).resolve().parent / "fixtures" / "goblins"
MANSION = Path(__file__).resolve().parent / "fixtures" / "mansion"

_GATE = (
    'room("gate",\n'
    '    name = "The Gate",\n'
    '    desc = "A gate.",\n'
    '    exits = [exit(DIR.west, to = ":hall")],\n'
    ")\n"
)


def _load(base: Path, name: str) -> str:
    return read_fixture(base, name)


def _api() -> ApiModel:
    return load_api()


class FakeEngine:
    def __init__(
        self, world: WorldDescription, api: ApiModel, lint: list[list[LintFinding]] | None = None
    ) -> None:
        self.world = world
        self._api = api
        self._lint = list(lint or [])
        self.world_calls = 0

    def describe_world(self, world: Path | None = None) -> WorldDescription:  # noqa: ARG002
        self.world_calls += 1
        return self.world

    def api(self) -> ApiModel:
        return self._api

    def lint(self, package: str, world: Path | None = None) -> list[LintFinding]:  # noqa: ARG002
        return self._lint.pop(0) if self._lint else []

    def test(self, package: str, world: Path | None = None) -> TestRun:  # noqa: ARG002
        return TestRun(ok=True, output="")


_ROOMS = [
    "hall",
    "kitchen",
    "study",
    "library",
    "parlour",
    "master_bedroom",
    "nursery",
    "bedroom",
    "cellar",
]


def _prose() -> list[str]:
    replies = [
        json.dumps(
            {
                "name": f"The {r.replace('_', ' ').title()}",
                "desc": f"Dust lies thick across the {r.replace('_', ' ')}.",
            }
        )
        for r in _ROOMS
    ]
    replies.append(_load(AREA, "prose_item.json"))
    return replies


def _ctx_factory(
    world_dir: Path, package: str, llm: FakeLlm, engine: FakeEngine
) -> Callable[[], Context]:
    return context_factory(world_dir, package, engine, llm, Config(max_repairs=1))


def _repl(
    tmp_path: Path,
    world: WorldDescription,
    llm: FakeLlm,
    package: str = "zones/mansion",
    engine: FakeEngine | None = None,
    mode: Literal["area", "mob", "skill"] = "area",
    view_log: Callable[[Path], None] | None = None,
) -> tuple[Repl, Session, Console, FakeEngine]:
    world_dir = tmp_path / "world"
    world_dir.mkdir(exist_ok=True)
    engine = engine or FakeEngine(world, _api())
    session = Session.create(world_dir, mode, package)
    console = Console(record=True)
    repl = Repl(
        session,
        _ctx_factory(world_dir, package, llm, engine),
        console,
        lambda _: "/quit",
        view_log=view_log,
    )
    return repl, session, console, engine


def test_mob_mode_in_a_missing_package_suggests_area(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": []})
    repl, session, console, _ = _repl(tmp_path, world_min, llm, mode="mob")
    repl.handle("a goblin")
    out = console.export_text()
    assert "zones/mansion does not exist; mob mode needs an existing package" in out
    assert "/mode area" in out
    assert session.state.pending is None
    assert len(llm.calls) == 0


def test_skill_mode_refuses_when_skills_is_off(tmp_path: Path, world_min: WorldDescription) -> None:
    world = world_min.model_copy(
        update={"features": [f for f in world_min.features if f != "skills"]}
    )
    llm = FakeLlm({"logic": []})
    repl, session, console, _ = _repl(tmp_path, world, llm, package="skills/arcane", mode="skill")
    repl.handle("a fire spell")
    assert "This world has the skills feature off." in console.export_text()
    assert session.state.pending is None
    assert len(llm.calls) == 0


def test_skill_mode_in_a_missing_package_suggests_area(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": []})
    repl, session, console, _ = _repl(
        tmp_path, world_min, llm, package="zones/mansion", mode="skill"
    )
    repl.handle("a fire spell")
    out = console.export_text()
    assert "zones/mansion does not exist; skill mode needs an existing package" in out
    assert "/mode area" in out
    assert session.state.pending is None
    assert len(llm.calls) == 0


def test_skill_mode_creation_becomes_the_pending_change(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    # A new skills package: no package.star, so the first write creates it.
    (tmp_path / "world" / "skills" / "arcane").mkdir(parents=True)
    llm = FakeLlm(
        {
            "logic": [_load(SKILL_CREATE, "brief.json"), _load(SKILL_CREATE, "plan.json")],
            "prose": [_load(SKILL_CREATE, "prose.json")],
        }
    )
    repl, session, console, _ = _repl(
        tmp_path, world_min, llm, package="skills/arcane", mode="skill"
    )
    repl.handle("a fire spell")
    assert session.state.pending is not None
    assert session.state.pending.summary == "+1 skill"
    assert "unrecognized.victim" in console.export_text()


def test_mob_mode_in_a_package_without_rooms_still_plans_mobs(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    # A package of mobs alone is valid: its mobs are spawned from other packages.
    (tmp_path / "world" / "zones" / "tutorial").mkdir(parents=True)
    llm = FakeLlm({"logic": [_load(MOB_CREATE, "brief_questions.json")]})
    repl, _, console, _ = _repl(tmp_path, world_min, llm, package="zones/tutorial", mode="mob")
    repl.handle("a goblin")
    assert len(llm.calls) == 1
    assert "Should the goblins attack players on sight?" in console.export_text()


def test_mob_mode_creation_turn_becomes_the_pending_change(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    world_dir = tmp_path / "world"
    (world_dir / "zones" / "tutorial").mkdir(parents=True)
    # Only rooms: without an items.star the goblin idol is a new item, so the
    # proposal creates it and its prose.
    (world_dir / "zones" / "tutorial" / "rooms.star").write_text(
        (GOBLINS / "rooms.star").read_text()
    )
    llm = FakeLlm(
        {
            "logic": [_load(MOB_CREATE, "brief.json"), _load(MOB_CREATE, "plan.json")],
            "prose": [
                _load(MOB_CREATE, "prose_goblin.json"),
                _load(MOB_CREATE, "prose_shaman.json"),
                _load(MOB_CREATE, "prose_idol.json"),
            ],
        }
    )
    repl, session, _console, _ = _repl(
        tmp_path, world_min, llm, package="zones/tutorial", mode="mob"
    )
    repl.handle("two goblins, one a shaman")
    assert session.state.pending is not None
    assert session.state.pending.summary == "+2 mobs, +1 item, 1 spawn"


def test_creation_turn_produces_pending_then_diff_then_write(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {"logic": [_load(AREA, "brief.json"), _load(AREA, "plan.json")], "prose": _prose()}
    )
    repl, session, console, _ = _repl(tmp_path, world_min, llm)
    assert repl.handle("a medium sized creepy mansion") is True
    assert session.state.pending is not None
    assert session.state.pending.summary.startswith("+9 rooms")
    repl.handle("/diff")
    out = console.export_text()
    assert "rooms.star" in out
    assert "+9 rooms" in out
    assert repl.handle("/write") is True
    pkg = tmp_path / "world" / "zones" / "mansion"
    assert (pkg / "rooms.star").is_file()
    assert (pkg / "items.star").is_file()
    assert (pkg / "package.star").is_file()
    assert (pkg / "builder_test.star").is_file()
    assert session.state.pending is None
    assert session.state.written_hashes
    assert session.state.builder_test_hash is not None


def test_a_brief_with_questions_prints_the_questions(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load(AREA, "brief_questions.json")]})
    repl, session, console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a mansion")
    out = console.export_text()
    assert "How many floors" in out
    assert session.state.pending is None


def test_hand_edit_between_proposal_and_write_is_refused(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {"logic": [_load(AREA, "brief.json"), _load(AREA, "plan.json")], "prose": _prose()}
    )
    repl, _session, console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a medium sized creepy mansion")
    pkg = tmp_path / "world" / "zones" / "mansion"
    pkg.mkdir(parents=True)
    (pkg / "rooms.star").write_text('room("hall", name = "Hand Hall", desc = "hand")\n')
    repl.handle("/write")
    out = console.export_text()
    assert "already exists" in out
    assert "The Kitchen" in (pkg / "rooms.star").read_text()


def test_undo_restores_the_previous_files(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {"logic": [_load(AREA, "brief.json"), _load(AREA, "plan.json")], "prose": _prose()}
    )
    repl, session, _console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a medium sized creepy mansion")
    repl.handle("/write")
    pkg = tmp_path / "world" / "zones" / "mansion"
    assert (pkg / "rooms.star").is_file()
    repl.handle("/undo")
    assert not (pkg / "rooms.star").exists()
    assert session.state.undo is None


def test_undo_after_a_hand_edit_is_refused(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {"logic": [_load(AREA, "brief.json"), _load(AREA, "plan.json")], "prose": _prose()}
    )
    repl, _session, console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a medium sized creepy mansion")
    repl.handle("/write")
    pkg = tmp_path / "world" / "zones" / "mansion"
    (pkg / "rooms.star").write_text((pkg / "rooms.star").read_text() + "# hand edit\n")
    repl.handle("/undo")
    out = console.export_text()
    assert "changed since the last write" in out
    assert (pkg / "rooms.star").is_file()


def test_mode_mob_prints_the_noted_request(tmp_path: Path, world_mansion: WorldDescription) -> None:
    # The package has rooms, so a turn goes to area_turn; "add a ghost" is out of scope.
    world_dir = tmp_path / "world"
    (world_dir / "zones" / "mansion").mkdir(parents=True)
    for name in ("rooms.star", "items.star"):
        (world_dir / "zones" / "mansion" / name).write_text((MANSION / name).read_text())
    llm = FakeLlm({"logic": [_load(REFINE, "turn_add_ghost.json")]})
    repl, session, console, _ = _repl(tmp_path, world_mansion, llm)
    repl.handle("add a ghost")
    assert session.state.notes == [("mob", "add a ghost")]
    repl.handle("/mode mob")
    out = console.export_text()
    assert "add a ghost" in out


def test_a_broken_package_reports_errors_and_makes_no_model_call(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    # describe-world lists zones/broken with ok: false; no files, no rooms.
    llm = FakeLlm({"logic": []})
    repl, session, console, _engine = _repl(tmp_path, world_min, llm, package="zones/broken")
    repl.handle("a room")
    out = console.export_text()
    assert "syntax error" in out
    assert "Fix these first" in out
    assert session.state.pending is None
    assert len(llm.calls) == 0


def test_reload_after_a_fix_resumes(tmp_path: Path, world_min: WorldDescription) -> None:
    # zones/broken is listed ok: false; the fix is a world that lists it ok. The engine's
    # answer to describe-world is what the loop rebuilds from on /reload.
    fixed = world_min.model_copy(deep=True)
    for p in fixed.packages:
        if p.path == "zones/broken":
            p.ok = True
    fixed.errors = [e for e in fixed.errors if e.package != "zones/broken"]
    llm = FakeLlm(
        {"logic": [_load(AREA, "brief.json"), _load(AREA, "plan.json")], "prose": _prose()}
    )
    engine = FakeEngine(world_min, _api())
    repl, session, console, engine = _repl(
        tmp_path, world_min, llm, package="zones/broken", engine=engine
    )
    repl.handle("a room")
    assert "Fix these first" in console.export_text()
    assert session.state.pending is None
    assert len(llm.calls) == 0
    engine.world = fixed  # the user's fix, as the engine now reports it
    repl.handle("/reload")
    repl.handle("a room")
    assert session.state.pending is not None
    assert len(llm.calls) > 0


def test_link_ops_applied_only_after_y(tmp_path: Path, world_min: WorldDescription) -> None:
    world_dir = tmp_path / "world"
    (world_dir / "zones" / "tutorial").mkdir(parents=True)
    (world_dir / "zones" / "tutorial" / "rooms.star").write_text(_GATE)
    brief = json.loads(_load(AREA, "brief.json"))
    brief["link_to"] = "//zones/tutorial:gate"
    plan = json.loads(_load(AREA, "plan.json"))
    plan["link"] = {"room": "hall", "exit": "south", "external_label": "//zones/tutorial:gate"}
    llm = FakeLlm({"logic": [json.dumps(brief), json.dumps(plan)], "prose": _prose()})
    repl, session, _console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a mansion south of the gate")
    assert session.state.pending is not None
    assert session.state.pending.link_ops
    repl.read_line = lambda _: "y"
    repl.handle("/write")
    gate = (tmp_path / "world" / "zones" / "tutorial" / "rooms.star").read_text()
    assert "//zones/mansion:hall" in gate


def test_link_ops_skipped_on_n(tmp_path: Path, world_min: WorldDescription) -> None:
    world_dir = tmp_path / "world"
    (world_dir / "zones" / "tutorial").mkdir(parents=True)
    (world_dir / "zones" / "tutorial" / "rooms.star").write_text(_GATE)
    brief = json.loads(_load(AREA, "brief.json"))
    brief["link_to"] = "//zones/tutorial:gate"
    plan = json.loads(_load(AREA, "plan.json"))
    plan["link"] = {"room": "hall", "exit": "south", "external_label": "//zones/tutorial:gate"}
    llm = FakeLlm({"logic": [json.dumps(brief), json.dumps(plan)], "prose": _prose()})
    repl, _session, _console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a mansion south of the gate")
    repl.read_line = lambda _: "n"
    repl.handle("/write")
    gate = (tmp_path / "world" / "zones" / "tutorial" / "rooms.star").read_text()
    assert "mansion" not in gate


def test_hand_edited_builder_test_makes_new_file(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(AREA, "brief.json"),
                _load(AREA, "plan.json"),
                _load(REFINE, "turn_rewrite_library.json"),
            ],
            "prose": [*_prose(), _load(REFINE, "prose_library.json")],
        }
    )
    repl, session, _console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a medium sized creepy mansion")
    repl.handle("/write")
    pkg = tmp_path / "world" / "zones" / "mansion"
    (pkg / "builder_test.star").write_text("# hand edit\n")
    repl.handle("make the library creepier")
    assert session.state.pending is not None
    repl.handle("/write")
    assert (pkg / "builder_test.star.new").is_file()
    assert (pkg / "builder_test.star").read_text() == "# hand edit\n"


def test_map_shows_the_rooms(tmp_path: Path, world_mansion: WorldDescription) -> None:
    world_dir = tmp_path / "world"
    (world_dir / "zones" / "mansion").mkdir(parents=True)
    for name in ("rooms.star", "items.star"):
        (world_dir / "zones" / "mansion" / name).write_text((MANSION / name).read_text())
    llm = FakeLlm({"logic": []})
    repl, _session, console, _ = _repl(tmp_path, world_mansion, llm)
    repl.handle("/map")
    out = console.export_text()
    assert "hall" in out
    assert "kitchen" in out


def test_show_prints_the_declaration(tmp_path: Path, world_mansion: WorldDescription) -> None:
    world_dir = tmp_path / "world"
    (world_dir / "zones" / "mansion").mkdir(parents=True)
    for name in ("rooms.star", "items.star"):
        (world_dir / "zones" / "mansion" / name).write_text((MANSION / name).read_text())
    llm = FakeLlm({"logic": []})
    repl, _session, console, _ = _repl(tmp_path, world_mansion, llm)
    repl.handle("/show hall")
    out = console.export_text()
    assert 'room("hall"' in out
    assert "The Hall" in out


def test_discard_clears_the_pending(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {"logic": [_load(AREA, "brief.json"), _load(AREA, "plan.json")], "prose": _prose()}
    )
    repl, session, console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a mansion")
    assert session.state.pending is not None
    repl.handle("/discard")
    assert session.state.pending is None
    assert "discarded" in console.export_text()


_REFINE_GRID: dict[str, tuple[int, int, int]] = {
    "hall": (0, 0, 0),
    "kitchen": (1, 0, 0),
    "study": (0, 1, 0),
    "library": (1, 1, 0),
    "parlour": (2, 0, 0),
    "master_bedroom": (0, 0, 1),
    "nursery": (1, 1, 1),
    "bedroom": (2, 0, 1),
    "cellar": (2, 0, -1),
    "vault": (-1, 0, 0),
}


def _mansion_files(tmp_path: Path) -> Path:
    pkg = tmp_path / "world" / "zones" / "mansion"
    pkg.mkdir(parents=True)
    for name in ("rooms.star", "items.star", "package.star"):
        (pkg / name).write_text((MANSION / name).read_text())
    return pkg


def _creation_llm(*extra_logic: str, extra_prose: tuple[str, ...] = ()) -> FakeLlm:
    return FakeLlm(
        {
            "logic": [_load(AREA, "brief.json"), _load(AREA, "plan.json"), *extra_logic],
            "prose": _prose() + list(extra_prose),
        }
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_a_file_that_does_not_parse_blocks_model_turns(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    pkg = _mansion_files(tmp_path)
    good = (pkg / "rooms.star").read_text()
    (pkg / "rooms.star").write_text(good + 'room("broken",\n')
    llm = FakeLlm({"logic": [_load(REFINE, "turn_add_ghost.json")]})
    repl, session, console, _ = _repl(tmp_path, world_mansion, llm)
    assert repl.handle("add a ghost") is True
    assert repl.handle("/map") is True
    assert repl.handle("/show hall") is True
    out = console.export_text()
    assert "rooms.star:" in out
    assert "Fix these first" in out
    assert len(llm.calls) == 0
    (pkg / "rooms.star").write_text(good)
    repl.handle("/reload")
    repl.handle("add a ghost")
    assert session.state.notes == [("mob", "add a ghost")]


def test_ctrl_c_during_a_model_call_returns_to_the_prompt(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    class Interrupted(FakeLlm):
        def complete(
            self,
            role: Role,  # noqa: ARG002
            messages: list[Message],  # noqa: ARG002
            json_schema: dict[str, Any] | None,  # noqa: ARG002
        ) -> str:
            raise KeyboardInterrupt

    repl, session, console, _ = _repl(tmp_path, world_min, Interrupted({}))
    assert repl.handle("a mansion") is True
    assert "cancelled" in console.export_text()
    assert session.state.pending is None


def test_ctrl_c_at_the_prompt_does_not_quit(tmp_path: Path, world_min: WorldDescription) -> None:
    repl, _, _, _ = _repl(tmp_path, world_min, FakeLlm({}))
    lines: list[str] = []

    def read_line(prompt: str) -> str:
        lines.append(prompt)
        if len(lines) == 1:
            raise KeyboardInterrupt
        if len(lines) == 2:
            raise EOFError
        msg = "Ctrl-D should have quit"
        raise AssertionError(msg)

    repl.read_line = read_line
    assert repl.run() == 0
    assert len(lines) == 2


def test_a_failed_model_call_is_reported(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm({"logic": ["not json", "still not json"]})
    repl, session, console, _ = _repl(tmp_path, world_min, llm)
    assert repl.handle("a mansion") is True
    assert "model failed" in console.export_text()
    assert session.state.pending is None


def test_the_current_message_reaches_the_model_once(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    _mansion_files(tmp_path)
    llm = FakeLlm({"logic": [_load(REFINE, "turn_add_ghost.json")]})
    repl, _, _, _ = _repl(tmp_path, world_mansion, llm)
    repl.handle("add a ghost")
    prompt = "\n".join(m.content for m in llm.calls[0][1])
    assert prompt.count("add a ghost") == 1


def test_creation_coordinates_are_kept_after_write(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = _creation_llm()
    repl, session, _, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a medium sized creepy mansion")
    assert session.state.grid == {}
    repl.handle("/write")
    plan = place_graph(AreaGraph.model_validate_json(_load(AREA, "plan.json")))
    assert session.state.grid == {r.id: (r.at.x, r.at.y, r.at.z) for r in plan.rooms}


def test_a_discarded_edit_leaves_the_coordinates_alone(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    _mansion_files(tmp_path)
    llm = FakeLlm(
        {
            "logic": [_load(REFINE, "turn_add_cellar.json")],
            "prose": [_load(REFINE, "prose_wine_cellar.json")],
        }
    )
    repl, session, _, _ = _repl(tmp_path, world_mansion, llm)
    # The refine fixtures' mansion keeps its cellar under the parlour, leaving the
    # kitchen's z-1 free for the wine cellar.
    known = {r: (x, y, z) for r, (x, y, z) in _REFINE_GRID.items()}
    session.state.grid = dict(known)
    repl.handle("add a wine cellar under the kitchen")
    assert session.state.pending is not None
    assert session.state.grid == known
    repl.handle("/discard")
    assert session.state.grid == known


def test_undo_restores_a_changed_file_and_the_builder_test_hash(
    tmp_path: Path, world_min: WorldDescription, world_mansion: WorldDescription
) -> None:
    # Locking the study takes its exit out of the generated test, so the second write
    # changes builder_test.star and the undo must restore its hash too.
    llm = _creation_llm(
        _load(REFINE, "turn_lock_study.json"),
        extra_prose=(_load(REFINE, "prose_study_key.json"),),
    )
    repl, session, _console, engine = _repl(tmp_path, world_min, llm)
    repl.handle("a medium sized creepy mansion")
    # After the write the engine knows the rooms; the REPL asks it right after writing.
    # world_mansion is the refine fixtures' mansion, whose cellar sits under the
    # parlour, so the session's grid follows it.
    engine.world = world_mansion
    repl.handle("/write")
    session.state.grid = dict(_REFINE_GRID)
    pkg = tmp_path / "world" / "zones" / "mansion"
    first = (pkg / "rooms.star").read_text()
    first_test = (pkg / "builder_test.star").read_text()
    repl.handle("lock the study")
    repl.handle("/write")
    assert (pkg / "builder_test.star").read_text() != first_test
    repl.handle("/undo")
    assert (pkg / "rooms.star").read_text() == first
    assert (pkg / "builder_test.star").read_text() == first_test
    assert session.state.builder_test_hash == _sha(pkg / "builder_test.star")
    assert "study_key" not in (pkg / "items.star").read_text()


def test_undo_also_removes_the_return_exit(tmp_path: Path, world_min: WorldDescription) -> None:
    world_dir = tmp_path / "world"
    (world_dir / "zones" / "tutorial").mkdir(parents=True)
    (world_dir / "zones" / "tutorial" / "rooms.star").write_text(_GATE)
    brief = json.loads(_load(AREA, "brief.json"))
    brief["link_to"] = "//zones/tutorial:gate"
    plan = json.loads(_load(AREA, "plan.json"))
    plan["link"] = {"room": "hall", "exit": "south", "external_label": "//zones/tutorial:gate"}
    llm = FakeLlm({"logic": [json.dumps(brief), json.dumps(plan)], "prose": _prose()})
    repl, _, _, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a mansion south of the gate")
    repl.read_line = lambda _: "y"
    repl.handle("/write")
    gate = world_dir / "zones" / "tutorial" / "rooms.star"
    assert "//zones/mansion:hall" in gate.read_text()
    repl.handle("/undo")
    assert gate.read_text() == _GATE


def test_an_untracked_builder_test_is_not_overwritten(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    pkg = _mansion_files(tmp_path)
    (pkg / "builder_test.star").write_text("# mine\n")
    llm = FakeLlm(
        {
            "logic": [_load(REFINE, "turn_rewrite_library.json")],
            "prose": [_load(REFINE, "prose_library.json")],
        }
    )
    repl, _, console, _ = _repl(tmp_path, world_mansion, llm)
    repl.handle("make the library creepier")
    repl.handle("/write")
    assert (pkg / "builder_test.star").read_text() == "# mine\n"
    assert (pkg / "builder_test.star.new").is_file()
    assert "builder_test.star.new" in console.export_text()


def test_an_untracked_builder_test_matching_the_generated_one_is_replaced(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    pkg = _mansion_files(tmp_path)
    view = read_package(pkg, "zones/mansion")
    (pkg / "builder_test.star").write_text(
        testgen.builder_test("zones/mansion", world_mansion, view)
    )
    llm = FakeLlm(
        {
            "logic": [_load(REFINE, "turn_rewrite_library.json")],
            "prose": [_load(REFINE, "prose_library.json")],
        }
    )
    repl, session, _, _ = _repl(tmp_path, world_mansion, llm)
    repl.handle("make the library creepier")
    repl.handle("/write")
    assert not (pkg / "builder_test.star.new").exists()
    assert session.state.builder_test_hash == _sha(pkg / "builder_test.star")


def test_show_after_write_sees_the_written_files(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    repl, _, console, _ = _repl(tmp_path, world_min, _creation_llm())
    repl.handle("a medium sized creepy mansion")
    repl.handle("/write")
    repl.handle("/show kitchen")
    assert 'room("kitchen"' in console.export_text()


def test_write_keeps_a_hand_edit_made_after_the_proposal(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    pkg = _mansion_files(tmp_path)
    llm = FakeLlm(
        {
            "logic": [_load(REFINE, "turn_rewrite_library.json")],
            "prose": [_load(REFINE, "prose_library.json")],
        }
    )
    repl, _, _, _ = _repl(tmp_path, world_mansion, llm)
    repl.handle("make the library creepier")
    rooms = (pkg / "rooms.star").read_text()
    (pkg / "rooms.star").write_text("# a hand-written note\n" + rooms)
    repl.handle("/write")
    after = (pkg / "rooms.star").read_text()
    assert after.startswith("# a hand-written note\n")
    library_desc = json.loads(_load(REFINE, "prose_library.json"))["desc"]
    assert library_desc in after


def test_map_with_a_bad_level_prints_usage(tmp_path: Path, world_mansion: WorldDescription) -> None:
    _mansion_files(tmp_path)
    repl, _, console, _ = _repl(tmp_path, world_mansion, FakeLlm({}))
    assert repl.handle("/map up") is True
    assert "usage: /map [z]" in console.export_text()


@pytest.mark.engine
def test_a_scripted_session_passes_the_real_engine(gups_bin: Path, template_world: Path) -> None:
    gups = Gups(gups_bin, template_world)
    llm = _creation_llm(
        _load(REFINE, "turn_lock_study.json"),
        _load(REFINE, "turn_rewrite_library.json"),
        extra_prose=(
            _load(REFINE, "prose_study_key.json"),
            _load(REFINE, "prose_library.json"),
        ),
    )
    package = "zones/mansion"
    session = Session.create(template_world, "area", package)
    console = Console(record=True)
    factory = context_factory(template_world, package, gups, llm, Config(max_repairs=3))
    repl = Repl(session, factory, console, lambda _: "/quit")
    for line in (
        "a medium sized creepy mansion",
        "lock the study",
        "make the library creepier",
    ):
        repl.handle(line)
        assert session.state.pending is not None, console.export_text()
        repl.handle("/write")
        assert session.state.pending is None, console.export_text()
    out = console.export_text()
    pkg = template_world / package
    rooms = (pkg / "rooms.star").read_text()
    assert "study_key" in (pkg / "items.star").read_text()
    assert "DOOR.locked" in rooms
    assert json.loads(_load(REFINE, "prose_library.json"))["desc"] in rooms
    assert "BUILDER BUG" not in out
    assert "NOT VALID" not in out
    assert session.state.grid["cellar"] == (1, 0, -1)
    findings = gups.lint(package)
    assert not [f for f in findings if f.severity == "error"]
    assert gups.test(package).ok, out


def test_commands_on_unchanged_files_do_not_rebuild_the_context(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    _mansion_files(tmp_path)
    llm = FakeLlm(
        {
            "logic": [_load(REFINE, "turn_rewrite_library.json")],
            "prose": [_load(REFINE, "prose_library.json")],
        }
    )
    repl, _, _, engine = _repl(tmp_path, world_mansion, llm)
    repl.handle("make the library creepier")
    calls = engine.world_calls
    for line in ("/map", "/show hall", "/diff", "/notes"):
        repl.handle(line)
    assert engine.world_calls == calls


def test_a_write_with_nothing_to_write_keeps_the_last_undo(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = _creation_llm(
        _load(REFINE, "turn_rewrite_library.json"),
        extra_prose=(_load(REFINE, "prose_library.json"),),
    )
    repl, session, console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a medium sized creepy mansion")
    repl.handle("/write")
    undo = session.state.undo
    repl.handle("make the library creepier")
    pkg = tmp_path / "world" / "zones" / "mansion"
    rooms = (pkg / "rooms.star").read_text()
    # The user makes the same change by hand, so the pending rewrite changes nothing.
    old_desc = "Dust lies thick across the library."
    new_desc = json.loads(_load(REFINE, "prose_library.json"))["desc"]
    (pkg / "rooms.star").write_text(rooms.replace(old_desc, new_desc))
    repl.handle("/write")
    assert "nothing to write" in console.export_text()
    assert session.state.undo == undo


def test_plan_mode_conversation_then_build_creates_the_area(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    brief1 = json.loads(_load(AREA, "brief.json"))
    brief1["questions"] = ["Should the study connect to the library?"]
    turn1 = {"reply": "I drafted a 9-room manor.", "brief": brief1}
    brief2 = json.loads(_load(AREA, "brief.json"))
    turn2 = {"reply": "Great, the study and library are both in the brief.", "brief": brief2}
    llm = FakeLlm(
        {
            "logic": [json.dumps(turn1), json.dumps(turn2), _load(AREA, "plan.json")],
            "prose": _prose(),
        }
    )
    repl, session, console, _ = _repl(tmp_path, world_min, llm)

    repl.handle("/plan")
    assert session.state.planning is True
    assert "planning mode is on" in console.export_text()

    repl.handle("a medium sized creepy mansion")
    assert session.state.pending is None
    assert session.state.plan_brief is not None
    assert session.state.plan_brief.title == "The Manor House"
    out = console.export_text()
    assert "Plan: The Manor House" in out
    assert "I drafted a 9-room manor." in out
    assert "Should the study connect to the library?" in out

    repl.handle("yes, connect them")
    assert session.state.pending is None
    assert len(llm.calls) == 2

    repl.handle("/build")
    assert session.state.planning is False
    assert session.state.plan_brief is None
    assert session.state.pending is not None
    assert session.state.pending.summary.startswith("+9 rooms")


def test_plan_with_inline_message_and_plan_off(tmp_path: Path, world_min: WorldDescription) -> None:
    brief = json.loads(_load(AREA, "brief.json"))
    turn = {"reply": "Drafted the manor.", "brief": brief}
    llm = FakeLlm({"logic": [json.dumps(turn)]})
    repl, session, console, _ = _repl(tmp_path, world_min, llm)

    repl.handle("/plan a creepy mansion")
    assert session.state.planning is True
    assert session.state.plan_brief is not None
    assert "Drafted the manor." in console.export_text()

    # /plan with no args when a brief exists prints the current brief without a model call.
    repl.handle("/plan")
    assert len(llm.calls) == 1
    assert "Plan: The Manor House" in console.export_text()

    repl.handle("/plan off")
    assert session.state.planning is False
    assert "left planning mode" in console.export_text()


def test_plan_and_build_on_existing_area_in_repl(
    tmp_path: Path, world_mansion: WorldDescription
) -> None:
    _mansion_files(tmp_path)
    brief = json.loads(_load(AREA, "brief.json"))
    brief["title"] = "Wine Cellar Expansion"
    turn = {"reply": "Ready to add the wine cellar under the kitchen.", "brief": brief}
    llm = FakeLlm(
        {
            "logic": [json.dumps(turn), _load(REFINE, "turn_add_cellar.json")],
            "prose": [_load(REFINE, "prose_wine_cellar.json")],
        }
    )
    repl, session, console, _ = _repl(tmp_path, world_mansion, llm)
    session.state.grid = dict(_REFINE_GRID)

    repl.handle("/plan add a wine cellar under the kitchen")
    assert session.state.planning is True
    assert session.state.pending is None

    repl.handle("/build")
    assert session.state.planning is False
    assert session.state.plan_brief is None
    assert session.state.pending is not None
    assert "+1 room" in console.export_text()


def test_plan_and_build_refusals(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm({})
    repl, session, console, _ = _repl(tmp_path, world_min, llm)

    repl.handle("/build")
    assert "nothing to build; use /plan to plan an area first" in console.export_text()

    repl.handle("/plan")
    assert session.state.planning is True
    repl.handle("/mode mob")
    assert session.state.planning is False
    repl.handle("/plan")
    assert "/plan is only available in area mode" in console.export_text()
    repl.handle("/build")
    assert "/build is only available in area mode" in console.export_text()


def test_plan_mode_prompt_indicator(tmp_path: Path, world_min: WorldDescription) -> None:
    session = Session.create(tmp_path, "area", "zones/test", "test")
    session.state.planning = True
    prompts: list[str] = []

    def read_line(prompt: str) -> str:
        prompts.append(prompt)
        return "/quit"

    factory = context_factory(
        tmp_path,
        "zones/test",
        FakeEngine(world_min, _api()),
        FakeLlm({}),
        Config(),
    )
    Repl(session, factory, Console(), read_line).run()
    assert "(plan) > " in prompts

    session.state.planning = False
    prompts.clear()
    Repl(session, factory, Console(), read_line).run()
    assert "> " in prompts
    assert "(plan) > " not in prompts


def test_status_updates_reach_the_live_status_line_and_input_guard(
    tmp_path: Path, world_mansion: WorldDescription, monkeypatch: pytest.MonkeyPatch
) -> None:
    _mansion_files(tmp_path)
    status_messages: list[str] = []
    monkeypatch.setattr(TurnStatus, "update", lambda _self, msg: status_messages.append(msg))

    brief = json.loads(_load(AREA, "brief.json"))
    plan = json.loads(_load(AREA, "plan.json"))
    prose_room = json.loads(_load(AREA, "prose_room.json"))
    prose_item = json.loads(_load(AREA, "prose_item.json"))

    prose_replies = [json.dumps(prose_room)] * 10 + [json.dumps(prose_item)] * 10
    llm = FakeLlm(
        {
            "logic": [json.dumps(brief), json.dumps(plan)],
            "prose": prose_replies,
        }
    )

    engine = FakeEngine(world_mansion, _api(), lint=[[], []])
    factory = context_factory(tmp_path, "zones/mansion", engine, llm, Config())
    session = Session.create(tmp_path, "area", "zones/mansion", "mansion")
    repl = Repl(session, factory, Console(), lambda _: "/quit")
    assert any("Loading world state" in msg for msg in status_messages)

    status_messages.clear()
    repl.handle("Build a mansion")

    assert any("drafting area brief" in msg for msg in status_messages)
    assert any("Validating in staged world (gups lint)..." in msg for msg in status_messages)
    assert any("writing room prose" in msg for msg in status_messages)
    assert all(msg != "idle" for msg in status_messages)

    r_fd, w_fd = os.pipe()
    try:
        console = Console(record=True)
        with InputGuard(console=console, fd=r_fd):
            os.write(w_fd, b"some typing\n")
            assert _wait_for(lambda: not select.select([r_fd], [], [], 0)[0])
            assert _wait_for(lambda: "Agent is working" in console.export_text(clear=False))
    finally:
        os.close(r_fd)
        os.close(w_fd)


def _wait_for(condition: Callable[[], bool], timeout: float = 2.0) -> bool:
    """Polls a condition a background thread is expected to make true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def test_turn_status_and_input_guard_advanced(tmp_path: Path) -> None:
    r_fd, w_fd = os.pipe()
    try:
        console = Console(record=True)
        with TurnStatus(console=console, badge="[area] test", fd=r_fd) as ts:
            ts.update("Waiting for logic model (qwen2.5:14b) — planning...")
            assert ts.format_line(elapsed=2) == (
                "[area] test — Waiting for logic model (qwen2.5:14b) — planning... [0:02]"
            )
            assert ts.format_line(elapsed=6) == (
                "[area] test — Waiting for logic model (qwen2.5:14b) — planning... "
                "[0:06] (waiting for model...)"
            )
            ts.update_usage("logic ctx ~3.2k/16.4k (20%)")
            assert ts.format_line(elapsed=2) == (
                "[area] test · logic ctx ~3.2k/16.4k (20%) — Waiting for logic model "
                "(qwen2.5:14b) — planning... [0:02]"
            )
            os.write(w_fd, b"user keystrokes\n")
            assert _wait_for(lambda: "Agent is working" in console.export_text(clear=False))
    finally:
        os.close(r_fd)
        os.close(w_fd)


def test_a_failed_model_call_prints_every_attempt_and_where_the_log_is(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": ["not json", '{"title": 1}']})
    repl, session, console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a medium sized creepy mansion")
    out = console.export_text()
    assert "error: model failed to produce a valid reply for drafting area brief" in out
    assert "attempt 1 of 2:" in out
    assert "attempt 2 of 2:" in out
    log = session.world / ".builder" / "logs" / f"{session.state.name}.jsonl"
    assert f"The full exchange is in {log}; /log to browse it." in out.replace("\n", "")


def test_a_repeated_reply_is_reported_after_the_attempts(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": ['{"title": 1}', '{"title": 1}']})
    repl, _session, console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a medium sized creepy mansion")
    out = console.export_text()
    assert "attempt 2 of 2:" in out
    assert (
        "attempt 2 sent back attempt 1's reply unchanged, so the remaining repairs were skipped"
        in " ".join(out.split())
    )


def test_log_opens_the_viewer_on_the_sessions_call_log(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    opened: list[Path] = []
    repl, session, console, _ = _repl(tmp_path, world_min, FakeLlm({}), view_log=opened.append)
    repl.handle("/log")
    assert opened == []
    assert "no model calls logged yet" in console.export_text()
    log = session.world / ".builder" / "logs" / f"{session.state.name}.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text("{}\n")
    repl.handle("/log")
    assert opened == [log]


def test_help_lists_log(tmp_path: Path, world_min: WorldDescription) -> None:
    repl, _session, console, _ = _repl(tmp_path, world_min, FakeLlm({}))
    repl.handle("/help")
    assert "/log  browse this session's model calls" in console.export_text()


def test_build_refuses_a_saved_brief_whose_link_is_not_a_room(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({})
    repl, session, console, _ = _repl(tmp_path, world_min, llm)
    brief = AreaBrief.model_validate_json(_load(AREA, "brief.json"))
    session.state.planning = True
    session.state.plan_brief = brief.model_copy(update={"link_to": "//zones/nowhere:void"})
    repl.handle("/build")
    out = console.export_text()
    assert "link_to '//zones/nowhere:void' is not a room of another package" in out
    assert "fix the link with /plan before /build" in out
    assert llm.calls == []
    assert session.state.planning is True
    assert session.state.plan_brief is not None


def test_a_random_exit_in_the_files_is_read_for_the_map(tmp_path: Path) -> None:
    pkg = tmp_path / "zones" / "funfair"
    pkg.mkdir(parents=True)
    (pkg / "rooms.star").write_text(
        'room("hall", name = "Hall", exits = [\n'
        '    exit(DIR.east, to = ":maze"),\n'
        '    exit("swirl", to = [":maze", "//zones/funfair:tent"]),\n'
        '    exit("away", to = [":maze", "//zones/other:room"]),\n'
        "])\n"
        'room("maze", name = "Maze", exits = [exit(DIR.west, to = ":hall")])\n'
        'room("tent", name = "Tent")\n'
    )
    view = read_package(pkg, "zones/funfair")
    hall = next(d for d in view.decls if d.id == "hall")
    found = _decl_random_exits(hall, "zones/funfair", {"hall", "maze", "tent"})
    # Only the exit whose rooms are all in the package is drawn; another package's is not.
    assert found == [RandomExit(room="hall", exit="swirl", to=["maze", "tent"])]


def _brief_asking(*questions: str) -> str:
    brief = json.loads(_load(AREA, "brief.json"))
    brief["questions"] = list(questions)
    return json.dumps(brief)


def _user_prompt(llm: FakeLlm, call: int) -> str:
    return [m.content for m in llm.calls[call][1] if m.role == "user"][-1]


def test_an_answer_reaches_the_next_brief_with_the_request_and_the_question(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _brief_asking("How many floors should the manor have?"),
                _load(AREA, "brief.json"),
                _load(AREA, "plan.json"),
            ],
            "prose": _prose(),
        }
    )
    repl, session, _console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a creepy mansion with a hedge maze")
    repl.handle("three floors")
    first, second = _user_prompt(llm, 0), _user_prompt(llm, 1)
    assert second.endswith(
        "User: a creepy mansion with a hedge maze\n"
        "You: How many floors should the manor have?\n"
        "User: three floors"
    )
    assert "never ask about it again" in second
    # The second prompt only adds to the end of the first, so a prefix cache keeps it all.
    assert second.startswith(first)
    assert session.state.pending is not None
    assert session.state.brief_draft == []


def test_a_brief_shows_only_its_first_question(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm({"logic": [_brief_asking("How many floors?", "Which key opens the barn?")]})
    repl, session, console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a mansion")
    out = console.export_text()
    assert "How many floors?" in out
    assert "Which key" not in out
    assert session.state.brief_draft == [("user", "a mansion"), ("builder", "How many floors?")]


def test_build_after_a_question_builds_the_draft(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [_brief_asking("How many floors?"), _load(AREA, "plan.json")],
            "prose": _prose(),
        }
    )
    repl, session, console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a mansion")
    assert "/build to build from the draft" in console.export_text()
    repl.handle("/build")
    assert session.state.pending is not None
    assert session.state.plan_brief is None
    assert session.state.brief_draft == []


def test_changing_mode_drops_a_half_settled_brief(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_brief_asking("How many floors?")]})
    repl, session, _console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a mansion")
    repl.handle("/mode mob")
    assert session.state.brief_draft == []


def test_the_status_line_carries_the_last_calls_context_usage(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_brief_asking("How many floors?")]})
    repl, _session, _console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("a mansion")
    assert repl._usage.startswith("logic ctx ~")
    assert "/32.8k (" in repl._usage


def test_a_planning_turn_hears_answers_older_than_six_messages(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    turn = {"brief": json.loads(_load(AREA, "brief.json")), "reply": "Noted."}
    llm = FakeLlm({"logic": [json.dumps(turn)] * 5})
    repl, _session, _console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("/plan")
    for answer in ("the rift is a dead end", "two", "three", "four", "five"):
        repl.handle(answer)
    last = _user_prompt(llm, 4)
    assert "user: the rift is a dead end" in last
    assert "left out for space" not in last
    # Rules, rooms, links and the history so far come before the draft brief and the
    # message, so each turn's prompt shares everything up to its new history lines.
    before = _user_prompt(llm, 3)
    assert last.startswith(before[: before.index("\n\nCurrent draft brief:")])
    assert last.index("could be linked") < last.index("Conversation so far:")
    assert last.index("Conversation so far:") < last.index("Current draft brief:")


def test_a_fold_moves_the_planning_cursor_and_the_next_turn_does_not_fold_again(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    brief = json.loads(_load(AREA, "brief.json"))
    turn = json.dumps({"brief": {**brief, "decisions": ["no link"]}, "reply": "Noted."})
    fold = json.dumps({"decisions": ["no link"]})
    llm = FakeLlm({"logic": [fold, turn, turn]})
    repl, session, console, _ = _repl(tmp_path, world_min, llm)
    session.state.history = [
        ("user" if i % 2 == 0 else "builder", f"old {i:02} " + "x" * 800) for i in range(30)
    ]
    repl.handle("/plan")
    ctx = repl._current()
    assert ctx is not None
    ctx.config = Config(
        logic=Config().logic.model_copy(update={"context_tokens": 6000, "max_tokens": 512}),
        max_repairs=1,
    )
    repl.handle("a mansion")
    assert session.state.plan_folded > 0
    assert "earlier messages into the decisions" in console.export_text()
    repl.handle("and a barn")
    assert len(llm.calls) == 3  # one fold, then two planning turns
    last = _user_prompt(llm, 2)
    assert "old 00" not in last
    assert "no link" in last


def test_build_of_a_planned_new_area_hands_the_decisions_to_the_graph(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    brief = json.loads(_load(AREA, "brief.json"))
    decided = "the rift chamber is a dead end"
    turn = json.dumps({"brief": {**brief, "decisions": [decided]}, "reply": "Noted."})
    llm = FakeLlm({"logic": [turn, _load(AREA, "plan.json")], "prose": _prose()})
    repl, session, _console, _ = _repl(tmp_path, world_min, llm)
    repl.handle("/plan")
    repl.handle("a mansion whose rift chamber is a dead end")
    repl.handle("/build")
    assert session.state.pending is not None
    graph = _user_prompt(llm, 1)
    assert f"the plan must keep every one:\n- {decided}" in graph
    # A clean context: none of the planning conversation goes to the build.
    assert "whose rift chamber" not in graph
