# Mobs, and the hooks that give them personality.

load("//lib:quest.star", "quest")
load("//lib:shop.star", "shop")
load("//lib:trainer.star", "trainer")
load(":helpers.star", "FIRST_STEPS", "READY_WORD", "said")

def _guide_hears(ctx):
    # on_say on a mob fires when someone *else* in the room speaks. ctx.text is what they
    # said and ctx.actor is who said it.
    if said(ctx.text, READY_WORD):
        # Attributes are this package's own state on any entity. Keys are namespaced
        # automatically ("ready" is stored as "tutorial:ready"), so packages never clash.
        ctx.set_attr(ctx.actor, "ready", True)
        ctx.say(ctx.self, "Then off you go, %s. The arena is north." % ctx.actor.name)
    elif said(ctx.text, "key") and not ctx.actor.has_item(":brass_key"):
        # give() spawns a fresh item straight into the player's inventory.
        ctx.give(ctx.actor, ":brass_key")
        ctx.emote(ctx.self, "hands %s a brass key." % ctx.actor.name)
        # from_step makes sure only a player on step 0 moves on.
        quest.advance(ctx, ctx.actor, FIRST_STEPS, from_step = 0)

def _guide_blocks(ctx):
    # A before_* hook on a mob is an *observer*: it fires for other actors' actions in the
    # mob's room. Returning ctx.deny(msg) vetoes the action, shows msg to the actor, and
    # costs them no lag. Returning anything else (or nothing) allows it.
    if ctx.direction == "north" and ctx.actor.is_player and not ctx.get_attr(ctx.actor, "ready", False):
        return ctx.deny("The guide blocks your way: say '%s' first." % READY_WORD)

def _guide_listens(ctx):
    # on_perceive is perception: it fires for events in the mob's own room, and for events
    # that reach it from rooms nearby while rules.can_perceive says it notices them. ctx.channel
    # says how it came, ctx.intensity how strong it started, ctx.cost how much it faded, and
    # ctx.via_exit is the guide's own exit toward the source.
    if ctx.event == "combat_start" and ctx.via_exit == "north":
        ctx.say(ctx.self, "I hear someone sparring in the arena. Good!")
    elif ctx.event == "crunch":
        # Events a script raises with ctx.emit arrive here like any other.
        ctx.say(ctx.self, "Someone is crunching about on the gravel to the west.")

def _guide_congratulates(ctx):
    # On a mob, on_level_change fires for the mob's own level changes and, as an observer,
    # for anyone else's in its room, so check who ctx.actor is. ctx.old_level and
    # ctx.new_level always differ by exactly one; a lost level has new_level < old_level.
    if ctx.actor.is_player and ctx.new_level > ctx.old_level:
        ctx.say(ctx.self, "Well done on reaching level %d, %s!" % (ctx.new_level, ctx.actor.name))

mob("guide",
    name = "the guide",
    desc = "A patient old adventurer with a lantern.",
    level = 10,
    vitals = vitals(hp = 200),
    senses = {"sound": 1},
    factions = ["//lib:townsfolk"],
    # Built-in behaviors: sentinel() keeps the guide at its post.
    behaviors = [sentinel()],
    on_say = _guide_hears,
    before_exit = _guide_blocks,
    on_perceive = _guide_listens,
    on_level_change = _guide_congratulates,
)

def _mend(ctx):
    # A timer callback: a top-level function called with a fresh ctx whose self is the
    # entity that set the timer. Keyword arguments given to ctx.after arrive as keywords.
    if ctx.self.in_world and ctx.self.hp < ctx.self.max_hp:
        ctx.heal(ctx.self, ctx.self.max_hp - ctx.self.hp, "hp")
        ctx.echo(ctx.self.room, "The training dummy creaks and mends itself.")
    ctx.del_attr(ctx.self, "mending")

def _dummy_hit(ctx):
    # on_damage is dispatched to the damaged entity first, then to observers, so check
    # that this hook is about the dummy itself.
    if ctx.target != ctx.self:
        return
    ctx.emote(ctx.self, "wobbles.")
    # Do checks first and verbs last: if a hook fails half way, the verbs it already ran
    # stay applied.
    if ctx.self.hp * 2 < ctx.self.max_hp and not ctx.get_attr(ctx.self, "mending", False):
        ctx.set_attr(ctx.self, "mending", True)
        ctx.after(8, _mend)    # 8 pulses = 2 seconds = one combat round

mob("training_dummy",
    name = "a training dummy",
    keywords = ["dummy", "training"],
    desc = "A straw dummy on a post. It has seen better days.",
    vitals = vitals(hp = 30),
    on_damage = _dummy_hit,
)

# The shopkeeper. A shop is a keeper bound to a stock container and a set of prices, all
# described by one shop.define call. The stock lines say what to keep and how it restocks
# over time; the till is the coins the keeper has to pay out with.

SHOP = shop.define(
    id = "shopkeeper",
    stock = [
        shop.stock("//zones/tutorial:copper_ring", count = 3, every = 600),
        shop.stock("//zones/tutorial:wooden_shield", count = 2, every = 600),
        shop.stock("//zones/tutorial:healing_draught"),   # no count: unlimited, spawned on demand
    ],
    till = shop.till(count = 100, every = 60, amount = 10),
    buy_markup = 120,    # the shop sells at 120% of item value
    sell_rate = 50,      # and buys at 50%
)

def _shop_price(ctx, action, customer, item, label, count, price, refusal):
    # The keeper's custom hook, fired by the shop library before it settles any price.
    # Returning an int overrides the price, returning a string refuses the transaction,
    # and returning None keeps the library's default. `action` is "list", "buy" or "sell";
    # `customer` is the acting player, `item` the item (None for an unlimited stock line),
    # and `price` the library's default for this customer (None when it would refuse).
    #
    # A customer whose charisma is above 10 is flattered and gets a discount on buying; the
    # keeper pays them more for selling. Each point of charisma above 10 is worth 2%.
    if action == "sell" and item != None and item.has_tag("stolen"):
        return "I don't deal in stolen goods."
    if price == None:
        return None     # keep the library's refusal
    bonus = max(0, customer.cha - 10) * 2
    if action in ("list", "buy"):
        return max(1, price * (100 - bonus) // 100)
    return price * (100 + bonus) // 100

def _shop_open(ctx):
    # A shop opens when its keeper spawns: the library fills a locked chest in the keeper's
    # room and gives the keeper the key. Re-spawning a keeper reuses the chest and catches
    # the stock up to where the time has brought it.
    shop.open(ctx, ctx.self, SHOP)

mob("shopkeeper",
    name = "the shopkeeper",
    desc = "A brisk merchant with ink-stained fingers and a chest full of wares.",
    vitals = vitals(hp = 60),
    factions = ["//lib:townsfolk"],
    behaviors = [sentinel(), aggressive(toward = "all", disposition_below = -40)],
    hooks = {"shop_price": _shop_price},
    on_spawn = _shop_open,
)

# The mentor teaches the arcane spells in the arena, where a training dummy stands ready for
# practice. A trainer is a keeper bound to a set of skills and the levels it will raise them to,
# all described by one trainer.define call. Students spend practice sessions, earned by changing
# level, to learn and improve a skill.

MENTOR = trainer.define(
    id = "mentor",
    teaches = [
        trainer.teach("//skills/arcane:spark", up_to = 50, gain = 10),
        trainer.teach("//skills/arcane:mend", up_to = 40, gain = 10),
    ],
)

def _mentor_offer(ctx, customer, skill, current, gain, cost, refusal):
    # The keeper's custom hook, fired by the trainer library before a session is settled.
    # Returning None keeps the library's default (one session), a string refuses the session,
    # and a dict may replace gain and/or cost. `current` is the student's proficiency, or None
    # for a skill they don't know yet. Once a student knows a skill well, the mentor charges two
    # sessions instead of one to push them to learn something new.
    if current != None and current > 30:
        return {"cost": 2}
    return None

def _mentor_open(ctx):
    # A trainer opens when its keeper spawns: the library marks the keeper as teaching MENTOR.
    trainer.open(ctx, ctx.self, MENTOR)

mob("mentor",
    name = "the mentor",
    desc = "A robed figure who hums to themselves, sparks of light dancing between their fingers.",
    vitals = vitals(hp = 60, mana = 40),
    factions = ["//lib:townsfolk"],
    behaviors = [sentinel()],
    skills = {"//skills/arcane:spark": 100, "//skills/arcane:mend": 100},
    hooks = {"practice_offer": _mentor_offer},
    on_spawn = _mentor_open,
)

mob("familiar",
    name = "a spectral familiar",
    keywords = ["familiar", "spectral"],
    desc = "A translucent, glowing creature bound to serve its summoner.",
    vitals = vitals(hp = 20),
    behaviors = [wander()],
)

mob("goblin",
    name = "a goblin scout",
    keywords = ["goblin", "scout"],
    desc = "A wiry green creature with shifty yellow eyes.",
    vitals = vitals(hp = 20),
    factions = ["//lib:goblins"],
    behaviors = [wander()],
)

mob("gate_guard",
    name = "the gate guard",
    keywords = ["guard", "gatekeeper"],
    desc = "A stern guard in polished mail watching the gate.",
    vitals = vitals(hp = 100),
    factions = ["//lib:townsfolk"],
    behaviors = [guard(exit = DIR.north, disposition_above = 50)],
)
