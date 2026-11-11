"""Apply StarOps to a package's file contents, preserving the existing format.

Ops apply in order, each re-reading its declaration from the contents the ops
before it left behind, so a later op may edit a declaration an earlier one
added. An op that cannot apply is refused with a reason written for the user;
the remaining ops still apply. New values are rendered in the template's style
and parsed back, so a written list keeps its one-item-per-line shape.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import overload

import libcst as cst
from libcst.metadata import CodeRange, MetadataWrapper, PositionProvider

from gups_builder.star.ops import (
    AddDecl,
    AddTodo,
    AppendItem,
    ItemMatch,
    RemoveArg,
    RemoveDecl,
    RemoveItem,
    SetArg,
    StarOp,
)
from gups_builder.star.reader import PackageView, StarParseError
from gups_builder.star.render import comment_lines, render_decl, render_value
from gups_builder.star.values import Value, is_editable, to_value

_WS = cst.SimpleWhitespace
_INDENT = _WS("    ")


class OpRefused(Exception):
    """An op that could not be applied, with a reason written for the user."""

    def __init__(self, op: StarOp, reason: str, file: str | None, line: int | None) -> None:
        super().__init__(reason)
        self.op = op
        self.reason = reason
        self.file = file
        self.line = line


@dataclass
class ApplyResult:
    files: dict[str, str]
    refused: list[OpRefused] = field(default_factory=list)


@dataclass(frozen=True)
class _Found:
    file: str
    module: cst.Module
    statement: cst.SimpleStatementLine
    call: cst.Call
    positions: Mapping[cst.CSTNode, CodeRange]


def apply_ops(current: dict[str, str], view: PackageView, ops: list[StarOp]) -> ApplyResult:
    contents = dict(current)
    refused: list[OpRefused] = []
    for op in ops:
        try:
            after = _apply_one(contents, view, op)
            _check_parses(op, contents, after)
        except OpRefused as refusal:
            refused.append(refusal)
            continue
        except (ValueError, cst.CSTValidationError) as error:
            refused.append(OpRefused(op, f"this change could not be written: {error}", None, None))
            continue
        contents = after
    files = {
        name: text
        for name, text in contents.items()
        if name not in current or current[name] != text
    }
    return ApplyResult(files=files, refused=refused)


def _check_parses(op: StarOp, before: dict[str, str], after: dict[str, str]) -> None:
    """Refuse an op that would leave any file it touched unparseable.

    The per-op checks should make this unreachable; it keeps one bad op from
    corrupting a file and making every later op fail to find its declaration.
    """
    for name, text in after.items():
        if before.get(name) == text:
            continue
        try:
            cst.parse_module(text)
        except cst.ParserSyntaxError as error:
            raise OpRefused(
                op,
                f"this change would not be valid Starlark ({name}:{error.editor_line}); "
                "it was not applied",
                name,
                error.editor_line,
            ) from error


def _writable(value: Value) -> bool:
    """A plain value that renders to an expression that parses back."""
    if not is_editable(value):
        return False
    try:
        cst.parse_expression(render_value(value, 0))
    except (ValueError, cst.ParserSyntaxError):
        return False
    return True


def _plain_only(op: SetArg | AppendItem) -> OpRefused:
    return OpRefused(op, f"{op.kind} '{op.id}': {op.name} may only hold a plain value", None, None)


def _refuse_if_computed(op: StarOp, found: _Found, name: str, node: cst.BaseExpression) -> None:
    """Refuse an op on an argument whose value the file computes by code."""
    if is_editable(to_value(node, found.module)):
        return
    line = found.positions[node].start.line
    text = (
        f"{op.kind} '{op.id}': {name} is computed by code ({found.file}:{line}); change it by hand"
    )
    raise OpRefused(op, text, found.file, line)


def _arg_indent(call: cst.Call) -> int:
    """The indent of the call's arguments: the file's own when it puts them on lines."""
    for arg in call.args:
        if isinstance(arg.comma, cst.Comma) and isinstance(
            arg.comma.whitespace_after, cst.ParenthesizedWhitespace
        ):
            return len(arg.comma.whitespace_after.last_line.value)
    return 4


def _item_indent(lst: cst.List, arg_indent: int) -> int:
    """The indent of a list's items: the file's own when it puts them on lines."""
    if isinstance(lst.lbracket.whitespace_after, cst.ParenthesizedWhitespace):
        return len(lst.lbracket.whitespace_after.last_line.value)
    return arg_indent


def _apply_one(contents: dict[str, str], view: PackageView, op: StarOp) -> dict[str, str]:
    if isinstance(op, AddDecl):
        return _op_add_decl(contents, view, op)
    if isinstance(op, RemoveDecl):
        return _op_remove_decl(contents, op)
    if isinstance(op, AddTodo):
        return _op_add_todo(contents, op)
    if isinstance(op, SetArg):
        return _op_set_arg(contents, op)
    if isinstance(op, RemoveArg):
        return _op_remove_arg(contents, op)
    if isinstance(op, AppendItem):
        return _op_append_item(contents, op)
    if isinstance(op, RemoveItem):
        return _op_remove_item(contents, op)
    raise AssertionError(f"unknown op: {op!r}")


def _find_decl(contents: dict[str, str], kind: str, id: str) -> _Found | None:
    """The plain declaration of (kind, id), re-parsed from the current contents."""
    for name in sorted(contents):
        module, positions = _parse_contents(contents, name)
        hit = _declaration_in(module, positions, kind, id)
        if hit is not None:
            statement, call = hit
            return _Found(name, module, statement, call, positions)
    return None


def _parse_contents(
    contents: dict[str, str], name: str
) -> tuple[cst.Module, Mapping[cst.CSTNode, CodeRange]]:
    """The parsed module and position table for ``name`` in ``contents``."""
    try:
        wrapper = MetadataWrapper(cst.parse_module(contents[name]))
    except cst.ParserSyntaxError as error:
        raise StarParseError(name, error.editor_line, error.message) from error
    return wrapper.module, wrapper.resolve(PositionProvider)


def _declaration_in(
    module: cst.Module,
    positions: Mapping[cst.CSTNode, CodeRange],
    kind: str,
    id: str,
) -> tuple[cst.SimpleStatementLine, cst.Call] | None:
    """The (statement, call) that plainly declares (kind, id) in ``module``, if any."""
    for statement in _simple_statements(module.body):
        call = _first_decl_call(statement, kind, id)
        if call is not None:
            return statement, call
    return None


def _simple_statements(body: Sequence[cst.BaseStatement]) -> Iterator[cst.SimpleStatementLine]:
    """The simple statements in a module body, in order."""
    for statement in body:
        if isinstance(statement, cst.SimpleStatementLine):
            yield statement


def _first_decl_call(statement: cst.SimpleStatementLine, kind: str, id: str) -> cst.Call | None:
    """The first item of a statement that declares (kind, id) plainly, if any."""
    for item in statement.body:
        call = _decl_item(item, kind, id)
        if call is not None:
            return call
    return None


def _decl_item(item: cst.BaseSmallStatement, kind: str, id: str) -> cst.Call | None:
    """The call a statement item declares, when it names (kind, id) plainly."""
    if not isinstance(item, cst.Expr):
        return None
    if not isinstance(item.value, cst.Call):
        return None
    call = item.value
    if not isinstance(call.func, cst.Name):
        return None
    if call.func.value != kind:
        return None
    if _decl_id(call) != id:
        return None
    return call


def _decl_of(contents: dict[str, str], op: StarOp) -> _Found:
    found = _find_decl(contents, op.kind, op.id)
    if found is None:
        raise OpRefused(
            op, f"{op.kind} '{op.id}' is not a plain declaration in this package", None, None
        )
    return found


def _decl_id(call: cst.Call) -> str | None:
    if not call.args or call.args[0].keyword is not None:
        return None
    value = call.args[0].value
    if not isinstance(value, cst.SimpleString):
        return None
    source = value.value
    if not source or source[0].isalpha():
        return None
    decoded = ast.literal_eval(source)
    return decoded if isinstance(decoded, str) else None


def _rewrite_call(contents: dict[str, str], found: _Found, call: cst.Call) -> dict[str, str]:
    items: list[cst.BaseSmallStatement] = []
    for item in found.statement.body:
        if isinstance(item, cst.Expr) and item.value is found.call:
            items.append(cst.Expr(value=call, semicolon=item.semicolon))
        else:
            items.append(item)
    statement = found.statement.with_changes(body=tuple(items))
    body = [statement if s is found.statement else s for s in found.module.body]
    return {**contents, found.file: found.module.with_changes(body=tuple(body)).code}


def _arg_index(args: Sequence[cst.Arg], name: str) -> int | None:
    """The index of the keyword argument named ``name``, or None if there is none."""
    for i, arg in enumerate(args):
        if arg.keyword is None:
            continue
        if arg.keyword.value != name:
            continue
        return i
    return None


def _op_set_arg(contents: dict[str, str], op: SetArg) -> dict[str, str]:
    if not _writable(op.value):
        raise _plain_only(op)
    found = _decl_of(contents, op)
    indent = _arg_indent(found.call)
    args = list(found.call.args)
    index = _arg_index(args, op.name)
    if index is None:
        expr = _from_value(op.value, indent)
        return _rewrite_call(
            contents, found, found.call.with_changes(args=_append_arg(found.call, op.name, expr))
        )
    arg = args[index]
    _refuse_if_computed(op, found, op.name, arg.value)
    args[index] = arg.with_changes(value=_from_value(op.value, indent))
    return _rewrite_call(contents, found, found.call.with_changes(args=tuple(args)))


def _op_remove_arg(contents: dict[str, str], op: RemoveArg) -> dict[str, str]:
    found = _decl_of(contents, op)
    args = list(found.call.args)
    index = _arg_index(args, op.name)
    if index is None:
        raise OpRefused(op, f"{op.kind} '{op.id}': {op.name} has no such argument", None, None)
    arg = args[index]
    _refuse_if_computed(op, found, op.name, arg.value)
    if index == len(args) - 1:
        # The new last argument takes over where the closing paren sits.
        args[index - 1] = args[index - 1].with_changes(
            comma=arg.comma, whitespace_after_arg=arg.whitespace_after_arg
        )
    else:
        args[index - 1] = _carry_comments(args[index - 1], arg)
    args.pop(index)
    return _rewrite_call(contents, found, found.call.with_changes(args=tuple(args)))


def _op_append_item(contents: dict[str, str], op: AppendItem) -> dict[str, str]:
    if not _writable(op.value):
        raise _plain_only(op)
    found = _decl_of(contents, op)
    indent = _arg_indent(found.call)
    args = list(found.call.args)
    index = _arg_index(args, op.name)
    if index is None:
        expr = _from_value([op.value], indent)
        return _rewrite_call(
            contents, found, found.call.with_changes(args=_append_arg(found.call, op.name, expr))
        )
    arg = args[index]
    node = arg.value
    _refuse_if_computed(op, found, op.name, node)
    if not isinstance(node, cst.List):
        raise OpRefused(op, f"{op.kind} '{op.id}': {op.name} is not a list", None, None)
    item = _from_value(op.value, _item_indent(node, indent))
    args[index] = arg.with_changes(value=_append_element(node, item))
    return _rewrite_call(contents, found, found.call.with_changes(args=tuple(args)))


def _op_remove_item(contents: dict[str, str], op: RemoveItem) -> dict[str, str]:
    found = _decl_of(contents, op)
    args = list(found.call.args)
    index = _arg_index(args, op.name)
    if index is None:
        raise OpRefused(op, f"{op.kind} '{op.id}': {op.name} has no such argument", None, None)
    node = args[index].value
    _refuse_if_computed(op, found, op.name, node)
    if not isinstance(node, cst.List):
        raise OpRefused(op, f"{op.kind} '{op.id}': {op.name} is not a list", None, None)
    element_index = _matched_index(node, op, found.module)
    args[index] = args[index].with_changes(value=_remove_element(node, element_index))
    return _rewrite_call(contents, found, found.call.with_changes(args=tuple(args)))


def _matched_index(node: cst.List, op: RemoveItem, module: cst.Module) -> int:
    """The one list element matching ``op.match``, or a refusal."""
    items = [to_value(element.value, module) for element in node.elements]
    matching = [j for j, item in enumerate(items) if _matches(item, op.match)]
    if len(matching) == 0:
        raise OpRefused(op, f"{op.kind} '{op.id}': {op.name} has no matching item", None, None)
    if len(matching) > 1:
        raise OpRefused(
            op, f"{op.kind} '{op.id}': {op.name} has more than one matching item", None, None
        )
    return matching[0]


def _remove_element(node: cst.List, index: int) -> cst.List:
    """The list with the item at ``index`` gone, its comments and whitespace moved."""
    elements = list(node.elements)
    removed = elements.pop(index)
    if not elements:
        return node.with_changes(
            elements=(),
            lbracket=node.lbracket.with_changes(whitespace_after=_WS("")),
            rbracket=node.rbracket.with_changes(whitespace_before=_WS("")),
        )
    if index == len(elements):
        elements[-1] = elements[-1].with_changes(comma=removed.comma)
    elif index == 0:
        # The first item's leading whitespace lives in the bracket.
        node = node.with_changes(
            lbracket=node.lbracket.with_changes(
                whitespace_after=_merge_comments(node.lbracket.whitespace_after, removed.comma)
            )
        )
    else:
        elements[index - 1] = _carry_comments(elements[index - 1], removed)
    return node.with_changes(elements=tuple(elements))


def _op_remove_decl(contents: dict[str, str], op: RemoveDecl) -> dict[str, str]:
    found = _decl_of(contents, op)
    module = found.module
    if len(found.statement.body) > 1:
        # Several statements share the line: drop only this one.
        items = [
            item
            for item in found.statement.body
            if not (isinstance(item, cst.Expr) and item.value is found.call)
        ]
        items[-1] = items[-1].with_changes(semicolon=cst.MaybeSentinel.DEFAULT)
        statement = found.statement.with_changes(body=tuple(items))
        body = [statement if s is found.statement else s for s in module.body]
        return {**contents, found.file: module.with_changes(body=tuple(body)).code}
    body = [s for s in module.body if s is not found.statement]
    if module.body and module.body[0] is found.statement:
        module = module.with_changes(header=_header_without_decl_comments(module.header))
    return {**contents, found.file: module.with_changes(body=tuple(body)).code}


def _header_without_decl_comments(header: Sequence[cst.EmptyLine]) -> tuple[cst.EmptyLine, ...]:
    """The module header minus the comment lines directly above the first statement.

    libcst puts every comment above the first statement into the header. A run
    that starts at the top of the file is the file's own header and stays; a run
    after a blank line belongs to the declaration and goes with it, together
    with the blank lines before it.
    """
    lines = list(header)
    end = len(lines)
    while end > 0 and lines[end - 1].comment is not None:
        end -= 1
    if end == 0 or end == len(lines):
        return tuple(lines)
    while end > 0 and lines[end - 1].comment is None:
        end -= 1
    return tuple(lines[:end])


def _op_add_todo(contents: dict[str, str], op: AddTodo) -> dict[str, str]:
    found = _decl_of(contents, op)
    todo = [
        cst.EmptyLine(
            indent=True, whitespace=_WS(""), comment=cst.Comment(line), newline=cst.Newline(None)
        )
        for line in comment_lines(op.text, "# TODO(gups-builder): ", "#   ")
    ]
    statement = found.statement.with_changes(leading_lines=(*found.statement.leading_lines, *todo))
    body = [statement if s is found.statement else s for s in found.module.body]
    return {**contents, found.file: found.module.with_changes(body=tuple(body)).code}


def _op_add_decl(contents: dict[str, str], view: PackageView, op: AddDecl) -> dict[str, str]:
    if _find_decl(contents, op.kind, op.id) is not None:
        raise OpRefused(op, f"{op.kind} '{op.id}' already exists", None, None)
    if not _all_writable(op.args.values()):
        raise OpRefused(
            op, f"new arguments of {op.kind} '{op.id}' must be plain values", None, None
        )
    rendered = render_decl(op.kind, op.id, op.args, op.comment)
    if op.file in contents:
        return {**contents, op.file: _with_rendered(contents[op.file], rendered)}
    header = f"# {op.kind.capitalize()}s of {view.path}, started by gups-builder.\n"
    return {**contents, op.file: header + rendered}


def _all_writable(values: Iterable[Value]) -> bool:
    """Whether every value renders to an expression that parses back."""
    return all(_writable(value) for value in values)


def _with_rendered(base: str, rendered: str) -> str:
    """A file's text with a new declaration appended, keeping a trailing newline."""
    if base and not base.endswith("\n"):
        base += "\n"
    return base + "\n" + rendered


def _matches(item: Value, match: ItemMatch) -> bool:
    """Whether a value an item holds satisfies the item filter."""
    if not isinstance(item, dict) or "$call" not in item:
        return _empty_match(match)
    if _call_mismatches(item, match.call):
        return False
    if _first_arg_mismatches(item, match.first_arg):
        return False
    return not _kwarg_mismatches(item, match.kwarg)


def _empty_match(match: ItemMatch) -> bool:
    """Whether a filter that names nothing matches a non-call item."""
    return match.call is None and match.first_arg is None and match.kwarg is None


def _call_mismatches(item: dict[str, Value], call: str | None) -> bool:
    """Whether a named-call filter rejects the call an item makes."""
    return call is not None and item["$call"] != call


def _first_arg_mismatches(item: dict[str, Value], first_arg: Value | None) -> bool:
    """Whether a first-argument filter rejects the arguments a call passes."""
    if first_arg is None:
        return False
    args = item["args"]
    return not isinstance(args, list) or not args or args[0] != first_arg


def _kwarg_mismatches(item: dict[str, Value], kwarg: tuple[str, Value] | None) -> bool:
    """Whether a keyword-argument filter rejects the keyword arguments a call passes."""
    if kwarg is None:
        return False
    name, wanted = kwarg
    kwargs = item["kwargs"]
    return not isinstance(kwargs, dict) or kwargs.get(name) != wanted


def _from_value(value: Value, indent: int) -> cst.BaseExpression:
    """The expression for a value rendered on a line indented ``indent`` spaces."""
    return cst.parse_expression(render_value(value, indent))


def _append_arg(call: cst.Call, name: str, expr: cst.BaseExpression) -> tuple[cst.Arg, ...]:
    """A new keyword argument after the last one, copying the file's comma style."""
    args = list(call.args)
    keyword = cst.Name(name)
    equal = cst.AssignEqual(whitespace_before=_WS(" "), whitespace_after=_WS(" "))
    if not args:
        return (_arg(expr, keyword, equal),)
    last = args[-1]
    if isinstance(last.comma, cst.Comma):
        new_arg = _arg(expr, keyword, equal, last.comma)
        last = last.with_changes(comma=_between_comma(args, last.comma, _INDENT))
        return (*args[:-1], last, new_arg)
    separator = _separator_ws(args)
    last = last.with_changes(comma=cst.Comma(whitespace_before=_WS(""), whitespace_after=separator))
    return (*args[:-1], last, _arg(expr, keyword, equal))


def _arg(
    expr: cst.BaseExpression,
    keyword: cst.Name,
    equal: cst.AssignEqual,
    comma: cst.Comma | cst.MaybeSentinel = cst.MaybeSentinel.DEFAULT,
) -> cst.Arg:
    """A keyword argument, carrying no trailing comma unless one is given."""
    return cst.Arg(
        value=expr, keyword=keyword, equal=equal, whitespace_after_arg=_WS(""), comma=comma
    )


def _append_element(lst: cst.List, expr: cst.BaseExpression) -> cst.List:
    """An item appended to the end, keeping a multi-line list one item per line."""
    elements = list(lst.elements)
    if not elements:
        return lst.with_changes(elements=(_element(expr),))
    last = elements[-1]
    if isinstance(last.comma, cst.Comma):
        new_element = _element(expr, last.comma)
        between = _between_comma(elements, last.comma, _element_indent(lst))
        elements[-1] = last.with_changes(comma=between)
        return lst.with_changes(elements=(*elements, new_element))
    separator = _separator_ws(elements)
    elements[-1] = last.with_changes(
        comma=cst.Comma(whitespace_before=_WS(""), whitespace_after=separator)
    )
    return lst.with_changes(elements=(*elements, _element(expr)))


def _element(
    expr: cst.BaseExpression,
    comma: cst.Comma | cst.MaybeSentinel = cst.MaybeSentinel.DEFAULT,
) -> cst.Element:
    """A list element, carrying no trailing comma unless one is given."""
    return cst.Element(value=expr, comma=comma)


def _element_indent(lst: cst.List) -> cst.SimpleWhitespace:
    """The indent for an appended item: the list's own when it is multi-line."""
    if isinstance(lst.lbracket.whitespace_after, cst.ParenthesizedWhitespace):
        return lst.lbracket.whitespace_after.last_line
    return _WS("    ")


def _prev_comma(items: Sequence[cst.Arg | cst.BaseElement]) -> cst.Comma | None:
    """The trailing comma of the item before the last, or None if there is none."""
    if len(items) < 2:
        return None
    prev = items[-2].comma
    return prev if isinstance(prev, cst.Comma) else None


def _between_comma(
    items: Sequence[cst.Arg | cst.BaseElement],
    old_comma: cst.Comma,
    indent_ws: cst.SimpleWhitespace,
) -> cst.Comma:
    """The comma that sits between the last item and the one being appended.

    A following item keeps the file's own multi-line comma where there is one;
    otherwise the last item's comma is re-indented to the item's indent.
    """
    prev_comma = _prev_comma(items)
    if prev_comma is not None:
        return prev_comma
    if not isinstance(old_comma.whitespace_after, cst.ParenthesizedWhitespace):
        return old_comma
    return old_comma.with_changes(
        whitespace_after=old_comma.whitespace_after.with_changes(last_line=indent_ws)
    )


def _separator_ws(items: Sequence[cst.Arg | cst.BaseElement]) -> cst.BaseParenthesizableWhitespace:
    """Whitespace after the last item when it has no trailing comma and we append."""
    prev_comma = _prev_comma(items)
    if prev_comma is None:
        return _WS(" ")
    if isinstance(prev_comma.whitespace_after, cst.ParenthesizedWhitespace):
        return prev_comma.whitespace_after
    return _WS(" ")


def _merge_comments(
    before: cst.BaseParenthesizableWhitespace, removed_comma: cst.Comma | cst.MaybeSentinel
) -> cst.BaseParenthesizableWhitespace:
    """Whitespace ``before`` an item, extended with the comment lines a removed item carried."""
    if not isinstance(removed_comma, cst.Comma) or not isinstance(
        removed_comma.whitespace_after, cst.ParenthesizedWhitespace
    ):
        return before
    after = removed_comma.whitespace_after
    if not any(line.comment is not None for line in after.empty_lines):
        return before
    if not isinstance(before, cst.ParenthesizedWhitespace):
        return before
    return before.with_changes(
        empty_lines=(*before.empty_lines, *after.empty_lines), last_line=after.last_line
    )


@overload
def _carry_comments(prev: cst.Arg, removed: cst.Arg) -> cst.Arg: ...


@overload
def _carry_comments(prev: cst.BaseElement, removed: cst.BaseElement) -> cst.BaseElement: ...


def _carry_comments(
    prev: cst.Arg | cst.BaseElement, removed: cst.Arg | cst.BaseElement
) -> cst.Arg | cst.BaseElement:
    """Move the comment lines a removed item carried onto its new predecessor."""
    if not isinstance(prev.comma, cst.Comma):
        return prev
    merged = _merge_comments(prev.comma.whitespace_after, removed.comma)
    return prev.with_changes(comma=prev.comma.with_changes(whitespace_after=merged))
