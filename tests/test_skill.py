"""The skill mode: creating a skill with a declarative effect, and refining it.

The creation tests script the brief, plan and prose calls with an empty view,
as area creation does for a new package, and compare the produced ops against
the declaration the engine would hold. The refinement tests start from the
arcane fixture, the package as the arcane school left it, and drive skill_turn
with scripted replies. The engine test stages a scripted skill into the
template world's skills/arcane package and runs the real linter on it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from gups_builder.config import Config
from gups_builder.engine.api import ApiModel
from gups_builder.engine.gups import Gups, TestRun
from gups_builder.engine.lint import LintFinding
from gups_builder.engine.world import PluginSummary, WorldDescription
from gups_builder.llm.fake import FakeLlm
from gups_builder.modes.common import Context, Proposal
from gups_builder.modes.skill import SkillBrief, create_skill, skill_turn
from gups_builder.star.ops import AddDecl, RemoveDecl, SetArg
from gups_builder.star.reader import PackageView, read_package
from gups_builder.star.render import render_decl
from gups_builder.star.writer import apply_ops

CREATE = Path(__file__).resolve().parent / "fixtures" / "llm" / "skill_create"
REFINE = Path(__file__).resolve().parent / "fixtures" / "llm" / "skill_refine"
ARCANE = Path(__file__).resolve().parent / "fixtures" / "arcane"
GOBLINS = Path(__file__).resolve().parent / "fixtures" / "goblins"
REPO = Path(__file__).resolve().parent.parent
PACKAGE = "skills/fire"
PACKAGE_ARCANE = "skills/arcane"
PACKAGE_TUTORIAL = "zones/tutorial"


class FakeEngine:
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


def _load(base: Path, name: str) -> str:
    return (base / name).read_text()


def _api() -> ApiModel:
    return ApiModel.model_validate_json((REPO / "tests" / "fixtures" / "api_min.json").read_text())


def _ctx(
    tmp_path: Path,
    llm: FakeLlm,
    world: WorldDescription,
    files: dict[str, str] | None = None,
    view: PackageView | None = None,
    package: str = PACKAGE,
    lint: list[list[LintFinding]] | None = None,
) -> tuple[Context, FakeEngine]:
    api = _api()
    engine = FakeEngine(world, api, lint)
    ctx = Context(
        world_dir=tmp_path,
        world=world,
        api=api,
        package=package,
        view=view or PackageView(path=package, files={}, decls=[], opaque=[]),
        files=dict(files or {}),
        llm=llm,
        config=Config(max_repairs=1),
        engine=engine,
    )
    return ctx, engine


def _arcane_ctx(
    tmp_path: Path, llm: FakeLlm, world: WorldDescription
) -> tuple[Context, FakeEngine]:
    files = {"spells.star": (ARCANE / "spells.star").read_text()}
    return _ctx(
        tmp_path,
        llm,
        world,
        files=files,
        view=read_package(ARCANE, PACKAGE_ARCANE),
        package=PACKAGE_ARCANE,
    )


def _goblins_ctx(
    tmp_path: Path, llm: FakeLlm, world: WorldDescription
) -> tuple[Context, FakeEngine]:
    files = {
        name: (GOBLINS / name).read_text() for name in ("rooms.star", "items.star", "mobs.star")
    }
    return _ctx(
        tmp_path,
        llm,
        world,
        files=files,
        view=read_package(GOBLINS, PACKAGE_TUTORIAL),
        package=PACKAGE_TUTORIAL,
    )


def _world_no_track(world: WorldDescription) -> WorldDescription:
    return world.model_copy(update={"features": [f for f in world.features if f != "track"]})


def _world_with_plugin(world: WorldDescription) -> WorldDescription:
    return world.model_copy(update={"plugins": [PluginSummary(id="combust", effects=["ember"])]})


def _plan_with(**changes: Any) -> str:
    """plan.json with the given fields replaced."""
    plan = json.loads(_load(CREATE, "plan.json"))
    plan.update(changes)
    return json.dumps(plan)


def _turn(*edits: dict[str, Any]) -> str:
    return json.dumps({"kind": "edit", "reply": "Done.", "edits": list(edits)})


def _last_user(call_index: int, llm: FakeLlm) -> str:
    return [m.content for m in llm.calls[call_index][1] if m.role == "user"][-1]


# -- creation ---------------------------------------------------------------


def test_a_brief_with_questions_is_returned_as_is(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm({"logic": [_load(CREATE, "brief_questions.json")]})
    ctx, _ = _ctx(tmp_path, llm, world_min)
    result = create_skill(ctx, "a fire spell")
    assert isinstance(result, SkillBrief)
    assert result.questions == ["How much mana should it cost?"]
    assert len(llm.calls) == 1


def test_creation_produces_the_expected_ops(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), _load(CREATE, "plan.json")],
            "prose": [_load(CREATE, "prose.json")],
        }
    )
    ctx, engine = _ctx(tmp_path, llm, world_min)
    proposal = create_skill(ctx, "a fire spell like spark")
    assert isinstance(proposal, Proposal)
    [op] = proposal.ops
    assert op == AddDecl(
        file="skills.star",
        kind="skill",
        id="spark",
        args={
            "kind": "spell",
            "cost": {"$call": "vitals", "args": [], "kwargs": {"mana": 8}},
            "lag": 4,
            "range": "near",
            "target": "enemy",
            "channels": ["spell", "sound"],
            "tags": ["blue"],
            "unrecognized": {
                "$dict": [
                    ["noun", "spark"],
                    ["begin", "{actor}'s fingertips crackle with heat."],
                    ["victim", "{actor}'s spark sears you!"],
                ]
            },
            "effect": {
                "$call": "damage",
                "args": [],
                "kwargs": {"dice": "2d4", "type": {"$const": "DAMAGE.fire"}},
            },
        },
    )
    assert proposal.new_files == {
        "package.star": 'package(api_version = 1, kind = "skills", '
        'title = "a fire spell that lashes a single enemy")\n'
    }
    assert proposal.summary == "+1 skill"
    assert not proposal.notes
    assert engine.test_calls == 0
    plan_prompt = "\n".join(m.content for m in llm.calls[1][1])
    assert "damage" in plan_prompt
    assert "knockback" in plan_prompt
    assert "melee, near, far, self, room" in plan_prompt
    # The world's one skill is a balance reference.
    assert "//zones/tutorial:fireball" in plan_prompt


def test_the_new_skill_renders_like_spark(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), _load(CREATE, "plan.json")],
            "prose": [_load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_skill(ctx, "a fire spell like spark")
    assert isinstance(proposal, Proposal)
    [op] = proposal.ops
    assert isinstance(op, AddDecl)
    assert render_decl("skill", "spark", op.args, None) == (
        'skill("spark",\n'
        '    kind = "spell",\n'
        "    cost = vitals(mana = 8),\n"
        "    lag = 4,\n"
        '    range = "near",\n'
        '    target = "enemy",\n'
        '    channels = ["spell", "sound"],\n'
        '    tags = ["blue"],\n'
        '    unrecognized = {"noun": "spark", "begin": "{actor}\'s fingertips crackle with heat.", '
        '"victim": "{actor}\'s spark sears you!"},\n'
        '    effect = damage(dice = "2d4", type = DAMAGE.fire),\n'
        ")\n"
    )
    assert proposal.prose_preview == [
        ("spark", "unrecognized.noun", "spark"),
        ("spark", "unrecognized.begin", "{actor}'s fingertips crackle with heat."),
        ("spark", "unrecognized.victim", "{actor}'s spark sears you!"),
    ]


def test_a_maneuver_with_melee_and_knockback(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), _load(CREATE, "plan_maneuver.json")],
            "prose": [_load(CREATE, "prose_maneuver.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_skill(ctx, "a shove maneuver")
    assert isinstance(proposal, Proposal)
    [op] = proposal.ops
    assert isinstance(op, AddDecl)
    assert op.id == "strike"
    assert op.args["range"] == "melee"
    assert op.args["effect"] == {"$call": "knockback", "args": [], "kwargs": {"steps": 2}}
    assert "cost" not in op.args
    assert op.args["unrecognized"] == {"$dict": [["victim", "{target} is shoved away!"]]}


def test_a_sequence_effect_is_nested(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), _load(CREATE, "plan_sequence.json")],
            "prose": [_load(CREATE, "prose_maneuver.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_skill(ctx, "a burn-then-stun spell")
    assert isinstance(proposal, Proposal)
    [op] = proposal.ops
    assert isinstance(op, AddDecl)
    assert op.args["effect"] == {
        "$call": "sequence",
        "args": [
            {
                "$call": "damage",
                "args": [],
                "kwargs": {"dice": "1d6", "type": {"$const": "DAMAGE.fire"}},
            },
            {"$call": "stun", "args": [], "kwargs": {"pulses": 2}},
        ],
        "kwargs": {},
    }
    assert op.args["warmup"] == 2


def test_teach_to_an_existing_mob_merges_the_skills(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), _load(CREATE, "plan_teach.json")],
            "prose": [_load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    proposal = create_skill(ctx, "a fire spell the shaman knows")
    assert isinstance(proposal, Proposal)
    assert proposal.summary == "+1 skill, 1 mob taught"
    [declare, teach] = proposal.ops
    assert declare.kind == "skill"
    assert teach == SetArg(
        kind="mob",
        id="goblin_shaman",
        name="skills",
        value={"$dict": [["//zones/tutorial:fireball", 40], [":spark", 30]]},
    )
    assert not proposal.notes


# -- the plan's extra check ---------------------------------------------------


def test_range_near_is_rejected_when_track_is_off(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    world = _world_no_track(world_min)
    good = _plan_with(
        range="self", effect={"primitive": "heal", "args": {"dice": "1d4", "vital": "hp"}}
    )
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _load(CREATE, "plan.json"),
                good,
            ],
            "prose": [_load(CREATE, "prose.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world)
    create_skill(ctx, "a fire spell")
    assert "the world has the track feature off" in _last_user(2, llm)


def test_a_range_not_in_the_api_is_repaired(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(range="long"),
                _load(CREATE, "plan.json"),
            ],
            "prose": [_load(CREATE, "prose.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_skill(ctx, "a fire spell")
    assert "the range 'long' is not an allowed range" in _last_user(2, llm)


def test_a_target_not_in_the_api_is_repaired(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(target="both"),
                _load(CREATE, "plan.json"),
            ],
            "prose": [_load(CREATE, "prose.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_skill(ctx, "a fire spell")
    assert "the target 'both' is not an allowed target" in _last_user(2, llm)


def test_a_cost_vital_not_in_the_schema_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(cost={"stamina": 10}),
                _load(CREATE, "plan.json"),
            ],
            "prose": [_load(CREATE, "prose.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_skill(ctx, "a fire spell")
    assert "'stamina' is not a vital of this world's schema" in _last_user(2, llm)


def test_an_unknown_effect_primitive_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(effect={"primitive": "flame", "args": {}}),
                _load(CREATE, "plan.json"),
            ],
            "prose": [_load(CREATE, "prose.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_skill(ctx, "a fire spell")
    assert "the effect 'flame' is not an effect of this world" in _last_user(2, llm)


def test_an_unknown_effect_argument_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(effect={"primitive": "damage", "args": {"pow": 5}}),
                _load(CREATE, "plan.json"),
            ],
            "prose": [_load(CREATE, "prose.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_skill(ctx, "a fire spell")
    assert "the effect 'damage' has no argument 'pow'" in _last_user(2, llm)


def test_a_sequence_without_effects_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(effect={"primitive": "sequence", "args": {}, "effects": []}),
                _load(CREATE, "plan.json"),
            ],
            "prose": [_load(CREATE, "prose.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_skill(ctx, "a fire spell")
    assert "the 'sequence' effect needs at least one effect" in _last_user(2, llm)


def test_effects_on_a_non_sequence_is_repaired(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(
                    effect={
                        "primitive": "damage",
                        "args": {"dice": "2d4", "type": "fire"},
                        "effects": [{"primitive": "stun", "args": {"pulses": 2}}],
                    }
                ),
                _load(CREATE, "plan.json"),
            ],
            "prose": [_load(CREATE, "prose.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_skill(ctx, "a fire spell")
    assert "only sequence combines effects" in _last_user(2, llm)


def test_knockback_is_rejected_when_track_is_off(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    world = _world_no_track(world_min)
    bad = _plan_with(range="self", effect={"primitive": "knockback", "args": {"steps": 2}})
    good = _plan_with(
        range="self", effect={"primitive": "heal", "args": {"dice": "1d4", "vital": "hp"}}
    )
    llm = FakeLlm(
        {"logic": [_load(CREATE, "brief.json"), bad, good], "prose": [_load(CREATE, "prose.json")]}
    )
    ctx, _ = _ctx(tmp_path, llm, world)
    create_skill(ctx, "a shove")
    assert "the effect 'knockback' needs the track feature" in _last_user(2, llm)


def test_a_room_skill_with_damage_is_repaired(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(target="room"),
                _plan_with(target="room", effect={"primitive": "dispel", "args": {"all": True}}),
            ],
            "prose": [_load(CREATE, "prose.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_skill(ctx, "a room-cleansing spell")
    assert "a room skill's effect may use only" in _last_user(2, llm)


def test_an_item_skill_with_heal_is_repaired(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(
                    target="item",
                    effect={"primitive": "heal", "args": {"dice": "1d4", "vital": "hp"}},
                ),
                _plan_with(
                    target="item",
                    effect={
                        "primitive": "apply_affect",
                        "args": {"name": "mended", "duration": 10},
                    },
                ),
            ],
            "prose": [_load(CREATE, "prose.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_skill(ctx, "an item-mending spell")
    assert "an item skill's effect may use only" in _last_user(2, llm)


def test_a_damage_type_not_in_the_schema_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(effect={"primitive": "damage", "args": {"dice": "2d4", "type": "cold"}}),
                _load(CREATE, "plan.json"),
            ],
            "prose": [_load(CREATE, "prose.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_skill(ctx, "a cold spell")
    assert "'cold' is not a damage type of this world's schema" in _last_user(2, llm)


def test_a_teach_of_an_unknown_mob_is_repaired(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(teach=[{"mob": "goblin", "proficiency": 30}]),
                _load(CREATE, "plan.json"),
            ],
            "prose": [_load(CREATE, "prose.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_skill(ctx, "a fire spell")
    assert "the teach names the unknown mob 'goblin'" in _last_user(2, llm)


def test_a_teach_of_a_mob_in_another_package_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(teach=[{"mob": "//zones/other:chief", "proficiency": 30}]),
                _load(CREATE, "plan.json"),
            ],
            "prose": [_load(CREATE, "prose.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    create_skill(ctx, "a fire spell")
    assert "is not a mob of this package" in _last_user(2, llm)


def test_a_teach_with_bad_proficiency_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(teach=[{"mob": "goblin_shaman", "proficiency": 120}]),
                _load(CREATE, "plan_teach.json"),
            ],
            "prose": [_load(CREATE, "prose.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    create_skill(ctx, "a fire spell the shaman knows")
    assert "the proficiency 120" in _last_user(2, llm)


def test_a_plugin_effect_is_accepted(tmp_path: Path, world_min: WorldDescription) -> None:
    world = _world_with_plugin(world_min)
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(
                    target="room",
                    effect={"primitive": "combust.ember", "args": {"radius": 2}},
                ),
            ],
            "prose": [_load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world)
    proposal = create_skill(ctx, "a room-embering spell")
    assert isinstance(proposal, Proposal)
    [op] = proposal.ops
    assert isinstance(op, AddDecl)
    assert op.args["effect"] == {"$call": "combust.ember", "args": [], "kwargs": {"radius": 2}}
    assert not proposal.notes


def test_an_unknown_plugin_effect_is_repaired(tmp_path: Path, world_min: WorldDescription) -> None:
    world = _world_with_plugin(world_min)
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(effect={"primitive": "nope.ember", "args": {}}),
                _load(CREATE, "plan.json"),
            ],
            "prose": [_load(CREATE, "prose.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world)
    create_skill(ctx, "a fire spell")
    assert "the effect 'nope.ember' is not an effect of this world" in _last_user(2, llm)


# -- the prose's extra check ---------------------------------------------------


def test_an_effect_argument_that_is_not_a_plain_value_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    # The writer would refuse the whole declaration, so the plan is repaired first.
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(
                    effect={
                        "primitive": "damage",
                        "args": {"dice": {"$ref": "_spark_dice"}, "type": "fire"},
                    }
                ),
                _load(CREATE, "plan.json"),
            ],
            "prose": [_load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_skill(ctx, "a fire spell")
    assert isinstance(proposal, Proposal)
    assert "the effect 'damage' argument 'dice' must be a plain value" in _last_user(2, llm)


def test_an_interrupt_that_is_not_a_plain_value_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(interrupt={"stun": {"$opaque": "_stuns()"}}),
                _load(CREATE, "plan.json"),
            ],
            "prose": [_load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_skill(ctx, "a fire spell")
    assert isinstance(proposal, Proposal)
    assert "the interrupt entry 'stun' must be a plain value" in _last_user(2, llm)


def test_the_plan_prompt_leaves_out_track_ranges_when_track_is_off(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    plan = _plan_with(
        range="self", effect={"primitive": "heal", "args": {"dice": "1d4", "vital": "hp"}}
    )
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), plan],
            "prose": [_load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, _world_no_track(world_min))
    create_skill(ctx, "a fire spell")
    assert "Allowed range values: self, room" in _last_user(1, llm)


def test_a_multiline_prose_line_is_repaired(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), _load(CREATE, "plan.json")],
            "prose": [_load(CREATE, "prose_multiline.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_skill(ctx, "a fire spell")
    assert isinstance(proposal, Proposal)
    assert "the unrecognized line 'begin' must be one line" in _last_user(3, llm)


def test_an_empty_prose_line_is_repaired(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), _load(CREATE, "plan.json")],
            "prose": [_load(CREATE, "prose_empty.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_skill(ctx, "a fire spell")
    assert isinstance(proposal, Proposal)
    assert "the unrecognized line 'begin' is empty" in _last_user(3, llm)


def test_an_unknown_placeholder_is_repaired(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), _load(CREATE, "plan.json")],
            "prose": [_load(CREATE, "prose_unknown_placeholder.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_skill(ctx, "a fire spell")
    assert isinstance(proposal, Proposal)
    assert "unknown placeholder" in _last_user(3, llm)


def test_a_target_placeholder_on_a_self_skill_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    plan = _plan_with(
        target="self",
        range="self",
        effect={"primitive": "heal", "args": {"dice": "1d4", "vital": "hp"}},
    )
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), plan],
            "prose": [_load(CREATE, "prose_target_self.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_skill(ctx, "a healing spell")
    assert isinstance(proposal, Proposal)
    assert "uses {target}" in _last_user(3, llm)
    assert "the skill's target is 'self'" in _last_user(3, llm)


def test_a_resisted_line_on_a_non_enemy_skill_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    plan = _plan_with(
        target="ally", effect={"primitive": "heal", "args": {"dice": "1d4", "vital": "hp"}}
    )
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), plan],
            "prose": [_load(CREATE, "prose_resisted_ally.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_skill(ctx, "a healing spell")
    assert isinstance(proposal, Proposal)
    assert "only an enemy skill may have resisted lines" in _last_user(3, llm)


def test_a_room_range_line_with_target_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    plan = _plan_with(
        target="ally",
        range="room",
        effect={"primitive": "heal", "args": {"dice": "1d4", "vital": "hp"}},
    )
    llm = FakeLlm(
        {
            "logic": [_load(CREATE, "brief.json"), plan],
            "prose": [_load(CREATE, "prose_room_range_target.json"), _load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _ctx(tmp_path, llm, world_min)
    proposal = create_skill(ctx, "a room-warming spell")
    assert isinstance(proposal, Proposal)
    assert "room-range skill has no single target" in _last_user(3, llm)


# -- refinement ----------------------------------------------------------------


def test_set_effect_changes_only_the_effect(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm({"logic": [_load(REFINE, "turn_set_effect.json")]})
    ctx, _ = _arcane_ctx(tmp_path, llm, world_min)
    turn, proposal = skill_turn(ctx, "make the spark hit harder", [])
    assert turn.kind == "edit"
    assert proposal is not None
    assert proposal.ops == [
        SetArg(
            kind="skill",
            id="spark",
            name="effect",
            value={
                "$call": "damage",
                "args": [],
                "kwargs": {"dice": "3d4", "type": {"$const": "DAMAGE.fire"}},
            },
        )
    ]


def test_remove_skill_removes_the_declaration(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm({"logic": [_load(REFINE, "turn_remove.json")]})
    ctx, _ = _arcane_ctx(tmp_path, llm, world_min)
    turn, proposal = skill_turn(ctx, "remove mend", [])
    assert turn.kind == "edit"
    assert proposal is not None
    assert proposal.ops == [RemoveDecl(kind="skill", id="mend")]


def test_set_skill_changes_named_fields(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm({"logic": [_load(REFINE, "turn_set_skill.json")]})
    ctx, _ = _arcane_ctx(tmp_path, llm, world_min)
    turn, proposal = skill_turn(ctx, "the spark costs more mana", [])
    assert turn.kind == "edit"
    assert proposal is not None
    assert proposal.ops == [
        SetArg(
            kind="skill",
            id="spark",
            name="cost",
            value={"$call": "vitals", "args": [], "kwargs": {"mana": 10}},
        )
    ]


def test_rewrite_rewrites_the_unrecognized_lines(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [_load(REFINE, "turn_rewrite.json")],
            "prose": [_load(REFINE, "prose_rewrite.json")],
        }
    )
    ctx, _ = _arcane_ctx(tmp_path, llm, world_min)
    turn, proposal = skill_turn(ctx, "make the begin line creepier", [])
    assert turn.kind == "edit"
    assert proposal is not None
    assert proposal.ops == [
        SetArg(
            kind="skill",
            id="spark",
            name="unrecognized",
            value={
                "$dict": [
                    ["noun", "spark"],
                    [
                        "begin",
                        "Heat gathers at {actor}'s fingertips, and the air smells of burning.",
                    ],
                    ["victim", "{actor}'s spark sears you!"],
                ]
            },
        )
    ]
    assert proposal.prose_preview == [
        ("spark", "unrecognized.noun", "spark"),
        (
            "spark",
            "unrecognized.begin",
            "Heat gathers at {actor}'s fingertips, and the air smells of burning.",
        ),
        ("spark", "unrecognized.victim", "{actor}'s spark sears you!"),
    ]


def test_teach_in_a_turn_adds_to_the_mob_skills(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {"logic": [_load(REFINE, "turn_teach.json")], "prose": [_load(CREATE, "prose.json")]}
    )
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    turn, proposal = skill_turn(ctx, "teach the goblin a spark", [])
    assert turn.kind == "edit"
    assert proposal is not None
    [declare, teach] = proposal.ops
    assert declare.kind == "skill"
    assert declare.id == "spark"
    assert teach == SetArg(
        kind="mob",
        id="goblin",
        name="skills",
        value={"$dict": [[":spark", 30]]},
    )
    assert not proposal.notes


def test_an_edit_naming_a_missing_skill_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {"logic": [_load(REFINE, "turn_unknown_skill.json"), _load(REFINE, "turn_set_effect.json")]}
    )
    ctx, _ = _arcane_ctx(tmp_path, llm, world_min)
    turn, proposal = skill_turn(ctx, "make the zap hit harder", [])
    assert turn.kind == "edit"
    assert "there is no skill 'zap' in this package" in _last_user(1, llm)
    assert proposal is not None
    assert proposal.ops[0].id == "spark"


def test_set_skill_with_an_unknown_field_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _turn({"edit": "set_skill", "id": "spark", "fields": {"damage": 5}}),
                _load(REFINE, "turn_set_skill.json"),
            ]
        }
    )
    ctx, _ = _arcane_ctx(tmp_path, llm, world_min)
    _, proposal = skill_turn(ctx, "buff the spark", [])
    assert "'damage' is not a field of the skill declaration" in _last_user(1, llm)
    assert proposal is not None


def test_set_skill_with_a_bad_cost_shape_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {"logic": [_load(REFINE, "turn_bad_cost_shape.json"), _load(REFINE, "turn_set_skill.json")]}
    )
    ctx, _ = _arcane_ctx(tmp_path, llm, world_min)
    _, proposal = skill_turn(ctx, "change the spark's cost", [])
    assert "the skill field 'cost' must be written as vitals(" in _last_user(1, llm)
    assert proposal is not None


def test_set_effect_with_an_unknown_primitive_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {"logic": [_load(REFINE, "turn_bad_primitive.json"), _load(REFINE, "turn_set_effect.json")]}
    )
    ctx, _ = _arcane_ctx(tmp_path, llm, world_min)
    _, proposal = skill_turn(ctx, "change the zap", [])
    assert "the effect 'flame' is not an effect of this world" in _last_user(1, llm)
    assert proposal is not None


def test_a_teach_of_an_unknown_mob_in_a_turn_is_repaired(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    good = _turn({"edit": "teach", "id": "spark", "mob": "goblin", "proficiency": 30})
    llm = FakeLlm(
        {
            "logic": [
                _turn({"edit": "teach", "id": "spark", "mob": "ghost", "proficiency": 30}),
                good,
            ],
            "prose": [_load(CREATE, "prose.json")],
        }
    )
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    _, proposal = skill_turn(ctx, "teach the ghost a spark", [])
    assert "the teach names the unknown mob 'ghost'" in _last_user(1, llm)
    assert proposal is not None


def test_teaching_a_known_skill_again_replaces_its_proficiency(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    # A Starlark dict literal may not repeat a key, so the entry is updated in place.
    llm = FakeLlm(
        {
            "logic": [
                _turn(
                    {
                        "edit": "teach",
                        "id": ":fireball",
                        "mob": "goblin_shaman",
                        "proficiency": 60,
                    }
                )
            ]
        }
    )
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    _, proposal = skill_turn(ctx, "the shaman gets better at fireball", [])
    assert proposal is not None
    assert proposal.ops == [
        SetArg(
            kind="mob",
            id="goblin_shaman",
            name="skills",
            value={"$dict": [["//zones/tutorial:fireball", 60]]},
        )
    ]


def test_a_teach_of_another_packages_skill_keeps_its_label(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _turn(
                    {
                        "edit": "teach",
                        "id": "//skills/arcane:spark",
                        "mob": "goblin",
                        "proficiency": 30,
                    }
                )
            ]
        }
    )
    ctx, _ = _goblins_ctx(tmp_path, llm, world_min)
    _, proposal = skill_turn(ctx, "teach the goblin the arcane spark", [])
    assert proposal is not None
    assert proposal.ops == [
        SetArg(
            kind="mob",
            id="goblin",
            name="skills",
            value={"$dict": [["//skills/arcane:spark", 30]]},
        )
    ]


def test_set_skill_range_near_is_repaired_when_track_is_off(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    llm = FakeLlm(
        {
            "logic": [
                _turn({"edit": "set_skill", "id": "spark", "fields": {"range": "far"}}),
                _turn({"edit": "set_skill", "id": "spark", "fields": {"lag": 6}}),
            ]
        }
    )
    ctx, _ = _arcane_ctx(tmp_path, llm, _world_no_track(world_min))
    _, proposal = skill_turn(ctx, "let the spark reach further", [])
    assert "the world has the track feature off; the range 'far' needs it" in _last_user(1, llm)
    assert proposal is not None


def test_a_rewrite_of_a_room_range_skill_rejects_target_in_the_room_line(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "spells.star").write_text(
        'skill("quake",\n'
        '    kind = "spell",\n'
        "    lag = 4,\n"
        '    range = "room",\n'
        '    target = "enemy",\n'
        '    effect = damage(dice = "1d6"),\n'
        ")\n"
    )
    files = {"spells.star": (pkg / "spells.star").read_text()}
    bad = json.dumps({"room": "The ground heaves under {target}."})
    good = json.dumps({"room": "The ground heaves."})
    llm = FakeLlm(
        {
            "logic": [_turn({"edit": "rewrite", "ids": ["quake"], "guidance": "louder"})],
            "prose": [bad, good],
        }
    )
    ctx, _ = _ctx(
        tmp_path,
        llm,
        world_min,
        files=files,
        view=read_package(pkg, PACKAGE_ARCANE),
        package=PACKAGE_ARCANE,
    )
    _, proposal = skill_turn(ctx, "make the quake louder", [])
    assert "room-range skill has no single target" in _last_user(2, llm)
    assert proposal is not None


def test_a_request_for_a_room_is_out_of_scope(tmp_path: Path, world_min: WorldDescription) -> None:
    llm = FakeLlm({"logic": [_load(REFINE, "turn_add_room.json")]})
    ctx, _ = _arcane_ctx(tmp_path, llm, world_min)
    turn, proposal = skill_turn(ctx, "add a room", [])
    assert turn.kind == "out_of_scope"
    assert turn.other_mode == "area"
    assert turn.note == "add a room"
    assert proposal is None


def test_set_skill_on_a_computed_field_is_noted(
    tmp_path: Path, world_min: WorldDescription
) -> None:
    # The writer refuses it with its file and line; the turn is not sent back for repair.
    pkg = tmp_path / "pkg"
    pkg.mkdir()
    (pkg / "spells.star").write_text(
        (ARCANE / "spells.star")
        .read_text()
        .replace("warmup = 0,\n      lag = 4,", "warmup = 0,\n      lag = _SPARK_LAG,", 1)
    )
    files = {p.name: p.read_text() for p in pkg.glob("*.star")}
    llm = FakeLlm({"logic": [_turn({"edit": "set_skill", "id": "spark", "fields": {"lag": 6}})]})
    ctx, _ = _ctx(
        tmp_path,
        llm,
        world_min,
        files=files,
        view=read_package(pkg, PACKAGE_ARCANE),
        package=PACKAGE_ARCANE,
    )
    _, proposal = skill_turn(ctx, "shorten the spark's lag", [])
    assert len(llm.calls) == 1
    assert proposal is not None
    assert any(
        "skill 'spark': lag is computed by code (spells.star:" in note for note in proposal.notes
    )


# -- the real engine ---------------------------------------------------------


@pytest.mark.engine
def test_the_scripted_skill_passes_the_real_engine(
    tmp_path: Path, gups_bin: Path, template_world: Path
) -> None:
    gups = Gups(gups_bin, template_world)
    world = gups.describe_world()
    api = gups.api()
    pkg = template_world / "skills" / "arcane"
    view = read_package(pkg, PACKAGE_ARCANE)
    files = {"spells.star": (pkg / "spells.star").read_text()}
    llm = FakeLlm(
        {
            "logic": [
                _load(CREATE, "brief.json"),
                _plan_with(id="ember"),
            ],
            "prose": [_load(CREATE, "prose.json")],
        }
    )
    ctx = Context(
        world_dir=template_world,
        world=world,
        api=api,
        package=PACKAGE_ARCANE,
        view=view,
        files=files,
        llm=llm,
        config=Config(max_repairs=3),
        engine=gups,
    )
    proposal = create_skill(ctx, "a fire spell like spark")
    assert isinstance(proposal, Proposal)
    assert not any(note.startswith("NOT VALID:") for note in proposal.notes)
    # Stage the proposal into the template world and run the engine on it.
    result = apply_ops(files, view, proposal.ops)
    for name, text in {**files, **result.files, **proposal.new_files}.items():
        (pkg / name).write_text(text)
    findings = gups.lint(PACKAGE_ARCANE)
    assert not [f for f in findings if f.severity == "error"]
