"""Unified diffs between two file maps, for the user's review before a write."""

from __future__ import annotations

import difflib


def unified_diff(before: dict[str, str], after: dict[str, str]) -> str:
    """One unified diff per changed file, files in name order.

    A file only in ``after`` is shown against ``/dev/null`` as a creation, and
    one only in ``before`` shows as a deletion.
    """
    chunks = []
    for name in sorted(set(before) | set(after)):
        old, new = before.get(name, ""), after.get(name, "")
        if old == new:
            continue
        chunks.append(
            "\n".join(
                difflib.unified_diff(
                    old.splitlines(),
                    new.splitlines(),
                    fromfile=name if name in before else "/dev/null",
                    tofile=name if name in after else "/dev/null",
                    lineterm="",
                )
            )
        )
    return "\n".join(chunks)
