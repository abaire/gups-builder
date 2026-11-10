"""The StarOp types: the changes a pipeline can ask a package for.

Each op is one change to a package, addressed by the kind and id of a plain
declaration. Modes produce lists of these; the writer applies them to file
contents.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, Field

from gups_builder.star.values import Value


class ItemMatch(BaseModel):
    """Which item of a list argument a RemoveItem takes.

    An item matches when every criterion given matches it; with no criteria
    every item matches. Exactly one item must match.
    """

    call: str | None = None  # e.g. "exit", "spawn", "loot"
    first_arg: Value | None = None  # e.g. {"$const": "DIR.north"}; args[0] of a $call item
    kwarg: tuple[str, Value] | None = None  # e.g. ("to", ":hall")


class AddDecl(BaseModel):
    op: Literal["add_decl"] = "add_decl"
    file: str
    kind: str
    id: str
    args: dict[str, Value]
    comment: str | None = None


class RemoveDecl(BaseModel):
    op: Literal["remove_decl"] = "remove_decl"
    kind: str
    id: str


class SetArg(BaseModel):
    op: Literal["set_arg"] = "set_arg"
    kind: str
    id: str
    name: str
    value: Value


class RemoveArg(BaseModel):
    op: Literal["remove_arg"] = "remove_arg"
    kind: str
    id: str
    name: str


class AppendItem(BaseModel):
    op: Literal["append_item"] = "append_item"
    kind: str
    id: str
    name: str
    value: Value


class RemoveItem(BaseModel):
    op: Literal["remove_item"] = "remove_item"
    kind: str
    id: str
    name: str
    match: ItemMatch


class AddTodo(BaseModel):
    op: Literal["add_todo"] = "add_todo"
    kind: str
    id: str
    text: str


StarOp = Annotated[
    AddDecl | RemoveDecl | SetArg | RemoveArg | AppendItem | RemoveItem | AddTodo,
    Field(discriminator="op"),
]
