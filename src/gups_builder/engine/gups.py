"""Finding and running the `gups` binary."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import ValidationError

from .api import ApiModel
from .lint import LintFinding, parse_github
from .world import WorldDescription, parse_describe_world

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
            raise EngineError(f"{source} names {candidate}, which is missing or not executable")
        path_env = env.get("PATH", "")
        found = shutil.which("gups", path=path_env) if path_env else None
        if found is not None:
            return Path(found)
        raise EngineError(
            "no gups binary found: set 'gups' in the config, GUPS_BIN, or install it on PATH"
        )

    def _run(
        self, subcommand: str, *args: str, world: Path | None = None
    ) -> subprocess.CompletedProcess[str]:
        # --world only exists after the subcommand name.
        cmd = [str(self.binary), subcommand]
        if world is not None:
            cmd += ["--world", str(world)]
        cmd += list(args)
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=_TIMEOUT_S)
        except FileNotFoundError as error:
            raise EngineError(f"gups binary {self.binary} is missing: {error}") from error
        except subprocess.TimeoutExpired as error:
            raise EngineError(f"gups {' '.join(cmd)} timed out after {_TIMEOUT_S:g} s") from error

    def describe_world(self, world: Path | None = None) -> WorldDescription:
        process = self._run("describe-world", world=world or self.world)
        if process.returncode != 0:
            raise EngineError(f"gups describe-world failed: {process.stderr.strip()}")
        return parse_describe_world(process.stdout)

    def api(self) -> ApiModel:
        # docs --json refuses --world, so it runs with no world flag at all.
        if self._api is None:
            process = self._run("docs", "--json")
            if process.returncode != 0:
                raise EngineError(f"gups docs --json failed: {process.stderr.strip()}")
            try:
                self._api = ApiModel.model_validate_json(process.stdout)
            except ValidationError as error:
                raise EngineError(
                    f"gups docs --json wrote an unusable document: {error}"
                ) from error
        return self._api

    def lint(self, package: str, world: Path | None = None) -> list[LintFinding]:
        process = self._run(
            "lint", "--format", "github", f"//{package}/...", world=world or self.world
        )
        findings = parse_github(process.stdout)
        # A failure with nothing to report is lint not running at all (a bad flag or
        # world), which must not pass for a clean package.
        if process.returncode != 0 and not findings:
            raise EngineError(f"gups lint failed: {process.stderr.strip()}")
        return findings

    def test(self, package: str, world: Path | None = None) -> TestRun:
        process = self._run("test", f"//{package}/...", world=world or self.world)
        return TestRun(ok=process.returncode == 0, output=process.stdout + process.stderr)
