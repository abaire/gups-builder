Rewrite the unrecognized lines of one skill of a GUPS MUD.
Style notes: {notes}
Skill id: {skill_id}
Target: {target}
Range: {range}
Current lines:
{old_text}
Guidance: {guidance}
Reply with the six lines, noun, begin, room, victim, resisted_room and
resisted_victim, each a single non-empty line or null. A line may use {{actor}}
and {{target}}, and no other placeholder. When the target is self, none or
room, no line may use {{target}}; when the range is room, the room line may not
use {{target}} either. resisted_room and resisted_victim exist only when the
target is enemy.
