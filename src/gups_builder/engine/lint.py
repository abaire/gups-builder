"""Parsing the GitHub-annotation lines that `gups lint --format github` writes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal, cast

_ANNOTATION = re.compile(
    r"^::(?P<severity>error|warning) file=(?P<file>[^,]+)"
    r"(?:,line=(?P<line>\d+))?,title=gups lint::(?P<message>.*)$"
)


@dataclass(frozen=True)
class LintFinding:
    severity: Literal["error", "warning"]
    file: str  # repo-relative, e.g. "zones/mansion/rooms.star"
    line: int | None
    message: str


def _unescape(message: str) -> str:
    # The inverse of the engine's %25 / %0D / %0A escapes.
    return message.replace("%0A", "\n").replace("%0D", "\r").replace("%25", "%")


def parse_github(output: str) -> list[LintFinding]:
    findings: list[LintFinding] = []
    for line in output.splitlines():
        match = _ANNOTATION.match(line)
        if match is None:
            continue
        line_no = match.group("line")
        findings.append(
            LintFinding(
                cast("Literal['error', 'warning']", match.group("severity")),
                match.group("file"),
                int(line_no) if line_no is not None else None,
                _unescape(match.group("message")),
            )
        )
    return findings
