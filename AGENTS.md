# Notes for Agents

`gups-builder` is built milestone by milestone from [docs/SPEC.md](docs/SPEC.md); each milestone is planned in its own
`docs/MILESTONE_N.md`. The spec is the source of truth for what the code does, and each phase updates the spec
sections it names in the same change.

## Working from the spec

The spec describes behaviour, not code. Where it is silent or ambiguous, make the choice, record it under
"Open choices" in `docs/SPEC.md` (noting the phase that made it), and keep going. This repository has no separate
decision log; the open-choices section of the spec is where unratified decisions live for human review.

Do not cite spec sections or decision IDs in code comments. Comments explain why something is done directly, for a
reader who knows Python and the milestone. Assume the reader is familiar with the GUPS engine; explain only the
builder's own choices.

## Naming: skills, not spells

Everything a character casts is a `skill`, and its `kind` is free-form; "spell" is only one kind. The interactive
mode is `skill`, and every identifier — file, class, function, prompt, test — says `skill`. Player-facing prose may
say "spell" where a world chooses that word, but the code never does.

## Writing code

- Follow test-driven development. Write the tests first, run them and watch them fail, then write the code that
  passes them.
- Tests never call a real model. They drive the builder with `FakeLlm`. A test that needs the real `gups` binary is
  marked `@pytest.mark.engine` and skips itself when the binary can't be found.

## Building and testing

A phase is not done until all four of these pass:

```bash
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest
```

`mypy` runs in strict mode over `src/` and `tests/`. `ruff` enforces line length 100 and the `E, F, I, UP, B, SIM`
rules. Use `uv` for everything: `uv sync` to install, `uv run <tool>` to invoke.

Run the engine tests too, not only the unit layer: when `gups` is not on the `PATH` they skip silently, and a
phase can pass every check while its output never met the engine. A built engine is usually at
`../gups-engine/cli/build/install/gups/bin/gups`; run `GUPS_BIN=<that path> uv run pytest` and confirm nothing is
reported as skipped (`-rs`).

## Code health

Code health is checked as you work, not left for review. It uses CodeScene's MCP server (`cs-mcp`):

- After changing a file, run `code_health_review` on it. A file you touch must not score lower than it did before
  your change; fix what the review raises in the functions you wrote or changed, then confirm with
  `code_health_score`.
- Prefer the shapes the review rewards: small functions with one job, early returns over nested conditionals,
  tables (a `dict` of handlers) over long `if`/`elif` chains, and a named helper for any condition with more than
  one `and`/`or`.
- Before calling a phase done, run `pre_commit_code_health_safeguard` on the repository and only stop when its
  quality gates pass. Report any finding you leave in place, and why, in your summary.
- If the CodeScene server is unavailable, say so in your summary instead of skipping the check silently.

## The engine boundary

The GUPS engine lives in the sibling repository `gups-engine/`. Treat it as read-only and versioned: never modify it,
and never write into `gups-engine/template-world/`. A test that needs the template world copies it to a temporary
directory first. Read from `gups-engine/` only what a phase's "Read / Reference Only" list names.

## The libcst version

`uv.lock` resolves `libcst` 1.9.0 (`pyproject.toml` does not pin it). It has no `Argument` class: argument and list
items are `Arg` and `Element`. Check names against the installed source rather than examples. `MetadataWrapper(module)`
takes a `Module` and works on a deep copy of it, so after wrapping, use `wrapper.module` and its nodes:
`wrapper.resolve(PositionProvider)` is keyed by the copy's nodes and raises `KeyError` for the original's.
`module.code_for_node(node)` renders any node, from either tree.

Its whitespace model is the part that silently corrupts formatting if guessed wrong:

- An item carries no *leading* whitespace of its own. The whitespace before the first item is in
  `List.lbracket.whitespace_after` (for a list) or `Call.whitespace_before_args` (for a call). The whitespace before
  every later item is in the preceding item's `Comma.whitespace_after`.
- That whitespace is a `ParenthesizedWhitespace` when it crosses a line (`first_line` holds the newline, `last_line`
  the next line's indent, `empty_lines` any comment lines in between), and a `SimpleWhitespace` otherwise.
- The whitespace before the closing bracket differs between lists and calls. A `List` keeps it in
  `rbracket.whitespace_before`, so a trailing comma before `]` has `whitespace_after == ''`. A `Call` has no such
  field: with a trailing comma, the newline before `)` is in that last comma's `whitespace_after`; without one, the
  last item's comma is a `MaybeSentinel` and the newline is in the last `Arg.whitespace_after_arg`.
- When inserting or removing an argument or list item, copy the neighbouring whitespace. Do not synthesize it.
- `Call` renders its own closing `)`; its `lpar` and `rpar` are sequences of *extra* parens, empty for a normal
  call.
- `SimpleStatementLine.leading_lines` (a sequence of `EmptyLine`) holds the comment and blank lines directly above
  a statement; a blank line is an `EmptyLine` with `comment=None`. The exception is the first statement of a file:
  every comment and blank line above it is in `Module.header`, and its `leading_lines` is empty.
- Only `Module` has `.code`. Rebuild a full module (`module.with_changes(body=...)`) and read `.code` for output.
- `cst.parse_expression` exists and preserves whitespace exactly; `star/writer.py` builds inserted expressions by
  rendering a `Value` to text and parsing it back.
- mypy strict: `List.elements` is `Sequence[BaseElement]` and `Arg.comma` is `Comma | MaybeSentinel`. A helper typed
  with a union-bounded type parameter (`def f[T: (Arg | Element)](x: T) -> T`) fails to type-check, because
  `x.with_changes(...)` returns the union. Use a constrained parameter (`[T: (Arg, Element)]`) or an `@overload`
  pair.
