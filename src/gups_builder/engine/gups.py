"""Finding and running the `gups` binary."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from gups_builder.engine.api import ApiModel
from gups_builder.engine.lint import LintFinding, parse_github
from gups_builder.engine.world import WorldDescription, parse_describe_world

if TYPE_CHECKING:
    from collections.abc import Mapping

_TIMEOUT_S = 120.0


class EngineError(Exception):
    """A gups command could not be found, failed, or produced unusable output."""


@dataclass(frozen=True)
class TestRun:
    ok: bool
    output: str  # combined stdout and stderr


class Gups:
    def __init__(self, binary: Path, world: Path) -> None:
        self.binary = binary
        self.world = world
        self._api: ApiModel | None = None

    @staticmethod
    def find(config_path: str | None, env: Mapping[str, str]) -> Path:
        """The config path, then GUPS_BIN, then gups on PATH; EngineError if none.

        A path the user named explicitly must work: falling through to another
        gups would run an engine they did not choose.
        """
        for source, candidate in (
            ("the gups setting", config_path),
            ("GUPS_BIN", env.get("GUPS_BIN")),
        ):
            if candidate is None:
                continue
            path = Path(candidate)
            if path.is_file() and os.access(candidate, os.X_OK):
                return path
            msg = f"{source} names {candidate}, which is missing or not executable"
            raise EngineError(msg)
        path_env = env.get("PATH", "")
        found = shutil.which("gups", path=path_env) if path_env else None
        if found is not None:
            return Path(found)
        msg = "no gups binary found: set 'gups' in the config, GUPS_BIN, or install it on PATH"
        raise EngineError(msg)

    def _run(
        self, subcommand: str, *args: str, world: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        # --world only exists after the subcommand name.
        cmd = [str(self.binary), subcommand]
        if world is not None:
            cmd += ["--world", str(world)]
        cmd += list(args)
        try:
            return subprocess.run(
                cmd, check=False, capture_output=True, text=True, timeout=_TIMEOUT_S
            )
        except FileNotFoundError as error:
            msg = f"gups binary {self.binary} is missing: {error}"
            raise EngineError(msg) from error
        except subprocess.TimeoutExpired as error:
            msg = f"gups {' '.join(cmd)} timed out after {_TIMEOUT_S:g} s"
            raise EngineError(msg) from error

    def describe_world(self, world: Path | None = None) -> WorldDescription:
        process = self._run("describe-world", world=world or self.world)
        if process.returncode != 0:
            msg = f"gups describe-world failed: {process.stderr.strip()}"
            raise EngineError(msg)
        return parse_describe_world(process.stdout)

    def api(self) -> ApiModel:
        # docs --json refuses --world, so it runs with no world flag at all.
        if self._api is None:
            process = self._run("docs", "--json")
            if process.returncode != 0:
                msg = f"gups docs --json failed: {process.stderr.strip()}"
                raise EngineError(msg)
            try:
                self._api = ApiModel.model_validate_json(process.stdout)
            except ValidationError as error:
                msg = f"gups docs --json wrote an unusable document: {error}"
                raise EngineError(msg) from error
        return self._api

    def lint(self, package: str, world: Path | None = None) -> list[LintFinding]:
        process = self._run(
            "lint", "--format", "github", f"//{package}/...", world=world or self.world
        )
        findings = parse_github(process.stdout)
        # A failure with nothing to report is lint not running at all (a bad flag or
        # world), which must not pass for a clean package.
        if process.returncode != 0 and not findings:
            msg = f"gups lint failed: {process.stderr.strip()}"
            raise EngineError(msg)
        return findings

    def test(self, package: str, world: Path | None = None) -> TestRun:
        process = self._run("test", f"//{package}/...", world=world or self.world)
        return TestRun(ok=process.returncode == 0, output=process.stdout + process.stderr)
