room("hall",
    name = "The Hall",
    desc = "Dust lies thick across the hall.",
    size = SIZE.large,
    exits = [
        exit(DIR.east, to = ":kitchen", door = door(state = DOOR.locked, key = ":silver_key")),
        exit(DIR.north, to = ":study"),
        exit(DIR.up, to = ":master_bedroom"),
    ],
)

room("kitchen",
    name = "The Kitchen",
    desc = "Dust lies thick across the kitchen.",
    exits = [
        exit(DIR.west, to = ":hall", door = door(state = DOOR.locked, key = ":silver_key")),
        exit(DIR.north, to = ":library"),
        exit(DIR.east, to = ":parlour"),
    ],
)

room("study",
    name = "The Study",
    desc = "Dust lies thick across the study.",
    exits = [exit(DIR.south, to = ":hall"), exit(DIR.east, to = ":library")],
)

room("library",
    name = "The Library",
    desc = "Dust lies thick across the library.",
    exits = [exit(DIR.south, to = ":kitchen"), exit(DIR.west, to = ":study"), exit(DIR.up, to = ":nursery")],
)

room("parlour",
    name = "The Parlour",
    desc = "Dust lies thick across the parlour.",
    exits = [exit(DIR.west, to = ":kitchen"), exit(DIR.up, to = ":bedroom"), exit(DIR.down, to = ":cellar")],
)

room("master_bedroom",
    name = "The Master Bedroom",
    desc = "Dust lies thick across the master bedroom.",
    exits = [exit(DIR.down, to = ":hall")],
)

room("nursery",
    name = "The Nursery",
    desc = "Dust lies thick across the nursery.",
    exits = [exit(DIR.down, to = ":library"), exit(DIR.southeast, to = ":bedroom")],
)

room("bedroom",
    name = "The Bedroom",
    desc = "Dust lies thick across the bedroom.",
    exits = [exit(DIR.down, to = ":parlour"), exit(DIR.northwest, to = ":nursery")],
)

room("cellar",
    name = "The Cellar",
    desc = "Dust lies thick across the cellar.",
    size = SIZE.cramped,
    tags = ["dark"],
    exits = [exit(DIR.up, to = ":parlour")],
    spawns = [spawn(":ghost", count = 1)],
)

# The vault's exits are an expression joined with a helper list, which the reader
# cannot hold as a plain value: it is opaque and the builder never edits it. Its
# only exit is one-way; the hall has no exit back to the vault.
_VAULT_EXTRA = []

room("vault",
    name = "The Vault",
    desc = "Dust lies thick across the vault.",
    exits = [exit(DIR.east, to = ":hall")] + _VAULT_EXTRA,
)
