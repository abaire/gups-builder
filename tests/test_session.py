"""The session store: state on disk, atomic saves, name collisions, the gitignore."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from gups_builder.modes.area_checks import AreaBrief, RoomCount
from gups_builder.session import PendingChange, Session
from gups_builder.star.ops import AppendItem, SetArg

if TYPE_CHECKING:
    from pathlib import Path


def test_create_load_save_round_trip(tmp_path: Path) -> None:
    world = tmp_path / "world"
    world.mkdir()
    sess = Session.create(world, "area", "zones/mansion")
    assert sess.state.name == "mansion"
    assert sess.state.mode == "area"
    assert sess.state.package == "zones/mansion"
    assert sess.state.version == 1
    assert sess.state.grid == {}
    assert sess.state.pending is None

    loaded = Session.load(world, "mansion")
    assert loaded.state.name == "mansion"
    assert loaded.state.package == "zones/mansion"

    loaded.state.notes.append(("mob", "add a ghost"))
    loaded.state.grid["hall"] = (0, 0, 0)
    loaded.save()
    again = Session.load(world, "mansion")
    assert again.state.notes == [("mob", "add a ghost")]
    assert again.state.grid == {"hall": (0, 0, 0)}


def test_name_defaults_to_the_package_segment(tmp_path: Path) -> None:
    world = tmp_path / "world"
    world.mkdir()
    sess = Session.create(world, "area", "zones/mansion")
    assert sess.state.name == "mansion"


def test_name_collision_appends_a_counter(tmp_path: Path) -> None:
    world = tmp_path / "world"
    world.mkdir()
    Session.create(world, "area", "zones/mansion")
    second = Session.create(world, "area", "zones/mansion")
    assert second.state.name == "mansion-2"
    third = Session.create(world, "area", "zones/mansion")
    assert third.state.name == "mansion-3"


def test_an_explicit_name_is_used(tmp_path: Path) -> None:
    world = tmp_path / "world"
    world.mkdir()
    sess = Session.create(world, "area", "zones/mansion", name="my-session")
    assert sess.state.name == "my-session"
    assert Session.path(world, "my-session").is_file()


def test_builder_gitignore_is_written_on_first_use(tmp_path: Path) -> None:
    world = tmp_path / "world"
    world.mkdir()
    Session.create(world, "area", "zones/mansion")
    gitignore = world / ".builder" / ".gitignore"
    assert gitignore.is_file()
    assert "*" in gitignore.read_text()


def test_gitignore_is_not_overwritten(tmp_path: Path) -> None:
    world = tmp_path / "world" / ".builder"
    world.mkdir(parents=True)
    (world / ".gitignore").write_text("custom\n")
    Session.create(world.parent, "area", "zones/mansion")
    assert (world / ".gitignore").read_text() == "custom\n"


def test_save_is_atomic_and_loadable(tmp_path: Path) -> None:
    world = tmp_path / "world"
    world.mkdir()
    sess = Session.create(world, "area", "zones/mansion")
    sess.state.grid["hall"] = (0, 0, 0)
    sess.save()
    path = Session.path(world, "mansion")
    data = json.loads(path.read_text())
    assert data["grid"] == {"hall": [0, 0, 0]}
    # no temp file is left behind
    assert [p.name for p in path.parent.iterdir()] == ["mansion.json"]


def test_pending_change_round_trips(tmp_path: Path) -> None:
    world = tmp_path / "world"
    world.mkdir()
    sess = Session.create(world, "area", "zones/mansion")
    sess.state.pending = PendingChange(
        ops=[SetArg(kind="room", id="hall", name="desc", value="x")],
        new_files={"package.star": "package(...)"},
        link_ops=[AppendItem(kind="room", id="gate", name="exits", value={"$const": "DIR.north"})],
        base_hashes={"rooms.star": "abc"},
        summary="+1 room",
        notes=["a note"],
    )
    sess.save()
    loaded = Session.load(world, "mansion")
    assert loaded.state.pending is not None
    assert loaded.state.pending.summary == "+1 room"
    assert loaded.state.pending.notes == ["a note"]
    assert loaded.state.pending.base_hashes == {"rooms.star": "abc"}
    assert isinstance(loaded.state.pending.ops[0], SetArg)
    assert isinstance(loaded.state.pending.link_ops[0], AppendItem)


def test_planning_state_and_brief_round_trip(tmp_path: Path) -> None:
    world = tmp_path / "world"
    world.mkdir()
    sess = Session.create(world, "area", "zones/mansion")
    assert sess.state.planning is False
    assert sess.state.plan_brief is None

    sess.state.planning = True
    sess.state.plan_brief = AreaBrief(
        title="The Sunken Library",
        theme="Flooded archives beneath the city",
        style_notes="Damp stone, bioluminescent moss, hushed echoes",
        room_count=RoomCount(min=4, max=6),
        room_concepts=["vestibule: flooded entry steps", "stacks: half-submerged bookshelves"],
        link_to="//zones/tutorial:gate",
        questions=["Should there be a locked vault?"],
    )
    sess.save()

    loaded = Session.load(world, "mansion")
    assert loaded.state.planning is True
    assert loaded.state.plan_brief is not None
    assert loaded.state.plan_brief.title == "The Sunken Library"
    assert loaded.state.plan_brief.room_count == RoomCount(min=4, max=6)
    assert loaded.state.plan_brief.link_to == "//zones/tutorial:gate"
