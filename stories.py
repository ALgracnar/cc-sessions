"""Stories: collapsible groups of related sessions in the sidebar.

A story is just a name with members. Sessions get into one three ways:

  * by hand      -- right-click a session, or drag it among a story's rows
  * by parent    -- a session started by another session (through the
                    start-session API) joins its starter's story; the story is
                    created and named after the starter if it has none yet
  * by name      -- sessions named alike ("AT-43-backend", "AT-43-pwa") group
                    under their shared prefix, without anything being saved.
                    A by-name story becomes a saved one the moment you rename
                    it or drag something into it.

Pure logic, no GTK, so it can be tested on its own. The window decides what
each row looks like; this module only decides which rows belong together and
how to sum a story up in one line.
"""

import re
from collections import Counter

PREFIX_ID = "prefix:"

# How a story sums up its members, most urgent first. The first state present
# also picks the header's colour, so a story with one blocked session in it
# shows the blocked colour even when it is collapsed -- a collapsed story must
# never hide a session that is waiting on you.
#   (member state, label in the summary, colour name in the app's palette)
SUMMARY_ORDER = (
    ("blocked", "needs you", "waiting"),
    ("finished", "finished", "finished"),
    ("busy", "busy", "busy"),
    ("idle", "idle", "idle"),
    ("done", "done", "done"),
    ("terminal", "terminal", "shell"),
    ("dormant", "to resume", "dim"),
    ("ended", "ended", "dim"),
)


def name_prefix(name):
    """The shared part of a family of names, or None.

    'AT-43-backend' -> 'AT-43'.  Needs at least three dash-separated parts, so
    a plain 'nevron-core' groups with nothing. The '-2', '-3' the app appends
    to a taken name is ignored first, otherwise every 'nevron-core-2' would
    claim a 'nevron-core' family.
    """
    if not name:
        return None
    base = re.sub(r"-\d+$", "", name.strip())
    parts = [p for p in base.split("-") if p]
    if len(parts) < 3:
        return None
    return "-".join(parts[:2])


def group(items, key_of, name_of, story_of, stories, use_prefix=True,
          ignored_prefixes=()):
    """Turn an ordered list of rows into blocks.

    items       rows, already in display order (automatic or manual)
    key_of      row -> stable key (the same key manual ordering uses)
    name_of     row -> display name
    story_of    {key: story id}, the saved memberships
    stories     [{"id", "name"}], the saved stories
    Returns a list of ("item", row) and ("story", story, [rows]).

    A story sits where its FIRST member would have been. In the automatic
    order that is its most urgent member, so a story with something waiting on
    you rises to the top as a block; in the manual order it is wherever you put
    its first session.
    """
    known = {s["id"]: s for s in stories}
    assigned = {}
    for item in items:
        sid = story_of.get(key_of(item))
        if sid in known:
            assigned[id(item)] = sid

    # Name families, among rows no saved story has claimed. A family of one is
    # not a story.
    if use_prefix:
        ignored = set(ignored_prefixes)
        candidates = {}
        for item in items:
            if id(item) in assigned:
                continue
            prefix = name_prefix(name_of(item))
            if prefix and prefix not in ignored:
                candidates.setdefault(prefix, []).append(item)
        for prefix, members in candidates.items():
            if len(members) < 2:
                continue
            sid = PREFIX_ID + prefix
            known[sid] = {"id": sid, "name": prefix, "virtual": True}
            for item in members:
                assigned[id(item)] = sid

    blocks = []
    open_blocks = {}
    for item in items:
        sid = assigned.get(id(item))
        if sid is None:
            blocks.append(("item", item))
            continue
        block = open_blocks.get(sid)
        if block is None:
            block = ("story", known[sid], [])
            open_blocks[sid] = block
            blocks.append(block)
        block[2].append(item)
    return blocks


def summarize(states):
    """(colour name, text) for a story header, from its members' states."""
    counts = Counter(states)
    parts = ["%d %s" % (counts[state], label)
             for state, label, _colour in SUMMARY_ORDER if counts[state]]
    colour = next((c for state, _label, c in SUMMARY_ORDER if counts[state]), "dim")
    return colour, "  ·  ".join(parts) or "empty"


def is_virtual(story_id):
    return isinstance(story_id, str) and story_id.startswith(PREFIX_ID)
