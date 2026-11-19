The builder is in area mode. Read the user's message and reply with a turn.

The turn's kind is one of: `edit` (make the edits below), `question` (ask the
user to settle something before you can act), `out_of_scope` (the request
belongs to another mode; set `other_mode` and keep the request in `note`), or
`chat` (answer it; no edits). An `edit` turn carries zero or more edits.

Edits you may make:
{catalogue}

Current rooms (id, name, coordinate, exits with their doors, spawns, the items
on the floor, and the fields computed by code, which you must not edit):
{rooms}
