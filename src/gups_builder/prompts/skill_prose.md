Write the lines a GUPS MUD shows to players who do not recognize a skill.
Style notes: {notes}
Concept: {concept}
Kind: {kind}
Target: {target}
Range: {range}
The lines are noun, begin, room, victim, resisted_room and resisted_victim.
noun names the skill in the engine's anonymous lines. begin is the warmup
line. room is what bystanders see when the cast resolves. victim is what the
target sees. resisted_room and resisted_victim are the lines when an enemy
skill is resisted, and they exist only for a skill whose target is enemy.
A line may use {{actor}} and {{target}}, and no other placeholder. When the
target is self, none or room, no line may use {{target}}; when the range is
room, the room line may not use {{target}} either. Leave a line null when the
skill has none; every line is a single non-empty line.
