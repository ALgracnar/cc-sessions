"""A single Claude Code session, living in its own VTE terminal."""

import os
import shlex
import subprocess
import sys

import gi

gi.require_version("Vte", "3.91")
gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gio, GLib, Gtk, Vte

# We launch through an interactive bash so the user's ~/.bashrc is sourced
# (PATH, nvm), then `exec` so bash REPLACES itself with claude. That matters:
# it means the pid we get back from spawn_async is claude's own pid, which is
# exactly the pid claude writes into ~/.claude/sessions/<pid>.json. So matching
# a terminal to its roster entry is a plain pid comparison, no guessing.
#
# `type -P` searches PATH only, deliberately ignoring the user's claude() shell
# function wrapper, which cannot be exec'd.
# `type -P` searches PATH only, ignoring the user's claude() shell function
# (which cannot be exec'd). A command containing a slash is used verbatim.
LAUNCH_CLAUDE = '{unset}exec "$(type -P {command})" {args}'
LAUNCH_CLAUDE_PATH = '{unset}exec {command} {args}'
# A plain terminal tab. Markers are cleared here too, so that if you later run
# `claude` by hand from this shell it starts as a proper top-level session
# rather than a child of whatever launched cc-sessions.
LAUNCH_SHELL = '{unset}exec "$SHELL"'


def marker_unset_prefix():
    """Shell prefix that clears Claude Code's own session markers.

    If cc-sessions is itself launched from inside a Claude Code session, the
    child inherits markers like CLAUDE_CODE_CHILD_SESSION and
    CLAUDE_CODE_SESSION_ID. The new session then believes it is a *child* of
    that one: it disables transcript saving and never registers in
    ~/.claude/sessions -- so it would never appear in our sidebar.

    We clear them in the shell rather than via spawn_async's `envv`, because
    that argument does not reliably replace the inherited environment (the
    child was observed keeping every marker). `unset` before `exec` is
    unambiguous. A fresh session re-derives what it needs from settings.json.
    """
    markers = sorted(key for key in os.environ if key.startswith("CLAUDE"))
    if not markers:
        return ""
    return "unset " + " ".join(markers) + "; "


# Clickable links. A bare VTE does none of this -- GNOME Terminal implements it
# on top, which is why links were dead here. Two separate mechanisms:
#   * OSC 8 hyperlinks, where the program explicitly marks text as a link
#   * a regex over the visible text, for plain URLs that were merely printed
# PCRE2 flags, since Vte.Regex takes them numerically.
PCRE2_CASELESS = 0x00000008
PCRE2_MULTILINE = 0x00000400
URL_PATTERN = (
    r"(?:https?://|ftp://|file://|www\.)"
    r"[^\s\"'<>`{}|\\^\[\]]+"
)
# Trailing punctuation is almost never part of the URL -- "see https://x.com."
_TRAILING = ".,;:!?)]}'\"…"


def _parent_pid(pid):
    try:
        with open("/proc/%d/stat" % pid, "rb") as handle:
            data = handle.read()
    except (OSError, ValueError):
        return None
    close = data.rfind(b")")          # comm can contain spaces and parens
    if close == -1:
        return None
    fields = data[close + 2:].split()
    if len(fields) < 2:
        return None
    try:
        return int(fields[1])
    except ValueError:
        return None


def _has_ancestor(pid, ancestor, max_depth=12):
    """Walk up the process tree looking for `ancestor`."""
    current = pid
    for _ in range(max_depth):
        current = _parent_pid(current)
        if current is None or current <= 1:
            return False
        if current == ancestor:
            return True
    return False


def default_session_name(cwd):
    """Name a new session after its git branch, falling back to the directory.

    The name is worth getting right: --name feeds the roster (so this app's
    sidebar), the /resume picker, the terminal title, and is persisted into the
    transcript as customTitle. An unnamed session gets a generic 'repo-4f'.
    """
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True, text=True, timeout=3,
        )
        branch = result.stdout.strip()
        if result.returncode == 0 and branch and branch != "HEAD":
            # In a worktree the directory name is usually more specific than
            # the branch (task-12345 vs worktree-task-12345).
            base = os.path.basename(cwd.rstrip(os.sep))
            if base and base not in ("", ".") and len(base) < len(branch):
                return base
            return branch
    except (OSError, subprocess.SubprocessError):
        pass
    return os.path.basename(cwd.rstrip(os.sep)) or "claude"


class TerminalSession:
    """Wraps one Vte.Terminal running one `claude` process."""

    _counter = 0

    def __init__(self, theme, cwd=None, name=None, on_exit=None, mode="claude",
                 command="claude", extra_args="", zoom=1.0, resume_id=None):
        TerminalSession._counter += 1
        self.key = "session-%d" % TerminalSession._counter
        self.cwd = cwd or os.path.expanduser("~")
        self.name = name or default_session_name(self.cwd)
        self.mode = mode            # "claude" or "shell"
        self.command = command or "claude"
        self.extra_args = extra_args or ""
        self.resume_id = resume_id       # resume this conversation, if given
        self.session_id = None           # learned from the roster once running
        self.roster_cwd = None           # Claude's real cwd, once known
        self.last_status = None
        self.pid = None
        self.exited = False
        self.on_exit = on_exit

        self.closing = False
        self.terminal = Vte.Terminal()
        self.terminal.set_hexpand(True)
        self.terminal.set_vexpand(True)
        theme.apply(self.terminal, zoom)
        self._child_exited_id = self.terminal.connect(
            "child-exited", self._on_child_exited
        )
        self._enable_links()

    # -- clickable links ---------------------------------------------------

    def _enable_links(self):
        """Ctrl+click to open URLs, and honour OSC 8 hyperlinks."""
        try:
            self.terminal.set_allow_hyperlink(True)
        except Exception:
            pass
        try:
            regex = Vte.Regex.new_for_match(
                URL_PATTERN, -1, PCRE2_MULTILINE | PCRE2_CASELESS
            )
            self._url_tag = self.terminal.match_add_regex(regex, 0)
            self.terminal.match_set_cursor_name(self._url_tag, "pointer")
        except Exception:
            self._url_tag = -1

        # CAPTURE, not the default bubble phase: VTE handles mouse input itself
        # (selection, and its own click behaviour) and claims the sequence, so a
        # bubble-phase handler never ran -- links looked clickable and did
        # nothing. Capturing lets us inspect the press first, and we claim the
        # sequence ONLY when we are actually opening a link, so ordinary
        # clicking and selecting are untouched.
        click = Gtk.GestureClick(button=1)
        click.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        click.connect("pressed", self._on_click)
        self.terminal.add_controller(click)

    def link_at(self, x, y):
        """An OSC 8 hyperlink or a matched URL under the pointer, if any."""
        try:
            uri = self.terminal.check_hyperlink_at(x, y)
            if uri:
                return uri
        except Exception:
            pass
        try:
            match, _tag = self.terminal.check_match_at(x, y)
        except Exception:
            return None
        if not match:
            return None
        match = match.rstrip(_TRAILING)
        if match.lower().startswith("www."):
            match = "https://" + match
        return match or None

    def _on_click(self, gesture, n_press, x, y):
        ctrl = bool(gesture.get_current_event_state() & Gdk.ModifierType.CONTROL_MASK)

        explicit = False
        try:
            explicit = bool(self.terminal.check_hyperlink_at(x, y))
        except Exception as exc:
            print("[cc-sessions] hyperlink check failed: %r" % exc, file=sys.stderr)

        # Ctrl+click for a plain URL; a bare click is enough for an explicit
        # OSC 8 hyperlink -- same as GNOME Terminal.
        if not (ctrl or explicit):
            return
        uri = self.link_at(x, y)
        if not uri:
            return

        # Ours now: stop VTE also starting a selection under the click.
        gesture.set_state(Gtk.EventSequenceState.CLAIMED)
        self.open_uri(uri)

    @staticmethod
    def open_uri(uri):
        """Hand a URI to the desktop. Failures are reported, never swallowed."""
        try:
            Gtk.UriLauncher.new(uri=uri).launch(None, None, None, None)
            return True
        except Exception as exc:
            print("[cc-sessions] UriLauncher failed for %s: %r" % (uri, exc),
                  file=sys.stderr)
        try:
            return Gio.AppInfo.launch_default_for_uri(uri, None)
        except Exception as exc:
            print("[cc-sessions] launch_default_for_uri failed for %s: %r"
                  % (uri, exc), file=sys.stderr)
            return False

    # -- lifecycle ---------------------------------------------------------

    def spawn(self):
        if self.mode == "shell":
            command = LAUNCH_SHELL.format(unset=marker_unset_prefix())
        else:
            # extra_args is passed through unquoted on purpose: it is the
            # user's own configured flag string (e.g.
            # "--dangerously-skip-permissions"), and quoting it would turn the
            # whole thing into a single meaningless argument.
            pieces = ["-n", shlex.quote(self.name)]
            if self.resume_id:
                # Pick the conversation back up rather than starting fresh.
                pieces += ["-r", shlex.quote(self.resume_id)]
            pieces.append(self.extra_args.strip())
            args = " ".join(part for part in pieces if part)
            template = LAUNCH_CLAUDE_PATH if "/" in self.command else LAUNCH_CLAUDE
            command = template.format(
                unset=marker_unset_prefix(),
                command=shlex.quote(self.command) if "/" in self.command else self.command,
                args=args,
            )
        argv = ["bash", "-i", "-c", command]

        # NOTE on the argument list: PyGObject's docstring for spawn_async
        # omits `child_setup_data`, but the real binding expects it between
        # `child_setup` and `timeout`. Getting this wrong yields confusing
        # errors ("Argument 9 does not allow None", "Must be number, not
        # Cancellable") that point at the wrong parameter. Order below is
        # verified working -- do not "tidy" it against the docstring.
        self.terminal.spawn_async(
            Vte.PtyFlags.DEFAULT,       # pty_flags
            self.cwd,                   # working_directory
            argv,                       # argv
            None,                       # envv -- unreliable; see marker_unset_prefix()
            GLib.SpawnFlags.DEFAULT,    # spawn_flags
            None,                       # child_setup
            None,                       # child_setup_data
            -1,                         # timeout (must be a number)
            None,                       # cancellable
            self._on_spawned,           # callback
            None,                       # user_data
        )

    def _on_spawned(self, terminal, pid, error, *user_data):
        if error is not None:
            self.exited = True
            self.feed_notice("failed to start claude: %s" % error.message)
            return
        self.pid = pid

    def detach(self):
        """Disconnect from the terminal before the session is discarded.

        Without this, closing a session raced its own teardown: we dropped the
        last Python reference while VTE was still finishing with the pty, then
        "child-exited" fired into a half-destroyed object and segfaulted the
        whole app -- taking every other session with it. Caught by
        stress_test.py, which passed its 60 steps and then died with SIGSEGV on
        shutdown.
        """
        self.closing = True
        if self._child_exited_id is not None:
            try:
                self.terminal.disconnect(self._child_exited_id)
            except Exception:
                pass
            self._child_exited_id = None

    def _on_child_exited(self, terminal, status):
        if self.closing:
            return
        self.exited = True
        self.feed_notice(
            "session ended (exit %d) — close this tab or start a new session"
            % status
        )
        if self.on_exit:
            self.on_exit(self)

    def feed_notice(self, text):
        """Write a dim italic line into the terminal itself.

        Guarded: feeding a terminal that is not realized (not currently in any
        window -- which happens while a session is being reparented between
        windows) can crash hard rather than raise.
        """
        try:
            if not self.terminal.get_realized():
                return
            self.terminal.feed(
                ("\r\n\x1b[2;3m[cc-sessions] %s\x1b[0m\r\n" % text).encode()
            )
        except Exception:
            pass

    def current_cwd(self):
        """Where this terminal actually is right now.

        Read from /proc rather than remembered from launch, so `cd` shows up in
        the sidebar and a restored terminal reopens where you left it. Does not
        need VTE shell integration (OSC 7), which would mean editing .bashrc.
        """
        if self.pid:
            try:
                return os.readlink("/proc/%d/cwd" % self.pid)
            except OSError:
                pass
        return self.roster_cwd or self.cwd

    # -- roster matching ---------------------------------------------------

    def matches(self, entry):
        """Is this roster entry running inside THIS terminal?

        Usually the pids are identical, because a Claude session is exec'd in
        place. But when you start `claude` by hand inside a plain shell tab it
        is a CHILD of our bash, with a different pid -- so we also walk the
        parent chain. Without this, a session you started yourself in one of
        our own terminals would be listed as "external" and be unclickable.
        """
        if self.pid is None or entry.pid is None:
            return False
        if entry.pid == self.pid:
            return True
        return _has_ancestor(entry.pid, self.pid)
