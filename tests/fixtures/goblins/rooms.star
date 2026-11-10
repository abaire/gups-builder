room("hall",
    name = "The Hall",
    desc = "Dust lies thick across the hall.",
    exits = [exit(DIR.east, to = ":gate", door = door(state = DOOR.locked, key = ":brass_key"))],
    spawns = [spawn(":goblin", count = 2)],
)

room("gate",
    name = "The Gate",
    desc = "A wooden gate stands open to the south.",
    exits = [exit(DIR.west, to = ":hall")],
)
