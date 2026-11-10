room("hall",
    name = "The Hall",
    desc = "A low hall, lit by a dying fire.",
    exits = [exit(DIR.east, to = ":gate")],
)

room("gate",
    name = "The Gate",
    desc = "A heavy gate, barred from outside.",
    exits = [exit(DIR.west, to = ":hall")],
)
