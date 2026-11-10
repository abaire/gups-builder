"""Conversion between Starlark expressions and JSON Values.

A ``Value`` is the JSON form of a Starlark expression. Plain literals map
through unchanged; constants, references, plain calls and dicts get a ``$``
prefix so the model can tell them apart from data; anything else is opaque
and keeps its exact source text.
"""

from __future__ import annotations

import ast

import libcst as cst
from pydantic import JsonValue

type Value = JsonValue


def to_value(expr: cst.BaseExpression, module: cst.Module) -> Value:
    """Convert a Starlark expression node to its Value form."""
    if isinstance(expr, cst.SimpleString):
        source = expr.value
        if not source or source[0].isalpha():
            # A prefixed string (f", b", r", u") is not a plain literal.
            return _opaque(expr, module)
        decoded = ast.literal_eval(source)
        assert isinstance(decoded, str)
        return decoded
    if isinstance(expr, (cst.Integer, cst.Float)):
        magnitude = expr.evaluated_value
        if isinstance(magnitude, (int, float)):
            return magnitude
        return _opaque(expr, module)
    if isinstance(expr, cst.Name):
        if expr.value == "None":
            return None
        if expr.value == "True":
            return True
        if expr.value == "False":
            return False
        return {"$ref": expr.value}
    if isinstance(expr, cst.Attribute):
        base = expr.value
        if isinstance(base, cst.Name) and base.value.isupper():
            return {"$const": module.code_for_node(expr)}
        return _opaque(expr, module)
    if isinstance(expr, cst.List):
        if any(not isinstance(element, cst.Element) for element in expr.elements):
            return _opaque(expr, module)
        return [to_value(element.value, module) for element in expr.elements]
    if isinstance(expr, cst.Dict):
        pairs = _dict_pairs(expr, module)
        if pairs is None:
            return _opaque(expr, module)
        return {"$dict": pairs}
    if isinstance(expr, cst.Call):
        func = expr.func
        if not isinstance(func, cst.Name):
            return _opaque(expr, module)
        args: list[Value] = []
        kwargs: dict[str, Value] = {}
        for arg in expr.args:
            if arg.star:
                # A *args or **kwargs expansion has no Value form.
                return _opaque(expr, module)
            value = to_value(arg.value, module)
            if isinstance(value, dict) and "$opaque" in value:
                return _opaque(expr, module)
            if arg.keyword is not None:
                kwargs[arg.keyword.value] = value
            else:
                args.append(value)
        return {"$call": func.value, "args": args, "kwargs": kwargs}
    if isinstance(expr, cst.UnaryOperation):
        operand = expr.expression
        if isinstance(expr.operator, cst.Minus) and isinstance(operand, (cst.Integer, cst.Float)):
            magnitude = operand.evaluated_value
            if isinstance(magnitude, (int, float)):
                return -magnitude
        return _opaque(expr, module)
    return _opaque(expr, module)


def _dict_pairs(expr: cst.Dict, module: cst.Module) -> list[Value] | None:
    """The key/value pairs of a dict literal, or None when it holds a ``**`` expansion."""
    pairs: list[Value] = []
    for element in expr.elements:
        if not isinstance(element, cst.DictElement):
            return None
        pairs.append([to_value(element.key, module), to_value(element.value, module)])
    return pairs


def is_editable(value: Value) -> bool:
    """True when the Value holds no ``$ref`` and no ``$opaque`` anywhere inside it."""
    if isinstance(value, dict):
        if "$ref" in value or "$opaque" in value:
            return False
        if "$const" in value:
            return True
        if "$dict" in value:
            pairs = value["$dict"]
            if not isinstance(pairs, list):
                return False
            return all(_is_pair_editable(pair) for pair in pairs)
        if "$call" in value:
            args = value.get("args", [])
            kwargs = value.get("kwargs", {})
            if not isinstance(args, list) or not isinstance(kwargs, dict):
                return False
            return all(is_editable(item) for item in args) and all(
                is_editable(item) for item in kwargs.values()
            )
        return all(is_editable(item) for item in value.values())
    if isinstance(value, list):
        return all(is_editable(item) for item in value)
    return True


def _is_pair_editable(pair: Value) -> bool:
    if not isinstance(pair, list):
        return False
    return all(is_editable(item) for item in pair)


def _opaque(expr: cst.BaseExpression, module: cst.Module) -> Value:
    return {"$opaque": module.code_for_node(expr)}
