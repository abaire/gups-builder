# gups-builder Specification

## 1. Purpose and scope

`gups-builder` is a separate Python repository providing the `gups-builder` command: an interactive terminal session
that helps a builder create GUPS content with two local LLMs. A **logic** model plans structure — room graphs,
numbers, effects, edits — and a **prose** model writes names, descriptions and messages. A session works in one mode
at a time: `area` (rooms, exits and doors), `mob` (mobs and their loot) or `skill` (skills of any `kind`); a request
that belongs to another mode is noted, not attempted. Python decides every step of a fixed pipeline: each model call
is a single request whose reply must match a pydantic schema, and an invalid reply is sent back for repair rather
than entering a tool-calling loop. The `.star` files are the source of truth; every turn re-reads the target package
into an editable view, treats values that are not plain literals as opaque (shown to the model, never changed), and
applies changes as minimal, formatting-preserving edits so the builder may edit the files by hand at any time. The
engine is the authority on the loaded world: `gups describe-world` gives the schema and declarations, `gups docs
--json` gives the API model, and `gups lint` and `gups test` validate. The builder writes plain declaration arguments
only — never Starlark functions — and where a request needs logic it inserts a `# TODO(gups-builder):` comment. Area
rooms live on an integer grid where the model gives coordinates and Python derives every exit and door. Before a
write, every request becomes a proposal that is applied to a temporary copy and run through `gups lint` and `gups
test`, repaired with the logic model, and shown to the user as a summary, map and diff; nothing touches the real
files until `/write`. In a zone package the builder owns one generated test, `builder_test.star`.

## 2. Installation and configuration

Install with `uv tool install .`, which builds the package and puts `gups-builder` on the `PATH`. The tool drives two
roles, `logic` and `prose`, each an OpenAI-compatible chat endpoint described by a `RoleConfig`: `base_url`
(default `http://127.0.0.1:11434/v1`), `model` (default empty, meaning not set), `api_key` (default unset),
`temperature` (default `0.2` for logic, `0.8` for prose), `max_tokens` (`4096`), `context_tokens` (`16384`),
`structured` (`json_schema`, `json_object` or `none`) and `timeout_s` (`300.0`). The whole configuration is a
`Config` holding the two roles plus `max_repairs` (`3`) and `gups` (a path, default unset).

Settings are merged per field — a file that sets only `logic.model` keeps every other logic field from the sources
below it — in this order of precedence, highest first: CLI flags, environment variables, the world file
`<world>/.builder/config.toml`, the user file `<home>/.config/gups-builder/config.toml`, then the defaults. The TOML
layout is a top-level `max_repairs` and `gups`, with `[logic]` and `[prose]` tables using the `RoleConfig` field
names. Environment variables are `GUPS_BUILDER_<ROLE>_<FIELD>` with the field uppercased — `..._URL` maps to
`base_url`, `..._MODEL` to `model`, `..._API_KEY`, `..._TEMPERATURE`, `..._STRUCTURED`, `..._CONTEXT_TOKENS`,
`..._MAX_TOKENS`, `..._TIMEOUT_S` — plus
`GUPS_BIN` for `gups` and `GUPS_BUILDER_MAX_REPAIRS`. The CLI flags are global and precede the subcommand: `--world`
(default `.`), `--gups`, `--logic-url`, `--logic-model`, `--prose-url`, `--prose-model`. A `structured` value outside
the three allowed names, or a number setting that is not a number, is rejected with a `ValueError` that names the
source it came from. A role whose `model` is
still empty after merging is an error only when that role is actually used; `gups-builder models` shows it as
`(not set)` and never prints the value of an API key; an empty `api_key` counts as not set, and no `Authorization`
header is sent for it.

## 3. Engine contract

The builder never talks to the GUPS engine directly: it runs the `gups` binary as a subprocess with a 120-second
timeout and reads only its standard output. The binary is found in this order — the `gups` path from the
configuration (§2), the `GUPS_BIN` environment variable, then `gups` on the `PATH`. A path set in the configuration
or in `GUPS_BIN` must name an executable file, or an `EngineError` naming that setting is raised; the builder never
falls through to a different `gups` than the one the user named. With neither set and no `gups` on the `PATH`, an
`EngineError` is raised too. Output from `describe-world` or `docs --json` that is not JSON, or not a document of
the expected shape, raises an `EngineError`.

Four commands are used:

- **`gups describe-world --world DIR`** loads the world with its plugins and writes the JSON document describing it
  to standard output. A non-zero exit raises an `EngineError` carrying the engine's standard error. The document's
  top-level `format` must be `1`; any other value refuses to start, with the message `gups describe-world format
  <n> is not supported; this gups-builder needs format 1`, where `<n>` is the value present. The document is
  modelled with one pydantic class per object kind (`WorldDescription`, `Schema`, `RoomInfo`, `MobInfo`,
  `ItemInfo`, `SkillInfo`, `PackageInfo`, `PluginSummary`, `LoadErrorInfo`, …); unknown extra keys are ignored so
  a newer engine that adds fields still parses, and `null` appears exactly where the engine's format allows it
  (a door on an exit, a mob's factions, a package kind or error line). The helpers `has_feature`,
  `rooms_in`/`mobs_in`/`items_in`/`skills_in` (labels carrying the `//<package>:` prefix) and `label_exists`
  (any declared room, mob, item, skill or faction) answer the questions the pipeline asks.
- **`gups docs --json`** writes the `api.json` model to standard output, and is the only command run without a
  world flag — the engine refuses `--world` here because api.json never includes plugin entries. The result is
  modelled by `ApiModel`: declarations, effects and behaviors, each with its name, doc, feature and parameters
  (name, type, required, default, allowed values), and the entry lookups read only what the builder reads.
  `declaration(name)` returns the entry and raises `KeyError` for an unknown name; `effect(name)` and
  `behavior(name)` return `None` when the entry is absent. The model is cached per `Gups` instance.
- **`gups lint --format github --world DIR //<package>/...`** writes one GitHub annotation per finding:
  `::error file=<path>,line=<n>,title=gups lint::<message>`, with `::warning` likewise and `line=` optional. The
  parser accepts only these `gups lint` annotations — every other line of the output is ignored — and decodes the
  engine's `%25`, `%0D` and `%0A` escapes. Findings are returned whatever the exit code says, but a non-zero exit
  with no findings means lint did not run (a bad flag or world) and raises an `EngineError` with its standard
  error, so it is never mistaken for a clean package.
- **`gups test --world DIR //<package>/...`** runs the package's tests. Its exit code alone decides `TestRun.ok`;
  `output` is the combined standard output and standard error.

Staging: a copy of the world that verification commands may examine is made by `staged_world`, which copies the
world into a temporary directory that is deleted on exit. The copy skips `.git`, `.gups`, `.builder` and
`local-world-test` but keeps `plugins/`, so the world's plugin jar checksums still match after staging.

## 4. Model access

The builder talks to two OpenAI-compatible chat endpoints — one per role — through `POST <base_url>/chat/
completions` with `model`, `messages`, `temperature` and `max_tokens`. The `Authorization: Bearer` header is
sent only when the role has an `api_key`. A role whose `model` is empty refuses the call with `LlmError: the
<role> model is not configured`; a network failure, a timeout or any non-200 reply raises an `LlmError` naming
the role, the URL and the status. The reply is `choices[0].message.content`; a 200 reply without a text there (not
JSON, no choices, `null` content) raises an `LlmError` too.

The `structured` mode of the role that is called decides how the schema of the expected reply is conveyed:

- `json_schema` (default) sends `response_format` with `type: json_schema`, `strict: true` and the schema under
  the name `reply`. If the server answers HTTP 400, that one call is retried once as `json_object` and the log
  records `error: "json_schema rejected; used json_object"`; a second 400 (or any other non-200) raises
  `LlmError`. The retry carries the schema in the messages, as `json_object` mode does.
- `json_object` sends `response_format: {type: json_object}` and appends the schema to the last user message
  as `Reply with JSON matching this schema:` followed by the schema formatted as JSON.
- `none` sends no `response_format` and appends the schema to the last user message the same way.

When there is no user message to carry the schema, it is sent as a user message of its own after the others.

`call_structured` makes one such call whose reply must parse as a given pydantic model. The schema it sends is
the model's `model_json_schema()`. A Markdown code fence around the reply is stripped before parsing. When the
reply fails to validate, or an `extra_check` returns complaints, the model's reply is appended as an assistant
message and a user message `That reply was not valid:` listing each error as a `- ` line, ending `Reply again
with the corrected JSON only.`, is added and the call is retried, up to `max_repairs` retries; then
`StructuredCallFailed` is raised carrying every attempt and every error.

Every call is appended to the JSONL call log as one JSON object with `ts` (ISO 8601), `role`, `model`,
`messages`, `reply`, `seconds` and `error`. A request that fails outright (the 400 fallback raising, a timeout,
a non-200) is logged with an empty `reply` and the error as its message. The REPL (Phase 10) puts the log at
`<world>/.builder/logs/<session>.jsonl`.

`gups-builder models` first prints the two roles and the `gups` path as in §2, then sends each role a plain
request with a single system message `Reply with the single word OK.` and no schema, printing `<role>: OK
(<t> s)` or `<role>: FAILED: <reason>`. Then, for each role whose `structured` is `json_schema`, it sends one
schema-constrained request — the schema an object with a required string `word`, the user message `Reply with
the word OK.` — and prints `<role>: json_schema OK`, or, when the server refused `json_schema` (an HTTP 400, even
one the client recovered from by falling back to `json_object`), any other request failure, or a reply that does
not validate, `<role>: json_schema not honoured (<reason>); set structured = "json_object"`.
It exits 1 when a plain request fails.

## 5. Editing Starlark

### Reading

The builder reads a package's `.star` files with `libcst`, which parses Starlark as a syntactic subset of Python.
`read_package` takes the package directory and its world-relative path and returns a `PackageView`. It reads every
`*.star` file in the directory — not recursively — in alphabetical order, skipping `package.star`, `*_test.star`
and `*_testdata.star`, and records the sha256 of each file's bytes in `view.files`, so a hand edit is visible the
next time the package is re-read. A file that does not parse raises `StarParseError(file, line, message)` carrying
the parser's own line and message, and the package is not read.

Every Starlark expression in the package is mapped to a JSON `Value` — pydantic's `JsonValue` — so the model sees
exactly what is written:

| Starlark | Value |
| --- | --- |
| `None`, `True`, `3`, `3.5`, `"x"` | `null`, `true`, `3`, `3.5`, `"x"` — string escapes are decoded |
| `[a, b]` | `[A, B]` |
| `{"k": v}` | `{"$dict": [["k", V]]}` — keys are Values, order is kept |
| `DIR.north`, `SIZE.large` | `{"$const": "DIR.north"}` — any `NAME.attr` with an uppercase `NAME` |
| `exit(DIR.north, to = ":x")` | `{"$call": "exit", "args": [...], "kwargs": {"to": ":x"}}` — a call to a bare identifier whose arguments are all non-opaque Values |
| `_hall_enter` | `{"$ref": "_hall_enter"}` — any other bare identifier |
| anything else | `{"$opaque": "<exact source text>"}` — string concatenation (`"a" + "b"`), attribute access on a lowercase name, method calls, tuples, f-strings, prefixed strings, `*`/`**` arguments, everything the table does not name |

A Value is *editable* when it contains no `$ref` and no `$opaque` anywhere inside it; the modes only ever write
back editable Values.

A **declaration** is a top-level expression statement whose value is a call to a bare name in `DECLARATION_KINDS`
(`room`, `mob`, `item`, `skill`, `faction`, `region`, `container`). When its first positional argument is a simple
string literal it becomes a `DeclView`: the kind, the id, the file, the 1-based line of the call, the keyword
arguments in source order, and any positional arguments after the id. A call to one of those names anywhere else —
inside a `def`, a `for` or an `if`, or assigned to a name — is an `OpaqueDecl` with reason `not at top level`; a
call whose first positional argument is not a string literal is an `OpaqueDecl` with reason `id is not a string
literal`. Both carry the id when it is a string literal, so the builder can tell the user which declaration it is
leaving alone. `view.decls` is in file-then-line order and `view.get(kind, id)` returns the first match or `None`.
Line numbers and the exact source text of opaque values come from the parser's position metadata, so the view
reports the file and line the user sees in their editor.

### Writing

`apply_ops` takes the package's current file contents, its `PackageView` and a list of `StarOp`s and returns the
changed or created files plus every refusal. Ops apply in order and each one re-reads the declaration it targets
from the contents the ops before it left behind, so a later op may edit a declaration an earlier op added. An op
that cannot apply is refused with a reason written for the user and the remaining ops still apply. A file no op
touches is byte-identical, and an empty op list changes nothing. A file that does not parse raises
`StarParseError`, since nothing in that package can be located.

Each op, addressed by the kind and id of a plain declaration:

- **`SetArg`** replaces the argument's value expression in place, or appends the argument after the last one when
  it is absent.
- **`RemoveArg`** removes the argument. Comment lines the argument carried after itself are moved onto its new
  predecessor, so they stay where they sit in the file; removing the last argument keeps the closing `)` where it
  was.
- **`AppendItem`** adds an item to the end of a list argument, creating `name = [<item>]` when the argument is
  absent.
- **`RemoveItem`** removes the one item matching the given criteria, where an item matches when every criterion
  given (`call`, `first_arg`, `kwarg`) matches it. Comment lines between the removed item and the next one stay,
  above the next one, including when the first item is removed.
- **`AddDecl`** appends the rendered declaration to a file, creating the file with a one-line comment `# <Kind>s of
  <package path>, started by gups-builder.` when it is absent, and refusing when the id is already declared.
- **`RemoveDecl`** removes the declaration statement together with the comment lines that sit directly above it.
  For the first statement of a file, a comment block that starts on the file's first line is the file's header and
  stays; only comment lines after a blank line go with the declaration. When several statements share a line
  (`room("a"); room("b")`), only the named one is removed.
- **`AddTodo`** inserts `# TODO(gups-builder): <text>` on its own line directly above the declaration, after any
  comments already there. Text with line breaks becomes several comment lines, the later ones starting `#   `; an
  `AddDecl` comment with line breaks becomes several `# ` lines.

The refusals, with their exact reasons: a declaration that is absent or opaque is `<kind> '<id>' is not a plain
declaration in this package`; an `AddDecl` with an id that exists is `<kind> '<id>' already exists`; a
`SetArg`, `RemoveArg`, `AppendItem` or `RemoveItem` on an argument whose current value is not editable — it holds a
reference or opaque code anywhere inside it, so a list holding one computed item is refused as a whole — is `<kind> '<id>': <name> is computed by code (<file>:<line>); change it by hand`,
naming the argument's own line; a missing argument is `<kind> '<id>': <name> has no such argument`, a non-list one
is `<kind> '<id>': <name> is not a list`, and a `RemoveItem` matching zero or more than one item is `<kind>
'<id>': <name> has no matching item` or `… has more than one matching item`. An op whose new value is not editable
is refused, since the model may only write plain values: `<kind> '<id>': <name> may only hold a plain value` (for
an `AddDecl`, `new arguments of <kind> '<id>' must be plain values`). A value that cannot be written as Starlark
is refused the same way: a `$const` that is not `UPPER_NAME.attr`, a `$call` keyword that is not an
identifier, a `$call` callee whose dot-separated segments are not each identifiers (a plugin primitive's
dotted builtin name is allowed, §7.4), or a float that is infinite or not a number. As a last check, an op that
would leave a file it changed unparseable is refused with `this change would not be valid Starlark (<file>:<line>); it was not applied`, and
the files keep the contents the ops before it left. A `RemoveItem` that leaves an empty list
renders it as `[]`.

New code is rendered in the template's style and parsed back, so a value written on an indented line carries that
indent into its structure: `kind("id",` then one `name = value,` per line indented four spaces, then `)`. A list
goes one item per line with a trailing comma when it holds more than one call or its one-line form runs past 80
characters, otherwise it stays on one line; a call whose arguments include such a value goes one argument per line
the same way. Dicts render as `{"k": v}`. Strings are double-quoted with only the backslash, the quote and a
newline escaped; every other character, including non-ASCII ones such as `—` and `’`, is written literally as
UTF-8. A value rendered across several lines takes its indent from where it goes: an item from the list's own item
indent, an argument from the call's argument indent. Appending an item or an argument copies the whitespace of the neighbouring item or argument, so a multi-line
list stays one item per line and a one-line list stays on one line, and a new argument takes the file's
trailing-comma style.

`unified_diff` shows a change to the user before it is written: one unified diff per changed file, in name order,
with a file that exists only afterwards shown against `/dev/null` as a creation.

## 6. The area grid

Area rooms live on an integer grid `(x, y, z)`: `+y` is north, `+x` east, `+z` up. A
`GridRoom` pairs a room id with its coordinate; `external` marks a label outside the
package, shown for reference and never given a coordinate. A `Connection` joins two
adjacent rooms and may carry a `DoorSpec` (`open`, `closed` or `locked` state, a `key`
that is an item id of the package or a full label, and free-form `tags`). A `Portal`
is a named exit from room `a` (`a_exit`, any string) to room `b`, with the return name
`b_exit` left to the opposite direction when the model does not choose one.

The eight horizontal neighbours and straight up/down are the only adjacent pairs;
`direction_between` names the direction (`north`, `south`, `east`, `west`, the four
diagonals, `up`, `down`) and returns nothing otherwise. `derive_exits` turns the plan
into exits in the order of connections then portals: each connection yields its two
exits with the door on both sides; each portal yields the two named exits, where a
missing `b_exit` is the opposite of `a_exit` when `a_exit` is a standard direction and
is omitted otherwise (validation reports it). A room whose `exits` is written by code
in the files never gets an exit here: only the plan's rooms and exits exist.

`validate` returns every problem it finds, each a full sentence naming the rooms so
the model can repair its plan: two rooms on one coordinate (`'hall' and 'attic' share
the coordinate (0,0,0)`); a connection whose rooms are not adjacent (`'hall' (0,0,0)
and 'attic' (0,2,1) are not neighbours; use a portal`); a room using the same exit
name twice, counting portals (`'hall' uses the exit name 'north' twice`); an
entry room that is not in the plan (`the entry room 'foyer' is not in the plan`),
which also skips the reachability check; a connection or portal naming a room
that is not in the plan, once per missing end (`the connection from 'hall' names
the unknown room 'ghost'` when the second end is missing, `the connection to
'hall' names the unknown room 'ghost'` when the first is, and the same with
`portal`); a room not reachable from the entry room through exits, doors
counted as passable (`'cellar' is not reachable from 'hall'`); a room count outside
the requested range (`the plan has 2 rooms; the request calls for 4 to 8`); a locked
door without a key (`the locked door between 'hall' and 'armory' has no key`) and a
key that is not an item of the plan (`the door between 'hall' and 'armory' has the
key 'ghost', which is not an item of the plan`), each door reported once; a
non-external room without a coordinate that a connection joins (`'attic' has no
coordinate but a connection joins it; give it a coordinate or reach it only by
portals`) — a room without a coordinate is off the map, and only portals may
reach it; and a portal whose non-standard
`a_exit` leaves no return name (`the portal from 'hall' has the non-standard exit
'escape' and no return exit`). External rooms are exempt from the coordinate and
count rules.

`place_existing` puts the package's existing rooms (from `describe-world`) back on
the grid: it starts from the saved coordinates, or, when none are saved, puts the
first room by label at `(0,0,0)`, and walks breadth-first through exits whose
direction is standard and whose target is in the package, placing each target one
step away from its neighbour. A room its exits place on two different coordinates, a
coordinate another room already holds, or that they never reach, is returned in the
second list, off-map; a package with no rooms gives two empty results; exits to
other packages are ignored, and the mode that builds
the grid adds the external rooms they name.

`render_map` draws one `z` level, north at the top. Each room is a 3-character cell
`[A]` with a letter code assigned in id order on that level (`A`–`Z`, then `a`–`z`,
then `0`–`9`, then `?`), one block of seven columns per grid column: the cell and a
four-column gap. The first three gap columns are the room's markers, each in its
own column: `^` for an up exit, `v` for down and `*` for a portal. `-` fills the
gap columns the markers leave between east–west neighbours, so the last gap column
always shows the link (`[A]^v*-[B]`). `|` joins north–south neighbours under the
centre of the cell, `/` and `\` the diagonals in the row between, in the gap's
third column, and `X` stands where a `/` and a `\` cross. Below the map a legend names each cell:
`A hall: The Grand Hall`. A level with no rooms reads `no rooms on this level`;
off-map rooms are not drawn and the REPL lists them separately.


## 7. Modes

What each mode creates and edits, the pipeline they share, and the generated test.

### 7.1 The pipeline every mode shares

Every mode runs the same pipeline: the request goes to the logic model as a
brief; the brief becomes a plan; the plan is validated against the world's
schema and the API; the prose model writes the text; the plan becomes `StarOp`s;
the ops are staged against a copy of the world and run through `gups lint` and
`gups test`; and the result is a `Proposal` the user reviews before a write.

The shared helpers live in `modes/common.py`. `Context` carries one turn's
inputs: the world directory, the `describe-world` document and the `api.json`
model, the package path and its `PackageView` plus current file contents, the
`LlmClient`, the `Config` and an `Engine` — the protocol the modes and the REPL
need from `engine.gups.Gups` (`describe_world`, `api`, `lint`, `test`), which
tests answer with fakes. `Proposal` is the mode's answer: the ops, any new
files, a one-line summary, the new or changed text fields as `(label, field,
text)` previews, notes, the post-change room coordinates in area mode, and
`link_ops` — edits to another package, applied only when the user confirms —
with `link_package`, the package those edits apply to.

`stage_and_check` applies the proposal's ops to a temporary copy of the world
(the staging rules of §3), writes the new files and — when the mode supplies
it — the generated `builder_test.star` into the package directory of the copy,
and runs `gups lint` on the package. The check is clean when lint has no
errors; then, when the package is a zone of the `describe-world` document or a
generated test was staged, it runs `gups test` and reports a failing run's
output. A lint error stops the check before any test runs.

`finalize` drives the repair loop around a mode's build callback: it calls
`build([])`, adds the reason of every op the writer refuses to the proposal's
notes (the op stays in the proposal; the staged files simply lack it), stages and
checks the proposal, and — only when lint has errors —
calls `build` again with each error rendered as `<file>:<line>: <message>` plus
the three source lines around it, up to `config.max_repairs` repairs. A failing
generated test is not repaired by the model, because the exits, doors and the
test are all derived by Python: its output goes into the proposal's notes as one
note, `BUILDER BUG: the generated test failed; please report it:`, a newline and
the output. After the last repair, the remaining lint errors go into the notes as
`NOT VALID: <rendered error>`. Lint warnings always go into the notes
unchanged, whatever the outcome. A first clean check returns the first proposal
with exactly one build call.

`allowed_values` and `param_allowed` read `api.json`: the allowed values of a
declaration's parameter, and whether the parameter is usable in this world —
false when the parameter's `feature` is set and the world does not have it, or
when the declaration or parameter is absent from the API. `new_id` turns a
wanted name into a snake_case id matching `[a-z][a-z0-9_]*` — non-alphanumerics
become underscores, a leading digit gets an `id_` prefix, an empty result
becomes `decl` — and appends `_2`, `_3`, … until it clashes with no existing id.

Prompts are Markdown files in `prompts/` with `{field}` placeholders; a missing
field is an error. `system_logic` tells the logic model it plans GUPS content
and replies with JSON only, following the schema it is given. `system_prose`
tells the prose model to write evocative, concise MUD prose in the second
person present tense, never mentioning mechanics or numbers. `repair_lint`
takes the rendered `problems` and the `previous` reply. Every mode builds its
prompt from `(priority, text)` sections: priority 0 is the request, the rules
and the current target declarations, never dropped; priority 1 is the schema
and the API entries the step needs; priority 2 is balance references and
candidate labels from other packages. The sections are kept in order and the
least important are dropped until the estimate — a token is about four
characters, `len // 4 + 1` — fits the role's budget, `context_tokens -
max_tokens`.

A follow-up message (not the first request in a mode) goes to the logic model
as a **turn**, a `Turn` with a `kind`. An `edit` turn carries the mode's edits
and becomes a proposal through the same `finalize` loop, so every edit turn is
staged, linted and tested before the user sees it. A `question` or `chat`
turn returns only its `reply`, with no proposal and no edits. An
`out_of_scope` turn names the other mode that owns the request in `other_mode`
and keeps the request itself in `note`, so the builder records it and the user
can switch modes without re-typing it.

### 7.2 area

The area mode owns zone packages: it creates them and refines them. Creation
runs the brief, plan, prose and ops pipeline inside `finalize`.

**Brief.** The request goes to the logic model with the size words — `tiny`
2–4 rooms, `small` 4–8, `medium` 8–15, `large` 15–30 — and the rooms already in the
package, all at priority 0, and the rooms of other packages that could be linked
(label and name) at priority 2. The reply is a
brief: a title, a theme, style notes, the room count as a minimum and maximum,
a short concept for every intended room, and a link — the label of an existing
room to connect to, or none. When the request leaves something important open,
the brief carries questions and the mode returns it as-is, with no further
calls.

**Plan.** The brief goes to the logic model with the coordinate rules: rooms
sit on an integer grid (x, y, z), with +y north, +x east and +z up; the eight
horizontal neighbours and straight up and down are adjacent; adjacent rooms are
joined with a connection, anything else with a portal; no two rooms share a
coordinate; every room is reachable from the entry room; and a locked door
names its key. The room sizes the model may choose are the `size` parameter's
allowed values, or a note that the world has the track feature off. The reply
is a plan: rooms with fresh snake_case ids, concepts, coordinates, sizes and
tags; a connection for every pair of adjacent rooms, with a door when one
belongs there; a portal for every non-adjacent link; a key for every locked
door; the entry room; an optional link — one room, its exit, the external
room's label, and the return exit; and todos.

The plan is validated before any prose is requested. Every problem is a full
sentence the model can act on: two rooms sharing a coordinate; a connection or
portal naming an unknown room; a connection whose rooms are not adjacent, with
the instruction to use a portal; a portal with a non-standard exit and no
return exit; a room using one exit name twice; a room unreachable from the
entry; a room count outside the brief's range; a locked door without a key; a
door whose key is not an item of the plan — the check knows the plan's own key
ids, the item ids already declared in the package and every item label in the
world; and a coordinate-less room joined by a connection. The rest of the plan
is checked too: a room or key id that is not snake_case (`the id 'Silver Key' is
not snake_case ([a-z][a-z0-9_]*)`), used twice in the plan (`the id 'hall' is
used twice in the plan`) or already declared in the package (`the id 'hall' is
already declared in the package`); a todo naming a room the plan lacks (`the todo
names the unknown room 'attic'`); and a link that starts from an unknown room
(`the link starts from the unknown room 'x'`), whose external room is not a room
of the world (`the link's external room '//zones/x:y' does not exist`) or is in
this package (`... is in this package`), whose non-standard exit has no return
exit (`the link from 'hall' has the non-standard exit 'escape' and no return
exit`), or whose return exit — given, or defaulted to the opposite — the external
room already has (`the link's return exit 'west' is already an exit of
'//zones/tutorial:gate'; choose another return_exit`). The link's exit counts
among its room's exits, so it may not reuse a
derived exit's name. A failing reply goes back with the problems until the plan
is sound.

**Prose.** One prose call per room, with the theme, the style notes, the room's
concept and size, and its neighbours as `direction: concept` lines from the
derived exits, with the door's state; the reply is a name and a
single-paragraph desc of 2 to 4 sentences that does not list the exits. One
prose call per key, with the theme and concept; the reply is a name, keywords
and a one-sentence desc. An empty name or desc, a newline in either, or a name
over 60 characters is returned to the model with the complaint. When a lint
repair rebuilds the plan, the plan call gets the original plan prompt followed by
`repair_lint` with the errors and the previous plan, and prose is re-requested
only for rooms whose concept, size, coordinates or neighbours changed, and for
keys whose concept changed; the rest keep their earlier text.

**Ops.** For each room, an `AddDecl` into `rooms.star` with the prose name and
desc, the size as `SIZE.<size>` only when the world allows the parameter and
the room is not `normal`, tags when there are any, and the exits in derived
order: a standard direction as `DIR.<direction>`, a portal's custom name as a
plain string, the target as `:<id>` for a room of the plan and the raw label
otherwise, and the door as a `door` call with its state, its key as `:<id>`
when the plan named it by id and left alone when it is already a label, and
its tags when any. For each key, an `AddDecl` into `items.star` with the prose
name, keywords and desc, and `kind = "key"`. A todo becomes an `AddTodo`. When
the plan carries a link, the new room gains an exit to the external label (a
standard direction as `DIR.<direction>`, as for every exit), and the return exit
— an `AppendItem` on the external room's exits, named by the plan's return exit
or else the opposite of the link's exit, leading to `//<package>:<room>` — goes
into `Proposal.link_ops`, with the external room's package in
`Proposal.link_package`, applied only when the user confirms. A package with no
`package.star` on disk also gets one: the world's api version,
`kind = "zone"`, the brief's title, and `reset_world_ticks = 10`.

The summary is `+<n> rooms, +<n> item(s), <n> exits`, counting the exits of
the plan's connections and portals, and gains `, 1 link to <label> (needs
confirmation)` when there is a link. The proposal's grid is one coordinate
triple per room id. The generated test is computed from the package as it
stands after the ops, so the staged check runs it.

**Refinement.** A follow-up message in area mode goes to the logic model with
the current rooms — id, name, coordinate or `off-map`, exits with their door
state and key, spawns, and the fields computed by code, which the model must
not edit — a note that only a portal may reach an off-map room, the last six
messages of the conversation, the edit catalogue (one line per edit), and the
message. Every mob in the world that may be spawned (label, name, level) is a
section of its own at priority 2. The reply is an `AreaTurn`.

The turn's edits are checked before any prose is requested. They are applied
to a copy of the grid and checked with the same `validate` as creation, but
only problems the edit introduces are returned — the diff against the grid
before the edit — so a room that was already off-map is not blamed again.
Known keys are this package's items, every item label in the world, and any
key the same turn adds. Each edit is also checked on its own, in order: an `add_room` id must be a
valid id and not an existing room; a room an edit names must exist (or be
added earlier in the turn); a `set_door` or `disconnect` needs a passage
between the two rooms; a size must be one of the world's room sizes; a spawned
or unspawned mob must be a mob of the world, and a spawn's count at least 1;
an `add_key` id must not be an existing item; and an edit naming a room the
reader could not hold as a plain declaration is a problem of its own. Each
problem is a sentence naming the rooms, sent back for repair.

**Edits.** An `add_room` adds a room at its coordinate with new prose; the
room's exits come from the connections drawn to it. A `remove_room` takes the
room and every exit in the package that points at it. A `connect` or
`set_door` draws or re-draws a passage; a `disconnect` removes one; an
`add_portal` adds a portal's named exits. Exits are derived on both sides and,
for a door, the door rides on both. The exits are turned into ops per passage
an edit touches, on each side that stays, by comparing the derived exits with
the exits the room has now (as `describe-world` reports them): an exit that
went away or whose door changed is a `RemoveItem` matched by its direction, an
exit that is new or changed is an `AppendItem`, and an existing exit in the
same direction to another room is removed first. A door is the same when its
state, its key as a full label and its tags agree, so a key written `:key` and
reported as `//<package>:key` is not a change. A passage that already matches
produces no ops, and a one-way exit gets only its missing side. A room being
added or removed has its exits in its `AddDecl` or `RemoveDecl`.

A `set_room` changes a room's size, tags or regions as `SetArg`s; a size is
left unchanged, with a note, when the world has the track feature off. A
`rewrite` rewrites the description of the named rooms; the prose model gets
the room's name, its current description and the guidance, and only `desc`
changes. A new room's prose names its neighbours by room name (the concept for
a room added in the same turn) with door states. An `add_key` adds a key item
with its prose. An `add_spawn` appends a `spawn` to a room's spawns, writing
this package's mobs as `:<id>`; a `remove_spawn` matches the spawn by the
label as the file writes it, short or full. An `add_todo` leaves a note. When
an edit would touch a value the reader holds as a `$ref` or `$opaque` — a room
whose exits are computed by code — the writer refuses that one op with its
file and line; the reason goes into the proposal's notes and the rest of the
turn stands.

**Coordinates.** The builder keeps one coordinate per room id, passed in and
carried out by the turn. The proposal's `grid` holds every placed room's
coordinate after the edit; the caller's coordinates are updated from it, and a
removed room's is dropped, so the rooms the edit does not touch keep theirs
and the map is stable across turns. Coordinates come only from the turn the
proposal was finally built from: a turn rejected by a lint repair leaves none
behind. The turn is staged and checked through `finalize` as every other
build; a lint repair re-asks the model for a corrected turn, sending its
previous reply, and the corrected turn is the one returned; if the correction
is no longer an edit, it is returned with no proposal. A refused op is noted,
not repaired.

### 7.3 mob

The mob mode owns the mobs of a package: it creates them — with their loot
items and placement spawns — and refines them. It requires an existing package.
`gups-builder mob <package>` refuses a package with no `package.star` with
`<package> does not exist; mob mode needs an existing package. Create it first
with: gups-builder area <package>`, and a mob-mode turn on a package that
`describe-world` does not list says `<package> does not exist; mob mode needs
an existing package. Create it first with /mode area.` and makes no model call.
A package with no rooms is fine — other packages can spawn its mobs — but its
mobs can have no spawns of their own. A request for a room or a new area
answered in mob mode is an `out_of_scope` turn naming `area`.

Item, skill, mob and faction labels may be written bare (`goblin`), short
(`:goblin`) or in full (`//zones/caves:goblin`); every check compares them in
full, a bare or short label being taken to be in the package. The package's own
labels of those kinds are taken from its files as well as from
`describe-world`, so a declaration made since the last load counts. A room is
named by its bare id.

**Brief.** The request goes to the logic model with the package's rooms and the
mobs already in the package, both at priority 0, and the balance references at
priority 2: the package's own mobs, then the ten mobs of other packages
closest in level to the median level of the package's mobs (of the whole
world's when the package has none yet), each with its level, stats and vitals. The reply is a brief:
a short concept for every intended mob — a variant, like a shaman among
goblins, is its own concept — style notes for the prose, and questions when the
request leaves something important open. A brief with questions is returned
as-is, with no further calls.

**Plan.** The brief goes to the logic model with the stat, vital and damage
type names with their labels, the sense channels, the world's features on, the
world's skills (label, kind, target — a
note that the world has the skills feature off when it does not), the factions
(a note that the standing feature is off when it is), the plugin traits, and
the ids already declared in the package, all at priority 0; the allowed
behaviors with their parameters and docs at priority 1; and the balance
references at priority 2. The reply is a plan: a mob for every
concept, each with a fresh snake_case id and its declaration fields: level,
stats, vitals, resists, skills as skill label to proficiency, senses,
behaviors, factions, traits, loot, spawns and todos. A level, skills, behaviors
and factions exist only when the world has the matching feature on.

The plan is validated before any prose is requested, with the same full-sentence
problems as every other check: an id that is not snake_case, used twice in the
plan, or already declared in the package (by a declaration of any kind, since
labels are package-wide); a stat, vital or resist name not in
the world's schema, with the allowed names listed; a sense channel not in the
schema; a level when the leveling feature is off; skills when the skills
feature is off, or a skill label that is not a skill of the world; a
proficiency outside 0–100; a behavior that is not in the world's API, or a
behavior or argument gated on a feature the world has off; factions when the
standing feature is off, or a faction that is not a faction of the world; a
trait no plugin of the world declares; a spawn naming a room the package does
not have, or with a count below 1; a loot item that is neither an item label of
the world nor a valid new id of this package with a concept; a new item id
already declared in the package or used by a mob of the plan; and a loot
chance outside 1–100. A loot entry with a concept that names an existing item
uses that item and creates nothing.
A failing reply goes back with the problems until the plan is sound.

**Prose.** One prose call per mob, with the brief's style notes, the mob's
concept, level, stats, vitals and behaviors; the reply is a name, keywords and
a single-paragraph desc. One prose call per new loot item, with the theme and
the loot's concept; the reply is a name, keywords and a one-sentence desc. An
empty name or desc, a newline in either, or a name over 60 characters is
returned to the model with the complaint. When a lint repair rebuilds the plan,
prose is re-requested only for mobs whose concept, level, stats, vitals or
behaviors changed, and for loot items whose concept changed; the rest keep
their earlier text.

**Ops.** For each mob, an `AddDecl` into `mobs.star` with the prose name,
keywords and desc, then `level` when there is one, `stats`, `vitals` and
`resists` as `stats(...)`/`vitals(...)`/`resists(...)` calls with the plan's
values as keyword arguments, `skills` and `senses` as dicts of label to
number, `behaviors` as a list of calls, `factions`, `traits`, and `loot` as
`loot(...)` calls with the item's label as the first argument — `:<id>` for an
item of this package, the label in full otherwise — and `chance` omitted when
it is 100. Empty fields are omitted. Each new loot item is an `AddDecl` into
`items.star` with its prose and the loot's kind, once however many mobs carry
it. Each spawn is an `AppendItem`
on the room's `spawns`, the mob written `:<id>` when it is in this package and
in full otherwise. A todo becomes an `AddTodo`. The summary is
`+<n> mobs, +<n> item(s), <n> spawn(s)`.

**Stat balancing.** The plan prompt's balance section is what the model is told
to keep consistent: the new mob's level, stats and vitals stay in the
neighbourhood of the mobs the world already fields — a little below the mobs
that sit in the same rooms it will — and a mob that fights harder than its
neighbours gets the vitals to match.

**Refinement.** A follow-up message in mob mode goes to the logic model with
the package's mobs — id, name, level, every field rendered as a value with
calls named and constants bare, the fields computed by code marked `do not
edit these`, and the mobs the reader could not hold as plain declarations
marked — the edit catalogue (one line per edit), the last six messages of the
conversation, and the message. The reply is a `MobTurn`.

The turn's edits are checked before any prose is requested, in order: an
`add_mob` is checked like a one-mob plan, against the ids already declared and
the ids the same turn adds; a `remove_mob`, `set_mob`, `add_loot`, `remove_loot`
or `rewrite` must name a mob of the package; a `set_mob` field must be a field
of the mob declaration, usable in this world, and its value a plain one; an
`add_loot` is checked like the plan's loot; a `place` or `unplace` must name a
room of the package and a mob the package or the world has, and a place needs a
count of 1 or more; a `set_mob` of `level`, `stats`, `vitals`, `resists`, `skills`, `senses`,
`behaviors`, `factions` or `traits` gets the plan's checks for that field
(`stats(...)`, `vitals(...)` and `resists(...)` must be calls of whole numbers
by name, `skills` and `senses` dicts of name to number); a `remove_loot` must
name an item the mob drops and an `unplace` a mob the room spawns, when those
lists are plain; an edit naming a mob the reader could not hold as a plain
declaration is a problem of its own. A field of a plain mob that is computed by
code is not a problem here: the writer refuses the op, as in area mode. Each problem is a sentence naming the mob,
sent back for repair.

**Edits.** An `add_mob` adds a mob with new prose, its new loot items, its
spawns and its todos. A `remove_mob` takes the mob and every spawn of it in the
package's rooms. A `set_mob` becomes one `SetArg` per field; a field the reader
holds as a `$ref` or `$opaque` is refused by the writer with its file and line,
and the reason goes into the proposal's notes. An `add_loot` appends the entry,
creating the item with its prose when the loot has a concept and names no
existing item. Since a turn has no brief, a new mob's prose takes the user's
message as its style notes, and so does a new item's, except that an item
created with an `add_mob` takes that mob's concept as its theme. A `remove_loot`
takes the entry out, matched by the item's label as the file writes it,
whatever form the edit names it in. A `place` appends a spawn to the room's spawns; an `unplace` takes it out,
matched the same way. A `rewrite` rewrites the description of the named mobs;
the prose model gets the mob's name, its current description and the guidance,
and only `desc` changes. The turn is staged and checked through `finalize` as
every other build.

### 7.4 skill

The skill mode owns the skills of a package: it creates one skill of any `kind`
with a declarative effect and its `unrecognized` lines, optionally teaching it
to mobs of the package, and refines skills. It needs the `skills` feature: a
skill-mode turn on a world without it says `This world has the skills feature
off.` and makes no model call. A package may be a `skills` or a `zone` package.
`gups-builder skill <package>` refuses a package with no `package.star` unless
its path starts with `skills/` — the first write creates that file with
`kind = "skills"` and the skill's concept as the package's title. A package that
`describe-world` does not list and is not under `skills/` is refused with
`<package> does not exist; skill mode needs an existing package. Create it first
with /mode area.` A request for a room or a new area is an `out_of_scope` turn
naming `area`; a request for a new mob, one naming `mob`. A skill's `resolve`
function and cast hooks are out of scope: the model puts them in a skill's
`todos`, which become `AddTodo` ops.

**Brief.** The request goes to the logic model with the package's existing
skills and mobs, at priority 0; the reply is a brief: a short concept for the
one intended skill, its kind, style notes for the prose, and questions when the
request leaves something important open — a request for several skills is a
question, or the first one with a note. A brief with questions is returned
as-is, with no further calls.

**Plan.** The brief goes to the logic model with the vital names (for `cost`),
the damage types, the sense channels, whether `track` is on, the allowed
`range` values (without `melee`, `near` and `far` when `track` is off) and
`target` values, the ids already declared in the package, all at
priority 0; the effect primitives with their parameters and docs, and the
world's plugin effects by name, at priority 1; and the existing skills of the
package and a few elsewhere as balance references (kind, cost, warmup, lag,
effect — the package's own effects as their files write them, rendered with
calls named and constants bare, when they are plain values) at
priority 2. The reply is a plan: a fresh snake_case id and the skill's fields —
cost, warmup, lag, range, target, channels, interrupt, tags, the effect, the
mobs it teaches with their proficiencies, and todos.

The plan is validated before any prose is requested, with the same full-sentence
problems as every other check: `range` when `track` is off and it is `melee`,
`near` or `far`, or a `range` outside the allowed values; a `target` outside
them; a cost vital not in the world's schema; an effect primitive that is in
neither the world's API nor its plugins, or an argument name it does not have;
an effect argument or `interrupt` entry that is not a plain value (the writer
would refuse the whole declaration); `sequence` without `effects`, or `effects` on any other primitive; `knockback`
or `pull` when `track` is off; for `target = "room"`, a primitive other than
`apply_affect`, `dispel`, `sequence` or a plugin effect; for `target = "item"`,
one other than `apply_affect`, `sequence` or a plugin effect; a damage `type`
not in the schema; a `teach` that names a mob not of the package, or a
proficiency outside 0–100. A failing reply goes back with the problems until
the plan is sound.

**Prose.** One prose call, with the brief's style notes, the skill's concept,
kind, target and range; the reply is the six `unrecognized` lines — `noun`,
`begin`, `room`, `victim`, `resisted_room` and `resisted_victim` — each a
single non-empty line or absent. A multi-line or empty value, an unknown
placeholder (only `{actor}` and `{target}` may appear), `{target}` on a skill
whose target is `self`, `none` or `room`, and `{target}` in `room` when the
range is `room`, are returned to the model with the complaint. The `resisted_*`
lines are requested only for a skill whose target is `enemy`. When a lint
repair rebuilds the plan, prose is re-requested only when the skill's concept,
kind, target, range or the brief's notes changed.

**Effect.** The effect is written as nested calls: each primitive is a call
with its arguments as keyword arguments, and `sequence` combines effects passed
as its positional arguments. A damage `type` is the schema's constant, written
`DAMAGE.<type>`. A plugin effect is the plugin's effect primitive, written as a
call on the plugin's dotted builtin name — `combust.ember(radius = 2)` — the
form the engine binds it in.

**Ops.** The skill is an `AddDecl` into `skills.star`, or into the package's one
existing file holding skills when there is exactly one. Its arguments are
`kind`; `cost` as a `vitals(...)` call; `warmup` (omitted when 0); `lag`;
`range`; `target`; `channels`; `interrupt` as a dict; `tags`; `unrecognized` as
a dict of the lines that are present; and `effect`, in the form above. Each
`teach` is a `SetArg` on the mob's `skills` dict as its file writes it: the
skill's entry — `:<skill>` for a skill of this package, else its full label —
at the new proficiency. A skill the mob already knows, under either spelling of
its label, has its entry replaced in place, since a Starlark dict literal may
not repeat a key; every other entry is kept as written. The op is refused when
the mob's `skills` is not a plain value. Each `todo` is an `AddTodo`.
The summary is `+1 skill`, with `, <n> mob(s) taught` when there are teaches.
A skills package has no generated test — its own tests are its business — so the
build is staged and checked through lint alone.

**Refinement.** A follow-up message in skill mode goes to the logic model with
the package's skills — id, kind, every field rendered as a value with calls
named and constants bare, the fields computed by code marked `do not edit
these`, and the skills the reader could not hold as plain declarations marked —
the edit catalogue (one line per edit), the last six messages of the
conversation, and the message. The reply is a `SkillTurn`.

The turn's edits are checked before any prose is requested, in order: an
`add_skill` is checked like a one-skill plan, against the ids already declared
and the ids the same turn adds; a `remove_skill`, `set_skill`, `set_effect` or
`rewrite` must name a skill of the package; a `set_skill` field must be a field
of the skill declaration, usable in this world, and its value a plain one of
the field's shape (`range` and `target` checked as in the plan, `track` included,
and `cost` as a `vitals(...)` call of schema vitals); a
`set_effect` is checked like the plan's effect; a `teach` must name a mob of the
package with a proficiency of 0–100 — the skill it teaches need not be declared
here, since the teach only adds a label to the mob's `skills` and the staged
lint decides whether that label resolves; an edit naming a skill the reader
could not hold as a plain declaration is a problem of its own. A field of a
plain skill that is computed by code is not a problem here: the writer refuses
the op and the reason goes into the proposal's notes. Each problem is a
sentence naming the skill, sent back for repair.

**Edits.** An `add_skill` adds a skill with new prose, its teaches and its
todos. A `remove_skill` takes the skill. A `set_skill` becomes one `SetArg` per
field; a `set_effect` a `SetArg` of the effect in the form above. A `teach`
becomes a `SetArg` on the mob's `skills` dict, as in creation. Since a turn has
no brief, a new skill's prose takes the user's message as its style notes. A
`rewrite` rewrites the `unrecognized` lines of the named skills; the prose model
gets the skill's id, target and range, its current lines and the guidance; its
reply is checked as creation's prose is, against that target and range; and
only `unrecognized` changes. The turn is staged and checked through `finalize`
as every other build.

### 7.5 The generated test

Every zone package carries a `builder_test.star` the builder owns and rewrites
in full on every write, so the staged check always runs it. A package the
`describe-world` document lists with another kind gets no test; a package it
does not list yet — a zone being created — gets one.

The file begins with two comment lines naming the builder and telling the user
to put their own tests in another `*_test.star`, then a
`test_builder_package_loads` function that loads the world with `seed = 1`.

After that, one function per room with at least one testable exit, named
`test_builder_<id>_exits`. An exit is testable when its direction is one of the
ten standard directions, it has no door or an open one, and its target is a
room of the same package, named `:<id>` or `//<package>:<id>`. Each function
loads a fresh world and walks each testable exit from the room the test names:
the first exit spawns `Walker` in the room, and every later exit spawns a new
player there (`Walker2`, `Walker3`, …) instead of walking back, so a one-way exit
or a door on the far side never breaks the test. The player moves in the
direction and the test asserts the target room.

Code can veto or redirect a move — a room's exit hook, a target's enter hook, a
mob's `before_exit` — and the builder cannot tell what it does. So a room with
any field computed by code (a `$ref` or `$opaque` value) gets no exit tests,
nor does a room that spawns a mob that has such a field, a mob of another
package, or a mob that is not a plain declaration; and no exit into a room with
code, or into one the reader could not hold as a plain declaration, is tested.
Those rooms are left to the package's own tests.

After the exit tests, one function per plain mob whose `vitals` is a
`vitals(...)` call with at least one declared vital, named
`test_builder_<id>_vitals`: it loads a fresh world, spawns the mob in the first
room whose spawns name it — else in the package's first room — and asserts
`t.eq(m.max_<vital>, <value>)` for every declared vital, in the order the file
writes them. A mob with any field computed by code is left to the package's
own tests, and a package with no rooms gets only the load test, because its
mobs cannot be spawned anywhere it owns.

## 8. Sessions and the interactive loop

A **session** is one interactive conversation about one package in one mode. Its state is a JSON document at
`<world>/.builder/sessions/<name>.json`, written atomically — to a temp file, then `os.replace` — so a crash never
leaves a half-written file. The first use of the builder in a world drops a `<world>/.builder/.gitignore` holding `*`
so nothing in `.builder/` is committed by accident. The state holds: a `version` (1), the `name`, the `mode`
(`area`, `mob` or `skill`) and the `package`; `created` and `updated` timestamps; the `history`, a list of
(`user` or `builder`, text) pairs; the `grid`, one coordinate triple per room id; the `notes`, a list of
(mode, request) pairs for out-of-scope turns; the `pending` change; the `builder_test_hash`, the sha256 of
`builder_test.star` as last written; the `undo` map, file to its previous content (`None` when it did not exist), and
`undo_grid`, the grid before the last write; and the `written_hashes`, file to sha256 right after the last write.
`undo` and `written_hashes` key each file by its path in the world (`zones/mansion/rooms.star`), so they cover the
linked package's files too. The pending change is the proposal the user has not yet written: its ops, new files, link
ops and the package those edit, the sha256 of the files the ops were built against, a one-line summary, its notes,
and the room coordinates after the change.

The session name defaults to the package's last path segment. `gups-builder area <package> [--name N]` offers to
resume when a session of that name (given or default) exists for the same package; declining, a session of that
name on another package, or no such session, creates a new one,
taking the next free `-2`, `-3` suffix when the name is taken and printing the name it chose. `gups-builder sessions`
lists each session's name, mode, package and `updated`; `gups-builder resume <name>` loads one and enters the loop, and
exits 1 when there is no session of that name.

The loop re-reads the package before every model turn and every command that reads it (`/map`, `/show`, `/diff`,
`/write`) and on `/reload`: it hashes the files and rebuilds the context — re-reading the package and calling
`describe-world` (with `docs --json` once per process) — only when a file's hash has changed or `/reload` was typed.
Before it talks to a model it checks the target package in `describe-world`; one listed with `ok: false` prints its
errors and `Fix these first (the package doesn't load); /reload when done.` and sends nothing to a model, though
`/reload`, `/show`, `/diff`, `/map`, `/notes`, `/help` and `/quit` still work. A package file that does not parse, or a
`describe-world` that fails, leaves the loop with no context: it prints the error (`<file>:<line>: <message>` for a
parse error) and the same `Fix these first` line for every model turn and for `/map`, `/show`, `/diff` and `/write`,
until the files parse again. Otherwise, plain text in area mode goes to `create_area` when the files hold no room
declaration and no opaque room — the decision is made from the files, not from `describe-world`, so a package the
engine has not loaded yet still creates — and to `area_turn` otherwise, which is given the history before the current
message. A brief with questions prints the questions. A proposal becomes the pending change, replacing any earlier
one, and the loop prints its summary, its notes, the map of each affected `z` level — every level holding a room the
proposal adds, moves, removes or edits, drawn from the package as it stands after the proposal, not as the world
knows it — and the new or changed prose. A proposal's room coordinates become the session's `grid` only on `/write`,
so a discarded proposal leaves none behind. An out-of-scope turn adds to the notes and prints the turn's reply with
`Noted for /mode <other>.`. A `question` or `chat` turn prints its reply and nothing else. Everything the loop prints
is plain text, never rich markup. The session is saved after every handled line. `Ctrl-C` during a command or a model
call cancels it and returns to the prompt, and at the prompt it starts a new line; `Ctrl-D` quits. A model call that
fails (an HTTP error, or no valid reply after the repairs) or an engine command that fails is printed as
`error: <message>` and the loop carries on.

The slash commands, exactly: `/map [z]` draws the map of the current package — every level with rooms by default, or
one `z` — with the off-map rooms below it. `/show <id>` prints a declaration's source text, from its file. `/diff`
shows the pending change as a unified diff against the files as they are now, including the regenerated
`builder_test.star`. `/write` writes it, in five steps: it re-reads the files and re-applies the pending ops to them,
saying so when they changed since the proposal and printing each refusal; a new file that now exists is left as it is.
It asks `Also add the return exit in <package>? [y/N]` when there are link ops and applies them only on `y`, to the
external package (Ctrl-D there answers N). It regenerates `builder_test.star` for a zone package and overwrites it only when it is the
builder's own: absent, matching the hash last written, or exactly the text the builder generates for the files before
the change (as another session on the package would have written it). Otherwise — a hand edit, or a file of that name
the builder did not write — it writes `builder_test.star.new` and says so. Files whose new text equals what is on disk are
left out; when nothing is left it says `nothing to write`, clears the pending change and keeps the last write's undo
record. Otherwise it records each file's previous content and the grid for `/undo`, writes every file atomically, and records the written hashes, the new `builder_test` hash and the
proposal's coordinates as the grid. Last, it runs `gups lint` on the package, prints the result, clears the pending
change and saves. `/discard` clears the pending change. `/undo` restores the recorded contents — deleting files that
were `None`, in the linked package too — with the `builder_test` hash and the grid as they were, and re-lints, but
only while every written file still matches its recorded hash; otherwise it refuses with `<file> changed since the
last write; /undo would lose those changes`. `/reload` rebuilds the context and names the files whose hashes changed.
`/mode area|mob|skill` switches the session's mode, keeps the package, and prints any request noted for the new mode.
`/notes` lists the noted requests. `/help` lists the commands; an unknown slash command prints `/help`; `/quit` leaves.
A package directory that does not exist is created when the first proposal is written, in area mode or in skill
mode for a new package under `skills/`.

## 9. Testing

The suite has three layers, and `uv run pytest` runs all of them.

**Unit tests** drive the modes, the writer, the grid and the REPL without a model and without the engine: a
`FakeLlm` replays the scripted replies of a logic or prose call, one per call, matched by role and in order,
and a fake `Engine` answers `describe_world`, `api`, `lint` and `test` from fixtures. These tests never send an
HTTP request and never run `gups`.

**Engine tests** are marked `@pytest.mark.engine` and need a real `gups` binary. A test that cannot find one
(`Gups.find` fails: no `gups` setting, no `GUPS_BIN`, nothing on the `PATH`) skips itself, as does a test that
needs the template world and cannot find it (the `GUPS_TEMPLATE_WORLD` directory, else
`../gups-engine/template-world` relative to the repository root, copied to a temporary directory before anything
touches it).
They check that the output the builder writes is what the engine accepts: a scripted package staged into a copy
of the template world passes `gups lint` and `gups test`.

**The end-to-end test** (`tests/test_end_to_end.py`) is the one conversation across all three modes, and it runs
only through `Repl.handle` lines against a copy of the template world with a real `Gups` engine and a scripted
`FakeLlm`: area mode creates a mansion and writes it; a hand edit to a room's description survives a later
refine turn that adds a wine cellar under the kitchen, and its write; mob mode adds a ghost that spawns in the cellar; skill mode adds a spell taught to
the ghost. After every write the real `gups lint` has no errors, and after the last write `gups test` of the
package passes.

Run with `uv run pytest`; the engine layer skips itself when no `gups` binary is reachable. Point the suite at
a binary with `GUPS_BIN=/path/to/gups uv run pytest` or by putting `gups` on the `PATH`, and at a template
world other than the sibling checkout with `GUPS_TEMPLATE_WORLD=/path/to/world uv run pytest`.

## 10. Open choices

Decisions made to fill gaps the spec does not settle, each noted with the phase that made it, plus what is not built.

- **Phase 1:** `ruff` is configured with `extend-exclude = ["docs"]` so `ruff format` does not reformat the Python
  code blocks embedded in the milestone documents, whose compact class definitions are intentionally non-conforming.
  Documentation prose is not linted either.
- **Phase 2:** `ApiModel.declaration` raises `KeyError` for an unknown declaration, where `effect` and `behavior`
  return `None`; the milestone's signatures imply the contrast but not the exception type. `label_exists` also
  counts faction labels, since the document lists them alongside the other declared labels. `WorldDescription`
  keeps the document's `schema` field name even though it shadows pydantic's deprecated `BaseModel.schema`; the
  warning is silenced at class creation.
- **Phase 3:** a model call that fails outright — a timeout, a non-200 reply, or a second 400 after the
  `json_schema` fallback — is recorded in the call log with an empty `reply` and the `LlmError` message as its
  `error`, so the log shows every attempt the builder made, not only the ones that got a reply.
- **Phase 4:** a line holding several `;`-separated expressions gives each of them declaration status. A first
  argument given as a keyword (`room(name = "x")`) counts as no id at all, so the declaration is opaque with reason
  `id is not a string literal`. A prefixed string (`f"…"`, `b"…"`, `r"…"`) is not a plain string literal: it becomes
  `$opaque` and is never decoded. `PackageView.get` returns the first declaration in file-then-line order when kind
  and id clash; the engine flags the duplicate when it loads.
- **Phase 5:** each `StarOp` model gives its `op` discriminator a default, so the modes can build ops by class
  (`SetArg(kind=…, id=…, name=…, value=…)`) without restating the type; validation of a `StarOp` from JSON still
  discriminates on `op`. The refusal reasons for a missing argument, a non-list argument, and an op whose new value
  is not editable (`has no such argument`, `is not a list`, `may only hold a plain value`) are this phase's own
  wording; the milestone names only the declaration-not-plain, already-exists, computed-by-code and zero-or-multiple
  match refusals. A `RemoveItem` that leaves no items renders the argument as `[]`.
- **Review of Phases 1–5:** these choices were made while fixing review findings, each recorded here against the
  phase it belongs to.
  - Phase 1: the environment covers every `RoleConfig` field with a variable, adding `..._MAX_TOKENS` and
    `..._TIMEOUT_S` to the milestone's list. An empty `api_key` counts as unset. `.gitignore` holds only the
    milestone's Python entries.
  - Phase 2: a `gups` path set in the configuration or `GUPS_BIN` that is not executable is an error rather than
    falling through to the `PATH`, so the builder never runs an engine the user did not choose. `lint` raises
    `EngineError` on a non-zero exit with no findings, since the milestone's "whatever its exit code" would
    otherwise report a usage error as clean lint.
  - Phase 3: `HttpLlm.last_fallback` records whether the last call fell back from `json_schema` to `json_object`.
    `models` reads it, since the reply alone cannot show the fallback. The `models` schema check sends its prompt
    as a user message. A schema with no user message to carry it is sent as a user message of its own.
  - Phase 5: multi-line TODO and comment text is split into comment lines rather than refused. `RemoveDecl` of a
    file's first statement takes only the comment lines after the header's last blank line, since libcst puts
    every comment above the first statement into the module header.
- **Phase 8:** the summary says `1 item`, not `1 items`, for a single key.
- **Phase 9:** the refine fixture places the cellar under the parlour rather than the kitchen — the Phase 8 creation
  grid puts the cellar at the kitchen's `z - 1`, exactly where the spec's "add a wine cellar under the kitchen" test
  needs a free coordinate, so the creation fixture is left untouched and the refine fixture moves the cellar. The
  turn's extra check reports only the problems an edit introduces (the difference from the grid before the edit), not
  a pre-existing one such as a room already off-map, so an edit is not blamed for the state it inherited. A room's
  prose in a refine uses a fixed theme and no style notes, because a refine has no creation brief to borrow them from.
- **Review of Phase 9:** these choices were made while fixing review findings.
  - *(Ratified.)* The turn's extra check refuses an edit naming a room that is not a plain declaration, but not an
    edit that touches an opaque field of a plain room: that one is left to the writer, which refuses the op and
    notes it while the rest of the turn stands. (Refusing it in the extra check would fail the whole turn once
    repairs run out.) The prompt marks the fields computed by code so the model avoids them.
  - Exit ops are diffed against the exits the files have now, per passage an edit touches, and `RemoveItem` matches
    an exit by direction alone, so a target written as a full label still matches.
  - *(Ratified.)* The generated test leaves out rooms with code and exits into them (§7.5); the rule in the
    milestone counted an exit blocked by a mob's `before_exit` hook as testable, which failed on the template
    world's tutorial hall.
  - The caller's coordinates keep a room added by an earlier, unwritten turn; only the rooms a turn removes are
    dropped.
  - The `add_todo` edit's class is `AddTodoEdit` (its `edit` value is still `add_todo`), so it does not clash with
    the `AddTodo` op it becomes.
- **Review of Phases 6–8:** these choices were made while fixing review findings, each recorded here against the
  phase it belongs to.
  - Phase 6: `place_existing` places a room only when every placed neighbour's exit agrees on its coordinate, and
    repeats until nothing changes; a room the exits place in two spots stays off-map rather than taking the first.
    This was confirmed with the maintainer. Map letters are assigned per level. The map's block is seven columns,
    not six: up, down and portal each need a marker column, and an east–west link needs one the markers never
    take. `validate` also reports an entry room that is not in the plan. Direction tables are public in
    `grid/derive.py` (`DIRECTION_DELTAS`, `OPPOSITES`) so the modes and the test generator share them.
  - Phase 7: `finalize`'s `builder_test` callback may be `None` for a mode with no generated test.
    `stage_and_check` also runs `gups test` when a generated test was staged, because a new zone is not yet in the
    `describe-world` document. `allowed_values` is empty for an unknown declaration or parameter. Refused ops become
    notes in `finalize`.
  - Phase 8: the prose fingerprint is the room's concept, size, coordinates and neighbour lines; key prose is kept
    across repairs while the key's concept is unchanged. `Proposal.link_package` names the package `link_ops`
    edits, since a `StarOp` carries no package; this was confirmed with the maintainer. The link's return exit
    defaults to the opposite of a standard exit, and a non-standard exit with no return exit is a plan problem. A
    link's external room must be a room of another package in the world.
- **Phase 10:** `PendingChange` carries `link_package`, the package the `link_ops` edit, because a `StarOp` holds no
  package and `/write` needs to know where to apply them; the milestone's `PendingChange` named only the ops, the
  new files, the link ops, the base hashes, the summary and the notes. The post-proposal map is drawn from the
  package as it stands after the ops are applied to a temporary copy, since a new zone's rooms are not yet in the
  `describe-world` document and the world's own room list would be empty. The session's `builder` history entries are
  sent to the model as `assistant` messages, since the model's `Message` role has no `builder`. `PendingChange` also
  carries `grid`, the coordinates after the change, and the state carries `undo_grid`: `area_turn` updates the
  coordinates it is given, so the loop hands it a copy and commits the result only on `/write`, and `/undo` puts the
  previous grid back. `undo` and `written_hashes` are keyed by world-relative path so the linked package's return exit
  is undone with the rest. A `builder_test.star` the session has no hash for is overwritten only when it is exactly the
  builder's text for the files before the change, otherwise `.new` is written; and an `area` session that declines to
  resume takes the next `-2` suffix and says so. Both were confirmed with the maintainer. A file that does not parse
  is treated as a package that does not load, because `read_package` cannot build a view of it.
- **Phase 11 (review):** these choices were made while fixing review findings.
  - *(Ratified.)* The balance references are the ten mobs of other packages closest in level to the median level
    of the package's own mobs, or of the whole world when the package has none; the milestone says "closest in
    level" without saying to what, and nothing is planned yet when the references are built.
  - *(Ratified.)* Mob mode needs only an existing package, not one with rooms: the milestone's generated test
    covers a package whose mobs have no room of their own. The CLI checks for `package.star`; the loop checks
    `describe-world`.
  - A new mob or item id must differ from every declaration of the package, whatever its kind: the engine
    refuses `item("goblin")` beside `mob("goblin")` as already declared.
  - `set_mob` values for the numeric fields get the plan's world checks; other fields (`name`, `desc`, hooks)
    are left to lint.
  - A turn's new prose uses the user's message as its style notes (an `add_mob`'s new items use the mob's
    concept), where area mode's refinement uses a fixed theme: the message is the only style guidance a turn has.
- **Phase 12:** these choices were made to fill gaps the milestone leaves open.
  - A plugin effect renders as a call on the plugin's dotted builtin name — `combust.ember(radius = 2)` — the
    form the engine binds the primitive in. The writer's renderer refused a dotted callee (a `$call` name that is
    not a single identifier), so it now accepts a name whose dot-separated segments are each identifiers. This
    reaches into `star/render.py`, outside the phase's working set, because the milestone's ops step writes every
    effect — plugin included — as a nested `$call` and names no other form; a `$const` would not do, since its
    constant grammar requires an uppercase base.
  - A `teach` edit in a turn need not name a skill the package declares: only the mob and the proficiency are
    checked. The teach adds a label to the mob's `skills` dict, and the staged lint decides whether that label
    resolves, so a same-turn `add_skill` and a skill another package provides both stand. `remove_skill`,
    `set_skill`, `set_effect` and `rewrite` still name a declared skill.
  - The missing-package refusal carves out only a new `skills/` path: skill mode on a package that does not exist
    and is not under `skills/` is refused, as mob mode is, rather than creating a `kind = "skills"` file under a
    zone path.
  - A `$dict` value is built by appending its pairs to a `list[Value]` (or as an inline literal), not as a
    `list[list[...]]` variable: strict mypy accepts the recursive `JsonValue` alias when inferring a literal in
    context but rejects a variable of a nested-list type checked against it, so the pair lists are typed `Value`.
- **Phase 13:** the end-to-end test's chilling touch deals `pierce` damage, because the template world's schema
  has no cold damage type. The test works on a world copy of its own rather than the shared session-scoped one, because it
  rewrites `zones/mansion`, which the REPL engine test (test_repl.py) also creates there expecting the package
  absent, and the test files run in alphabetical order.
- **Code-health refactor after Phase 13:** each of the area, mob and skill modes is split in two, which departs
  from the one-module-per-mode layout of the milestone. `modes/area_checks.py`, `modes/mob_checks.py` and
  `modes/skill_checks.py` hold the mode's plan and turn models and every check run on them; `modes/area.py`,
  `modes/mob.py` and `modes/skill.py` keep the brief, the prompts, the prose calls and the ops. `modes/area.py`
  re-exports the models its callers import from it (`AreaBrief`, `AreaPlan`, `AreaTurn`, `plan_to_grid`). The
  REPL's `/map` grid is now the area checks' own `grid_from_coords`, which built the same grid.
- **Not built:** a standalone item mode (items exist only as loot and keys of the modes that place them), a
  hook-writing mode (the builder writes `# TODO(gups-builder):` comments where hooks belong, never functions),
  and a re-import of `describe-world` fields that are not in format 1 (a world that writes another format is
  refused, §3).
