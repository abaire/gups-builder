"""Filling out a brief's room concepts before the plan, when it lists too few."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from gups_builder.llm.fake import FakeLlm
from gups_builder.modes.area import AreaBrief, build_from_brief
from tests.test_area_create import (
    _brief_with,
    _ctx,
    _expected_ops,
    _load,
    _prose_replies,
)

if TYPE_CHECKING:
    from pathlib import Path

    from gups_builder.engine.world import WorldDescription


def test_a_brief_with_too_few_room_concepts_gets_more_before_the_plan(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    # The planning model keeps its draft's list; a call that only lists rooms fills it out.
    short = AreaBrief.model_validate_json(_brief_with(room_concepts=["a grand entrance hall"]))
    concepts = json.loads(_load("brief.json"))["room_concepts"]
    llm = FakeLlm(
        {
            "logic": [
                json.dumps({"room_concepts": concepts[:3]}),
                json.dumps({"room_concepts": concepts}),
                _load("plan.json"),
            ],
            "prose": _prose_replies(),
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = build_from_brief(ctx, short)
    assert proposal.ops == _expected_ops()
    first = llm.calls[0][1][-1].content
    assert "- a grand entrance hall" in first
    assert "8 to 15 room concepts" in first
    retry = [m.content for m in llm.calls[1][1] if m.role == "user"][-1]
    assert "the list has 3 room concepts; the area needs 8 to 15, so add 5" in retry
    plan_prompt = llm.calls[2][1][-1].content
    assert '"a dark cellar"' in plan_prompt


def test_a_concept_list_longer_than_the_room_count_is_cut_to_its_maximum(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    # Counting is the one thing the model gets wrong here, and the listed rooms come first.
    short = AreaBrief.model_validate_json(
        _brief_with(room_concepts=["a grand entrance hall"], room_count={"min": 8, "max": 9})
    )
    concepts = json.loads(_load("brief.json"))["room_concepts"]
    long = [*concepts, "an attic", "a chapel"]
    llm = FakeLlm(
        {
            "logic": [json.dumps({"room_concepts": long}), _load("plan.json")],
            "prose": _prose_replies(),
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    build_from_brief(ctx, short)
    plan_prompt = llm.calls[1][1][-1].content
    assert '"a dark cellar"' in plan_prompt
    assert "an attic" not in plan_prompt
