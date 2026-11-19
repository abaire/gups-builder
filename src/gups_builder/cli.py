"""Argument parsing and the subcommands of gups-builder."""
# ruff: noqa: T201

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING

from prompt_toolkit import PromptSession
from prompt_toolkit.history import FileHistory
from pydantic import BaseModel, ValidationError
from rich.console import Console

from gups_builder.area_tui import run_area_tui
from gups_builder.config import Config, RoleConfig, load_config
from gups_builder.engine.gups import EngineError, Gups
from gups_builder.llm.client import HttpLlm, LlmError, Message, NoLlm
from gups_builder.llm.log import CallLog
from gups_builder.models_tui import run_models_tui
from gups_builder.modes.design import (
    AreaDesign,
    apply_design,
    check_design,
    design_rules,
    world_facts,
    write_design,
)
from gups_builder.repl import Repl, context_factory, session_log_path
from gups_builder.session import Mode, Session, SessionState, ensure_builder_dir
from gups_builder.star.reader import StarParseError

if TYPE_CHECKING:
    from collections.abc import Callable

    from gups_builder.modes.common import Context

_CLI_FLAGS = ("logic_url", "logic_model", "prose_url", "prose_model", "gups")


def _build_cli_dict(args: argparse.Namespace) -> dict[str, str]:
    cli: dict[str, str] = {}
    for key in _CLI_FLAGS:
        value = getattr(args, key)
        if value is not None:
            cli[key] = value
    return cli


def _format_role(name: str, rc: RoleConfig) -> str:
    model = rc.model if rc.model else "(not set)"
    api = "set" if rc.api_key else "not set"
    return (
        f"{name}: model={model} url={rc.base_url} api_key={api} "
        f"structured={rc.structured} temperature={rc.temperature}"
    )


def _make_client(config: Config) -> HttpLlm:
    return HttpLlm(config)


class _WordReply(BaseModel):
    word: str


def _refusal_reason(error: Exception) -> str:
    if isinstance(error, LlmError):
        text = str(error)
        if "got HTTP" in text:
            status = text.split("got HTTP", 1)[1].strip().split(":", 1)[0].strip()
            url = text.split(" request to ", 1)[1].split(" got HTTP", 1)[0]
            return f"{status} from {url}"
        if "is not configured" in text:
            return "the model is not configured"
    return str(error)


def _run_models(args: argparse.Namespace) -> int:
    cli = _build_cli_dict(args)
    world = Path(args.world)
    home = Path.home()
    if not args.check and sys.stdin.isatty() and sys.stdout.isatty():
        return run_models_tui(world, cli, os.environ, home, _make_client)
    if not args.check:
        print("No terminal attached, falling back to --check mode")

    config: Config = load_config(world, cli, os.environ, home)
    print(_format_role("logic", config.logic))
    print(_format_role("prose", config.prose))
    gups = config.gups if config.gups is not None else "(not set)"
    print(f"gups: {gups}")
    print(f"max_repairs: {config.max_repairs}")

    llm = _make_client(config)
    exit_code = 0
    for role in ("logic", "prose"):
        rc = config.logic if role == "logic" else config.prose
        started = time.monotonic()
        try:
            llm.complete(role, [Message("system", "Reply with the single word OK.")], None)
        except LlmError as error:
            print(f"{role}: FAILED: {_refusal_reason(error)}")
            exit_code = 1
            continue
        print(f"{role}: OK ({time.monotonic() - started:.1f} s)")
        if rc.structured == "json_schema":
            try:
                reply = llm.complete(
                    role,
                    [Message("user", "Reply with the word OK.")],
                    _WordReply.model_json_schema(),
                )
                # A 400 that the client absorbed by retrying as json_object
                # still means this server refuses json_schema.
                if llm.last_fallback:
                    print(
                        f"{role}: json_schema not honoured (400 from {rc.base_url}); "
                        'set structured = "json_object"'
                    )
                    continue
                _WordReply.model_validate_json(reply)
            except ValidationError:
                print(
                    f"{role}: json_schema not honoured (reply is not valid JSON); "
                    'set structured = "json_object"'
                )
            except LlmError as error:
                print(
                    f"{role}: json_schema not honoured ({_refusal_reason(error)}); "
                    'set structured = "json_object"'
                )
            else:
                print(f"{role}: json_schema OK")
    return exit_code


def toolbar_text(state: SessionState, config: Config) -> str:
    """The prompt's bottom toolbar; it ends with the pending proposal's summary, if any."""
    planning = " (plan)" if state.planning else ""
    logic = config.logic.model or "(not set)"
    prose = config.prose.model or "(not set)"
    pending = f" | {state.pending.summary}" if state.pending else ""
    return (
        f" gups-builder | mode: {state.mode}{planning} | pkg: {state.package} | "
        f"logic: {logic} | prose: {prose}{pending} "
    )


def _make_read_line(session: Session, config: Config, history_path: Path) -> Callable[[str], str]:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    return PromptSession(
        history=FileHistory(str(history_path)),
        bottom_toolbar=lambda: toolbar_text(session.state, config),
    ).prompt


def _start_repl(world: Path, config: Config, session: Session) -> int:
    try:
        binary = Gups.find(config.gups, os.environ)
    except EngineError as error:
        print(str(error))
        return 1
    gups = Gups(binary, world)
    console = Console()
    llm = HttpLlm(config, CallLog(session_log_path(world, session.state.name)))
    ctx_factory = context_factory(world, session.state.package, gups, llm, config)
    return Repl(
        session,
        ctx_factory,
        console,
        _make_read_line(session, config, world / ".builder" / "history"),
    ).run()


def _require_terminal() -> bool:
    if sys.stdin.isatty() and sys.stdout.isatty():
        return True
    print("Error: gups-builder requires an interactive terminal.")
    return False


def _run_mode(args: argparse.Namespace, mode: Mode) -> int:
    if not _require_terminal():
        return 1
    world = Path(args.world)
    config = load_config(world, _build_cli_dict(args), os.environ, Path.home())
    package = args.package
    if package is None:
        if mode == "area":
            package = run_area_tui(world)
            if package is None:
                return 0
        else:
            package = input("Package path (e.g. zones/mansion): ").strip()
    if not package:
        print("no package given")
        return 1
    # Area mode creates a package; skill mode may start one under skills/; the rest
    # edit a package that is already there.
    if (
        mode != "area"
        and not (world / package / "package.star").is_file()
        and not (mode == "skill" and package.startswith("skills/"))
    ):
        print(
            f"{package} does not exist; {mode} mode needs an existing package. "
            f"Create it first with: gups-builder area {package}"
        )
        return 1
    wanted = args.name if args.name is not None else package.rsplit("/", 1)[-1]
    session: Session | None = None
    # Only a session on the same package is offered: zones/a/mansion and zones/b/mansion
    # share a default name but are different work.
    if (
        Session.path(world, wanted).exists()
        and Session.load(world, wanted).state.package == package
        and input(f"A session named '{wanted}' exists. Resume it? [y/N] ").strip().lower()
        in ("y", "yes")
    ):
        session = Session.load(world, wanted)
    if session is None:
        session = Session.create(world, mode, package, wanted)
        if session.state.name != wanted:
            print(f"Starting a new session named '{session.state.name}'.")
    return _start_repl(world, config, session)


def _run_resume(args: argparse.Namespace) -> int:
    if not _require_terminal():
        return 1
    world = Path(args.world)
    config = load_config(world, _build_cli_dict(args), os.environ, Path.home())
    if not Session.path(world, args.session).exists():
        print(f"no session named '{args.session}'; gups-builder sessions lists them")
        return 1
    return _start_repl(world, config, Session.load(world, args.session))


def _run_sessions(args: argparse.Namespace) -> int:
    sessions_dir = Path(args.world) / ".builder" / "sessions"
    if not sessions_dir.is_dir():
        print("no sessions")
        return 0
    for path in sorted(sessions_dir.glob("*.json")):
        state = SessionState.model_validate_json(path.read_text())
        print(f"{state.name}  {state.mode}  {state.package}  {state.updated}")
    return 0


# Exit codes of the model-free commands: clean, a design or lint problem, a usage or engine error.
_CLEAN, _PROBLEMS, _ERROR = 0, 1, 2


def _print_json(data: object) -> None:
    print(json.dumps(data, indent=2))


def _design_context(args: argparse.Namespace) -> Context:
    """A context on the package with no model behind it; raises EngineError or StarParseError."""
    world = Path(args.world)
    config = load_config(world, _build_cli_dict(args), os.environ, Path.home())
    gups = Gups(Gups.find(config.gups, os.environ), world)
    return context_factory(world, args.package, gups, NoLlm(), config)()


def _run_world_info(args: argparse.Namespace) -> int:
    ensure_builder_dir(Path(args.world))
    try:
        ctx = _design_context(args)
    except (EngineError, StarParseError) as error:
        _print_json({"error": str(error)})
        return _ERROR
    _print_json(world_facts(ctx))
    return _CLEAN


def _load_design(path: str) -> AreaDesign | str:
    """The design file, or why it could not be read."""
    try:
        return AreaDesign.model_validate_json(Path(path).read_text(encoding="utf-8"))
    except OSError as error:
        return f"cannot read {path}: {error}"
    except ValidationError as error:
        return f"{path} is not a valid design: {error}"


def _run_apply(args: argparse.Namespace) -> int:
    # Design files are kept under .builder/designs/; the first use keeps all of it out of git.
    ensure_builder_dir(Path(args.world))
    design = _load_design(args.design)
    if isinstance(design, str):
        _print_json({"error": design})
        return _ERROR
    try:
        ctx = _design_context(args)
        if args.check_only:
            report = check_design(ctx, design)
            _print_json(report.to_json())
            return _PROBLEMS if report.problems else _CLEAN
        result = apply_design(ctx, design)
        out = result.to_json()
        if args.write and result.ok:
            written = write_design(ctx, result, with_link=args.link)
            out["written"] = written.files
            out["notes"] = [*out["notes"], *written.notes]
            out["lint_after_write"] = [
                {"severity": f.severity, "file": f.file, "line": f.line, "message": f.message}
                for f in written.lint
            ]
    except (EngineError, StarParseError) as error:
        _print_json({"error": str(error)})
        return _ERROR
    _print_json(out)
    lint_errors = any(f["severity"] == "error" for f in out.get("lint_after_write", []))
    return _CLEAN if result.ok and not lint_errors else _PROBLEMS


def _add_design_commands(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    """The commands that apply a design written outside the builder; they call no model."""
    info = sub.add_parser("world-info", help="print what a designer needs to know, as JSON")
    info.add_argument("package")
    sub.add_parser("design-schema", help="print the design file's schema and rules, as JSON")
    apply = sub.add_parser("apply", help="check, stage and optionally write a design file")
    apply.add_argument("package")
    apply.add_argument("design")
    apply.add_argument("--check-only", action="store_true", help="check without staging")
    apply.add_argument("--write", action="store_true", help="write the files when clean")
    apply.add_argument("--link", action="store_true", help="also write the link's return exit")


def _run_design_schema(_args: argparse.Namespace) -> int:
    _print_json(design_rules())
    return _CLEAN


_HANDLERS: dict[str, Callable[[argparse.Namespace], int]] = {
    "models": _run_models,
    "area": lambda args: _run_mode(args, "area"),
    "mob": lambda args: _run_mode(args, "mob"),
    "skill": lambda args: _run_mode(args, "skill"),
    "resume": _run_resume,
    "sessions": _run_sessions,
    "world-info": _run_world_info,
    "design-schema": _run_design_schema,
    "apply": _run_apply,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="gups-builder")
    parser.add_argument("--world", default=".")
    parser.add_argument("--gups", default=None)
    parser.add_argument("--logic-url", default=None)
    parser.add_argument("--logic-model", default=None)
    parser.add_argument("--prose-url", default=None)
    parser.add_argument("--prose-model", default=None)
    sub = parser.add_subparsers(dest="command")

    for name in ("area", "mob", "skill"):
        p = sub.add_parser(name)
        p.add_argument("package", nargs="?")
        p.add_argument("--name", default=None)

    resume = sub.add_parser("resume")
    resume.add_argument("session")
    sub.add_parser("sessions")

    models_parser = sub.add_parser("models")
    models_parser.add_argument(
        "--check",
        action="store_true",
        help="print configuration and probe models without opening the interactive TUI",
    )

    _add_design_commands(sub)

    args = parser.parse_args(argv)

    handler = _HANDLERS.get(args.command)
    if handler is not None:
        return handler(args)
    parser.print_help()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
