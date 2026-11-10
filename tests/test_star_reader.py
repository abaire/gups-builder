"""Starlark reader tests: expressions to Value, and PackageView over the template fixtures."""

from __future__ import annotations

from pathlib import Path

import libcst as cst
import pytest
from libcst.metadata import MetadataWrapper

from gups_builder.star.reader import StarParseError, read_package
from gups_builder.star.values import Value, is_editable, to_value

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def _value(source: str) -> Value:
    """Parse a single bare expression statement and convert it to a Value."""
    module = MetadataWrapper(cst.parse_module(source)).module
    statement = module.body[0]
    assert isinstance(statement, cst.SimpleStatementLine)
    item = statement.body[0]
    assert isinstance(item, cst.Expr)
    return to_value(item.value, module)


def _as_dict(value: Value) -> dict[str, Value]:
    assert isinstance(value, dict)
    return value


def _as_list(value: Value) -> list[Value]:
    assert isinstance(value, list)
    return value


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("None", None),
        ("True", True),
        ("False", False),
        ("3", 3),
        ("2.5", 2.5),
        ('"hello"', "hello"),
        ('"a\\nb"', "a\nb"),
        ("'don\\'t'", "don't"),
    ],
)
def test_to_value_literals(source: str, expected: object) -> None:
    assert _value(source) == expected


def test_to_value_list() -> None:
    assert _value('[1, "x", None, True]') == [1, "x", None, True]


def test_to_value_list_of_nested_calls() -> None:
    assert _value('[damage(dice = "1d4"), 3]') == [
        {"$call": "damage", "args": [], "kwargs": {"dice": "1d4"}},
        3,
    ]


def test_to_value_dict_keeps_order() -> None:
    assert _value('{"noun": "spark", "begin": "crackle"}') == {
        "$dict": [["noun", "spark"], ["begin", "crackle"]]
    }


def test_to_value_dict_with_constant_key() -> None:
    assert _value("{MODIFIER.damage: _blunted}") == {
        "$dict": [[{"$const": "MODIFIER.damage"}, {"$ref": "_blunted"}]]
    }


def test_to_value_constant() -> None:
    assert _value("DIR.north") == {"$const": "DIR.north"}


def test_to_value_call() -> None:
    assert _value('exit(DIR.north, to = ":hall")') == {
        "$call": "exit",
        "args": [{"$const": "DIR.north"}],
        "kwargs": {"to": ":hall"},
    }


def test_to_value_call_with_ref_arg_holds_ref() -> None:
    value = _value("spawn(_loot_table)")
    assert value == {"$call": "spawn", "args": [{"$ref": "_loot_table"}], "kwargs": {}}
    assert not is_editable(value)


def test_to_value_ref() -> None:
    assert _value("_hall_enter") == {"$ref": "_hall_enter"}
    assert not is_editable({"$ref": "_hall_enter"})


def test_to_value_negative_number() -> None:
    assert _value("-10") == -10


def test_to_value_opaque_concatenation() -> None:
    assert _value('"a" + "b"') == {"$opaque": '"a" + "b"'}


def test_to_value_opaque_lowercase_attribute() -> None:
    assert _value("ctx.args") == {"$opaque": "ctx.args"}


def test_to_value_opaque_method_call() -> None:
    assert _value("foo.bar(1)") == {"$opaque": "foo.bar(1)"}


@pytest.mark.parametrize(
    "value",
    [
        3,
        "x",
        [1, 2],
        {"$const": "DIR.north"},
        {"$call": "exit", "args": [], "kwargs": {"to": ":hall"}},
        {"$dict": [["a", 1]]},
    ],
)
def test_is_editable(value: Value) -> None:
    assert is_editable(value)


@pytest.mark.parametrize(
    "value",
    [
        {"$ref": "_hook"},
        {"$opaque": "x"},
        [{"$ref": "_hook"}],
        {"$call": "spawn", "args": [{"$ref": "_t"}], "kwargs": {}},
        {"$dict": [["k", {"$opaque": "v"}]]},
    ],
)
def test_is_not_editable(value: Value) -> None:
    assert not is_editable(value)


def test_tutorial_package_files() -> None:
    view = read_package(FIXTURES / "tutorial", "zones/tutorial")
    assert view.path == "zones/tutorial"
    assert sorted(view.files) == ["items.star", "mobs.star", "rooms.star"]
    assert all(len(digest) == 64 for digest in view.files.values())
    assert view.opaque == []


def test_declaration_order_is_file_then_line() -> None:
    view = read_package(FIXTURES / "tutorial", "zones/tutorial")
    assert view.decls[0].file == "items.star"
    assert view.decls[0].id == "brass_key"
    assert {d.file for d in view.decls} == {"items.star", "mobs.star", "rooms.star"}
    rooms = [d for d in view.decls if d.file == "rooms.star"]
    assert [d.id for d in rooms] == [
        "gate",
        "hall",
        "tar_pit",
        "gravel",
        "armory",
        "shop",
        "practice_grounds",
        "arena",
    ]


def test_tutorial_room_hall() -> None:
    view = read_package(FIXTURES / "tutorial", "zones/tutorial")
    hall = view.get("room", "hall")
    assert hall is not None
    assert hall.file == "rooms.star"
    assert hall.args["name"] == "The Hall of Lessons"
    assert hall.args["size"] == {"$const": "SIZE.large"}
    assert hall.args["on_enter"] == {"$ref": "_hall_enter"}
    assert not is_editable(hall.args["on_enter"])

    exits = _as_list(hall.args["exits"])
    assert len(exits) == 4
    assert [item["$call"] for item in map(_as_dict, exits)] == ["exit"] * 4
    assert [item["args"] for item in map(_as_dict, exits)] == [
        [{"$const": "DIR.south"}],
        [{"$const": "DIR.west"}],
        [{"$const": "DIR.north"}],
        [{"$const": "DIR.east"}],
    ]
    assert _as_dict(_as_dict(exits[2])["kwargs"])["entry_index"] == -1
    door = _as_dict(_as_dict(_as_dict(exits[3])["kwargs"])["door"])
    assert door["$call"] == "door"
    door_kwargs = _as_dict(door["kwargs"])
    assert door_kwargs["state"] == {"$const": "DOOR.locked"}
    assert door_kwargs["key"] == ":brass_key"
    assert door_kwargs["tags"] == ["creaky"]


def test_declaration_line_number() -> None:
    view = read_package(FIXTURES / "tutorial", "zones/tutorial")
    hall = view.get("room", "hall")
    assert hall is not None
    lines = (FIXTURES / "tutorial" / "rooms.star").read_text().splitlines()
    assert hall.line == lines.index('room("hall",') + 1


def test_tutorial_room_gate_commands_are_refs() -> None:
    view = read_package(FIXTURES / "tutorial", "zones/tutorial")
    gate = view.get("room", "gate")
    assert gate is not None
    commands = _as_dict(gate.args["commands"])
    pairs = commands["$dict"]
    assert isinstance(pairs, list) and pairs == [["read", {"$ref": "_read"}]]
    assert not is_editable(gate.args["commands"])


def test_tutorial_region_declaration() -> None:
    view = read_package(FIXTURES / "tutorial", "zones/tutorial")
    region = view.get("region", "practice_grounds")
    assert region is not None
    assert region.kind == "region"
    modifiers = _as_dict(region.args["modifiers"])
    pairs = _as_list(modifiers["$dict"])
    assert pairs == [[{"$const": "MODIFIER.damage"}, {"$ref": "_blunted"}]]


def test_tutorial_mob() -> None:
    view = read_package(FIXTURES / "tutorial", "zones/tutorial")
    guide = view.get("mob", "guide")
    assert guide is not None
    assert guide.args["on_say"] == {"$ref": "_guide_hears"}
    behaviors = _as_list(guide.args["behaviors"])
    assert len(behaviors) == 1
    assert behaviors[0] == {"$call": "sentinel", "args": [], "kwargs": {}}
    assert is_editable(behaviors[0])
    senses = _as_dict(guide.args["senses"])
    pairs = senses["$dict"]
    assert isinstance(pairs, list) and pairs == [["sound", 1]]


def test_tutorial_item_nested_calls() -> None:
    view = read_package(FIXTURES / "tutorial", "zones/tutorial")
    key = view.get("item", "brass_key")
    assert key is not None
    assert key.args["keywords"] == ["brass", "key"]
    dagger = view.get("item", "ember_dagger")
    assert dagger is not None
    [proc] = _as_list(dagger.args["procs"])
    effect = _as_dict(_as_dict(_as_dict(proc)["kwargs"])["effect"])
    assert effect["$call"] == "damage"
    damage_type = _as_dict(_as_dict(effect["kwargs"])["type"])
    assert damage_type["$const"] == "DAMAGE.fire"
    assert is_editable(dagger.args["procs"])


def test_spells_star() -> None:
    view = read_package(FIXTURES / "arcane", "skills/arcane")
    spark = view.get("skill", "spark")
    assert spark is not None
    assert _as_dict(spark.args["effect"])["$call"] == "damage"
    assert is_editable(spark.args["effect"])
    pacify = view.get("skill", "pacify")
    assert pacify is not None
    assert pacify.args["resolve"] == {"$ref": "_pacify"}
    assert not is_editable(pacify.args["resolve"])


def test_declaration_in_for_loop_is_opaque(tmp_path: Path) -> None:
    (tmp_path / "a.star").write_text('for x in (1,):\n    room("x", name = "X")\n')
    view = read_package(tmp_path, "zones/x")
    assert view.decls == []
    [(entry)] = view.opaque
    assert entry.kind == "room"
    assert entry.id == "x"
    assert entry.reason == "not at top level"
    assert entry.line == 2


def test_assignment_is_opaque(tmp_path: Path) -> None:
    (tmp_path / "a.star").write_text('R = room("y", name = "Y")\n')
    view = read_package(tmp_path, "zones/x")
    [(entry)] = view.opaque
    assert entry.reason == "not at top level"
    assert entry.id == "y"


def test_non_literal_id_is_opaque(tmp_path: Path) -> None:
    (tmp_path / "a.star").write_text('room(make_id(), name = "Z")\n')
    view = read_package(tmp_path, "zones/x")
    [(entry)] = view.opaque
    assert entry.reason == "id is not a string literal"
    assert entry.id is None


def test_extra_positional_arguments(tmp_path: Path) -> None:
    (tmp_path / "a.star").write_text('room("a", 1, 2, name = "x")\n')
    view = read_package(tmp_path, "zones/x")
    assert len(view.decls) == 1
    decl = view.decls[0]
    assert decl.id == "a"
    assert decl.extra_positional == [1, 2]
    assert decl.args == {"name": "x"}


def test_test_and_package_files_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "a.star").write_text('room("a", name = "A")\n')
    (tmp_path / "a_test.star").write_text('room("b", name = "B")\n')
    (tmp_path / "a_testdata.star").write_text('item("c", name = "C")\n')
    (tmp_path / "package.star").write_text('package(name = "a")\n')
    view = read_package(tmp_path, "zones/x")
    assert list(view.files) == ["a.star"]
    assert [d.id for d in view.decls] == ["a"]


def test_hash_changes_when_file_changes(tmp_path: Path) -> None:
    (tmp_path / "a.star").write_text('room("a", name = "A")\n')
    first = read_package(tmp_path, "zones/x").files["a.star"]
    (tmp_path / "a.star").write_text('room("a", name = "B")\n')
    second = read_package(tmp_path, "zones/x").files["a.star"]
    assert first != second


def test_parse_error_names_file_and_line(tmp_path: Path) -> None:
    (tmp_path / "broken.star").write_text("x = 1\nroom(:)\n")
    with pytest.raises(StarParseError) as excinfo:
        read_package(tmp_path, "zones/x")
    error = excinfo.value
    assert error.file == "broken.star"
    assert error.line == 2
    assert error.message


def test_get_returns_match_or_none() -> None:
    view = read_package(FIXTURES / "tutorial", "zones/tutorial")
    assert view.get("room", "gate") is not None
    assert view.get("room", "nope") is None
    assert view.get("mob", "hall") is None


@pytest.mark.parametrize(
    "source", ["exit(*_dirs)", "vitals(**_base)", 'exit(DIR.up, *_more, to = ":a")']
)
def test_to_value_star_arguments_are_opaque(source: str) -> None:
    assert _value(source) == {"$opaque": source}
