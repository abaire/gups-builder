"""The end-to-end check: one conversation across all three modes, against the engine.

A scripted ``FakeLlm`` drives a ``Repl`` built on a real ``Gups`` engine and a copy
of the template world, through ``handle`` lines only: create a mansion in area
mode, hand-edit a room, refine the area, add a mob, add a skill the mob teaches.
After every write the real engine lints the package clean; after the last write
``gups test`` passes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from rich.console import Console

from gups_builder.config import Config
from gups_builder.engine.gups import Gups
from gups_builder.llm.fake import FakeLlm
from gups_builder.repl import Repl, context_factory
from gups_builder.session import Session

EE = Path(__file__).resolve().parent / "fixtures" / "llm" / "end_to_end"
PACKAGE = "zones/mansion"


def _load(name: str) -> str:
    return (EE / name).read_text()


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


def _room_prose() -> list[str]:
    """One prose reply per room of the planned mansion, by room id."""
    return [
        json.dumps(
            {
                "name": f"The {r.replace('_', ' ').title()}",
                "desc": f"Dust lies thick across the {r.replace('_', ' ')}.",
            }
        )
        for r in _ROOMS
    ]


def _llm() -> FakeLlm:
    return FakeLlm(
        {
            "logic": [
                # area: brief, plan
                _load("brief_mansion.json"),
                _load("plan_mansion.json"),
                # area refine: the wine cellar turn
                _load("turn_cellar.json"),
                # mob: brief, plan
                _load("mob_brief.json"),
                _load("mob_plan.json"),
                # skill: brief, plan
                _load("skill_brief.json"),
                _load("skill_plan.json"),
            ],
            "prose": [
                *_room_prose(),
                _load("prose_item.json"),
                _load("prose_cellar.json"),
                _load("mob_prose.json"),
                _load("skill_prose.json"),
            ],
        }
    )


@pytest.mark.engine
def test_the_three_modes_end_to_end(gups_bin: Path, fresh_template_world: Path) -> None:
    world = fresh_template_world
    gups = Gups(gups_bin, world)
    llm = _llm()
    session = Session.create(world, "area", PACKAGE)
    console = Console(record=True)
    repl = Repl(
        session,
        context_factory(world, PACKAGE, gups, llm, Config(max_repairs=3)),
        console,
        lambda _: "/quit",
    )
    out_parts: list[str] = []

    def out() -> str:
        # export_text clears the recorded buffer, so accumulate every export.
        out_parts.append(console.export_text())
        return "".join(out_parts)

    pkg = world / PACKAGE

    def lint_is_clean() -> None:
        findings = gups.lint(PACKAGE)
        assert not [f for f in findings if f.severity == "error"], findings

    # -- 1. area: create the mansion, write it --------------------------------
    assert repl.handle("a medium sized creepy mansion") is True
    assert session.state.pending is not None
    assert "+9 rooms" in session.state.pending.summary
    assert repl.handle("/write") is True
    assert (pkg / "rooms.star").is_file()
    assert (pkg / "items.star").is_file()
    assert (pkg / "package.star").is_file()
    assert (pkg / "builder_test.star").is_file()
    lint_is_clean()
    assert "lint: clean" in out()

    # -- 2. a hand edit survives a later turn and its write -------------------
    rooms = (pkg / "rooms.star").read_text()
    hand = "Dust lies thick across the hall, and something watches from the dark."
    assert "Dust lies thick across the hall." in rooms
    (pkg / "rooms.star").write_text(rooms.replace("Dust lies thick across the hall.", hand))
    assert repl.handle("add a wine cellar under the kitchen") is True
    assert session.state.pending is not None
    assert repl.handle("/write") is True
    written = (pkg / "rooms.star").read_text()
    assert hand in written
    assert 'exit(DIR.down, to = ":wine_cellar")' in written
    assert 'exit(DIR.up, to = ":kitchen")' in written
    lint_is_clean()
    assert out().count("lint: clean") == 2

    # -- 3. mob: a restless ghost in the cellar --------------------------------
    assert repl.handle("/mode mob") is True
    assert repl.handle("a restless ghost in the cellar") is True
    assert session.state.pending is not None
    assert session.state.pending.summary == "+1 mob, +0 items, 1 spawn"
    assert repl.handle("/write") is True
    assert 'mob("ghost",' in (pkg / "mobs.star").read_text()
    assert 'spawn(":ghost"' in (pkg / "rooms.star").read_text()
    lint_is_clean()
    assert out().count("lint: clean") == 3

    # -- 4. skill: a chilling touch taught to the ghost ------------------------
    assert repl.handle("/mode skill") is True
    assert repl.handle("a chilling touch the ghost uses") is True
    assert session.state.pending is not None
    assert session.state.pending.summary == "+1 skill, 1 mob taught"
    assert repl.handle("/write") is True
    assert 'skill("chilling_touch",' in (pkg / "skills.star").read_text()
    assert '":chilling_touch": 100' in (pkg / "mobs.star").read_text()
    lint_is_clean()
    assert out().count("lint: clean") == 4

    run = gups.test(PACKAGE)
    assert run.ok, run.output
