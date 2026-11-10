# Milestone 1: gups-builder, an LLM-assisted content builder for GUPS worlds

## Overview & Agreed Design

- **Source `DESIGN_NEEDED`:** None. This came from a maintainer design session. There is no `DESIGN_NEEDED` entry in
  either repository to mark completed.

- **Prerequisite:** `gups-engine` Milestone 49 (`gups-engine/docs/MILESTONE_49.md`) is implemented: `gups
  describe-world` exists and writes `"format": 1`. Read that milestone's "The JSON document (format 1)" section; it is
  the contract this tool consumes. Do not modify `gups-engine` in any phase of this milestone.

- **Rules for every phase** (Phase 1 writes them into `AGENTS.md`):

  - Test first: write the tests, run them and watch them fail, then write the code.
  - Tests never call a real model. Use `FakeLlm` (Phase 3). Tests that need a real `gups` binary are marked
    `@pytest.mark.engine` and skip themselves when it can't be found.
  - Every phase ends with `uv run ruff format --check`, `uv run ruff check`, `uv run mypy` and `uv run pytest` all
    passing.
  - `docs/SPEC.md` describes what the code does. Each phase updates the sections it names, in the same change.
  - Naming: everything a character casts is a skill. The mode is `skill`, identifiers say `skill`, and "spell" is only
    one value of a skill's `kind`.
  - Never read or write anything in `gups-engine/` except as listed under a phase's "Read / Reference Only", and never
    write into `gups-engine/template-world/`: tests copy it to a temporary directory first.

- **Design Summary:**

  - **What it is.** A separate Python repository, `gups-project/gups-builder/`, providing the `gups-builder` command: an
    interactive terminal session that helps a builder create GUPS content with two local LLMs. A **logic** model plans
    structure (room graphs, numbers, effects, edits). A **prose** model writes names, descriptions and messages.
  - **Modes.** One session works in one mode at a time: `area` (rooms, exits, doors and their keys), `mob` (mobs and
    their loot) or `skill` (skills of any `kind`). A request that belongs to another mode is not attempted; it is noted.
  - **Fixed pipeline.** Python decides every step. Each model call is a single request whose reply must match a
    pydantic schema; an invalid reply is sent back for repair up to `max_repairs` times. No tool-calling loop.
  - **The `.star` files are the source of truth.** Every turn re-reads the target package with `libcst` into an
    editable view. Values that aren't plain literals (hook references, helper calls, loops) are *opaque*: shown to the
    model, never changed. Changes are applied as minimal, formatting-preserving edits, so the user can edit the files by
    hand at any time and keep talking to the builder.
  - **The engine is the authority on the loaded world.** `gups describe-world` gives the schema, features, renames,
    every declaration's summary, plugins and load errors. `gups docs --json` gives the API model (declaration
    parameters and allowed values, effect primitives, behaviors). `gups lint` and `gups test` validate.
  - **Declarations only.** The builder writes plain declaration arguments. It never writes Starlark functions; where a
    request needs logic, it inserts a `# TODO(gups-builder): ...` comment describing the function to write.
  - **Area grid.** Area rooms live on an integer grid `(x, y, z)`; `+y` is north, `+x` east, `+z` up. The model
    gives coordinates and which neighbours connect; Python derives every direction, both sides of every exit, and
    doors on both sides. The 8 horizontal neighbours and straight up/down are adjacent. Anything else is an explicit
    `portal`.
  - **Review before write.** Every request produces a *proposal*. Before the user sees it, Python applies it to a
    temporary copy of the world and runs `gups lint` and `gups test` on the package, repairing failures with the logic
    model. The user sees a summary, the map and the diff, and nothing touches their files until `/write`.
  - **Generated test.** In a zone package the builder owns one file, `builder_test.star`, regenerated on every write:
    it checks the package loads, that each room's open exits lead where they should, and each mob's vitals. A hand
    edit to that file is never overwritten (it is written as `builder_test.star.new` instead).

- **Specification Impact:** This milestone creates `gups-builder/docs/SPEC.md`. Phase 1 writes its outline; each later
  phase fills the sections it names.

- **Decision Record Updates:** None. (`gups-builder` has no decision log. If you must make a choice this milestone
  doesn't settle, record it in `docs/SPEC.md` under "§10 Open choices" with the phase that made it.)

### Repository layout (final state)

```
gups-builder/
  pyproject.toml          uv project; console script gups-builder = gups_builder.cli:main
  uv.lock
  README.md               install, configure models, quick start
  AGENTS.md               the rules above
  .gitignore              .venv/, __pycache__/, .pytest_cache/, .mypy_cache/, dist/
  docs/
    SPEC.md
    MILESTONE_1.md        this file
  src/gups_builder/
    __init__.py           __version__
    cli.py                argument parsing, subcommands
    config.py             roles, precedence, TOML
    engine/
      gups.py             finding and running the gups binary
      world.py            pydantic models of describe-world format 1
      api.py              pydantic models of the api.json subset used
      lint.py             gups lint --format github parser
      staging.py          temporary world copies for validation
    llm/
      client.py           LlmClient protocol, HttpLlm
      fake.py             FakeLlm
      structured.py       structured calls with repair
      log.py              JSONL call log
    star/
      values.py           Starlark literal <-> Value
      reader.py           package -> PackageView
      ops.py              StarOp types
      writer.py           applying StarOps with libcst
      render.py           formatting new declarations and files
      diff.py             unified diffs
    grid/
      model.py            Grid, Connection, Portal
      derive.py           coordinates -> exits; validation
      place.py            existing rooms -> coordinates
      ascii.py            the /map rendering
    modes/
      common.py           Proposal, validation against the API and schema
      area.py             area pipeline and edits
      mob.py              mob pipeline and edits
      skill.py            skill pipeline and edits
      testgen.py          builder_test.star
    prompting.py          templates, context budgeting
    prompts/*.md          prompt templates
    session.py            session state and persistence
    repl.py               the interactive loop
  tests/
    conftest.py           fixtures: template world copy, gups binary, FakeLlm
    fixtures/             small .star files, describe-world and api.json samples, scripted LLM replies
    test_*.py
```

### Shared types (referenced by several phases)

These are the cross-phase interfaces. Phases implement them exactly; field names are part of the contract.

**Value: the JSON form of a Starlark expression** (`star/values.py`). A `Value` is JSON-serializable:

|          Starlark          |                               Value                                |
|----------------------------|--------------------------------------------------------------------|
| `None`, `True`, `3`, `"x"` | `None`, `True`, `3`, `"x"` (floats too)                            |
| `[a, b]`                   | `[A, B]`                                                           |
| `{"k": v}`                 | `{"$dict": [["k", V]]}` (keys are Values; order kept)              |
| `DIR.north`, `SIZE.large`  | `{"$const": "DIR.north"}` (any `NAME.attr` on an uppercase name)   |
| `exit(DIR.north, to = ":x")` | `{"$call": "exit", "args": [...], "kwargs": {"to": ":x"}}` when every argument is itself a non-opaque Value and the callee is a bare identifier |
| `_hall_enter`              | `{"$ref": "_hall_enter"}` (a bare identifier other than the above) |
| anything else              | `{"$opaque": "<exact source text>"}`                              |

In Python, `Value = pydantic.JsonValue` (pydantic 2.5 or later; pin `pydantic>=2.5`); the `$` forms are plain dicts
with those keys. A Value is *editable* when it contains no `$ref` and no `$opaque` anywhere inside it.

**PackageView** (`star/reader.py`):

```python
@dataclass(frozen=True)
class DeclView:
    kind: str                 # "room" | "mob" | "item" | "skill" | other declaration names
    id: str                   # the first positional string argument
    file: str                 # file name within the package, e.g. "rooms.star"
    line: int                 # 1-based line of the call
    args: dict[str, Value]    # keyword arguments in source order
    extra_positional: list[Value]   # positional arguments after the id

@dataclass(frozen=True)
class OpaqueDecl:
    kind: str
    id: str | None            # None when the id isn't a string literal
    file: str
    line: int
    reason: str               # "not at top level" | "id is not a string literal" | ...

@dataclass(frozen=True)
class PackageView:
    path: str                         # "zones/mansion"
    files: dict[str, str]             # file name -> sha256 of its bytes (non-test, non-testdata .star files)
    decls: list[DeclView]             # top-level declaration calls, in file then line order
    opaque: list[OpaqueDecl]
    def get(self, kind: str, id: str) -> DeclView | None: ...
```

**StarOp: one change to a package** (`star/ops.py`), a pydantic discriminated union on `op`:

```python
class AddDecl(BaseModel):    op: Literal["add_decl"]; file: str; kind: str; id: str; args: dict[str, Value]; comment: str | None = None
class RemoveDecl(BaseModel): op: Literal["remove_decl"]; kind: str; id: str
class SetArg(BaseModel):     op: Literal["set_arg"]; kind: str; id: str; name: str; value: Value
class RemoveArg(BaseModel):  op: Literal["remove_arg"]; kind: str; id: str; name: str
class AppendItem(BaseModel): op: Literal["append_item"]; kind: str; id: str; name: str; value: Value
class RemoveItem(BaseModel): op: Literal["remove_item"]; kind: str; id: str; name: str; match: ItemMatch
class AddTodo(BaseModel):    op: Literal["add_todo"]; kind: str; id: str; text: str
StarOp = Annotated[AddDecl | RemoveDecl | SetArg | RemoveArg | AppendItem | RemoveItem | AddTodo, Field(discriminator="op")]

class ItemMatch(BaseModel):
    call: str | None = None            # e.g. "exit", "spawn", "loot"
    first_arg: Value | None = None     # e.g. {"$const": "DIR.north"}; matches args[0] of a $call item
    kwarg: tuple[str, Value] | None = None   # e.g. ("to", ":hall")
    # an item matches when every given criterion matches; exactly one item must match
```

**Proposal** (`modes/common.py`):

```python
@dataclass
class Proposal:
    ops: list[StarOp]
    new_files: dict[str, str]          # file name -> full content, for files that don't exist yet
    summary: str                        # e.g. "+6 rooms, +1 item, 2 exits changed"
    prose_preview: list[tuple[str, str, str]]   # (label, field, text) for every new or changed text field
    notes: list[str]                    # out-of-scope requests and TODOs, shown to the user
    grid: dict[str, tuple[int, int, int]] | None = None   # area mode: room id -> coordinate after the change
    link_ops: list[StarOp] = field(default_factory=list)  # edits to another package; applied only after the user confirms
    link_package: str | None = None    # the package link_ops edit (added in the Phase 6–8 review)
```

---

## Phase 1: Repository scaffold, configuration and the CLI skeleton

### Objective & Scope

- **In Scope:**
  - `git init` of `gups-project/gups-builder/` (this file already exists in `docs/`; keep it).
  - `pyproject.toml`, `uv.lock`, `README.md`, `AGENTS.md`, `.gitignore`, `docs/SPEC.md` outline.
  - `config.py`: model roles and settings precedence.
  - `cli.py`: the subcommand skeleton and `gups-builder models`, which in this phase prints the effective
    configuration only.
- **Out of Scope:** *The agent must not attempt these in this phase.*
  - Any HTTP request, any engine call, any `libcst` code.
  - The `area`, `mob`, `skill`, `resume` and `sessions` subcommands beyond parsing their arguments and printing
    `not implemented yet` with exit code 2.

### Context Working Set

- **Read / Reference Only:** this milestone; `gups-engine/AGENTS.md` for tone and structure of `AGENTS.md`.
- **Files to Modify / Create:** `pyproject.toml`, `README.md`, `AGENTS.md`, `.gitignore`, `docs/SPEC.md`,
  `src/gups_builder/__init__.py`, `src/gups_builder/config.py`, `src/gups_builder/cli.py`, `tests/test_config.py`,
  `tests/test_cli.py`.
- **Do Not Touch:** `gups-engine/`.

### Specification & Decision Record Changes

Create `docs/SPEC.md` with these headings, each with one sentence saying what it will cover, and fill §1 and §2 now:

```
# gups-builder Specification
## 1. Purpose and scope
## 2. Installation and configuration
## 3. Engine contract
## 4. Model access
## 5. Editing Starlark
## 6. The area grid
## 7. Modes
### 7.1 The pipeline every mode shares
### 7.2 area
### 7.3 mob
### 7.4 skill
### 7.5 The generated test
## 8. Sessions and the interactive loop
## 9. Testing
## 10. Open choices
```

§1 states the Design Summary of this milestone in the present tense. §2 states the configuration rules below.

### Implementation Blueprint

- **`pyproject.toml`:** Python `>=3.12`; build backend `hatchling`; `src/` layout; dependencies `httpx`, `pydantic>=2.5`,
  `libcst`, `rich`, `prompt_toolkit`, `tomli-w`; dev group `pytest`, `ruff`, `mypy`. `[tool.mypy] strict = true`,
  `files = ["src", "tests"]`. `[tool.ruff] line-length = 100`, lint rules `E,F,I,UP,B,SIM`. `[tool.pytest.ini_options]`
  registers the marker `engine: needs a real gups binary`.

- **Signatures & Types (`config.py`):**

```python
StructuredMode = Literal["json_schema", "json_object", "none"]

class RoleConfig(BaseModel):
    base_url: str = "http://127.0.0.1:11434/v1"
    model: str = ""
    api_key: str | None = None
    temperature: float
    max_tokens: int = 4096
    context_tokens: int = 16384
    structured: StructuredMode = "json_schema"
    timeout_s: float = 300.0

class Config(BaseModel):
    logic: RoleConfig = RoleConfig(temperature=0.2)
    prose: RoleConfig = RoleConfig(temperature=0.8)
    max_repairs: int = 3
    gups: str | None = None            # path to the gups binary

def load_config(world: Path, cli: dict[str, str], env: Mapping[str, str], home: Path) -> Config: ...
```

- **Algorithmic Steps & Invariants:**
  1. Precedence, highest first: CLI flags, environment, `<world>/.builder/config.toml`, `<home>/.config/gups-builder/config.toml`,
     defaults. Merge per field, not per file: a world file that sets only `logic.model` keeps the user file's
     `logic.base_url`.
  2. TOML layout: top-level `max_repairs`, `gups`; tables `[logic]` and `[prose]` with the `RoleConfig` field names.
  3. Environment variables: `GUPS_BUILDER_<ROLE>_<FIELD>` uppercased (`GUPS_BUILDER_LOGIC_URL` maps to `base_url`,
     `GUPS_BUILDER_PROSE_MODEL` to `model`, `..._API_KEY`, `..._TEMPERATURE`, `..._STRUCTURED`, `..._CONTEXT_TOKENS`),
     plus `GUPS_BIN` for `gups` and `GUPS_BUILDER_MAX_REPAIRS`.
  4. CLI flags (global, before the subcommand): `--world DIR` (default `.`), `--gups PATH`, `--logic-url`,
     `--logic-model`, `--prose-url`, `--prose-model`.
  5. A role whose `model` is empty after merging is an error only when that role is used (Phase 3); `models` shows it
     as `(not set)`.
  6. `cli.main(argv: list[str] | None = None) -> int` uses `argparse` with subcommands `area`, `mob`, `skill` (each
     `[package] [--name SESSION]`), `resume SESSION`, `sessions`, `models`. `models` prints both roles (never the API
     key's value: print `set` or `not set`) and the `gups` path setting.

### Test & Documentation Requirements

- `tests/test_config.py`: defaults; each precedence level overriding the one below; per-field merge across files; env
  var mapping including `GUPS_BIN`; a bad `structured` value raises a `ValueError` naming the source.
- `tests/test_cli.py`: `models` prints both roles and hides the API key; unimplemented subcommands exit 2.
- `README.md`: what the tool is, the engine prerequisite, `uv tool install .`, a sample `config.toml` for Ollama.
- `AGENTS.md`: the rules of this milestone's "Rules for every phase".

### Exit Conditions

- [ ] `uv sync` succeeds; `uv run gups-builder models` prints both roles.
- [ ] `uv run ruff format --check && uv run ruff check && uv run mypy && uv run pytest` all pass.
- [ ] `docs/SPEC.md` has the outline, and §1 and §2 match the code.

---

## Phase 2: The engine bridge

### Objective & Scope

- **In Scope:** finding `gups`; running `describe-world`, `docs --json`, `lint` and `test`; pydantic models of their
  output; temporary world copies.
- **Out of Scope:** *The agent must not attempt these in this phase.* Any model call; any `.star` parsing; caching
  across processes.

### Context Working Set

- **Read / Reference Only:**
  - `gups-engine/docs/MILESTONE_49.md`, section "The JSON document (format 1)".
  - `gups-engine/docs/SPEC.md` §4.13 (CLI) and §4.16 (`api.json` shape).
  - `gups-engine/script/src/main/kotlin/gups/script/Linter.kt`, `renderGithub()`: the lint line format
    `::error file=<path>,line=<n>,title=gups lint::<message>` (`line=` optional; `%25`, `%0D`, `%0A` escapes).
  - `gups-engine/cli/src/test/resources/api.golden.json`: copy a trimmed version (the `declarations` entries `room`,
    `exit`, `door`, `spawn`, `mob`, `item`, `loot`, `skill`, `package`, all `effects`, all `behaviors`, `constants`) into
    `tests/fixtures/api_min.json`.
- **Files to Modify / Create:** `src/gups_builder/engine/{__init__,gups,world,api,lint,staging}.py`,
  `tests/conftest.py`, `tests/fixtures/describe_world_min.json`, `tests/fixtures/api_min.json`,
  `tests/test_engine.py`, `docs/SPEC.md` §3.
- **Do Not Touch:** `gups-engine/`.

### Specification & Decision Record Changes

- §3 "Engine contract": the commands used, the required `format` (1), how `gups` is found, that a `format` other than
  1 refuses to start with the message `gups describe-world format <n> is not supported; this gups-builder needs format 1`,
  and the staging rules below.

### Implementation Blueprint

- **Signatures & Types:**

```python
# engine/gups.py
class EngineError(Exception): ...
class Gups:
    def __init__(self, binary: Path, world: Path) -> None: ...
    @staticmethod
    def find(config_path: str | None, env: Mapping[str, str]) -> Path: ...   # config, then GUPS_BIN, then PATH; EngineError if none
    def describe_world(self, world: Path | None = None) -> WorldDescription: ...
    def api(self) -> ApiModel: ...                                           # `gups docs --json`, cached per instance
    def lint(self, package: str, world: Path | None = None) -> list[LintFinding]: ...
    def test(self, package: str, world: Path | None = None) -> TestRun: ...

@dataclass(frozen=True)
class TestRun:
    ok: bool
    output: str          # combined stdout and stderr

# engine/lint.py
@dataclass(frozen=True)
class LintFinding:
    severity: Literal["error", "warning"]
    file: str            # repo-relative, e.g. "zones/mansion/rooms.star"
    line: int | None
    message: str
def parse_github(output: str) -> list[LintFinding]: ...

# engine/world.py: pydantic models mirroring format 1 exactly (one class per object kind):
# WorldDescription, Schema, SchemaName, DamageTypeInfo, PackageInfo, RoomInfo, ExitInfo, DoorInfo,
# MobInfo, ItemInfo, SkillInfo, PluginSummary, LoadErrorInfo. Unknown extra keys are allowed (model_config
# extra="ignore") so newer engines that add fields still work.
# Helpers on WorldDescription:
#   def has_feature(self, f: str) -> bool
#   def rooms_in(self, package: str) -> list[RoomInfo]   (and mobs_in, items_in, skills_in)
#   def label_exists(self, label: str) -> bool

# engine/api.py: only what the builder reads
class ApiParam(BaseModel): name: str; required: bool = False; default: str | None = None; allowed_values: list[str] = []; doc: str = ""; feature: str | None = None; type: str | None = None
class ApiEntry(BaseModel): name: str; doc: str = ""; params: list[ApiParam] = []; feature: str | None = None
class ApiModel(BaseModel):
    api_version: int
    declarations: list[ApiEntry]; effects: list[ApiEntry]; behaviors: list[ApiEntry]
    def declaration(self, name: str) -> ApiEntry: ...
    def effect(self, name: str) -> ApiEntry | None: ...
    def behavior(self, name: str) -> ApiEntry | None: ...

# engine/staging.py
@contextmanager
def staged_world(world: Path) -> Iterator[Path]: ...
```

- **Algorithmic Steps & Invariants:**
  1. `Gups` runs subprocesses with a 120 s timeout and `text=True`, passing `--world <path>` to every command except
     `docs --json`, which the engine refuses with `--world` (run it as `gups docs --json`, no other flags). A non-zero exit of
     `describe-world` or `docs --json` raises `EngineError` with stderr. `lint` returns findings whatever its exit code.
     `lint` runs `gups lint --format github --world W //<package>/...`. `test` runs `gups test --world W
     //<package>/...` and returns `TestRun(ok = exit code 0)`.
  2. `describe_world` checks `format == 1` before building the model.
  3. `staged_world` copies the world into a `tempfile.TemporaryDirectory`, skipping `.git`, `.gups`, `.builder` and
     `local-world-test`, keeping `plugins/` (jar checksums must still match). The copy is deleted on exit.
  4. `tests/conftest.py` provides: `gups_bin` (a `Path`, or `pytest.skip` when `Gups.find` fails), `template_world` (a
     temp copy of `$GUPS_TEMPLATE_WORLD`, default `../gups-engine/template-world` relative to the repository root;
     skip when missing), and `world_min` (the parsed `describe_world_min.json`).
  5. `describe_world_min.json` is hand-written: a fantasy schema (`str`, `dex`, `con`, `int`, `wis`, `armor`; `hp`,
     `mana`; three damage types), all four features, one zone `zones/tutorial` with two rooms, one mob, one item and
     one skill, and one failed package with one error.

### Test & Documentation Requirements

- `tests/test_engine.py`:
  - parsing `describe_world_min.json`, including a failed package and `null` door/factions values;
  - a `format: 2` document raises `EngineError` with the message above;
  - `parse_github` on lines with and without `line=`, with escapes, and ignoring non-annotation lines;
  - `ApiModel` parses `api_min.json`; `declaration("room")` has param `size` with allowed values;
  - `staged_world` skips the listed directories and removes the copy;
  - `@pytest.mark.engine`: `describe_world` on the template world has room `//zones/tutorial:hall` with an east exit
    whose door is locked; `lint` on a staged copy with a broken file reports its file and line; `test` on
    `zones/tutorial` passes; `api()` returns a model with a `room` declaration and a `damage` effect.

### Exit Conditions

- [ ] `uv run pytest tests/test_engine.py` passes (engine tests pass when `gups` is installed, skip otherwise).
- [ ] The four checks of the rules pass.
- [ ] §3 matches the code.

---

## Phase 3: Model access

### Objective & Scope

- **In Scope:** the `LlmClient` protocol, the HTTP client for OpenAI-compatible servers, `FakeLlm`, structured calls
  with validation and repair, the JSONL call log, and `gups-builder models` sending a test request to each role.
- **Out of Scope:** *The agent must not attempt these in this phase.* Prompt templates and context budgeting (Phase
  7); streaming; tool calling.

### Context Working Set

- **Read / Reference Only:** `src/gups_builder/config.py`.
- **Files to Modify / Create:** `src/gups_builder/llm/{__init__,client,fake,structured,log}.py`, `src/gups_builder/cli.py`
  (the `models` subcommand), `tests/test_llm.py`, `docs/SPEC.md` §4.
- **Do Not Touch:** `engine/`, `gups-engine/`.

### Specification & Decision Record Changes

- §4 "Model access": the request shape, the three `structured` modes, repair, the log format, and what `models`
  checks.

### Implementation Blueprint

- **Signatures & Types:**

```python
Role = Literal["logic", "prose"]

@dataclass(frozen=True)
class Message:
    role: Literal["system", "user", "assistant"]
    content: str

class LlmClient(Protocol):
    def complete(self, role: Role, messages: list[Message], json_schema: dict[str, Any] | None) -> str: ...

class HttpLlm:
    def __init__(self, config: Config, log: CallLog | None) -> None: ...
    def complete(self, role: Role, messages: list[Message], json_schema: dict[str, Any] | None) -> str: ...

class FakeLlm:
    """Replays scripted replies. Each reply is matched by role, in order."""
    def __init__(self, replies: dict[Role, list[str]]) -> None: ...
    calls: list[tuple[Role, list[Message], dict[str, Any] | None]]
    def complete(self, role, messages, json_schema) -> str: ...   # raises AssertionError when a role runs out

class StructuredCallFailed(Exception):
    attempts: list[str]; errors: list[str]

def call_structured(llm: LlmClient, role: Role, messages: list[Message], model: type[T],
                    max_repairs: int, extra_check: Callable[[T], list[str]] | None = None) -> T: ...

class CallLog:
    def __init__(self, path: Path) -> None: ...
    def record(self, role: Role, model: str, messages: list[Message], reply: str, seconds: float, error: str | None) -> None: ...
```

- **Algorithmic Steps & Invariants:**
  1. `HttpLlm.complete` POSTs `<base_url>/chat/completions` with `model`, `messages`, `temperature`, `max_tokens`, and
     `Authorization: Bearer <api_key>` when set. Structured modes:
     - `json_schema`: `response_format = {"type": "json_schema", "json_schema": {"name": "reply", "schema": <schema>, "strict": true}}`;
     - `json_object`: `response_format = {"type": "json_object"}`, and the schema is appended to the last user message
       as `Reply with JSON matching this schema:\n<schema>`;
     - `none`: no `response_format`; the schema is appended as for `json_object`.
     It returns `choices[0].message.content`. An HTTP error or timeout raises `LlmError` with role, URL and status.
     An empty `model` raises `LlmError("the <role> model is not configured")`. When a `json_schema` request gets HTTP
     400, retry that one call once as `json_object` and record `error: "json_schema rejected; used json_object"` in the
     log; a 400 again raises `LlmError`.
  2. `call_structured`: the schema is `model.model_json_schema()`. Strip a leading/trailing Markdown code fence from the
     reply, then `model.model_validate_json`. On a validation error, or when `extra_check` returns messages, append the
     assistant's reply and a user message `That reply was not valid:\n- <error>\n...\nReply again with the corrected
     JSON only.` and retry, up to `max_repairs` retries. Then raise `StructuredCallFailed`.
  3. `CallLog.record` appends one JSON object per line: `ts` (ISO 8601), `role`, `model`, `messages`, `reply`,
     `seconds`, `error`. The REPL (Phase 10) puts it at `<world>/.builder/logs/<session>.jsonl`.
  4. `gups-builder models` additionally sends each role `[system: "Reply with the single word OK."]` with no schema
     and prints `logic: OK (1.2 s)` or `logic: FAILED: <error>`. Then, for each role whose `structured` is
     `json_schema`, it sends one schema-constrained request (schema: an object with a required string `word`; prompt
     `Reply with the word OK.`) and prints `logic: json_schema OK` or `logic: json_schema not honoured (<reason>); set
     structured = "json_object"` when the request was refused or the reply didn't validate. It exits 1 if a plain
     request fails.

### Test & Documentation Requirements

- `tests/test_llm.py` (use `httpx.MockTransport`; inject the transport through an optional `HttpLlm` constructor
  argument `transport: httpx.BaseTransport | None = None`):
  - request body for each `structured` mode; bearer header only with an API key;
  - `call_structured` succeeds first time; repairs once after invalid JSON; repairs after `extra_check` complaints;
    raises `StructuredCallFailed` after `max_repairs`; strips code fences;
  - `FakeLlm` records calls and fails when out of replies;
  - `CallLog` writes one valid JSON line per call;
  - a 400 on a `json_schema` request is retried once as `json_object` and logged;
  - `models` with a mock transport prints OK for both roles and reports a refused `json_schema` check.

### Exit Conditions

- [ ] `uv run pytest tests/test_llm.py` passes.
- [ ] The four checks pass; §4 matches the code.

---

## Phase 4: Reading Starlark

### Objective & Scope

- **In Scope:** `star/values.py` (expression ↔ `Value`) and `star/reader.py` (`PackageView`), with `libcst`.
- **Out of Scope:** *The agent must not attempt these in this phase.* Any writing or editing (Phase 5); any semantic
  checks beyond what is listed.

### Context Working Set

- **Read / Reference Only:** "Shared types" in this milestone; `gups-engine/template-world/zones/tutorial/rooms.star`,
  `mobs.star`, `items.star` and `gups-engine/template-world/skills/arcane/spells.star` (copy them to
  `tests/fixtures/tutorial/` and `tests/fixtures/arcane/`; tests read the copies).
- **Files to Modify / Create:** `src/gups_builder/star/{__init__,values,reader}.py`, `tests/fixtures/...`,
  `tests/test_star_reader.py`, `docs/SPEC.md` §5 (reading half).
- **Do Not Touch:** `engine/`, `llm/`.

### Specification & Decision Record Changes

- §5 "Editing Starlark": the Value table, what counts as a declaration, what is opaque and why.

### Implementation Blueprint

- **Signatures & Types:**

```python
def to_value(expr: cst.BaseExpression, module: cst.Module) -> Value: ...
def is_editable(v: Value) -> bool: ...
def read_package(pkg_dir: Path, package_path: str) -> PackageView: ...
DECLARATION_KINDS = frozenset({"room", "mob", "item", "skill", "faction", "region", "container"})
```

- **Algorithmic Steps & Invariants:**
  1. Files read: every `*.star` in `pkg_dir` (not recursive) except `package.star`, `*_test.star` and
     `*_testdata.star`, in alphabetical order. Hash each file's bytes with sha256.
  2. Parse with `cst.parse_module` (Starlark is a syntactic subset of Python). A file that fails to parse raises
     `StarParseError(file, line, message)`.
  3. A *declaration* is a top-level `Expr` statement whose value is a `Call` to a bare name in `DECLARATION_KINDS`.
     If its first positional argument is a simple string literal it becomes a `DeclView`; otherwise an `OpaqueDecl`
     with reason `id is not a string literal`. A call to one of those names anywhere else (inside `def`, `for`, `if`,
     or assigned to a name) is an `OpaqueDecl` with reason `not at top level` (its id when it is a literal).
  4. `to_value` follows the Value table. String literals: decode Python-style escapes (`ast.literal_eval` on the
     literal's source is acceptable); concatenated strings (`"a" + "b"`) are `$opaque`. Negative numbers (`-10`) are
     ints. `$opaque` keeps the exact source text (`module.code_for_node(expr)`).
  5. Line numbers come from `cst.metadata.PositionProvider`.

### Test & Documentation Requirements

- `tests/test_star_reader.py`:
  - every Value form, one test each, including nested `$call` inside a list and a `$call` whose argument is a `$ref`
    (the outer call then holds a `$ref` and is not editable);
  - the tutorial rooms: `hall` has `exits` as a list of four `$call` exits, `on_enter` as `$ref`, `size` as
    `{"$const": "SIZE.large"}`;
  - `spells.star`: `spark`'s `effect` is an editable `$call`; `pacify`'s `resolve` is a `$ref`;
  - a declaration inside a `for` loop is an `OpaqueDecl`; `_test.star` files are skipped; hashes change when a file
    changes.

### Exit Conditions

- [ ] `uv run pytest tests/test_star_reader.py` passes.
- [ ] The four checks pass; §5 (reading) matches the code.

---

## Phase 5: Writing Starlark

### Objective & Scope

- **In Scope:** applying `StarOp`s to file contents with formatting preserved, rendering new declarations and new
  files, and unified diffs.
- **Out of Scope:** *The agent must not attempt these in this phase.* Writing to disk (the session does that in Phase
  9); choosing ops (modes do that).

### Context Working Set

- **Read / Reference Only:** "Shared types"; `src/gups_builder/star/{values,reader}.py`; the fixture copies from Phase
  4 (their style is the style new code must match).
- **Files to Modify / Create:** `src/gups_builder/star/{ops,writer,render,diff}.py`, `tests/test_star_writer.py`,
  `docs/SPEC.md` §5 (writing half).
- **Do Not Touch:** `engine/`, `llm/`, `star/reader.py` except to fix a bug a writer test exposes.

### Specification & Decision Record Changes

- §5: each op's meaning, when it is refused, and the formatting rules for new code.

### Implementation Blueprint

- **Signatures & Types:**

```python
class OpRefused(Exception):
    def __init__(self, op: StarOp, reason: str, file: str | None, line: int | None) -> None: ...

@dataclass
class ApplyResult:
    files: dict[str, str]          # file name -> new content (only changed or new files)
    refused: list[OpRefused]

def apply_ops(current: dict[str, str], view: PackageView, ops: list[StarOp]) -> ApplyResult: ...
def render_value(v: Value, indent: int) -> str: ...
def render_decl(kind: str, id: str, args: dict[str, Value], comment: str | None) -> str: ...
def unified_diff(before: dict[str, str], after: dict[str, str]) -> str: ...
```

- **Algorithmic Steps & Invariants:**
  1. Ops apply in order; each sees the result of the ones before. An op that is refused is collected in `refused`,
     and the remaining ops still apply. Find declarations again (by kind and id) after each op; never keep CST nodes
     across ops.
  2. Refusals (the `reason` text is shown to the user, so write it for them):
     - the declaration doesn't exist, or is an `OpaqueDecl`: `<kind> '<id>' is not a plain declaration in this package`;
     - `AddDecl` with an id that exists: `<kind> '<id>' already exists`;
     - `SetArg`/`RemoveArg`/`AppendItem`/`RemoveItem` on an argument whose current Value is not editable:
       `<kind> '<id>': <name> is computed by code (<file>:<line>); change it by hand`;
     - `RemoveItem` matching zero or more than one item;
     - any op whose new Value is not editable (models may only write plain Values).
  3. `SetArg` replaces the argument's value expression in place, or appends a new keyword argument after the last one
     when absent. `AppendItem` adds to the end of a list argument, creating `name = [<item>]` when absent. Both copy
     the whitespace of the neighbouring element/argument so a multi-line list stays one item per line and a one-line
     list stays one line.
  4. `AddDecl` appends `\n` + `render_decl(...)` to `file` (creating it when absent; the content of a created file
     starts with a one-line comment `# <Kind>s of <package path>, started by gups-builder.`). `RemoveDecl` removes the
     statement and its leading comment lines. `AddTodo` inserts `# TODO(gups-builder): <text>` on its own line
     directly above the declaration, after any existing leading comments.
  5. Rendering style, matching the template: `kind("id",` then one `name = value,` per line indented 4 spaces, then
     `)`. Lists of more than one `$call`, or longer than 80 characters, go one item per line with a trailing comma.
     `$dict` renders as `{"k": v}`. Strings use double quotes; escape only `\` (as `\\`), `"` (as `\"`) and a newline (as `\n`). Write every
     other character, including non-ASCII ones such as `—` and `’`, literally as UTF-8: never `\uXXXX` (so do not use
     `json.dumps`). Files are read and written as UTF-8.
     `$const` renders as its text; `$call` as `callee(arg, k = v)`.
  6. Invariant: applying an empty op list returns no changed files, and an unchanged file is byte-identical.

### Test & Documentation Requirements

- `tests/test_star_writer.py`:
  - `SetArg` on `hall.desc` changes only that line; comments elsewhere in the file are unchanged (compare the rest of
    the file byte for byte);
  - `AppendItem` of an exit to the multi-line `hall.exits` keeps one item per line; to a one-line list keeps one line;
  - `RemoveItem` by `first_arg {"$const":"DIR.west"}`; zero and two matches are refused;
  - `SetArg` on `on_enter` (a `$ref`) is refused with the file and line;
  - `AddDecl` into a new file and into an existing one; `RemoveDecl` takes its comment with it; `AddTodo` placement;
  - round trip: `read_package` of the result shows the new values;
  - `render_decl` output for a room with exits, a mob with `vitals(...)` and behaviors, a skill with a nested
    `sequence` effect, each compared with an expected string;
  - a `desc` containing `—` and `’` renders literally (no `\u` escapes) and reads back equal;
  - `unified_diff` lists new files as created.

### Exit Conditions

- [ ] `uv run pytest tests/test_star_writer.py tests/test_star_reader.py` passes.
- [ ] The four checks pass; §5 matches the code.

---

## Phase 6: The area grid

### Objective & Scope

- **In Scope:** the grid model, deriving exits from coordinates, validation, placing existing rooms on the grid, and
  the ASCII map.
- **Out of Scope:** *The agent must not attempt these in this phase.* Any model call; converting the grid into
  `StarOp`s (Phase 8).

### Context Working Set

- **Read / Reference Only:** `src/gups_builder/engine/world.py` (`RoomInfo`, `ExitInfo`); the map comment at the top of
  `tests/fixtures/tutorial/rooms.star` as an example of the intended look.
- **Files to Modify / Create:** `src/gups_builder/grid/{__init__,model,derive,place,ascii}.py`, `tests/test_grid.py`,
  `docs/SPEC.md` §6.
- **Do Not Touch:** `star/`, `llm/`, `engine/`.

### Specification & Decision Record Changes

- §6 "The area grid": axes, adjacency, directions, portals, every validation rule and its message, placement of
  existing rooms, and the map format.

### Implementation Blueprint

- **Signatures & Types:**

```python
Coord = tuple[int, int, int]

class DoorSpec(BaseModel):
    state: Literal["open", "closed", "locked"] = "closed"
    key: str | None = None            # an item id in the target package, or a full label
    tags: list[str] = []

class GridRoom(BaseModel):  id: str; coord: Coord | None; external: bool = False   # external: a label outside the package
class Connection(BaseModel): a: str; b: str; door: DoorSpec | None = None
class Portal(BaseModel): a: str; a_exit: str; b: str; b_exit: str | None = None; door: DoorSpec | None = None

class Grid(BaseModel):
    rooms: list[GridRoom]; connections: list[Connection]; portals: list[Portal]

@dataclass(frozen=True)
class DerivedExit:
    room: str; direction: str; to: str; door: DoorSpec | None

def direction_between(a: Coord, b: Coord) -> str | None: ...
def derive_exits(grid: Grid) -> list[DerivedExit]: ...
def validate(grid: Grid, entry: str, room_range: tuple[int, int] | None, known_items: set[str]) -> list[str]: ...
def place_existing(rooms: list[RoomInfo], package: str, known: dict[str, Coord]) -> tuple[dict[str, Coord], list[str]]: ...
def render_map(grid: Grid, z: int, names: dict[str, str]) -> str: ...
```

- **Algorithmic Steps & Invariants:**
  1. `direction_between`: with `d = b - a`: `(0,1,0)` north, `(0,-1,0)` south, `(1,0,0)` east, `(-1,0,0)` west,
     `(1,1,0)` northeast, `(-1,1,0)` northwest, `(1,-1,0)` southeast, `(-1,-1,0)` southwest, `(0,0,1)` up, `(0,0,-1)`
     down; anything else `None`.
  2. `derive_exits`: each `Connection` yields two exits, `a→b` in `direction_between(a, b)` and `b→a` in the opposite,
     both carrying the same `door`. Each `Portal` yields `a→b` named `a_exit` and `b→a` named `b_exit`, where a missing
     `b_exit` is the opposite of `a_exit` if it is a standard direction (else a validation error). Exits come out in
     the order of `connections` then `portals`.
  3. `validate` returns every problem, each a full sentence naming the rooms, for example:
     - two rooms on one coordinate;
     - a connection between rooms that are not adjacent (`'hall' (0,0,0) and 'attic' (0,2,1) are not neighbours; use a portal`);
     - a room using the same exit name twice (including portals);
     - a connection or portal naming an unknown room;
     - a room not reachable from `entry` through exits (doors count as passable);
     - a room count of non-external rooms outside `room_range`;
     - a `locked` door without a `key`, or a key not in `known_items`;
     - a non-external room without a coordinate that isn't connected only by portals.
  4. `place_existing`: rooms of `package` from `describe-world`. Start from `known` (coordinates saved in the session).
     If none are known, put the first room by label at `(0,0,0)`. Breadth-first through exits whose direction is
     standard and whose target is in the package: the target gets `coord + delta` unless it already has a coordinate
     or that coordinate is taken. Rooms left without a coordinate are returned in the second list (shown as
     "off-map"). Exits to other packages produce `GridRoom(external = True)` entries with no coordinate.
  5. `render_map` for one `z` level: each room is a 3-character cell `[A]`, using letter codes `A`, `B`, ... in id
     order, with a legend `A hall: The Grand Hall` below. Draw `-` for east–west connections, `|` for north–south, `/`
     and `\` for diagonals, and `X` where two diagonals cross. Mark rooms with an up exit `^`, down `v`, and portals `*`
     after the cell. North is at the top.

### Test & Documentation Requirements

- `tests/test_grid.py`: every direction; paired exits with doors on both sides; every validation message once;
  a valid 3×3 mansion with a cellar; portals with and without `b_exit`; `place_existing` on the tutorial rooms from
  `describe_world_min.json`; a contradictory pair of exits leaves a room off-map; `render_map` of a small grid compared
  with an expected string, including a crossing `X`.

### Exit Conditions

- [ ] `uv run pytest tests/test_grid.py` passes.
- [ ] The four checks pass; §6 matches the code.

---

## Phase 7: Prompting, shared validation and staged checks

### Objective & Scope

- **In Scope:** prompt templates and context budgeting (`prompting.py`); the shared mode helpers (`modes/common.py`):
  `Context`, the `Engine` protocol, API- and schema-driven validation helpers, `stage_and_check` and `finalize`; the
  two system prompts and the lint-repair prompt.
- **Out of Scope:** *The agent must not attempt these in this phase.* Any mode pipeline (area creation is Phase 8);
  the generated test (Phase 8); sessions and the REPL (Phase 10).

### Context Working Set

- **Read / Reference Only:** signatures in `src/gups_builder/{engine,llm,star}/`; "Shared types" in this milestone.
- **Files to Modify / Create:** `src/gups_builder/prompting.py`, `src/gups_builder/prompts/{system_logic,system_prose,
  repair_lint}.md`, `src/gups_builder/modes/{__init__,common}.py`, `tests/test_prompting.py`, `tests/test_common.py`,
  `docs/SPEC.md` §7.1.
- **Do Not Touch:** `star/`, `grid/`, `engine/`, `llm/` (call only).

### Specification & Decision Record Changes

- §7.1 the shared pipeline: brief → plan → validate → prose → ops → staged validation → proposal; which failures are
  repaired by the model and which are reported; context budgeting.

### Implementation Blueprint

- **Signatures & Types:**

```python
# prompting.py
def load_prompt(name: str, **fields: str) -> str: ...      # str.format_map on prompts/<name>.md; a missing field is an error
def estimate_tokens(text: str) -> int: ...                  # len(text) // 4 + 1
def fit(sections: list[tuple[int, str]], budget: int) -> str: ...
    # (priority, text): keep sections in order; drop the highest priority number first until it fits; never drop priority 0

# modes/common.py
class Engine(Protocol):           # what modes and the REPL need from engine.gups.Gups; tests pass fakes
    def describe_world(self, world: Path | None = None) -> WorldDescription: ...
    def api(self) -> ApiModel: ...
    def lint(self, package: str, world: Path | None = None) -> list[LintFinding]: ...
    def test(self, package: str, world: Path | None = None) -> TestRun: ...

@dataclass
class Context:
    world_dir: Path; world: WorldDescription; api: ApiModel
    package: str; view: PackageView; files: dict[str, str]     # files: current content of the package's .star files
    llm: LlmClient; config: Config; engine: Engine

class Check(BaseModel):            # what stage_and_check found
    lint_errors: list[LintFinding]; lint_warnings: list[LintFinding]
    test_failure: str | None       # output of a failing gups test, or None

def allowed_values(api: ApiModel, world: WorldDescription, decl: str, param: str) -> list[str]: ...
def param_allowed(api: ApiModel, world: WorldDescription, decl: str, param: str) -> bool: ...   # False when its feature is off
def new_id(existing: set[str], wanted: str) -> str: ...     # snake_case, [a-z][a-z0-9_]*, suffix _2, _3 on clashes
def stage_and_check(ctx: Context, proposal: Proposal, builder_test: str | None) -> Check: ...
def finalize(ctx: Context, build: Callable[[list[str]], Proposal],
             builder_test: Callable[[Proposal], str | None]) -> Proposal: ...
```

- **Algorithmic Steps & Invariants:**
  1. `stage_and_check` copies the world with `staged_world`, writes `apply_ops(ctx.files, ctx.view, proposal.ops)`
     output, `proposal.new_files` and (when given) `builder_test.star` into the package directory of the copy, then runs
     `engine.lint` on the package and, when the package is a zone and lint has no errors, `engine.test`.
  2. `finalize` calls `build([])`, then `stage_and_check`. **Only lint errors are sent back to the model**: it calls
     `build(problems)` with each error rendered as `<file>:<line>: <message>` plus the three source lines around it,
     and repeats up to `config.max_repairs` times. A failing `gups test` is not repaired by the model, because exits,
     doors and the test are all derived by Python: its output goes into `notes` as `BUILDER BUG: the generated test
     failed; please report it:` followed by the output. After the last repair, remaining lint errors go into `notes`
     with the prefix `NOT VALID:`. Lint warnings always go into `notes` unchanged.
  3. `allowed_values` reads `api.declaration(decl)` param `param`'s `allowed_values`; `param_allowed` is False when
     that param's `feature` is set and `world.has_feature(feature)` is False.
  4. Prompts: `system_logic.md` says the model plans GUPS content and replies with JSON only, following the schema it
     is given. `system_prose.md` says it writes evocative, concise MUD prose in second person present tense and never
     mentions game mechanics or numbers. `repair_lint.md` takes fields `problems` and `previous` (the previous reply).
  5. Context: every mode builds its prompt from `(priority, text)` sections: 0 for the request, the rules and the
     current target declarations; 1 for the schema and the API entries the step needs; 2 for balance references and
     candidate labels from other packages. Budget: `config.<role>.context_tokens - config.<role>.max_tokens`.
  6. **Model-facing schemas** (every pydantic type passed to `call_structured` in any mode) use only objects, lists,
     strings, numbers, booleans and string enums: no tuples (use small objects such as `XYZ {x, y, z}`), no
     recursion except where a phase says so, and no `dict[str, Value]` with free-form values except the `args` fields
     a phase names. Local grammar engines reject or ignore more complex schemas.

### Test & Documentation Requirements

- `tests/test_prompting.py`: `load_prompt` substitutes and errors on a missing field; `fit` drops priority 2 before 1
  and never drops 0.
- `tests/test_common.py` (a fake `Engine` returning scripted `Check`s through `lint`/`test`, and `FakeLlm` unused):
  - `finalize` with clean lint returns the first proposal and calls `build` once;
  - lint errors trigger `build(problems)` with the rendered problem; after `max_repairs` the errors are in `notes` as
    `NOT VALID:`;
  - a failing test produces a `BUILDER BUG:` note and no repair;
  - `allowed_values("room", "size")` from `api_min.json`; `param_allowed` False with `track` off;
  - `new_id` clashes and normalization.

### Exit Conditions

- [ ] `uv run pytest tests/test_prompting.py tests/test_common.py` passes.
- [ ] The four checks pass; §7.1 matches the code.

---

## Phase 8: Creating a new area and the generated test

### Objective & Scope

- **In Scope:** the area *creation* pipeline (a request for a new area in an empty or new package, producing a
  `Proposal`) and the generated test file `builder_test.star`.
- **Out of Scope:** *The agent must not attempt these in this phase.* Refining an existing area (Phase 9); sessions
  and the REPL (Phase 10); mob tests (Phase 11).

### Context Working Set

- **Read / Reference Only:** `modes/common.py`, `prompting.py`, `grid/` and `star/` signatures;
  `gups-engine/docs/SPEC.md` §4.2 (`room`, `exit`, `door`, `spawn`, `item`) and §4.12 (harness API).
- **Files to Modify / Create:** `src/gups_builder/prompts/{area_brief,area_graph,room_prose,item_prose}.md`,
  `src/gups_builder/modes/{area,testgen}.py`, `tests/fixtures/llm/area_mansion/*.json` (scripted replies),
  `tests/test_area_create.py`, `tests/test_testgen.py`, `docs/SPEC.md` §7.2 (creation) and §7.5.
- **Do Not Touch:** `star/`, `grid/`, `engine/`, `llm/`, `modes/common.py` (call only).

### Specification & Decision Record Changes

- §7.2 area creation, including the size words and the coordinate rules given to the model. §7.5 the generated test.

### Implementation Blueprint

- **Signatures & Types:**

```python
# modes/area.py
SIZE_WORDS = {"tiny": (2, 4), "small": (4, 8), "medium": (8, 15), "large": (15, 30)}

class XYZ(BaseModel): x: int; y: int; z: int
class RoomCount(BaseModel): min: int; max: int
class AreaBrief(BaseModel):
    title: str; theme: str; style_notes: str
    room_count: RoomCount
    room_concepts: list[str]              # one short phrase per intended room
    link_to: str | None = None            # an existing room label outside the package
    questions: list[str] = []             # non-empty: ask the user, don't plan yet
class PlannedRoom(BaseModel): id: str; concept: str; at: XYZ; size: Literal["cramped", "normal", "large"] = "normal"; tags: list[str] = []
class PlannedKey(BaseModel): id: str; concept: str
class PlannedLink(BaseModel): room: str; exit: str; external_label: str; return_exit: str | None = None
class RoomTodo(BaseModel): room: str; text: str
class AreaPlan(BaseModel):
    rooms: list[PlannedRoom]; connections: list[Connection]; portals: list[Portal] = []
    keys: list[PlannedKey] = []
    entry: str                             # the room players arrive in
    link: PlannedLink | None = None        # to brief.link_to
    todos: list[RoomTodo] = []
class RoomProse(BaseModel): name: str; desc: str
class ItemProse(BaseModel): name: str; keywords: list[str]; desc: str

def create_area(ctx: Context, request: str) -> AreaBrief | Proposal: ...   # a brief with questions means "ask the user"
def plan_to_grid(plan: AreaPlan) -> Grid: ...

# modes/testgen.py
def builder_test(package: str, world: WorldDescription, view_after: PackageView) -> str: ...
```

- **Algorithmic Steps & Invariants:**
  1. **Brief.** Logic call (`area_brief.md`) with the request, the size words, the package's existing rooms, and the
     candidate link targets (rooms of other zone packages: label and name, priority 2). The reply is an `AreaBrief`.
     If it has `questions`, return it.
  2. **Plan.** Logic call (`area_graph.md`) with the brief, the coordinate rules (axes, adjacency, portals, as one
     short paragraph), allowed room sizes (`allowed_values(api, world, "room", "size")`; leave sizes out of the
     prompt when `param_allowed` is False) and existing ids. `extra_check` builds `plan_to_grid(plan)` and returns
     `validate(...)` with `known_items` = the planned key ids plus existing item labels. This is where the grid is
     repaired.
  3. **Prose.** One prose call per room (`room_prose.md`): the brief's theme and style notes, the room concept and size,
     and its neighbours as `north: <concept>` lines from `derive_exits` (with door states), so the description agrees
     with the exits. Ask for a single-paragraph `desc` of 2–4 sentences that does not list the exits. One prose call per
     key (`item_prose.md`). `extra_check` rejects empty strings, newlines in `name` or `desc`, and a `name` over 60
     characters.
  4. **Ops.** For a new package, `new_files["package.star"]` is the text in "package.star for a new zone" below. One
     `AddDecl` per room into `rooms.star` with `name`, `desc`, `size` (only when allowed and not `normal`), `tags` when
     any, and `exits` = `[{"$call":"exit","args":[{"$const":"DIR.<direction>"}],"kwargs":{"to":":<id>"}}]` in
     `derive_exits` order. A standard direction uses `DIR.<name>`; a portal's custom exit name is a plain string. A door
     adds `"door": {"$call":"door","kwargs":{"state":{"$const":"DOOR.<state>"},"key":":<key id>","tags":[...]}}` (omit
     empty `tags` and a `None` key). One `AddDecl` per key into `items.star` with `name`, `keywords`, `desc` and
     `kind = "key"`. `AddTodo` per `todos` entry. When `link` is set, add the exit from the new room to
     `external_label`, and put the return exit (`AppendItem` on the external room, in its own package) in
     `Proposal.link_ops`, applied only when the user confirms (Phase 10).
  5. **Build function.** Steps 2–4 run inside the `build` callback given to `finalize`; with `problems`, the plan call
     gets `repair_lint.md` with the problems and the previous plan, and prose is re-requested only for rooms whose
     concept, coordinate or neighbours changed.
  6. `summary`: `+<n> rooms, +<n> items, <n> exits` (and `, 1 link to <label> (needs confirmation)` when `link_ops`).
  7. **Generated test** (`testgen.builder_test`), only for `kind = "zone"` packages, rewritten in full each time; the
     text is in "builder_test.star" below. One function per room with at least one testable exit. An exit is testable
     when its direction is one of the ten standard directions, it has no door or its door is `open`, and its target is
     in the package. Each block after the first spawns a new player (`Walker2`, `Walker3`, ...) in the room, so each
     step starts there. Mob tests are added in Phase 11.

package.star for a new zone:

```python
package(
    api_version = <world.api_version>,
    kind = "zone",
    title = "<brief.title>",
    reset_world_ticks = 10,
)
```

builder_test.star:

```python
# Generated by gups-builder from this package's rooms and mobs; it is rewritten on every write.
# Put your own tests in another *_test.star file.

def test_builder_package_loads(t):
    t.world(seed = 1)

def test_builder_<room id>_exits(t):
    w = t.world(seed = 1)
    p = w.spawn_player("Walker", at = ":<room id>")
    p.do("<direction>")
    t.eq(p.room, ":<target room id>")
```

### Test & Documentation Requirements

- `tests/test_area_create.py` with scripted `FakeLlm` replies in `tests/fixtures/llm/area_mansion/` and a fake
  `Engine` whose lint is clean:
  - a brief with questions is returned as-is, with no further calls;
  - a 9-room mansion (two floors and a cellar) with one locked door and its key, producing the expected ops (compare
    ops as JSON) and `package.star`;
  - a plan with a non-adjacent connection is repaired once (the second reply is valid), and the repair message lists
    the validation sentence;
  - prose with a newline is repaired;
  - `size` is left out when the world has `track` off (a copy of `describe_world_min.json` with `track` removed);
  - `@pytest.mark.engine`: with the real `Gups`, the scripted mansion staged into a copy of the template world passes
    `gups lint` and `gups test` with no repairs and no `BUILDER BUG` note.
- `tests/test_testgen.py`: expected text for a three-room package with one closed door and one portal (neither gets a
  test block).

### Exit Conditions

- [ ] `uv run pytest tests/test_area_create.py tests/test_testgen.py` passes (the engine test passes with `gups`
  installed).
- [ ] The four checks pass; §7.2 (creation) and §7.5 match the code.

---

## Phase 9: Refining an area

### Objective & Scope

- **In Scope:** turning a follow-up message in area mode into a structured turn (edit, prose rewrite, question, or out
  of scope), converting area edits into `StarOp`s against the current files, and keeping grid coordinates stable.
- **Out of Scope:** *The agent must not attempt these in this phase.* Sessions on disk and the REPL (Phase 10); mob and
  skill modes.

### Context Working Set

- **Read / Reference Only:** `modes/common.py`, `modes/area.py` (Phase 8), `grid/`, `star/ops.py`, `star/writer.py`.
- **Files to Modify / Create:** `src/gups_builder/modes/area.py`, `src/gups_builder/modes/common.py` (the `Turn`
  types), `src/gups_builder/prompts/{area_turn,room_rewrite}.md`, `tests/fixtures/llm/area_refine/*.json`,
  `tests/test_area_refine.py`, `docs/SPEC.md` §7.1 (turns) and §7.2 (refinement).
- **Do Not Touch:** `star/`, `grid/`, `engine/`, `llm/`.

### Specification & Decision Record Changes

- §7.1: the turn kinds and how out-of-scope requests become notes. §7.2: every area edit, what it changes, and how the
  grid stays stable across turns.

### Implementation Blueprint

- **Signatures & Types:**

```python
# modes/common.py
class Turn(BaseModel):
    kind: Literal["edit", "question", "out_of_scope", "chat"]
    reply: str                          # what to tell the user, one or two sentences
    other_mode: Literal["area", "mob", "skill"] | None = None   # for out_of_scope
    note: str | None = None             # the request, kept for that mode

# modes/area.py: AreaEdit, a discriminated union on "edit"
AddRoom(edit="add_room", id, concept, at: XYZ, size="normal", tags=[])
RemoveRoom(edit="remove_room", id)
Connect(edit="connect", a, b, door: DoorSpec | None = None)
Disconnect(edit="disconnect", a, b)
SetDoor(edit="set_door", a, b, door: DoorSpec | None)
AddPortal(edit="add_portal", portal: Portal)
SetRoom(edit="set_room", id, size: str | None = None, tags: list[str] | None = None, regions: list[str] | None = None)
Rewrite(edit="rewrite", ids: list[str], guidance: str)      # prose only
AddKey(edit="add_key", id, concept)
AddSpawn(edit="add_spawn", room, mob: str, count: int = 1, respawn_pulses: int | None = None)   # mob: an existing label
RemoveSpawn(edit="remove_spawn", room, mob: str)
AddTodo(edit="add_todo", room, text)

class AreaTurn(Turn):
    edits: list[AreaEdit] = []

def area_turn(ctx: Context, grid_coords: dict[str, Coord], message: str, history: list[Message]) -> tuple[AreaTurn, Proposal | None]: ...
```

- **Algorithmic Steps & Invariants:**
  1. Build the current grid: `place_existing(ctx.world.rooms_in(pkg), pkg, grid_coords)`. Off-map rooms and their exits
     are listed in the prompt as `off-map` with a note that only portals may reach them.
  2. Logic call (`area_turn.md`): the current rooms (id, name, coordinate, exits with doors, spawns, and which fields
     are opaque), the existing mobs that may be spawned (label, name, level; priority 2), the last 6 messages of
     `history`, the edit catalogue with one line per edit, and the message. Reply: `AreaTurn`. `extra_check` applies
     the edits to a copy of the grid and returns `validate(...)` (without a room range), plus an error for any edit
     naming a declaration that is opaque.
  3. `question` and `chat` return no proposal. `out_of_scope` returns no proposal and its `note`.
  4. `edit`: convert edits to ops against the current files. Geometry edits recompute the derived exits of the
     affected rooms and diff them with the current `exits` of each room: `RemoveItem` (matched by `first_arg`) for
     exits that went away or changed, `AppendItem` for new ones. Doors are set on both sides. `RemoveRoom` also removes
     every exit pointing to it. `AddSpawn` appends `{"$call":"spawn","args":[label],"kwargs":{"count":n}}` (plus
     `respawn_pulses` when given) to `spawns`. `Rewrite` and new rooms make prose calls (`room_rewrite.md` gets the old
     text and the guidance); unchanged rooms are not rewritten.
  5. Coordinates of new rooms are added to `grid_coords`; removed rooms are deleted from it. The caller saves them in
     the session (Phase 10).
  6. Every edit turn ends with `finalize` (staged lint and test, repair). A repair here asks for a corrected
     `AreaTurn`.
  7. A request to edit an opaque value is refused by `extra_check` first; if the model still produces it, the
     `OpRefused` reasons go into `notes` and the rest of the proposal stands.

### Test & Documentation Requirements

- `tests/test_area_refine.py`, starting from the files and `describe-world` fixture of the Phase 8 mansion:
  - "add a wine cellar under the kitchen": one `AddRoom` at the kitchen's `z - 1` and a `Connect`, producing an
    `AppendItem` on the kitchen and a new room;
  - "lock the study": a `SetDoor` producing doors on both sides and a new key with `AddKey`;
  - "make the library creepier": only the library's `desc` changes;
  - "add a ghost": an `out_of_scope` turn with `other_mode = "mob"` and no proposal;
  - an edit that would put two rooms on one coordinate is repaired;
  - a room whose `exits` are `$opaque` is refused with the file and line in `notes`;
  - coordinates stay the same for untouched rooms across two turns.

### Exit Conditions

- [ ] `uv run pytest tests/test_area_refine.py tests/test_area_create.py` passes.
- [ ] The four checks pass; §7.1 and §7.2 match the code.

---

## Phase 10: Sessions and the interactive loop

### Objective & Scope

- **In Scope:** session state on disk; the REPL with slash commands; writing, undo, reload; the `area`, `resume` and
  `sessions` subcommands working end to end with area mode.
- **Out of Scope:** *The agent must not attempt these in this phase.* Mob and skill modes (their subcommands still say
  `not implemented yet`).

### Context Working Set

- **Read / Reference Only:** `modes/common.py`, `modes/area.py`, `star/writer.py`, `star/diff.py`, `grid/ascii.py`,
  `engine/gups.py`, `llm/log.py`, `cli.py`.
- **Files to Modify / Create:** `src/gups_builder/session.py`, `src/gups_builder/repl.py`, `src/gups_builder/cli.py`,
  `tests/test_session.py`, `tests/test_repl.py`, `docs/SPEC.md` §8, `README.md` (quick start).
- **Do Not Touch:** `star/`, `grid/`, `engine/`, `llm/`, `modes/` (call only).

### Specification & Decision Record Changes

- §8: the session file, every slash command, the review flow, write/undo/reload rules, and hand-edit detection.

### Implementation Blueprint

- **Signatures & Types:**

```python
class PendingChange(BaseModel):
    ops: list[StarOp]; new_files: dict[str, str]; link_ops: list[StarOp] = []; link_package: str | None = None
    base_hashes: dict[str, str]          # hashes of the files the ops were built against
    summary: str; notes: list[str]

class SessionState(BaseModel):
    version: Literal[1] = 1
    name: str; mode: Literal["area", "mob", "skill"]; package: str
    created: str; updated: str
    history: list[tuple[Literal["user", "builder"], str]] = []
    grid: dict[str, tuple[int, int, int]] = {}
    notes: list[tuple[str, str]] = []                  # (mode, request)
    pending: PendingChange | None = None
    builder_test_hash: str | None = None               # sha256 of builder_test.star as last written
    undo: dict[str, str | None] | None = None          # file -> previous content (None: file didn't exist) for /undo
    written_hashes: dict[str, str] = {}                # file -> sha256 right after the last /write

class Session:
    @staticmethod
    def path(world: Path, name: str) -> Path: ...      # <world>/.builder/sessions/<name>.json
    @classmethod
    def create(cls, world: Path, mode: str, package: str, name: str | None) -> "Session": ...
    @classmethod
    def load(cls, world: Path, name: str) -> "Session": ...
    def save(self) -> None: ...                        # write to a temp file, then os.replace

class Repl:
    def __init__(self, session: Session, ctx_factory: Callable[[], Context], console: Console,
                 read_line: Callable[[str], str]) -> None: ...
    def run(self) -> int: ...
    def handle(self, line: str) -> bool: ...           # False on /quit
```

- **Algorithmic Steps & Invariants:**
  1. Session names default to the package's last path segment (`mansion`), then `mansion-2`, ... when taken.
     `gups-builder sessions` lists name, mode, package and `updated`. On first use the builder writes
     `<world>/.builder/.gitignore` containing `*` so nothing in `.builder/` is committed by accident.
  2. `ctx_factory` re-reads the package (`read_package`) and calls `describe-world` (and `docs --json` once per
     process). It runs before every model turn and on `/reload`; the REPL keeps the last `Context` and rebuilds it only
     when any package file's hash changed or `/reload` was typed.
  3. Before any model turn, check the target package in `describe-world`. If it is listed with `ok: false`, print
     its `errors` and `Fix these first (the package doesn't load); /reload when done.` and send nothing to a model;
     `/reload`, `/show`, `/diff`, `/map`, `/notes`, `/help` and `/quit` still work. Otherwise, plain text in area mode
     calls `create_area` when the `PackageView` (from the files, not from `describe-world`) has no `room`
     declarations and no opaque ones, else `area_turn`. A brief with questions
     prints the questions. A proposal becomes `pending` (replacing any earlier one) and the REPL prints the summary,
     notes, the map of each affected `z` level, and the new or changed prose. Out-of-scope turns add to `notes` and
     print the turn's `reply` plus `Noted for /mode <other>.`
  4. Slash commands, exactly:
     - `/map [z]`: `render_map` (default: every level with rooms), then the off-map rooms.
     - `/show <id>`: the source text of that declaration, from the file.
     - `/diff`: `unified_diff` of applying `pending` to the current files (and `builder_test.star`).
     - `/write`: see step 5.
     - `/discard`: clear `pending`.
     - `/undo`: if every file still has its `written_hashes` value, restore `undo`'s contents (delete files that were
       `None`), clear `undo`, re-lint; else refuse with `<file> changed since the last write; /undo would lose those changes`.
     - `/reload`: rebuild the context and say what changed (files whose hashes changed).
     - `/mode area|mob|skill`: switch the session mode, keep the package; print notes for the new mode.
     - `/notes`, `/help`, `/quit`.
     Unknown slash commands print `/help`.
  5. `/write`:
     1. Re-read the files. If any `base_hashes` differ, re-apply `pending.ops` to the current files with `apply_ops`
        and print each refusal.
     2. If `link_ops` exist, ask `Also add the return exit in <package of external room>? [y/N]`; apply them only on
        `y`.
     3. Regenerate `builder_test.star` for zone packages. If its hash on disk differs from `builder_test_hash` (a hand
        edit), write `builder_test.star.new` instead and say so.
     4. Record `undo` (previous contents), write each file (temp file, then `os.replace`), and record
        `written_hashes` and `builder_test_hash`.
     5. Run `gups lint` on the package, print the result, clear `pending`, and save the session.
  6. The session is saved after every handled line. `Ctrl-C` cancels the current model call and returns to the
     prompt; `Ctrl-D` is `/quit`.
  7. `cli.py`: `area [package] [--name]` creates or (when a session with that name exists) asks whether to resume;
     `resume <name>` loads; the package defaults to asking `Package path (e.g. zones/mansion):`. A package directory
     that doesn't exist is created only in area mode, when the first proposal is written.
  8. The REPL uses `rich.Console` for output and `prompt_toolkit.PromptSession` (with history in
     `<world>/.builder/history`) for input; tests inject `read_line` and a `Console(record=True)`.

### Test & Documentation Requirements

- `tests/test_session.py`: create/load/save round trip; name collision; atomic save; `.builder/.gitignore` created.
- `tests/test_repl.py` (FakeLlm, a temp world with the `describe_world_min.json` fixture served by a fake `Engine`):
  - a creation turn, `/diff`, `/write`: files appear on disk and the session has `written_hashes`;
  - hand-edit a file between proposal and `/write`: the ops are re-applied; a refused op is printed;
  - `/undo` restores; `/undo` after a hand edit is refused;
  - a hand-edited `builder_test.star` makes `/write` produce `builder_test.star.new`;
  - `link_ops` applied only after `y`;
  - `/mode mob` prints the noted request;
  - a package that describe-world reports `ok: false` (a hand edit broke it): plain text prints its errors and makes no
    model call, `create_area` is never called even though describe-world lists no rooms, and `/reload` after a fix
    resumes;
  - `@pytest.mark.engine`: a scripted three-turn session against a copy of the template world, then `gups lint` and
    `gups test` of the new package pass.
- `README.md`: a quick start that walks through the creepy-mansion example.

### Exit Conditions

- [ ] `uv run pytest` passes.
- [ ] The four checks pass; §8 matches the code.
- [ ] Manually (when a model is available): `uv run gups-builder --world <copy of template-world> area zones/mansion`
  creates a mansion that passes `gups lint`.

---

## Phase 11: Mob mode

### Objective & Scope

- **In Scope:** the `mob` subcommand and `/mode mob`: creating one mob (or a few variants) with loot items and
  placement spawns, refining mobs, and mob tests in `builder_test.star`.
- **Out of Scope:** *The agent must not attempt these in this phase.* Skill mode; standalone items; anything needing
  `think` or hooks (those become `AddTodo`).

### Context Working Set

- **Read / Reference Only:** `gups-engine/docs/SPEC.md` §4.2 (`mob`, `loot`, `spawn`) and §4.7 (behaviors);
  `tests/fixtures/api_min.json` (behaviors and their params); `tests/fixtures/tutorial/mobs.star`; `modes/area.py` as
  the pattern for brief → plan → prose → ops → finalize and for turns.
- **Files to Modify / Create:** `src/gups_builder/modes/mob.py`, `src/gups_builder/modes/testgen.py` (mob tests),
  `src/gups_builder/prompts/{mob_brief,mob_plan,mob_prose,mob_turn}.md`, `src/gups_builder/repl.py` and `cli.py`
  (dispatch), `tests/fixtures/llm/mob_*/*.json`, `tests/test_mob.py`, `docs/SPEC.md` §7.3 and §7.5.
- **Do Not Touch:** `star/`, `grid/`, `engine/`, `llm/`, `modes/area.py`.

### Specification & Decision Record Changes

- §7.3: what mob mode creates and may edit, every validation rule, and how stats are balanced. §7.5: the mob tests.

### Implementation Blueprint

- **Signatures & Types:**

```python
class BehaviorSpec(BaseModel): name: str; args: dict[str, Value] = {}
class LootSpec(BaseModel): item: str; chance: int = 100; concept: str | None = None; kind: str = "misc"   # item: existing label, or a new id with a concept
class SpawnSpec(BaseModel): room: str; count: int = 1; respawn_pulses: int | None = None   # room: an id in the package
class PlannedMob(BaseModel):
    id: str; concept: str
    level: int | None = None
    stats: dict[str, int] = {}; vitals: dict[str, int] = {}; resists: dict[str, int] = {}
    skills: dict[str, int] = {}          # existing skill label -> proficiency 0..100
    senses: dict[str, int] = {}
    behaviors: list[BehaviorSpec] = []
    factions: list[str] = []; traits: list[str] = []
    loot: list[LootSpec] = []; spawns: list[SpawnSpec] = []
    todos: list[str] = []
class MobPlan(BaseModel): mobs: list[PlannedMob]
class MobProse(BaseModel): name: str; keywords: list[str]; desc: str
# MobEdit union: AddMob(mob: PlannedMob), RemoveMob(id), SetMob(id, fields: dict[str, Value]) (a named free-form field),
# AddLoot(id, loot: LootSpec), RemoveLoot(id, item), Place(spawn: SpawnSpec, mob: str), Unplace(room, mob), Rewrite(ids, guidance)
class MobTurn(Turn): edits: list[MobEdit] = []

class MobBrief(BaseModel): concepts: list[str]; notes: str = ""; questions: list[str] = []
def create_mobs(ctx: Context, request: str) -> MobBrief | Proposal: ...   # a brief with questions means "ask the user"
def mob_turn(ctx: Context, message: str, history: list[Message]) -> tuple[MobTurn, Proposal | None]: ...
```

- **Algorithmic Steps & Invariants:**
  1. Brief (`mob_brief.md`, an `AreaBrief`-like `MobBrief` with `concepts`, `questions`), then plan (`mob_plan.md`).
     The plan prompt includes: the stat, vital and damage type names with labels; which features are on; the allowed
     behaviors with their params and docs from `api.json`; existing skills (label, kind, target; only when `skills` is
     on); factions; plugin traits; and **balance references**: the existing mobs of the same package, plus up to 10
     other mobs closest in level, with their level, stats and vitals (priority 2).
  2. `extra_check` rejects: stat/vital/resist names not in the schema; `level` when `leveling` is off; `skills` when
     `skills` is off, or a skill label that doesn't exist; proficiency outside 0–100; a behavior not in `api.json`, or an
     argument name it doesn't have; a faction label that doesn't exist; a trait no plugin declares; a `spawns` room not
     in the package; a loot `item` that is neither an existing label nor a new id with a `concept`; a `chance` outside
     1–100. Any parameter whose `api.json` entry has a `feature` that is off.
  3. Prose: one call per mob (`mob_prose.md`) and per new loot item (`item_prose.md`, from Phase 8).
  4. Ops: `AddDecl` of each mob into `mobs.star`: `name`, `keywords`, `desc`, then `level`, `stats` as
     `{"$call":"stats","kwargs":{...}}`, `vitals` as `{"$call":"vitals",...}`, `resists` as `{"$call":"resists",...}`,
     `skills`/`senses` as `$dict`, `behaviors` as a list of `$call`s, `factions`, `traits`, `loot` as
     `{"$call":"loot","args":[":<item>"],"kwargs":{"chance":n}}` (omit `chance` when 100). Omit empty fields. New loot
     items go into `items.star` as in Phase 8, with `kind` from `LootSpec.kind`. Spawns: `AppendItem` on the room's `spawns`. `todos` become `AddTodo`.
  5. Mob tests appended by `testgen` for each mob of the package, using the world's vital names:

```python
def test_builder_<mob id>_vitals(t):
    w = t.world(seed = 1)
    m = w.spawn(":<mob id>", at = ":<a room of the package>")
    t.eq(m.max_<vital>, <declared value>)      # one line per declared vital
```

A mob whose package has no rooms gets only the load test (it can't be spawned anywhere it owns).
  6. Refinement mirrors Phase 9: `mob_turn.md` shows the package's mobs (with opaque fields marked), returns a
     `MobTurn`; `SetMob` maps to `SetArg` per field (refused on opaque values); `Rewrite` rewrites prose only.
  7. `cli.py` and `repl.py`: `mob` subcommand and `/mode mob` now work. Mob mode requires an existing package; when
     it doesn't exist, say so and suggest area mode.

### Test & Documentation Requirements

- `tests/test_mob.py`: a goblin and a goblin shaman with one known skill, a loot item and a spawn, compared as ops;
  each `extra_check` rejection once (unknown stat, unknown skill, unknown behavior arg, feature off); a variant world
  (copy of `describe_world_min.json` with the robot-world schema: `integrity`, `power`, `sanity`, ...) produces
  `vitals(integrity = ...)`; refinement "make the shaman tougher" changes only `vitals`; `testgen` mob test text;
  `@pytest.mark.engine`: the scripted goblins added to a staged template world pass `gups lint` and `gups test`.

### Exit Conditions

- [ ] `uv run pytest` passes.
- [ ] The four checks pass; §7.3 and §7.5 match the code.

---

## Phase 12: Skill mode

### Objective & Scope

- **In Scope:** the `skill` subcommand and `/mode skill`: creating one skill of any `kind` with a declarative effect
  and its `unrecognized` lines, optionally teaching it to mobs of the package, and refining skills.
- **Out of Scope:** *The agent must not attempt these in this phase.* `resolve` functions or cast hooks (they become
  `AddTodo`); new mobs.

### Context Working Set

- **Read / Reference Only:** `gups-engine/docs/SPEC.md` §4.3 (skills: ranges, targets, `unrecognized`, which primitives
  each target allows); `tests/fixtures/api_min.json` (`skill` params, `effects`); `tests/fixtures/arcane/spells.star`;
  `modes/mob.py` as the pattern.
- **Files to Modify / Create:** `src/gups_builder/modes/skill.py`, `src/gups_builder/prompts/{skill_brief,skill_plan,
  skill_prose,skill_turn}.md`, `src/gups_builder/repl.py`, `src/gups_builder/cli.py`, `tests/fixtures/llm/skill_*/*.json`,
  `tests/test_skill.py`, `docs/SPEC.md` §7.4.
- **Do Not Touch:** `star/`, `grid/`, `engine/`, `llm/`, `modes/area.py`, `modes/mob.py`.

### Specification & Decision Record Changes

- §7.4: what skill mode creates and may edit, the effect form, and every validation rule.

### Implementation Blueprint

- **Signatures & Types:**

```python
class EffectSpec(BaseModel):
    primitive: str                      # an api.json effect, or a plugin effect "<plugin id>.<name>"
    args: dict[str, Value] = {}
    effects: list["EffectSpec"] = []    # only for "sequence"; the one recursive model-facing type (allowed)
class Teach(BaseModel): mob: str; proficiency: int   # mob: an id in the package
class PlannedSkill(BaseModel):
    id: str; concept: str; kind: str
    cost: dict[str, int] = {}; warmup: int = 0; lag: int = 4
    range: str | None = None; target: str
    channels: list[str] = []; tags: list[str] = []
    interrupt: dict[str, Value] = {}
    effect: EffectSpec | None = None
    teach: list[Teach] = []
    todos: list[str] = []
class UnrecognizedProse(BaseModel):
    noun: str | None = None; begin: str | None = None; room: str | None = None
    victim: str | None = None; resisted_room: str | None = None; resisted_victim: str | None = None
# SkillEdit union: AddSkill(skill), RemoveSkill(id), SetSkill(id, fields), SetEffect(id, effect), Teach(id, mob, proficiency), Rewrite(ids, guidance)
class SkillTurn(Turn): edits: list[SkillEdit] = []
class SkillBrief(BaseModel): concept: str; kind: str; notes: str = ""; questions: list[str] = []
def create_skill(ctx: Context, request: str) -> Proposal | SkillBrief: ...
def skill_turn(ctx: Context, message: str, history: list[Message]) -> tuple[SkillTurn, Proposal | None]: ...
```

- **Algorithmic Steps & Invariants:**
  1. Skill mode requires the `skills` feature; without it, refuse at start with `This world has the skills feature
     off.` The target package may be a `skills` or a `zone` package; when it doesn't exist and its path starts with
     `skills/`, the first write creates `package.star` with `kind = "skills"`.
  2. The plan prompt includes: the vital names (for `cost`); damage types; channels; whether `track` is on and the
     allowed `range` and `target` values (`allowed_values(api, world, "skill", ...)`); the effect primitives with their
     params and docs; plugin effects by name; existing skills of the package and a few elsewhere as balance references
     (kind, cost, warmup, lag, effect source text from the files when editable).
  3. `extra_check` rejects:
     - `range` when `track` is off and it is `melee`, `near` or `far`, or `range` not in the allowed values;
     - `target` not allowed;
     - a cost vital not in the schema;
     - an unknown primitive or argument name;
     - `sequence` without `effects`, or `effects` on another primitive;
     - `knockback`/`pull` when `track` is off;
     - for `target = "room"`, any primitive other than `apply_affect`, `dispel`, `sequence` and plugin effects;
     - for `target = "item"`, any other than `apply_affect`, `sequence` and plugin effects;
     - a damage `type` not in the schema;
     - a `teach` mob not in the package, or a proficiency outside 0–100.
  4. Prose (`skill_prose.md`) writes `UnrecognizedProse`. `extra_check` rejects multi-line or empty values, unknown
     placeholders (only `{actor}` and `{target}`), `{target}` when `target` is `self`, `none` or `room`, and `{target}`
     in `room` when `range` is `room`. `resisted_*` lines are requested only for `enemy` skills.
  5. Ops: `AddDecl` into `skills.star` (or the package's existing file holding skills, if there is exactly one):
     `kind`, `cost` as `vitals(...)`, `warmup` (omit 0), `lag`, `range`, `target`, `channels`, `interrupt` as `$dict`,
     `tags`, `unrecognized` as `$dict` of the non-empty lines, `effect` as nested `$call`s (`sequence(<effects>...)` as
     positional args; damage `type` as `{"$const":"DAMAGE.<type>"}`). `teach` becomes `SetArg` on the mob's `skills`
     `$dict` with the new entry added (refused when that value is opaque). `todos` become `AddTodo`.
  6. Refinement mirrors Phase 11.

### Test & Documentation Requirements

- `tests/test_skill.py`: a fire `spell` like `spark`, compared with an expected `render_decl` string; a `maneuver`
  with `range = "melee"` and `knockback`; a `sequence` effect; each rejection in step 3 and step 4 once; a world with
  `track` off rejects `near`; teaching a skill to an existing mob; `@pytest.mark.engine`: the scripted skill staged into
  the template world's `skills/arcane` package passes `gups lint`.

### Exit Conditions

- [ ] `uv run pytest` passes.
- [ ] The four checks pass; §7.4 matches the code.

---

## Phase 13: End-to-end check and finalization

### Objective & Scope

- **In Scope:** one end-to-end engine test across all three modes; README and SPEC review; the final checks.
- **Out of Scope:** *The agent must not attempt these in this phase.* New features, new modes (a standalone item mode
  and a hook-writing mode are future work: list them in §10 as not built).

### Context Working Set

- **Read / Reference Only:** the whole `src/gups_builder/` tree (signatures), `docs/SPEC.md`.
- **Files to Modify / Create:** `tests/test_end_to_end.py`, `tests/fixtures/llm/end_to_end/*.json`, `README.md`,
  `docs/SPEC.md` (§9 Testing, §10, and corrections anywhere it disagrees with the code).
- **Do Not Touch:** `gups-engine/`.

### Specification & Decision Record Changes

- §9: the test layers (unit with `FakeLlm`, engine-marked tests, the end-to-end test) and how to run them with and
  without `gups`. §10: open choices recorded during the milestone, plus "Not built: item mode, hook-writing mode,
  re-import of `describe-world` fields that are not in format 1".

### Implementation Blueprint

- **Algorithmic Steps & Invariants:**
  1. `tests/test_end_to_end.py` (`@pytest.mark.engine`), with scripted replies, against a copy of the template world,
     through `Repl.handle` lines only:
     1. `area zones/mansion`: "a medium sized creepy mansion" → `/write`;
     2. a hand edit to one room's `desc` in `rooms.star`, then "add a wine cellar under the kitchen" → `/write` (the hand
        edit survives);
     3. `/mode mob`: "a restless ghost in the cellar" → `/write`;
     4. `/mode skill`: "a chilling touch the ghost uses" taught to the ghost → `/write`.
     After each write: `gups lint` has no errors, and after the last, `gups test //zones/mansion/...` passes.
  2. Read `docs/SPEC.md` top to bottom against the code; fix the spec wherever it is wrong.
- Mark `DESIGN_NEEDED`: there is no entry for this work in either repository; nothing to mark.

### Test & Documentation Requirements

- `README.md`: install, configure (Ollama and llama.cpp examples, two roles), `gups-builder models`, the three modes
  with one example each, the slash commands, and where sessions and logs live.

### Exit Conditions

- [ ] `uv run pytest` passes, including engine tests with `gups` on `PATH`.
- [ ] The four checks pass.
- [ ] `docs/SPEC.md` matches the code; §10 lists the open choices and what is not built.
