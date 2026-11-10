# The arcane school's spells. spark lashes a single enemy; mend mends the caster's wounds.
# soothe heals an ally, and ward hardens an ally's armor for a while.
# familiar summons a spectral companion to the caster's side.
# pacify calms a mob that is fighting the caster.

skill("spark",
      kind = "spell",
      cost = vitals(mana = 8),
      warmup = 0,
      lag = 4,
      range = "near",
      target = "enemy",
      channels = ["spell", "sound"],
      tags = ["blue"],
      unrecognized = {"noun": "spark", "begin": "{actor}'s fingertips crackle with heat."},
      effect = damage(dice = "2d4", type = DAMAGE.fire),
)

skill("mend",
      kind = "spell",
      cost = vitals(mana = 10),
      lag = 4,
      range = "self",
      target = "self",
      effect = heal(dice = "2d6", vital = "hp"),
)

skill("soothe",
      kind = "spell",
      cost = vitals(mana = 8),
      lag = 4,
      range = "near",
      target = "ally",
      effect = heal(dice = "2d6", vital = "hp"),
)

skill("ward",
      kind = "spell",
      cost = vitals(mana = 8),
      lag = 4,
      range = "near",
      target = "ally",
      effect = apply_affect(
          name = "warded",
          duration = 60,
          stat_deltas = stats(armor = 2),
          expire_msg = "Your ward fades.",
      ),
)

# Calls a spectral familiar that follows its caster and defends them in combat.
skill("familiar",
      kind = "spell",
      cost = vitals(mana = 12),
      lag = 4,
      range = "self",
      target = "self",
      effect = summon("//zones/tutorial:familiar", duration = 600),
)

PACIFY_PULSES = 40   # how long a pacified mob notices nobody

def _clamp(n, lo, hi):
    return max(lo, min(hi, n))

# Ends one mob's fight with the caster alone and makes the mob forget the caster. The affect keeps
# it from noticing anyone for a while; standing is untouched, so a grudge returns when it ends.
def _pacify(ctx):
    mob = ctx.target
    if mob == None or mob == ctx.actor or mob.is_player:
        ctx.send(ctx.actor, "That has no quarrel to forget.")
        return
    chance = ctx.power
    if ctx.has_feature("leveling"):
        chance -= 10 * max(0, mob.level - ctx.actor.level)
    if ctx.roll("1d100") > _clamp(chance, 5, 95):
        ctx.send(ctx.actor, "{} shrugs off your calm.".format(mob.name))
        return
    if ctx.actor in mob.fighting():
        ctx.stop_combat(mob, ctx.actor)
    # The affect is a helpful one and the mob remembers it, so forget after applying it.
    ctx.apply_affect(mob, "pacified", PACIFY_PULSES, flags = ["pacified"])
    ctx.forget(mob, subject = ctx.actor)
    ctx.send(ctx.actor, "{} calms, its anger toward you gone.".format(mob.name))
    ctx.echo(ctx.room, "{} calms down.".format(mob.name), exclude = [ctx.actor])

skill("pacify",
      kind = "spell",
      cost = vitals(mana = 15),
      warmup = 8,
      lag = 4,
      range = "near",
      target = "ally",
      interrupt = {"damage_pct": 10},
      resolve = _pacify,
)
