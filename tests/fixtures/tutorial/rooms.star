# Rooms and exits. Map:
#
#                      arena (LARGE)
#                        |
#  gravel - tar_pit ---- hall (LARGE) ==== armory == shop   (the guide waits in the hall)
#  (crunchy)  (sticky floor)  |   (locked, creaky door)
#                           gate  <- start here
#
# Each room's `size` sets its track: SIZE.cramped (1 position), SIZE.normal (3) or SIZE.large (5). Combat
# range depends on how far apart actors stand on it.

load("//lib:quest.star", "quest")
load(":helpers.star", "FIRST_STEPS", "greeting")

def _read(ctx):
    # A zone command: `read sign` runs this in the tick lane, like a normal action.
    # ctx.args is the text after the verb.
    if ctx.args in ("", "sign"):
        ctx.send(ctx.actor, "The sign reads: 'Go north to meet the guide. Type `help` for commands.'")
    else:
        ctx.send(ctx.actor, "There is nothing like that to read here.")

room("gate",
    name = "The Tutorial Gate",
    desc = "A wooden gate stands open to the north. A sign is nailed to the post.",
    size = SIZE.normal,
    exits = [exit(DIR.north, to = ":hall")],
    commands = {"read": _read},
)

def _hall_enter(ctx):
    # on_enter fires on the room after an actor arrives. Greet each player only once.
    if ctx.actor.is_player and not ctx.get_attr(ctx.actor, "greeted", False):
        ctx.set_attr(ctx.actor, "greeted", True)
        ctx.send(ctx.actor, greeting(ctx.actor.name))
        # A library helper: it stores the quest in the player's attributes and tells them.
        quest.start(ctx, ctx.actor, FIRST_STEPS)

room("hall",
    name = "The Hall of Lessons",
    desc = "A long, bright hall. Doors lead east and north, a dark passage west; the gate is south.",
    size = SIZE.large,
    exits = [
        exit(DIR.south, to = ":gate"),
        exit(DIR.west, to = ":tar_pit"),
        # Arrive at the far end of the arena: entry_index -1 means the back of the track.
        exit(DIR.north, to = ":arena", entry_index = -1),
        # A locked door. Doors on both sides of a passage open and close together.
        # Tags are free-form words the rules read: rules/manipulation.star makes a "creaky"
        # door twice as loud to open and close, so people next door hear it.
        exit(DIR.east, to = ":armory", door = door(state = DOOR.locked, key = ":brass_key", tags = ["creaky"])),
    ],
    # Spawns refill on zone reset, and respawn_pulses after the last one leaves the world.
    spawns = [spawn(":guide", at_index = 2)],
    on_enter = _hall_enter,
)

def _stock_tar_pit(ctx):
    # Keep one pinecone on the floor for the squirrel on the gravel path (forager.star).
    if not [i for i in ctx.self.items() if i.label == "//zones/tutorial:pinecone"]:
        ctx.spawn(":pinecone", ctx.self)

# The engine gives tags no meaning. The `sticky_floor` tag does something here only because
# rules/combat.star (`_footing`) doubles the advance and retreat lag in rooms that carry it.
room("tar_pit",
    name = "The Tar Pit",
    desc = "Black tar clings to your boots; every step is an effort.",
    size = SIZE.normal,
    tags = ["sticky_floor"],
    exits = [exit(DIR.east, to = ":hall"), exit(DIR.west, to = ":gravel")],
    on_load = _stock_tar_pit,
    on_reset = _stock_tar_pit,
)

def _crunch(ctx):
    # ctx.emit raises a perceivable event: a channel, an event name, and how strong it is where
    # it happens. It spreads through the exits, weakening on the way (rules/perception.star
    # decides how much), and whoever can still perceive it reacts: mobs through on_perceive,
    # players in other rooms through rules.perceive_message, which shows them the text.
    if ctx.actor.is_player:
        ctx.emit("sound", "crunch", intensity = 3, text = "Gravel crunches loudly somewhere to the west.")

room("gravel",
    name = "The Gravel Path",
    desc = "Loose stones shift and crunch with every step.",
    size = SIZE.normal,
    exits = [exit(DIR.east, to = ":tar_pit")],
    spawns = [spawn(":squirrel")],
    on_enter = _crunch,
)

def _stock_armory(ctx):
    # on_load runs once when the world starts; on_reset at every zone reset. Both keep a
    # practice sword on the armory floor without ever stacking up duplicates.
    # (ctx.find(":practice_sword") would also count swords carried by players in the room.)
    on_floor = [i for i in ctx.self.items() if i.label.endswith(":practice_sword")]
    if not on_floor:
        ctx.spawn(":practice_sword", ctx.self)

room("armory",
    name = "The Armory",
    desc = "A cramped closet of racks, mostly empty. A passage leads east to a shop.",
    size = SIZE.cramped,
    exits = [
        exit(DIR.west, to = ":hall", door = door(state = DOOR.locked, key = ":brass_key", tags = ["creaky"])),
        exit(DIR.east, to = ":shop"),
    ],
    on_load = _stock_armory,
    on_reset = _stock_armory,
)

room("shop",
    name = "The Shop",
    desc = "A tidy stall of wares. A stout chest sits by the door and the shopkeeper keeps a close watch on it.",
    size = SIZE.normal,
    exits = [exit(DIR.west, to = ":armory")],
    spawns = [spawn(":shopkeeper")],
)

def _blunted(ctx, base):
    # A modifier gets the rules ctx and the value so far, and returns the new value. This one
    # halves every auto-attack's damage. ctx is read-only: modifiers adjust numbers, nothing else.
    return max(1, base // 2)

# A region is a tag rooms carry (room(regions = [...])); it may span zones. Its modifiers
# apply in every room that carries it. Points: to_hit, defend_hit, damage, bonus_damage,
# receive_damage, mitigate, initiative, cast_allowed, move_cost, regen. `@trace rules` shows each
# modifier at work.
region("practice_grounds",
    modifiers = {MODIFIER.damage: _blunted},
)

def _arena_enter(ctx):
    w = ctx.actor.equipped("wield")
    sword = w != None and w.label.endswith(":practice_sword")
    if ctx.actor.is_player and sword and quest.step(ctx, ctx.actor, FIRST_STEPS) == 2:
        quest.complete(ctx, ctx.actor, FIRST_STEPS)
        ctx.give(ctx.actor, ":leather_cap")
        ctx.give(ctx.actor, ":healing_draught")
        ctx.send(ctx.actor, "The guide calls from the hall: 'Well done! Take these, you've earned them.'")

def _stock_arena(ctx):
    # Keep demonstration equipment on the arena benches.
    for label in (":copper_ring", ":wooden_shield", ":dagger", ":greatsword", ":amulet",
                  ":shadow_cloak", ":siphon_amulet"):
        on_floor = [i for i in ctx.self.items() if i.label.endswith(label)]
        if not on_floor:
            ctx.spawn(label, ctx.self)

room("arena",
    name = "The Practice Arena",
    desc = "Sand, a few benches, and a battered training dummy at the far end.",
    size = SIZE.large,
    regions = ["practice_grounds"],
    exits = [exit(DIR.south, to = ":hall")],
    spawns = [spawn(":training_dummy", at_index = 0), spawn(":mentor", at_index = 4)],
    on_enter = _arena_enter,
    on_load = _stock_arena,
    on_reset = _stock_arena,
)
