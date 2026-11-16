"""Area planning turns: decisions are kept, old messages are folded, never just dropped."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from gups_builder.config import Config, RoleConfig
from gups_builder.llm.client import Message
from gups_builder.llm.fake import FakeLlm
from gups_builder.modes.area_checks import AreaBrief
from gups_builder.modes.area_plan import PlanRequest, _prompt_parts, plan_area_turn
from tests.conftest import FakeEngine, PackageState, build_context, load_api, read_fixture

if TYPE_CHECKING:
    from gups_builder.engine.world import WorldDescription
    from gups_builder.modes.common import Context

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llm" / "area_mansion"
BRIEF = json.loads(read_fixture(FIXTURES, "brief.json"))
CLOSED = "doors are closed, never locked"
PER_LEVEL = "10-15 rooms on each catacomb level"


def _ctx(tmp_path: Path, llm: FakeLlm, world: WorldDescription) -> Context:
    engine = FakeEngine(world, load_api())
    return build_context(tmp_path, llm, engine, PackageState("zones/mansion"))


def _turn(decisions: list[str]) -> str:
    return json.dumps({"reply": "Noted.", "brief": {**BRIEF, "decisions": decisions}})


def _history(n: int) -> list[Message]:
    return [
        Message("assistant" if i % 2 else "user", f"message {i:02} " + "x" * 120) for i in range(n)
    ]


def _with_history_room(ctx: Context, request: PlanRequest, room: int) -> None:
    """Size the logic context so the history has ``room`` tokens to itself."""
    fixed = -_prompt_parts(ctx, request, 0).room
    max_tokens = 512
    logic = RoleConfig(temperature=0.2, max_tokens=max_tokens, context_tokens=fixed + room + 512)
    ctx.config = Config(logic=logic, max_repairs=1)


def _prompt(llm: FakeLlm, call: int) -> str:
    return [m.content for m in llm.calls[call][1] if m.role == "user"][-1]


def test_old_messages_are_folded_into_the_decisions_not_dropped(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    fold = json.dumps({"decisions": [CLOSED, PER_LEVEL]})
    llm = FakeLlm({"logic": [fold, _turn([PER_LEVEL, CLOSED])]})
    ctx = _ctx(tmp_path, llm, world_min)
    brief = AreaBrief.model_validate({**BRIEF, "decisions": [PER_LEVEL]})
    request = PlanRequest("and a well house", _history(20), brief)
    _with_history_room(ctx, request, 200)

    result = plan_area_turn(ctx, request)

    assert result.folded > 0
    assert result.dropped == []
    fold_prompt, plan_prompt = _prompt(llm, 0), _prompt(llm, 1)
    assert "message 00" in fold_prompt
    assert PER_LEVEL in fold_prompt  # what is recorded already, so it is not added twice
    # The folded messages leave the prompt; what they settled stays, added once.
    assert "message 00" not in plan_prompt
    assert "(earlier messages are folded into the brief's decisions)" in plan_prompt
    assert plan_prompt.count(CLOSED) == 1
    assert "message 19" in plan_prompt


def test_a_history_that_fits_is_sent_whole_with_no_fold(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_turn([])]})
    ctx = _ctx(tmp_path, llm, world_min)
    result = plan_area_turn(ctx, PlanRequest("and a barn", _history(12), None))
    assert result.folded == 0
    assert len(llm.calls) == 1
    assert "message 00" in _prompt(llm, 0)
    assert "folded" not in _prompt(llm, 0)


def test_a_decision_the_turn_loses_is_reported(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm({"logic": [_turn([PER_LEVEL])]})
    ctx = _ctx(tmp_path, llm, world_min)
    brief = AreaBrief.model_validate({**BRIEF, "decisions": [PER_LEVEL, CLOSED]})
    result = plan_area_turn(ctx, PlanRequest("make it bigger", [], brief))
    assert result.dropped == [CLOSED]


def test_the_prompt_keeps_stable_text_first_and_counts_links_against_history(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_turn([]), _turn([])]})
    ctx = _ctx(tmp_path, llm, world_min)
    history = _history(6)
    plan_area_turn(ctx, PlanRequest("one", history, None))
    plan_area_turn(ctx, PlanRequest("two", [*history, *_history(2)], None))
    first, second = _prompt(llm, 0), _prompt(llm, 1)
    assert second.startswith(first[: first.index("\n\nCurrent draft brief:")])
    assert first.index("could be linked") < first.index("Conversation so far:")
