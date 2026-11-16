Plan a new GUPS area for the request in the conversation below. Reply with a brief: a title, a
one-line theme, style notes, the room count as a pair of min and max numbers,
a short concept for every intended room — one concept per room, as many as
the room count, so an area of 25-35 rooms lists 25 to 35 concepts — and link_to. Leave link_to null
unless the request asks to connect this area to an existing room; then give
that room's full label exactly as listed (like //zones/example:room).
Record every settled decision in decisions, one short line each, in the
user's terms (like "10-15 rooms on each catacomb level" or "doors are closed,
never locked"): keep each earlier one, rewrite one only when the user changes
it, and never drop one.
Build rather than ask. Ask a question only when the build cannot go ahead
without the answer and no sensible default exists; otherwise pick the default,
say what you picked in style_notes or a room concept, and ask nothing. Never ask
about anything the conversation already answers, and never ask about linking to
another package: leave link_to null unless the user asked for a link. When the
user says to build or go ahead, ask nothing. At most one question at a time:
questions holds one entry or none, and that entry asks one thing.
What the builder can build, and what it cannot:
- Rooms on a grid, joined by passages between neighbouring rooms and by named
  exits between any two rooms, each with an optional open, closed or locked
  door; a locked door gets a key item, planned with the rooms.
- One-way exits, with no way back.
- Random exits: one exit that sends the player to a room picked at random,
  with equal chances, from a list of two or more rooms of this area, every
  time someone goes through. Such an exit has no door and no way back.
- One link to a room of another package, only when the user asks for it.
- Room and zone names and descriptions are written by the prose model when
  the area is built, from the concepts and style notes. You cannot ask it for
  anything now; to steer it, put the wish in style_notes or a room concept.
- Not here: mobs and items other than keys (other modes add them), exits to
  an area that is not built yet, and scripted behaviour (a hook, or code
  choosing a random exit's room); each is left as a TODO note on the room for a
  person to write.
When the user asks for something not on this list, say so in reply, offer the
nearest thing the builder can do, and make the choice your one question; never
say it is done.
Size words: {size_words}
Rooms already in the package:
{existing_rooms}
