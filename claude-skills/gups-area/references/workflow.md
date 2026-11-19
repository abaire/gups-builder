# Design file walkthrough

`GB design-schema` → `schema` is the authority on field names. This page shows one small, complete design, the
order to fill it in, and the problems you will meet most often.

## A complete small design

Three rooms, one locked gate with its key lying on the stair, a candle on the reliquary's floor, a portal, a
random exit, and one mob with new loot:

```json
{
  "brief": {
    "title": "The Sunken Crypt",
    "theme": "a flooded crypt beneath a ruined chapel",
    "style_notes": "cold water, old stone, candle stubs",
    "room_count": {"min": 3, "max": 4},
    "room_concepts": ["a flooded entry stair", "a vaulted ossuary", "a drowned reliquary"],
    "link_to": null,
    "decisions": ["the reliquary is behind a locked gate", "a loose slab in the ossuary drops back to the stair"],
    "questions": []
  },
  "graph": {
    "rooms": [
      {"id": "stair", "concept": "a flooded entry stair", "size": "cramped", "tags": []},
      {"id": "ossuary", "concept": "a vaulted ossuary", "size": "normal", "tags": []},
      {"id": "reliquary", "concept": "a drowned reliquary", "size": "large", "tags": []}
    ],
    "passages": [
      {"a": "stair", "b": "ossuary", "direction": "north"},
      {"a": "ossuary", "b": "reliquary", "direction": "east",
       "door": {"state": "locked", "key": "iron_key", "tags": []}}
    ],
    "portals": [
      {"a": "ossuary", "a_exit": "loose_slab", "b": "stair", "b_exit": null, "one_way": true}
    ],
    "random_exits": [
      {"room": "reliquary", "exit": "whirlpool", "to": ["stair", "ossuary"]}
    ],
    "keys": [{"id": "iron_key", "concept": "a rusted iron key", "room": "stair"}],
    "items": [{"room": "reliquary", "item": "candle_stub", "concept": "a stub of black candle", "kind": "misc"}],
    "entry": "stair",
    "link": null,
    "todos": [{"room": "reliquary", "text": "the casket should curse whoever opens it"}]
  },
  "room_prose": {
    "stair": {"name": "The Flooded Stair", "desc": "Black water laps at your shins. The steps climb out of it into the dark."},
    "ossuary": {"name": "The Ossuary", "desc": "Skulls line the niches in neat rows. One slab in the floor sits a little proud of the rest."},
    "reliquary": {"name": "The Drowned Reliquary", "desc": "A silver casket rests on a drowned altar. The water here turns slowly, as if something stirs beneath it."}
  },
  "key_prose": {
    "iron_key": {"name": "a rusted iron key", "keywords": ["rusted", "iron", "key"], "desc": "Flakes of rust come away on your fingers."}
  },
  "mobs": {
    "mobs": [
      {
        "id": "skeleton",
        "concept": "a skeletal sentry guarding the ossuary",
        "level": 2,
        "behaviors": [{"name": "sentinel", "args": {}}],
        "loot": [{"item": "bone_charm", "chance": 25, "concept": "a charm of knotted finger bones", "kind": "misc"}],
        "spawns": [{"room": "ossuary", "max": 1}]
      }
    ]
  },
  "mob_prose": {
    "skeleton": {"name": "a skeletal sentry", "keywords": ["skeletal", "sentry", "skeleton"], "desc": "Bones held together by rusted wire creak as it turns its head."}
  },
  "item_prose": {
    "candle_stub": {"name": "a candle stub", "keywords": ["candle", "stub"], "desc": "Black wax, bitten down to the wick."},
    "bone_charm": {"name": "a bone charm", "keywords": ["bone", "charm"], "desc": "Finger bones knotted on a cord click against each other."}
  }
}
```

Use only behaviors, skills, factions and traits that `world-info` lists. The `sentinel` above is an example,
and your world may not have it.

## Fill order and the commands between steps

1. `brief` + `graph` → `apply --check-only`. Fix graph problems first, and ignore the "has no entry" prose
   complaints until the graph is clean.
2. `room_prose` + `key_prose` + `item_prose` for the floor's new items (the check's `new_items`), written from
   the check's `neighbours` → `apply --check-only`.
3. `mobs` → `apply --check-only` (it lists `new_items`) → `mob_prose` + `item_prose` → `apply --check-only`.
4. `apply` (staged lint and test) → show the user → `apply --write [--link]` on a yes.

Exit codes: `0` clean, `1` problems or lint errors (read the JSON), `2` the command could not run (bad JSON,
no `gups`, a file that does not parse). The JSON's `error` says why.

## Common problems and what they mean

The wording below is abridged. Act on the full sentence `apply` prints.

| Problem says | Fix |
|---|---|
| `'a' and 'b' share the coordinate ...` | The passages' directions put two rooms on one cell (often a loop that doesn't close). Change a direction, or make one of the ways a portal. |
| `'x' is not reachable from 'entry'; ...` | Add a passage or portal to it, or change `entry`. |
| `the locked door between 'a' and 'b' has no key` | Name a key id on the door and add it to `graph.keys`. |
| `the key 'x' lies in no room` | Give the key a `room`: one the player reaches without that key. |
| `the id 'x' is already declared in the package` / `used twice in the plan` | Pick a fresh snake_case id. Room, key, mob and item ids share one namespace. |
| the room count is outside the brief's range | Match `brief.room_count` to the rooms, or the rooms to it. |
| `room_prose has no entry for the room 'x'` / `... names 'y', which is not a room of the graph` | Prose keys must be exactly the graph's room ids (likewise for keys, mobs, new items). |
| `x: name is 72 characters, the limit is 60` / `desc must be one paragraph` | Shorten the name, or drop the newline. |
| `the spawn names the unknown room 'x'` | Spawns use room ids of this design. |
| `link_to '...' is not a room of another package` | Use a full label from `world-info` → `area.linkable_rooms`, or `null`. |
