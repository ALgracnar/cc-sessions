#!/usr/bin/env python3
"""cc-sessions — a session-aware terminal for Claude Code.

Your GNOME Terminal, plus a sidebar that tells you which session is actually
blocked on you (`waiting`) rather than merely quiet (`idle`).

  cc-sessions                 open a window
  cc-sessions --new-instance  run a separate process (see multi-window note)
"""

import faulthandler
import json
import os
import signal
import sys
import time

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
gi.require_version("Graphene", "1.0")

from gi.repository import Gdk, Gio, GLib, GObject, Graphene, Gtk, Vte

import roster
import state
import stories
from session import TerminalSession, _parent_pid, default_session_name
from theme import TerminalTheme

APP_ID = "eu.nevron.ccsessions"

# Colours by meaning, not by palette index -- mostly the user's own ANSI
# colours, so the sidebar speaks the same visual language as the terminal below
# it.
FINISHED_ORANGE = "#CC7832"

PALETTE_SLOT = {
    "waiting": 11,   # bright yellow  — blocked on you; the loudest thing here
    "busy": 12,      # bright blue    — working
    "shell": 14,     # bright cyan    — running a command
    "idle": 10,      # bright green   — done, nothing pending
    "done": 13,      # bright magenta — YOU marked it finished; ours, not Claude's
    "dim": 8,        # bright black   — dormant / unknown
}
# Orange is a literal because the ANSI palette has none. It carries the quieter
# of the two attention states -- finished while you were away -- leaving the
# more insistent yellow for a session that is actually blocked.
# "Waiting on it": YOUR mark that a session (or story) is parked until
# something outside it happens -- a deploy, a reply. Every ANSI slot already
# means something here, so it gets its own soft periwinkle.
PARKED_PERIWINKLE = "#9A8CFF"
LITERAL_COLOUR = {"finished": FINISHED_ORANGE, "parked": PARKED_PERIWINKLE}
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


class NameDialog(Gtk.Window):
    """Ask for one line of text: a story's name."""

    def __init__(self, parent, title, label, text, on_accept, button="Save"):
        super().__init__(title=title, transient_for=parent, modal=True)
        self.on_accept = on_accept
        self.set_default_size(420, -1)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        for setter in ("set_margin_top", "set_margin_bottom",
                       "set_margin_start", "set_margin_end"):
            getattr(box, setter)(16)

        box.append(Gtk.Label(label=label, xalign=0))
        self.entry = Gtk.Entry()
        self.entry.set_text(text or "")
        self.entry.connect("activate", lambda *_: self._accept())
        box.append(self.entry)

        buttons = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        buttons.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.close())
        ok = Gtk.Button(label=button)
        ok.add_css_class("suggested-action")
        ok.connect("clicked", lambda *_: self._accept())
        buttons.append(cancel)
        buttons.append(ok)
        box.append(buttons)
        self.set_child(box)
        self.entry.grab_focus()

    def _accept(self):
        text = self.entry.get_text().strip()
        if not text:
            return
        self.close()
        self.on_accept(text)


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

        prefix_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        prefix_text = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        prefix_text.set_hexpand(True)
        prefix_text.append(Gtk.Label(label="Group sessions named alike", xalign=0))
        prefix_hint = Gtk.Label(xalign=0)
        prefix_hint.set_markup(
            "<span alpha='55%'>On: “AT-43-backend” and “AT-43-pwa” sit together "
            "in a story called AT-43.  Stories you make by hand, and sessions "
            "started by another session, are grouped either way.</span>"
        )
        prefix_hint.set_wrap(True)
        prefix_text.append(prefix_hint)
        prefix_row.append(prefix_text)
        self.prefix_switch = Gtk.Switch()
        self.prefix_switch.set_valign(Gtk.Align.CENTER)
        self.prefix_switch.set_active(app.story_by_prefix())
        self.prefix_switch.connect(
            "notify::active",
            lambda sw, _p: app.set_story_by_prefix(sw.get_active()),
        )
        prefix_row.append(self.prefix_switch)
        box.append(prefix_row)

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

        box.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        side_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        side_label = Gtk.Label(label="Session list side", xalign=0)
        side_label.set_hexpand(True)
        side_row.append(side_label)
        self.side_dropdown = Gtk.DropDown.new_from_strings(["Left", "Right"])
        self.side_dropdown.set_selected(1 if app.sidebar_side() == "right" else 0)
        side_row.append(self.side_dropdown)
        box.append(side_row)

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
        self.app.set_sidebar_side(
            "right" if self.side_dropdown.get_selected() == 1 else "left"
        )
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
        # A right-click menu is parented to its row; rebuilding the list under
        # an open menu would pull the row out from beneath it. So the list
        # holds still while a menu is open.
        self._menu_open = False
        # Same for a drag: a rebuild mid-drag would throw away the row being
        # dragged and the row lit up under the pointer. The drop's changes
        # are drawn once the drag ends.
        self._dragging = False
        self._drop_lit = None        # (widget, css class, "+ add" hint) lit now
        self._story_members = {}     # story id -> member keys, as last drawn
        self._story_items = {}       # story id -> member items, as last drawn
        self._story_colour = {}      # story id -> its status colour name
        self._story_first = {}       # story id -> member first in the stored
                                     # order (where the story sits), which a
                                     # starred member drawn on top may not be
        self._pointer_ages = []      # (label, row key): pointer-line ages
        self._hold_scroll = None     # scroll to restore after a rebuild's layout
        self._hold_queued = False
        for name, handler in (
            ("story-start", self._story_start),
            ("story-new", self._story_new),
            ("story-add", self._story_add),
            ("story-remove", self._story_remove),
            ("story-pin", lambda key: self.app.toggle_pinned(key)),
            ("story-done", self._story_done),
            ("story-parked", self._story_parked),
            ("story-rename", self._story_rename),
            ("story-ungroup", self._story_ungroup),
            ("story-toggle", self._story_toggle),
        ):
            action = Gio.SimpleAction.new(name, GLib.VariantType.new("s"))
            action.connect("activate",
                           lambda _a, param, h=handler: h(param.get_string()))
            self.add_action(action)
        if restorable:
            self.app.add_pending(restorable)

        # Top-level vertical box. v2's render revealer slots in above the
        # terminal area without restructuring anything.
        self.outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        self.paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.paned.set_vexpand(True)
        self.sidebar_box = self._build_sidebar()
        self._build_terminal_area()      # creates self.stack
        self.seat_panes(self.app.sidebar_side())

        self.outer.append(self.paned)
        self.set_child(self.outer)
        self.add_css_class("ccs-window")

        self.refresh()

    def seat_panes(self, side):
        """Seat the session list on the chosen side of the divider.

        Safe to call on a live window: both children are held by Python refs
        and stay inside the same window, so reparenting them across the paned
        does not tear down the terminals.
        """
        on_right = side == "right"
        self.paned.set_start_child(None)
        self.paned.set_end_child(None)
        if on_right:
            self.paned.set_start_child(self.stack)
            self.paned.set_end_child(self.sidebar_box)
        else:
            self.paned.set_start_child(self.sidebar_box)
            self.paned.set_end_child(self.stack)
        # Window growth goes to the terminal; the list keeps its width.
        self.paned.set_resize_start_child(on_right)
        self.paned.set_shrink_start_child(on_right)
        self.paned.set_resize_end_child(not on_right)
        self.paned.set_shrink_end_child(not on_right)
        # 4px gap so terminal text does not sit flush against the divider --
        # on whichever side the divider now is.
        self.stack.set_margin_start(0 if on_right else 4)
        self.stack.set_margin_end(4 if on_right else 0)
        # Paned position is measured from the LEFT edge, so a right-hand list
        # sits at (width - list width). Before the window is realized the
        # allocated width is 0; fall back to the default size.
        if on_right:
            width = self.get_width() or self.get_default_size()[0]
            self.paned.set_position(max(200, width - 250))
        else:
            self.paned.set_position(250)

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

        # Below the last row: a drop zone that fills the empty space (and is
        # never thinner than a row's grip area, even in a list that scrolls).
        # Drops land ABOVE the row you are over, so without it nothing could
        # ever be dropped at the very end of the list.
        self.list_end = Gtk.Box()
        self.list_end.set_vexpand(True)
        self.list_end.add_css_class("ccs-list-end")
        self._attach_drop(self.list_end, lambda payload: self._drop_plan_end(payload))

        column = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        column.append(self.listbox)
        column.append(self.list_end)

        scroller = Gtk.ScrolledWindow()
        scroller.set_vexpand(True)
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.set_child(column)
        self.scroller = scroller

        # The list sits in an overlay so the little tag that says what a drop
        # will do can float over it. (A pinned copy of the lid of the story
        # you were scrolled into used to float here too; it was removed: it
        # drew oddly and did work on every scroll step.)
        self.overlay = Gtk.Overlay()
        self.overlay.set_vexpand(True)
        self.overlay.set_child(scroller)

        self.drop_tag = Gtk.Label(xalign=0)
        self.drop_tag.add_css_class("ccs-drop-tag")
        self.drop_tag.set_valign(Gtk.Align.START)
        self.drop_tag.set_halign(Gtk.Align.END)
        self.drop_tag.set_margin_end(8)
        self.drop_tag.set_ellipsize(3)
        self.drop_tag.set_can_target(False)
        self.drop_tag.set_visible(False)
        self.overlay.add_overlay(self.drop_tag)

        box.append(self.overlay)

        return box

    def _build_terminal_area(self):
        self.stack = Gtk.Stack()
        self.stack.set_hexpand(True)
        self.stack.set_vexpand(True)
        # The divider-side breathing gap is set by seat_panes(), which knows
        # which side the divider is on.

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
        key = item_key("entry", entry, owned)
        # Only an idle session can be marked done -- a busy or blocked one is
        # plainly not finished, so offering the mark there would be nonsense.
        done = entry.status == "idle" and self.app.is_done(key)
        parked = entry.status == "idle" and not done and self.app.is_parked(key)

        colour = entry.status if entry.status in LIVE_STATUSES else "dim"
        if done:
            colour = "done"         # purple: you called it finished
        elif attention == "blocked":
            colour = "waiting"      # orange: blocked on you
        elif attention == "finished":
            colour = "finished"     # yellow: done, but you were not looking
        elif parked:
            # periwinkle: you are waiting on it. Below "finished" on purpose:
            # it finishing while you were away is the news you waited for.
            colour = "parked"

        dot = Gtk.Label(label="■" if done else ("●" if attention else "■"))
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

        box.append(top)

        # The age and the close button sit on the status line, not the name
        # line: the status is short ("idle", "busy"), the name is the long
        # thing, so this is where the free space actually is. On the top line
        # they ellipsized the name to make room for themselves.
        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        # Say the one thing that matters, without the internal status name
        # alongside it. `waitingFor` is already readable on its own ("input
        # needed", "dialog open", "sandbox request").
        if done:
            detail = "done"        # ✓ already says idle; the long form was cut off
        elif entry.status == "waiting" or attention == "blocked":
            detail = entry.waiting_for or "input needed"
        elif attention == "finished":
            detail = "finished"
        elif parked:
            detail = "waiting"
        else:
            detail = entry.status_label
        sub = Gtk.Label(label=detail, xalign=0)
        sub.set_hexpand(True)
        sub.set_ellipsize(3)
        sub.add_css_class("ccs-sub")
        sub.add_css_class("ccs-sub-%s" % colour)
        line.append(sub)

        # The mark lives next to the status it qualifies, and only appears
        # when Claude says idle -- it is an annotation on that state.
        if owned is not None and entry.status == "idle":
            line.append(self._parked_button(key, parked))
            line.append(self._done_button(key, done))

        age = Gtk.Label(label=entry.describe_age(), xalign=1)
        age.add_css_class("ccs-age")
        line.append(age)
        row.age_label = age

        # Only sessions we own can be closed -- we have no handle on anything
        # started outside the app.
        if owned is not None:
            line.append(self._close_button(owned))

        box.append(line)

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

    def _parked_button(self, key, parked):
        """Toggle YOUR 'waiting on it' mark: parked until something outside
        happens. Sits in front of ✓; the two marks exclude each other."""
        button = Gtk.Button(label="◷")
        button.add_css_class("ccs-done-btn")
        if parked:
            button.add_css_class("ccs-parked-on")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text("Stop waiting on it" if parked else "Mark as waiting on it")
        button.connect("clicked", lambda *_: self.app.toggle_parked(key))
        return button

    def _done_button(self, key, done):
        """Toggle YOUR 'done' mark on an idle session.

        Separate from closing: a finished session often has to stay visible
        until the work actually ships, so this settles it rather than ending it.
        """
        button = Gtk.Button(label="✓")
        button.add_css_class("ccs-done-btn")
        if done:
            button.add_css_class("ccs-done-on")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text("Unmark as done" if done else "Mark as done")
        button.connect("clicked", lambda *_: self.app.toggle_done(key))
        return button

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
        box.append(top)

        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

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
        sub.set_hexpand(True)
        sub.set_ellipsize(3)
        sub.add_css_class("ccs-sub")
        sub.add_css_class("ccs-sub-dim")
        line.append(sub)

        dismiss = Gtk.Button(label="×")
        dismiss.add_css_class("ccs-close")
        dismiss.set_valign(Gtk.Align.CENTER)
        dismiss.set_tooltip_text("Forget this session")
        dismiss.connect("clicked", lambda *_: self.dismiss_restorable(record))
        line.append(dismiss)
        box.append(line)

        path = Gtk.Label(label=short_path(record.get("cwd", "")), xalign=0)
        path.set_ellipsize(3)
        path.add_css_class("ccs-path")
        box.append(path)

        row.set_child(box)
        return row

    def dismiss_restorable(self, record):
        self.app.forget_key(item_key("record", record))
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
        # Keep the row where it was -- in its story, at its place -- from the
        # first moment. Until Claude registered, the new terminal was known
        # only as "local:…", which no story or saved order knew, so a resumed
        # story member dropped out to the bottom for a few seconds.
        if resume_id:
            # Resuming keeps the conversation's id: claim it right away.
            sess.session_id = resume_id
        else:
            # Starting fresh: the new terminal takes over the old row's place,
            # story, pin and marks.
            self.app.rename_key(item_key("record", record), "local:" + sess.key)
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
        box.append(top)

        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)

        if sess.exited:
            detail = "ended"
        elif sess.mode == "shell":
            detail = "terminal"
        else:
            detail = "starting…"
        sub = Gtk.Label(label=detail, xalign=0)
        sub.set_hexpand(True)
        sub.set_ellipsize(3)
        sub.add_css_class("ccs-sub")
        sub.add_css_class("ccs-sub-%s" % colour)
        line.append(sub)

        line.append(self._close_button(sess))
        box.append(line)

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
        # Live rows above, dormant ones below. A session still starting up is
        # live; a record you have not resumed is not.
        group = {"entry": 0, "local": 1, "record": 2}
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

        Every row must be PINNED into the stored order the first time it is
        seen, not merely fall back to the automatic order when absent. That was
        the original bug: the stored order starts empty, so switching this mode
        on appeared to do nothing at all -- the whole list kept auto-sorting
        until you happened to drag something.
        """
        order = self.app.manual_order()
        unseen = [
            key for key in (item_key(*i) for i in self._auto_sorted(items))
            if key not in order
        ]
        if unseen:
            # Seed newcomers in automatic order, then leave them alone forever.
            self.app.extend_order(unseen)
            order = self.app.manual_order()

        position = {k: i for i, k in enumerate(order)}
        story_of = self.app.story_of()

        def sort_key(item):
            # One exception to "nothing moves unless you move it": LOOSE
            # dormant rows always sit at the bottom. They are not part of the
            # working set until resumed, so they should never come between
            # live sessions -- and a resumed one takes its manual place
            # automatically, since the record and the session it becomes
            # share an id.
            # Rows in a saved story do NOT sink: a story stays where you put
            # it even when everything in it is waiting to be resumed. Sinking
            # those too dragged whole stories down among the dormant rows, and
            # nothing live could then be placed below them.
            key = item_key(*item)
            sinks = item[0] == "record" and key not in story_of
            return (1 if sinks else 0, position.get(key, len(order)))

        return sorted(items, key=sort_key)

    def _attach_reorder(self, row):
        """Make a row draggable onto its siblings (manual mode only).

        The drag starts from an explicit grip, NOT from the row itself. A
        GtkDragSource on the whole row is a gesture that claims the button
        sequence, so the smallest movement while pressing began a drag and the
        row never activated -- clicking a session simply did nothing. Giving
        drag its own handle removes the conflict entirely, and makes it
        discoverable rather than a hidden gesture.
        """
        line = self._status_line(row)
        if line is not None:
            if getattr(row, "pinned", False):
                grip = self._star_label(draggable=True)
            else:
                grip = Gtk.Label(label="⠿")
                grip.add_css_class("ccs-grip")
                grip.set_tooltip_text("Drag to reorder")
                grip.set_valign(Gtk.Align.CENTER)
            source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
            source.connect(
                "prepare",
                lambda _s, _x, _y, key=row.item_key: Gdk.ContentProvider.new_for_value(key),
            )
            self._watch_drag(source, row.item_key,
                             lambda row=row: getattr(row, "item_name", "") or "",
                             lambda row=row: [row.get_child()])
            grip.add_controller(source)

            # The status line is already a box (status … age ×), so the grip
            # just takes the front seat.
            line.prepend(grip)

        # Drops are still accepted anywhere on the row -- only the *start* of a
        # drag needs to be deliberate.
        self._attach_drop(row, lambda payload, row=row: self._drop_plan(payload, row=row))

    @staticmethod
    def _status_line(row):
        """The row's second line (status … age ×), where the grip sits."""
        box = getattr(row, "body", None) or row.get_child()
        top = box.get_first_child() if box is not None else None
        return top.get_next_sibling() if top is not None else None

    @staticmethod
    def _star_label(draggable=False):
        """★ in the grip's seat: this session is pinned to the top of its
        story. Right-click the row to unpin."""
        star = Gtk.Label(label="★")
        star.add_css_class("ccs-grip")
        star.add_css_class("ccs-star")
        star.set_valign(Gtk.Align.CENTER)
        star.set_tooltip_text(
            "Pinned to the top of its story%s.\nRight-click the row to unpin."
            % (" — drag to reorder pinned ones" if draggable else ""))
        return star

    # -- drag and drop -----------------------------------------------------
    #
    # Two payloads travel: a session's row key, or STORY_DRAG + a story id
    # (from a lid's grip). Every drop handler goes through _drop_plan, which
    # checks for a story payload FIRST -- otherwise "story\t…" would be filed
    # into a story as if it were a row key.

    STORY_DRAG = "story\t"

    def _watch_drag(self, source, payload, label, ghosts):
        """Fade what is being dragged, hold the list still, show its name."""
        def begin(_source, drag):
            self._dragging = True
            self.app.drag_payload = payload
            for widget in ghosts():
                if widget is not None:
                    widget.add_css_class("ccs-dragging")
            # Without an icon GTK draws the payload itself ("story\ts17…").
            icon = Gtk.Label(label=label())
            icon.add_css_class("ccs-drop-tag")
            Gtk.DragIcon.get_for_drag(drag).set_child(icon)

        def end(_source, _drag, _delete):
            self.app.drag_payload = None
            for widget in ghosts():
                if widget is not None:
                    widget.remove_css_class("ccs-dragging")
            self._clear_drop()
            self._dragging = False
            self._last_signature = None

            def after():
                self.refresh()
                return False
            GLib.idle_add(after)

        source.connect("drag-begin", begin)
        source.connect("drag-end", end)

    def _attach_drop(self, widget, plan_for, banded=False):
        """Accept drops on widget; plan_for(payload) says what they would do.

        banded: plan_for also gets where the pointer is on the widget --
        "top", "middle" or "bottom" -- so a story lid can tell "put it in
        here" (middle) from "put it above / below this story" (its edges).
        """
        def band_of(y):
            height = widget.get_height()
            if height <= 0:
                return "middle"
            edge = max(10, min(18, height * 0.3))
            if y < edge:
                return "top"
            if y > height - edge:
                return "bottom"
            return "middle"

        def plan_at(payload, y):
            return plan_for(payload, band_of(y)) if banded else plan_for(payload)
        target = Gtk.DropTarget.new(GObject.TYPE_STRING, Gdk.DragAction.MOVE)
        target.set_preload(True)

        def payload_of(t):
            if self.app.drag_payload is not None:
                return self.app.drag_payload
            value = t.get_value()
            if isinstance(value, GObject.Value):
                value = value.get_value()
            return value

        def hover(t, _x, y):
            plan = plan_at(payload_of(t), y)
            if plan is None:
                self._clear_drop()
                return Gdk.DragAction(0)
            self._light_drop(plan)
            return Gdk.DragAction.MOVE

        def dropped(_t, value, _x, y):
            self._clear_drop()
            plan = plan_at(value, y)
            if plan is None:
                return False
            plan[2]()
            return True

        target.connect("enter", hover)
        target.connect("motion", hover)
        target.connect("leave", lambda *_: self._clear_drop())
        target.connect("drop", dropped)
        widget.add_controller(target)

    def _drop_plan_beside(self, key, sid, lid_box, band):
        """Dropping a session on the top/bottom edge of story sid's lid:
        it leaves any saved story and lands just above / below sid."""
        members = list(self._story_members.get(sid, ()))
        if not members:
            return None
        src_sid = self._story_of_key(key)
        if src_sid is not None and stories.is_virtual(src_sid):
            # Grouped by its name: moving it cannot take it out of that family.
            if src_sid == sid:
                return None
        seeds = [r.item_key for r in self._rows if getattr(r, "item_key", None)]
        collapsed = self.app.story_collapsed(sid)
        name = self._key_name(key)
        story_name = self._story_name(sid)

        if band == "bottom" and collapsed:
            # Before whatever is drawn right after this story, or at the end.
            after = self._first_key_after_story(sid)
            if after == key:
                after = self._first_key_after_story(sid, skip=key)

            def move():
                self.app.follow_story(key, None, self._story_members)
                if after is None:
                    self.app.move_block_to_end([key], seeds)
                else:
                    self.app.move_in_order(key, after, seeds)
            return ((lid_box, "ccs-drop-after", False),
                    "%s goes below %s" % (name, story_name), move)
        if band == "bottom":
            return None          # under an open lid: its first member is there

        first = self._story_first.get(sid, members[0])

        def move():
            self.app.follow_story(key, None, self._story_members)
            if key != first:
                self.app.move_in_order(key, first, seeds)
        return ((lid_box, "ccs-drop-before", False),
                "%s goes above %s" % (name, story_name), move)

    def _first_key_after_story(self, sid, skip=None):
        """Key of the first row drawn after story sid's block (a following
        story counts by its first member), or None at the end of the list."""
        rows = self._rows
        start = next((i for i, r in enumerate(rows)
                      if getattr(r, "story", None) is not None
                      and r.story["id"] == sid), None)
        if start is None:
            return None
        for row in rows[start + 1:]:
            if getattr(row, "story_id", None) == sid:
                continue
            story = getattr(row, "story", None)
            key = (self._story_first.get(story["id"])
                   if story is not None else getattr(row, "item_key", None))
            if key is not None and key != skip:
                return key
        return None

    def _drop_plan(self, payload, row=None, lid_sid=None, lid_box=None, band="middle"):
        """What dropping payload on a session row (row) or a lid (lid_sid,
        lid_box) would do: ((widget to light, css class, show "+ add"), tag
        text or None, action), or None when nothing would happen.

        band is where on a lid the pointer is (see _attach_drop)."""
        if not isinstance(payload, str) or not payload:
            return None
        seeds = [r.item_key for r in self._rows if getattr(r, "item_key", None)]

        if payload.startswith(self.STORY_DRAG):
            sid = payload[len(self.STORY_DRAG):]
            keys = list(self._story_members.get(sid, ()))
            if not keys:
                return None
            if lid_sid is not None:
                # Over another story's lid: the block lands above that story.
                if lid_sid == sid:
                    return None
                over_sid, line_on = lid_sid, lid_box
            else:
                if row is None or row.item_key in keys:
                    return None          # over itself: nothing
                over_sid = getattr(row, "story_id", None)
                line_on = row.body if over_sid is None else self._lid_of(over_sid)
            if over_sid is not None:
                # Stories never nest, split or merge: over any part of another
                # story, the block goes above that story's lid.
                dst = self._story_first.get(over_sid)
            else:
                dst = row.item_key
            if dst is None or line_on is None:
                return None
            tag = "%s · %d move here" % (self._story_name(sid), len(keys))
            return ((line_on, "ccs-drop-before", False), tag,
                    lambda: self.app.move_block_in_order(keys, dst, seeds))

        key = payload
        if lid_sid is not None and band in ("top", "bottom"):
            # On a lid's edge: the session goes above (or, under a folded
            # story, below) the story, as a loose row -- the only way to put
            # one BETWEEN stories when stories sit next to each other.
            plan = self._drop_plan_beside(key, lid_sid, lid_box, band)
            if plan is not None:
                return plan
        if lid_sid is not None:
            # A session onto a lid joins that story, as its last member.
            members = list(self._story_members.get(lid_sid, ()))

            def join():
                self.app.add_to_story(key, lid_sid, members)
                others = [k for k in members if k != key]
                if others and not self.app.auto_sort():
                    self.app.move_after_in_order(key, others[-1], seeds)
            return ((lid_box, "ccs-lid-drop", True), None, join)

        if row is None or key == row.item_key:
            return None
        if (getattr(row, "record", None) is not None
                and getattr(row, "story_id", None) is None
                and not self._is_record_key(key)):
            # Loose dormant rows always sink below everything live, so a live
            # session dropped among them lands above the first of them. Say
            # so with the line, instead of drawing it where it cannot go.
            first = next((r for r in self._rows
                          if getattr(r, "record", None) is not None
                          and getattr(r, "story_id", None) is None), row)
            src_sid = self._story_of_key(key)
            tag = ("%s leaves %s" % (self._key_name(key), self._story_name(src_sid))
                   if src_sid is not None and not stories.is_virtual(src_sid)
                   else None)

            def to_end():
                # A loose dormant row's stored position means nothing (it
                # sinks either way), so "just above them" is simply after
                # everything that does not sink: the end of the order.
                self.app.follow_story(key, None, self._story_members)
                self.app.move_block_to_end([key], seeds)
            return ((first.body, "ccs-drop-before", False), tag, to_end)
        src_sid = self._story_of_key(key)
        dst_sid = getattr(row, "story_id", None)
        if dst_sid is not None and dst_sid != src_sid:
            tag = "%s joins %s" % (self._key_name(key), self._story_name(dst_sid))
        elif dst_sid is None and src_sid is not None:
            tag = "%s leaves %s" % (self._key_name(key), self._story_name(src_sid))
        else:
            tag = None
        dst_key = row.item_key

        def move():
            # Where you drop it is where it lives: among a story's rows it
            # joins that story, among loose rows it leaves its story.
            self.app.follow_story(key, dst_key, self._story_members)
            self.app.move_in_order(key, dst_key, seeds)
        return ((row.body, "ccs-drop-before", False), tag, move)

    def _drop_plan_end(self, payload):
        """What dropping payload in the zone below the last row would do:
        it goes to the very end of the list, outside any story."""
        if not isinstance(payload, str) or not payload:
            return None
        seeds = [r.item_key for r in self._rows if getattr(r, "item_key", None)]
        last = seeds[-1] if seeds else None

        if payload.startswith(self.STORY_DRAG):
            sid = payload[len(self.STORY_DRAG):]
            keys = list(self._story_members.get(sid, ()))
            if not keys or last in keys:
                return None          # already the last thing in the list
            tag = "%s · %d move here" % (self._story_name(sid), len(keys))
            return ((self.list_end, "ccs-drop-before", False), tag,
                    lambda: self.app.move_block_to_end(keys, seeds))

        key = payload
        src_sid = self._story_of_key(key)
        if key == last and src_sid is None:
            return None              # already last and loose: nothing to do
        # A by-name story cannot be left by moving (the name still matches),
        # so only a saved story gets the "leaves" tag.
        tag = ("%s leaves %s" % (self._key_name(key), self._story_name(src_sid))
               if src_sid is not None and not stories.is_virtual(src_sid)
               else None)

        def move():
            self.app.follow_story(key, None, self._story_members)
            self.app.move_block_to_end([key], seeds)
        return ((self.list_end, "ccs-drop-before", False), tag, move)

    def _light_drop(self, plan):
        (widget, css, hint), tag, _act = plan
        lit = self._drop_lit
        if (lit is not None and lit[0] is widget and lit[1] == css
                and (tag or "") == (self.drop_tag.get_label()
                                    if self.drop_tag.get_visible() else "")):
            return
        self._clear_drop()
        widget.add_css_class(css)
        hint_label = getattr(widget, "drop_hint", None) if hint else None
        if hint_label is not None:
            hint_label.set_visible(True)
        self._drop_lit = (widget, css, hint_label)
        if tag:
            # The tag sits on the line: the target's top edge, right end.
            ok, point = widget.compute_point(self.overlay, Graphene.Point().init(0, 0))
            y = point.y if ok else 0
            self.drop_tag.set_label(tag)
            self.drop_tag.set_margin_top(max(0, int(y) - 8))
            self.drop_tag.set_visible(True)

    def _clear_drop(self):
        lit, self._drop_lit = self._drop_lit, None
        if lit is not None:
            widget, css, hint_label = lit
            widget.remove_css_class(css)
            if hint_label is not None:
                hint_label.set_visible(False)
        self.drop_tag.set_visible(False)

    def _is_record_key(self, key):
        row = next((r for r in self._rows if getattr(r, "item_key", None) == key), None)
        return getattr(row, "record", None) is not None

    def _story_of_key(self, key):
        """The story a row key is drawn in (by-name stories too), or None."""
        return next((sid for sid, keys in self._story_members.items()
                     if key in keys), None)

    def _key_name(self, key):
        row = next((r for r in self._rows if getattr(r, "item_key", None) == key), None)
        return getattr(row, "item_name", None) or "session"

    def _lid_of(self, sid):
        header = next((r for r in self._rows if getattr(r, "story", None) is not None
                       and r.story["id"] == sid), None)
        return getattr(header, "lid", None)

    def update_ages(self, entries):
        """Retick the age labels without touching the rows themselves.

        The list used to be rebuilt every second purely to advance these, which
        swallowed clicks. Editing the label in place costs nothing and leaves
        the widgets -- and any in-progress click or drag -- alone.
        """
        fresh = {}
        for entry in entries:
            sess = self.app.session_for_entry(entry)
            if sess is not None:
                fresh[item_key("entry", entry, sess)] = entry.describe_age()
        for row in self._rows:
            label = getattr(row, "age_label", None)
            text = fresh.get(getattr(row, "item_key", None))
            if label is not None and text is not None and label.get_label() != text:
                label.set_label(text)
        # The pointer line on a folded lid ("↑ AL-9 · busy · 19m") ticks too.
        for label, key in self._pointer_ages:
            text = fresh.get(key)
            if text is not None:
                text = ("  ·  " + text) if text else ""
                if label.get_label() != text:
                    label.set_label(text)

    def refresh(self, entries=None):
        if self._menu_open or self._dragging:
            return
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
        matched = {id(self.app.session_for_entry(e)) for e in entries}
        local_only = [
            sess for sess in self.app.sessions
            if self.owns(sess) and id(sess) not in matched
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
                     tuple(r.get("session_id") or r.get("name") for r in self.restorable),
                     self.app.stories_signature(),
                     tuple(self._item_name(i) for i in items))
        if signature == self._last_signature:
            return
        self._last_signature = signature

        selected = self.stack.get_visible_child()

        # Where the list is scrolled to. A rebuild must not move it: putting
        # the focus back on a freshly built row (below) made GTK scroll to a
        # row it had not placed yet, which threw the list to the top -- so
        # folding a story jumped you away from where you were.
        adj = self.scroller.get_vadjustment()
        keep_scroll = adj.get_value()

        # If a row has the keyboard focus, note WHICH one and let go of it
        # before the rows are thrown away. Removing the focused row made GTK
        # hand focus -- and with it the selection -- to whatever row it found
        # next, so folding a story looked like being moved to another session.
        focus = self.get_focus()
        focus_row = (focus if isinstance(focus, Gtk.ListBoxRow)
                     else focus.get_ancestor(Gtk.ListBoxRow) if focus is not None
                     else None)
        focus_id = None
        if focus_row is not None and focus_row.get_parent() is self.listbox:
            story = getattr(focus_row, "story", None)
            focus_id = ("story", story["id"]) if story is not None else (
                "item", getattr(focus_row, "item_key", None))
            self.set_focus(None)

        while True:
            row = self.listbox.get_row_at_index(0)
            if row is None:
                break
            self.listbox.remove(row)
        self._rows = []
        self._pointer_ages = []
        self._drop_lit = None

        manual = not self.app.auto_sort()
        blocks = stories.group(
            items,
            key_of=lambda i: item_key(*i),
            name_of=self._item_name,
            story_of=self.app.story_of(),
            stories=self.app.stories(),
            use_prefix=self.app.story_by_prefix(),
            ignored_prefixes=self.app.ignored_prefixes(),
        )
        # Where each story sits is decided by its first member in the stored
        # order -- note that before starred members are lifted to the top.
        self._story_first = {
            block[1]["id"]: item_key(*block[2][0])
            for block in blocks if block[0] == "story" and block[2]
        }
        pinned = self.app.pinned_keys()
        for block in blocks:
            if block[0] == "story":
                # Starred (pinned) members first, in their own order; the
                # rest keep theirs. Sorting is stable, so nothing else moves.
                block[2].sort(key=lambda i: 0 if item_key(*i) in pinned else 1)
        self._story_members = {
            block[1]["id"]: [item_key(*i) for i in block[2]]
            for block in blocks if block[0] == "story"
        }
        self._story_items = {
            block[1]["id"]: list(block[2])
            for block in blocks if block[0] == "story"
        }

        def add_item(item, story_id=None, index=0, count=0):
            kind, obj, owned = item
            if kind == "entry":
                row = self._make_row(obj, owned, self.app.attention_kind(obj))
            elif kind == "record":
                row = self._make_restore_row(obj)
            else:
                row = self._make_local_row(obj)
            row.item_key = item_key(kind, obj, owned)
            row.item_name = self._item_name(item)
            row.story_id = story_id
            row.pinned = story_id is not None and row.item_key in pinned
            # The row's own box: drop highlighting and the grip find it here,
            # whether or not it is wrapped in a tray piece below.
            row.body = row.get_child()
            if manual:
                self._attach_reorder(row)
            elif row.pinned:
                # No dragging in the automatic order, but the star still says
                # "this one stays on top".
                line = self._status_line(row)
                if line is not None:
                    line.prepend(self._star_label())
            if story_id is not None:
                # A member sits in its story's tray: the existing row box,
                # unchanged inside, wrapped in one piece of the tray.
                body = row.body
                row.set_child(None)
                tray = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                tray.add_css_class("ccs-tray")
                if index == 0:
                    tray.add_css_class("ccs-tray-first")
                if index == count - 1:
                    tray.add_css_class("ccs-tray-last")
                body.add_css_class("ccs-tray-member")
                # The stripe down the tray's left carries the story's status
                # colour, the same as its lid's.
                tray.add_css_class("ccs-st-%s" % self._story_colour.get(story_id, "dim"))
                tray.append(body)
                row.set_child(tray)
                row.add_css_class("ccs-tray-row")
            else:
                # A session in no story: no stripe, a hairline under it, and a
                # bar (not a box) when selected -- never mistaken for a story.
                row.body.add_css_class("ccs-loose")
                row.add_css_class("ccs-loose-row")
            self._attach_menu(row, lambda key=row.item_key: self._session_menu(key))
            self.listbox.append(row)
            self._rows.append(row)
            if owned is not None and owned.terminal is selected:
                self.listbox.select_row(row)
            return row

        for block in blocks:
            if block[0] == "item":
                add_item(block[1])
                continue
            _, story, members = block
            collapsed = self.app.story_collapsed(story["id"])
            # Which member is on screen, if any: a folded lid boxes its square
            # and names it on the pointer line.
            here = next((i for i, (_k, _o, owned) in enumerate(members)
                         if selected is not None and owned is not None
                         and owned.terminal is selected), None)
            header = self._make_story_row(story, members, collapsed,
                                          here if collapsed else None)
            self.listbox.append(header)
            self._rows.append(header)
            header.last_member_row = None
            if not collapsed:
                for index, item in enumerate(members):
                    header.last_member_row = add_item(
                        item, story["id"], index, len(members))
            elif here is not None:
                # The session on screen is folded away inside this story:
                # the story itself stands selected, "you are in here".
                self.listbox.select_row(header)

        if focus_id is not None:
            # Give the focus back to the same row, rebuilt. If that row is
            # gone (its story folded), the session on screen takes it, so
            # typing keeps going where you were.
            target = next((r for r in self._rows if (
                ("story", r.story["id"]) if getattr(r, "story", None) is not None
                else ("item", getattr(r, "item_key", None))) == focus_id), None)
            if target is not None:
                target.grab_focus()
            elif selected is not None:
                selected.grab_focus()

        # Put the scroll back now, and once more after the new rows are laid
        # out (a shorter list may only then have room for it). Low priority
        # runs after GTK's layout pass.
        adj.set_value(keep_scroll)
        self._hold_scroll = keep_scroll
        if not self._hold_queued:
            self._hold_queued = True

            def hold():
                self._hold_queued = False
                if self._hold_scroll is not None:
                    adj.set_value(self._hold_scroll)
                    self._hold_scroll = None
                return False
            GLib.idle_add(hold, priority=GLib.PRIORITY_LOW)

    # -- stories -----------------------------------------------------------

    @staticmethod
    def _item_name(item):
        kind, obj, _owned = item
        if kind == "record":
            return obj.get("name") or ""
        return obj.name or ""

    def _member_state(self, item):
        """One word per member, for the story's summary line."""
        kind, obj, owned = item
        if kind == "record":
            return "dormant"
        if kind == "local":
            if obj.exited:
                return "ended"
            return "terminal" if obj.mode == "shell" else "busy"
        key = item_key(kind, obj, owned)
        if obj.status == "idle" and self.app.is_done(key):
            return "done"
        attention = self.app.attention_kind(obj)
        if obj.status == "waiting" or attention == "blocked":
            return "blocked"
        if attention == "finished":
            return "finished"
        if obj.status == "idle" and self.app.is_parked(key):
            return "parked"
        if obj.status == "busy":
            return "busy"
        if obj.status == "shell":
            # Claude is running a shell command: said apart from "busy", in
            # the same cyan its own row uses.
            return "shell"
        return "idle"

    def _make_story_row(self, story, members, collapsed, here_index=None):
        """A story's header row: its lid (see _build_lid).

        Collapsed or not, the squares and the sum cover every member -- so a
        session waiting on you inside a folded story still lights the lid.
        """
        row = Gtk.ListBoxRow()
        row.entry = None
        row.session = None
        row.story = story
        row.item_key = None
        row.add_css_class("ccs-story-row")

        lid = self._build_lid(story, members, collapsed, here_index)
        row.lid = lid
        row.set_child(lid)

        kind = ("grouped by name" if story.get("virtual") else "story")
        row.set_tooltip_text("%s  (%s)\nClick to %s.  Right-click for more." % (
            story["name"], kind, "expand" if collapsed else "collapse"))

        # The lid's own grip moves the whole story (manual order only). The
        # row itself never gets a drag source: that swallowed clicks.
        if not self.app.auto_sort():
            self._attach_story_grip(lid, story["id"])

        # Drop a session on the lid to put it in this story, as its last
        # member; drop a whole story on it to move that story above this one.
        # Its top edge (and bottom edge, when folded) puts the session beside
        # the story instead.
        self._attach_drop(row, lambda payload, band, sid=story["id"], lid=lid:
                          self._drop_plan(payload, lid_sid=sid, lid_box=lid,
                                          band=band),
                          banded=True)

        self._attach_menu(row, lambda sid=story["id"]: self._story_menu(sid))
        return row

    def _build_lid(self, story, members, collapsed, here_index=None):
        """The raised box at the top of a story.

        Top line: grip slot, arrow, name, "+ add" (only while a session is
        dragged over it), member count, +▾. Second line: one square per
        member in its state colour, then the summary. Third line, only when
        the session on screen is folded away in here: "↑ name · state · age".
        """
        sid = story["id"]
        states = [self._member_state(item) for item in members]
        colour, summary = stories.summarize(states)
        attention = "blocked" in states
        # YOUR mark that the story is finished, like ✓ on a session. Something
        # inside waiting on you still wins: a done story must never hide that.
        done = self.app.story_done(sid) and not attention
        parked = self.app.story_parked(sid) and not attention and not done
        if done:
            colour, summary = "done", "done  ·  " + summary
        elif parked:
            colour, summary = "parked", "waiting  ·  " + summary

        lid = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
        lid.add_css_class("ccs-lid")
        if not collapsed:
            lid.add_css_class("ccs-lid-open")
        if attention:
            lid.add_css_class("ccs-lid-attn")
        if done:
            lid.add_css_class("ccs-lid-done")
        # The stripe on the left says how the story stands: its summary's
        # colour (needs you, finished, busy, idle, done...).
        lid.add_css_class("ccs-st-%s" % colour)
        self._story_colour[sid] = colour

        top = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        arrow = Gtk.Label(label="▸" if collapsed else "▾")
        arrow.add_css_class("ccs-story-arrow")
        top.append(arrow)

        name = Gtk.Label(label=story["name"], xalign=0)
        name.set_hexpand(True)
        name.set_ellipsize(3)
        name.add_css_class("ccs-story-name")
        if attention:
            name.add_css_class("ccs-attention")
        top.append(name)

        hint = Gtk.Label(label="+ add")
        hint.add_css_class("ccs-drop-hint")
        hint.set_visible(False)
        top.append(hint)
        lid.drop_hint = hint

        count = Gtk.Label(label=str(len(members)), xalign=1)
        count.add_css_class("ccs-age")
        count.add_css_class("ccs-story-count")
        top.append(count)

        top.append(self._story_add_button(sid))
        lid.append(top)
        lid.top_line = top

        line = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        line.add_css_class("ccs-lid-line")
        squares = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        squares.add_css_class("ccs-squares")
        squares.set_valign(Gtk.Align.CENTER)
        shown = states[:self.MAX_SQUARES]
        for index, state in enumerate(shown):
            square = Gtk.Label(label="●" if state in ("blocked", "finished") else "■")
            square.add_css_class("ccs-sq")
            square.add_css_class("ccs-dot-%s" % stories.state_colour(state))
            if index == here_index:
                square.add_css_class("ccs-sq-here")
            squares.append(square)
        if len(states) > len(shown):
            more = Gtk.Label(label="+%d" % (len(states) - len(shown)))
            more.add_css_class("ccs-sq-more")
            if here_index is not None and here_index >= len(shown):
                more.add_css_class("ccs-sq-here")
            squares.append(more)
        line.append(squares)

        sub = Gtk.Label(label=summary, xalign=0)
        sub.set_hexpand(True)
        sub.set_ellipsize(3)
        sub.add_css_class("ccs-sub")
        sub.add_css_class("ccs-sub-%s" % colour)
        line.append(sub)
        lid.append(line)

        if here_index is not None and here_index < len(members):
            item = members[here_index]
            pointer = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
            pointer.add_css_class("ccs-pointer")
            text = Gtk.Label(xalign=0)
            text.set_markup("↑ <b>%s</b>  ·  %s" % (
                GLib.markup_escape_text(self._item_name(item)),
                GLib.markup_escape_text(stories.state_word(states[here_index]))))
            text.set_ellipsize(3)
            pointer.append(text)
            kind, obj, owned = item
            age_text = obj.describe_age() if kind == "entry" else ""
            age = Gtk.Label(label=("  ·  " + age_text) if age_text else "", xalign=0)
            pointer.append(age)
            if kind == "entry":
                self._pointer_ages.append((age, item_key(kind, obj, owned)))
            lid.append(pointer)
        return lid

    MAX_SQUARES = 12

    def _story_add_button(self, sid):
        """+▾ on the lid: start a Claude session or a terminal in this story.

        A button inside a row claims its own click, like ✓ and ×, so pressing
        it never folds the story.
        """
        button = Gtk.MenuButton()
        button.add_css_class("ccs-story-add")
        button.set_valign(Gtk.Align.CENTER)
        button.set_tooltip_text("Start something in this story")
        label = Gtk.Label(label="+▾")
        button.set_child(label)
        model = Gio.Menu()
        model.append_section(None, self._story_start_menu(sid))
        model.append_section(None, self._story_done_menu(sid))
        button.set_menu_model(model)
        popover = button.get_popover()
        if popover is not None:
            # Same rule as the right-click menus: the list holds still while
            # the dropdown is open, or a rebuild would destroy it.
            def shown(_pop):
                self._menu_open = True

            def closed(_pop):
                def after():
                    self._menu_open = False
                    # The keyboard goes back to the session on screen, not to
                    # the button, so typing carries on where it was.
                    current = self.stack.get_visible_child()
                    if current is not None:
                        current.grab_focus()
                    self._last_signature = None
                    self.refresh()
                    return False
                GLib.idle_add(after)
            popover.connect("show", shown)
            popover.connect("closed", closed)
        return button

    def _story_start_menu(self, sid):
        name = self._story_name(sid)
        section = Gio.Menu()
        section.append_item(self._menu_item("New Claude session in %s" % name,
                                            "story-start", "%s\tclaude" % sid))
        section.append_item(self._menu_item("New terminal in %s" % name,
                                            "story-start", "%s\tshell" % sid))
        return section

    def _attach_story_grip(self, lid, sid, ghosts=None):
        """Put the grip in front of the lid's top line; it drags the story."""
        grip = Gtk.Label(label="⠿")
        grip.add_css_class("ccs-grip")
        grip.set_tooltip_text("Drag to move the whole story")
        grip.set_valign(Gtk.Align.CENTER)
        source = Gtk.DragSource(actions=Gdk.DragAction.MOVE)
        source.connect("prepare", lambda *_: Gdk.ContentProvider.new_for_value(
            self.STORY_DRAG + sid))

        def faded():
            widgets = [self._lid_of(sid)]
            widgets += [r.get_child() for r in self._rows
                        if getattr(r, "story_id", None) == sid]
            return widgets + (ghosts() if ghosts else [])

        self._watch_drag(source, self.STORY_DRAG + sid,
                         lambda: "%s  ·  %d" % (self._story_name(sid),
                                                len(self._story_members.get(sid, ()))),
                         faded)
        grip.add_controller(source)
        lid.top_line.prepend(grip)

    def _attach_menu(self, row, build):
        """Right-click on a row opens the menu `build()` returns."""
        click = Gtk.GestureClick(button=3)

        def pressed(_gesture, _n, x, y):
            menu = build()
            if menu is None:
                return
            popover = Gtk.PopoverMenu.new_from_model(menu)
            popover.set_has_arrow(False)
            popover.set_parent(row)
            rect = Gdk.Rectangle()
            rect.x, rect.y, rect.width, rect.height = int(x), int(y), 1, 1
            popover.set_pointing_to(rect)
            self._menu_open = True

            def closed(pop):
                # Let the chosen action run first, then let the list move.
                def after():
                    pop.unparent()
                    self._menu_open = False
                    self._last_signature = None
                    self.refresh()
                    return False
                GLib.idle_add(after)

            popover.connect("closed", closed)
            popover.popup()

        click.connect("pressed", pressed)
        row.add_controller(click)

    @staticmethod
    def _menu_item(label, action, value):
        item = Gio.MenuItem.new(label, None)
        item.set_action_and_target_value("win." + action, GLib.Variant("s", value))
        return item

    def _session_menu(self, key):
        if not key:
            return None
        current = self.app.story_of().get(key)
        story_names = {s["id"]: s["name"] for s in self.app.stories()}
        menu = Gio.Menu()

        moves = Gio.Menu()
        for sid, name in story_names.items():
            if sid != current:
                moves.append_item(self._menu_item(
                    "Move to “%s”" % name, "story-add", "%s\t%s" % (key, sid)))
        if moves.get_n_items():
            menu.append_section(None, moves)

        rest = Gio.Menu()
        rest.append_item(self._menu_item("New story with this session…",
                                         "story-new", key))
        if current in story_names:
            rest.append_item(self._menu_item(
                "Take out of “%s”" % story_names[current], "story-remove", key))
        menu.append_section(None, rest)

        if self._story_of_key(key) is not None:
            pin = Gio.Menu()
            pin.append_item(self._menu_item(
                "Unpin from top of story" if key in self.app.pinned_keys()
                else "Pin to top of story", "story-pin", key))
            menu.append_section(None, pin)
        return menu

    def _story_done_menu(self, sid):
        section = Gio.Menu()
        section.append_item(self._menu_item(
            "◷  Stop waiting on story" if self.app.story_parked(sid)
            else "◷  Mark story as waiting", "story-parked", sid))
        section.append_item(self._menu_item(
            "✓  Unmark story done" if self.app.story_done(sid) else "✓  Mark story done",
            "story-done", sid))
        return section

    def _story_done(self, sid):
        self.app.toggle_story_done(sid, self._story_members.get(sid, ()))

    def _story_parked(self, sid):
        self.app.toggle_story_parked(sid, self._story_members.get(sid, ()))

    def _story_menu(self, sid):
        menu = Gio.Menu()
        # The +▾ items on top, as their own sections.
        menu.append_section(None, self._story_start_menu(sid))
        menu.append_section(None, self._story_done_menu(sid))
        rest = Gio.Menu()
        collapsed = self.app.story_collapsed(sid)
        rest.append_item(self._menu_item("Expand" if collapsed else "Collapse",
                                         "story-toggle", sid))
        rest.append_item(self._menu_item("Rename…", "story-rename", sid))
        rest.append_item(self._menu_item(
            "Stop grouping by this name" if stories.is_virtual(sid) else "Ungroup",
            "story-ungroup", sid))
        menu.append_section(None, rest)
        return menu

    def _story_folder(self, sid):
        """Where a session started from a story's +▾ opens: the folder of the
        story's first member (the one its row shows), else the default."""
        items = self._story_items.get(sid) or []
        folder = None
        if items:
            kind, obj, owned = items[0]
            if kind == "record":
                folder = obj.get("cwd")
            elif owned is not None:
                folder = owned.current_cwd()
            else:
                folder = getattr(obj, "cwd", None)
        if folder and os.path.isdir(folder):
            return folder
        return self.app.default_folder()

    def _story_start(self, payload):
        """+▾ → start a session or terminal as the story's last member.

        The order matters (see _on_start_session_action): seat the terminal
        first, THEN touch the story, because changing a story refreshes the
        list and a refresh adopts any unseated terminal.
        """
        sid, _, mode = payload.partition("\t")
        if mode not in ("claude", "shell") or sid not in self._story_members:
            return
        members = list(self._story_members.get(sid, ()))
        folder = self._story_folder(sid)
        sess = self.app.start_session(
            name=self.app.unique_name(default_session_name(folder)),
            cwd=folder, mode=mode)
        self.show_session(sess)
        sid = self.app.add_to_story("local:" + sess.key, sid, members)
        if self.app.story_collapsed(sid):
            self.app.toggle_story(sid)
        self._last_signature = None
        self.refresh()
        sess.terminal.grab_focus()

    def _story_name(self, sid):
        if stories.is_virtual(sid):
            return sid[len(stories.PREFIX_ID):]
        for story in self.app.stories():
            if story["id"] == sid:
                return story["name"]
        return ""

    def _story_new(self, key):
        NameDialog(self, "New story", "Story name", "",
                   lambda name: self.app.create_story(name, [key]),
                   button="Create").present()

    def _story_add(self, payload):
        key, _, sid = payload.partition("\t")
        self.app.add_to_story(key, sid, self._story_members.get(sid, ()))

    def _story_remove(self, key):
        self.app.assign_story(key, None)

    def _story_rename(self, sid):
        members = list(self._story_members.get(sid, ()))
        NameDialog(self, "Rename story", "Story name", self._story_name(sid),
                   lambda name: self.app.rename_story(sid, name, members)).present()

    def _story_ungroup(self, sid):
        self.app.ungroup_story(sid)

    def _story_toggle(self, sid):
        self.app.toggle_story(sid)
        current = self.stack.get_visible_child()
        if current is not None:
            current.grab_focus()

    # -- actions -----------------------------------------------------------

    def _on_row_activated(self, listbox, row):
        if getattr(row, "story", None) is not None:
            # Folding is not navigating: the session on screen stays, and the
            # keyboard goes back to it (the list rebuild has just re-selected
            # its row, or the folded story's header).
            self.app.toggle_story(row.story["id"])
            current = self.stack.get_visible_child()
            if current is not None:
                current.grab_focus()
            return
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

    def cycle_session(self, step):
        """Ctrl+Down / Ctrl+Up: step through the list in its visible order.

        Only rows with a live terminal of ours take part. Dormant rows are
        skipped deliberately -- landing on one would silently RESUME it -- and
        external rows have no terminal here to show.
        """
        rows = [r for r in self._rows if getattr(r, "session", None) is not None]
        if not rows:
            return
        current = self.stack.get_visible_child()
        index = next(
            (i for i, r in enumerate(rows) if r.session.terminal is current), None
        )
        if index is None:
            # Nothing of ours on screen yet: Down enters at the top of the
            # list, Up at the bottom.
            index = 0 if step > 0 else len(rows) - 1
        else:
            index = (index + step) % len(rows)
        self.listbox.select_row(rows[index])
        self.show_session(rows[index].session)

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
        self.drag_payload = None  # what is being dragged right now, if anything
        self._owner_cache = {}    # (pid, proc start) -> (terminal pid, when)
        self._prev_status = {}    # key -> last seen status
        self.config = load_config()

    # -- lifecycle ---------------------------------------------------------

    def do_startup(self):
        Gtk.Application.do_startup(self)
        self._apply_css()
        self._start_freeze_log()

        for name, callback, accels in (
            ("new-window", lambda *_: self.new_window(), ["<Control><Shift>n"]),
            ("new-session", lambda *_: self._new_session_here(), ["<Control><Shift>t"]),
            ("close-session", lambda *_: self.close_current(), ["<Control><Shift>w"]),
            # VTE ships with NO copy/paste bindings -- these belong to the
            # embedding app. Without them Ctrl+Shift+C/V silently did nothing.
            ("copy", lambda *_: self.copy_selection(), ["<Control><Shift>c"]),
            ("paste", lambda *_: self.paste_clipboard(), ["<Control><Shift>v"]),
            ("next-session", lambda *_: self.cycle_session(1), ["<Control>Down"]),
            ("prev-session", lambda *_: self.cycle_session(-1), ["<Control>Up"]),
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

        # The API: start a named session from outside, exactly as the "+"
        # button would (same claude command, same configured flags such as
        # --dangerously-skip-permissions). Parameter is a dict:
        #   name   -- session name (made unique if taken)    default: from folder
        #   cwd    -- folder to start in                     default: Settings folder
        #   prompt -- first message to send it               default: none
        #   mode   -- "claude" or "shell"                    default: "claude"
        #   focus  -- switch the window to it                default: False, so a
        #             script never yanks you out of the session you're typing in
        #   parent -- Claude session id of the session asking  default: none.
        #             The new session joins that session's story (one is
        #             created, named after it, if it has none yet).
        # Use the cc-sessions-start helper rather than raw gdbus.
        action = Gio.SimpleAction.new("start-session", GLib.VariantType.new("a{sv}"))
        action.connect("activate", self._on_start_session_action)
        self.add_action(action)

        # The other half of the API: close a session from outside, exactly as
        # its × button would (the tab ends and is NOT offered back on restart;
        # its transcript stays on disk, so `claude -r` can still resume it).
        # Parameter is a dict:
        #   name       -- session name, as shown in the sidebar
        #   session_id -- Claude session id (wins over name when both given)
        #   force      -- also close a session that is busy or waiting on you
        #                 default: False, so a script cannot kill work in flight
        # Only sessions this app started can be closed. Use the
        # cc-sessions-close helper rather than raw gdbus.
        action = Gio.SimpleAction.new("close-session", GLib.VariantType.new("a{sv}"))
        action.connect("activate", self._on_close_session_action)
        self.add_action(action)

        GLib.timeout_add(1000, self._tick)

    FREEZE_LOG = os.path.expanduser("~/.cache/cc-sessions/freeze.log")
    FREEZE_SECONDS = 4

    def _start_freeze_log(self):
        """Write down what the app was doing whenever it hangs.

        A timer re-arms faulthandler every half second; if the main loop is
        stuck for FREEZE_SECONDS (long enough for "not responding"), the
        timer cannot re-arm it, and faulthandler -- from its own thread --
        appends every thread's Python stack to FREEZE_LOG. Costs nothing
        while the app is healthy.
        """
        try:
            os.makedirs(os.path.dirname(self.FREEZE_LOG), exist_ok=True)
            self._freeze_file = open(self.FREEZE_LOG, "a", encoding="utf-8")
            self._freeze_file.write("\n== started pid %d at %s ==\n" % (
                os.getpid(), time.strftime("%Y-%m-%d %H:%M:%S")))
            self._freeze_file.flush()
        except OSError:
            return

        def rearm():
            faulthandler.dump_traceback_later(
                self.FREEZE_SECONDS, repeat=False, file=self._freeze_file)
            return True
        rearm()
        GLib.timeout_add(500, rearm)

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

    def _on_start_session_action(self, _action, parameter):
        options = parameter.unpack() if parameter is not None else {}
        mode = options.get("mode") or "claude"
        if mode not in ("claude", "shell"):
            print("[cc-sessions] start-session: unknown mode %r" % mode, file=sys.stderr)
            return
        cwd = os.path.expanduser(options.get("cwd") or "") or self.default_folder()
        if not os.path.isdir(cwd):
            print("[cc-sessions] start-session: no such folder %r" % cwd, file=sys.stderr)
            return
        name = self.unique_name(options.get("name") or default_session_name(cwd))

        window = self.get_active_window()
        if not isinstance(window, SessionWindow):
            windows = self.session_windows()
            window = windows[0] if windows else None
        if window is None:
            window = SessionWindow(self)
            window.present()

        sess = self.start_session(name=name, cwd=cwd, mode=mode,
                                  prompt=options.get("prompt") or None)
        if options.get("focus"):
            window.show_session(sess)
        else:
            # Seat it in the window straight away: a live VTE left without a
            # parent segfaults (see SessionWindow.unassigned).
            window.stack.add_named(sess.terminal, sess.key)
        # Only AFTER seating it: joining a story refreshes the list, and a
        # refresh adopts any unseated terminal -- seating it twice.
        parent = options.get("parent")
        if isinstance(parent, str) and parent:
            self.join_parent_story(parent, "local:" + sess.key)
        window._last_signature = None
        window.refresh()

    def _on_close_session_action(self, _action, parameter):
        options = parameter.unpack() if parameter is not None else {}
        session_id = options.get("session_id") or ""
        name = options.get("name") or ""
        if not session_id and not name:
            print("[cc-sessions] close-session: give a name or a session_id",
                  file=sys.stderr)
            return
        live = [s for s in self.sessions if not s.exited]
        if session_id:
            matches = [s for s in live if s.session_id == session_id]
        else:
            matches = [s for s in live if s.name == name]
        if not matches:
            print("[cc-sessions] close-session: no open session %r"
                  % (session_id or name), file=sys.stderr)
            return
        if len(matches) > 1:
            # Names are unique per folder only; closing the wrong twin is worse
            # than closing nothing.
            print("[cc-sessions] close-session: %d sessions are called %r -- "
                  "pass session_id" % (len(matches), name), file=sys.stderr)
            return
        sess = matches[0]
        if (sess.mode == "claude"
                and getattr(sess, "last_status", None) in ("busy", "waiting", "shell")
                and not options.get("force")):
            print("[cc-sessions] close-session: %r is %s -- not closed (force to override)"
                  % (sess.name, sess.last_status), file=sys.stderr)
            return
        self.close_session(sess)

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

    def sidebar_side(self):
        """Which side of the window the session list sits on."""
        return "right" if self.config.get("sidebar_side") == "right" else "left"

    def set_sidebar_side(self, side):
        self.config["sidebar_side"] = "right" if side == "right" else "left"
        save_config(self.config)
        self._apply_css()      # the divider border + padding swap edges
        for window in self.session_windows():
            window.seat_panes(self.sidebar_side())

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

    # -- "done" marks ------------------------------------------------------

    def done_keys(self):
        """Sessions YOU marked finished. Ours entirely -- Claude knows nothing
        of this: it reports `idle` either way. The point is to keep a finished
        session in the list, visibly settled, until the work actually ships.
        """
        keys = self.config.get("done")
        return set(keys) if isinstance(keys, list) else set()

    def is_done(self, key):
        return key in self.done_keys()

    def parked_keys(self):
        """Sessions YOU marked as waiting on something outside them."""
        keys = self.config.get("parked")
        return set(keys) if isinstance(keys, list) else set()

    def is_parked(self, key):
        return key in self.parked_keys()

    def toggle_parked(self, key):
        keys = self.parked_keys()
        keys.discard(key) if key in keys else keys.add(key)
        self.config["parked"] = sorted(keys)
        if key in keys:
            self.config["done"] = sorted(self.done_keys() - {key})
        save_config(self.config)
        for window in self.session_windows():
            window._last_signature = None
            window.refresh()

    def toggle_done(self, key):
        keys = self.done_keys()
        keys.discard(key) if key in keys else keys.add(key)
        self.config["done"] = sorted(keys)
        if key in keys:
            # Done and "waiting on it" exclude each other.
            self.config["parked"] = sorted(self.parked_keys() - {key})
        save_config(self.config)
        for window in self.session_windows():
            window._last_signature = None
            window.refresh()

    def manual_order(self):
        order = self.config.get("manual_order")
        return list(order) if isinstance(order, list) else []

    def extend_order(self, keys):
        """Pin newly seen rows at the bottom. Saves, but does NOT refresh.

        Called from inside a refresh, so triggering another one here would
        recurse.
        """
        order = self.manual_order()
        order.extend(k for k in keys if k not in order)
        self.config["manual_order"] = order
        save_config(self.config)

    def rename_key(self, old_key, new_key):
        """Follow a row whose identity changed, keeping its place and story.

        A session is 'local:session-3' until Claude registers it, then it
        becomes its session id -- and /clear gives it a new id again. Without
        this the row would look brand new: it would jump to the bottom of a
        manual list and fall out of its story.
        """
        if not old_key or not new_key or old_key == new_key:
            return
        changed = False
        order = self.manual_order()
        if old_key in order and new_key not in order:
            order[order.index(old_key)] = new_key
            self.config["manual_order"] = order
            changed = True
        story_of = self.story_of()
        if old_key in story_of and new_key not in story_of:
            story_of[new_key] = story_of.pop(old_key)
            self.config["story_of"] = story_of
            changed = True
        parked = self.parked_keys()
        if old_key in parked and new_key not in parked:
            self.config["parked"] = sorted((parked - {old_key}) | {new_key})
            changed = True
        pinned = self.pinned_keys()
        if old_key in pinned and new_key not in pinned:
            pinned[pinned.index(old_key)] = new_key
            self.config["story_pinned"] = pinned
            changed = True
        if changed:
            save_config(self.config)

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

    def move_block_in_order(self, keys, dst_key, seed_keys=()):
        """Move a whole story: its rows, in their order, to just above dst_key.

        Only reorders. A by-name story is not saved by being moved -- its
        members still share their name, so they still group.
        """
        order = self.manual_order()
        for key in seed_keys:
            if key not in order:
                order.append(key)
        moved = stories.move_block(order, set(keys), dst_key)
        if moved is None:
            return
        self.config["manual_order"] = moved
        save_config(self.config)
        for window in self.session_windows():
            window._last_signature = None
            window.refresh()

    def move_block_to_end(self, keys, seed_keys=()):
        """Move rows (in their order) to the bottom of the stored order."""
        order = self.manual_order()
        for key in seed_keys:
            if key not in order:
                order.append(key)
        wanted = set(keys)
        if not wanted & set(order):
            return
        block = [k for k in order if k in wanted]
        order = [k for k in order if k not in wanted] + block
        self.config["manual_order"] = order
        save_config(self.config)
        for window in self.session_windows():
            window._last_signature = None
            window.refresh()

    def move_after_in_order(self, src_key, after_key, seed_keys=()):
        """Put src_key just below after_key, and remember it."""
        order = self.manual_order()
        for key in seed_keys:
            if key not in order:
                order.append(key)
        if src_key not in order or after_key not in order or src_key == after_key:
            return
        order.remove(src_key)
        order.insert(order.index(after_key) + 1, src_key)
        self.config["manual_order"] = order
        save_config(self.config)
        for window in self.session_windows():
            window._last_signature = None
            window.refresh()

    # -- stories -----------------------------------------------------------
    #
    # Saved in config.json:
    #   stories          [{"id", "name"}]        the stories you have
    #   story_of         {row key: story id}     who is in which
    #   story_collapsed  {story id: true}        folded ones (by-name ones too)
    #   story_by_prefix  bool                    group sessions named alike
    #   story_prefix_off [prefix]                name families you un-grouped

    def stories(self):
        raw = self.config.get("stories")
        if not isinstance(raw, list):
            return []
        return [s for s in raw
                if isinstance(s, dict) and s.get("id") and s.get("name")]

    def story_of(self):
        raw = self.config.get("story_of")
        known = {s["id"] for s in self.stories()}
        if not isinstance(raw, dict):
            return {}
        return {k: v for k, v in raw.items() if v in known}

    def story_collapsed(self, sid):
        raw = self.config.get("story_collapsed")
        return isinstance(raw, dict) and bool(raw.get(sid))

    def story_by_prefix(self):
        return bool(self.config.get("story_by_prefix", True))

    def set_story_by_prefix(self, enabled):
        self.config["story_by_prefix"] = bool(enabled)
        self._stories_changed()

    def ignored_prefixes(self):
        raw = self.config.get("story_prefix_off")
        return [p for p in raw if isinstance(p, str)] if isinstance(raw, list) else []

    def pinned_keys(self):
        """Rows pinned (starred) to the top of whatever story they are in."""
        raw = self.config.get("story_pinned")
        return [k for k in raw if isinstance(k, str)] if isinstance(raw, list) else []

    def _story_marks(self, name):
        raw = self.config.get(name)
        return [s for s in raw if isinstance(s, str)] if isinstance(raw, list) else []

    def story_done(self, sid):
        return sid in self._story_marks("story_done")

    def story_parked(self, sid):
        return sid in self._story_marks("story_parked")

    def _toggle_story_mark(self, name, other, sid, member_keys):
        """Flip one story mark; done and waiting exclude each other."""
        if stories.is_virtual(sid):
            # A by-name story has nothing saved to hang the mark on: save it.
            sid = self.materialize(sid, member_keys)
        marks = self._story_marks(name)
        if sid in marks:
            marks.remove(sid)
        else:
            marks.append(sid)
            self.config[other] = [s for s in self._story_marks(other) if s != sid]
        self.config[name] = marks
        self._stories_changed()

    def toggle_story_done(self, sid, member_keys=()):
        self._toggle_story_mark("story_done", "story_parked", sid, member_keys)

    def toggle_story_parked(self, sid, member_keys=()):
        self._toggle_story_mark("story_parked", "story_done", sid, member_keys)

    def toggle_pinned(self, key):
        if not key:
            return
        pinned = self.pinned_keys()
        if key in pinned:
            pinned.remove(key)
        else:
            pinned.append(key)
        self.config["story_pinned"] = pinned
        self._stories_changed()

    def stories_signature(self):
        """Everything about stories that changes what the list looks like."""
        return json.dumps([self.stories(), self.story_of(), self.pinned_keys(),
                           self.config.get("story_done"),
                           self.config.get("story_parked"),
                           self.config.get("story_collapsed"),
                           self.story_by_prefix(), self.ignored_prefixes()],
                          sort_keys=True)

    def _stories_changed(self):
        save_config(self.config)
        for window in self.session_windows():
            window._last_signature = None
            window.refresh()

    def _save_stories(self, stories_list, story_of):
        # Drop stories nobody is in any more, and their folded flag.
        used = set(story_of.values())
        kept = [s for s in stories_list if s["id"] in used]
        self.config["stories"] = kept
        self.config["story_of"] = story_of
        collapsed = self.config.get("story_collapsed")
        if isinstance(collapsed, dict):
            ids = {s["id"] for s in kept}
            self.config["story_collapsed"] = {
                k: v for k, v in collapsed.items()
                if v and (k in ids or stories.is_virtual(k))
            }
        ids = {s["id"] for s in kept}
        for mark in ("story_done", "story_parked"):
            marks = self.config.get(mark)
            if isinstance(marks, list):
                self.config[mark] = [k for k in marks if k in ids]

    def create_story(self, name, keys):
        sid = "s%d" % int(time.time() * 1000)
        while any(s["id"] == sid for s in self.stories()):
            sid += "x"
        story_of = self.story_of()
        for key in keys:
            if key:
                story_of[key] = sid
        self._save_stories(self.stories() + [{"id": sid, "name": name}], story_of)
        self._stories_changed()
        return sid

    def materialize(self, sid, member_keys, name=None):
        """Turn a by-name story into a saved one, members and fold included.

        Needed as soon as you touch a by-name story: rename it, or drop
        something into it that does not share its name.
        """
        if not stories.is_virtual(sid):
            return sid
        collapsed = self.story_collapsed(sid)
        new_sid = self.create_story(name or sid[len(stories.PREFIX_ID):],
                                    list(member_keys))
        if collapsed:
            self.config.setdefault("story_collapsed", {})[new_sid] = True
        self._stories_changed()
        return new_sid

    def assign_story(self, key, sid):
        """Put one row in a saved story, or (sid None) take it out."""
        if not key:
            return
        story_of = self.story_of()
        if sid:
            story_of[key] = sid
        else:
            story_of.pop(key, None)
        self._save_stories(self.stories(), story_of)
        self._stories_changed()

    def add_to_story(self, key, sid, member_keys=()):
        """Put key in story sid. Returns the story id actually used: a by-name
        story gets a new id when it is saved by this."""
        if stories.is_virtual(sid):
            sid = self.materialize(sid, member_keys)
        self.assign_story(key, sid)
        return sid

    def follow_story(self, src_key, dst_key, members_by_story):
        """A row dragged onto another takes on that row's saved story."""
        story_of = self.story_of()
        dst_sid = story_of.get(dst_key)
        if dst_sid is None:
            for sid, keys in members_by_story.items():
                if dst_key in keys and stories.is_virtual(sid):
                    if src_key not in keys:
                        dst_sid = self.materialize(sid, keys)
                    break
        if dst_sid is not None:
            if story_of.get(src_key) != dst_sid:
                self.assign_story(src_key, dst_sid)
        elif src_key in story_of:
            self.assign_story(src_key, None)

    def rename_story(self, sid, name, member_keys=()):
        if stories.is_virtual(sid):
            self.materialize(sid, member_keys, name=name)
            return
        updated = [dict(s, name=name) if s["id"] == sid else s
                   for s in self.stories()]
        self.config["stories"] = updated
        self._stories_changed()

    def ungroup_story(self, sid):
        """Let a story's sessions go back to being loose rows."""
        if stories.is_virtual(sid):
            prefix = sid[len(stories.PREFIX_ID):]
            off = self.ignored_prefixes()
            if prefix not in off:
                off.append(prefix)
            self.config["story_prefix_off"] = off
            self._stories_changed()
            return
        story_of = {k: v for k, v in self.story_of().items() if v != sid}
        self._save_stories(self.stories(), story_of)
        self._stories_changed()

    def toggle_story(self, sid):
        collapsed = self.config.get("story_collapsed")
        if not isinstance(collapsed, dict):
            collapsed = {}
        if collapsed.get(sid):
            collapsed.pop(sid, None)
        else:
            collapsed[sid] = True
        self.config["story_collapsed"] = collapsed
        self._stories_changed()

    def forget_key(self, key):
        pinned = self.pinned_keys()
        if key in pinned:
            pinned.remove(key)
            self.config["story_pinned"] = pinned
            save_config(self.config)
        story_of = self.story_of()
        if key in story_of:
            story_of.pop(key)
            self._save_stories(self.stories(), story_of)
            save_config(self.config)

    def join_parent_story(self, parent_id, child_key):
        """A session started by another one joins the starter's story.

        If the starter is not in a saved story yet, one is made for it, named
        after it, with the starter as its first member -- so an orchestrator
        and everything it starts fold away together.
        """
        story_of = self.story_of()
        sid = story_of.get(parent_id)
        if sid is None:
            name = None
            for sess in self.sessions:
                if sess.session_id == parent_id:
                    name = sess.name
                    break
            if name is None:
                for entry in roster.read():
                    if entry.session_id == parent_id:
                        name = entry.name
                        break
            sid = self.create_story(name or "story", [parent_id])
        self.assign_story(child_key, sid)

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

    def start_session(self, name, cwd, mode="claude", resume_id=None, prompt=None):
        sess = TerminalSession(
            self.theme, cwd=cwd, name=name, mode=mode,
            command=self.claude_command(), extra_args=self.claude_args(),
            zoom=self.terminal_zoom(), resume_id=resume_id, prompt=prompt,
        )
        sess.spawn()
        self.sessions.append(sess)
        self.persist()
        return sess

    OWNER_TTL = 10.0     # seconds a "which terminal runs this pid" answer is kept

    def session_for_entry(self, entry):
        """The terminal a roster entry runs in, or None.

        Same answer as asking every session's matches(), but much cheaper.
        That asked each session in turn, and every miss climbed the entry's
        whole process tree in /proc -- 25 entries x 23 terminals came to
        ~1,250 file reads per pass, and the app makes several passes a second
        (tick, attention, list rebuild). That kept the main thread busy and
        made scrolling and folding stutter, worse on a loaded machine. Now an
        entry's own pid is checked first, then its parent chain is walked
        ONCE, and the answer is kept for a few seconds (a running process does
        not change parents).
        """
        pid = entry.pid
        if not isinstance(pid, int):
            return None
        by_pid = {s.pid: s for s in self.sessions if s.pid is not None}
        sess = by_pid.get(pid)
        if sess is not None:
            return sess
        cache_key = (pid, entry.proc_start)
        now = time.monotonic()
        cached = self._owner_cache.get(cache_key)
        if cached is not None and now - cached[1] < self.OWNER_TTL:
            owner = cached[0]
        else:
            owner = None
            current = pid
            for _ in range(12):            # same depth limit as _has_ancestor
                current = _parent_pid(current)
                if current is None or current <= 1:
                    break
                if current in by_pid:
                    owner = current
                    break
            if len(self._owner_cache) > 500:
                self._owner_cache = {k: v for k, v in self._owner_cache.items()
                                     if now - v[1] < self.OWNER_TTL}
            self._owner_cache[cache_key] = (owner, now)
        return by_pid.get(owner) if owner is not None else None

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
            # A session you have marked done should not keep announcing that it
            # finished -- you already know; that is why you marked it.
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
                elif (entry.status == "idle" and previous in ("busy", "shell")
                      and not self.is_done(key)):
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
        # Closed on purpose: it leaves its story (and an emptied story goes).
        self.forget_key(item_key("local", sess, sess))
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

    def cycle_session(self, step):
        window = self.get_active_window()
        if isinstance(window, SessionWindow):
            window.cycle_session(step)

    def current_terminal(self):
        """The terminal on screen in the focused window, if any."""
        window = self.get_active_window()
        if not isinstance(window, SessionWindow):
            return None
        current = window.stack.get_visible_child()
        for sess in self.sessions:
            if sess.terminal is current:
                return sess.terminal
        return None

    def copy_selection(self):
        terminal = self.current_terminal()
        if terminal is not None and terminal.get_has_selection():
            # TEXT, not HTML: pasting into an editor should give the characters
            # on screen, not markup dressed up with the terminal's colours.
            terminal.copy_clipboard_format(Vte.Format.TEXT)

    def paste_clipboard(self):
        terminal = self.current_terminal()
        if terminal is not None:
            terminal.paste_clipboard()

    def clear_attention_for(self, sess):
        """Clear the flag the moment a session is opened, not a tick later."""
        for entry in roster.read():
            if self.session_for_entry(entry) is sess:
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
                if entry.session_id and entry.session_id != sess.session_id:
                    # Its row identity is about to change; carry its manual
                    # position and its story across.
                    self.rename_key(sess.session_id or ("local:" + sess.key),
                                    entry.session_id)
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
                window.update_ages(entries)
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
               flush against the divider line -- whichever side that is. */
            .ccs-sidebar {{ {divider_edge}: 1px solid {line}; {divider_pad}: 4px; }}
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
            .ccs-row {{ padding: 7px 12px; }}
            .ccs-dot {{ font-size: {s_dot}; }}
            .ccs-name {{ color: {fg}; font-family: {font}; font-size: {s_name}; }}
            .ccs-attention {{ color: {attn}; font-weight: bold; }}
            .ccs-external {{ color: {dim}; }}
            .ccs-age, .ccs-path, .ccs-sub {{
                color: {dim}; font-family: {font}; font-size: {s_meta};
            }}
            .ccs-grip {{
                color: {dim}; font-family: {font}; font-size: {s_name};
                opacity: 0.5; padding: 0 2px;
            }}
            .ccs-grip:hover {{ opacity: 1; }}
            .ccs-grip.ccs-star {{ color: {fg}; opacity: 0.95; }}
            .ccs-done-btn {{
                background: transparent; border: 0; box-shadow: none;
                color: {dim}; font-family: {font}; padding: 0 4px;
                min-width: 0; min-height: 0; opacity: 0.45;
            }}
            .ccs-done-btn:hover {{ opacity: 1; background: transparent; }}
            .ccs-done-on {{ color: {done}; opacity: 1; }}
            .ccs-parked-on {{ color: {parked}; opacity: 1; }}
            .ccs-close {{
                background: transparent; border: 0; box-shadow: none;
                color: {dim}; font-family: {font}; padding: 0 4px;
                min-width: 0; min-height: 0; opacity: 0.55;
            }}
            .ccs-close:hover {{ color: {attn}; opacity: 1; background: transparent; }}
            .ccs-story-arrow {{
                color: {fg}; font-family: {font}; font-size: {s_name};
                min-width: 1em;
            }}
            /* How many sessions the story holds: the timestamps' size, in a
               lighter grey so it can be read at a glance. */
            .ccs-lid .ccs-story-count {{ color: mix({dim}, {fg}, 0.6); }}
            .ccs-story-name {{
                color: {fg}; font-family: {font}; font-size: {s_lid};
                font-weight: bold;
            }}
            /* Rows that hold a lid or a tray piece paint nothing themselves. */
            .ccs-list > row.ccs-story-row,
            .ccs-list > row.ccs-story-row:selected,
            .ccs-list > row.ccs-tray-row:selected {{ background: transparent; }}

            /* The lid: a story's raised header box. */
            .ccs-lid {{
                margin: 4px 5px; padding: 8px 8px 7px 6px;
                background: mix({bg}, {fg}, 0.10);
                border: 1px solid alpha({fg}, 0.17);
                border-top-color: alpha({fg}, 0.30);
                border-left: 4px solid {dim};
                border-radius: 6px;
            }}
            .ccs-lid-open {{
                margin-bottom: 0; border-radius: 6px 6px 0 0;
                border-bottom-color: alpha({fg}, 0.10);
            }}
            .ccs-lid-attn {{
                background: mix({bg}, {attn}, 0.12);
                border-color: alpha({attn}, 0.45);
            }}
            .ccs-lid-attn .ccs-story-arrow,
            .ccs-story-name.ccs-attention {{ color: {attn}; }}
            .ccs-list > row.ccs-story-row:selected > .ccs-lid,
            .ccs-lid-drop {{
                background: mix({bg}, {sel}, 0.22); border-color: {sel};
            }}
            .ccs-lid .ccs-grip {{ color: {fg}; opacity: 0.85; }}
            .ccs-lid-line {{ margin-left: 3px; }}

            /* +▾ on the lid (a MenuButton draws an inner button node). */
            .ccs-story-add > button {{
                background: transparent; box-shadow: none;
                min-width: 22px; min-height: 18px; padding: 0 3px;
                border: 1px solid alpha({fg}, 0.25); border-radius: 4px;
                color: {fg}; opacity: 0.85;
                font-family: {font}; font-size: {s_add};
            }}
            .ccs-story-add > button:hover {{
                background: alpha({sel}, 0.18); opacity: 1;
            }}
            .ccs-story-add > button:checked {{
                background: alpha({sel}, 0.45); border-color: {sel}; opacity: 1;
            }}

            /* One square per member (they also get .ccs-dot-<colour>), and
               the pointer line naming the session folded away in here. */
            .ccs-sq {{ font-family: {font}; font-size: {s_sq}; }}
            .ccs-sq-here {{
                outline: 1px solid {fg}; outline-offset: 1px; border-radius: 1px;
            }}
            .ccs-sq-more {{
                color: {dim}; font-family: {font}; font-size: {s_tag};
                margin-left: 2px;
            }}
            .ccs-pointer {{
                color: {fg}; font-family: {font}; font-size: {s_ptr};
                margin-left: 10px;
            }}

            /* The tray: a story's members, in a darker box under its lid. */
            /* The tray: a story's members, continuing its lid as one card,
               with the same status stripe down the left. */
            .ccs-tray {{
                margin: 0 5px; padding-left: 0;
                background: mix({bg}, {fg}, 0.055);
                border-left: 4px solid {dim};
                border-right: 1px solid alpha({fg}, 0.17);
            }}
            .ccs-tray-last {{
                margin-bottom: 10px; padding-bottom: 4px;
                border-bottom: 1px solid alpha({fg}, 0.17);
                border-radius: 0 0 6px 6px;
            }}
            .ccs-row.ccs-tray-member {{
                padding: 6px 10px 6px 18px;
                border-top: 1px solid alpha({fg}, 0.07);
            }}
            /* A loose session: flat, a hairline under it, a bar when chosen. */
            /* The hairline belongs to the row's outer box and is drawn
               shorter than the row, so it never sticks out past a rounded
               highlight; the highlight sits 2px clear of it above and below. */
            .ccs-row.ccs-loose {{ margin: 2px 5px; }}
            .ccs-list > row.ccs-loose-row {{
                background-image: linear-gradient(alpha({fg}, 0.12), alpha({fg}, 0.12));
                background-size: calc(100% - 34px) 1px;
                background-position: center bottom;
                background-repeat: no-repeat;
            }}
            .ccs-list > row:selected {{ background-color: transparent; }}
            .ccs-list > row:selected > .ccs-loose {{
                background: alpha({sel}, 0.18);
                border-radius: 6px;   /* the same corners as a story card */
            }}
            .ccs-tray-first > .ccs-tray-member {{ border-top-width: 0; }}
            .ccs-list > row.ccs-tray-row:selected .ccs-tray-member {{
                background: alpha({sel}, 0.18);
            }}

            /* Drag and drop. */
            .ccs-drop-before {{ box-shadow: inset 0 2px 0 0 {sel}; }}
            .ccs-drop-after {{ box-shadow: inset 0 -2px 0 0 {sel}; }}
            .ccs-list-end {{ min-height: 28px; background: transparent; }}
            .ccs-drop-hint {{ color: {sel}; font-family: {font}; font-size: {s_ptr}; }}
            .ccs-drop-tag {{
                color: {sel}; background: {bg}; border: 1px solid {sel};
                border-radius: 3px; padding: 0 4px;
                font-family: {font}; font-size: {s_tag};
            }}
            .ccs-dragging {{ opacity: 0.28; }}
            /* GTK's own drop-target frame would draw a box around the whole
               row; the line, the lit lid and the tag say it instead. */
            .ccs-list > row:drop(active),
            .ccs-lid:drop(active):not(.ccs-drop-before):not(.ccs-drop-after) {{
                box-shadow: none; outline-width: 0;
            }}
            """.format(
                bg=theme.bg_css,
                fg=theme.fg_css,
                dim=colour_for(theme, "dim"),
                line=colour_for(theme, "dim"),
                sel=colour_for(theme, "busy"),
                attn=colour_for(theme, "waiting"),
                done=colour_for(theme, "done"),
                parked=colour_for(theme, "parked"),
                font=theme.font_family_css,
                s_header=rem(0.75), s_dot=rem(0.62),
                s_name=rem(0.92), s_meta=rem(0.78),
                s_lid=rem(0.98), s_sq=rem(0.56), s_ptr=rem(0.70),
                s_tag=rem(0.66), s_add=rem(0.82),
                divider_edge=("border-left" if self.sidebar_side() == "right"
                              else "border-right"),
                divider_pad=("padding-left" if self.sidebar_side() == "right"
                             else "padding-right"),
            )
        ]
        for name in COLOUR_NAMES:
            value = colour_for(theme, name)
            parts.append(".ccs-dot-%s { color: %s; }" % (name, value))
            parts.append(".ccs-sub-%s { color: %s; }" % (name, value))
            # The status stripe wins over the selected look, so a folded story
            # holding the session on screen still says how it stands.
            parts.append(".ccs-lid.ccs-st-%s, .ccs-tray.ccs-st-%s, "
                         ".ccs-list > row.ccs-story-row:selected > .ccs-lid.ccs-st-%s "
                         "{ border-left-color: %s; }" % (name, name, name, value))

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
