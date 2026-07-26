#!/usr/bin/env python3
"""cc-sessions — a session-aware terminal for Claude Code.

Your GNOME Terminal, plus a sidebar that tells you which session is actually
blocked on you (`waiting`) rather than merely quiet (`idle`).

  cc-sessions                 open a window
  cc-sessions --new-instance  run a separate process (see multi-window note)
"""

import json
import os
import signal
import sys

# Force GTK4's software renderer BEFORE gi/Gtk is imported.
#
# GTK4 defaults to a GL renderer, which segfaulted inside Mesa
# (gsk_renderer_render -> libgtk-4 GL backend -> libgallium, SIGSEGV) taking
# the whole app down with no Python traceback. A terminal does not need GPU
# compositing, and cairo is rock solid. Override by setting GSK_RENDERER
# yourself if you ever want to test the GL path again.
os.environ.setdefault("GSK_RENDERER", "cairo")

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Vte", "3.91")

from gi.repository import Gdk, Gio, GLib, GObject, Gtk

import roster
import state
from session import TerminalSession, default_session_name
from theme import TerminalTheme

APP_ID = "eu.nevron.ccsessions"

# Colours by meaning, not by palette index. Mostly the user's own ANSI colours
# so the sidebar speaks the same visual language as the terminal below it --
# except "waiting", which needs to be unmistakable next to "finished". The ANSI
# set has no orange, so that one is a literal: Darcula's own orange, which sits
# correctly in this theme.
WAITING_ORANGE = "#CC7832"

PALETTE_SLOT = {
    "busy": 12,      # bright blue   — working
    "shell": 14,     # bright cyan   — running a command
    "idle": 10,      # bright green  — done, nothing pending
    "finished": 11,  # bright yellow — finished while you were away
    "dim": 8,        # bright black  — dormant / unknown
}
LITERAL_COLOUR = {"waiting": WAITING_ORANGE}
COLOUR_NAMES = list(PALETTE_SLOT) + list(LITERAL_COLOUR)
LIVE_STATUSES = ("waiting", "busy", "shell", "idle")


def colour_for(theme, name):
    if name in LITERAL_COLOUR:
        return LITERAL_COLOUR[name]
    return theme.palette_css(PALETTE_SLOT.get(name, PALETTE_SLOT["dim"]))


CONFIG_DIR = os.path.expanduser("~/.config/cc-sessions")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")


def load_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_config(config):
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(CONFIG_PATH, "w", encoding="utf-8") as handle:
            json.dump(config, handle, indent=2)
    except OSError:
        pass


def short_path(path):
    """Abbreviate a directory for display, same shape as roster.Entry."""
    home = os.path.expanduser("~")
    if path.startswith(home):
        path = "~" + path[len(home):]
    parts = path.split(os.sep)
    if len(parts) > 3:
        return os.sep.join(["…"] + parts[-2:])
    return path


def item_key(kind, obj, owned=None):
    """A stable id for a sidebar row, used by manual ordering.

    Deliberately keyed on the Claude session id when there is one, so a row
    keeps its place across resume: the dormant record and the live session it
    becomes share an id, where our internal per-process key would not.
    """
    if kind == "record":
        return obj.get("session_id") or ("name:" + (obj.get("name") or ""))
    sess = owned if owned is not None else obj
    return getattr(sess, "session_id", None) or ("local:" + sess.key)


def load_css(provider, css):
    """Gtk.CssProvider gained/lost methods across 4.x; try them in order."""
    if hasattr(provider, "load_from_string"):
        provider.load_from_string(css)
        return
    try:
        provider.load_from_data(css, -1)
    except TypeError:
        provider.load_from_data(css.encode())


class NewSessionDialog(Gtk.Window):
    """Choose what to start, where, and what to call it."""

    def __init__(self, parent, cwd, on_accept):
        super().__init__(title="New session", transient_for=parent, modal=True)
        self.on_accept = on_accept
        self.cwd = cwd
        self._name_edited = False
        self.set_default_size(520, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for setter in ("set_margin_top", "set_margin_bottom",
                       "set_margin_start", "set_margin_end"):
            getattr(box, setter)(16)

        # -- what to start -------------------------------------------------
        modes = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        modes.add_css_class("linked")
        self.btn_claude = Gtk.ToggleButton(label="Claude session")
        self.btn_shell = Gtk.ToggleButton(label="Terminal only")
        self.btn_shell.set_group(self.btn_claude)
        self.btn_claude.set_active(True)
        self.btn_claude.set_hexpand(True)
        self.btn_shell.set_hexpand(True)
        self.btn_shell.connect("toggled", self._on_mode_changed)
        modes.append(self.btn_claude)
        modes.append(self.btn_shell)
        box.append(modes)

        # -- where ---------------------------------------------------------
        folder_label = Gtk.Label(label="Folder", xalign=0)
        folder_label.add_css_class("dim-label")
        box.append(folder_label)

        self.folder = FolderRow(self, cwd, on_change=self._on_folder_changed)
        box.append(self.folder)

        # -- name ----------------------------------------------------------
        self.name_label = Gtk.Label(xalign=0)
        self.name_label.add_css_class("dim-label")
        box.append(self.name_label)

        self.entry = Gtk.Entry()
        self.entry.set_text(default_session_name(cwd))
        self.entry.connect("activate", lambda *_: self._accept())
        self.entry.connect("changed", self._on_name_typed)
        box.append(self.entry)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        self.start = Gtk.Button(label="Start")
        self.start.add_css_class("suggested-action")
        self.start.connect("clicked", lambda *_: self._accept())
        buttons.append(cancel)
        buttons.append(self.start)
        box.append(buttons)

        self.set_child(box)
        self._on_mode_changed()

    # -- reactions ---------------------------------------------------------

    @property
    def mode(self):
        return "shell" if self.btn_shell.get_active() else "claude"

    def _on_mode_changed(self, *_):
        if self.mode == "shell":
            self.name_label.set_text("Label  (for the sidebar only)")
        else:
            self.name_label.set_text(
                "Session name  (sidebar, /resume picker, terminal title)"
            )

    def _on_name_typed(self, *_):
        self._name_edited = True

    def _on_folder_changed(self, path):
        # Follow the folder with the default name until the user overrides it.
        if self._name_edited or not os.path.isdir(path):
            return
        self.entry.set_text(default_session_name(path))
        self._name_edited = False

    def _accept(self):
        cwd = self.folder.path or self.cwd
        if not os.path.isdir(cwd):
            cwd = self.cwd
        name = self.entry.get_text().strip() or default_session_name(cwd)
        mode = self.mode
        self.close()
        self.on_accept(name, cwd, mode)


class FolderRow(Gtk.Box):
    """Folder entry + Home shortcut + Browse, reused by both dialogs."""

    def __init__(self, parent, path, on_change=None):
        super().__init__(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self.parent_window = parent
        self.on_change = on_change

        self.entry = Gtk.Entry()
        self.entry.set_text(path)
        self.entry.set_hexpand(True)
        if on_change:
            self.entry.connect("changed", lambda *_: on_change(self.path))
        self.append(self.entry)

        # A fresh terminal opens in $HOME; make that one click rather than
        # something you have to navigate to.
        home = Gtk.Button(label="Home")
        home.set_tooltip_text(os.path.expanduser("~"))
        home.connect("clicked", lambda *_: self.set_path(os.path.expanduser("~")))
        self.append(home)

        browse = Gtk.Button(label="Browse…")
        browse.connect("clicked", self._browse)
        self.append(browse)

    @property
    def path(self):
        return self.entry.get_text().strip()

    def set_path(self, path):
        self.entry.set_text(path)

    def _browse(self, *_):
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose a folder")
        start = self.path or os.path.expanduser("~")
        if os.path.isdir(start):
            dialog.set_initial_folder(Gio.File.new_for_path(start))
        dialog.select_folder(self.parent_window, None, self._browsed)

    def _browsed(self, dialog, result):
        try:
            folder = dialog.select_folder_finish(result)
        except GLib.Error:
            return  # cancelled
        if folder and folder.get_path():
            self.set_path(folder.get_path())


class SettingsDialog(Gtk.Window):
    """Where new sessions start from."""

    def __init__(self, parent, app):
        super().__init__(title="Settings", transient_for=parent, modal=True)
        self.app = app
        self.set_default_size(560, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for setter in ("set_margin_top", "set_margin_bottom",
                       "set_margin_start", "set_margin_end"):
            getattr(box, setter)(16)

        label = Gtk.Label(label="Default folder for new sessions", xalign=0)
        box.append(label)

        hint = Gtk.Label(xalign=0)
        hint.set_markup(
            "<span alpha='55%'>Both the Claude and terminal buttons start "
            "here.</span>"
        )
        box.append(hint)

        self.folder = FolderRow(self, app.default_folder())
        box.append(self.folder)

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        sort_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        sort_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        sort_text.set_hexpand(True)
        sort_label = Gtk.Label(label="Rearrange the list automatically", xalign=0)
        sort_text.append(sort_label)
        sort_hint = Gtk.Label(xalign=0)
        sort_hint.set_markup(
            "<span alpha='55%'>On: sessions waiting on you move to the top by "
            "themselves.  Off: the order stays put and you drag rows to "
            "arrange them.</span>"
        )
        sort_hint.set_wrap(True)
        sort_text.append(sort_hint)
        sort_row.append(sort_text)

        self.sort_switch = Gtk.Switch()
        self.sort_switch.set_valign(Gtk.Align.CENTER)
        self.sort_switch.set_active(app.auto_sort())
        # Live, so the effect is visible while the dialog is still open.
        self.sort_switch.connect(
            "notify::active",
            lambda sw, _p: app.set_auto_sort(sw.get_active()),
        )
        sort_row.append(self.sort_switch)
        box.append(sort_row)

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        zoom_label = Gtk.Label(label="Text size", xalign=0)
        box.append(zoom_label)

        # Two independent sizes: the terminal font and the session list. Both
        # preview live, because choosing a text size blind is pointless.
        grid = Gtk.Grid(column_spacing=8, row_spacing=6)

        grid.attach(Gtk.Label(label="Terminal", xalign=0), 0, 0, 1, 1)
        self.term_spin = Gtk.SpinButton.new_with_range(50, 300, 5)
        self.term_spin.set_value(round(app.terminal_zoom() * 100))
        self.term_spin.set_hexpand(True)
        self.term_spin.connect(
            "value-changed",
            lambda spin: app.set_terminal_zoom(spin.get_value() / 100.0, persist=False),
        )
        grid.attach(self.term_spin, 1, 0, 1, 1)
        grid.attach(Gtk.Label(label="%"), 2, 0, 1, 1)

        grid.attach(Gtk.Label(label="Session list", xalign=0), 0, 1, 1, 1)
        self.side_spin = Gtk.SpinButton.new_with_range(50, 300, 5)
        self.side_spin.set_value(round(app.sidebar_zoom() * 100))
        self.side_spin.set_hexpand(True)
        self.side_spin.connect(
            "value-changed",
            lambda spin: app.set_sidebar_zoom(spin.get_value() / 100.0, persist=False),
        )
        grid.attach(self.side_spin, 1, 1, 1, 1)
        grid.attach(Gtk.Label(label="%"), 2, 1, 1, 1)

        reset = Gtk.Button(label="Reset both")
        reset.connect("clicked", lambda *_: (self.term_spin.set_value(100),
                                             self.side_spin.set_value(100)))
        grid.attach(reset, 3, 0, 1, 2)
        box.append(grid)

        zoom_hint = Gtk.Label(xalign=0)
        zoom_hint.set_markup(
            "<span alpha='55%'>Ctrl +  /  Ctrl −  /  Ctrl 0 change the "
            "terminal only.</span>"
        )
        zoom_hint.set_wrap(True)
        box.append(zoom_hint)

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        cmd_label = Gtk.Label(label="Claude command", xalign=0)
        box.append(cmd_label)
        self.command_entry = Gtk.Entry()
        self.command_entry.set_text(app.claude_command())
        self.command_entry.set_placeholder_text("claude")
        box.append(self.command_entry)

        args_label = Gtk.Label(label="Extra arguments", xalign=0)
        box.append(args_label)
        self.args_entry = Gtk.Entry()
        self.args_entry.set_text(app.claude_args())
        self.args_entry.set_placeholder_text("--dangerously-skip-permissions")
        box.append(self.args_entry)

        args_hint = Gtk.Label(xalign=0)
        args_hint.set_markup(
            "<span alpha='55%'>Added to every Claude session. "
            "The session name is passed automatically with -n.</span>"
        )
        args_hint.set_wrap(True)
        box.append(args_hint)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        save = Gtk.Button(label="Save")
        save.add_css_class("suggested-action")
        save.connect("clicked", lambda *_: self._save())
        buttons.append(cancel)
        buttons.append(save)
        box.append(buttons)

        self.set_child(box)

    def _save(self):
        path = self.folder.path
        if os.path.isdir(path):
            self.app.set_default_folder(path)
        self.app.set_claude_command(self.command_entry.get_text().strip())
        self.app.set_claude_args(self.args_entry.get_text().strip())
        self.app.set_terminal_zoom(self.term_spin.get_value() / 100.0)
        self.app.set_sidebar_zoom(self.side_spin.get_value() / 100.0)
        self._saved = True
        self.close()

    def do_close_request(self):
        # Cancelling must undo the live previews, not leave them applied.
        if not getattr(self, "_saved", False):
            self.app.set_terminal_zoom(self.app.terminal_zoom(), persist=False)
            self.app.set_sidebar_zoom(self.app.sidebar_zoom(), persist=False)
        return False


class SessionWindow(Gtk.ApplicationWindow):
    """One window: sidebar of live sessions on the left, terminal on the right."""

    def __init__(self, app, restorable=None):
        super().__init__(application=app, title="Claude sessions")
        self.app = app
        self.set_default_size(1180, 720)

        self._rows = []
        self._last_signature = None
        if restorable:
            self.app.add_pending(restorable)

        # Top-level vertical box. v2's render revealer slots in above the
        # terminal area without restructuring anything.
        self.outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_position(250)
        self.paned.set_vexpand(True)
        self.paned.set_start_child(self._build_sidebar())
        self.paned.set_end_child(self._build_terminal_area())
        self.paned.set_resize_start_child(False)
        self.paned.set_shrink_start_child(False)

        self.outer.append(self.paned)
        self.set_child(self.outer)
        self.add_css_class("ccs-window")

        self.refresh()

    # -- construction ------------------------------------------------------

    def _build_sidebar(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        box.add_css_class("ccs-sidebar")

        # Toolbar at the TOP, away from the terminal's input line -- down at
        # the bottom it sat right where you click to type, and was easy to hit
        # by accident.
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        bar.add_css_class("ccs-toolbar")

        header = Gtk.Label(label="SESSIONS", xalign=0)
        header.add_css_class("ccs-header")
        header.set_hexpand(True)
        bar.append(header)

        for icon, tooltip, handler in (
            ("emblem-system-symbolic", "Settings",
             lambda *_: SettingsDialog(self, self.app).present()),
            ("utilities-terminal-symbolic", "New terminal",
             lambda *_: self.quick_new("shell")),
            ("list-add-symbolic", "New Claude session (Ctrl+Shift+T for options)",
             lambda *_: self.quick_new("claude")),
        ):
            button = Gtk.Button.new_from_icon_name(icon)
            button.set_tooltip_text(tooltip)
            button.add_css_class("ccs-toolbtn")
            button.connect("clicked", handler)
            bar.append(button)

        box.append(bar)

        self.listbox = Gtk.ListBox()
        self.listbox.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.listbox.add_css_class("ccs-list")
        self.listbox.connect("row-activated", self._on_row_activated)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(self.listbox)
        box.append(scroller)

        return box

    def _build_terminal_area(self):
        self.stack = Gtk.Stack()
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        # 4px gap so terminal text does not sit flush against the divider.
        self.stack.set_margin_start(4)

        self.placeholder = Gtk.Label()
        self.placeholder.set_markup(
            "<span alpha='55%'>no session selected\n\n"
            "press <b>+ new session</b>, or pick one on the left</span>"
        )
        self.placeholder.set_justify(Gtk.Justification.CENTER)
        self.stack.add_named(self.placeholder, "__placeholder__")

        return self.stack

    # -- sidebar -----------------------------------------------------------

    def _make_row(self, entry, owned, attention=None):
        row = Gtk.ListBoxRow()
        row.entry = entry
        row.session = owned

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("ccs-row")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        # Only a genuinely blocked session gets the loud treatment. A session
        # that merely finished a turn is marked, not shouted about.
        colour = entry.status if entry.status in LIVE_STATUSES else "dim"
        if attention == "blocked":
            colour = "waiting"      # orange: blocked on you
        elif attention == "finished":
            colour = "finished"     # yellow: done, but you were not looking

        dot = Gtk.Label(label="●" if attention else "■")
        dot.add_css_class("ccs-dot")
        dot.add_css_class("ccs-dot-%s" % colour)
        top.append(dot)

        name = Gtk.Label(label=entry.name, xalign=0)
        name.set_hexpand(True)
        name.set_ellipsize(3)  # Pango.EllipsizeMode.END
        name.add_css_class("ccs-name")
        if attention == "blocked":
            name.add_css_class("ccs-attention")
        if not owned:
            name.add_css_class("ccs-external")
        top.append(name)

        age = Gtk.Label(label=entry.describe_age(), xalign=1)
        age.add_css_class("ccs-age")
        top.append(age)

        # Only sessions we own can be closed -- we have no handle on anything
        # started outside the app.
        if owned is not None:
            top.append(self._close_button(owned))

        box.append(top)

        # Say the one thing that matters, without the internal status name
        # alongside it. `waitingFor` is already readable on its own ("input
        # needed", "dialog open", "sandbox request").
        if entry.status == "waiting" or attention == "blocked":
            detail = entry.waiting_for or "input needed"
        elif attention == "finished":
            detail = "finished"
        else:
            detail = entry.status_label
        sub = Gtk.Label(label=detail, xalign=0)
        sub.set_ellipsize(3)
        sub.add_css_class("ccs-sub")
        sub.add_css_class("ccs-sub-%s" % colour)
        box.append(sub)

        # The roster records where Claude started; /proc knows where it is.
        where = owned.current_cwd() if owned is not None else entry.cwd
        path = Gtk.Label(label=short_path(where), xalign=0)
        path.set_ellipsize(3)
        path.add_css_class("ccs-path")
        box.append(path)

        row.set_child(box)
        return row

    def _update_title(self):
        """Just the session you are looking at -- nothing appended.

        The blocked count used to be tacked on here, but the sidebar already
        shows that in orange and the title is the one place a plain name is
        useful.
        """
        current = self.stack.get_visible_child()
        for sess in self.app.sessions:
            if sess.terminal is current:
                self.set_title(sess.name)
                return
        self.set_title("Claude sessions")

    def _close_button(self, sess):
        button = Gtk.Button(label="×")
        button.add_css_class("ccs-close")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text("Close this session (Ctrl+Shift+W)")
        button.connect("clicked", lambda *_: self.app.close_session(sess))
        return button

    @property
    def restorable(self):
        """Restorable sessions are a SHARED pool, shown in every window.

        They were per-window at first, which stranded them: close the window a
        session belonged to and nothing could offer it back. Now any window can
        resume any of them, and the session lands in the window you clicked
        from -- which is also how you deal them back out across two windows.
        """
        return self.app.pending

    def owns(self, sess):
        """A live session belongs to exactly one window: the one holding it.

        A terminal widget can only have one parent, so the stack it sits in IS
        the window that owns it. Asked directly rather than cached, so it can
        never go stale after a session is moved between windows.
        """
        return sess.terminal.get_parent() is self.stack

    @staticmethod
    def unassigned(sess):
        """Alive but held by no window. Should not happen for long.

        Leaving a live VTE parentless SEGFAULTS -- tried it, it crashed. So
        closing a window hands its sessions to another window immediately
        rather than letting them float, and this exists only as a safety net
        for the brief window in between.
        """
        return not sess.exited and sess.terminal.get_parent() is None

    def adopt_orphans(self):
        """Take in any session left without a window."""
        for sess in self.app.sessions:
            if self.unassigned(sess):
                self.stack.add_named(sess.terminal, sess.key)

    def do_close_request(self):
        """Hand our live sessions to another window before closing.

        A GTK window destroys its children, so they must be moved, not just
        detached -- and they cannot be left parentless (see unassigned()).
        """
        others = [w for w in self.app.session_windows() if w is not self]
        if others:
            target = others[0]
            for sess in list(self.app.sessions):
                if sess.exited or not self.owns(sess):
                    continue
                self.stack.remove(sess.terminal)
                target.stack.add_named(sess.terminal, sess.key)
            target._last_signature = None
            target.refresh()
        self.app.persist()
        return False

    def _make_restore_row(self, record):
        """A greyed row for a session from a previous run."""
        row = Gtk.ListBoxRow()
        row.entry = None
        row.session = None
        row.record = record

        is_shell = record.get("mode") == "shell"

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("ccs-row")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        dot = Gtk.Label(label="○")
        dot.add_css_class("ccs-dot")
        # Always dim: colour means LIVE. A dormant row wearing the shell
        # colour read as an active tab, which is exactly the wrong signal.
        # The word "terminal" on the line below carries the distinction.
        dot.add_css_class("ccs-dot-dim")
        top.append(dot)

        name = Gtk.Label(label=record.get("name") or "(unnamed)", xalign=0)
        name.set_hexpand(True)
        name.set_ellipsize(3)
        name.add_css_class("ccs-name")
        name.add_css_class("ccs-external")   # dimmed
        top.append(name)

        dismiss = Gtk.Button(label="×")
        dismiss.add_css_class("ccs-close")
        dismiss.set_valign(Gtk.Align.CENTER)
        dismiss.set_tooltip_text("Forget this session")
        dismiss.connect("clicked", lambda *_: self.dismiss_restorable(record))
        top.append(dismiss)
        box.append(top)

        # Type first: the sidebar truncates, so "terminal"/"session" must
        # survive being cut off. "from last session" did not distinguish them.
        if is_shell:
            detail = "terminal  ·  opens here"
        elif state.transcript_exists(record.get("cwd", ""), record.get("session_id")):
            detail = "session  ·  resumes chat"
        else:
            detail = "session  ·  starts fresh"
        row.set_tooltip_text("%s\n%s\n%s" % (
            record.get("name") or "(unnamed)",
            "Terminal from last run" if is_shell else "Claude session from last run",
            record.get("cwd", "")))
        sub = Gtk.Label(label=detail, xalign=0)
        sub.set_ellipsize(3)
        sub.add_css_class("ccs-sub")
        sub.add_css_class("ccs-sub-dim")
        box.append(sub)

        path = Gtk.Label(label=short_path(record.get("cwd", "")), xalign=0)
        path.set_ellipsize(3)
        path.add_css_class("ccs-path")
        box.append(path)

        row.set_child(box)
        return row

    def dismiss_restorable(self, record):
        self.app.drop_pending(record)

    def resume_restorable(self, record):
        """Pick a previous session back up, in this window."""
        cwd = record.get("cwd") or os.path.expanduser("~")
        if not os.path.isdir(cwd):
            cwd = os.path.expanduser("~")
        mode = record.get("mode") or "claude"
        session_id = record.get("session_id")
        resume_id = session_id if (
            mode == "claude" and state.transcript_exists(cwd, session_id)
        ) else None

        sess = self.app.start_session(
            name=self.app.unique_name(record.get("name") or default_session_name(cwd)),
            cwd=cwd, mode=mode, resume_id=resume_id,
        )
        self.show_session(sess)
        self.app.drop_pending(record)

    def _make_local_row(self, sess):
        """A row for one of our terminals that has no roster entry."""
        row = Gtk.ListBoxRow()
        row.entry = None
        row.session = sess

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        box.add_css_class("ccs-row")

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        colour = "shell" if sess.mode == "shell" else "dim"
        dot = Gtk.Label(label="■")
        dot.add_css_class("ccs-dot")
        dot.add_css_class("ccs-dot-%s" % colour)
        top.append(dot)

        name = Gtk.Label(label=sess.name, xalign=0)
        name.set_hexpand(True)
        name.set_ellipsize(3)
        name.add_css_class("ccs-name")
        top.append(name)
        top.append(self._close_button(sess))
        box.append(top)

        if sess.exited:
            detail = "ended"
        elif sess.mode == "shell":
            detail = "terminal"
        else:
            detail = "starting…"
        sub = Gtk.Label(label=detail, xalign=0)
        sub.set_ellipsize(3)
        sub.add_css_class("ccs-sub")
        sub.add_css_class("ccs-sub-%s" % colour)
        box.append(sub)

        path = Gtk.Label(label=short_path(sess.current_cwd()), xalign=0)
        path.set_ellipsize(3)
        path.add_css_class("ccs-path")
        box.append(path)

        row.set_child(box)
        return row

    # -- ordering ----------------------------------------------------------

    def _auto_sorted(self, items):
        """The original behaviour: the list rearranges itself as state changes.

        Live sessions first with anything blocked on you at the top, then
        dormant rows, then sessions still starting up.
        """
        group = {"entry": 0, "record": 1, "local": 2}
        rank = {"blocked": 0, "finished": 1}

        def key(item):
            kind, obj, _owned = item
            if kind == "entry":
                return (0, rank.get(self.app.attention_kind(obj), 2),
                        obj.rank, obj.name.lower())
            name = (obj.get("name") if kind == "record" else obj.name) or ""
            return (group[kind], 0, 0, name.lower())

        return sorted(items, key=key)

    def _manually_sorted(self, items):
        """Static order you control by dragging; new rows land at the bottom.

        Nothing here reacts to status changes -- that is the point. A row only
        moves when you move it.
        """
        order = self.app.manual_order()
        position = {k: i for i, k in enumerate(order)}
        # Unknown rows keep the auto order among themselves and go last, so a
        # newly started session appears predictably at the bottom.
        fallback = {item_key(*i): n for n, i in enumerate(self._auto_sorted(items))}
        return sorted(
            items,
            key=lambda i: (position.get(item_key(*i), len(order) + fallback[item_key(*i)]),),
        )

    def _attach_reorder(self, row):
        """Make a row draggable onto its siblings (manual mode only)."""
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        source.connect(
            "prepare",
            lambda _s, _x, _y, key=row.item_key: Gdk.ContentProvider.new_for_value(key),
        )
        row.add_controller(source)

        target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)

        def dropped(_t, value, _x, _y, key=row.item_key):
            if not isinstance(value, str) or not value or value == key:
                return False
            self.app.move_in_order(value, key,
                                   [r.item_key for r in self._rows])
            return True

        target.connect("drop", dropped)
        row.add_controller(target)

    def refresh(self, entries=None):
        if entries is None:
            entries = roster.read()
            self.app.update_attention(entries)

        # Only sessions this app owns AND this window holds. Anything
        # started elsewhere cannot be shown or closed from here, and a
        # session living in another window belongs on that window's list.
        self.adopt_orphans()
        entries = [
            e for e in entries
            if (lambda s: s is not None and self.owns(s))(self.app.session_for_entry(e))
        ]

        self._update_title()

        # Sessions we own that the roster does not describe: plain shell tabs
        # (which never register), and Claude sessions in the first seconds
        # before they do. Without these the sidebar would be blank right after
        # you start something, which reads as a failure.
        # Exited sessions stay listed (as "ended") rather than vanishing, so
        # the last output is still readable and you can close them deliberately.
        local_only = [
            sess for sess in self.app.sessions
            if self.owns(sess) and not any(sess.matches(e) for e in entries)
        ]

        # One list, ordered once -- rather than three groups each with their
        # own rules, which is what made the order hard to reason about.
        items = [("entry", e, self.app.session_for_entry(e)) for e in entries]
        items += [("record", r, None) for r in self.restorable]
        items += [("local", s, s) for s in local_only]

        if self.app.auto_sort():
            items = self._auto_sorted(items)
        else:
            items = self._manually_sorted(items)

        signature = (tuple(item_key(k, o, w) for k, o, w in items),
                     roster.signature(entries),
                     tuple(self.app.attention_kind(e) for e in entries),
                     tuple((s.key, s.mode, s.pid is None, s.exited, s.current_cwd())
                           for s in local_only),
                     tuple((s.key, s.current_cwd()) for s in self.app.sessions
                           if self.owns(s)),
                     tuple(r.get("session_id") or r.get("name") for r in self.restorable))
        if signature == self._last_signature:
            return
        self._last_signature = signature

        selected = self.stack.get_visible_child()

        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)
        self._rows = []

        manual = not self.app.auto_sort()
        for kind, obj, owned in items:
            if kind == "entry":
                row = self._make_row(obj, owned, self.app.attention_kind(obj))
            elif kind == "record":
                row = self._make_restore_row(obj)
            else:
                row = self._make_local_row(obj)
            row.item_key = item_key(kind, obj, owned)
            if manual:
                self._attach_reorder(row)
            self.listbox.append(row)
            self._rows.append(row)
            if owned is not None and owned.terminal is selected:
                self.listbox.select_row(row)

    # -- actions -----------------------------------------------------------

    def _on_row_activated(self, listbox, row):
        if getattr(row, "record", None) is not None:
            self.resume_restorable(row.record)
            return
        if row.session is not None:
            self.show_session(row.session)
        else:
            # Started outside this app (or by another instance): we do not own
            # its pty, so there is nothing to show. Say so rather than no-op.
            self._flash(
                "“%s” was started outside this app — its terminal lives "
                "elsewhere, so it cannot be shown here." % row.entry.name
            )

    def _flash(self, message):
        dialog = Gtk.AlertDialog()
        dialog.set_message("External session")
        dialog.set_detail(message)
        dialog.show(self)

    def quick_new(self, mode):
        """One click, no dialog: start in the configured default folder."""
        cwd = self.app.default_folder()
        self._start_session(self.app.unique_name(default_session_name(cwd)), cwd, mode)

    def prompt_new_session(self):
        """The full dialog, for when you want to pick name/folder/mode."""
        cwd = self.app.default_folder()
        current = self.stack.get_visible_child()
        for sess in self.app.sessions:
            if sess.terminal is current:
                cwd = sess.cwd
                break
        NewSessionDialog(self, cwd, self._start_session).present()

    def _start_session(self, name, cwd, mode):
        sess = self.app.start_session(name=name, cwd=cwd, mode=mode)
        self.show_session(sess)
        self._last_signature = None
        self.refresh()

    def show_session(self, sess):
        """Display a session here, taking it from another window if needed.

        All terminals belong to one process, so any window can show any
        session -- we just reparent the widget.
        """
        parent = sess.terminal.get_parent()
        if parent is not None and parent is not self.stack:
            parent.remove(sess.terminal)
            parent = None
        if parent is None:
            self.stack.add_named(sess.terminal, sess.key)
        self.stack.set_visible_child(sess.terminal)
        sess.terminal.grab_focus()
        self.app.clear_attention_for(sess)
        self._last_signature = None
        self._update_title()


class CCSessions(Gtk.Application):
    def __init__(self, non_unique=False):
        flags = Gio.ApplicationFlags.DEFAULT_FLAGS
        super().__init__(
            application_id=None if non_unique else APP_ID,
            flags=flags | (Gio.ApplicationFlags.NON_UNIQUE if non_unique else 0),
        )
        self.theme = TerminalTheme.load()
        self.sessions = []
        self._provider = None
        self._retired = []   # closed sessions, kept alive briefly
        self._persist_tick = 0
        self.pending = []    # restorable sessions, shared across windows

        # "Needs you" tracking. Claude reports `waiting` only while a dialog is
        # literally on screen; a session that finished its turn reports `idle`,
        # even though that is equally your move. So we track something Claude
        # cannot know: whether you were LOOKING when it went quiet. A session
        # that became idle/waiting while you were elsewhere is flagged until
        # you actually visit it.
        self.attention = {}       # key -> bool
        self._prev_status = {}    # key -> last seen status
        self.config = load_config()

    # -- lifecycle ---------------------------------------------------------

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self._apply_css()

        for name, callback, accels in (
            ("new-window", lambda *_: self.new_window(), ["<Control><Shift>n"]),
            ("new-session", lambda *_: self._new_session_here(), ["<Control><Shift>t"]),
            ("close-session", lambda *_: self.close_current(), ["<Control><Shift>w"]),
            ("zoom-in", lambda *_: self.adjust_zoom(0.1),
             ["<Control>plus", "<Control>equal", "<Control>KP_Add"]),
            ("zoom-out", lambda *_: self.adjust_zoom(-0.1),
             ["<Control>minus", "<Control>KP_Subtract"]),
            ("zoom-reset", lambda *_: self.reset_zoom(), ["<Control>0"]),
        ):
            action = Gio.SimpleAction.new(name, None)
            action.connect("activate", callback)
            self.add_action(action)
            self.set_accels_for_action("app.%s" % name, accels)

        GLib.timeout_add(1000, self._tick)

    def do_activate(self):
        if not self.session_windows():
            self.restore_windows()
        else:
            self.new_window()

    def new_window(self, restorable=None):
        window = SessionWindow(self, restorable=restorable)
        window.present()
        self.persist()
        return window

    # -- persistence -------------------------------------------------------

    def session_windows(self):
        return [w for w in self.get_windows() if isinstance(w, SessionWindow)]

    def add_pending(self, records):
        for record in records:
            if record not in self.pending:
                self.pending.append(record)

    def drop_pending(self, record):
        if record in self.pending:
            self.pending.remove(record)
        for window in self.session_windows():
            window._last_signature = None
            window.refresh()
        self.persist()

    def persist(self):
        """Snapshot every window's sessions. Called on every change."""
        windows = []
        for window in self.session_windows():
            windows.append([
                state.record_for(sess)
                for sess in self.sessions
                if not sess.exited and window.owns(sess)
            ])
        if not windows:
            return

        # Rows offered but not yet resumed or declined go back into the slot
        # they came from, so window grouping survives another restart.
        for record in self.pending:
            slot = record.get("window", 0)
            while len(windows) <= slot:
                windows.append([])
            windows[slot].append(record)
        state.save(windows)

    def restore_windows(self):
        """Recreate last run's windows; their sessions become the shared pool."""
        saved = [records for records in state.load() if records]
        if not saved:
            self.new_window()
            return
        for slot, records in enumerate(saved):
            for record in records:
                record.setdefault("window", slot)
        for records in saved:
            self.new_window(restorable=records)

    def _new_session_here(self):
        window = self.get_active_window()
        if window is not None:
            window.prompt_new_session()

    # -- sessions ----------------------------------------------------------

    # -- settings ----------------------------------------------------------

    def default_folder(self):
        path = self.config.get("default_folder")
        if path and os.path.isdir(path):
            return path
        return os.path.expanduser("~")

    def set_default_folder(self, path):
        self.config["default_folder"] = path
        save_config(self.config)

    def claude_command(self):
        return self.config.get("claude_command") or "claude"

    def set_claude_command(self, command):
        self.config["claude_command"] = command or "claude"
        save_config(self.config)

    def claude_args(self):
        return self.config.get("claude_args") or ""

    def set_claude_args(self, args):
        self.config["claude_args"] = args or ""
        save_config(self.config)

    # -- list ordering -----------------------------------------------------

    def auto_sort(self):
        """True (default) keeps the original self-rearranging list."""
        return bool(self.config.get("auto_sort", True))

    def set_auto_sort(self, enabled):
        self.config["auto_sort"] = bool(enabled)
        save_config(self.config)
        for window in self.session_windows():
            window._last_signature = None
            window.refresh()

    def manual_order(self):
        order = self.config.get("manual_order")
        return list(order) if isinstance(order, list) else []

    def move_in_order(self, src_key, dst_key, seed_keys=()):
        """Put src_key where dst_key is, and remember it."""
        order = self.manual_order()
        # Rows that have never been dragged are not in the stored order yet;
        # seed them from what is currently on screen so the first drag has
        # something coherent to rearrange.
        for key in seed_keys:
            if key not in order:
                order.append(key)
        if src_key not in order or dst_key not in order:
            return
        order.remove(src_key)
        order.insert(order.index(dst_key), src_key)
        self.config["manual_order"] = order
        save_config(self.config)
        for window in self.session_windows():
            window._last_signature = None
            window.refresh()

    # -- zoom --------------------------------------------------------------

    def _zoom_value(self, key):
        # "zoom" is the old single setting; fall back to it so an existing
        # config keeps the size the user already chose.
        raw = self.config.get(key, self.config.get("zoom", 1.0))
        try:
            return max(0.5, min(3.0, float(raw)))
        except (TypeError, ValueError):
            return 1.0

    def terminal_zoom(self):
        return self._zoom_value("terminal_zoom")

    def sidebar_zoom(self):
        return self._zoom_value("sidebar_zoom")

    def set_terminal_zoom(self, value, persist=True):
        value = max(0.5, min(3.0, round(float(value), 2)))
        self.config["terminal_zoom"] = value
        if persist:
            save_config(self.config)
        for sess in self.sessions:
            self.theme.apply(sess.terminal, value)

    def set_sidebar_zoom(self, value, persist=True):
        value = max(0.5, min(3.0, round(float(value), 2)))
        self.config["sidebar_zoom"] = value
        if persist:
            save_config(self.config)
        self._apply_css()
        for window in self.get_windows():
            if isinstance(window, SessionWindow):
                window._last_signature = None
                window.refresh()

    def adjust_zoom(self, delta):
        """Ctrl +/- targets the terminal, as it would in any terminal app."""
        self.set_terminal_zoom(self.terminal_zoom() + delta)

    def reset_zoom(self):
        self.set_terminal_zoom(1.0)

    def unique_name(self, base):
        """Avoid two sessions in the same folder sharing a name."""
        taken = {s.name for s in self.sessions if not s.exited}
        if base not in taken:
            return base
        index = 2
        while "%s-%d" % (base, index) in taken:
            index += 1
        return "%s-%d" % (base, index)

    def start_session(self, name, cwd, mode="claude", resume_id=None):
        sess = TerminalSession(
            self.theme, cwd=cwd, name=name, mode=mode,
            command=self.claude_command(), extra_args=self.claude_args(),
            zoom=self.terminal_zoom(), resume_id=resume_id,
        )
        sess.spawn()
        self.sessions.append(sess)
        self.persist()
        return sess

    def session_for_entry(self, entry):
        for sess in self.sessions:
            if sess.matches(entry):
                return sess
        return None

    # -- "needs you" tracking ---------------------------------------------

    @staticmethod
    def entry_key(entry):
        return entry.session_id or ("pid:%s" % entry.pid)

    def is_watched(self, entry):
        """True if this session is on screen in a window that has focus."""
        sess = self.session_for_entry(entry)
        if sess is None:
            return False
        for window in self.get_windows():
            if not isinstance(window, SessionWindow):
                continue
            if window.is_active() and window.stack.get_visible_child() is sess.terminal:
                return True
        return False

    def update_attention(self, entries):
        live = set()
        for entry in entries:
            key = self.entry_key(entry)
            live.add(key)
            watched = self.is_watched(entry)
            previous = self._prev_status.get(key)

            if entry.status != previous:
                self._prev_status[key] = entry.status
                external = self.session_for_entry(entry) is None

                if entry.status == "waiting":
                    # A dialog is physically BLOCKING the session -- Claude
                    # itself says so via waitingFor. Counts on first sighting
                    # and for external sessions too.
                    flag = "blocked" if not watched else None
                elif entry.status == "idle" and previous in ("busy", "shell"):
                    # It finished a turn while you were elsewhere. Worth
                    # knowing, but NOT the same as needing you: the last
                    # exchange may well have been "bye" / "ok bye". Claude
                    # reports waitingFor=None here, and we must not dress that
                    # up as a demand.
                    flag = "finished" if (not watched and not external) else None
                else:
                    flag = None

                self.attention[key] = flag

            if watched:
                self.attention[key] = None

        for key in list(self.attention):
            if key not in live:
                del self.attention[key]
                self._prev_status.pop(key, None)

    def attention_kind(self, entry):
        """None, 'blocked' (Claude is waiting on you), or 'finished'."""
        return self.attention.get(self.entry_key(entry))

    def blocked_count(self):
        return sum(1 for kind in self.attention.values() if kind == "blocked")

    def close_session(self, sess):
        """End a session and drop its tab.

        SIGHUP goes to the whole process group, not just our child -- a plain
        shell tab may have `claude` (or anything else) running inside it, and
        signalling only the shell would leave that orphaned.
        """
        # Detach FIRST: stop listening for child-exited before we tear anything
        # down, so the signal cannot fire into a half-destroyed object.
        sess.detach()

        if sess.pid:
            try:
                os.killpg(os.getpgid(sess.pid), signal.SIGHUP)
            except OSError:
                try:
                    os.kill(sess.pid, signal.SIGTERM)
                except OSError:
                    pass

        parent = sess.terminal.get_parent()
        if parent is not None:
            try:
                parent.remove(sess.terminal)
            except Exception:
                pass
        if sess in self.sessions:
            self.sessions.remove(sess)
        # Hold the widget alive past this call so VTE can finish with the pty
        # on its own schedule rather than during garbage collection.
        self._retired.append(sess)
        if len(self._retired) > 50:
            del self._retired[:-50]

        for window in self.get_windows():
            if isinstance(window, SessionWindow):
                window._last_signature = None
                window.refresh()
        # Closing on purpose means "do not offer this back on restart".
        self.persist()

    def close_current(self):
        window = self.get_active_window()
        if not isinstance(window, SessionWindow):
            return
        current = window.stack.get_visible_child()
        for sess in list(self.sessions):
            if sess.terminal is current:
                self.close_session(sess)
                return

    def clear_attention_for(self, sess):
        """Clear the flag the moment a session is opened, not a tick later."""
        for entry in roster.read():
            if sess.matches(entry):
                self.attention[self.entry_key(entry)] = None
                break

    def _tick(self):
        entries = roster.read()
        self.update_attention(entries)
        # Learn each session's id from the roster -- it is what lets a
        # crashed session be resumed later with `claude -r <id>`.
        for entry in entries:
            sess = self.session_for_entry(entry)
            if sess is not None:
                sess.session_id = entry.session_id
                sess.last_status = entry.status
                # Where Claude actually is, which may not be where this
                # tab started if you cd'd before launching it.
                sess.roster_cwd = entry.cwd or sess.cwd
                # Track renames done inside the session itself, otherwise
                # we would persist the name it was launched with and the
                # new one would vanish on restart.
                if entry.name and entry.name != sess.name:
                    sess.name = entry.name
        self._persist_tick += 1
        if self._persist_tick % 5 == 0:
            self.persist()
        for window in self.get_windows():
            if isinstance(window, SessionWindow):
                window.refresh(entries)
        return True

    # -- styling -----------------------------------------------------------

    def _apply_css(self):
        theme = self.theme
        zoom = self.sidebar_zoom()

        def rem(base):
            return "%.3frem" % (base * zoom)

        parts = [
            """
            .ccs-window, .ccs-sidebar {{ background: {bg}; }}
            /* 4px breathing room so rows and the close button do not sit
               flush against the divider line. */
            .ccs-sidebar {{ border-right: 1px solid {line}; padding-right: 4px; }}
            .ccs-header {{
                color: {dim}; font-family: {font}; font-size: {s_header};
                letter-spacing: 0.09em; padding: 0 0 0 12px;
            }}
            .ccs-toolbar {{
                padding: 6px 6px 6px 0; border-bottom: 1px solid {line};
            }}
            .ccs-toolbtn {{
                background: transparent; border: 0; box-shadow: none;
                color: {dim}; min-width: 28px; min-height: 28px;
                padding: 2px 6px;   /* 4px extra either side -> wider buttons */
            }}
            .ccs-toolbtn:hover {{ background: alpha({sel}, 0.18); color: {fg}; }}
            .ccs-list {{ background: {bg}; }}
            .ccs-list > row {{
                background: transparent; padding: 0; border: 0;
            }}
            .ccs-list > row:selected {{ background: alpha({sel}, 0.18); }}
            .ccs-row {{ padding: 7px 12px; }}
            .ccs-dot {{ font-size: {s_dot}; }}
            .ccs-name {{ color: {fg}; font-family: {font}; font-size: {s_name}; }}
            .ccs-attention {{ color: {attn}; font-weight: bold; }}
            .ccs-external {{ color: {dim}; }}
            .ccs-age, .ccs-path, .ccs-sub {{
                color: {dim}; font-family: {font}; font-size: {s_meta};
            }}
            .ccs-close {{
                background: transparent; border: 0; box-shadow: none;
                color: {dim}; font-family: {font}; padding: 0 4px;
                min-width: 0; min-height: 0; opacity: 0.55;
            }}
            .ccs-close:hover {{ color: {attn}; opacity: 1; background: transparent; }}
            """.format(
                bg=theme.bg_css,
                fg=theme.fg_css,
                dim=colour_for(theme, "dim"),
                line=colour_for(theme, "dim"),
                sel=colour_for(theme, "busy"),
                attn=colour_for(theme, "waiting"),
                font=theme.font_family_css,
                s_header=rem(0.75), s_dot=rem(0.62),
                s_name=rem(0.92), s_meta=rem(0.78),
            )
        ]
        for name in COLOUR_NAMES:
            value = colour_for(theme, name)
            parts.append(".ccs-dot-%s { color: %s; }" % (name, value))
            parts.append(".ccs-sub-%s { color: %s; }" % (name, value))

        # Reuse one provider and just reload its content. Adding a new provider
        # on every zoom step would stack them on the display forever.
        if self._provider is None:
            self._provider = Gtk.CssProvider()
            Gtk.StyleContext.add_provider_for_display(
                Gdk.Display.get_default(),
                self._provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )
        load_css(self._provider, "\n".join(parts))


def main():
    non_unique = "--new-instance" in sys.argv
    argv = [a for a in sys.argv if a != "--new-instance"]
    app = CCSessions(non_unique=non_unique)
    return app.run(argv)


if __name__ == "__main__":
    sys.exit(main())
