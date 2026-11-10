"""Starlark writer tests: applying StarOps with formatting preserved, rendering, diffs."""

from __future__ import annotations

from pathlib import Path

import pytest

from gups_builder.star.diff import unified_diff
from gups_builder.star.ops import (
    AddDecl,
    AddTodo,
    AppendItem,
    ItemMatch,
    RemoveArg,
    RemoveDecl,
    RemoveItem,
    SetArg,
    StarOp,
)
from gups_builder.star.reader import PackageView, read_package
from gups_builder.star.render import render_decl, render_value
from gups_builder.star.values import Value
from gups_builder.star.writer import apply_ops

FIXTURES = Path(__file__).resolve().parent / "fixtures"
TUTORIAL = FIXTURES / "tutorial"

EXIT_UP: Value = {"$call": "exit", "args": [{"$const": "DIR.up"}], "kwargs": {"to": ":attic"}}
EXIT_WEST: Value = {"$const": "DIR.west"}
SPAWN_GUIDE: Value = {"$call": "spawn", "args": [":guide"], "kwargs": {}}


def _package() -> tuple[dict[str, str], PackageView]:
    view = read_package(TUTORIAL, "zones/tutorial")
    current = {name: (TUTORIAL / name).read_text() for name in view.files}
    return current, view


def _as_list(value: Value) -> list[Value]:
    assert isinstance(value, list)
    return value


def _as_dict(value: Value) -> dict[str, Value]:
    assert isinstance(value, dict)
    return value


def _first_arg(item: Value) -> Value:
    args = _as_dict(item)["args"]
    assert isinstance(args, list)
    return args[0]


def test_empty_ops_change_nothing() -> None:
    current, view = _package()
    result = apply_ops(current, view, [])
    assert result.files == {}
    assert result.refused == []


def test_set_arg_changes_only_that_line() -> None:
    current, view = _package()
    result = apply_ops(
        current, view, [SetArg(kind="room", id="hall", name="desc", value="New desc.")]
    )
    assert result.refused == []
    assert set(result.files) == {"rooms.star"}
    old_lines = current["rooms.star"].splitlines()
    new_lines = result.files["rooms.star"].splitlines()
    assert len(old_lines) == len(new_lines)
    changed = [i for i, (a, b) in enumerate(zip(old_lines, new_lines, strict=True)) if a != b]
    assert len(changed) == 1
    assert new_lines[changed[0]] == '    desc = "New desc.",'


def test_set_arg_appends_missing_argument() -> None:
    current, view = _package()
    result = apply_ops(
        current, view, [SetArg(kind="room", id="hall", name="tags", value=["quiet"])]
    )
    assert result.refused == []
    lines = result.files["rooms.star"].splitlines()
    on_enter_i = lines.index("    on_enter = _hall_enter,")
    assert lines[on_enter_i + 1] == '    tags = ["quiet"],'


def test_append_item_multiline_list_stays_one_per_line() -> None:
    current, view = _package()
    result = apply_ops(
        current, view, [AppendItem(kind="room", id="hall", name="exits", value=EXIT_UP)]
    )
    assert result.refused == []
    lines = result.files["rooms.star"].splitlines()
    assert '        exit(DIR.up, to = ":attic"),' in lines
    hall_i = lines.index('room("hall",')
    exits_block = lines[hall_i : lines.index("    ],", hall_i)]
    exits = [line for line in exits_block if line.lstrip().startswith("exit(")]
    assert len(exits) == 5
    assert all(line == "        " + line.lstrip() for line in exits)
    assert exits[-1] == '        exit(DIR.up, to = ":attic"),'


def test_append_item_one_line_list_stays_one_line() -> None:
    current, view = _package()
    result = apply_ops(
        current, view, [AppendItem(kind="room", id="tar_pit", name="exits", value=EXIT_UP)]
    )
    assert result.refused == []
    lines = result.files["rooms.star"].splitlines()
    pit_i = lines.index('room("tar_pit",')
    exits_line = next(line for line in lines[pit_i:] if line.strip().startswith("exits"))
    assert exits_line.count("exit(") == 3
    assert exits_line.rstrip().endswith("],")
    assert 'exit(DIR.up, to = ":attic")' in exits_line


def test_append_item_creates_the_argument() -> None:
    current, view = _package()
    result = apply_ops(
        current, view, [AppendItem(kind="room", id="hall", name="on_load", value=SPAWN_GUIDE)]
    )
    assert result.refused == []
    assert '    on_load = [spawn(":guide")],' in result.files["rooms.star"].splitlines()


def test_remove_item_by_first_arg() -> None:
    current, view = _package()
    result = apply_ops(
        current,
        view,
        [RemoveItem(kind="room", id="hall", name="exits", match=ItemMatch(first_arg=EXIT_WEST))],
    )
    assert result.refused == []
    new = result.files["rooms.star"]
    assert new.count("exit(DIR.west") == 3  # tar_pit's, armory's and shop's west exits remain
    lines = new.splitlines()
    hall_i = lines.index('room("hall",')
    exits_block = lines[hall_i : lines.index("    ],", hall_i)]
    east = next(
        line
        for line in current["rooms.star"].splitlines()
        if 'exit(DIR.east, to = ":armory"' in line
    )
    assert [line for line in exits_block if line.lstrip().startswith("exit(")] == [
        '        exit(DIR.south, to = ":gate"),',
        '        exit(DIR.north, to = ":arena", entry_index = -1),',
        east,
    ]


def test_remove_item_zero_matches_refused() -> None:
    current, view = _package()
    northe: Value = {"$const": "DIR.northeast"}
    result = apply_ops(
        current,
        view,
        [
            RemoveItem(
                kind="room",
                id="hall",
                name="exits",
                match=ItemMatch(first_arg=northe),
            )
        ],
    )
    assert result.files == {}
    assert len(result.refused) == 1
    assert "no matching" in result.refused[0].reason


def test_remove_item_multiple_matches_refused() -> None:
    current, view = _package()
    result = apply_ops(
        current,
        view,
        [RemoveItem(kind="room", id="hall", name="exits", match=ItemMatch(call="exit"))],
    )
    assert result.files == {}
    assert len(result.refused) == 1
    assert "more than one" in result.refused[0].reason


def test_set_arg_refused_on_ref_value() -> None:
    current, view = _package()
    result = apply_ops(
        current, view, [SetArg(kind="room", id="hall", name="on_enter", value="_other")]
    )
    assert result.files == {}
    assert len(result.refused) == 1
    refusal = result.refused[0]
    line = current["rooms.star"].splitlines().index("    on_enter = _hall_enter,") + 1
    assert (
        refusal.reason
        == f"room 'hall': on_enter is computed by code (rooms.star:{line}); change it by hand"
    )
    assert refusal.file == "rooms.star"
    assert refusal.line == line


def test_set_arg_refused_on_opaque_value() -> None:
    current, view = _package()
    result = apply_ops(
        current, view, [SetArg(kind="room", id="gate", name="commands", value={"$dict": []})]
    )
    assert result.files == {}
    [refusal] = result.refused
    assert "is computed by code (rooms.star:" in refusal.reason
    assert refusal.reason.endswith("change it by hand")
    assert refusal.line is not None


def test_set_arg_refused_on_non_editable_new_value() -> None:
    current, view = _package()
    result = apply_ops(
        current, view, [SetArg(kind="room", id="hall", name="desc", value={"$ref": "_x"})]
    )
    assert result.files == {}
    [refusal] = result.refused
    assert "plain value" in refusal.reason


def test_add_decl_into_existing_file() -> None:
    current, view = _package()
    result = apply_ops(
        current,
        view,
        [AddDecl(kind="item", id="fresh_item", file="items.star", args={"name": "a fresh item"})],
    )
    assert result.refused == []
    new = result.files["items.star"]
    lines = new.splitlines()
    assert lines[-3:] == ['item("fresh_item",', '    name = "a fresh item",', ")"]
    assert lines[-4] == ""
    assert new.startswith(current["items.star"].rstrip("\n"))


def test_add_decl_into_new_file() -> None:
    current, view = _package()
    result = apply_ops(
        current,
        view,
        [
            AddDecl(
                kind="region",
                id="quiet",
                file="regions.star",
                args={"modifiers": {"$dict": [[{"$const": "MODIFIER.regen"}, 3]]}},
            )
        ],
    )
    assert result.refused == []
    assert result.files == {
        "regions.star": (
            "# Regions of zones/tutorial, started by gups-builder.\n"
            'region("quiet",\n'
            "    modifiers = {MODIFIER.regen: 3},\n"
            ")\n"
        )
    }


def test_add_decl_duplicate_id_refused() -> None:
    current, view = _package()
    result = apply_ops(
        current, view, [AddDecl(kind="room", id="hall", file="rooms.star", args={"name": "x"})]
    )
    assert result.files == {}
    [refusal] = result.refused
    assert refusal.reason == "room 'hall' already exists"


def test_remove_decl_takes_its_comments() -> None:
    current, view = _package()
    result = apply_ops(current, view, [RemoveDecl(kind="item", id="ember_dagger")])
    assert result.refused == []
    new = result.files["items.star"]
    assert "ember_dagger" not in new
    assert "The ember only flares" not in new
    assert "\n\n\n" not in new
    assert "cracked_wand" in new
    assert new.endswith(")\n")


def test_add_todo_placement() -> None:
    current, view = _package()
    result = apply_ops(
        current, view, [AddTodo(kind="item", id="ember_dagger", text="write the _flame modifier")]
    )
    assert result.refused == []
    lines = result.files["items.star"].splitlines()
    decl_i = lines.index('item("ember_dagger",')
    assert lines[decl_i - 1] == "# TODO(gups-builder): write the _flame modifier"
    assert lines[decl_i - 2].startswith("# The ember only flares")
    assert sum(1 for line in lines if line.startswith("# TODO(gups-builder):")) == 1


def test_later_ops_see_earlier_ops() -> None:
    current, view = _package()
    result = apply_ops(
        current,
        view,
        [
            AddDecl(kind="item", id="fresh", file="items.star", args={"name": "a fresh thing"}),
            SetArg(kind="item", id="fresh", name="desc", value="Shiny."),
        ],
    )
    assert result.refused == []
    new = result.files["items.star"]
    assert 'name = "a fresh thing"' in new
    assert 'desc = "Shiny."' in new


def test_refused_op_does_not_stop_the_rest() -> None:
    current, view = _package()
    result = apply_ops(
        current,
        view,
        [
            SetArg(kind="room", id="hall", name="on_enter", value="_other"),
            SetArg(kind="room", id="hall", name="desc", value="New desc."),
        ],
    )
    assert len(result.refused) == 1
    assert '    desc = "New desc.",' in result.files["rooms.star"].splitlines()


def test_round_trip_read_package(tmp_path: Path) -> None:
    current, view = _package()
    result = apply_ops(
        current,
        view,
        [
            SetArg(kind="room", id="hall", name="size", value={"$const": "SIZE.normal"}),
            AppendItem(
                kind="room",
                id="gate",
                name="exits",
                value={
                    "$call": "exit",
                    "args": [{"$const": "DIR.south"}],
                    "kwargs": {"to": ":outside"},
                },
            ),
            AddDecl(
                kind="region",
                id="quiet",
                file="rooms.star",
                args={"modifiers": {"$dict": [["MODIFIER.regen", 1]]}},
            ),
        ],
    )
    assert result.refused == []
    for name, text in {**current, **result.files}.items():
        (tmp_path / name).write_text(text)
    view2 = read_package(tmp_path, "zones/tutorial")
    hall = view2.get("room", "hall")
    assert hall is not None and hall.args["size"] == {"$const": "SIZE.normal"}
    gate = view2.get("room", "gate")
    assert gate is not None
    directions = [_first_arg(item) for item in _as_list(gate.args["exits"])]
    assert {"$const": "DIR.south"} in directions
    quiet = view2.get("region", "quiet")
    assert quiet is not None
    assert _as_dict(quiet.args["modifiers"])["$dict"] == [["MODIFIER.regen", 1]]


def test_render_decl_room_with_one_exit() -> None:
    text = render_decl(
        "room",
        "cellar",
        {
            "name": "The Cellar",
            "exits": [{"$call": "exit", "args": [{"$const": "DIR.up"}], "kwargs": {"to": ":hall"}}],
        },
        None,
    )
    assert text == (
        'room("cellar",\n    name = "The Cellar",\n    exits = [exit(DIR.up, to = ":hall")],\n)\n'
    )


def test_render_decl_room_with_multiline_exits() -> None:
    text = render_decl(
        "room",
        "door",
        {
            "exits": [
                {"$call": "exit", "args": [{"$const": "DIR.up"}], "kwargs": {"to": ":hall"}},
                {"$call": "exit", "args": [{"$const": "DIR.down"}], "kwargs": {"to": ":cellar"}},
            ]
        },
        None,
    )
    assert text == (
        'room("door",\n'
        "    exits = [\n"
        '        exit(DIR.up, to = ":hall"),\n'
        '        exit(DIR.down, to = ":cellar"),\n'
        "    ],\n"
        ")\n"
    )


def test_render_decl_mob_with_vitals_and_behaviors() -> None:
    text = render_decl(
        "mob",
        "guard",
        {
            "name": "a guard",
            "vitals": {"$call": "vitals", "args": [], "kwargs": {"hp": 100}},
            "behaviors": [
                {"$call": "sentinel", "args": [], "kwargs": {}},
                {"$call": "guard", "args": [], "kwargs": {"exit": {"$const": "DIR.north"}}},
            ],
        },
        None,
    )
    assert text == (
        'mob("guard",\n'
        '    name = "a guard",\n'
        "    vitals = vitals(hp = 100),\n"
        "    behaviors = [\n"
        "        sentinel(),\n"
        "        guard(exit = DIR.north),\n"
        "    ],\n"
        ")\n"
    )


def test_render_decl_skill_with_nested_sequence() -> None:
    text = render_decl(
        "skill",
        "combo",
        {
            "effect": {
                "$call": "sequence",
                "args": [],
                "kwargs": {
                    "effects": [
                        {
                            "$call": "damage",
                            "args": [],
                            "kwargs": {"dice": "1d4", "type": {"$const": "DAMAGE.fire"}},
                        },
                        {"$call": "heal", "args": [], "kwargs": {"dice": "1d6", "vital": "hp"}},
                    ]
                },
            }
        },
        None,
    )
    assert text == (
        'skill("combo",\n'
        "    effect = sequence(\n"
        "        effects = [\n"
        '            damage(dice = "1d4", type = DAMAGE.fire),\n'
        '            heal(dice = "1d6", vital = "hp"),\n'
        "        ],\n"
        "    ),\n"
        ")\n"
    )


def test_render_decl_comment_and_no_args() -> None:
    assert render_decl("item", "note", {}, "A plain note.") == '# A plain note.\nitem("note")\n'


def test_render_string_non_ascii_is_literal() -> None:
    text = render_decl("room", "r", {"desc": "The door creaks — the hall’s light gutters."}, None)
    assert "The door creaks — the hall’s light gutters." in text
    assert "\\u" not in text
    assert render_value("A — b’s c", 0) == '"A — b’s c"'


def test_render_string_escaping() -> None:
    assert render_value('say "hi"', 0) == '"say \\"hi\\""'
    assert render_value("a\\b", 0) == '"a\\\\b"'
    assert render_value("line1\nline2", 0) == '"line1\\nline2"'


def test_unified_diff_lists_new_files_as_created() -> None:
    text = unified_diff({}, {"notes.star": 'item("note",\n    name = "a note",\n)\n'})
    assert "--- /dev/null" in text
    assert "+++ notes.star" in text
    assert '+item("note",' in text


def test_unified_diff_shows_changed_lines() -> None:
    before = {"rooms.star": 'room("a",\n    name = "Old",\n)\n'}
    after = {"rooms.star": 'room("a",\n    name = "New",\n)\n'}
    text = unified_diff(before, after)
    assert "--- rooms.star" in text
    assert "+++ rooms.star" in text
    assert '-    name = "Old",' in text
    assert '+    name = "New",' in text


# Edge cases found in review: refusals the writer must make, and comments and
# formatting it must not lose.

HALL = """room("hall",
    name = "Hall",
    on_enter = _hall_enter,
    exits = [
        exit(DIR.south, to = ":gate"),
        # the west wing
        exit(DIR.west, to = ":wing"),
        exit(DIR.north, to = ":stairs"),
    ],
    more = [exit(DIR.up, to = ":a"), *_extra],
    spawns = [spawn(":rat"), _boss_spawn],
)
"""
EMPTY_VIEW = PackageView("zones/x", {}, [], [])


def _apply_hall(ops: list[StarOp], src: str = HALL) -> tuple[str, list[str]]:
    result = apply_ops({"rooms.star": src}, EMPTY_VIEW, ops)
    return result.files.get("rooms.star", src), [r.reason for r in result.refused]


def test_remove_arg_refused_on_ref_value() -> None:
    text, refused = _apply_hall([RemoveArg(kind="room", id="hall", name="on_enter")])
    assert refused == [
        "room 'hall': on_enter is computed by code (rooms.star:3); change it by hand"
    ]
    assert text == HALL


def test_list_items_refused_when_the_list_is_not_editable() -> None:
    down: Value = {"$call": "exit", "args": [{"$const": "DIR.down"}], "kwargs": {"to": ":b"}}
    text, refused = _apply_hall(
        [
            AppendItem(kind="room", id="hall", name="more", value=down),
            RemoveItem(kind="room", id="hall", name="more", match=ItemMatch(call="exit")),
            AppendItem(kind="room", id="hall", name="spawns", value=SPAWN_GUIDE),
            RemoveItem(kind="room", id="hall", name="spawns", match=ItemMatch(call="spawn")),
        ]
    )
    assert refused == [
        "room 'hall': more is computed by code (rooms.star:10); change it by hand",
        "room 'hall': more is computed by code (rooms.star:10); change it by hand",
        "room 'hall': spawns is computed by code (rooms.star:11); change it by hand",
        "room 'hall': spawns is computed by code (rooms.star:11); change it by hand",
    ]
    assert text == HALL


def test_remove_first_item_keeps_the_comment_above_the_next() -> None:
    text, refused = _apply_hall(
        [
            RemoveItem(
                kind="room",
                id="hall",
                name="exits",
                match=ItemMatch(first_arg={"$const": "DIR.south"}),
            )
        ]
    )
    assert refused == []
    assert text == HALL.replace('        exit(DIR.south, to = ":gate"),\n', "")


def test_remove_last_arg_without_trailing_comma_keeps_the_paren_line() -> None:
    src = 'room("a",\n    name = "A",\n    desc = "D"\n)\n'
    text, refused = _apply_hall([RemoveArg(kind="room", id="a", name="desc")], src)
    assert refused == []
    assert text == 'room("a",\n    name = "A"\n)\n'


def test_multiline_todo_becomes_several_comment_lines() -> None:
    text, refused = _apply_hall([AddTodo(kind="room", id="hall", text="write a hook\nthat locks")])
    assert refused == []
    assert text == "# TODO(gups-builder): write a hook\n#   that locks\n" + HALL


def test_multiline_decl_comment_becomes_several_comment_lines() -> None:
    text, refused = _apply_hall(
        [
            AddDecl(file="rooms.star", kind="room", id="b", args={}, comment="one\ntwo"),
            SetArg(kind="room", id="hall", name="name", value="Big Hall"),
        ]
    )
    assert refused == []
    assert text.endswith('\n# one\n# two\nroom("b")\n')
    assert 'name = "Big Hall",' in text


@pytest.mark.parametrize(
    "value",
    [
        {"$const": "X) ; boom("},
        {"$const": "north"},
        {"$call": "a.b-c", "args": [], "kwargs": {}},
        {"$call": "door", "args": [], "kwargs": {"x = 1, y": 2}},
        float("nan"),
        float("inf"),
    ],
)
def test_malformed_values_are_refused_not_written(value: Value) -> None:
    text, refused = _apply_hall(
        [
            SetArg(kind="room", id="hall", name="name", value=value),
            AddDecl(file="rooms.star", kind="room", id="b", args={"name": value}),
            SetArg(kind="room", id="hall", name="desc", value="still applied"),
        ]
    )
    assert refused == [
        "room 'hall': name may only hold a plain value",
        "new arguments of room 'b' must be plain values",
    ]
    assert text == HALL.replace(
        '    spawns = [spawn(":rat"), _boss_spawn],\n)',
        '    spawns = [spawn(":rat"), _boss_spawn],\n    desc = "still applied",\n)',
    )


def test_remove_decl_on_a_shared_line_removes_only_that_one() -> None:
    src = 'room("a", name = "A"); room("b", name = "B")\n'
    text, refused = _apply_hall([RemoveDecl(kind="room", id="a")], src)
    assert refused == []
    assert text == 'room("b", name = "B")\n'
    text, refused = _apply_hall([RemoveDecl(kind="room", id="b")], src)
    assert text == 'room("a", name = "A")\n'


def test_remove_first_decl_takes_the_comment_directly_above_it() -> None:
    src = '# Map:\n#   [A]-[B]\n\n# The first room.\nroom("a")\n\nroom("b")\n'
    text, refused = _apply_hall([RemoveDecl(kind="room", id="a")], src)
    assert refused == []
    assert text == '# Map:\n#   [A]-[B]\n\nroom("b")\n'


def test_append_multiline_item_is_indented_as_an_item() -> None:
    long_exit: Value = {
        "$call": "exit",
        "args": [{"$const": "DIR.east"}],
        "kwargs": {"to": ":e", "tags": ["a" * 40, "b" * 40]},
    }
    text, refused = _apply_hall([AppendItem(kind="room", id="hall", name="exits", value=long_exit)])
    assert refused == []
    expected = (
        '        exit(DIR.north, to = ":stairs"),\n'
        "        exit(\n"
        "            DIR.east,\n"
        '            to = ":e",\n'
        "            tags = [\n"
        f'                "{"a" * 40}",\n'
        f'                "{"b" * 40}",\n'
        "            ],\n"
        "        ),\n"
        "    ],\n"
    )
    assert expected in text
