---
name: gups-area
description: Design and build a new GUPS zone (rooms, exits, doors, keys, mobs and their loot) by interviewing the user one question at a time, then generating the .star files through `gups-builder apply`, which lays out the rooms, derives every exit and door, writes builder_test.star, and runs gups lint and gups test. Use when the user wants to create, design, plan or build a new GUPS area, zone, dungeon or map in a GUPS world.
argument-hint: "[zones/<name>] [short description of the area]"
---

# GUPS area designer

You design a new GUPS zone with the user, then build it. You play both of gups-builder's models: the
**logic** model (the brief, the room graph, the mob plan) and the **prose** model (names and descriptions).
You never write `.star` files yourself. You write one JSON **design file**, and `gups-builder apply` checks it,
lays out the grid, derives the exits and doors on both sides, writes the generated test, and stages it through
`gups lint` and `gups test`. The builder's checks are the authority. When they report a problem, fix the
design and apply again.

## 1. Preflight

1. **World:** the current directory, or the nearest ancestor that holds `MODULE.gups`. If there is none, ask
   the user for the world path. Never build inside `gups-engine/template-world`. If the user wants to try
   things out, offer to copy it first.
2. **gups-builder:** call the command you settle on `GB` below.
   - First try `gups-builder design-schema`. An older install on `PATH` lacks the design commands, so trust the
     `PATH` binary only if this command succeeds.
   - If it fails, find the checkout. The skill is usually installed as a symlink, so resolve this skill's base
     directory to its real path first:

     ```bash
     python3 -c 'import os,sys;print(os.path.realpath(sys.argv[1]))' <base dir>
     ```

     The checkout is two directories above that real path. It is the right one if its `pyproject.toml` has
     `name = "gups-builder"`. Then use `uv run --project <checkout> gups-builder`.
   - If neither works, ask the user for their gups-builder checkout. Tell them that `uv tool install --force .`
     in it refreshes the command on `PATH`.
3. **gups:** `GB` finds the engine the same way it always does (`--gups`, `GUPS_BIN`, the `gups` config value,
   then `PATH`). Don't hardcode a path. If a command fails with "gups binary not found", ask the user where
   their `gups` is and pass it with `--gups PATH` (a global flag, before the subcommand).
4. **Package:** take it from the arguments, or ask for it (`zones/<name>`, snake_case). Then run:

   ```bash
   GB --world <world> world-info <package>
   GB design-schema
   ```

   - If `world-info` says `"has_rooms": true`, stop. This skill builds new areas only. Point the user at
     `gups-builder area <package>` for refining an existing one.
   - Keep both outputs in mind for the rest of the session:
     - `world-info` has the room sizes, the rooms you may link to, and the mob catalogue (stats, vitals,
       skills, factions, traits, behaviors, and balance references).
     - `design-schema` has the design file's JSON schema and the exact rules the builder's models are given
       (`prompts.*`).
   - Read `prompts.area_plan_turn` before the interview. It was written for the builder's small planning
     model, so take only part of it:
     - **Use** its rules for decisions and room concepts, and its list of rooms, exits, doors, keys, items on
       floors, one-way and random exits, and the link.
     - **Ignore** three things in it:
       - its `/build` instruction (here, the user just says build);
       - "names and descriptions cannot be asked for now" (you write the prose, so honour naming and
         description wishes directly);
       - "mobs belong to mob mode" (this skill builds mobs and their loot too).

     §2 below wins wherever the two disagree.

## 2. Interview

Work toward a settled design one question at a time.

- **Ask exactly one question per message**, the one that most blocks building, with your recommended answer
  so the user can just agree. When there are 2–4 clear options, ask with AskUserQuestion, putting your
  recommendation first. Otherwise ask in plain text. For every other open choice, pick a sensible default and
  say what you picked.
- **Keep a Draft** and reprint it, compact, after every answer:

  ```
  Draft: <title> (<package>)
  Theme: …            Style: …
  Rooms: <min>-<max>  Link: <label or none>
  Room concepts:
    1. …
  Mobs:
    - <concept>, level <n>, in <room concept> (loot: …)
  Decisions:
    - …
  Open question: …
  ```

- **Decisions** are every settled choice, one short line each, in the user's own words ("doors are closed,
  never locked"). Keep every earlier decision. Rewrite one only when the user changes it, and never drop one.
  When a decision changes the rooms, rewrite the room concepts it affects.
- **Size words:** tiny 2–4 rooms, small 4–8, medium 8–15, large 15–30 (see `design-schema` `size_words`).
  Aim for one concept per room.
- **What can be built** is in `prompts.area_plan_turn`, plus mobs and their loot:
  - rooms on a grid joined by compass/up/down passages;
  - named exits (portals) between any two rooms;
  - open, closed or locked doors, where a locked door gets a key lying on the floor of a room the player
    can reach without it;
  - items lying on a room's floor (`graph.items`): new items you describe with a concept, or items the world
    already has (in this package or another). One copy of each is kept; a player who picks it up keeps it, and
    it is put back at a zone reset only once it has left the world;
  - one-way exits;
  - random exits (two or more rooms of the area, no door, no way back);
  - at most one link to an existing room, only when the user asks for it;
  - mobs with level, stats, vitals, resists, skills, senses, behaviors, factions, traits, loot (existing
    items, or new ones you describe) and spawns in the new rooms.
- **What cannot be built:** scripted behaviour (hooks, commands, quests, boss phases), exits to areas that
  don't exist yet, and items inside a container. Say so plainly, offer the nearest thing (usually a TODO
  note on the room or mob, for a person to write later), and never claim it is done.
- Ground mob numbers in `world-info` → `mobs.balance`. Use only skills, factions, traits and behaviors that
  `world-info` lists. Check a behavior's arguments there too.
- Don't ask about anything the conversation has already answered, and don't propose a link the user didn't
  ask for.
- **The user decides when to build.** When they say build, go ahead, or similar, stop asking and go to §3,
  filling any remaining gaps with defaults you state.

## 3. Build

Write the design to `<world>/.builder/designs/<package with / replaced by _>.json`. `world-info` has already
made `.builder/` and its `.gitignore`, so the design stays out of version control. The file is one `AreaDesign` object (schema in `design-schema` → `schema`).
`references/workflow.md` has a complete small example.

Work in this order, because each step's output feeds the next:

1. **Brief and graph.** Fill `brief` from the Draft. The decisions go in `brief.decisions`, and `questions`
   stays empty. Write `graph` following `prompts.area_graph` and `world-info` → `area.layout_rules`:
   - give no coordinates;
   - every passage's direction is the way from `a` to `b`;
   - each room has at most one exit per direction;
   - use portals for named exits;
   - every room must be reachable from `entry`;
   - every locked door names a key in `keys`;
   - use fresh snake_case ids that are not in `area.existing_ids`;
   - use sizes from `area.room_sizes`.

   Set `graph.link` only when `brief.link_to` is set. Then run:

   ```bash
   GB --world <world> apply <package> <design.json> --check-only
   ```

   Fix every problem it lists and rerun until `problems` is empty. Graph problems are reported before prose
   problems, so the prose complaints you see first are expected; ignore them until the graph is clean.
2. **Room, key and floor item prose.** Use the check's `neighbours` (exactly what the builder's prose model
   would be shown) and `maps`. Write `room_prose[<room id>]` for every room, `key_prose[<key id>]` for every key
   and `item_prose[<id>]` for every new floor item the check lists in `new_items`, following
   `prompts.system_prose`, `prompts.room_prose` and `prompts.item_prose`:
   - second person, present tense;
   - a one-paragraph desc of 2–4 sentences;
   - never list exits, and never mention mechanics or numbers;
   - names of at most 60 characters, all on one line.
3. **Mobs.** If the Draft has mobs, write `mobs` (a `MobPlan`) following `prompts.mob_plan`. Spawns name
   room ids of the graph. A loot entry with a `concept` and an id not already declared creates a new item.
   Run `--check-only` again: its `new_items` lists every item that needs `item_prose`, the floors' and the
   loot's. Then write `mob_prose`
   and `item_prose`, following `prompts.mob_prose` and `prompts.item_prose`.
4. **Stage and check:**

   ```bash
   GB --world <world> apply <package> <design.json>
   ```

   This stages the area and mobs on a copy of the world, runs `gups lint`, then `gups test` with the generated
   `builder_test.star`, and writes nothing. If `ok` is false:
   - fix the design for each `problems` or `lint_errors` entry (lint errors show the generated source around
     the line) and rerun;
   - a `test_failure` note starting `BUILDER BUG` is a gups-builder bug, not yours: show it to the user and
     stop;
   - after 5 rounds that don't converge, stop and show the user what is left.
5. **Show the result:** the `summary`, the `maps`, the prose (`prose`), any `notes`, and the `diff`. Shorten a
   long diff to the files and their first lines, and offer the full diff.
6. **Confirm, then write.** Ask the user to confirm the write. If `link_package` is set, ask separately whether
   to also add the return exit in that package. Only after a clear yes:

   ```bash
   GB --world <world> apply <package> <design.json> --write [--link]
   ```

   Report `written` and `lint_after_write` (empty means lint is clean). Mention that the design file stays in
   `.builder/designs/`, and that `gups-builder area <package>` / `mob <package>` can refine the zone from here.

## Rules

- Never edit the world's `.star` files directly, and never run `--write` without the user's go-ahead in this
  conversation.
- The builder's problem messages are full sentences written for a model to act on. Act on them literally.
- When the user asks for a change after building but before writing, update the Draft and the design, then go
  through §3 again from the step the change affects.
