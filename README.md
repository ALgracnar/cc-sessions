# cc-sessions

A GTK4 terminal with a live list of your running [Claude Code](https://claude.com/claude-code)
sessions down the left-hand side.

Not affiliated with Anthropic.

## Why

Claude Code sessions running in terminal tabs all look the same. A tab title
can tell you a session is *working* or *not working* — it cannot tell you the
difference between one grinding through a build and one that is **blocked
waiting for you**. With several sessions open you end up cycling tabs to find
out who wants something.

Claude already publishes that signal: every running session writes
`~/.claude/sessions/<pid>.json` containing a status — `busy`, `shell`,
`waiting`, or `idle` — plus `waitingFor` describing what it is blocked on.
Nothing surfaces it. This does.

## What it does

- **Session list** with each session's live status, what it is waiting for, and
  how long it has been that way. Sessions blocked on you sort to the top, and
  the window title reports the count so you can see it from the taskbar.
- **A real terminal**, not an imitation: VTE, the same widget GNOME Terminal is
  built from, with your GNOME Terminal profile applied verbatim — colours,
  palette, font, cell spacing, scrollback.
- **Plain terminal tabs** alongside Claude sessions. Run `claude` by hand in
  one and it is recognised as a session, not a stranger.
- **Session recovery.** What was open when the app last stopped is offered back
  as greyed rows; clicking one resumes the actual conversation with
  `claude -r <id>`. Written continuously, so it survives a crash rather than
  depending on clean shutdown.
- **Clickable links.** Explicit hyperlinks (OSC 8) open on a plain click;
  a plain URL that was merely printed opens with Ctrl+click. Same split as
  GNOME Terminal.
- **Mark a session done.** When Claude reports `idle`, a ✓ appears next to
  the status: click it and the row reads `idle · done` in purple, and stops
  announcing that it finished. For work that is over but has to stay
  visible until it ships. Click again to unmark.
- **Multiple windows**, each owning its own sessions, sharing one recovery pool.
- **Two list orders**: automatic (waiting sessions rise to the top by
  themselves) or static, where you drag rows into the order you want and
  nothing moves on its own.
- **Stories**: related sessions fold under one header with a ▸/▾ arrow and a
  one-line sum of their states ("1 needs you · 3 busy"). A folded story still
  lights up when a session inside it waits on you. Sessions get into a story
  three ways: a session started by another session (via `cc-sessions-start`)
  joins its starter's story automatically; sessions named alike
  (`AT-43-backend`, `AT-43-pwa`) group under their shared prefix (switchable
  in Settings); and by hand -- right-click a session for *Move to…*, *New
  story…*, *Take out of…*, right-click a header to rename or ungroup, or (in
  the static order) drag a session onto a header or among a story's rows.
  Closing a session takes it out of its story; an emptied story disappears.
  A story is one card with a stripe down its left in its status colour
  (yellow needs you, orange finished, blue busy, green idle, purple done,
  periwinkle waiting); loose sessions have no stripe. Drop a session on the
  middle of a header to put it in the story, or on the header's top edge
  (bottom edge, when folded) to put it above (below) the story as a loose row.
- **Mark a session as waiting on it.** A ◷ sits in front of ✓: click it and
  the row reads `idle · waiting` in periwinkle -- parked until something
  outside it happens (a deploy, a reply). Waiting and done exclude each other;
  "needs you" and "finished" still win over it.
- **Story marks.** The story's +▾ dropdown (and right-click) has *Mark story
  as waiting* and *Mark story done*, which colour its stripe and summary.
- **Pin to the top of a story.** Right-click a session in a story, *Pin to top
  of story*: it stays first however many sessions join later, and its drag
  grip becomes ★. Several can be pinned; drag the ★ to order them.

## Install

Three system packages, then the app:

```bash
sudo apt install -y python3-gi gir1.2-gtk-4.0 gir1.2-vte-3.91 gir1.2-webkit-6.0
make install
```

`make install` targets `~/.local` and needs no root. For system-wide:

```bash
sudo make install PREFIX=/usr
```

Or build a package:

```bash
make deb
sudo apt install ../cc-sessions_1.0.0_all.deb
```

Remove with `make uninstall` — your settings in `~/.config/cc-sessions` are
left alone.

### Update

```bash
git pull
make install
```

Then quit and reopen cc-sessions. Quitting closes every session; they come
back as "resumes chat" rows you can reopen with one click.

If the app ever hangs ("not responding"), what it was doing is written to
`~/.cache/cc-sessions/freeze.log`.

## Use

Run `cc-sessions`, or find **Claude Sessions** in your applications.

| | |
|---|---|
| `Ctrl+Shift+T` | new session (dialog: name, folder, Claude or plain terminal) |
| `Ctrl+Shift+W` | close the current session |
| `Ctrl+Shift+N` | new window |
| `Ctrl` `+` / `−` / `0` | terminal text size |

The toolbar buttons are settings, new terminal, and new Claude session. Both
start immediately in your configured default folder; the dialog is for when you
want to choose.

Scripts (and Claude sessions) can do the same from outside:

```sh
cc-sessions-start --name my-task --cwd ~/project --prompt "read note.md"
cc-sessions-close --name my-task            # same as its × button
cc-sessions-close --name my-task --force    # even if busy or waiting on you
```

`cc-sessions-close` leaves a busy or waiting session alone unless `--force`,
and only closes sessions the app started. The transcript stays, so
`claude -r <id>` can still resume the conversation.

## Settings

Stored in `~/.config/cc-sessions/config.json`:

| key | meaning |
|---|---|
| `default_folder` | where the toolbar buttons start sessions |
| `claude_command` | binary to run (default `claude`); a path with `/` is used verbatim |
| `claude_args` | flags added to every session, e.g. `--dangerously-skip-permissions` |
| `auto_sort` | `true` (default) rearranges the list as state changes; `false` keeps it static and drag-orderable |
| `manual_order` | the order you dragged rows into, used when `auto_sort` is off |
| `done` | session ids you marked done |
| `parked` | session ids you marked as waiting on it |
| `stories`, `story_of`, `story_collapsed` | your stories, who is in which, which are folded |
| `story_done`, `story_parked` | stories you marked done / waiting |
| `story_pinned` | sessions pinned to the top of their story |
| `terminal_zoom` | terminal text scale |
| `sidebar_zoom` | session list text scale |

Open sessions are recorded separately in `state.json` in the same directory.

## Requirements

Ubuntu 24.04 or newer (or any distro with GTK4 and VTE 3.91). GTK4's GL
renderer crashes inside Mesa on some systems, so the app forces
`GSK_RENDERER=cairo`; override it if you want to test the GL path.

## Development

```bash
make check     # syntax check plus a stress run
```

`stress_test.py` drives the risky paths — many sessions, rapid switching,
reparenting between windows, zoom churn, closes and respawns — and reports
memory growth. It has already caught one segfault that a happy-path test would
not have.

## Licence

MIT. See `LICENSE`.
