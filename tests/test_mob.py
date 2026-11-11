"""The mob mode: creating mobs with loot and spawns, and refining them turn by turn.

The creation tests use the minimal world, whose zones/tutorial package holds two
rooms, a guide, a key and one spell, and script the brief, plan and prose calls
with an empty view, as area creation does for a new package. The refinement
tests start from the goblins fixture, the package as create_mobs left it, and
drive mob_turn with scripted replies. The generated test's mob blocks are
checked directly, and the engine test stages the scripted goblins into a copy
of the template world.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from conftest import FakeEngine, PackageState, build_context, load_api, read_fixture

from gups_builder.config import Config
from gups_builder.engine.gups import Gups
from gups_builder.engine.lint import LintFinding
from gups_builder.engine.world import (
    MobInfo,
    PackageInfo,
    Schema,
    SchemaName,
    WorldDescription,
)
from gups_builder.llm.fake import FakeLlm
from gups_builder.modes.common import Context, Proposal
from gups_builder.modes.mob import MobBrief, create_mobs, mob_turn
from gups_builder.modes.testgen import builder_test
from gups_builder.star.ops import (
    AddDecl,
    AppendItem,
    ItemMatch,
    RemoveDecl,
    RemoveItem,
    SetArg,
)
from gups_builder.star.reader import DeclView, PackageView, read_package
from gups_builder.star.values import Value, is_editable
from gups_builder.star.writer import apply_ops

CREATE = Path(__file__).resolve().parent / "fixtures" / "llm" / "mob_create"
REFINE = Path(__file__).resolve().parent / "fixtures" / "llm" / "mob_refine"
GOBLINS = Path(__file__).resolve().parent / "fixtures" / "goblins"
MOB_ROOMS = Path(__file__).resolve().parent / "fixtures" / "mob_rooms"
REPO = Path(__file__).resolve().parent.parent
PACKAGE = "zones/tutorial"


def _load(base: Path, name: str) -> str:
    return read_fixture(base, name)


def _ctx(
    tmp_path: Path,
    llm: FakeLlm,
    world: WorldDescription,
    files: dict[str, str] | None = None,
    view: PackageView | None = None,
    package: str = PACKAGE,
    lint: list[list[LintFinding]] | None = None,
) -> tuple[Context, FakeEngine]:
    engine = FakeEngine(world, load_api(), lint)
    return build_context(tmp_path, llm, engine, PackageState(package, files or {}, view)), engine


def _goblins_ctx(
    tmp_path: Path, llm: FakeLlm, world: WorldDescription
) -> tuple[Context, FakeEngine]:
    files = {
        name: (GOBLINS / name).read_text() for name in ("rooms.star", "items.star", "mobs.star")
    }
    return _ctx(tmp_path, llm, world, files=files, view=read_package(GOBLINS, PACKAGE))


def _rooms_ctx(tmp_path: Path, llm: FakeLlm, world: WorldDescription) -> tuple[Context, FakeEngine]:
    """A creation context: the package has rooms but no mobs or items yet."""
    files = {"rooms.star": (MOB_ROOMS / "rooms.star").read_text()}
    return _ctx(tmp_path, llm, world, files=files, view=read_package(MOB_ROOMS, PACKAGE))


def _prose() -> list[str]:
    return [
        _load(CREATE, "prose_goblin.json"),
        _load(CREATE, "prose_shaman.json"),
        _load(CREATE, "prose_idol.json"),
    ]


WANDER: Value = {"$call": "wander", "args": [], "kwargs": {}}


def _expected_ops() -> list[AddDecl | AppendItem]:
    return [
        AddDecl(
            file="mobs.star",
            kind="mob",
            id="goblin",
            args={
                "name": "a goblin",
                "keywords": ["goblin"],
                "desc": "A wiry green creature with shifty yellow eyes.",
                "level": 3,
                "stats": {"$call": "stats", "args": [], "kwargs": {"str": 10, "dex": 12}},
                "vitals": {"$call": "vitals", "args": [], "kwargs": {"hp": 30}},
                "behaviors": [WANDER],
                "loot": [{"$call": "loot", "args": [":goblin_idol"], "kwargs": {"chance": 10}}],
            },
        ),
        AddDecl(
            file="mobs.star",
            kind="mob",
            id="goblin_shaman",
            args={
                "name": "a goblin shaman",
                "keywords": ["goblin", "shaman"],
                "desc": "A hooded goblin who mutters to the spirits.",
                "level": 5,
                "stats": {"$call": "stats", "args": [], "kwargs": {"int": 14}},
                "vitals": {"$call": "vitals", "args": [], "kwargs": {"hp": 20, "mana": 30}},
                "skills": {"$dict": [["//zones/tutorial:fireball", 40]]},
                "behaviors": [WANDER],
            },
        ),
        AddDecl(
            file="items.star",
            kind="item",
            id="goblin_idol",
            args={
                "name": "a goblin idol",
                "keywords": ["idol", "goblin"],
                "desc": "A carved wooden totem, older than it looks.",
                "kind": "misc",
            },
        ),
        AppendItem(
            kind="room",
            id="hall",
            name="spawns",
            value={"$call": "spawn", "args": [":goblin"], "kwargs": {"count": 2}},
        ),
    ]


def _last_user(call_index: int, llm: FakeLlm) -> str:
    return [m.content for m in llm.calls[call_index][1] if m.role == "user"][-1]


# -- creation ---------------------------------------------------------------


def test_a_brief_with_questions_is_returned_as_is(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load(CREATE, "brief_questions.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_min)
    result = create_mobs(ctx, "two goblins")
    assert isinstance(result, MobBrief)
    assert result.questions == ["Should the goblins attack players on sight?"]
    assert len(llm.calls) == 1


def test_creation_produces_the_expected_ops(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), _load(CREATE, "plan.json")],
            "prose": _prose(),
        }
    )
    ctx, engine = _rooms_ctx(tmp_path, llm, world_min)
    proposal = create_mobs(ctx, "two goblins, one a shaman that knows the fireball")
    assert isinstance(proposal, Proposal)
    assert proposal.ops == _expected_ops()
    assert proposal.new_files == {}
    assert proposal.summary == "+2 mobs, +1 item, 1 spawn"
    assert not proposal.notes
    assert engine.test_calls == 1
    plan_prompt = "\n".join(m.content for m in llm.calls[1][1])
    assert "//zones/tutorial:fireball" in plan_prompt
    assert "//zones/tutorial:guide" in plan_prompt
    assert "wander" in plan_prompt
    # The brief's concepts and style notes reach the plan.
    assert "the shaman knows the fireball spell" in plan_prompt
    assert "sight, smell, sound, spell" in plan_prompt


def test_an_unknown_stat_is_repaired(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _load(CREATE, "plan_unknown_stat.json"),
                _load(CREATE, "plan.json"),
            ],
            "prose": _prose(),
        }
    )
    ctx, _ = _rooms_ctx(tmp_path, llm, world_min)
    proposal = create_mobs(ctx, "two goblins")
    assert isinstance(proposal, Proposal)
    assert "'charm' is not a stat of this world's schema" in _last_user(2, llm)
    assert proposal.ops == _expected_ops()


def test_an_unknown_skill_is_repaired(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _load(CREATE, "plan_unknown_skill.json"),
                _load(CREATE, "plan.json"),
            ],
            "prose": _prose(),
        }
    )
    ctx, _ = _rooms_ctx(tmp_path, llm, world_min)
    proposal = create_mobs(ctx, "two goblins")
    assert isinstance(proposal, Proposal)
    assert "the skill '//zones/tutorial:zap' is not a skill of this world" in _last_user(2, llm)
    assert proposal.ops == _expected_ops()


def test_an_unknown_behavior_argument_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _load(CREATE, "plan_bad_behavior.json"),
                _load(CREATE, "plan.json"),
            ],
            "prose": _prose(),
        }
    )
    ctx, _ = _rooms_ctx(tmp_path, llm, world_min)
    proposal = create_mobs(ctx, "two goblins")
    assert isinstance(proposal, Proposal)
    assert "the behavior 'wander' has no argument 'speed'" in _last_user(2, llm)
    assert proposal.ops == _expected_ops()


def test_feature_off_fields_are_repaired(tmp_path: Path, world_min: WorldDescription) -> None:
    world = world_min.model_copy(update={"features": ["combat", "track"]})
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _load(CREATE, "plan_features_off.json"),
                _load(CREATE, "plan_plain.json"),
            ],
            "prose": _prose(),
        }
    )
    ctx, _ = _rooms_ctx(tmp_path, llm, world)
    proposal = create_mobs(ctx, "two goblins")
    assert isinstance(proposal, Proposal)
    retry = _last_user(2, llm)
    assert "the world has the leveling feature off" in retry
    assert "the world has the skills feature off" in retry
    shaman = next(op for op in proposal.ops if op.id == "goblin_shaman")
    assert isinstance(shaman, AddDecl)
    assert "level" not in shaman.args
    assert "skills" not in shaman.args


def test_a_robot_world_produces_robot_vitals(tmp_path: Path, world_min: WorldDescription) -> None:
    robot = world_min.model_copy(deep=True)
    robot.schema.vitals = [
        SchemaName(name="integrity", label="Integrity"),
        SchemaName(name="power", label="Power"),
        SchemaName(name="sanity", label="Sanity"),
    ]
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief_robot.json"), _load(CREATE, "plan_robot.json")],
            "prose": [_load(CREATE, "prose_robot.json")],
        }
    )
    ctx, _ = _rooms_ctx(tmp_path, llm, robot)
    proposal = create_mobs(ctx, "a sentinel robot")
    assert isinstance(proposal, Proposal)
    bot = next(op for op in proposal.ops if op.id == "sentinel_bot")
    assert isinstance(bot, AddDecl)
    assert bot.args["vitals"] == {
        "$call": "vitals",
        "args": [],
        "kwargs": {"integrity": 100, "power": 20},
    }
    plan_prompt = "\n".join(m.content for m in llm.calls[1][1])
    assert "integrity" in plan_prompt


# -- refinement -------------------------------------------------------------


def test_make_the_shaman_tougher_changes_only_vitals(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load(REFINE, "turn_tougher.json")]})
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    turn, proposal = mob_turn(ctx, "make the shaman tougher", [])
    assert turn.kind == "edit"
    assert proposal is not None
    assert proposal.ops == [
        SetArg(
            kind="mob",
            id="goblin_shaman",
            name="vitals",
            value={"$call": "vitals", "args": [], "kwargs": {"hp": 40}},
        )
    ]


def test_place_appends_a_spawn_to_the_room(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm({"logic": [_load(REFINE, "turn_place.json")]})
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    turn, proposal = mob_turn(ctx, "move the goblin into the gate", [])
    assert turn.kind == "edit"
    assert proposal is not None
    assert proposal.ops == [
        AppendItem(
            kind="room",
            id="gate",
            name="spawns",
            value={"$call": "spawn", "args": [":goblin"], "kwargs": {"count": 3}},
        )
    ]


def test_remove_mob_takes_the_mob_and_its_spawn(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load(REFINE, "turn_remove.json")]})
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    turn, proposal = mob_turn(ctx, "remove the goblin", [])
    assert turn.kind == "edit"
    assert proposal is not None
    assert proposal.ops == [
        RemoveDecl(kind="mob", id="goblin"),
        RemoveItem(
            kind="room",
            id="hall",
            name="spawns",
            match=ItemMatch(call="spawn", first_arg=":goblin"),
        ),
    ]


def test_remove_loot_takes_the_entry_from_the_mob(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load(REFINE, "turn_remove_loot.json")]})
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    turn, proposal = mob_turn(ctx, "the goblin drops nothing now", [])
    assert turn.kind == "edit"
    assert proposal is not None
    assert proposal.ops == [
        RemoveItem(
            kind="mob",
            id="goblin",
            name="loot",
            match=ItemMatch(call="loot", first_arg=":goblin_idol"),
        )
    ]


def test_unplace_removes_the_spawn_from_the_room(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load(REFINE, "turn_unplace.json")]})
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    turn, proposal = mob_turn(ctx, "unplace the goblin", [])
    assert turn.kind == "edit"
    assert proposal is not None
    assert proposal.ops == [
        RemoveItem(
            kind="room",
            id="hall",
            name="spawns",
            match=ItemMatch(call="spawn", first_arg=":goblin"),
        )
    ]


def test_rewrite_rewrites_the_prose(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [_load(REFINE, "turn_rewrite.json")],
            "prose": [_load(REFINE, "prose_rewrite.json")],
        }
    )
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    turn, proposal = mob_turn(ctx, "make the shaman more ominous", [])
    assert turn.kind == "edit"
    assert proposal is not None
    assert proposal.ops == [
        SetArg(
            kind="mob",
            id="goblin_shaman",
            name="desc",
            value="A hooded goblin mutters to the spirits, and laughs when they answer.",
        )
    ]
    assert proposal.prose_preview == [
        (
            "goblin_shaman",
            "desc",
            "A hooded goblin mutters to the spirits, and laughs when they answer.",
        )
    ]


def test_an_edit_naming_a_missing_mob_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {"logic": [_load(REFINE, "turn_unknown_mob.json"), _load(REFINE, "turn_tougher.json")]}
    )
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    turn, proposal = mob_turn(ctx, "buff the dragon", [])
    assert turn.kind == "edit"
    assert "there is no mob 'dragon' in this package" in _last_user(1, llm)
    assert proposal is not None
    assert proposal.ops == [
        SetArg(
            kind="mob",
            id="goblin_shaman",
            name="vitals",
            value={"$call": "vitals", "args": [], "kwargs": {"hp": 40}},
        )
    ]


def test_a_request_for_a_room_is_out_of_scope(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm({"logic": [_load(REFINE, "turn_add_room.json")]})
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    turn, proposal = mob_turn(ctx, "add a room", [])
    assert turn.kind == "out_of_scope"
    assert turn.other_mode == "area"
    assert turn.note == "add a room"
    assert proposal is None


def _turn(*edits: dict[str, Any]) -> str:
    return json.dumps({"kind": "edit", "reply": "Done.", "edits": list(edits)})


def _plan_with(**changes: Any) -> str:
    """plan.json with the goblin's fields replaced."""
    plan = json.loads(_load(CREATE, "plan.json"))
    plan["mobs"][0].update(changes)
    return json.dumps(plan)


def test_a_new_loot_id_clashing_with_a_declaration_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    # Labels are package-wide, so an item may not share an id with a room or a mob.
    clash = _plan_with(loot=[{"item": "hall", "chance": 10, "concept": "a totem"}])
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), clash, _load(CREATE, "plan.json")],
            "prose": _prose(),
        }
    )
    ctx, _ = _rooms_ctx(tmp_path, llm, world_min)
    proposal = create_mobs(ctx, "two goblins")
    assert isinstance(proposal, Proposal)
    assert "the new item id 'hall' is already declared in the package" in _last_user(2, llm)
    assert proposal.ops == _expected_ops()


def test_a_new_loot_id_clashing_with_a_planned_mob_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    clash = _plan_with(loot=[{"item": "goblin_shaman", "chance": 10, "concept": "a totem"}])
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), clash, _load(CREATE, "plan.json")],
            "prose": _prose(),
        }
    )
    ctx, _ = _rooms_ctx(tmp_path, llm, world_min)
    create_mobs(ctx, "two goblins")
    assert "the new item id 'goblin_shaman' is also a mob of the plan" in _last_user(2, llm)


def test_two_mobs_sharing_a_new_loot_item_create_it_once(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    plan = json.loads(_load(CREATE, "plan.json"))
    plan["mobs"][1]["loot"] = [{"item": "goblin_idol", "chance": 50, "concept": "a totem"}]
    llm = FakeLlm({"logic": [_load(CREATE, "brief.json"), json.dumps(plan)], "prose": _prose()})
    ctx, _ = _rooms_ctx(tmp_path, llm, world_min)
    proposal = create_mobs(ctx, "two goblins")
    assert isinstance(proposal, Proposal)
    items = [op for op in proposal.ops if isinstance(op, AddDecl) and op.kind == "item"]
    assert [op.id for op in items] == ["goblin_idol"]
    assert proposal.summary == "+2 mobs, +1 item, 1 spawn"
    assert not proposal.notes


def test_a_local_skill_may_be_named_short(tmp_path: Path, world_min: WorldDescription) -> None:
    plan = json.loads(_load(CREATE, "plan.json"))
    plan["mobs"][1]["skills"] = {":fireball": 40}
    llm = FakeLlm({"logic": [_load(CREATE, "brief.json"), json.dumps(plan)], "prose": _prose()})
    ctx, _ = _rooms_ctx(tmp_path, llm, world_min)
    proposal = create_mobs(ctx, "two goblins")
    assert isinstance(proposal, Proposal)
    shaman = next(op for op in proposal.ops if op.id == "goblin_shaman")
    assert isinstance(shaman, AddDecl)
    assert shaman.args["skills"] == {"$dict": [[":fireball", 40]]}


def test_a_local_faction_may_be_named_short(tmp_path: Path, world_min: WorldDescription) -> None:
    world = world_min.model_copy(
        update={
            "factions": ["//zones/tutorial:goblins"],
            "features": [*world_min.features, "standing"],
        }
    )
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), _plan_with(factions=[":goblins"])],
            "prose": _prose(),
        }
    )
    ctx, _ = _rooms_ctx(tmp_path, llm, world)
    proposal = create_mobs(ctx, "two goblins")
    assert isinstance(proposal, Proposal)
    goblin = next(op for op in proposal.ops if op.id == "goblin")
    assert isinstance(goblin, AddDecl)
    assert goblin.args["factions"] == [":goblins"]


def test_balance_references_are_the_mobs_closest_in_level(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    world = world_min.model_copy(deep=True)
    world.mobs = [MobInfo(label="//zones/tutorial:chief", name="the chief", level=12)] + [
        MobInfo(label=f"//zones/other:lv{n:02d}", name=f"mob {n}", level=n) for n in range(1, 16)
    ]
    llm = FakeLlm({"logic": [_load(CREATE, "brief_questions.json")]})
    ctx, _ = _ctx(tmp_path, llm, world)
    create_mobs(ctx, "a goblin")
    prompt = _last_user(0, llm)
    assert "//zones/tutorial:chief:" in prompt
    # Ten others nearest the package's level 12: 6 to 15. Levels 1 to 5 are left out.
    for n in range(6, 16):
        assert f"//zones/other:lv{n:02d}:" in prompt
    for n in range(1, 6):
        assert f"//zones/other:lv{n:02d}:" not in prompt


def test_add_loot_of_an_existing_local_item_needs_no_concept(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _turn({"edit": "add_loot", "id": "goblin_shaman", "loot": {"item": "goblin_idol"}})
            ]
        }
    )
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    _, proposal = mob_turn(ctx, "the shaman carries an idol", [])
    assert proposal is not None
    assert proposal.ops == [
        AppendItem(
            kind="mob",
            id="goblin_shaman",
            name="loot",
            value={"$call": "loot", "args": [":goblin_idol"], "kwargs": {}},
        )
    ]


def test_add_loot_with_a_concept_for_an_existing_item_does_not_redeclare_it(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    loot = {"item": "goblin_idol", "concept": "a totem"}
    llm = FakeLlm({"logic": [_turn({"edit": "add_loot", "id": "goblin_shaman", "loot": loot})]})
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    _, proposal = mob_turn(ctx, "the shaman carries an idol", [])
    assert proposal is not None
    assert [op.op for op in proposal.ops] == ["append_item"]


def test_add_mob_clashing_with_a_room_id_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    clash = _turn({"edit": "add_mob", "mob": {"id": "hall", "concept": "a hall ghost"}})
    llm = FakeLlm({"logic": [clash, _load(REFINE, "turn_tougher.json")]})
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    mob_turn(ctx, "add a ghost", [])
    assert "the id 'hall' is already declared in the package" in _last_user(1, llm)


def test_set_mob_with_an_unknown_vital_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    bad = _turn(
        {
            "edit": "set_mob",
            "id": "goblin_shaman",
            "fields": {"vitals": {"$call": "vitals", "args": [], "kwargs": {"grit": 40}}},
        }
    )
    llm = FakeLlm({"logic": [bad, _load(REFINE, "turn_tougher.json")]})
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    _, proposal = mob_turn(ctx, "make the shaman tougher", [])
    assert "'grit' is not a vital of this world's schema" in _last_user(1, llm)
    assert proposal is not None


def test_set_mob_on_a_field_computed_by_code_is_noted(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    # The writer refuses it with its file and line; the turn is not sent back for repair.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    for name in ("rooms.star", "items.star", "mobs.star"):
        (pkg / name).write_text((GOBLINS / name).read_text())
    mobs = pkg / "mobs.star"
    mobs.write_text(
        mobs.read_text().replace("vitals = vitals(hp = 20, mana = 30)", "vitals = SHAMAN_VITALS")
    )
    files = {p.name: p.read_text() for p in pkg.glob("*.star")}
    llm = FakeLlm({"logic": [_load(REFINE, "turn_tougher.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_min, files=files, view=read_package(pkg, PACKAGE))
    _, proposal = mob_turn(ctx, "make the shaman tougher", [])
    assert len(llm.calls) == 1
    assert proposal is not None
    assert any(
        "mob 'goblin_shaman': vitals is computed by code (mobs.star:" in note
        for note in proposal.notes
    )


def test_unplace_of_a_mob_the_room_does_not_spawn_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    bad = _turn({"edit": "unplace", "room": "gate", "mob": "goblin"})
    llm = FakeLlm({"logic": [bad, _load(REFINE, "turn_unplace.json")]})
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    _, proposal = mob_turn(ctx, "unplace the goblin", [])
    assert "the room 'gate' does not spawn 'goblin'" in _last_user(1, llm)
    assert proposal is not None


def test_remove_loot_the_mob_does_not_drop_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    bad = _turn({"edit": "remove_loot", "id": "goblin_shaman", "item": "goblin_idol"})
    llm = FakeLlm({"logic": [bad, _load(REFINE, "turn_remove_loot.json")]})
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    _, proposal = mob_turn(ctx, "the goblin drops nothing now", [])
    assert "the mob 'goblin_shaman' does not drop 'goblin_idol'" in _last_user(1, llm)
    assert proposal is not None


def test_remove_loot_matches_the_label_as_the_file_writes_it(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _turn(
                    {"edit": "remove_loot", "id": "goblin", "item": "//zones/tutorial:goblin_idol"}
                )
            ]
        }
    )
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    _, proposal = mob_turn(ctx, "the goblin drops nothing now", [])
    assert proposal is not None
    assert proposal.ops == [
        RemoveItem(
            kind="mob",
            id="goblin",
            name="loot",
            match=ItemMatch(call="loot", first_arg=":goblin_idol"),
        )
    ]


def test_place_writes_a_local_mob_short(tmp_path: Path, world_min: WorldDescription) -> None:
    place = {"edit": "place", "spawn": {"room": "gate"}, "mob": "//zones/tutorial:goblin"}
    llm = FakeLlm({"logic": [_turn(place)]})
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    _, proposal = mob_turn(ctx, "put a goblin at the gate", [])
    assert proposal is not None
    assert proposal.ops == [
        AppendItem(
            kind="room",
            id="gate",
            name="spawns",
            value={"$call": "spawn", "args": [":goblin"], "kwargs": {"count": 1}},
        )
    ]


# -- the generated test's mob blocks ----------------------------------------


def _world(zone: str) -> WorldDescription:
    return WorldDescription(
        format=1,
        api_version=1,
        engine_version="t",
        schema=Schema(unarmed_damage_type="bludgeon"),
        packages=[PackageInfo(path=zone, kind="zone", ok=True)],
    )


def _decl(kind: str, rid: str, args: dict[str, Value], line: int) -> DeclView:
    return DeclView(
        kind=kind, id=rid, file=f"{kind}s.star", line=line, args=args, extra_positional=[]
    )


def _header() -> str:
    return (
        "# Generated by gups-builder from this package's rooms and mobs; "
        "it is rewritten on every write.\n"
        "# Put your own tests in another *_test.star file.\n"
        "\n"
        "def test_builder_package_loads(t):\n"
        "    t.world(seed = 1)\n"
    )


def test_a_mob_with_declared_vitals_gets_a_vitals_test() -> None:
    hall = _decl("room", "hall", {}, 1)
    goblin = _decl(
        "mob",
        "goblin",
        {
            "name": "a goblin",
            "vitals": {"$call": "vitals", "args": [], "kwargs": {"hp": 30, "mana": 10}},
        },
        1,
    )
    view = PackageView(path="zones/mansion", files={}, decls=[hall, goblin], opaque=[])
    assert builder_test("zones/mansion", _world("zones/mansion"), view) == (
        _header() + "\n"
        "def test_builder_goblin_vitals(t):\n"
        "    w = t.world(seed = 1)\n"
        '    m = w.spawn(":goblin", at = ":hall")\n'
        "    t.eq(m.max_hp, 30)\n"
        "    t.eq(m.max_mana, 10)\n"
    )


def test_a_mob_with_code_gets_no_vitals_test() -> None:
    hall = _decl("room", "hall", {}, 1)
    hooked = _decl(
        "mob",
        "hooked",
        {
            "name": "a hooked mob",
            "vitals": {"$call": "vitals", "args": [], "kwargs": {"hp": 30}},
            "on_damage": {"$ref": "_hit"},
        },
        1,
    )
    plain = _decl(
        "mob",
        "plain",
        {"name": "a plain mob", "vitals": {"$call": "vitals", "args": [], "kwargs": {"hp": 5}}},
        2,
    )
    view = PackageView(path="zones/mansion", files={}, decls=[hall, hooked, plain], opaque=[])
    text = builder_test("zones/mansion", _world("zones/mansion"), view)
    assert "hooked" not in text
    assert '    m = w.spawn(":plain", at = ":hall")\n' in text


def test_a_mob_without_declared_vitals_gets_no_vitals_test() -> None:
    hall = _decl("room", "hall", {}, 1)
    bare = _decl("mob", "bare", {"name": "a bare mob"}, 1)
    view = PackageView(path="zones/mansion", files={}, decls=[hall, bare], opaque=[])
    assert builder_test("zones/mansion", _world("zones/mansion"), view) == _header()


def test_a_package_without_rooms_gives_the_mob_no_vitals_test() -> None:
    goblin = _decl(
        "mob",
        "goblin",
        {"name": "a goblin", "vitals": {"$call": "vitals", "args": [], "kwargs": {"hp": 30}}},
        1,
    )
    view = PackageView(path="zones/mansion", files={}, decls=[goblin], opaque=[])
    assert builder_test("zones/mansion", _world("zones/mansion"), view) == _header()


# -- the real engine ---------------------------------------------------------


@pytest.mark.engine
def test_the_scripted_goblins_pass_the_real_engine(
    tmp_path: Path, gups_bin: Path, template_world: Path
) -> None:
    gups = Gups(gups_bin, template_world)
    world = gups.describe_world()
    api = gups.api()
    pkg = template_world / "zones" / "goblin_caves"
    pkg.mkdir(parents=True)
    (pkg / "package.star").write_text(
        'package(api_version = 1, kind = "zone", title = "The Goblin Caves", '
        "reset_world_ticks = 10)\n"
    )
    (pkg / "rooms.star").write_text(
        'room("cave",\n'
        '    name = "The Cave",\n'
        '    desc = "Drip, drip.",\n'
        '    exits = [exit(DIR.east, to = ":den")],\n'
        ")\n"
        "\n"
        'room("den",\n'
        '    name = "The Den",\n'
        '    desc = "Bones and ash.",\n'
        '    exits = [exit(DIR.west, to = ":cave")],\n'
        ")\n"
    )
    view = read_package(pkg, "zones/goblin_caves")
    files = {"rooms.star": (pkg / "rooms.star").read_text()}
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief_engine.json"), _load(CREATE, "plan_engine.json")],
            "prose": [_load(CREATE, "prose_goblin.json"), _load(CREATE, "prose_shaman.json")],
        }
    )
    ctx = Context(
        world_dir=template_world,
        world=world,
        api=api,
        package="zones/goblin_caves",
        view=view,
        files=files,
        llm=llm,
        config=Config(max_repairs=3),
        engine=gups,
    )
    proposal = create_mobs(ctx, "two goblins in the caves")
    assert isinstance(proposal, Proposal)
    assert not any(note.startswith("NOT VALID:") for note in proposal.notes)
    assert not any(note.startswith("BUILDER BUG:") for note in proposal.notes)
    # Stage the proposal into the template world and run the engine on it.
    result = apply_ops(files, view, proposal.ops)
    for name, text in {**files, **result.files, **proposal.new_files}.items():
        (pkg / name).write_text(text)
    view_after = read_package(pkg, "zones/goblin_caves")
    (pkg / "builder_test.star").write_text(builder_test("zones/goblin_caves", world, view_after))
    findings = gups.lint("zones/goblin_caves")
    assert not [f for f in findings if f.severity == "error"]
    assert gups.test("zones/goblin_caves").ok


def test_the_fixture_mobs_are_plain_and_editable() -> None:
    view = read_package(GOBLINS, PACKAGE)
    assert {d.id for d in view.decls if d.kind == "mob"} == {"goblin", "goblin_shaman"}
    assert view.opaque == []
    for decl in view.decls:
        assert all(is_editable(value) for value in decl.args.values())
    assert view.get("room", "hall") is not None
