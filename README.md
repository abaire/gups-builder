# gups-builder

`gups-builder` is an interactive terminal tool that helps you build [GUPS](../gups-engine) world content with
two local LLMs. A **logic** model plans structure — room graphs, numbers, effects, edits — and a **prose** model
writes the names, descriptions and messages. You steer it in one of three modes: `area` (rooms, exits and doors),
`mob` (mobs and their loot) or `skill` (skills of any `kind`). Every change is reviewed before it touches your
files, and the `.star` files stay the source of truth you can edit by hand at any time.

## Requirements

- Python 3.12 or newer and [`uv`](https://docs.astral.sh/uv/).
- A GUPS engine providing `gups describe-world` with `"format": 1` (Milestone 49 of `gups-engine`).
- A GUPS world to work in (a directory you point `--world` at; the engine's `template-world/` is a good start —
  copy it, don't build in place).
- Two OpenAI-compatible chat endpoints, one per role (see [Configuring the models](#configuring-the-models)).
  Local servers such as Ollama or `llama-server` work.

## Building the engine and referencing the `gups` binary

`gups-builder` never links against the engine; it runs the `gups` command-line tool as a subprocess (`describe-world`,
`docs --json`, `lint`, `test`). Build the launcher from a `gups-engine` checkout (needs JDK 21+):

```bash
cd ../gups-engine
./gradlew :cli:installDist
# -> cli/build/install/gups/bin/gups
```

The binary is looked up in this order, and the first one set wins:

1. `--gups PATH` on the command line (a global flag, before the subcommand),
2. the `GUPS_BIN` environment variable,
3. `gups = "..."` in `<world>/.builder/config.toml`, then in `~/.config/gups-builder/config.toml`,
4. `gups` on your `PATH`.

A path given through 1–3 must be an executable file; it is an error, not a fallback, if it is not. Either put the
launcher's `bin` directory on your `PATH`, or export the variable:

```bash
export GUPS_BIN="$PWD/../gups-engine/cli/build/install/gups/bin/gups"
```

`gups-builder models` prints the `gups` path in effect.

## Installation

To install the command:

```bash
uv tool install .
```

To work on it from a checkout instead, and run it without installing:

```bash
uv sync
uv run gups-builder --help
```

## Configuring the models

`gups-builder` drives two roles, `logic` and `prose`, each an OpenAI-compatible chat endpoint. Point them at a
local server such as Ollama. Create a config file at `~/.config/gups-builder/config.toml`:

```toml
# ~/.config/gups-builder/config.toml

[logic]
base_url = "http://127.0.0.1:11434/v1"
model = "qwen2.5-coder"
temperature = 0.2

[prose]
base_url = "http://127.0.0.1:11434/v1"
model = "llama3.1"
temperature = 0.8
```

A `max_repairs` and `gups` value can be set at the top level of the same file. Settings can be overridden per
world in `<world>/.builder/config.toml`, by environment variables (`GUPS_BUILDER_LOGIC_MODEL`, `GUPS_BIN`, …)
and by command-line flags (`--logic-model`, `--gups`, …). Check what is in effect with `gups-builder models`.

llama.cpp's OpenAI-compatible server (`llama-server`) works the same way. One `llama-server` serves one model, so
run one per role on its own port (for example `llama-server -m logic.gguf --port 8080` and
`llama-server -m prose.gguf --port 8081`) and point each role at its server:

```toml
# ~/.config/gups-builder/config.toml

[logic]
base_url = "http://127.0.0.1:8080/v1"
model = "logic"
temperature = 0.2

[prose]
base_url = "http://127.0.0.1:8081/v1"
model = "prose"
temperature = 0.8
```

`llama-server` answers with whichever model it loaded, but `model` must still be set: an empty one is an error.

## Quick start

`gups-builder models` shows the effective configuration for both roles and checks that each one answers. Then start
a session in a mode, naming the package you are working on:

```bash
gups-builder --world my-world area zones/mansion
```

When no package is given you are asked for one (`Package path (e.g. zones/mansion):`). The first turn is a plain
sentence describing what you want, and the builder answers with a proposal it has already staged against a copy of
the world and run through `gups lint` and `gups test`:

```
> a medium sized creepy mansion
+9 rooms, +1 item, 20 exits
[A]-[B]-[D]
 |
[C]-[E]
A hall: The Hall
...
hall.desc: Dust lies thick across the hall.
```

Nothing touches your files yet. Review with `/diff`, see the layout with `/map` (or `/map <z>` for one level), look at
a declaration with `/show <id>`, and drop the change with `/discard`. When you are happy, `/write` writes the files,
regenerates `builder_test.star`, and lints the package. `/undo` reverses the last write, but only while you have not
edited the written files by hand. `/reload` re-reads the world and the package; `/mode mob` and `/mode skill` switch
modes and print any request noted for them; `/notes` lists those requests; `/help` and `/quit` do what they say. A
request that belongs to another mode is noted, not attempted, so you can switch and pick it up.

### The other two modes

The same session flow works in `mob` and `skill` modes — from the `mob` and `skill` subcommands, or by switching
inside a session with `/mode mob` and `/mode skill`. In the mansion session:

```
> /mode mob
mode is now mob
> a restless ghost that paces the cellar
+1 mob, +0 items, 1 spawn
ghost.name: a restless ghost
ghost.desc: A pale shape drifts through the dark, its eyes like cold lamps.
> /write
wrote 3 file(s)
lint: clean
> /mode skill
mode is now skill
> a chilling touch the ghost uses
+1 skill, 1 mob taught
chilling_touch.unrecognized.victim: {actor}'s chilling touch seeps into you!
...
> /write
wrote 2 file(s)
lint: clean
```

Mob mode needs a package that exists (mobs of a package can be spawned from anywhere, so rooms are optional);
skill mode accepts a zone package or a `skills/` package, and a `skills/` package that does not exist yet is
created, with its `package.star`, on the first `/write`. Skill mode needs a world with the `skills` feature on.

The session, the call log and the prompt history live under `<world>/.builder/` — `sessions/<name>.json`,
`logs/<name>.jsonl` and `history` — and a `.gitignore` there keeps them out of version control. Resume a session with
`gups-builder resume <name>`, and list the sessions of a world with `gups-builder sessions`.

## Development

The tool is built milestone by milestone from [docs/SPEC.md](docs/SPEC.md), which is the source of truth for
behaviour; each milestone is planned in `docs/MILESTONE_N.md`. [AGENTS.md](AGENTS.md) has the working conventions
(test-first, the "skill, not spell" naming rule, the engine boundary).

Before considering a change done, all four must pass:

```bash
uv run ruff format --check
uv run ruff check
uv run mypy
uv run pytest -rs
```

Tests never call a real model; they drive the builder with a `FakeLlm`. Tests marked `engine` need a real `gups`
binary and skip themselves without one — CI has none, so run them locally against your build and check that nothing
is reported as skipped:

```bash
GUPS_BIN=../gups-engine/cli/build/install/gups/bin/gups uv run pytest -rs
```

### Layout

| Path                         | Contents                                                                      |
|------------------------------|-------------------------------------------------------------------------------|
| `src/gups_builder/cli.py`    | Argument parsing and subcommands                                              |
| `src/gups_builder/repl.py`   | The interactive session and its `/` commands                                  |
| `src/gups_builder/config.py` | Layered configuration (flags, env, world file, user file)                     |
| `src/gups_builder/engine/`   | Wrapper around the `gups` subprocess, world description, lint parsing, staging |
| `src/gups_builder/llm/`      | OpenAI-compatible client, structured-reply repair loop, call log, `FakeLlm`   |
| `src/gups_builder/modes/`    | The `area`, `mob` and `skill` pipelines and the generated test                |
| `src/gups_builder/star/`     | Formatting-preserving reading and editing of `.star` files (libcst)           |
| `src/gups_builder/grid/`     | Integer-grid room placement and ASCII maps                                    |
| `src/gups_builder/prompts/`  | Prompt templates                                                              |
