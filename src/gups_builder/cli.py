"""Argument parsing and the subcommands of gups-builder."""

from __future__ import annotations

import argparse
import os
import time
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel, ValidationError
from rich.console import Console

from .config import Config, RoleConfig, load_config
from .engine.gups import EngineError, Gups
from .llm.client import HttpLlm, LlmError, Message
from .llm.log import CallLog
from .repl import Repl, context_factory
from .session import Mode, Session, SessionState

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
    config: Config = load_config(Path(args.world), cli, os.environ, Path.home())
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
        except Exception as error:
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
            except Exception as error:
                print(
                    f"{role}: json_schema not honoured ({_refusal_reason(error)}); "
                    'set structured = "json_object"'
                )
            else:
                print(f"{role}: json_schema OK")
    return exit_code


def _make_read_line(history_path: Path) -> Callable[[str], str]:
    from prompt_toolkit import PromptSession
    from prompt_toolkit.history import FileHistory

    history_path.parent.mkdir(parents=True, exist_ok=True)
    return PromptSession(history=FileHistory(str(history_path))).prompt


def _start_repl(world: Path, config: Config, session: Session) -> int:
    try:
        binary = Gups.find(config.gups, os.environ)
    except EngineError as error:
        print(str(error))
        return 1
    gups = Gups(binary, world)
    llm = HttpLlm(config, CallLog(world / ".builder" / "logs" / f"{session.state.name}.jsonl"))
    ctx_factory = context_factory(world, session.state.package, gups, llm, config)
    return Repl(
        session, ctx_factory, Console(), _make_read_line(world / ".builder" / "history")
    ).run()


def _run_mode(args: argparse.Namespace, mode: Mode) -> int:
    world = Path(args.world)
    config = load_config(world, _build_cli_dict(args), os.environ, Path.home())
    package = args.package
    if package is None:
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
    sub.add_parser("models")

    args = parser.parse_args(argv)

    if args.command == "models":
        return _run_models(args)
    if args.command in ("area", "mob", "skill"):
        return _run_mode(args, args.command)
    if args.command == "resume":
        return _run_resume(args)
    if args.command == "sessions":
        return _run_sessions(args)
    parser.print_help()
    return 0
