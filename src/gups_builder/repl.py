"""The interactive loop: a conversation about one package in one mode.

The REPL re-reads the package before every model turn, refuses to talk to a
model while the loaded package is broken, turns plain text into a mode call, and
holds the result as a pending change the user reviews. Slash commands show the
map, a declaration's source, and the diff, and control the write, the undo and
the reload. A hand edit to a file the builder wrote is detected by hash and
either re-applies the pending ops or blocks an undo.
"""

from __future__ import annotations

import ast
import hashlib
import os
import tempfile
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Literal, cast

import libcst as cst
from rich.console import Console

from gups_builder.config import Config
from gups_builder.engine.gups import EngineError
from gups_builder.engine.world import WorldDescription
from gups_builder.grid.ascii import render_map
from gups_builder.grid.derive import DIRECTION_DELTAS
from gups_builder.grid.model import Connection, Coord, DoorSpec, Grid, GridRoom
from gups_builder.grid.place import place_existing, room_id
from gups_builder.llm.client import LlmClient, LlmError, Message
from gups_builder.llm.structured import StructuredCallFailed
from gups_builder.modes import testgen
from gups_builder.modes.area import AreaBrief, area_turn, create_area
from gups_builder.modes.area_checks import grid_from_coords
from gups_builder.modes.common import Context, Engine, Proposal, Turn
from gups_builder.modes.mob import MobBrief, create_mobs, mob_turn
from gups_builder.modes.skill import SkillBrief, create_skill, skill_turn
from gups_builder.session import Mode, PendingChange, Session, SessionState
from gups_builder.star.diff import unified_diff
from gups_builder.star.ops import StarOp
from gups_builder.star.reader import (
    DeclView,
    PackageView,
    StarParseError,
    _star_files,
    read_package,
)
from gups_builder.star.values import Value
from gups_builder.star.writer import apply_ops

_DoorState = Literal["open", "closed", "locked"]
_STATE: dict[str, _DoorState] = {"open": "open", "closed": "closed", "locked": "locked"}


def _local_id(label: str, package: str) -> str | None:
    """The id when the label names something in the package; None otherwise."""
    if label.startswith(":"):
        return label[1:]
    prefix = f"//{package}:"
    return label[len(prefix) :] if label.startswith(prefix) else None


def _direction_name(value: Value) -> str | None:
    """The exit's direction name, whatever it is called."""
    if isinstance(value, dict):
        const = value.get("$const")
        if isinstance(const, str) and const.startswith("DIR."):
            return const[4:]
        return None
    if isinstance(value, str):
        return value
    return None


def _door_state(value: Value) -> _DoorState | None:
    """The state a ``DOOR.<state>`` constant names, or None for anything else."""
    const = value.get("$const") if isinstance(value, dict) else None
    if not isinstance(const, str) or not const.startswith("DOOR."):
        return None
    return _STATE.get(const[5:])


def _call_kwargs(value: Value, callee: str) -> dict[str, Value] | None:
    """The keyword arguments of a ``callee(...)`` value, or None when it is not one."""
    if not isinstance(value, dict) or value.get("$call") != callee:
        return None
    kwargs = value.get("kwargs")
    return kwargs if isinstance(kwargs, dict) else None


def _door_spec(value: Value) -> DoorSpec | None:
    """A DoorSpec from a ``door`` call value, or None when it is not one."""
    kwargs = _call_kwargs(value, "door")
    state = _door_state(kwargs.get("state")) if kwargs is not None else None
    if kwargs is None or state is None:
        return None
    key = kwargs.get("key")
    tags = kwargs.get("tags")
    tags_list = [t for t in tags if isinstance(t, str)] if isinstance(tags, list) else []
    return DoorSpec(state=state, key=key if isinstance(key, str) else None, tags=tags_list)


def _first_direction(args: Value) -> str | None:
    if not isinstance(args, list) or not args:
        return None
    return _direction_name(args[0])


def _exit_entry(item: Value) -> tuple[str, str, DoorSpec | None] | None:
    """An ``exit(direction, to = ..., door = ...)`` value as (direction, target, door)."""
    kwargs = _call_kwargs(item, "exit")
    if kwargs is None:
        return None
    direction = _first_direction(item.get("args") if isinstance(item, dict) else None)
    to = kwargs.get("to")
    if direction is None or not isinstance(to, str):
        return None
    return direction, to, _door_spec(kwargs.get("door"))


def _exits_of(decl: DeclView) -> list[tuple[str, str, DoorSpec | None]]:
    """The room's exits as (direction, target, door), in source order."""
    exits = decl.args.get("exits")
    if not isinstance(exits, list):
        return []
    entries = (_exit_entry(item) for item in exits)
    return [entry for entry in entries if entry is not None]


def _passage_target(direction: str, to: str, package: str, room_ids: set[str]) -> str | None:
    """The package's room a standard-direction exit leads to, or None for any other exit."""
    if direction not in DIRECTION_DELTAS:
        return None
    local = _local_id(to, package)
    return local if local in room_ids else None


def _decl_passages(decl: DeclView, package: str, room_ids: set[str]) -> list[Connection]:
    """The room's standard-direction exits to the package's rooms, as connections."""
    passages: list[Connection] = []
    for direction, to, door in _exits_of(decl):
        local = _passage_target(direction, to, package, room_ids)
        if local is not None:
            passages.append(Connection(a=decl.id, b=local, door=door))
    return passages


def _unique_passages(connections: Iterable[Connection]) -> list[Connection]:
    """The connections, keeping the first of any two that join the same pair of rooms."""
    unique: list[Connection] = []
    seen: set[frozenset[str]] = set()
    for connection in connections:
        pair = frozenset((connection.a, connection.b))
        if pair not in seen:
            seen.add(pair)
            unique.append(connection)
    return unique


def _room_names(rooms: list[DeclView]) -> dict[str, str]:
    names: dict[str, str] = {}
    for decl in rooms:
        name = decl.args.get("name")
        if isinstance(name, str):
            names[decl.id] = name
    return names


def _read_contents(contents: dict[str, str], package: str) -> PackageView:
    """The package as the given file contents stand it, read from a scratch copy."""
    with tempfile.TemporaryDirectory() as tmp:
        pkg = Path(tmp) / "pkg"
        pkg.mkdir()
        for name, text in contents.items():
            (pkg / name).write_text(text, encoding="utf-8")
        return read_package(pkg, package)


def _affected_rooms(
    proposal: Proposal, grid: dict[str, Coord], before: dict[str, Coord]
) -> set[str]:
    """The rooms a proposal adds, moves, removes or edits."""
    affected = {op.id for op in proposal.ops if op.kind == "room"}
    affected |= {rid for rid, coord in grid.items() if before.get(rid) != coord}
    return affected | (before.keys() - grid.keys())


def _levels(rooms: set[str], grid: dict[str, Coord], before: dict[str, Coord]) -> set[int]:
    """The z levels the rooms are on after the proposal, or were on before it."""
    levels = {grid[r][2] for r in rooms if r in grid}
    return levels | {before[r][2] for r in rooms - grid.keys() if r in before}


def _text_sha(text: str | None) -> str | None:
    return _sha(text.encode()) if text is not None else None


def _builder_test(contents: dict[str, str], package: str, world: WorldDescription) -> str:
    """The generated test for the package as the given contents stand it."""
    return testgen.builder_test(package, world, _read_contents(contents, package))


def _kind_call(item: cst.BaseSmallStatement, kind: str) -> cst.Call | None:
    """The ``kind(...)`` call an expression statement makes, or None for anything else."""
    call = item.value if isinstance(item, cst.Expr) else None
    if not isinstance(call, cst.Call):
        return None
    func = call.func
    return call if isinstance(func, cst.Name) and func.value == kind else None


def _plain_string_arg(call: cst.Call) -> object:
    """The value of the call's first positional argument when it is a plain string literal."""
    first = call.args[0] if call.args else None
    if first is None or first.keyword is not None:
        return None
    value = first.value
    if not isinstance(value, cst.SimpleString):
        return None
    source = value.value
    # A prefixed string (f"", b"", r"") is not a plain literal.
    if not source or source[0].isalpha():
        return None
    return ast.literal_eval(source)


def _declares(item: cst.BaseSmallStatement, decl: DeclView) -> bool:
    call = _kind_call(item, decl.kind)
    return call is not None and _plain_string_arg(call) == decl.id


def _decl_source(file_text: str, decl: DeclView) -> str:
    """The declaration's source text, from its file."""
    try:
        module = cst.parse_module(file_text)
    except cst.ParserSyntaxError:
        return "(the file does not parse)"
    for statement in module.body:
        if not isinstance(statement, cst.SimpleStatementLine):
            continue
        if any(_declares(item, decl) for item in statement.body):
            return module.code_for_node(statement)
    return "(not found)"


def _package_exists(ctx: Context) -> bool:
    return any(p.path == ctx.package for p in ctx.world.packages)


def _has_decls(ctx: Context, kind: str) -> bool:
    """Whether the package declares anything of the kind, plainly or not."""
    return any(d.kind == kind for d in ctx.view.decls) or any(
        o.kind == kind for o in ctx.view.opaque
    )


_NOT_LOADED = "Fix these first (the package doesn't load); /reload when done."


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _star_names(directory: Path) -> list[str]:
    """The package's own .star files, chosen exactly as read_package chooses them.

    The REPL compares these hashes with the view's; any difference in the selection
    would rebuild the context (and call describe-world) on every command.
    """
    return _star_files(directory) if directory.is_dir() else []


def _read_texts(directory: Path) -> dict[str, str]:
    # Decoded from bytes so CRLF line endings survive a rewrite unchanged.
    return {
        name: (directory / name).read_bytes().decode("utf-8") for name in _star_names(directory)
    }


def _write_file(path: Path, text: str) -> None:
    """Write through a temp file and os.replace, so a crash never leaves half a file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def context_factory(
    world: Path, package: str, engine: Engine, llm: LlmClient, config: Config
) -> Callable[[], Context]:
    """A function that builds a fresh Context from the package's files and the engine.

    It raises StarParseError when a file does not parse; the REPL treats that as a
    package that does not load.
    """

    def factory() -> Context:
        pkg_dir = world / package
        view = (
            read_package(pkg_dir, package)
            if pkg_dir.is_dir()
            else PackageView(path=package, files={}, decls=[], opaque=[])
        )
        return Context(
            world_dir=world,
            world=engine.describe_world(world),
            api=engine.api(),
            package=package,
            view=view,
            files=_read_texts(pkg_dir),
            llm=llm,
            config=config,
            engine=engine,
        )

    return factory


class Repl:
    """One interactive session: read a line, act on it, save, repeat."""

    def __init__(
        self,
        session: Session,
        ctx_factory: Callable[[], Context],
        console: Console,
        read_line: Callable[[str], str],
    ) -> None:
        self.session = session
        self._factory = ctx_factory
        self.console = console
        self.read_line = read_line
        self._world = session.world
        self._pkg_dir = session.world / session.state.package
        self._ctx: Context | None = None
        self._load_errors: list[str] = []
        self._hashes: dict[str, str] = {}
        self._load()

    def run(self) -> int:
        self._say(
            f"Package {self.session.state.package} in {self.session.state.mode} mode. "
            "/help for commands."
        )
        while True:
            try:
                line = self.read_line("> ")
            except KeyboardInterrupt:
                continue
            except EOFError:
                line = "/quit"
            if not self.handle(line):
                return 0

    def handle(self, line: str) -> bool:
        """Act on one line; False means the session quits."""
        line = line.strip()
        if not line:
            return True
        if line.split()[0] == "/quit":
            self.session.save()
            return False
        try:
            if line.startswith("/"):
                self._slash(line)
            else:
                self._turn(line)
        except KeyboardInterrupt:
            self._say("cancelled")
        except (LlmError, StructuredCallFailed, EngineError) as error:
            self._say(f"error: {error}")
        self.session.save()
        return True

    def _say(self, text: str) -> None:
        # Everything printed is plain text: names, prose, diffs and the map's "[A]" cells
        # all contain brackets that rich would otherwise read as markup.
        self.console.print(text, markup=False, highlight=False)

    # -- the context --------------------------------------------------------

    def _load(self) -> None:
        """Rebuild the context; a file that doesn't parse or a failing engine leaves none."""
        try:
            ctx = self._factory()
        except StarParseError as error:
            self._fail([f"{error.file}:{error.line}: {error.message}"])
            return
        except EngineError as error:
            self._fail([str(error)])
            return
        self._ctx = ctx
        self._load_errors = []
        self._hashes = dict(ctx.view.files)

    def _fail(self, errors: list[str]) -> None:
        self._ctx = None
        self._load_errors = errors
        self._hashes = self._disk_hashes()

    def _current(self) -> Context | None:
        """The context for the files as they are now; None (and the errors printed) if broken."""
        if self._disk_hashes() != self._hashes:
            self._load()
        if self._ctx is None:
            for error in self._load_errors:
                self._say(error)
            self._say(_NOT_LOADED)
        return self._ctx

    def _disk_hashes(self) -> dict[str, str]:
        return {
            name: _sha((self._pkg_dir / name).read_bytes()) for name in _star_names(self._pkg_dir)
        }

    def _check_loaded(self, ctx: Context) -> bool:
        info = next((p for p in ctx.world.packages if p.path == ctx.package), None)
        if info is None or info.ok:
            return True
        for e in (e for e in ctx.world.errors if e.package == ctx.package):
            self._say(
                f"{e.file}:{e.line}: {e.message}"
                if e.line is not None
                else f"{e.file}: {e.message}"
            )
        self._say(_NOT_LOADED)
        return False

    # -- the model turn -----------------------------------------------------

    def _turn(self, message: str) -> None:
        # The model gets the message on its own, so the history it sees stops before it.
        history = self._history()
        self.session.state.history.append(("user", message))
        ctx = self._current()
        if ctx is None or not self._check_loaded(ctx):
            return
        mode = self.session.state.mode
        turns = {"area": self._area_turn, "mob": self._mob_turn, "skill": self._skill_turn}
        handler = turns.get(mode)
        if handler is None:
            self._say(f"{mode} mode is not implemented yet")
            return
        handler(ctx, message, history)

    def _history(self) -> list[Message]:
        return [
            Message("assistant" if role == "builder" else "user", content)
            for role, content in self.session.state.history
        ]

    def _created(self, ctx: Context, result: AreaBrief | MobBrief | SkillBrief | Proposal) -> None:
        """A mode's first answer: questions about the brief, or a proposal."""
        if isinstance(result, Proposal):
            self._offer(ctx, result, dict(result.grid or {}), {})
            return
        for q in result.questions:
            self._say(q)
        self.session.state.history.append(("builder", " ".join(result.questions)))

    def _reply(self, turn: Turn, message: str) -> None:
        """A turn that proposes nothing: its reply, and a note when it is for another mode."""
        self._say(turn.reply)
        if turn.kind == "out_of_scope":
            self.session.state.notes.append((turn.other_mode or "", turn.note or message))
            self._say(f"Noted for /mode {turn.other_mode}.")
        self.session.state.history.append(("builder", turn.reply))

    def _offer(
        self, ctx: Context, proposal: Proposal, grid: dict[str, Coord], before: dict[str, Coord]
    ) -> None:
        self._propose(ctx, proposal, grid, before)
        self.session.state.history.append(("builder", proposal.summary))

    def _area_turn(self, ctx: Context, message: str, history: list[Message]) -> None:
        if not _has_decls(ctx, "room"):
            self._created(ctx, create_area(ctx, message))
            return
        before, _ = place_existing(
            ctx.world.rooms_in(ctx.package), ctx.package, self.session.state.grid
        )
        # area_turn updates the coordinates it is given; hand it a copy so they reach the
        # session only when the proposal is written.
        grid = dict(self.session.state.grid)
        turn, proposal = area_turn(ctx, grid, message, history)
        if proposal is None:
            self._reply(turn, message)
        else:
            self._offer(ctx, proposal, grid, before)

    def _mob_turn(self, ctx: Context, message: str, history: list[Message]) -> None:
        # Mob mode edits a package that exists; a package of mobs alone is fine, since
        # other packages can spawn them.
        if not _package_exists(ctx):
            self._say(
                f"{ctx.package} does not exist; mob mode needs an existing package. "
                "Create it first with /mode area."
            )
            return
        if not _has_decls(ctx, "mob"):
            self._created(ctx, create_mobs(ctx, message))
            return
        turn, proposal = mob_turn(ctx, message, history)
        if proposal is None:
            self._reply(turn, message)
        else:
            self._offer(ctx, proposal, {}, {})

    def _skill_turn(self, ctx: Context, message: str, history: list[Message]) -> None:
        if not ctx.world.has_feature("skills"):
            self._say("This world has the skills feature off.")
            return
        # A package may be a skills or a zone package. One that doesn't exist is
        # allowed only under skills/: the first write creates its package.star.
        if not _package_exists(ctx) and not ctx.package.startswith("skills/"):
            self._say(
                f"{ctx.package} does not exist; skill mode needs an existing package. "
                "Create it first with /mode area."
            )
            return
        if not _has_decls(ctx, "skill"):
            self._created(ctx, create_skill(ctx, message))
            return
        turn, proposal = skill_turn(ctx, message, history)
        if proposal is None:
            self._reply(turn, message)
        else:
            self._offer(ctx, proposal, {}, {})

    def _propose(
        self,
        ctx: Context,
        proposal: Proposal,
        grid: dict[str, Coord],
        before: dict[str, Coord],
    ) -> None:
        self.session.state.pending = PendingChange(
            ops=proposal.ops,
            new_files=proposal.new_files,
            link_ops=proposal.link_ops,
            link_package=proposal.link_package,
            base_hashes=dict(ctx.view.files),
            summary=proposal.summary,
            notes=proposal.notes,
            grid=grid if proposal.grid is not None else None,
        )
        self._say(proposal.summary)
        for note in proposal.notes:
            self._say(note)
        self._print_proposal_map(ctx, proposal, grid, before)
        for label, fieldname, text in proposal.prose_preview:
            self._say(f"{label}.{fieldname}: {text}")

    def _print_proposal_map(
        self,
        ctx: Context,
        proposal: Proposal,
        grid: dict[str, Coord],
        before: dict[str, Coord],
    ) -> None:
        """Draw each z level that holds a room the proposal adds, moves, removes or edits."""
        if proposal.grid is None:
            return
        levels = _levels(_affected_rooms(proposal, grid, before), grid, before)
        if not levels:
            return
        post, names = self._post_grid(ctx, proposal, grid)
        for z in sorted(levels):
            self._say(render_map(post, z, names))

    def _post_grid(
        self, ctx: Context, proposal: Proposal, coords: dict[str, Coord]
    ) -> tuple[Grid, dict[str, str]]:
        """The grid and names of the package as it stands after the proposal."""
        result = apply_ops(ctx.files, ctx.view, proposal.ops)
        contents = {**ctx.files, **result.files, **proposal.new_files}
        view_after = _read_contents(contents, ctx.package)
        rooms = [d for d in view_after.decls if d.kind == "room"]
        room_ids = {d.id for d in rooms}
        grid_rooms = [GridRoom(id=d.id, coord=coords.get(d.id), external=False) for d in rooms]
        connections = _unique_passages(
            c for decl in rooms for c in _decl_passages(decl, ctx.package, room_ids)
        )
        grid = Grid(rooms=grid_rooms, connections=connections, portals=[])
        return grid, _room_names(rooms)

    # -- slash commands -----------------------------------------------------

    def _slash(self, line: str) -> None:
        parts = line.split()
        cmd = parts[0]
        arg = parts[1] if len(parts) > 1 else None
        with_arg: dict[str, Callable[[str | None], None]] = {
            "/map": self._map,
            "/show": self._show,
            "/mode": self._mode,
        }
        plain: dict[str, Callable[[], None]] = {
            "/diff": self._diff,
            "/write": self._write,
            "/discard": self._discard,
            "/undo": self._undo,
            "/reload": self._reload,
            "/notes": self._notes,
        }
        if cmd in with_arg:
            with_arg[cmd](arg)
        else:
            plain.get(cmd, self._help)()

    def _discard(self) -> None:
        if self.session.state.pending is None:
            self._say("nothing pending")
        else:
            self.session.state.pending = None
            self._say("discarded")

    def _map(self, z_arg: str | None) -> None:
        z: int | None = None
        if z_arg is not None:
            try:
                z = int(z_arg)
            except ValueError:
                self._say("usage: /map [z]")
                return
        ctx = self._current()
        if ctx is None:
            return
        rooms = ctx.world.rooms_in(ctx.package)
        coords, off_map = place_existing(rooms, ctx.package, self.session.state.grid)
        grid = grid_from_coords(ctx, coords)
        names = {room_id(r.label): r.name for r in rooms}
        levels = [z] if z is not None else sorted({c[2] for c in coords.values()})
        for level in levels:
            self._say(render_map(grid, level, names))
        if off_map:
            self._say("Off-map: " + ", ".join(off_map))

    def _show(self, id_arg: str | None) -> None:
        if id_arg is None:
            self._say("usage: /show <id>")
            return
        ctx = self._current()
        if ctx is None:
            return
        decl = next((d for d in ctx.view.decls if d.id == id_arg), None)
        if decl is None:
            self._say(f"no declaration named '{id_arg}'")
            return
        self._say(_decl_source(ctx.files[decl.file], decl))

    def _diff(self) -> None:
        pending = self.session.state.pending
        if pending is None:
            self._say("nothing pending; make a change first")
            return
        ctx = self._current()
        if ctx is None:
            return
        result = apply_ops(ctx.files, ctx.view, pending.ops)
        for refusal in result.refused:
            self._say(f"refused: {refusal.reason}")
        after = {**ctx.files, **result.files, **pending.new_files}
        before = dict(ctx.files)
        test_path = self._pkg_dir / "builder_test.star"
        if test_path.is_file():
            before["builder_test.star"] = test_path.read_bytes().decode("utf-8")
        builder = _builder_test(after, ctx.package, ctx.world)
        if builder:
            after["builder_test.star"] = builder
        diff = unified_diff(before, after)
        self._say(diff if diff else "(no changes)")

    def _write(self) -> None:
        pending = self.session.state.pending
        if pending is None:
            self._say("nothing pending; make a change first")
            return
        ctx = self._current()
        if ctx is None:
            return
        if pending.base_hashes != ctx.view.files:
            self._say("The files changed since the proposal; applying it to them as they are.")
        result = apply_ops(ctx.files, ctx.view, pending.ops)
        for refusal in result.refused:
            self._say(f"refused: {refusal.reason}")
        writes = self._package_writes(ctx, pending, result.files)
        writes.update(self._confirmed_link_writes(pending))
        contents = {**ctx.files, **result.files, **pending.new_files}
        test_hash = self._add_builder_test(ctx, contents, writes)
        writes = {path: text for path, text in writes.items() if self._changes(path, text)}
        if not writes:
            # Keep the last write's undo record; there is nothing new to undo.
            self._say("nothing to write")
            self.session.state.pending = None
            return
        self._commit(writes, pending, test_hash)
        self._say(f"wrote {len(writes)} file(s)")
        self._load()
        self._lint(ctx)

    def _package_writes(
        self, ctx: Context, pending: PendingChange, files: dict[str, str]
    ) -> dict[str, str]:
        """The package's changed files and its new ones, keyed by their path in the world."""
        writes = {f"{ctx.package}/{name}": text for name, text in files.items()}
        for name, text in pending.new_files.items():
            if (self._pkg_dir / name).exists():
                self._say(f"{name} now exists; left as it is")
            else:
                writes[f"{ctx.package}/{name}"] = text
        return writes

    def _confirmed_link_writes(self, pending: PendingChange) -> dict[str, str]:
        """The linked package's files, when the proposal has a link and the user agrees."""
        if not pending.link_ops or pending.link_package is None:
            return {}
        try:
            answer = self.read_line(f"Also add the return exit in {pending.link_package}? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            return {}
        return self._link_writes(pending.link_package, pending.link_ops)

    def _add_builder_test(
        self, ctx: Context, contents: dict[str, str], writes: dict[str, str]
    ) -> str | None:
        """Add the generated test to ``writes``; the builder test's hash after the write."""
        test_hash = self.session.state.builder_test_hash
        builder = _builder_test(contents, ctx.package, ctx.world)
        if not builder:
            return test_hash
        if not self._builder_test_is_ours(ctx):
            _write_file(self._pkg_dir / "builder_test.star.new", builder)
            self._say("builder_test.star was edited by hand; wrote builder_test.star.new")
            return test_hash
        writes[f"{ctx.package}/builder_test.star"] = builder
        return _sha(builder.encode())

    def _changes(self, path: str, text: str) -> bool:
        """Whether writing the text changes the file at the path in the world."""
        target = self._world / path
        return not target.is_file() or target.read_bytes() != text.encode("utf-8")

    def _disk_text(self, path: str) -> str | None:
        target = self._world / path
        return target.read_bytes().decode("utf-8") if target.is_file() else None

    def _commit(
        self, writes: dict[str, str], pending: PendingChange, test_hash: str | None
    ) -> None:
        """Write the files, keeping what they held before so /undo can restore it."""
        state = self.session.state
        state.undo = {path: self._disk_text(path) for path in writes}
        state.undo_grid = dict(state.grid)
        state.written_hashes = {path: _sha(text.encode()) for path, text in writes.items()}
        state.builder_test_hash = test_hash
        for path, text in writes.items():
            _write_file(self._world / path, text)
        if pending.grid is not None:
            state.grid = pending.grid
        state.pending = None

    def _builder_test_is_ours(self, ctx: Context) -> bool:
        """Whether builder_test.star on disk is the builder's own, safe to overwrite.

        It is when absent, when it matches the hash last written, or when it is exactly
        what the builder generates for the files as they are (written by another session).
        """
        path = self._pkg_dir / "builder_test.star"
        if not path.is_file():
            return True
        data = path.read_bytes()
        if self.session.state.builder_test_hash == _sha(data):
            return True
        return data.decode("utf-8", errors="replace") == _builder_test(
            ctx.files, ctx.package, ctx.world
        )

    def _link_writes(self, link_package: str, link_ops: list[StarOp]) -> dict[str, str]:
        """The linked package's changed files, keyed by their path in the world."""
        link_dir = self._world / link_package
        try:
            view = (
                read_package(link_dir, link_package)
                if link_dir.is_dir()
                else PackageView(path=link_package, files={}, decls=[], opaque=[])
            )
        except StarParseError as error:
            self._say(f"refused (link): {error}")
            return {}
        result = apply_ops(_read_texts(link_dir), view, link_ops)
        for refusal in result.refused:
            self._say(f"refused (link): {refusal.reason}")
        if result.files:
            self._say(f"adding the return exit in {link_package}")
        return {f"{link_package}/{name}": text for name, text in result.files.items()}

    def _lint(self, ctx: Context) -> None:
        findings = ctx.engine.lint(ctx.package)
        if not findings:
            self._say("lint: clean")
        for f in findings:
            where = f"{f.file}:{f.line}" if f.line is not None else f.file
            self._say(f"lint {f.severity}: {where}: {f.message}")

    def _undo(self) -> None:
        state = self.session.state
        if state.undo is None:
            self._say("nothing to undo")
            return
        if self._changed_since_write(state):
            return
        self._restore(state.undo)
        test_key = f"{state.package}/builder_test.star"
        if test_key in state.undo:
            state.builder_test_hash = _text_sha(state.undo[test_key])
        if state.undo_grid is not None:
            state.grid = state.undo_grid
        state.undo = None
        state.undo_grid = None
        state.written_hashes = {}
        self._say("undone")
        ctx = self._current()
        if ctx is not None:
            self._lint(ctx)

    def _changed_since_write(self, state: SessionState) -> bool:
        """Whether a file the last write wrote has changed since; says which."""
        for path, digest in state.written_hashes.items():
            target = self._world / path
            if not target.is_file() or _sha(target.read_bytes()) != digest:
                self._say(f"{path} changed since the last write; /undo would lose those changes")
                return True
        return False

    def _restore(self, undo: dict[str, str | None]) -> None:
        """Put back what the files held before the write; a file it created is removed."""
        for path, content in undo.items():
            target = self._world / path
            if content is not None:
                _write_file(target, content)
            elif target.is_file():
                target.unlink()

    def _reload(self) -> None:
        old = dict(self._hashes)
        self._load()
        new = self._hashes
        changed = [n for n in sorted(set(old) | set(new)) if old.get(n) != new.get(n)]
        self._say(
            "reloaded; changed: " + ", ".join(changed) if changed else "reloaded; nothing changed"
        )
        if self._ctx is None:
            for error in self._load_errors:
                self._say(error)
            self._say(_NOT_LOADED)

    def _mode(self, mode: str | None) -> None:
        if mode not in ("area", "mob", "skill"):
            self._say("usage: /mode area|mob|skill")
            return
        self.session.state.mode = cast(Mode, mode)
        self._say(f"mode is now {mode}")
        for m, note in self.session.state.notes:
            if m == mode:
                self._say(note)

    def _notes(self) -> None:
        if not self.session.state.notes:
            self._say("no notes")
            return
        for m, note in self.session.state.notes:
            self._say(f"[{m}] {note}")

    def _help(self) -> None:
        self._say(
            "/map [z]  show the room map\n"
            "/show <id>  show a declaration's source\n"
            "/diff  show the pending change as a diff\n"
            "/write  write the pending change to the files\n"
            "/discard  drop the pending change\n"
            "/undo  undo the last write\n"
            "/reload  re-read the world and the package\n"
            "/mode area|mob|skill  switch the mode\n"
            "/notes  show the noted requests\n"
            "/help  this help\n"
            "/quit  leave"
        )
