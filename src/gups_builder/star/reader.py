"""Read a package directory into a PackageView with libcst.

Starlark is a syntactic subset of Python, so a package's files parse with
``cst.parse_module``. The view separates plain declarations the builder may
edit from everything else, which stays opaque.
"""

from __future__ import annotations

import ast
import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import libcst as cst
from libcst.metadata import CodeRange, MetadataWrapper, PositionProvider

from gups_builder.star.values import Value, to_value

DECLARATION_KINDS = frozenset({"room", "mob", "item", "skill", "faction", "region", "container"})

_SKIP_FILENAMES = ("package.star",)
_SKIP_SUFFIXES = ("_test.star", "_testdata.star")


class StarParseError(Exception):
    """A .star file in the package does not parse."""

    def __init__(self, file: str, line: int, message: str) -> None:
        super().__init__(f"{file}:{line}: {message}")
        self.file = file
        self.line = line
        self.message = message


@dataclass(frozen=True)
class DeclView:
    kind: str
    id: str
    file: str
    line: int
    args: dict[str, Value]
    extra_positional: list[Value]


@dataclass(frozen=True)
class OpaqueDecl:
    kind: str
    id: str | None
    file: str
    line: int
    reason: str


@dataclass(frozen=True)
class PackageView:
    path: str
    files: dict[str, str]
    decls: list[DeclView]
    opaque: list[OpaqueDecl]

    def get(self, kind: str, id: str) -> DeclView | None:
        for decl in self.decls:
            if decl.kind == kind and decl.id == id:
                return decl
        return None


def read_package(pkg_dir: Path, package_path: str) -> PackageView:
    files: dict[str, str] = {}
    decls: list[DeclView] = []
    opaque: list[OpaqueDecl] = []
    for name in _star_files(pkg_dir):
        raw = (pkg_dir / name).read_bytes()
        files[name] = hashlib.sha256(raw).hexdigest()
        try:
            module = cst.parse_module(raw.decode("utf-8"))
        except cst.ParserSyntaxError as error:
            raise StarParseError(name, error.editor_line, error.message) from error
        wrapper = MetadataWrapper(module)
        # The wrapper re-parses into its own nodes; use its module and the
        # matching position mapping, never the original.
        parsed = wrapper.module
        positions = wrapper.resolve(PositionProvider)
        file_decls, file_opaque = _declarations(parsed, positions, name)
        decls.extend(file_decls)
        opaque.extend(file_opaque)
    return PackageView(path=package_path, files=files, decls=decls, opaque=opaque)


def _star_files(pkg_dir: Path) -> list[str]:
    return sorted(
        entry.name
        for entry in pkg_dir.iterdir()
        if entry.is_file()
        and entry.name.endswith(".star")
        and entry.name not in _SKIP_FILENAMES
        and not entry.name.endswith(_SKIP_SUFFIXES)
    )


def _declarations(
    module: cst.Module,
    positions: Mapping[cst.CSTNode, CodeRange],
    name: str,
) -> tuple[list[DeclView], list[OpaqueDecl]]:
    top_level = _top_level_calls(module)
    collector = _CallCollector()
    module.visit(collector)
    decls: list[DeclView] = []
    opaque: list[OpaqueDecl] = []
    for call in collector.calls:
        func = call.func
        if not isinstance(func, cst.Name):
            continue
        line = positions[call].start.line
        if call in top_level:
            decl_id = _first_literal_id(call)
            if decl_id is None:
                opaque.append(
                    OpaqueDecl(
                        kind=func.value,
                        id=None,
                        file=name,
                        line=line,
                        reason="id is not a string literal",
                    )
                )
                continue
            args: dict[str, Value] = {}
            extra_positional: list[Value] = []
            for index, arg in enumerate(call.args):
                value = to_value(arg.value, module)
                if arg.keyword is not None:
                    args[arg.keyword.value] = value
                elif index > 0:
                    extra_positional.append(value)
            decls.append(
                DeclView(
                    kind=func.value,
                    id=decl_id,
                    file=name,
                    line=line,
                    args=args,
                    extra_positional=extra_positional,
                )
            )
        else:
            opaque.append(
                OpaqueDecl(
                    kind=func.value,
                    id=_first_literal_id(call),
                    file=name,
                    line=line,
                    reason="not at top level",
                )
            )
    return decls, opaque


def _top_level_calls(module: cst.Module) -> set[cst.Call]:
    """Every declaration-kind call that stands as an expression statement."""
    calls = set()
    for statement in module.body:
        if not isinstance(statement, cst.SimpleStatementLine):
            continue
        for item in statement.body:
            if (
                isinstance(item, cst.Expr)
                and isinstance(item.value, cst.Call)
                and isinstance(item.value.func, cst.Name)
                and item.value.func.value in DECLARATION_KINDS
            ):
                calls.add(item.value)
    return calls


class _CallCollector(cst.CSTVisitor):
    def __init__(self) -> None:
        self.calls: list[cst.Call] = []

    def visit_Call(self, node: cst.Call) -> None:
        if isinstance(node.func, cst.Name) and node.func.value in DECLARATION_KINDS:
            self.calls.append(node)


def _first_literal_id(call: cst.Call) -> str | None:
    if not call.args:
        return None
    first = call.args[0]
    if first.keyword is not None:
        return None
    value = first.value
    if not isinstance(value, cst.SimpleString):
        return None
    source = value.value
    if not source or source[0].isalpha():
        return None
    decoded = ast.literal_eval(source)
    assert isinstance(decoded, str)
    return decoded
