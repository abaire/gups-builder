Plan the area from the brief below. Reply with the plan: one GraphRoom per
room concept, in the brief's order, with a fresh snake_case id, its concept
and a size; a Passage for every way between two rooms that runs in a compass
direction or up or down, with a door when one belongs there; a Portal for an
exit with a name of its own (a hidden passage, a hatch), with one_way true and
no b_exit when there is no way back (a chute, a drop, a door that only opens
from one side); a RandomExit for every exit that sends the player to a room
picked at random each time (a trick door, a shifting passage), listing in `to`
at least two of the plan's room ids it may pick from, named like trick_door,
never with a compass direction, and with no door; a PlannedKey for every key a
locked door needs; the entry room; a RoomTodo on the room, not a portal, for
an exit to an area that is not built yet; and a PlannedLink only when the
brief's link_to is set, with external_label exactly that label. When link_to
is null, link is null.
Brief:
{brief}
Decisions settled with the user; the plan must keep every one:
{decisions}
Layout: {layout_rules}
Room sizes: {sizes}
Ids already used in the package:
{existing_ids}
