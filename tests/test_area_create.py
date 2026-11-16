"""The area creation pipeline: brief, plan, prose, ops, generated test."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from gups_builder.config import Config
from gups_builder.engine.gups import Gups
from gups_builder.engine.lint import LintFinding
from gups_builder.llm.client import Message
from gups_builder.llm.fake import FakeLlm
from gups_builder.modes.area import (
    AreaBrief,
    AreaGraph,
    AreaPlanTurn,
    build_from_brief,
    create_area,
    format_brief,
    plan_to_grid,
)
from gups_builder.modes.area_checks import place_graph
from gups_builder.modes.area_plan import PlanRequest, plan_area_turn
from gups_builder.modes.common import Context, Proposal
from gups_builder.star.ops import AddDecl, AppendItem
from gups_builder.star.reader import PackageView
from tests.conftest import FakeEngine, PackageState, build_context, load_api, read_fixture

if TYPE_CHECKING:
    from gups_builder.engine.world import WorldDescription
    from gups_builder.star.values import Value

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "llm" / "area_mansion"


def _ctx(
    tmp_path: Path,
    llm: FakeLlm,
    world: WorldDescription,
    lint: list[list[LintFinding]] | None = None,
) -> tuple[Context, FakeEngine]:
    engine = FakeEngine(world, load_api(), lint)
    return build_context(tmp_path, llm, engine, PackageState("zones/mansion")), engine


def _load(name: str) -> str:
    return read_fixture(FIXTURES, name)


NoneS = [
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


def _room_name(rid: str) -> str:
    return f"The {rid.replace('_', ' ').title()}"


def _room_desc(rid: str) -> str:
    return f"Dust lies thick across the {rid.replace('_', ' ')}."


def _room_reply(rid: str) -> str:
    return json.dumps({"name": _room_name(rid), "desc": _room_desc(rid)})


def _prose_replies(*, bad_first: bool = False) -> list[str]:
    rooms = [_room_reply(rid) for rid in NoneS]
    if bad_first:
        rooms.insert(0, _load("prose_room_bad.json"))
    return [*rooms, _load("prose_item.json")]


def _decl(proposal: Proposal, rid: str) -> AddDecl:
    op = next(op for op in proposal.ops if op.id == rid)
    assert isinstance(op, AddDecl)
    return op


def _exit(direction: str, to: str, door: Value | None = None) -> dict[str, Value]:
    kwargs: dict[str, Value] = {"to": to}
    if door is not None:
        kwargs["door"] = door
    return {
        "$call": "exit",
        "args": [{"$const": f"DIR.{direction}"}],
        "kwargs": kwargs,
    }


DOOR: dict[str, Value] = {
    "$call": "door",
    "args": [],
    "kwargs": {"state": {"$const": "DOOR.locked"}, "key": ":silver_key"},
}


def _expected_ops() -> list[AddDecl]:
    return [
        AddDecl(
            file="rooms.star",
            kind="room",
            id="hall",
            args={
                "name": _room_name("hall"),
                "desc": _room_desc("hall"),
                "size": {"$const": "SIZE.large"},
                "exits": [
                    _exit("east", ":kitchen", DOOR),
                    _exit("north", ":study"),
                    _exit("up", ":master_bedroom"),
                ],
            },
        ),
        AddDecl(
            file="rooms.star",
            kind="room",
            id="kitchen",
            args={
                "name": _room_name("kitchen"),
                "desc": _room_desc("kitchen"),
                "exits": [
                    _exit("west", ":hall", DOOR),
                    _exit("north", ":library"),
                    _exit("east", ":parlour"),
                    _exit("down", ":cellar"),
                ],
            },
        ),
        AddDecl(
            file="rooms.star",
            kind="room",
            id="study",
            args={
                "name": _room_name("study"),
                "desc": _room_desc("study"),
                "exits": [_exit("south", ":hall"), _exit("east", ":library")],
            },
        ),
        AddDecl(
            file="rooms.star",
            kind="room",
            id="library",
            args={
                "name": _room_name("library"),
                "desc": _room_desc("library"),
                "exits": [
                    _exit("south", ":kitchen"),
                    _exit("west", ":study"),
                    _exit("up", ":nursery"),
                ],
            },
        ),
        AddDecl(
            file="rooms.star",
            kind="room",
            id="parlour",
            args={
                "name": _room_name("parlour"),
                "desc": _room_desc("parlour"),
                "exits": [_exit("west", ":kitchen"), _exit("up", ":bedroom")],
            },
        ),
        AddDecl(
            file="rooms.star",
            kind="room",
            id="master_bedroom",
            args={
                "name": _room_name("master_bedroom"),
                "desc": _room_desc("master_bedroom"),
                "exits": [_exit("down", ":hall")],
            },
        ),
        AddDecl(
            file="rooms.star",
            kind="room",
            id="nursery",
            args={
                "name": _room_name("nursery"),
                "desc": _room_desc("nursery"),
                "exits": [_exit("down", ":library"), _exit("southeast", ":bedroom")],
            },
        ),
        AddDecl(
            file="rooms.star",
            kind="room",
            id="bedroom",
            args={
                "name": _room_name("bedroom"),
                "desc": _room_desc("bedroom"),
                "exits": [_exit("down", ":parlour"), _exit("northwest", ":nursery")],
            },
        ),
        AddDecl(
            file="rooms.star",
            kind="room",
            id="cellar",
            args={
                "name": _room_name("cellar"),
                "desc": _room_desc("cellar"),
                "size": {"$const": "SIZE.cramped"},
                "tags": ["dark"],
                "exits": [_exit("up", ":kitchen")],
            },
        ),
        AddDecl(
            file="items.star",
            kind="item",
            id="silver_key",
            args={
                "name": "a small silver key",
                "keywords": ["key", "silver"],
                "desc": "A small silver key, cold to the touch.",
                "kind": "key",
            },
        ),
    ]


def test_a_brief_with_questions_is_returned_as_is(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load("brief_questions.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_min)
    result = create_area(ctx, "a creepy mansion")
    assert isinstance(result, AreaBrief)
    assert result.questions == ["How many floors should the manor have?"]
    assert len(llm.calls) == 1


def test_mansion_creation_produces_the_expected_ops(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load("brief.json"), _load("plan.json")], "prose": _prose_replies()})
    ctx, engine = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    assert proposal.ops == _expected_ops()
    assert proposal.new_files == {
        "package.star": (
            "package(\n"
            "    api_version = 1,\n"
            '    kind = "zone",\n'
            '    title = "The Manor House",\n'
            "    reset_world_ticks = 10,\n"
            ")\n"
        )
    }
    assert proposal.summary == "+9 rooms, +1 item, 20 exits"
    assert proposal.grid == {
        "hall": (0, 0, 0),
        "kitchen": (1, 0, 0),
        "study": (0, 1, 0),
        "library": (1, 1, 0),
        "parlour": (2, 0, 0),
        "master_bedroom": (0, 0, 1),
        "nursery": (1, 1, 1),
        "bedroom": (2, 0, 1),
        "cellar": (1, 0, -1),
    }
    assert engine.test_calls == 1


def test_a_plan_with_a_passage_to_an_unknown_room_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [_load("brief.json"), _load("plan_bad.json"), _load("plan.json")],
            "prose": _prose_replies(),
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    retry_messages = llm.calls[2][1]
    last_user = [m.content for m in retry_messages if m.role == "user"][-1]
    assert "the connection from 'hall' names the unknown room 'nursry'" in last_user
    # The rooms beyond the typo are not also reported as cut off: one fix settles them.
    assert "not reachable" not in last_user


def test_prose_with_a_newline_is_repaired(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [_load("brief.json"), _load("plan.json")],
            "prose": _prose_replies(bad_first=True),
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    retry_messages = llm.calls[3][1]
    last_user = [m.content for m in retry_messages if m.role == "user"][-1]
    assert "desc must be one paragraph" in last_user
    hall = _decl(proposal, "hall")
    assert hall.args["desc"] == _room_desc("hall")


def test_size_is_left_out_when_the_world_has_track_off(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    world = world_min.model_copy(
        update={"features": [f for f in world_min.features if f != "track"]}
    )
    llm = FakeLlm({"logic": [_load("brief.json"), _load("plan.json")], "prose": _prose_replies()})
    ctx, _ = _ctx(tmp_path, llm, world)
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    hall = _decl(proposal, "hall")
    assert "size" not in hall.args
    cellar = _decl(proposal, "cellar")
    assert "size" not in cellar.args


def _plan_with(**changes: object) -> str:
    plan = json.loads(_load("plan.json"))
    plan.update(changes)
    return json.dumps(plan)


def _brief_with(**changes: object) -> str:
    brief = json.loads(_load("brief.json"))
    brief.update(changes)
    return json.dumps(brief)


def test_a_link_adds_the_exit_and_a_confirmed_return_exit(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    link = {"room": "hall", "exit": "south", "external_label": "//zones/tutorial:gate"}
    llm = FakeLlm(
        {
            "logic": [_brief_with(link_to="//zones/tutorial:gate"), _plan_with(link=link)],
            "prose": _prose_replies(),
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a medium sized creepy mansion south of the tutorial gate")
    assert isinstance(proposal, Proposal)
    hall_exits = _decl(proposal, "hall").args["exits"]
    assert isinstance(hall_exits, list)
    assert hall_exits[-1] == _exit("south", "//zones/tutorial:gate")
    assert proposal.link_ops == [
        AppendItem(
            kind="room",
            id="gate",
            name="exits",
            value=_exit("north", "//zones/mansion:hall"),
        )
    ]
    assert proposal.link_package == "zones/tutorial"
    assert proposal.summary == (
        "+9 rooms, +1 item, 21 exits, 1 link to //zones/tutorial:gate (needs confirmation)"
    )


def test_a_bad_link_is_repaired(tmp_path: Path, world_min: WorldDescription) -> None:
    bad = {"room": "attic", "exit": "south", "external_label": "//zones/tutorial:gate"}
    good = {"room": "hall", "exit": "south", "external_label": "//zones/tutorial:gate"}
    llm = FakeLlm(
        {
            "logic": [
                _brief_with(link_to="//zones/tutorial:gate"),
                _plan_with(link=bad),
                _plan_with(link=good),
            ],
            "prose": _prose_replies(),
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    retry = [m.content for m in llm.calls[2][1] if m.role == "user"][-1]
    assert "the link starts from the unknown room 'attic'" in retry


def test_a_bare_external_label_is_held_to_the_briefs_link(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    # A model given a full label in the brief may echo only the room's id in the plan.
    link = {"room": "hall", "exit": "south", "external_label": "gate"}
    llm = FakeLlm(
        {
            "logic": [_brief_with(link_to="//zones/tutorial:gate"), _plan_with(link=link)],
            "prose": _prose_replies(),
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a mansion south of the tutorial gate")
    assert isinstance(proposal, Proposal)
    assert sum(1 for role, _, _ in llm.calls if role == "logic") == 2
    assert proposal.link_package == "zones/tutorial"
    assert proposal.summary.endswith("1 link to //zones/tutorial:gate (needs confirmation)")


def test_a_link_the_brief_does_not_name_is_dropped(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    link = {"room": "hall", "exit": "south", "external_label": "gate"}
    llm = FakeLlm(
        {"logic": [_load("brief.json"), _plan_with(link=link)], "prose": _prose_replies()}
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    assert sum(1 for role, _, _ in llm.calls if role == "logic") == 2
    assert proposal.link_ops == []
    assert proposal.link_package is None
    assert "link" not in proposal.summary


def test_a_brief_linking_to_a_missing_room_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _brief_with(link_to="//zones/nowhere:void"),
                _brief_with(link_to="//zones/tutorial:gate"),
                _plan_with(link={"room": "hall", "exit": "south", "external_label": "gate"}),
            ],
            "prose": _prose_replies(),
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    retry = [m.content for m in llm.calls[1][1] if m.role == "user"][-1]
    assert (
        "link_to '//zones/nowhere:void' is not a room of another package; use the full "
        "label of one of the rooms listed as linkable, or null"
    ) in retry


def test_the_prompts_ask_for_a_link_only_when_the_user_does(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    turn = {"reply": "ok", "brief": json.loads(_load("brief.json"))}
    llm = FakeLlm(
        {
            "logic": [_load("brief.json"), _load("plan.json"), json.dumps(turn)],
            "prose": _prose_replies(),
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_area(ctx, "a medium sized creepy mansion")
    plan_area_turn(ctx, PlanRequest("a mansion", [], None))
    brief_prompt, graph_prompt, turn_prompt = (
        [m.content for m in messages if m.role == "user"][-1]
        for role, messages, _ in llm.calls
        if role == "logic"
    )
    assert "Leave link_to null\nunless the request asks to connect" in brief_prompt
    assert "PlannedLink only when the\nbrief's link_to is set" in graph_prompt
    assert "`link_to`: null unless the user asked to connect" in turn_prompt
    # The planning prompts say what can be built, so a small model declines the rest.
    for prompt in (brief_prompt, turn_prompt):
        assert "Random exits: one exit that sends the player to a room picked at random" in prompt
        assert "You cannot ask it for\n  anything now" in prompt
        assert "never\nsay it is done" in prompt
    # One question at a time, and nothing asked twice.
    assert "At most one question at a time" in brief_prompt
    assert "never ask about linking" in brief_prompt
    assert "`questions`: at most one entry" in turn_prompt
    assert "Ask at most one question" in turn_prompt
    assert "a RandomExit for every exit that sends the player to a room\npicked" in graph_prompt
    assert "with one_way true and\nno b_exit" in graph_prompt
    assert "rewrite every concept a later decision changed" in turn_prompt


def test_a_bare_link_to_in_the_brief_becomes_the_full_label(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_brief_with(link_to="gate", questions=["How many floors?"])]})
    ctx, _ = _ctx(tmp_path, llm, world_min)
    brief = create_area(ctx, "a mansion by the gate")
    assert isinstance(brief, AreaBrief)
    assert brief.link_to == "//zones/tutorial:gate"


def test_a_return_exit_the_external_room_already_uses_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    # The tutorial gate already has a west exit, the default return of an east link.
    bad = {"room": "parlour", "exit": "east", "external_label": "//zones/tutorial:gate"}
    good = {
        "room": "parlour",
        "exit": "east",
        "external_label": "//zones/tutorial:gate",
        "return_exit": "mansion",
    }
    llm = FakeLlm(
        {
            "logic": [
                _brief_with(link_to="//zones/tutorial:gate"),
                _plan_with(link=bad),
                _plan_with(link=good),
            ],
            "prose": _prose_replies(),
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    retry = [m.content for m in llm.calls[2][1] if m.role == "user"][-1]
    assert (
        "the link's return exit 'west' is already an exit of '//zones/tutorial:gate'; "
        "choose another return_exit"
    ) in retry
    assert [op.value for op in proposal.link_ops if isinstance(op, AppendItem)] == [
        {
            "$call": "exit",
            "args": ["mansion"],
            "kwargs": {"to": "//zones/mansion:parlour", "one_way": True},
        }
    ]


def test_plan_problems_catch_bad_ids_and_todos(tmp_path: Path, world_min: WorldDescription) -> None:
    plan = json.loads(_load("plan.json"))
    plan["rooms"][1]["id"] = "hall"
    plan["keys"].append({"id": "Silver Key", "concept": "a second key"})
    plan["todos"] = [{"room": "attic", "text": "write a hook"}]
    llm = FakeLlm(
        {
            "logic": [_load("brief.json"), json.dumps(plan), _load("plan.json")],
            "prose": _prose_replies(),
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_area(ctx, "a medium sized creepy mansion")
    retry = [m.content for m in llm.calls[2][1] if m.role == "user"][-1]
    assert "the id 'hall' is used twice in the plan" in retry
    assert "the id 'Silver Key' is not snake_case" in retry
    assert "the todo names the unknown room 'attic'" in retry


def test_an_existing_package_star_is_kept(tmp_path: Path, world_min: WorldDescription) -> None:
    pkg = tmp_path / "zones" / "mansion"
    pkg.mkdir(parents=True)
    (pkg / "package.star").write_text('package(api_version = 1, kind = "zone", title = "Mine")\n')
    llm = FakeLlm({"logic": [_load("brief.json"), _load("plan.json")], "prose": _prose_replies()})
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    assert proposal.new_files == {}


def test_a_lint_repair_resends_the_plan_prompt_and_keeps_the_prose(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [_load("brief.json"), _load("plan.json"), _load("plan.json")],
            "prose": _prose_replies(),
        }
    )
    lint = [[LintFinding("error", "zones/mansion/rooms.star", 3, "something is off")]]
    ctx, _ = _ctx(tmp_path, llm, world_min, lint=lint)
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    repair = [m.content for m in llm.calls[-1][1] if m.role == "user"][-1]
    assert "Layout: Give no coordinates" in repair
    assert "zones/mansion/rooms.star:3: something is off" in repair
    # 9 rooms and 1 key, written once: nothing about them changed in the repaired plan.
    assert sum(1 for role, _, _ in llm.calls if role == "prose") == 10
    assert llm.calls[-1][0] == "logic"


def test_plan_to_grid_maps_rooms_connections_and_portals() -> None:
    plan = place_graph(AreaGraph.model_validate_json(_load("plan.json")))
    grid = plan_to_grid(plan)
    assert [room.id for room in grid.rooms] == [
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
    assert grid.rooms[0].coord == (0, 0, 0)
    assert grid.connections == plan.connections
    assert grid.portals == []


def test_plan_area_turn_starts_and_refines_a_brief(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    brief1 = json.loads(_load("brief.json"))
    turn1 = {"reply": "Here is an initial mansion plan. Want a cellar?", "brief": brief1}
    brief2 = {**brief1, "title": "The Haunted Manor", "link_to": "gate"}
    turn2 = {"reply": "Updated the title and linked to the tutorial gate.", "brief": brief2}
    llm = FakeLlm({"logic": [json.dumps(turn1), json.dumps(turn2)]})
    ctx, _ = _ctx(tmp_path, llm, world_min)

    first = plan_area_turn(ctx, PlanRequest("let's design a creepy mansion", [], None)).turn
    assert isinstance(first, AreaPlanTurn)
    assert first.reply == "Here is an initial mansion plan. Want a cellar?"
    assert first.brief.title == "The Manor House"
    prompt1 = [m.content for m in llm.calls[0][1] if m.role == "user"][-1]
    assert "Current draft brief:\nnone" in prompt1

    history = [
        Message("user", "let's design a creepy mansion"),
        Message("assistant", first.reply),
    ]
    second = plan_area_turn(
        ctx, PlanRequest("call it The Haunted Manor and link to the gate", history, first.brief)
    ).turn
    assert second.brief.title == "The Haunted Manor"
    assert second.brief.link_to == "//zones/tutorial:gate"
    prompt2 = [m.content for m in llm.calls[1][1] if m.role == "user"][-1]
    assert '"The Manor House"' in prompt2
    assert "assistant: Here is an initial mansion plan." in prompt2

    rendered = format_brief(second.brief)
    assert "Plan: The Haunted Manor" in rendered
    assert "Size: 8-15 rooms" in rendered
    assert "Link: //zones/tutorial:gate" in rendered
    assert "- a grand entrance hall" in rendered


def test_build_from_brief_generates_proposal_without_rebriefing(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    brief = AreaBrief.model_validate_json(_load("brief.json"))
    llm = FakeLlm({"logic": [_load("plan.json")], "prose": _prose_replies()})
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = build_from_brief(ctx, brief)
    assert proposal.ops == _expected_ops()
    assert sum(1 for role, _, _ in llm.calls if role == "logic") == 1


def _real_mansion(gups_bin: Path, template_world: Path, plan: str) -> Proposal:
    gups = Gups(gups_bin, template_world)
    world = gups.describe_world()
    api = gups.api()
    llm = FakeLlm({"logic": [_load("brief.json"), plan], "prose": _prose_replies()})
    ctx = Context(
        world_dir=template_world,
        world=world,
        api=api,
        package="zones/mansion",
        view=PackageView(path="zones/mansion", files={}, decls=[], opaque=[]),
        files={},
        llm=llm,
        config=Config(max_repairs=3),
        engine=gups,
    )
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    return proposal


def _passes(proposal: Proposal) -> None:
    assert not any(note.startswith("NOT VALID:") for note in proposal.notes)
    assert not any(note.startswith("BUILDER BUG:") for note in proposal.notes)


@pytest.mark.engine
def test_the_mansion_passes_the_real_engine(gups_bin: Path, template_world: Path) -> None:
    _passes(_real_mansion(gups_bin, template_world, _load("plan.json")))


def test_the_plan_prompt_asks_for_directions_not_coordinates(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load("brief.json"), _load("plan.json")], "prose": _prose_replies()})
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_area(ctx, "a medium sized creepy mansion")
    _, messages, schema = llm.calls[1]
    assert schema is not None
    assert "at" not in schema["$defs"]["GraphRoom"]["properties"]
    assert "direction" in schema["$defs"]["Passage"]["properties"]
    assert "Give no coordinates" in messages[-1].content


def test_a_passage_the_grid_cannot_place_becomes_a_portal_in_the_files(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    # The library holds the cell northwest of the parlour, so the study cannot go there.
    plan = json.loads(_load("plan.json"))
    plan["passages"].append({"a": "parlour", "b": "study", "direction": "northwest"})
    llm = FakeLlm({"logic": [_load("brief.json"), json.dumps(plan)], "prose": _prose_replies()})
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_area(ctx, "a medium sized creepy mansion")
    assert isinstance(proposal, Proposal)
    assert proposal.grid is not None
    assert proposal.grid["study"] == (0, 1, 0)
    assert proposal.grid["parlour"] == (2, 0, 0)
    parlour = _decl(proposal, "parlour")
    assert "northwest" in json.dumps(parlour.args["exits"])
    assert not any(note.startswith("NOT VALID:") for note in proposal.notes)
