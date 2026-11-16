"""Exit names and exit kinds through area creation: renaming, one-way and random exits."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from rich.console import Console

from gups_builder.config import Config
from gups_builder.engine.gups import Gups
from gups_builder.llm.fake import FakeLlm
from gups_builder.modes.area import create_area
from gups_builder.modes.common import Proposal
from gups_builder.repl import Repl, context_factory
from gups_builder.session import Session
from tests.test_area_create import (
    _brief_with,
    _ctx,
    _decl,
    _load,
    _passes,
    _plan_with,
    _prose_replies,
    _real_mansion,
)

if TYPE_CHECKING:
    from pathlib import Path

    from gups_builder.engine.world import WorldDescription


def test_clashing_portal_exits_are_renamed_without_a_repair(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    portals = [
        {"a": "cellar", "a_exit": "Secret Stair", "b": "study", "b_exit": "trapdoor"},
        {"a": "cellar", "a_exit": "secret stair", "b": "nursery", "b_exit": "hatch"},
    ]
    llm = FakeLlm(
        {"logic": [_load("brief.json"), _plan_with(portals=portals)], "prose": _prose_replies()}
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    assert sum(1 for role, _, _ in llm.calls if role == "logic") == 2
    cellar = str(_decl(proposal, "cellar"))
    assert "secret_stair_to_study" in cellar
    assert "secret_stair_to_nursery" in cellar


def test_a_portal_gives_way_to_the_links_exit_name(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    link = {
        "room": "hall",
        "exit": "gate",
        "return_exit": "mansion",
        "external_label": "//zones/tutorial:gate",
    }
    portals = [{"a": "hall", "a_exit": "gate", "b": "nursery", "b_exit": "hatch"}]
    llm = FakeLlm(
        {
            "logic": [
                _brief_with(link_to="//zones/tutorial:gate"),
                _plan_with(link=link, portals=portals),
            ],
            "prose": _prose_replies(),
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a mansion beside the tutorial gate")
    assert isinstance(proposal, Proposal)
    assert sum(1 for role, _, _ in llm.calls if role == "logic") == 2
    assert "gate_to_nursery" in str(_decl(proposal, "hall"))
    # 'gate' and 'mansion' are not opposite directions, so both sides of the link are one-way.
    hall = str(_decl(proposal, "hall"))
    assert "'args': ['gate'], 'kwargs': {'to': '//zones/tutorial:gate', 'one_way': True}" in hall
    assert "'one_way': True" in str(proposal.link_ops[0])


_TRICKS = {
    "random_exits": [{"room": "cellar", "exit": "Trick Door", "to": ["study", "nursery"]}],
    "portals": [{"a": "nursery", "a_exit": "chute", "b": "cellar", "one_way": True}],
}


def test_random_exits_and_one_way_portals_are_built(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {"logic": [_load("brief.json"), _plan_with(**_TRICKS)], "prose": _prose_replies()}
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    assert sum(1 for role, _, _ in llm.calls if role == "logic") == 2
    cellar = str(_decl(proposal, "cellar"))
    assert "'args': ['trick_door'], 'kwargs': {'to': [':study', ':nursery']}" in cellar
    nursery = str(_decl(proposal, "nursery"))
    assert "'args': ['chute'], 'kwargs': {'to': ':cellar', 'one_way': True}" in nursery
    assert "chute" not in cellar
    assert "1 random exit" in proposal.summary
    prose_prompts = [m.content for role, msgs, _ in llm.calls if role == "prose" for m in msgs]
    assert any("trick_door: one of " in text for text in prose_prompts)


@pytest.mark.engine
def test_random_exits_and_one_way_portals_pass_the_real_engine(
    gups_bin: Path, fresh_template_world: Path
) -> None:
    gups = Gups(gups_bin, fresh_template_world)
    llm = FakeLlm(
        {"logic": [_load("brief.json"), _plan_with(**_TRICKS)], "prose": _prose_replies()}
    )
    session = Session.create(fresh_template_world, "area", "zones/mansion")
    console = Console(record=True)
    factory = context_factory(fresh_template_world, "zones/mansion", gups, llm, Config())
    repl = Repl(session, factory, console, lambda _: "/quit")
    assert repl.handle("a medium sized creepy mansion") is True
    assert repl.handle("/write") is True
    assert not [f for f in gups.lint("zones/mansion") if f.severity == "error"]
    rooms = {room.label: room for room in gups.describe_world().rooms}
    cellar_exits = {e.dir: e for e in rooms["//zones/mansion:cellar"].exits}
    assert "trick_door" in cellar_exits
    assert cellar_exits["trick_door"].one_way
    nursery_exits = {e.dir: e for e in rooms["//zones/mansion:nursery"].exits}
    assert nursery_exits["chute"].one_way


@pytest.mark.engine
def test_renamed_portal_exits_pass_the_real_engine(gups_bin: Path, template_world: Path) -> None:
    portals = [
        {"a": "cellar", "a_exit": "one-way trick door", "b": "study", "b_exit": "trapdoor"},
        {"a": "cellar", "a_exit": "one-way trick door", "b": "nursery", "b_exit": "hatch"},
    ]
    proposal = _real_mansion(gups_bin, template_world, _plan_with(portals=portals))
    _passes(proposal)
    assert "one_way_trick_door_to_nursery" in str(_decl(proposal, "cellar"))
