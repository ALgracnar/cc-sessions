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
- **Multiple windows**, each owning its own sessions, sharing one recovery pool.
- **Two list orders**: automatic (waiting sessions rise to the top by
  themselves) or static, where you drag rows into the order you want and
  nothing moves on its own.

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

## Settings

Stored in `~/.config/cc-sessions/config.json`:

| key | meaning |
|---|---|
| `default_folder` | where the toolbar buttons start sessions |
| `claude_command` | binary to run (default `claude`); a path with `/` is used verbatim |
| `claude_args` | flags added to every session, e.g. `--dangerously-skip-permissions` |
| `auto_sort` | `true` (default) rearranges the list as state changes; `false` keeps it static and drag-orderable |
| `manual_order` | the order you dragged rows into, used when `auto_sort` is off |
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
