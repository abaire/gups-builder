"""Prompt templates and context budgeting."""

from __future__ import annotations

import pytest

from gups_builder.prompting import estimate_tokens, fit, load_prompt


def test_load_prompt_substitutes_fields() -> None:
    text = load_prompt("repair_lint", problems="rooms.star:3: unexpected token", previous="{}")
    assert "rooms.star:3: unexpected token" in text
    assert "{}" in text
    assert "{problems}" not in text and "{previous}" not in text


def test_load_prompt_missing_field_is_an_error() -> None:
    with pytest.raises(ValueError, match="missing the field 'previous'$"):
        load_prompt("repair_lint", problems="p")


def test_load_prompt_unknown_name_is_an_error() -> None:
    with pytest.raises(FileNotFoundError):
        load_prompt("no_such_prompt")


def test_estimate_tokens_is_quarter_length_plus_one() -> None:
    assert estimate_tokens("") == 1
    assert estimate_tokens("abcd") == 2
    assert estimate_tokens("a" * 400) == 101


def test_fit_drops_priority_2_then_1_and_never_zero() -> None:
    sections = [
        (0, "request " * 20),
        (2, "balance " * 20),
        (1, "schema " * 20),
        (0, "rules " * 20),
    ]
    out = fit(sections, 100)
    assert "balance" not in out
    assert "schema" not in out
    assert out.index("request") < out.index("rules")


def test_fit_keeps_all_that_fit() -> None:
    sections = [(0, "a" * 40), (1, "b" * 40), (2, "c" * 40)]
    out = fit(sections, 1000)
    assert out == "a" * 40 + "\n\n" + "b" * 40 + "\n\n" + "c" * 40


def test_fit_with_no_budget_keeps_priority_zero_only() -> None:
    sections = [(2, "z" * 400), (0, "keep" * 10)]
    out = fit(sections, 10)
    assert out == "keep" * 10
