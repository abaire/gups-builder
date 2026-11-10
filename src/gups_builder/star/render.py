"""Render Values and declarations as Starlark text in the template's style.

A declaration is its kind and id, then one ``name = value,`` per line indented
four spaces. A list holds more than one call, or its one-line form runs past 80
characters, one item per line with a trailing comma; otherwise it stays on one
line. Strings are double-quoted with only the backslash, the quote and a
newline escaped; every other character, including non-ASCII ones, is written
literally as UTF-8.
"""

from __future__ import annotations

import keyword
import math
import re

from gups_builder.star.values import Value

LINE_LIMIT = 80

# The reader's $const form: an attribute of an uppercase name.
_CONST = re.compile(r"[A-Z_][A-Z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*")


def _identifier(name: object) -> str:
    """The name, when it can stand as a callee or keyword; ValueError otherwise."""
    if not isinstance(name, str) or not name.isidentifier() or keyword.iskeyword(name):
        raise ValueError(f"not a Starlark identifier: {name!r}")
    return name


def render_string(text: str) -> str:
    out = []
    for char in text:
        if char == "\\":
            out.append("\\\\")
        elif char == '"':
            out.append('\\"')
        elif char == "\n":
            out.append("\\n")
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


def render_value(value: Value, indent: int) -> str:
    """The source text of a Value on a line indented ``indent`` spaces."""
    if value is None:
        return "None"
    if value is True:
        return "True"
    if value is False:
        return "False"
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"{value!r} has no Starlark literal")
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, str):
        return render_string(value)
    if isinstance(value, list):
        return _render_list(value, indent)
    if isinstance(value, dict):
        if "$const" in value:
            const = value["$const"]
            if not isinstance(const, str) or not _CONST.fullmatch(const):
                raise ValueError(f"not a constant: {const!r}")
            return const
        if "$dict" in value:
            pairs = value["$dict"]
            if not isinstance(pairs, list):
                raise ValueError(f"not a Value: {value!r}")
            return _render_dict(pairs, indent)
        if "$call" in value:
            return _render_call(value, indent)
        if "$ref" in value or "$opaque" in value:
            raise ValueError("a $ref or $opaque value cannot be written")
        raise ValueError(f"not a Value: {value!r}")
    raise ValueError(f"not a Value: {value!r}")


def _is_call(value: Value) -> bool:
    return isinstance(value, dict) and value.get("$call") is not None


def _list_multiline(items: list[Value], indent: int) -> bool:
    oneline = "[" + ", ".join(render_value(item, indent + 4) for item in items) + "]"
    return sum(1 for item in items if _is_call(item)) >= 2 or len(oneline) > LINE_LIMIT


def _render_list(items: list[Value], indent: int) -> str:
    if not items:
        return "[]"
    if not _list_multiline(items, indent):
        return "[" + ", ".join(render_value(item, indent + 4) for item in items) + "]"
    lines = ["["]
    lines += [" " * (indent + 4) + render_value(item, indent + 4) + "," for item in items]
    lines.append(" " * indent + "]")
    return "\n".join(lines)


def _render_dict(pairs: list[Value], indent: int) -> str:
    parts = []
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ValueError(f"not a key/value pair: {pair!r}")
        key, value = pair
        parts.append(render_value(key, indent) + ": " + render_value(value, indent))
    return "{" + ", ".join(parts) + "}"


def _callee(name: object) -> str:
    """A callee name: an identifier, or a plugin primitive's dotted builtin name."""
    if not isinstance(name, str):
        raise ValueError(f"not a Starlark identifier: {name!r}")
    for segment in name.split("."):
        _identifier(segment)
    return name


def _render_call(value: dict[str, Value], indent: int) -> str:
    name = _callee(value["$call"])
    args = value.get("args", [])
    kwargs = value.get("kwargs", {})
    if not isinstance(args, list) or not isinstance(kwargs, dict):
        raise ValueError(f"not a Value: {value!r}")
    rendered = [render_value(arg, indent + 4) for arg in args]
    rendered += [
        _identifier(key) + " = " + render_value(item, indent + 4) for key, item in kwargs.items()
    ]
    if any("\n" in part for part in rendered):
        lines = [name + "("]
        lines += [" " * (indent + 4) + part + "," for part in rendered]
        lines.append(" " * indent + ")")
        return "\n".join(lines)
    return name + "(" + ", ".join(rendered) + ")"


def comment_lines(text: str, first: str, rest: str) -> list[str]:
    """Text as comment lines: ``first`` before its first line, ``rest`` before the others.

    A newline inside a comment would end it and leave the rest as code.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    return [((first if i == 0 else rest) + line).rstrip() for i, line in enumerate(lines)]


def render_decl(kind: str, id: str, args: dict[str, Value], comment: str | None) -> str:
    """A new declaration, ending in a newline, in the template's style."""
    lines = []
    if comment is not None:
        lines += comment_lines(comment, "# ", "# ")
    if not args:
        lines.append(f"{kind}({render_string(id)})")
    else:
        lines.append(f"{kind}({render_string(id)},")
        lines += [f"    {name} = {render_value(value, 4)}," for name, value in args.items()]
        lines.append(")")
    return "\n".join(lines) + "\n"
