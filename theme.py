"""Read the user's GNOME Terminal profile and translate it into VTE settings.

The point of this module: the terminal in cc-sessions should not merely *look
like* the user's GNOME Terminal, it should BE it. Both are VTE; GNOME Terminal
stores its appearance in gsettings, and every one of those keys maps onto a
VTE setter. So we read the profile at startup and apply it verbatim.
"""

import gi

gi.require_version("Gdk", "4.0")

from gi.repository import Gdk, Gio, Pango

PROFILE_LIST_SCHEMA = "org.gnome.Terminal.ProfilesList"
PROFILE_SCHEMA = "org.gnome.Terminal.Legacy.Profile"
PROFILE_PATH = "/org/gnome/terminal/legacy/profiles:/:{uuid}/"
INTERFACE_SCHEMA = "org.gnome.desktop.interface"

# Fallbacks match the user's profile "aljaz" (JetBrains Darcula) so the app
# still looks right if gnome-terminal is not installed on the machine.
FALLBACK_FG = "rgb(169,183,198)"
FALLBACK_BG = "rgb(43,43,43)"
FALLBACK_FONT = "Monospace 12"
FALLBACK_PALETTE = [
    "rgb(23,20,33)", "rgb(192,28,40)", "rgb(38,162,105)", "rgb(162,115,76)",
    "rgb(18,72,139)", "rgb(163,71,186)", "rgb(42,161,179)", "rgb(208,207,204)",
    "rgb(94,92,100)", "rgb(246,97,81)", "rgb(51,218,122)", "rgb(233,173,12)",
    "rgb(42,123,222)", "rgb(192,97,203)", "rgb(51,199,222)", "rgb(255,255,255)",
]


def _schema_exists(schema_id):
    source = Gio.SettingsSchemaSource.get_default()
    return source is not None and source.lookup(schema_id, True) is not None


def _rgba(spec):
    """Parse a gsettings colour string like 'rgb(43,43,43)' into a Gdk.RGBA."""
    colour = Gdk.RGBA()
    if spec and colour.parse(spec):
        return colour
    return None


class TerminalTheme:
    """A snapshot of the user's terminal appearance, ready to apply to a VTE."""

    def __init__(self):
        self.foreground = _rgba(FALLBACK_FG)
        self.background = _rgba(FALLBACK_BG)
        self.palette = [_rgba(c) for c in FALLBACK_PALETTE]
        self.font = Pango.FontDescription.from_string(FALLBACK_FONT)
        self.cell_height_scale = 1.0
        self.cell_width_scale = 1.0
        self.scrollback_lines = 10000
        self.bold_is_bright = False
        self.source = "fallback"

    # -- reading -----------------------------------------------------------

    def _adopt_system_monospace(self):
        """Upgrade the hardcoded fallback font to the desktop's monospace font.

        Without gnome-terminal installed there is no profile to read, but the
        desktop-wide monospace font is still the user's own choice -- generic
        "Monospace" is not. The difference is visible: on a machine where the
        generic alias resolved to DejaVu Sans Mono, that font's two-story 'i'
        (serif flag + dot) reads as 'ï' at sidebar sizes, so "finished"
        displayed as "fïnïshed".
        """
        if not _schema_exists(INTERFACE_SCHEMA):
            return
        try:
            font_string = Gio.Settings.new(INTERFACE_SCHEMA).get_string(
                "monospace-font-name"
            )
        except Exception:
            return
        if font_string:
            self.font = Pango.FontDescription.from_string(font_string)
            self.source = "system monospace"

    @classmethod
    def load(cls):
        theme = cls()
        # Baseline first, so EVERY early return below (no gnome-terminal, no
        # default profile, unreadable profile) still gets the user's font.
        theme._adopt_system_monospace()
        if not _schema_exists(PROFILE_LIST_SCHEMA) or not _schema_exists(PROFILE_SCHEMA):
            return theme

        try:
            uuid = Gio.Settings.new(PROFILE_LIST_SCHEMA).get_string("default")
        except Exception:
            return theme
        if not uuid:
            return theme

        try:
            profile = Gio.Settings.new_with_path(
                PROFILE_SCHEMA, PROFILE_PATH.format(uuid=uuid)
            )
        except Exception:
            return theme

        theme.source = profile.get_string("visible-name") or uuid

        # Colours. use-theme-colors means "inherit from the GTK theme"; in that
        # case gnome-terminal ignores the stored fg/bg, so we do too.
        if not profile.get_boolean("use-theme-colors"):
            fg = _rgba(profile.get_string("foreground-color"))
            bg = _rgba(profile.get_string("background-color"))
            if fg:
                theme.foreground = fg
            if bg:
                theme.background = bg

        palette = [_rgba(c) for c in profile.get_strv("palette")]
        palette = [c for c in palette if c is not None]
        if len(palette) == 16:
            theme.palette = palette

        # Font. use-system-font means the profile's own 'font' key is ignored
        # in favour of the desktop-wide monospace font.
        font_string = None
        if profile.get_boolean("use-system-font"):
            if _schema_exists(INTERFACE_SCHEMA):
                font_string = Gio.Settings.new(INTERFACE_SCHEMA).get_string(
                    "monospace-font-name"
                )
        else:
            font_string = profile.get_string("font")
        if font_string:
            theme.font = Pango.FontDescription.from_string(font_string)

        for key, attr in (
            ("cell-height-scale", "cell_height_scale"),
            ("cell-width-scale", "cell_width_scale"),
        ):
            try:
                setattr(theme, attr, profile.get_double(key))
            except Exception:
                pass

        try:
            theme.bold_is_bright = profile.get_boolean("bold-is-bright")
        except Exception:
            pass

        try:
            if profile.get_boolean("scrollback-unlimited"):
                theme.scrollback_lines = -1
            else:
                theme.scrollback_lines = profile.get_int("scrollback-lines")
        except Exception:
            pass

        return theme

    # -- applying ----------------------------------------------------------

    def scaled_font(self, zoom=1.0):
        """A copy of the profile font, scaled by `zoom`."""
        font = self.font.copy()
        size = font.get_size()
        if size <= 0 or zoom == 1.0:
            return font
        if font.get_size_is_absolute():
            font.set_absolute_size(int(size * zoom))
        else:
            font.set_size(int(size * zoom))
        return font

    def apply(self, terminal, zoom=1.0):
        """Apply this theme to a Vte.Terminal."""
        terminal.set_colors(self.foreground, self.background, self.palette)
        terminal.set_font(self.scaled_font(zoom))
        terminal.set_cell_height_scale(self.cell_height_scale)
        terminal.set_cell_width_scale(self.cell_width_scale)
        terminal.set_scrollback_lines(self.scrollback_lines)
        terminal.set_bold_is_bright(self.bold_is_bright)

        # GNOME Terminal never switches sixel on, even though VTE supports it.
        # We do -- inline images in the terminal, for free, if the distro build
        # has it compiled in. Harmless no-op if not.
        try:
            terminal.set_enable_sixel(True)
        except Exception:
            pass

    # -- for the sidebar CSS ----------------------------------------------

    def css_colour(self, rgba):
        return "rgb(%d,%d,%d)" % (
            round(rgba.red * 255),
            round(rgba.green * 255),
            round(rgba.blue * 255),
        )

    @property
    def bg_css(self):
        return self.css_colour(self.background)

    @property
    def fg_css(self):
        return self.css_colour(self.foreground)

    def palette_css(self, index):
        return self.css_colour(self.palette[index])

    @property
    def font_family_css(self):
        family = self.font.get_family()
        return family if family else "monospace"
