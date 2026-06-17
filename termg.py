#!/usr/bin/env python3
"""
termg - a tabbed & tiling terminal emulator for Linux.

Built on GTK 3 + VTE 2.91 (the same terminal engine GNOME Terminal and Tilix
use), so each tab is a genuine PTY-backed shell. It is not Mint-specific; it
runs on any Linux with GTK 3, VTE 2.91 and PyGObject (see README for per-distro
package names).

Features
  * Real terminal tabs and an optional tiled layout (split view), with an
    always-visible tab switcher that works in both modes.
  * Open / run / edit scripts, a resizable file-tree browser, a searchable
    command-history journal with timestamps, paste line-by-line, screenshots,
    and a self-contained light/dark theme.

Requires (Debian / Ubuntu / Mint):
    sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91
Run:
    python3 termg.py

Architecture (one file, two classes)
  Session
      One shell = one `Vte.Terminal`. The reparentable unit is `Session.frame`
      (terminal + its header strip); the *same* frame widget is moved between
      layouts rather than recreated, so a shell is never killed by switching
      views. Each Session also keeps its own scrollback-derived command history
      and the small state machine that reconstructs typed commands from VTE's
      "commit" (keystroke) stream.

  Termg
      The application window and all UI. Key pieces:
        * Layout: `rebuild_layout()` reparents every Session frame into either a
          `Gtk.Notebook` (tabbed; the notebook's own tab strip is hidden) or a
          recursively split tree of `Gtk.Paned` (tiled, rows-first). A custom
          always-visible tab-switcher row drives selection in both modes.
        * Panels: a left file-tree pane and a right history pane, each in a
          `Gtk.Paned` so they have draggable width dividers.
        * Theming: `_apply_chrome_theme()` writes one self-contained CSS block
          scoped to the `.tt-root` class, so the app themes itself fully and
          does not depend on (or get broken by) the system GTK theme.
        * Command history is reconstructed from keystrokes, so it is inherently
          approximate for Tab-completed commands (documented in the README).
"""

import os
import sys
import re
import json
import shlex
import shutil
import datetime
import subprocess
from urllib.parse import urlparse, unquote

import gi
gi.require_version("Gtk", "3.0")
gi.require_version("Vte", "2.91")
from gi.repository import Gtk, Gdk, GLib, Pango, Vte  # noqa: E402

APP_NAME = "termg"
__version__ = "0.4.2"

# URLs in terminal output, for Ctrl+click to open. PCRE2 syntax (compiled by VTE).
URL_PATTERN = (r"(https?|ftp|file)://[^\s<>\"'`|()\[\]{}]+"
               r"|www\.[^\s<>\"'`|()\[\]{}]+")
_PCRE2_MULTILINE = 0x00000400
_PCRE2_CASELESS = 0x00000008

# ----------------------------------------------------------------------------
# Colour themes.  16-entry palettes (verified against VTE 0.76's set_colors).
# ----------------------------------------------------------------------------
THEMES = {
    "dark": {
        "fg": "#c5cbd6", "bg": "#1b1e24", "cursor": "#5aa7ff",
        "palette": [
            "#2b303b", "#e06c75", "#98c379", "#d8a657",
            "#61afef", "#c678dd", "#56b6c2", "#abb2bf",
            "#4b5364", "#ef7b85", "#a7d28b", "#e5c07b",
            "#74bbff", "#d28ee6", "#6bd0db", "#ffffff",
        ],
        # GTK chrome
        "chrome_bg": "#13151a", "chrome_fg": "#c5cbd6",
        "panel_bg": "#1b1e24", "accent": "#5aa7ff",
        "border": "#2b303b", "header_bg": "#23272f",
        "sel_bg": "#2f3a4d", "sel_fg": "#ffffff",
        "btn_bg": "#262b34", "btn_hover": "#313845", "btn_fg": "#c5cbd6",
        "entry_bg": "#0f1115", "muted_fg": "#8b93a3",
        "prefer_dark": True,
    },
    "light": {
        "fg": "#23272e", "bg": "#fbfbf9", "cursor": "#1565c0",
        "palette": [
            "#3b4252", "#b00020", "#2e7d32", "#8a6d00",
            "#1565c0", "#8e24aa", "#00838f", "#cfd2d6",
            "#5a6270", "#c62828", "#2e7d32", "#a37500",
            "#1976d2", "#9c27b0", "#0097a7", "#1f2328",
        ],
        "chrome_bg": "#eceae5", "chrome_fg": "#23272e",
        "panel_bg": "#f4f2ec", "accent": "#1565c0",
        "border": "#d9d6cd", "header_bg": "#e6e3db",
        "sel_bg": "#cfe0f5", "sel_fg": "#10243a",
        "btn_bg": "#e3e0d8", "btn_hover": "#d8d4ca", "btn_fg": "#23272e",
        "entry_bg": "#ffffff", "muted_fg": "#6b7280",
        "prefer_dark": False,
    },
}

TIME_FMT = "%H:%M:%S"   # history column shows the time of day only

CONFIG_DIR = os.path.join(
    os.environ.get("XDG_CONFIG_HOME",
                   os.path.join(os.path.expanduser("~"), ".config")),
    "termg")
CONFIG_FILE = os.path.join(CONFIG_DIR, "settings.json")
HISTORY_FILE = os.path.join(CONFIG_DIR, "history.jsonl")

DEFAULT_SETTINGS = {
    "theme": "dark",
    "editor": "nano",            # command used for "Edit"
    "editor_in_terminal": True,  # nano/vim/micro run inside the terminal
    "show_hidden": True,         # file tree shows dotfiles by default
    "screenshot_cmd": "",        # blank = auto (built-in, then detect a tool)
    "font_scale": 1.0,           # remembered terminal font zoom
    "restore_session": True,     # reopen last session's tabs/layout on launch
    "persist_history": True,      # remember command history between sessions
    "redact_secrets": False,     # scrub inline secrets from history (opt-in)
    "session": None,             # snapshot of last session (tabs + layout)
    "show_tree": False,          # file-tree panel open on launch
    "show_history": False,       # history panel open on launch
    "show_clipboard": False,     # clipboard panel open on launch
    "tree_width": 300,           # remembered file-tree panel width
    "history_width": 380,        # remembered history panel width
}

# External screenshot tools tried (in order) when the built-in X11 capture is
# unavailable, e.g. on Wayland. {path} is substituted with the output file.
SCREENSHOT_TOOLS = [
    ("gnome-screenshot", "gnome-screenshot -w -f {path}"),
    ("spectacle",        "spectacle -a -b -n -o {path}"),
    ("xfce4-screenshooter", "xfce4-screenshooter -w -s {path}"),
    ("grim",             "grim {path}"),
    ("flameshot",        "flameshot full -p {path}"),
    ("scrot",            "scrot -u {path}"),
    ("import",           "import -window root {path}"),
]


def rgba(hex_str):
    c = Gdk.RGBA()
    c.parse(hex_str)
    return c


# ----------------------------------------------------------------------------
# A single terminal session (one shell). The `frame` widget is the unit that
# gets re-parented when switching between tabbed and tiled layouts.
# ----------------------------------------------------------------------------
class Session:
    """One shell: a `Vte.Terminal` plus the widgets and state tied to it.

    `frame` (the terminal and its header strip) is the unit that gets
    re-parented when switching between tabbed and tiled layouts, so the live
    shell survives layout changes. `history` holds (datetime, command) pairs
    reconstructed from the terminal's "commit" keystroke stream, for which
    `input_buffer`/`in_escape` are the parser state.
    """

    def __init__(self, number):
        self.number = number
        self.title = ""               # last VTE window title
        self.custom_name = None       # user-assigned name (overrides auto)
        self.pinned = False
        self.cwd = None               # last known working directory (for restore)
        self.terminal = None
        self.scroller = None
        self.frame = None
        self.header = None
        self.header_label = None
        self.name_label = None        # label inside the tab-bar button
        self.tab_button = None        # the tab-bar button itself
        # command history: list of (datetime, command_str)
        self.history = []
        # state for reconstructing typed commands from the "commit" stream
        self.input_buffer = ""
        self.in_escape = False
        # per-line state: whether the current input line has begun, and whether
        # it should be suppressed (a password prompt) so it isn't recorded
        self.line_active = False
        self.suppress_line = False
        # pid of this tab's shell, used to tell when the shell (vs. a program
        # like sudo) is the foreground process, so passwords aren't recorded
        self.shell_pid = None

    def set_header_visible(self, visible):
        if self.header is not None:
            self.header.set_visible(visible)
            self.header.set_no_show_all(not visible)


# ----------------------------------------------------------------------------
# Main application window.
# ----------------------------------------------------------------------------
class Termg:
    """The application window and all of its UI and behaviour.

    Owns the list of `Session`s, builds the toolbar / tab-switcher / panels,
    and reparents session frames into a `Gtk.Notebook` (tabbed) or a tree of
    `Gtk.Paned` (tiled) via `rebuild_layout()`. See the module docstring for the
    overall architecture.
    """

    def __init__(self):
        self.sessions = []
        self.active_session = None
        self.term_counter = 0
        self.tiled = False
        self.settings = self._load_settings()
        self.theme_name = self.settings.get("theme", "dark")
        if self.theme_name not in THEMES:
            self.theme_name = "dark"
        self.layout_widget = None
        self.paste_queue = []
        self._status_timeout = 0
        self._switching = False
        self._tabbar_updating = False
        self._tree_refresh_pending = False   # debounce for auto-refresh on cd
        # global, cross-session command history (persisted to HISTORY_FILE)
        self.cmd_history = []
        self._history_cap = 5000             # entries kept in memory / the panel
        self._history_file_max = 12000       # compact the file above this
        # clipboard history (in-memory only; never written to disk)
        self._clip_capturing = False
        self._clip_last = None
        self._clip_max = 200
        self._clip_query = ""
        self.tree_root = os.path.expanduser("~")
        self._tree_width = int(self.settings.get("tree_width", 300) or 300)
        self._history_width = int(self.settings.get("history_width", 380) or 380)
        self._font_scale = float(self.settings.get("font_scale", 1.0) or 1.0)
        self.broadcast = False        # mirror typing to selected tabs/tiles
        self._broadcasting = False    # re-entrancy guard for broadcast
        self.cast_targets = set()     # sessions that receive broadcast input

        self.css_provider = Gtk.CssProvider()
        Gtk.StyleContext.add_provider_for_screen(
            Gdk.Screen.get_default(),
            self.css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )

        self._build_window()
        self._apply_chrome_theme()
        # watch the system clipboard so the Clipboard panel can record copies
        try:
            self._clip = self._clipboard()
            self._clip.connect("owner-change", self._on_clip_change)
        except Exception:
            self._clip = None
        # open the saved session's tabs/layout, or a single fresh tab
        self._load_history()      # populate the history panel from past sessions
        self._open_initial_tabs()
        self.window.show_all()
        # Panels start hidden; restore the saved open/closed state (and width)
        # once the window is laid out so the dividers land correctly.
        self.history_panel.set_no_show_all(True)
        self.history_panel.hide()
        self.clip_panel.set_no_show_all(True)
        self.clip_panel.hide()
        self.right_side.set_no_show_all(True)
        self.right_side.hide()
        self.tree_panel.set_no_show_all(True)
        self.tree_panel.hide()

        def _restore_panels():
            if self.settings.get("show_history"):
                self.history_toggle.set_active(True)
            if self.settings.get("show_clipboard"):
                self.clip_toggle.set_active(True)
            if self.settings.get("show_tree"):
                self.tree_toggle.set_active(True)
            return False
        GLib.idle_add(_restore_panels)

    # ---- window / chrome -------------------------------------------------
    def _build_window(self):
        self.window = Gtk.Window()
        self.window.set_default_size(1040, 680)
        self.window.set_title(APP_NAME)
        self.window.get_style_context().add_class("tt-root")
        self.window.connect("destroy", self._on_destroy)
        self.window.connect("key-press-event", self.on_key_press)
        # No custom HeaderBar: use the window manager's normal title bar so the
        # title stays readable in every theme (a CSD header inherited the system
        # theme and went unreadable in light mode).

        self.status_label = Gtk.Label(label="")
        self.status_label.set_xalign(0.0)
        self.status_label.set_ellipsize(Pango.EllipsizeMode.END)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.window.add(root)

        # ---- toolbar ----
        toolbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        toolbar.get_style_context().add_class("tt-toolbar")
        toolbar.set_border_width(3)
        root.pack_start(toolbar, False, False, 0)

        # left group lives in a horizontal scroller so a narrow window never
        # clips the buttons; Settings + theme stay pinned on the right.
        left = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        lscr = Gtk.ScrolledWindow()
        lscr.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        try:
            lscr.set_propagate_natural_height(True)
        except AttributeError:
            pass
        lscr.add(left)
        toolbar.pack_start(lscr, True, True, 0)

        def make_button(icon, label, tip, toggle=False):
            b = Gtk.ToggleButton() if toggle else Gtk.Button()
            inner = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            inner.pack_start(
                Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.SMALL_TOOLBAR),
                False, False, 0)
            lab = Gtk.Label(label=label)
            lab.get_style_context().add_class("tt-btnlabel")
            inner.pack_start(lab, False, False, 0)
            b.add(inner)
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_tooltip_text(tip)
            return b

        def btn(icon, label, tip, cb):
            b = make_button(icon, label, tip)
            b.connect("clicked", cb)
            left.pack_start(b, False, False, 0)
            return b

        def tog(icon, label, tip, cb):
            b = make_button(icon, label, tip, toggle=True)
            b.connect("toggled", cb)
            left.pack_start(b, False, False, 0)
            return b

        def sep():
            s = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
            s.set_margin_start(3)
            s.set_margin_end(3)
            left.pack_start(s, False, False, 0)

        self.tile_toggle = tog("view-grid-symbolic", "Tile",
                               "Switch between tabbed and tiled (split) layout",
                               self.on_toggle_tiled)
        self.tree_toggle = tog("folder-symbolic", "Files",
                               "Show the file tree (browse folders, no cd needed)",
                               self.on_toggle_tree)
        self.history_toggle = tog("document-open-recent-symbolic", "History",
                                  "Show the command history panel",
                                  self.on_toggle_history)
        self.clip_toggle = tog("view-list-symbolic", "Clipboard",
                               "Show the clipboard history panel "
                               "(records copies while open)",
                               self.on_toggle_clipboard)
        sep()
        btn("document-edit-symbolic", "Edit",
            "Edit a script (uses the file-tree selection, or asks you to pick one)",
            lambda *_: self.toolbar_edit())
        btn("media-playback-start-symbolic", "Run",
            "Run a script in the current tab",
            lambda *_: self.toolbar_run())
        btn("security-high-symbolic", "Sudo",
            "Run a script with sudo in the current tab",
            lambda *_: self.toolbar_run_sudo())
        sep()
        self.broadcast_toggle = tog("send-to-symbolic", "Cast",
                                    "Broadcast typing to the chosen tabs/tiles "
                                    "(click the caret to pick which)",
                                    self.on_toggle_broadcast)
        self.broadcast_toggle.connect("button-press-event",
                                      self._on_cast_button_press)
        self.cast_menu_btn = make_button("pan-down-symbolic", "",
                                         "Choose which tabs/tiles to cast to")
        self.cast_menu_btn.get_style_context().add_class("tt-caret")
        self.cast_menu_btn.connect("clicked", lambda *_: self._open_cast_menu())
        left.pack_start(self.cast_menu_btn, False, False, 0)
        sep()
        btn("edit-copy-symbolic", "Copy sel",
            "Copy the highlighted selection (what you dragged over)",
            lambda *_: self.copy_selection())
        btn("video-display-symbolic", "Copy view",
            "Copy the visible screen as plain text",
            lambda *_: self.copy_visible())
        btn("edit-select-all-symbolic", "Copy all",
            "Copy the entire scrollback journal as plain text",
            lambda *_: self.copy_journal())
        btn("document-save-symbolic", "Save",
            "Save the entire journal to a text file",
            lambda *_: self.save_journal())
        sep()
        btn("edit-paste-symbolic", "Paste",
            "Paste the clipboard into the terminal",
            lambda *_: self.paste_all())
        btn("media-skip-forward-symbolic", "Per line",
            "Paste & run the next clipboard line (one command per click)",
            lambda *_: self.paste_next_line())
        sep()
        btn("camera-photo-symbolic", "Shot",
            "Screenshot the window to a PNG",
            lambda *_: self.screenshot())

        # right side (pinned): Settings only (theme now lives in Settings)
        settings_btn = make_button("emblem-system-symbolic", "Settings",
                                   "Settings (theme, editor, file tree, screenshots)")
        settings_btn.connect("clicked", lambda *_: self.open_settings())
        toolbar.pack_end(settings_btn, False, False, 0)

        # Tab switcher row — always visible, in both tabbed and tiled modes.
        root.pack_start(self._build_tab_bar_row(), False, False, 0)

        # Body: file tree (left, resizable via a drag handle) | terminals
        # (centre) | history (right). The outer Paned gives the file tree a
        # draggable width divider.
        body = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        root.pack_start(body, True, True, 0)
        self.body_paned = body

        self.tree_panel = self._build_file_tree_panel()
        body.pack1(self.tree_panel, False, False)   # resize=False, shrink=False

        # right side: a Paned so the history panel also gets a drag handle.
        self.right_paned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        body.pack2(self.right_paned, True, True)

        self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.right_paned.pack1(self.content_box, True, True)  # resize, shrink

        # Right column holds the History panel (top) and Clipboard panel
        # (bottom), each independently shown/hidden; a vertical handle splits
        # them when both are open.
        self.right_side = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.history_panel = self._build_history_panel()
        self.clip_panel = self._build_clipboard_panel()
        self.right_side.pack1(self.history_panel, True, True)
        self.right_side.pack2(self.clip_panel, True, True)
        self.right_paned.pack2(self.right_side, False, False)

        # Find bar (hidden until Ctrl+Shift+F), sits just above the status bar.
        root.pack_start(self._build_find_bar(), False, False, 0)

        # Status bar (label was created earlier in this method)
        statusbar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        statusbar.get_style_context().add_class("tt-statusbar")
        statusbar.set_border_width(3)
        statusbar.pack_start(self.status_label, True, True, 6)
        root.pack_start(statusbar, False, False, 0)

    def _build_find_bar(self):
        bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        bar.get_style_context().add_class("tt-panel")
        bar.set_border_width(4)
        lab = Gtk.Label(label="Find:")
        bar.pack_start(lab, False, False, 2)
        self.find_entry = Gtk.SearchEntry()
        self.find_entry.set_hexpand(True)
        self.find_entry.connect("search-changed", lambda *_: self._find(False, fresh=True))
        self.find_entry.connect("activate", lambda *_: self._find(False))
        self.find_entry.connect("key-press-event", self._on_find_key)
        bar.pack_start(self.find_entry, True, True, 0)

        def iconbtn(icon, tip, cb):
            b = Gtk.Button()
            b.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.MENU))
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_tooltip_text(tip)
            b.connect("clicked", lambda *_: cb())
            bar.pack_start(b, False, False, 0)

        iconbtn("go-up-symbolic", "Previous match (Shift+Enter)",
                lambda: self._find(True))
        iconbtn("go-down-symbolic", "Next match (Enter)",
                lambda: self._find(False))
        iconbtn("window-close-symbolic", "Close find (Esc)", self._hide_find)

        self.find_bar = bar
        bar.set_no_show_all(True)
        bar.hide()
        return bar

    def _show_find(self):
        self.find_bar.set_no_show_all(False)
        self.find_bar.show_all()
        self.find_entry.grab_focus()
        self.find_entry.select_region(0, -1)

    def _hide_find(self):
        self.find_bar.hide()
        self.find_bar.set_no_show_all(True)
        term = self.active_terminal()
        if term is not None:
            try:
                term.search_set_regex(None, 0)
            except Exception:
                pass
            term.grab_focus()

    def _on_find_key(self, widget, event):
        if event.keyval == Gdk.KEY_Escape:
            self._hide_find()
            return True
        if (event.keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter)
                and (event.state & Gdk.ModifierType.SHIFT_MASK)):
            self._find(backward=True)
            return True
        return False

    def _find(self, backward=False, fresh=False):
        """Search the active terminal's scrollback for the find-bar text."""
        term = self.active_terminal()
        if term is None:
            return
        txt = self.find_entry.get_text()
        if not txt:
            try:
                term.search_set_regex(None, 0)
            except Exception:
                pass
            return
        pat = re.escape(txt)
        try:
            rx = Vte.Regex.new_for_search(
                pat, len(pat), _PCRE2_MULTILINE | _PCRE2_CASELESS)
            term.search_set_regex(rx, 0)
            term.search_set_wrap_around(True)
        except Exception as e:
            self.status("Find error: %s" % e)
            return
        found = (term.search_find_previous() if backward
                 else term.search_find_next())
        if not found and not fresh:
            self.status("No matches for %r." % txt)

    def _build_history_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_size_request(220, -1)   # min width; the divider can grow it
        box.set_border_width(8)
        box.get_style_context().add_class("tt-panel")

        title = Gtk.Label()
        title.set_markup("<b>History</b>")
        title.set_xalign(0.0)
        box.pack_start(title, False, False, 0)

        # search / grep row
        self._history_query = ""
        self._history_regex = False
        self._history_re = None
        srow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.history_search = Gtk.SearchEntry()
        self.history_search.set_placeholder_text("Search history\u2026")
        self.history_search.connect("search-changed", self._on_history_search)
        srow.pack_start(self.history_search, True, True, 0)
        self.history_regex_btn = Gtk.ToggleButton(label=".*")
        self.history_regex_btn.set_tooltip_text(
            "Match as a regular expression (grep-style)")
        self.history_regex_btn.connect("toggled", self._on_history_regex)
        srow.pack_start(self.history_regex_btn, False, False, 0)
        box.pack_start(srow, False, False, 0)

        # model: time string, command string  (+ filter for search)
        self.history_store = Gtk.ListStore(str, str)
        self.history_filter = self.history_store.filter_new()
        self.history_filter.set_visible_func(self._history_visible_func)
        self.history_view = Gtk.TreeView(model=self.history_filter)
        self.history_view.set_headers_visible(True)
        self.history_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)

        # Command first, then a narrow time-only column.
        r_cmd = Gtk.CellRendererText()
        r_cmd.set_property("family", "monospace")
        r_cmd.set_property("ellipsize", Pango.EllipsizeMode.END)
        col_cmd = Gtk.TreeViewColumn("Command", r_cmd, text=1)
        col_cmd.set_expand(True)
        self.history_view.append_column(col_cmd)

        r_time = Gtk.CellRendererText()
        col_time = Gtk.TreeViewColumn("Time", r_time, text=0)
        col_time.set_min_width(70)
        self.history_view.append_column(col_time)

        self.history_view.connect("row-activated", self.on_history_activated)
        self.history_view.connect("button-press-event", self._on_history_click)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.history_view)
        box.pack_start(scroller, True, True, 0)

        hint = Gtk.Label()
        hint.set_markup(
            "<small>Double-click to re-run in this tab. Right-click for run / "
            "copy / save. Select several to act on them together. Type to "
            "filter; <tt>.*</tt> toggles regex.</small>")
        hint.set_xalign(0.0)
        hint.set_line_wrap(True)
        hint.get_style_context().add_class("tt-muted")
        box.pack_start(hint, False, False, 0)
        return box

    # ---- history search / filter ----------------------------------------
    def _history_visible_func(self, model, it, data):
        q = self._history_query
        if not q:
            return True
        cmd = model.get_value(it, 1) or ""
        if self._history_regex:
            if self._history_re is not None:
                return self._history_re.search(cmd) is not None
            return q.lower() in cmd.lower()   # invalid regex -> substring
        return q.lower() in cmd.lower()

    def _on_history_search(self, entry):
        self._history_query = entry.get_text()
        self._compile_history_re()
        self.history_filter.refilter()

    def _on_history_regex(self, btn):
        self._history_regex = btn.get_active()
        self._compile_history_re()
        self.history_filter.refilter()
        self.history_search.get_style_context().remove_class("error")
        if self._history_regex and self._history_query and self._history_re is None:
            self.history_search.get_style_context().add_class("error")

    def _compile_history_re(self):
        self._history_re = None
        if self._history_regex and self._history_query:
            try:
                self._history_re = re.compile(self._history_query, re.IGNORECASE)
            except re.error:
                self._history_re = None

    # ---- clipboard history panel ----------------------------------------
    def _build_clipboard_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_size_request(220, -1)
        box.set_border_width(8)
        box.get_style_context().add_class("tt-panel")

        title = Gtk.Label()
        title.set_markup("<b>Clipboard</b>")
        title.set_xalign(0.0)
        box.pack_start(title, False, False, 0)

        srow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        self.clip_search = Gtk.SearchEntry()
        self.clip_search.set_placeholder_text("Search clipboard\u2026")
        self.clip_search.connect("search-changed", self._on_clip_search)
        srow.pack_start(self.clip_search, True, True, 0)
        box.pack_start(srow, False, False, 0)

        # model: time string, one-line display, full text (+ filter)
        self.clip_store = Gtk.ListStore(str, str, str)
        self.clip_filter = self.clip_store.filter_new()
        self.clip_filter.set_visible_func(self._clip_visible_func)
        self.clip_view = Gtk.TreeView(model=self.clip_filter)
        self.clip_view.set_headers_visible(True)
        self.clip_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)

        r_txt = Gtk.CellRendererText()
        r_txt.set_property("family", "monospace")
        r_txt.set_property("ellipsize", Pango.EllipsizeMode.END)
        col_txt = Gtk.TreeViewColumn("Content", r_txt, text=1)
        col_txt.set_expand(True)
        self.clip_view.append_column(col_txt)

        r_time = Gtk.CellRendererText()
        col_time = Gtk.TreeViewColumn("Time", r_time, text=0)
        col_time.set_min_width(70)
        self.clip_view.append_column(col_time)

        self.clip_view.connect("row-activated", self.on_clip_activated)
        self.clip_view.connect("button-press-event", self._on_clip_click)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scroller.add(self.clip_view)
        box.pack_start(scroller, True, True, 0)

        hint = Gtk.Label()
        hint.set_markup(
            "<small>Records copies while this panel is open (newest on top, "
            "in memory only). Double-click to copy back. Right-click to paste, "
            "save or remove.</small>")
        hint.set_xalign(0.0)
        hint.set_line_wrap(True)
        hint.get_style_context().add_class("tt-muted")
        box.pack_start(hint, False, False, 0)
        return box

    def _clip_display(self, text):
        one = " ".join(text.split())
        return one if len(one) <= 200 else one[:200] + "\u2026"

    def _on_clip_search(self, entry):
        self._clip_query = entry.get_text()
        self.clip_filter.refilter()

    def _clip_visible_func(self, model, it, data):
        q = self._clip_query
        if not q:
            return True
        full = model.get_value(it, 2) or ""
        return q.lower() in full.lower()

    # ---- clipboard capture ----------------------------------------------
    def _on_clip_change(self, clip, event):
        if not self._clip_capturing:
            return
        try:
            clip.request_text(self._on_clip_text)
        except Exception:
            pass

    def _on_clip_text(self, clip, text):
        if not text or not text.strip():
            return
        if text == self._clip_last:
            return
        self._clip_last = text
        self._add_clip_entry(text)

    def _add_clip_entry(self, text):
        ts = datetime.datetime.now().strftime(TIME_FMT)
        # drop any existing identical entry so re-copies bubble to the top
        it = self.clip_store.get_iter_first()
        while it is not None:
            if self.clip_store.get_value(it, 2) == text:
                if not self.clip_store.remove(it):
                    it = None
            else:
                it = self.clip_store.iter_next(it)
        self.clip_store.insert(0, [ts, self._clip_display(text), text])
        n = self.clip_store.iter_n_children(None)
        while n > self._clip_max:
            last = self.clip_store.iter_nth_child(None, n - 1)
            if last is None:
                break
            self.clip_store.remove(last)
            n -= 1

    def _set_clip_capturing(self, on):
        self._clip_capturing = on
        if on and self._clip is not None:
            try:
                self._clip.request_text(self._on_clip_text)  # seed with current
            except Exception:
                pass

    def _clip_set(self, text):
        """Put text on the clipboard without recording the echo as a new row."""
        self._clip_last = text
        try:
            self._clipboard().set_text(text, -1)
        except Exception:
            pass

    # ---- clipboard actions ----------------------------------------------
    def on_clip_activated(self, view, path, column):
        """Double-click a clipboard row: copy it back to the clipboard."""
        try:
            text = view.get_model()[path][2]
        except Exception:
            return
        self._clip_set(text)
        self.status("Copied to clipboard.")

    def _clip_selected_texts(self):
        model, paths = self.clip_view.get_selection().get_selected_rows()
        return [model[p][2] for p in paths]

    def _on_clip_click(self, view, event):
        if event.button != 3:
            return False
        info = view.get_path_at_pos(int(event.x), int(event.y))
        sel = view.get_selection()
        if info is not None:
            path = info[0]
            if not sel.path_is_selected(path):
                sel.unselect_all()
                sel.select_path(path)
        if sel.count_selected_rows() == 0:
            return False
        self._popup_clip_menu(event)
        return True

    def _popup_clip_menu(self, event):
        texts = self._clip_selected_texts()
        n = len(texts)
        if n == 0:
            return
        joined = "\n".join(texts)
        menu = Gtk.Menu()
        menu.get_style_context().add_class("tt-root")

        def item(label, cb):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda *_: cb())
            menu.append(mi)

        item("Copy to clipboard",
             lambda: (self._clip_set(joined),
                      self.status("Copied to clipboard.")))
        item("Paste into terminal", lambda: self._clip_paste_terminal(joined))
        item("Save\u2026", lambda: self._clip_save(texts))
        menu.append(Gtk.SeparatorMenuItem())
        item("Remove" if n == 1 else "Remove %d" % n, self._clip_remove_selected)
        item("Clear all", self._clip_clear)
        menu.show_all()
        try:
            menu.popup_at_pointer(event)
        except Exception:
            menu.popup(None, None, None, None, event.button, event.time)

    def _clip_paste_terminal(self, text):
        term = self.active_terminal()
        if term is not None:
            self._feed(term, text)
            term.grab_focus()
            self.status("Pasted into terminal.")

    def _clip_save(self, texts):
        default = "clipboard-%s.txt" % datetime.datetime.now().strftime(
            "%Y%m%d-%H%M%S")
        path = self._save_dialog(
            "Save clipboard items", default,
            filters=[("Text file (*.txt)", "*.txt")])
        if not path:
            return
        body = "\n".join(texts)
        if not body.endswith("\n"):
            body += "\n"
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            self.status("Saved %d item%s to %s"
                        % (len(texts), "" if len(texts) == 1 else "s", path))
        except OSError as e:
            self.status("Could not save: %s" % e)

    def _clip_remove_selected(self):
        fmodel, paths = self.clip_view.get_selection().get_selected_rows()
        refs = []
        for p in paths:
            fit = fmodel.get_iter(p)
            bit = self.clip_filter.convert_iter_to_child_iter(fit)
            refs.append(Gtk.TreeRowReference.new(
                self.clip_store, self.clip_store.get_path(bit)))
        for ref in refs:
            bp = ref.get_path()
            if bp is not None:
                self.clip_store.remove(self.clip_store.get_iter(bp))
        self.status("Removed.")

    def _clip_clear(self):
        self.clip_store.clear()
        self._clip_last = None
        self.status("Clipboard history cleared.")

    # ---- theming ---------------------------------------------------------
    def _apply_chrome_theme(self):
        t = THEMES[self.theme_name]
        # Hint dialogs/CSD toward the matching variant, but never rely on it:
        # the CSS below fully styles our own chrome regardless of the system theme.
        Gtk.Settings.get_default().set_property(
            "gtk-application-prefer-dark-theme", t["prefer_dark"])
        css = ("""
        .tt-root { background-color: %(chrome_bg)s; color: %(chrome_fg)s; }
        .tt-root label { color: %(chrome_fg)s; }
        .tt-root .tt-muted { color: %(muted_fg)s; }

        .tt-toolbar { background-color: %(chrome_bg)s; }
        .tt-statusbar { background-color: %(chrome_bg)s; color: %(chrome_fg)s;
                        border-top: 1px solid %(border)s; }
        .tt-panel { background-color: %(panel_bg)s; }
        .tt-frame { border: 2px solid transparent; background-color: %(bg)s; }
        .tt-frame.active { border: 2px solid %(accent)s; }
        .tt-frame.tt-broadcast { border: 2px solid #e0894e; }
        .tt-tab.tt-cast { background-color: rgba(224, 137, 78, 0.30); }
        .tt-caret { padding-left: 0; padding-right: 0; min-width: 16px; }
        .tt-header { background-color: %(header_bg)s; padding: 2px 4px;
                     border-bottom: 1px solid %(border)s; }

        /* buttons (toolbar + panel) */
        .tt-root button {
            background-image: none; background-color: %(btn_bg)s;
            color: %(btn_fg)s; border: 1px solid %(border)s;
            border-radius: 5px; padding: 3px 8px; text-shadow: none;
        }
        .tt-root button:hover { background-color: %(btn_hover)s; }
        .tt-root button:checked, .tt-root button:active {
            background-color: %(accent)s; color: #ffffff; border-color: %(accent)s;
        }
        .tt-root button image { color: %(btn_fg)s; }
        .tt-root button:checked image { color: #ffffff; }

        /* scrolled areas + viewports */
        .tt-root scrolledwindow, .tt-root viewport,
        .tt-root textview, .tt-root textview text {
            background-color: %(panel_bg)s; color: %(chrome_fg)s;
        }

        /* tree views (history + file tree) */
        .tt-root treeview.view {
            background-color: %(panel_bg)s; color: %(chrome_fg)s;
        }
        .tt-root treeview.view:selected,
        .tt-root treeview.view:selected:focus {
            background-color: %(sel_bg)s; color: %(sel_fg)s;
        }
        .tt-root treeview.view header button {
            background-image: none; background-color: %(header_bg)s;
            color: %(chrome_fg)s; border: 1px solid %(border)s; border-radius: 0;
        }
        .tt-root treeview.view header button:hover { background-color: %(btn_hover)s; }

        /* text entries (settings + save dialog reuse the system theme,
           but in-window entries get themed) */
        .tt-root entry {
            background-image: none; background-color: %(entry_bg)s;
            color: %(chrome_fg)s; border: 1px solid %(border)s; border-radius: 5px;
        }
        .tt-root entry:focus { border-color: %(accent)s; }

        /* notebook tab strip */
        .tt-root notebook > header { background-color: %(chrome_bg)s;
                                     border-color: %(border)s; }
        .tt-root notebook > header tab {
            background-color: %(header_bg)s; color: %(muted_fg)s;
            border-color: %(border)s; padding: 2px 6px;
        }
        .tt-root notebook > header tab:checked {
            background-color: %(bg)s; color: %(chrome_fg)s;
        }
        .tt-root notebook > header tab label { color: inherit; }

        .tt-root separator { background-color: %(border)s; }
        .tt-root paned > separator { background-color: %(border)s; }

        /* persistent tab switcher bar */
        .tt-root .tt-tabbar { background-color: %(chrome_bg)s;
                              border-bottom: 1px solid %(border)s; }
        .tt-root .tt-tab {
            background-image: none; background-color: %(header_bg)s;
            color: %(muted_fg)s; border: 1px solid %(border)s;
            border-radius: 5px; padding: 1px 4px; margin: 1px;
        }
        .tt-root .tt-tab:hover { background-color: %(btn_hover)s; }
        .tt-root .tt-tab-active {
            background-color: %(bg)s; color: %(chrome_fg)s; border-color: %(accent)s;
        }
        .tt-root .tt-tabclose { padding: 0 1px; border: none;
                                background-color: transparent; }
        .tt-root .tt-tabclose:hover { background-color: %(btn_hover)s; }

        /* tiny labels under toolbar icons */
        .tt-root .tt-btnlabel { font-size: 8.5pt; padding: 0; margin: 0; }
        .tt-root .tt-section { color: %(accent)s; }
        """) % t
        self.css_provider.load_from_data(css.encode("utf-8"))

    def _apply_terminal_colors(self, term):
        t = THEMES[self.theme_name]
        palette = [rgba(c) for c in t["palette"]]
        term.set_colors(rgba(t["fg"]), rgba(t["bg"]), palette)
        term.set_color_cursor(rgba(t["cursor"]))

    # ---- sessions / layout ----------------------------------------------
    def new_session(self, *_, cwd=None, name=None, pinned=False):
        """Create a new shell/tab, add it to the current layout, and focus it.
        cwd/name/pinned are used when restoring a saved session."""
        self.term_counter += 1
        s = Session(self.term_counter)
        s.custom_name = name
        s.pinned = bool(pinned)
        if cwd and os.path.isdir(cwd):
            s.cwd = cwd

        term = Vte.Terminal()
        s.terminal = term
        term.set_scrollback_lines(100000)
        term.set_mouse_autohide(True)
        term.set_cursor_blink_mode(Vte.CursorBlinkMode.ON)
        term.set_font(Pango.FontDescription("Monospace 11"))
        try:
            term.set_font_scale(self._font_scale)
        except Exception:
            pass
        try:
            term.set_allow_hyperlink(True)
        except Exception:
            pass
        self._apply_terminal_colors(term)
        self._setup_url_matching(term)

        try:
            term.connect("commit", self.on_commit, s)
        except Exception:
            pass
        term.connect("child-exited", self.on_child_exited, s)
        try:
            term.connect("window-title-changed", self.on_title_changed, s)
        except Exception:
            pass
        try:
            term.connect("current-directory-uri-changed",
                         self.on_cwd_changed, s)
        except Exception:
            pass
        term.connect("focus-in-event", self.on_term_focus, s)
        term.connect("button-press-event", self.on_term_button, s)

        scroller = Gtk.ScrolledWindow()
        scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scroller.add(term)
        s.scroller = scroller

        frame = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        frame.get_style_context().add_class("tt-frame")
        frame.pack_start(self._build_frame_header(s), False, False, 0)
        frame.pack_start(scroller, True, True, 0)
        s.frame = frame

        self.sessions.append(s)
        self.active_session = s
        if self.broadcast:
            self.cast_targets.add(s)   # new tabs join an active broadcast
        self._spawn_shell(term, s, s.cwd)
        self.rebuild_layout()
        self._refresh_tab_bar()
        self._update_tab_label(s)
        self.refresh_history_panel()
        self.update_active_visual()
        GLib.idle_add(term.grab_focus)
        self.status("New terminal opened.")

    def _build_frame_header(self, s):
        header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        header.get_style_context().add_class("tt-header")
        lbl = Gtk.Label(label=s.title)
        lbl.set_xalign(0.0)
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        s.header_label = lbl
        header.pack_start(lbl, True, True, 2)
        close = Gtk.Button()
        close.set_image(Gtk.Image.new_from_icon_name(
            "window-close-symbolic", Gtk.IconSize.MENU))
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.connect("clicked", lambda *_: self.close_session(s))
        header.pack_start(close, False, False, 0)
        s.header = header
        return header

    # ---- tab switcher bar ------------------------------------------------
    def _build_tab_bar_row(self):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        row.get_style_context().add_class("tt-tabbar")
        row.set_border_width(2)

        self.tab_bar = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        scr = Gtk.ScrolledWindow()
        scr.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.NEVER)
        try:
            scr.set_propagate_natural_height(True)
        except AttributeError:
            pass
        scr.add(self.tab_bar)
        row.pack_start(scr, True, True, 0)

        newbtn = Gtk.Button()
        newbtn.set_image(Gtk.Image.new_from_icon_name(
            "list-add-symbolic", Gtk.IconSize.SMALL_TOOLBAR))
        newbtn.set_relief(Gtk.ReliefStyle.NONE)
        newbtn.set_tooltip_text("New tab  (Ctrl+Shift+T)")
        newbtn.connect("clicked", lambda *_: self.new_session())
        row.pack_start(newbtn, False, False, 0)
        return row

    def _display_name(self, s):
        """Descriptive tab name: custom > current directory > title > shell N."""
        if s.custom_name:
            return s.custom_name
        try:
            uri = s.terminal.get_current_directory_uri()
        except Exception:
            uri = None
        if uri:
            path = unquote(urlparse(uri).path or "")
            if path:
                home = os.path.expanduser("~")
                if os.path.normpath(path) == os.path.normpath(home):
                    return "~"
                base = os.path.basename(path.rstrip("/"))
                return base or "/"
        if s.title:
            return s.title[:22]
        return "shell %d" % s.number

    def _tab_text(self, s):
        return ("\U0001F4CC " if s.pinned else "") + self._display_name(s)

    def _refresh_tab_bar(self):
        for c in self.tab_bar.get_children():
            self.tab_bar.remove(c)
        for s in self.sessions:
            self.tab_bar.pack_start(self._make_tab_button(s), False, False, 0)
        self.tab_bar.show_all()

    def _make_tab_button(self, s):
        # An EventBox (not a Gtk.Button) so the close button nested inside can
        # receive its own clicks; a button inside a button never fires.
        btn = Gtk.EventBox()
        btn.set_visible_window(True)
        ctx = btn.get_style_context()
        ctx.add_class("tt-tab")
        if s is self.active_session:
            ctx.add_class("tt-tab-active")

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        lbl = Gtk.Label(label=self._tab_text(s))
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
        lbl.set_max_width_chars(20)
        lbl.set_tooltip_text(self._display_name(s))
        s.name_label = lbl
        box.pack_start(lbl, True, True, 0)
        if not s.pinned:
            close = Gtk.Button()
            close.set_image(Gtk.Image.new_from_icon_name(
                "window-close-symbolic", Gtk.IconSize.MENU))
            close.set_relief(Gtk.ReliefStyle.NONE)
            close.get_style_context().add_class("tt-tabclose")
            close.set_tooltip_text("Close tab")
            close.connect("clicked", lambda *_: self.close_session(s))
            box.pack_start(close, False, False, 0)
        btn.add(box)
        btn.connect("button-press-event", self._on_tab_button_press, s)
        s.tab_button = btn
        return btn

    def _update_tabbar_active(self):
        for s in self.sessions:
            if s.tab_button is None:
                continue
            ctx = s.tab_button.get_style_context()
            if s is self.active_session:
                ctx.add_class("tt-tab-active")
            else:
                ctx.remove_class("tt-tab-active")

    def _update_tab_label(self, s):
        name = self._display_name(s)
        if s.header_label is not None:
            s.header_label.set_text(name)
        if s.name_label is not None:
            s.name_label.set_text(self._tab_text(s))
            s.name_label.set_tooltip_text(name)

    def _on_tab_button_press(self, widget, event, s):
        if (event.type == Gdk.EventType._2BUTTON_PRESS
                and event.button == 1):
            self._rename_session(s)
            return True
        if event.button == 3:
            self._popup_tab_menu(s, event)
            return True
        if event.button == 1:
            self.select_session(s)
            return True
        return False

    def _popup_tab_menu(self, s, event):
        menu = Gtk.Menu()
        menu.get_style_context().add_class("tt-root")

        def item(label, cb):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda *_: cb())
            menu.append(mi)

        item("Rename\u2026", lambda: self._rename_session(s))
        item("Unpin" if s.pinned else "Pin", lambda: self._toggle_pin(s))
        item("Close", lambda: self.close_session(s))
        menu.show_all()
        try:
            menu.popup_at_pointer(event)
        except AttributeError:
            menu.popup(None, None, None, None, event.button, event.time)

    def _toggle_pin(self, s):
        s.pinned = not s.pinned
        self._refresh_tab_bar()
        self.status("Tab pinned." if s.pinned
                    else "Tab unpinned.")

    def _rename_session(self, s):
        dlg = Gtk.Dialog(title="Rename tab", parent=self.window)
        dlg.set_modal(True)
        dlg.get_style_context().add_class("tt-root")
        dlg.add_button(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL)
        dlg.add_button(Gtk.STOCK_OK, Gtk.ResponseType.OK)
        dlg.set_default_response(Gtk.ResponseType.OK)
        area = dlg.get_content_area()
        area.set_border_width(12)
        area.set_spacing(6)
        area.add(Gtk.Label(label="Tab name (leave blank for automatic):",
                           xalign=0))
        entry = Gtk.Entry()
        entry.set_text(s.custom_name or self._display_name(s))
        entry.set_activates_default(True)
        area.add(entry)
        dlg.show_all()
        if dlg.run() == Gtk.ResponseType.OK:
            s.custom_name = entry.get_text().strip() or None
            self._update_tab_label(s)
        dlg.destroy()

    def select_session(self, s):
        """Make session `s` the active tab and focus its terminal."""
        if s not in self.sessions:
            return
        self.active_session = s
        if (not self.tiled and isinstance(self.layout_widget, Gtk.Notebook)
                and s in self.sessions):
            idx = self.sessions.index(s)
            self._switching = True
            try:
                if self.layout_widget.get_current_page() != idx:
                    self.layout_widget.set_current_page(idx)
            finally:
                self._switching = False
        self._update_active_borders()
        self._update_tabbar_active()
        self.refresh_history_panel()
        GLib.idle_add(s.terminal.grab_focus)

    def _on_destroy(self, *_):
        self._save_session()
        Gtk.main_quit()

    def _session_cwd(self, s):
        """The tab's working directory for restore. Read the shell's live cwd
        from /proc (works regardless of the shell's config / OSC 7 support);
        fall back to the OSC 7 value, if any."""
        pid = s.shell_pid
        if pid:
            try:
                return os.readlink("/proc/%d/cwd" % pid)
            except OSError:
                pass
        return s.cwd

    def _session_snapshot(self):
        """Capture the current tabs and layout for restoring next launch."""
        home = os.path.expanduser("~")
        tabs = [{"cwd": self._session_cwd(s) or home,
                 "name": s.custom_name,
                 "pinned": bool(s.pinned)} for s in self.sessions]
        try:
            active = self.sessions.index(self.active_session)
        except ValueError:
            active = 0
        return {"tiled": bool(self.tiled), "active": active, "tabs": tabs}

    def _capture_layout_prefs(self):
        """Record the file-tree/history panel open state and widths so they're
        restored next launch."""
        show_tree = bool(self.tree_toggle.get_active())
        show_history = bool(self.history_toggle.get_active())
        show_side = show_history or bool(self.clip_toggle.get_active())
        tw, hw = self._tree_width, self._history_width
        try:
            if show_tree:
                pos = self.body_paned.get_position()
                if pos and pos > 10:
                    tw = pos
        except Exception:
            pass
        try:
            if show_side:
                total = self.right_paned.get_allocated_width()
                pos = self.right_paned.get_position()
                if total and pos and (total - pos) > 10:
                    hw = total - pos
        except Exception:
            pass
        self.settings["show_tree"] = show_tree
        self.settings["show_history"] = show_history
        self.settings["show_clipboard"] = bool(self.clip_toggle.get_active())
        self.settings["tree_width"] = tw
        self.settings["history_width"] = hw

    def _save_session(self):
        try:
            self._capture_layout_prefs()
            if self.settings.get("restore_session", True) and self.sessions:
                self.settings["session"] = self._session_snapshot()
            else:
                self.settings["session"] = None
            self._save_settings()
        except Exception:
            pass

    def _open_initial_tabs(self):
        """Restore the saved session's tabs/layout, or open one fresh tab."""
        snap = self.settings.get("session")
        tabs = snap.get("tabs") if (self.settings.get("restore_session", True)
                                    and isinstance(snap, dict)) else None
        if not tabs:
            self.new_session()
            return
        for t in tabs:
            self.new_session(cwd=t.get("cwd"),
                             name=t.get("name"),
                             pinned=t.get("pinned", False))
        try:
            self.tile_toggle.set_active(bool(snap.get("tiled", False)))
        except Exception:
            self.rebuild_layout()
        idx = snap.get("active", 0)
        if isinstance(idx, int) and 0 <= idx < len(self.sessions):
            self.select_session(self.sessions[idx])

    def _spawn_shell(self, term, s, cwd=None):
        shell = os.environ.get("SHELL") or "/bin/bash"
        home = os.environ.get("HOME") or "/"
        workdir = cwd if (cwd and os.path.isdir(cwd)) else home
        envv = ["%s=%s" % (k, v) for k, v in os.environ.items()]
        try:
            term.spawn_async(
                Vte.PtyFlags.DEFAULT, workdir, [shell], envv,
                GLib.SpawnFlags.DEFAULT, None, None, -1, None,
                self._on_spawn_done, s)
        except Exception as e:
            self.status("Failed to start shell: %s" % e)

    def _on_spawn_done(self, terminal, pid, *rest):
        error = rest[0] if rest else None
        s = rest[1] if len(rest) > 1 else None
        if error is not None:
            self.status("Shell error: %s" % error)
        if s is not None and isinstance(pid, int) and pid > 0:
            s.shell_pid = pid

    def rebuild_layout(self):
        """Reparent every session frame into the current layout (tabbed notebook or tiled paned tree)."""
        frames = [s.frame for s in self.sessions]
        # Detach every frame from its current parent (keep Python refs alive).
        for f in frames:
            p = f.get_parent()
            if p is not None:
                p.remove(f)
        # Tear down the previous layout container. CRITICAL: when only one
        # session is open, the tiled layout_widget *is* that frame, so we must
        # never destroy it here or we'd kill the live terminal.
        old = self.layout_widget
        self.layout_widget = None
        if old is not None and old not in frames:
            op = old.get_parent()
            if op is not None:
                op.remove(old)
            old.destroy()

        if self.tiled:
            for s in self.sessions:
                s.set_header_visible(True)
            self.layout_widget = self._build_tiled(frames, 0)
        else:
            for s in self.sessions:
                s.set_header_visible(False)
            self.layout_widget = self._build_notebook(frames)

        self.content_box.pack_start(self.layout_widget, True, True, 0)
        self.content_box.show_all()
        if self.tiled:
            GLib.idle_add(self._balance_paned, self.layout_widget)

    def _build_notebook(self, frames):
        nb = Gtk.Notebook()
        nb.set_show_tabs(False)   # our own tab bar drives switching
        nb.set_show_border(False)
        for f in frames:
            nb.append_page(f, None)
        nb.connect("switch-page", self.on_switch_page)
        return nb

    def _build_tiled(self, frames, depth):
        if len(frames) == 1:
            return frames[0]
        # Rows first, then columns: split top/bottom at even depth.
        orient = (Gtk.Orientation.VERTICAL if depth % 2 == 0
                  else Gtk.Orientation.HORIZONTAL)
        paned = Gtk.Paned(orientation=orient)
        mid = (len(frames) + 1) // 2
        paned.pack1(self._build_tiled(frames[:mid], depth + 1), True, True)
        paned.pack2(self._build_tiled(frames[mid:], depth + 1), True, True)
        return paned

    def _balance_paned(self, widget):
        if isinstance(widget, Gtk.Paned):
            if widget.get_orientation() == Gtk.Orientation.HORIZONTAL:
                extent = widget.get_allocated_width()
            else:
                extent = widget.get_allocated_height()
            if extent > 1:
                widget.set_position(extent // 2)
            self._balance_paned(widget.get_child1())
            self._balance_paned(widget.get_child2())
        return False

    def close_session(self, s):
        """Close session `s` and rebuild the layout; closes the window if it was the last tab."""
        if s not in self.sessions:
            return
        idx = self.sessions.index(s)
        self.sessions.remove(s)
        self.cast_targets.discard(s)
        p = s.frame.get_parent()
        if p is not None:
            p.remove(s.frame)
        s.frame.destroy()
        if not self.sessions:
            self.window.close()
            return
        if self.active_session is s:
            self.active_session = self.sessions[min(idx, len(self.sessions) - 1)]
        self.rebuild_layout()
        self._refresh_tab_bar()
        self.refresh_history_panel()
        self.update_active_visual()

    def active_terminal(self):
        """Return the active session's `Vte.Terminal`, or None."""
        if self.active_session is not None:
            return self.active_session.terminal
        if self.sessions:
            return self.sessions[0].terminal
        return None

    def update_active_visual(self):
        """Sync the active-tab highlight, focus border and notebook page to the active session."""
        self._update_active_borders()
        self._update_tabbar_active()
        self._update_cast_visuals()
        if (not self.tiled and isinstance(self.layout_widget, Gtk.Notebook)
                and self.active_session in self.sessions):
            idx = self.sessions.index(self.active_session)
            # Guard against the switch-page feedback loop: during notebook
            # realization get_current_page() can lag (returns -1), so calling
            # set_current_page() here would re-enter on_switch_page endlessly.
            self._switching = True
            try:
                if self.layout_widget.get_current_page() != idx:
                    self.layout_widget.set_current_page(idx)
            finally:
                self._switching = False

    def _update_active_borders(self):
        for s in self.sessions:
            ctx = s.frame.get_style_context()
            if s is self.active_session:
                ctx.add_class("active")
            else:
                ctx.remove_class("active")

    def _update_cast_visuals(self):
        """Mark the tabs/tiles that will receive broadcast input (orange). The
        active pane is the source, so it's never marked as a target."""
        casting = self.broadcast and len(self.sessions) > 1
        for s in self.sessions:
            target = (casting and s in self.cast_targets
                      and s is not self.active_session)
            fctx = s.frame.get_style_context()
            (fctx.add_class if target else fctx.remove_class)("tt-broadcast")
            if s.tab_button is not None:
                tctx = s.tab_button.get_style_context()
                (tctx.add_class if target else tctx.remove_class)("tt-cast")

    # ---- signal handlers -------------------------------------------------
    def on_switch_page(self, notebook, page, page_num):
        if self._switching:
            return
        if 0 <= page_num < len(self.sessions):
            self.active_session = self.sessions[page_num]
            self._update_active_borders()
            self._update_tabbar_active()
            self.refresh_history_panel()
            GLib.idle_add(self.active_session.terminal.grab_focus)

    def on_page_reordered(self, notebook, child, new_index):
        moved = next((s for s in self.sessions if s.frame is child), None)
        if moved is not None:
            self.sessions.remove(moved)
            self.sessions.insert(new_index, moved)

    def on_term_focus(self, term, event, s):
        if self.active_session is not s:
            self.active_session = s
            self._update_active_borders()
            self._update_tabbar_active()
            self.refresh_history_panel()
        return False

    def on_title_changed(self, term, s):
        try:
            t = term.get_window_title()
        except Exception:
            t = None
        if t:
            s.title = t.strip()
        self._update_tab_label(s)

    def on_cwd_changed(self, term, s):
        try:
            uri = term.get_current_directory_uri()
            if uri and uri.startswith("file://"):
                path = unquote(urlparse(uri).path)
                if path:
                    s.cwd = path
        except Exception:
            pass
        self._update_tab_label(s)
        # auto-refresh the file tree when the active tab changes directory
        if s is self.active_session:
            self._schedule_tree_autorefresh()

    def on_child_exited(self, term, status, s):
        self.close_session(s)

    def on_commit(self, term, text, size, s):
        """Reconstruct entered command lines from the input stream, while keeping
        passwords out of the history. The suppress decision is made once when a
        line begins (cheap, and avoids re-scanning the screen per keystroke)."""
        # Broadcast: mirror this input to the chosen target tabs/tiles. Works in
        # both tabbed and tiled view. Only the *active* pane broadcasts, so
        # mirrored input (delivered to inactive panes) never echoes back; the
        # re-entrancy guard is a second safety net.
        if (self.broadcast and not self._broadcasting
                and len(self.sessions) > 1
                and self.active_session is not None
                and term is self.active_session.terminal):
            self._broadcasting = True
            try:
                data = text.encode("utf-8", "replace")
                for other in self.sessions:
                    if other is not self.active_session and other in self.cast_targets:
                        try:
                            other.terminal.feed_child(data)
                        except Exception:
                            pass
            finally:
                self._broadcasting = False
        buf = s.input_buffer
        for ch in text:
            o = ord(ch)
            if s.in_escape:
                if ch.isalpha() or ch == "~":
                    s.in_escape = False
                continue
            if ch in ("\r", "\n"):
                cmd = buf.strip()
                if cmd and not s.suppress_line:
                    self._record_command(s, cmd)
                buf = ""
                s.line_active = False
                s.suppress_line = False
            elif o in (0x7f, 0x08):       # backspace / delete
                buf = buf[:-1]
            elif o in (0x15, 0x03):       # Ctrl-U (kill line) / Ctrl-C (abort)
                buf = ""
                s.line_active = False
                s.suppress_line = False
            elif o == 0x1b:               # ESC -> start of an escape sequence
                s.in_escape = True
            elif o < 0x20:                # other control chars: ignore
                pass
            else:
                if not s.line_active:     # first char of a new line: decide once
                    s.line_active = True
                    # Only the shell prompt should be recorded. If any program
                    # is the foreground process (sudo asking for a password, an
                    # editor, a pager, a REPL...), its input isn't a shell
                    # command, so don't log it.
                    s.suppress_line = not self._shell_is_foreground(term, s)
                if not s.suppress_line:
                    buf += ch
        s.input_buffer = buf

    def _shell_is_foreground(self, term, s):
        """True when this tab's shell is the terminal's foreground process group
        -- i.e. we're sitting at the shell prompt. When any program is running
        (sudo prompting for a password, an editor, a pager, a REPL...) the
        foreground group differs, so its input is not recorded. This keeps
        passwords (including sudo pwfeedback) and full-screen-app keystrokes out
        of the command history. Fails safe to True (record)."""
        if s.shell_pid is None:
            return True
        try:
            return os.tcgetpgrp(term.get_pty().get_fd()) == s.shell_pid
        except Exception:
            return True

    def _record_command(self, s, cmd):
        if self.settings.get("redact_secrets"):
            cmd = self._redact_secrets(cmd)
        ts = datetime.datetime.now()
        self.cmd_history.append((ts, cmd))
        # cap memory + the panel together
        over = len(self.cmd_history) - self._history_cap
        if over > 0:
            del self.cmd_history[:over]
            for _ in range(over):
                it = self.history_store.get_iter_first()
                if it is None:
                    break
                self.history_store.remove(it)
        self.history_store.append([self._fmt_history_time(ts), cmd])
        self._scroll_history_to_end()
        self._persist_command(ts, cmd)

    def _fmt_history_time(self, dt):
        """Time of day for today's commands; date + time for older ones."""
        if dt.date() == datetime.date.today():
            return dt.strftime(TIME_FMT)
        return dt.strftime("%d %b %H:%M")

    def _persist_command(self, ts, cmd):
        if not self.settings.get("persist_history", True):
            return
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(HISTORY_FILE, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"t": ts.isoformat(timespec="seconds"),
                                     "c": cmd}, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _load_history(self):
        """Load past commands from HISTORY_FILE into the panel, compacting the
        file if it has grown large."""
        self.cmd_history = []
        if not self.settings.get("persist_history", True):
            return
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as fh:
                lines = fh.read().splitlines()
        except OSError:
            return
        if len(lines) > self._history_file_max:
            lines = lines[-self._history_cap:]
            try:
                with open(HISTORY_FILE, "w", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + ("\n" if lines else ""))
            except OSError:
                pass
        for ln in lines[-self._history_cap:]:
            try:
                d = json.loads(ln)
                ts = datetime.datetime.fromisoformat(d["t"])
                cmd = d["c"]
            except (ValueError, KeyError, TypeError):
                continue
            self.cmd_history.append((ts, cmd))
            self.history_store.append([self._fmt_history_time(ts), cmd])

    def _clear_history(self):
        """Clear the history panel and the persisted history file."""
        self.cmd_history = []
        self.history_store.clear()
        try:
            if os.path.exists(HISTORY_FILE):
                os.remove(HISTORY_FILE)
        except OSError:
            pass
        self.status("Command history cleared.")

    def _redact_secrets(self, cmd):
        """Mask secret values that appear inline on a command line. Opt-in
        (Settings). The command keeps its shape; only the secret becomes ***."""
        out = cmd
        # KEY=value assignments where KEY looks secret-ish
        out = re.sub(
            r"(?i)\b(\w*(?:password|passwd|secret|token|api[_-]?key|"
            r"access[_-]?key|auth)\w*\s*=\s*)(\S+)", r"\1***", out)
        # long options: --password=x, --password x, --token=, --api-key, ...
        out = re.sub(
            r"(?i)(--(?:password|token|secret|api[-_]?key|access[-_]?key|"
            r"auth[-_]?token)[=\s]+)(\S+)", r"\1***", out)
        # mysql/mariadb -pPASSWORD (attached form only)
        out = re.sub(
            r"(?i)\b((?:mysql|mysqldump|mariadb)\b[^|;&]*\s-p)(\S+)",
            r"\1***", out)
        # Authorization headers / bearer tokens
        out = re.sub(r"(?i)(authorization:\s*(?:bearer|basic)\s+)(\S+)",
                     r"\1***", out)
        out = re.sub(r"(?i)(\bbearer\s+)([A-Za-z0-9._\-]{8,})", r"\1***", out)
        return out

    def on_term_button(self, term, event, s):
        # Ctrl+left-click opens a URL under the pointer (typed/output or OSC 8).
        if (event.button == 1
                and (event.state & Gdk.ModifierType.CONTROL_MASK)):
            uri = self._uri_at_event(term, event)
            if uri:
                self._open_uri(uri)
                return True
        if event.button == 3:  # right click -> context menu
            self.active_session = s
            self.update_active_visual()
            self._popup_terminal_menu(term, event)
            return True
        return False

    def _setup_url_matching(self, term):
        """Make URLs in this terminal clickable (Ctrl+click) and hover as links."""
        try:
            rx = Vte.Regex.new_for_match(URL_PATTERN, len(URL_PATTERN),
                                         _PCRE2_MULTILINE | _PCRE2_CASELESS)
            tag = term.match_add_regex(rx, 0)
            term.match_set_cursor_name(tag, "pointer")
        except Exception:
            pass

    def _uri_at_event(self, term, event):
        """Return the URL under the click: an explicit OSC 8 link if present,
        otherwise a regex-matched URL in the text."""
        try:
            link = term.hyperlink_check_event(event)
            if link:
                return link
        except Exception:
            pass
        try:
            res = term.match_check_event(event)
            match = res[0] if isinstance(res, tuple) else res
            if match:
                return match
        except Exception:
            pass
        return None

    def _open_uri(self, uri):
        if "://" not in uri and uri.lower().startswith("www."):
            uri = "https://" + uri
        try:
            Gtk.show_uri_on_window(self.window, uri, Gdk.CURRENT_TIME)
            self.status("Opening %s" % uri)
            return
        except Exception:
            pass
        try:
            subprocess.Popen(["xdg-open", uri])
            self.status("Opening %s" % uri)
        except Exception as e:
            self.status("Could not open link: %s" % e)

    def _popup_terminal_menu(self, term, event):
        menu = Gtk.Menu()

        def item(label, cb):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", cb)
            menu.append(mi)

        item("Copy", lambda *_: self._copy_selection(term))
        item("Paste", lambda *_: self.paste_all())
        item("Select All", lambda *_: term.select_all())
        menu.append(Gtk.SeparatorMenuItem())
        item("New Tab", lambda *_: self.new_session())
        item("Close Tab", lambda *_: self.close_session(self.active_session))
        menu.show_all()
        menu.popup_at_pointer(event)

    def on_key_press(self, widget, event):
        """Window key handler: tab shortcuts, new/close tab, and font zoom."""
        ctrl = event.state & Gdk.ModifierType.CONTROL_MASK
        shift = event.state & Gdk.ModifierType.SHIFT_MASK
        kv = Gdk.keyval_to_lower(event.keyval)
        if ctrl and shift:
            if kv == Gdk.KEY_t:
                self.new_session(); return True
            if kv == Gdk.KEY_w:
                self.close_session(self.active_session); return True
            if kv == Gdk.KEY_c:
                self._copy_selection(self.active_terminal()); return True
            if kv == Gdk.KEY_v:
                self.paste_all(); return True
            if kv == Gdk.KEY_f:
                self._show_find(); return True
        if ctrl and not shift:
            term = self.active_terminal()
            if event.keyval == Gdk.KEY_Page_Down:
                self._cycle_tab(+1); return True
            if event.keyval == Gdk.KEY_Page_Up:
                self._cycle_tab(-1); return True
            if term is None:
                return False
            if event.keyval in (Gdk.KEY_plus, Gdk.KEY_equal, Gdk.KEY_KP_Add):
                self._zoom(+0.1); return True
            if event.keyval in (Gdk.KEY_minus, Gdk.KEY_KP_Subtract):
                self._zoom(-0.1); return True
            if event.keyval == Gdk.KEY_0:
                self._zoom(reset=True); return True
        return False

    def _zoom(self, delta=0.0, reset=False):
        """Change the font scale for all terminals and remember it."""
        if reset:
            scale = 1.0
        else:
            scale = min(max(self._font_scale + delta, 0.3), 4.0)
        self._font_scale = scale
        for s in self.sessions:
            try:
                s.terminal.set_font_scale(scale)
            except Exception:
                pass
        self.settings["font_scale"] = scale
        self._save_settings()

    def _cycle_tab(self, step):
        if not self.sessions or self.active_session not in self.sessions:
            return
        i = (self.sessions.index(self.active_session) + step) % len(self.sessions)
        self.select_session(self.sessions[i])

    # ---- toolbar toggles -------------------------------------------------
    def on_toggle_tiled(self, btn):
        """Toolbar: switch between tabbed and tiled (split) layout."""
        self.tiled = btn.get_active()
        self.rebuild_layout()
        self.update_active_visual()
        self.status("Tiled layout on." if self.tiled else "Tabbed layout on.")

    def on_toggle_broadcast(self, btn):
        """Mirror typing from the active tab to the chosen tabs/tiles. Works in
        both tabbed and tiled view; right-click the Cast button to pick targets."""
        self.broadcast = btn.get_active()
        if self.broadcast:
            self.cast_targets &= set(self.sessions)      # drop any stale ones
            if not self.cast_targets:
                self.cast_targets = set(self.sessions)   # default: every tab
        self.update_active_visual()
        if not self.broadcast:
            self.status("Broadcast off.")
        else:
            self._cast_status()

    def _cast_status(self):
        if not self.broadcast:
            return
        n = sum(1 for t in self.cast_targets
                if t in self.sessions and t is not self.active_session)
        if n == 0:
            self.status("Broadcast on, but no other tabs are selected \u2014 "
                        "right-click Cast to choose targets.")
        else:
            self.status("Broadcasting to %d other tab%s \u2014 right-click Cast "
                        "to change." % (n, "" if n == 1 else "s"))

    def _on_cast_button_press(self, widget, event):
        if event.button == 3:                 # right-click -> choose targets
            self._build_cast_menu().popup_at_pointer(event)
            return True
        return False

    def _open_cast_menu(self):
        """Open the cast-target chooser from the dropdown caret."""
        menu = self._build_cast_menu()
        try:
            menu.popup_at_widget(self.cast_menu_btn,
                                 Gdk.Gravity.SOUTH_WEST,
                                 Gdk.Gravity.NORTH_WEST, None)
        except Exception:
            menu.popup(None, None, None, None, 0, Gtk.get_current_event_time())

    def _build_cast_menu(self):
        menu = Gtk.Menu()
        menu.get_style_context().add_class("tt-root")

        def plain(label, cb):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda *_: cb())
            menu.append(mi)

        plain("Cast to all tabs", lambda: self._cast_set_all(True))
        plain("Cast to none", lambda: self._cast_set_all(False))
        menu.append(Gtk.SeparatorMenuItem())
        for s in self.sessions:
            label = self._display_name(s)
            if s is self.active_session:
                label += "  (this tab)"
            ci = Gtk.CheckMenuItem(label=label)
            ci.set_active(s in self.cast_targets)
            ci.connect("toggled",
                       lambda w, ss=s: self._cast_toggle_target(ss, w.get_active()))
            menu.append(ci)
        menu.show_all()
        return menu

    def _cast_set_all(self, on):
        self.cast_targets = set(self.sessions) if on else set()
        self.update_active_visual()
        self._cast_status()

    def _cast_toggle_target(self, s, on):
        if on:
            self.cast_targets.add(s)
        else:
            self.cast_targets.discard(s)
        self.update_active_visual()
        self._cast_status()

    def on_toggle_history(self, btn):
        """Toolbar: show or hide the command-history panel."""
        if btn.get_active():
            self.refresh_history_panel()
        self._update_side_panels()

    def on_toggle_clipboard(self, btn):
        """Toolbar: show or hide the clipboard-history panel. Recording the
        clipboard only happens while this panel is open."""
        on = btn.get_active()
        self._set_clip_capturing(on)
        self._update_side_panels()
        if on:
            self.status("Clipboard panel on \u2014 copies are recorded while "
                        "it's open (kept in memory only).")

    def _update_side_panels(self):
        """Show/hide the History and Clipboard panels in the right column and
        size the dividers. The right column collapses when neither is open."""
        show_h = self.history_toggle.get_active()
        show_c = self.clip_toggle.get_active()

        def reveal(panel, show):
            if show:
                panel.set_no_show_all(False)
                panel.show_all()
            else:
                panel.hide()
                panel.set_no_show_all(True)

        reveal(self.history_panel, show_h)
        reveal(self.clip_panel, show_c)

        if show_h or show_c:
            self.right_side.set_no_show_all(False)
            self.right_side.show()

            def place():
                total = self.right_paned.get_allocated_width()
                if total > self._history_width + 60:
                    self.right_paned.set_position(total - self._history_width)
                if show_h and show_c:
                    h = self.right_side.get_allocated_height()
                    if h > 80:
                        self.right_side.set_position(h // 2)
                return False
            GLib.idle_add(place)
        else:
            total = self.right_paned.get_allocated_width()
            pos = self.right_paned.get_position()
            if total and pos and (total - pos) > 10:
                self._history_width = total - pos
            self.right_side.hide()
            self.right_side.set_no_show_all(True)

    def set_theme(self, name):
        """Apply and persist a colour theme ('light' or 'dark'); restyles chrome and every terminal."""
        if name not in THEMES:
            return
        self.theme_name = name
        self._apply_chrome_theme()
        for s in self.sessions:
            self._apply_terminal_colors(s.terminal)
        self._save_settings()
        self.status("%s theme." % name.capitalize())

    # ---- text extraction helpers ----------------------------------------
    def _get_all_text(self, term):
        try:
            r = term.get_text_format(Vte.Format.TEXT)
            return (r[0] if isinstance(r, (tuple, list)) else r) or ""
        except Exception:
            pass
        try:
            r = term.get_text()
            return (r[0] if isinstance(r, (tuple, list)) else r) or ""
        except Exception:
            return ""

    def _get_visible_text(self, term):
        try:
            vadj = term.get_vadjustment()
            top = int(vadj.get_value())
            rows = int(term.get_row_count())
            cols = int(term.get_column_count())
            bottom = top + rows - 1
            r = term.get_text_range_format(Vte.Format.TEXT, top, 0, bottom, cols)
            return (r[0] if isinstance(r, (tuple, list)) else r) or ""
        except Exception:
            try:
                r = term.get_text_range(top, 0, bottom, cols)
                return (r[0] if isinstance(r, (tuple, list)) else r) or ""
            except Exception:
                return self._get_all_text(term)

    def _feed(self, term, text):
        if not text:
            return
        data = text.encode("utf-8")
        try:
            term.feed_child(data)          # VTE >= 0.72 (Mint 22): bytes
            return
        except (TypeError, ValueError):
            pass
        try:
            term.feed_child(text, len(data))   # VTE 0.68 (Mint 21): (str, len)
            return
        except (TypeError, ValueError):
            pass
        try:
            term.feed_child_binary(data)   # last-resort fallback
        except Exception as e:
            self.status("Paste failed: %s" % e)

    def _copy_selection(self, term):
        if term is None:
            return
        try:
            if term.get_has_selection():
                term.copy_clipboard_format(Vte.Format.TEXT)
                self.status("Selection copied.")
        except Exception:
            try:
                term.copy_clipboard()
            except Exception:
                pass

    def _clipboard(self):
        return Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)

    # ---- toolbar actions -------------------------------------------------
    def copy_selection(self):
        """Copy the terminal's highlighted selection to the clipboard."""
        term = self.active_terminal()
        if term is None:
            return
        if not term.get_has_selection():
            self.status("Nothing selected \u2014 drag over text to highlight it first.")
            return
        try:
            term.copy_clipboard_format(Vte.Format.TEXT)
        except (AttributeError, TypeError):
            term.copy_clipboard()   # older VTE fallback
        self.status("Copied selection to clipboard.")

    def copy_visible(self):
        """Copy the visible screen (no scrollback) to the clipboard as plain text."""
        term = self.active_terminal()
        if term is None:
            return
        text = self._get_visible_text(term).rstrip("\n")
        self._clipboard().set_text(text, -1)
        self.status("Copied visible screen (%d characters)." % len(text))

    def copy_journal(self):
        """Copy the entire scrollback to the clipboard as plain text."""
        term = self.active_terminal()
        if term is None:
            return
        text = self._get_all_text(term).rstrip("\n")
        self._clipboard().set_text(text, -1)
        self.status("Copied full journal (%d characters)." % len(text))

    def save_journal(self):
        """Save the entire scrollback to a text file chosen by the user."""
        term = self.active_terminal()
        if term is None:
            return
        text = self._get_all_text(term).rstrip("\n") + "\n"
        default = "terminal-journal-%s.txt" % datetime.datetime.now().strftime(
            "%Y%m%d-%H%M%S")
        path = self._save_dialog("Save journal", default)
        if path:
            try:
                with open(path, "w", encoding="utf-8") as fh:
                    fh.write(text)
                self.status("Journal saved to %s" % path)
            except OSError as e:
                self.status("Could not save: %s" % e)

    def paste_all(self):
        """Paste the whole clipboard into the active terminal."""
        if self.active_terminal() is None:
            return

        def got(_clip, text, *_):
            term = self.active_terminal()
            if term is None:
                return
            if not text:
                self.status("Clipboard is empty.")
                return
            self._feed(term, text)
            self.status("Pasted clipboard into terminal.")
        self._clipboard().request_text(got)

    def paste_next_line(self):
        """Paste and run the next clipboard line, one command per call."""
        term = self.active_terminal()
        if term is None:
            return
        # If a paste is already in progress, just run the next queued line.
        if self.paste_queue:
            self._feed_next_paste_line(term)
            return

        # Otherwise fetch the clipboard asynchronously, then run line one.
        # (Async request_text avoids the first-call wait_for_text() race,
        # where a freshly-focused window reads an externally-owned clipboard
        # as empty until something else touches it first.)
        def got(_clip, text, *_):
            self.paste_queue = [ln.rstrip() for ln in (text or "").splitlines()
                                if ln.strip() != ""]
            if not self.paste_queue:
                self.status("Clipboard has no command lines to run.")
                return
            self._feed_next_paste_line(self.active_terminal())
        self._clipboard().request_text(got)

    def _feed_next_paste_line(self, term):
        if term is None or not self.paste_queue:
            return
        line = self.paste_queue.pop(0)
        self._feed(term, line + "\n")
        left = len(self.paste_queue)
        self.status("Ran: %s   (%d line%s remaining)"
                    % (line, left, "" if left == 1 else "s"))

    def _screenshot_target_path(self):
        pics = os.path.join(os.path.expanduser("~"), "Pictures")
        out_dir = pics if os.path.isdir(pics) else os.path.expanduser("~")
        return os.path.join(out_dir, "%s-%s.png"
                            % (APP_NAME,
                               datetime.datetime.now().strftime("%Y%m%d-%H%M%S")))

    def screenshot(self):
        """Capture the window to a PNG (built-in on X11, external tool on Wayland, or a custom Settings command)."""
        path = self._screenshot_target_path()

        # 1) Explicit user-configured command wins (use {path} placeholder).
        custom = self.settings.get("screenshot_cmd", "").strip()
        if custom:
            if self._run_screenshot_template(custom, path):
                self.status("Screenshot saved to %s" % path)
            else:
                self.status("Custom screenshot command failed: %s" % custom)
            return

        # 2) Built-in in-process capture. Works on X11 and grabs just our
        #    window. Returns None under Wayland (compositor blocks it).
        gdk_win = self.window.get_window()
        if gdk_win is not None:
            pb = Gdk.pixbuf_get_from_window(
                gdk_win, 0, 0, gdk_win.get_width(), gdk_win.get_height())
            if pb is not None:
                try:
                    pb.savev(path, "png", [], [])
                    self.status("Screenshot saved to %s" % path)
                    return
                except Exception:
                    pass

        # 3) Fall back to whatever real screenshot tool is installed
        #    (this is the Wayland path: gnome-screenshot, spectacle, grim...).
        tool = self._external_screenshot(path)
        if tool:
            self.status("Screenshot saved to %s  (via %s)" % (path, tool))
        else:
            self.status("Screenshot failed. On Wayland install one of: "
                        "gnome-screenshot, spectacle, grim, flameshot "
                        "(or set a command in Settings).")

    def _run_screenshot_template(self, template, path):
        cmd = template.replace("{path}", shlex.quote(path))
        try:
            subprocess.run(cmd, shell=True, timeout=20,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except Exception:
            return False
        return os.path.isfile(path) and os.path.getsize(path) > 0

    def _external_screenshot(self, path):
        for binary, template in SCREENSHOT_TOOLS:
            if shutil.which(binary) is None:
                continue
            argv = [shlex.quote(path) if tok == "{path}" else tok
                    for tok in template.split()]
            try:
                subprocess.run(" ".join(argv), shell=True, timeout=20,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            except Exception:
                continue
            if os.path.isfile(path) and os.path.getsize(path) > 0:
                return binary
        return None

    # ---- history panel actions ------------------------------------------
    def refresh_history_panel(self):
        """Rebuild the history list from the global (cross-session) history."""
        self.history_store.clear()
        for ts, cmd in self.cmd_history:
            self.history_store.append([self._fmt_history_time(ts), cmd])
        self._scroll_history_to_end()

    def _scroll_history_to_end(self):
        # Only scroll when the list is actually on screen; scrolling an
        # unrealized TreeView (panel hidden) triggers a Gtk-CRITICAL.
        if not self.history_view.get_mapped():
            return
        model = self.history_view.get_model()   # the filter model
        n = model.iter_n_children(None)
        if n:
            path = Gtk.TreePath.new_from_indices([n - 1])
            GLib.idle_add(self.history_view.scroll_to_cell, path, None, False, 0, 0)

    def _selected_commands(self):
        model, paths = self.history_view.get_selection().get_selected_rows()
        return [model[p][1] for p in paths]

    def on_history_activated(self, view, path, column):
        """Double-click / Enter on a history row: re-run that command in the active tab."""
        cmd = view.get_model()[path][1]
        term = self.active_terminal()
        if term is not None:
            self._feed(term, cmd + "\n")
            self.status("Re-ran: %s" % cmd)

    def history_run_selected(self):
        """Run the selected history command(s) in the active tab."""
        cmds = self._selected_commands()
        term = self.active_terminal()
        if not cmds or term is None:
            return
        for c in cmds:
            self._feed(term, c + "\n")
        self.status("Re-ran %d command%s." % (len(cmds), "" if len(cmds) == 1 else "s"))

    def history_run_new_tab(self):
        """Run the selected history command(s) in a fresh tab."""
        cmds = self._selected_commands()
        if not cmds:
            return
        self._new_tab_then("\n".join(cmds) + "\n")
        self.status("Running %d command%s in a new tab."
                    % (len(cmds), "" if len(cmds) == 1 else "s"))

    def history_copy_selected(self):
        """Copy the selected history command(s) to the clipboard."""
        cmds = self._selected_commands()
        if not cmds:
            self.status("No history rows selected.")
            return
        self._clipboard().set_text("\n".join(cmds), -1)
        self.status("Copied %d command%s to clipboard."
                    % (len(cmds), "" if len(cmds) == 1 else "s"))

    def history_save(self):
        """Save the selected history command(s) to a file (.sh with shebang + exec bit, or .txt)."""
        cmds = self._selected_commands()
        if not cmds:
            self.status("Select one or more commands to save.")
            return
        default = "commands-%s.sh" % datetime.datetime.now().strftime(
            "%Y%m%d-%H%M%S")
        path = self._save_dialog(
            "Save commands", default,
            filters=[("Shell script (*.sh)", "*.sh"),
                     ("Text file (*.txt)", "*.txt")])
        if not path:
            return
        is_sh = path.lower().endswith(".sh") or not path.lower().endswith(".txt")
        body = "\n".join(cmds) + "\n"
        if is_sh:
            body = "#!/usr/bin/env bash\n" + body
        try:
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(body)
            if is_sh:
                os.chmod(path, 0o755)
            self.status("Saved %d command%s to %s"
                        % (len(cmds), "" if len(cmds) == 1 else "s", path))
        except OSError as e:
            self.status("Could not save: %s" % e)

    # ---- history right-click menu ---------------------------------------
    def _on_history_click(self, view, event):
        if event.button != 3:
            return False
        # If the right-clicked row isn't part of the selection, select just it.
        info = view.get_path_at_pos(int(event.x), int(event.y))
        sel = view.get_selection()
        if info is not None:
            path = info[0]
            if not sel.path_is_selected(path):
                sel.unselect_all()
                sel.select_path(path)
        if sel.count_selected_rows() == 0:
            return False
        self._popup_history_menu(event)
        return True

    def _confirm_clear_history(self):
        dlg = Gtk.MessageDialog(
            transient_for=self.window, modal=True,
            message_type=Gtk.MessageType.QUESTION,
            buttons=Gtk.ButtonsType.OK_CANCEL,
            text="Clear command history?")
        dlg.format_secondary_text(
            "This removes every command from the panel and deletes the saved "
            "history file. It cannot be undone.")
        resp = dlg.run()
        dlg.destroy()
        if resp == Gtk.ResponseType.OK:
            self._clear_history()

    def _popup_history_menu(self, event):
        cmds = self._selected_commands()
        n = len(cmds)
        many = "" if n == 1 else "s"
        menu = Gtk.Menu()
        menu.get_style_context().add_class("tt-root")

        def item(label, cb):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda *_: cb())
            menu.append(mi)

        item("Run in this tab" if n == 1 else "Run %d in this tab" % n,
             self.history_run_selected)
        item("Run in new tab" if n == 1 else "Run %d in new tab" % n,
             self.history_run_new_tab)
        menu.append(Gtk.SeparatorMenuItem())
        item("Copy" if n == 1 else "Copy %d command%s" % (n, many),
             self.history_copy_selected)
        item("Save\u2026", self.history_save)
        menu.append(Gtk.SeparatorMenuItem())
        item("Clear history\u2026", self._confirm_clear_history)
        menu.show_all()
        try:
            menu.popup_at_pointer(event)
        except Exception:
            menu.popup(None, None, None, None,
                       event.button, event.time)

    # ---- settings persistence -------------------------------------------
    def _load_settings(self):
        data = dict(DEFAULT_SETTINGS)
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as fh:
                saved = json.load(fh)
            if isinstance(saved, dict):
                data.update({k: saved[k] for k in saved if k in DEFAULT_SETTINGS})
        except (OSError, ValueError):
            pass
        return data

    def _save_settings(self):
        self.settings["theme"] = self.theme_name
        try:
            os.makedirs(CONFIG_DIR, exist_ok=True)
            with open(CONFIG_FILE, "w", encoding="utf-8") as fh:
                json.dump(self.settings, fh, indent=2)
        except OSError:
            pass

    # ---- file tree panel -------------------------------------------------
    def _build_file_tree_panel(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_size_request(150, -1)   # min width; the divider can grow it
        box.set_border_width(8)
        box.get_style_context().add_class("tt-panel")

        title = Gtk.Label()
        title.set_markup("<b>Files</b>")
        title.set_xalign(0.0)
        box.pack_start(title, False, False, 0)

        # navigation row (icon buttons)
        nav = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        for icon, tip, cb in [
            ("go-home-symbolic", "Home folder",
             lambda *_: self._set_tree_root(os.path.expanduser("~"))),
            ("drive-harddisk-symbolic", "Filesystem root  /",
             lambda *_: self._set_tree_root("/")),
            ("folder-open-symbolic", "Choose a folder to browse",
             lambda *_: self._choose_tree_root()),
            ("view-refresh-symbolic", "Reload the tree (keeps open folders)",
             lambda *_: self.refresh_tree()),
        ]:
            b = Gtk.Button()
            b.set_image(Gtk.Image.new_from_icon_name(icon, Gtk.IconSize.SMALL_TOOLBAR))
            b.set_relief(Gtk.ReliefStyle.NONE)
            b.set_tooltip_text(tip)
            b.connect("clicked", cb)
            nav.pack_start(b, False, False, 0)
        box.pack_start(nav, False, False, 0)

        # icon, display name, full path, is_dir, loaded
        self.tree_store = Gtk.TreeStore(str, str, str, bool, bool)
        self.tree_view = Gtk.TreeView(model=self.tree_store)
        self.tree_view.set_headers_visible(False)
        col = Gtk.TreeViewColumn("Name")
        icon_r = Gtk.CellRendererPixbuf()
        col.pack_start(icon_r, False)
        col.add_attribute(icon_r, "icon-name", 0)
        txt_r = Gtk.CellRendererText()
        col.pack_start(txt_r, True)
        col.add_attribute(txt_r, "text", 1)
        self.tree_view.append_column(col)
        self.tree_view.connect("row-expanded", self.on_tree_row_expanded)
        self.tree_view.connect("row-activated", self.on_tree_row_activated)
        self.tree_view.connect("button-press-event", self.on_tree_button)

        scr = Gtk.ScrolledWindow()
        scr.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scr.add(self.tree_view)
        box.pack_start(scr, True, True, 0)

        hint = Gtk.Label()
        hint.set_markup(
            "<small>Double-click a folder to cd there, a file to edit it. "
            "Right-click any item for Run / sudo / new-tab / file manager.</small>")
        hint.set_xalign(0.0)
        hint.set_line_wrap(True)
        hint.get_style_context().add_class("tt-muted")
        box.pack_start(hint, False, False, 0)

        self._tree_populated = False
        return box

    def on_toggle_tree(self, btn):
        """Toolbar: show or hide the file-tree panel."""
        show = btn.get_active()
        if show:
            if not self._tree_populated:
                self._reload_tree()
            self.tree_panel.set_no_show_all(False)
            self.tree_panel.show_all()
            self.body_paned.set_position(self._tree_width)
        else:
            pos = self.body_paned.get_position()
            if pos and pos > 10:
                self._tree_width = pos
            self.tree_panel.hide()
            self.tree_panel.set_no_show_all(True)

    def _set_tree_root(self, path):
        if os.path.isdir(path):
            self.tree_root = path
            self._reload_tree()

    def _choose_tree_root(self):
        dlg = Gtk.FileChooserDialog(
            title="Choose a folder to browse", parent=self.window,
            action=Gtk.FileChooserAction.SELECT_FOLDER)
        dlg.get_style_context().add_class("tt-root")
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        if os.path.isdir(self.tree_root):
            dlg.set_current_folder(self.tree_root)
        if dlg.run() == Gtk.ResponseType.OK:
            self._set_tree_root(dlg.get_filename())
        dlg.destroy()

    def _reload_tree(self):
        self.tree_store.clear()
        name = os.path.basename(self.tree_root.rstrip("/")) or self.tree_root
        root_it = self.tree_store.append(
            None, ["folder-symbolic", name, self.tree_root, True, False])
        self.tree_store.append(root_it, ["", "", "", False, False])  # dummy
        self._tree_populated = True
        self.tree_view.expand_row(self.tree_store.get_path(root_it), False)

    def refresh_tree(self):
        """Reload the tree from disk while keeping the open folders, the
        selection and the scroll position (so a refresh isn't disorienting)."""
        if not getattr(self, "_tree_populated", False):
            self._reload_tree()
            return
        expanded = self._capture_expanded_paths()
        sel_path = None
        model, it = self.tree_view.get_selection().get_selected()
        if it is not None:
            sel_path = self.tree_store.get_value(it, 2)
        vadj = self.tree_view.get_vadjustment()
        scroll = vadj.get_value() if vadj is not None else 0

        self._reload_tree()
        self._expand_paths(expanded)

        if sel_path:
            sit = self._find_tree_iter(sel_path)
            if sit is not None:
                self.tree_view.get_selection().select_iter(sit)
        if vadj is not None:
            # restore scroll after the view has relaid out
            GLib.idle_add(lambda: (vadj.set_value(
                min(scroll, vadj.get_upper() - vadj.get_page_size())), False)[-1])

    def _capture_expanded_paths(self):
        """Set of folder paths currently expanded in the tree."""
        paths = set()

        def walk(it):
            while it is not None:
                tpath = self.tree_store.get_path(it)
                if self.tree_view.row_expanded(tpath):
                    full = self.tree_store.get_value(it, 2)
                    if full:
                        paths.add(full)
                    walk(self.tree_store.iter_children(it))
                it = self.tree_store.iter_next(it)

        walk(self.tree_store.get_iter_first())
        return paths

    def _expand_paths(self, paths):
        """Re-expand the folders in `paths`, loading lazily from the top down."""
        if not paths:
            return

        def walk(it):
            while it is not None:
                if (self.tree_store.get_value(it, 3)
                        and self.tree_store.get_value(it, 2) in paths):
                    tpath = self.tree_store.get_path(it)
                    self.tree_view.expand_row(tpath, False)  # loads children
                    walk(self.tree_store.iter_children(it))
                it = self.tree_store.iter_next(it)

        walk(self.tree_store.get_iter_first())

    def _find_tree_iter(self, full_path):
        """Find the iter for a loaded row with the given full path, if visible."""
        result = [None]

        def walk(it):
            while it is not None:
                if self.tree_store.get_value(it, 2) == full_path:
                    result[0] = it
                    return True
                if walk(self.tree_store.iter_children(it)):
                    return True
                it = self.tree_store.iter_next(it)
            return False

        walk(self.tree_store.get_iter_first())
        return result[0]

    def _schedule_tree_autorefresh(self):
        """Queue a tree refresh (coalesced) when the working directory changes,
        but only while the tree is actually on screen."""
        if self._tree_refresh_pending:
            return
        if not getattr(self, "_tree_populated", False):
            return
        if not self.tree_toggle.get_active():
            return
        self._tree_refresh_pending = True
        GLib.timeout_add(400, self._do_tree_autorefresh)

    def _do_tree_autorefresh(self):
        self._tree_refresh_pending = False
        if getattr(self, "_tree_populated", False) and self.tree_toggle.get_active():
            self.refresh_tree()
        return False

    def _list_dir(self, path):
        try:
            entries = os.listdir(path)
        except OSError:
            return []
        show_hidden = self.settings.get("show_hidden", True)
        items = []
        for nm in entries:
            if not show_hidden and nm.startswith("."):
                continue
            full = os.path.join(path, nm)
            items.append((nm, full, os.path.isdir(full)))
        items.sort(key=lambda it: (not it[2], it[0].lower()))
        return items

    def on_tree_row_expanded(self, view, it, path):
        if self.tree_store.get_value(it, 4):
            return  # already loaded
        child = self.tree_store.iter_children(it)
        if child is None or self.tree_store.get_value(child, 2) != "":
            return  # not the dummy placeholder
        self.tree_store.set_value(it, 4, True)
        folder = self.tree_store.get_value(it, 2)
        for nm, full, is_dir in self._list_dir(folder):
            icon = "folder-symbolic" if is_dir else "text-x-generic-symbolic"
            child_it = self.tree_store.append(it, [icon, nm, full, is_dir, False])
            if is_dir:
                self.tree_store.append(child_it, ["", "", "", False, False])
        self.tree_store.remove(child)  # drop the dummy

    def on_tree_row_activated(self, view, path, column):
        it = self.tree_store.get_iter(path)
        full = self.tree_store.get_value(it, 2)
        if not full:
            return
        if self.tree_store.get_value(it, 3):
            self.cd_to(full)
        else:
            self.edit_path(full)

    def on_tree_button(self, view, event):
        if event.button == 3:
            info = view.get_path_at_pos(int(event.x), int(event.y))
            if info is not None:
                view.set_cursor(info[0])
                self._popup_tree_menu(event)
            return True
        return False

    def _tree_selected(self):
        model, it = self.tree_view.get_selection().get_selected()
        if it is None:
            return None
        full = model.get_value(it, 2)
        if not full:
            return None
        return (full, model.get_value(it, 3))

    def _popup_tree_menu(self, event):
        sel = self._tree_selected()
        if sel is None:
            return
        full, is_dir = sel
        menu = Gtk.Menu()
        menu.get_style_context().add_class("tt-root")

        def item(label, cb):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", lambda *_: cb())
            menu.append(mi)

        if is_dir:
            item("Open in current tab (cd)", lambda: self.tree_open_current_tab())
            item("Open in new tab (cd)", lambda: self.tree_open_new_tab())
        else:
            item("Edit", lambda: self.tree_open_editor())
            item("Run", lambda: self.tree_run_current_tab())
            item("Run with sudo", lambda: self.tree_run_sudo())
            item("Run in new tab", lambda: self.tree_run_new_tab())
            item("Run with sudo in new tab",
                 lambda: self.tree_run_new_tab(sudo=True))
        item("Open in file manager", lambda: self.tree_open_file_manager())
        sep = Gtk.SeparatorMenuItem()
        menu.append(sep)
        item("Insert path into terminal", lambda: self.tree_insert_path())
        item("Copy path", lambda: self._clipboard().set_text(full, -1))
        menu.show_all()
        try:
            menu.popup_at_pointer(event)
        except AttributeError:
            menu.popup(None, None, None, None, event.button, event.time)

    # ---- file tree actions (operate on the selected item) ---------------
    def tree_insert_path(self):
        """Insert the selected item's shell-quoted path at the terminal cursor
        (does not run anything), so you can build a command around it."""
        sel = self._tree_selected()
        if sel is None:
            self.status("Select a file or folder first.")
            return
        term = self.active_terminal()
        if term is None:
            return
        self._feed(term, shlex.quote(sel[0]) + " ")
        term.grab_focus()
        self.status("Inserted path into terminal.")

    def tree_open_file_manager(self):
        """Open the selection (or its parent folder) in the system file manager."""
        sel = self._tree_selected()
        if sel is None:
            self.status("Select a file or folder first.")
            return
        full, is_dir = sel
        target = full if is_dir else os.path.dirname(full)
        try:
            subprocess.Popen(["xdg-open", target])
            self.status("Opened %s in file manager" % target)
        except Exception as e:
            self.status("Could not open file manager: %s" % e)

    def tree_open_current_tab(self):
        """cd the active tab to the selected folder."""
        sel = self._tree_selected()
        if sel is None:
            self.status("Select a file or folder first.")
            return
        full, is_dir = sel
        self.cd_to(full if is_dir else os.path.dirname(full))

    def tree_open_new_tab(self):
        """Open a new tab and cd it to the selected folder."""
        sel = self._tree_selected()
        if sel is None:
            self.status("Select a file or folder first.")
            return
        full, is_dir = sel
        target = full if is_dir else os.path.dirname(full)
        self._new_tab_then("cd %s\n" % shlex.quote(target))
        self.status("New tab in %s" % target)

    def tree_open_editor(self):
        """Open the selected file in the configured editor."""
        sel = self._tree_selected()
        if sel is None:
            self.status("Select a file first.")
            return
        full, is_dir = sel
        if is_dir:
            self.status("Select a file to edit (this is a folder).")
            return
        self.edit_path(full)

    def tree_run_current_tab(self):
        """Run the selected script in the active tab."""
        sel = self._tree_selected()
        if sel is None:
            self.status("Select a script first.")
            return
        full, is_dir = sel
        if is_dir:
            self.status("Select a script to run (this is a folder).")
            return
        self.run_path(full)

    def tree_run_sudo(self):
        """Run the selected script with sudo in the active tab."""
        sel = self._tree_selected()
        if sel is None:
            self.status("Select a script first.")
            return
        full, is_dir = sel
        if is_dir:
            self.status("Select a script to run (this is a folder).")
            return
        self.run_path(full, sudo=True)

    def tree_run_new_tab(self, sudo=False):
        """Run the selected script in a new tab (optionally with sudo)."""
        sel = self._tree_selected()
        if sel is None:
            self.status("Select a script first.")
            return
        full, is_dir = sel
        if is_dir:
            self.status("Select a script to run (this is a folder).")
            return
        cmd = self._run_command_for(full)
        if sudo:
            cmd = "sudo " + cmd
        self._new_tab_then(cmd + "\n")
        self.status("Running %s%s in a new tab"
                    % ("(sudo) " if sudo else "", os.path.basename(full)))

    def _new_tab_then(self, command):
        """Open a new tab and feed `command` once its shell has started."""
        self.new_session()
        s = self.active_session
        GLib.timeout_add(250, lambda: (self._feed(s.terminal, command), False)[-1])

    # ---- open / run / edit scripts --------------------------------------
    def _interpreter_for(self, path):
        ext = os.path.splitext(path)[1].lower()
        return {".py": "python3", ".sh": "bash", ".bash": "bash",
                ".pl": "perl", ".rb": "ruby", ".js": "node",
                ".lua": "lua", ".php": "php"}.get(ext)

    def cd_to(self, folder):
        """Send a `cd` to the given folder in the active terminal."""
        term = self.active_terminal()
        if term is None:
            return
        self._feed(term, "cd %s\n" % shlex.quote(folder))
        self.status("cd %s" % folder)

    def _run_command_for(self, path):
        q = shlex.quote(path)
        if os.access(path, os.X_OK):
            return q  # respects the shebang
        interp = self._interpreter_for(path)
        return "%s %s" % (interp, q) if interp else "bash %s" % q

    def run_path(self, path, sudo=False):
        """Run the file at `path` in the active terminal (optionally with sudo)."""
        term = self.active_terminal()
        if term is None:
            return
        cmd = self._run_command_for(path)
        if sudo:
            cmd = "sudo " + cmd
        self._feed(term, cmd + "\n")
        self.status("Running %s%s" % ("(sudo) " if sudo else "",
                                      os.path.basename(path)))

    def edit_path(self, path):
        """Open `path` in the configured editor (inside the terminal or as an external app)."""
        editor = (self.settings.get("editor", "nano") or "nano").strip()
        if self.settings.get("editor_in_terminal", True):
            term = self.active_terminal()
            if term is None:
                return
            self._feed(term, "%s %s\n" % (editor, shlex.quote(path)))
            self.status("Editing %s in %s" % (os.path.basename(path), editor))
        else:
            try:
                subprocess.Popen([editor, path])
                self.status("Opened %s in %s" % (os.path.basename(path), editor))
            except Exception as e:
                self.status("Could not launch %s: %s" % (editor, e))

    # ---- toolbar Edit / Run / Sudo --------------------------------------
    def _resolve_file(self):
        """Use the file-tree selection if it's a file, else ask the user."""
        sel = self._tree_selected()
        if sel is not None and not sel[1]:
            return sel[0]
        return self._choose_file()

    def _choose_file(self):
        dlg = Gtk.FileChooserDialog(
            title="Choose a script", parent=self.window,
            action=Gtk.FileChooserAction.OPEN)
        dlg.get_style_context().add_class("tt-root")
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_OPEN, Gtk.ResponseType.OK)
        start = self.tree_root if os.path.isdir(self.tree_root) \
            else os.path.expanduser("~")
        dlg.set_current_folder(start)
        path = dlg.get_filename() if dlg.run() == Gtk.ResponseType.OK else None
        dlg.destroy()
        return path

    def toolbar_edit(self):
        """Toolbar Edit: edit the file-tree selection, or prompt for a file."""
        p = self._resolve_file()
        if p:
            self.edit_path(p)

    def toolbar_run(self):
        """Toolbar Run: run the file-tree selection, or prompt for a file."""
        p = self._resolve_file()
        if p:
            self.run_path(p)

    def toolbar_run_sudo(self):
        """Toolbar Sudo: run the selection with sudo, or prompt for a file."""
        p = self._resolve_file()
        if p:
            self.run_path(p, sudo=True)

    # ---- settings dialog -------------------------------------------------
    def open_settings(self):
        """Open the Settings dialog (theme, editor, file tree, screenshots)."""
        dlg = Gtk.Dialog(title="termg settings", parent=self.window)
        dlg.set_modal(True)
        dlg.set_default_size(440, -1)
        dlg.get_style_context().add_class("tt-root")
        dlg.add_button(Gtk.STOCK_CLOSE, Gtk.ResponseType.OK)

        content = dlg.get_content_area()
        content.set_border_width(18)
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        content.add(outer)

        def heading(text, first=False):
            lbl = Gtk.Label()
            lbl.set_markup("<b>%s</b>" % text)
            lbl.set_xalign(0.0)
            if not first:
                lbl.set_margin_top(12)
            lbl.get_style_context().add_class("tt-section")
            outer.pack_start(lbl, False, False, 0)

        def indent(widget):
            widget.set_margin_start(6)
            outer.pack_start(widget, False, False, 0)

        # ---- Appearance (theme) ----
        heading("Appearance", first=True)
        trow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        tlab = Gtk.Label(label="Dark theme")
        tlab.set_xalign(0.0)
        trow.pack_start(tlab, True, True, 0)
        theme_switch = Gtk.Switch()
        theme_switch.set_active(self.theme_name == "dark")
        theme_switch.set_halign(Gtk.Align.END)
        theme_switch.connect(
            "state-set",
            lambda _sw, st: (self.set_theme("dark" if st else "light"), False)[-1])
        trow.pack_start(theme_switch, False, False, 0)
        indent(trow)

        # ---- Editor ----
        heading("Editor")
        erow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        erow.pack_start(Gtk.Label(label="Command", xalign=0), False, False, 0)
        editor_entry = Gtk.Entry()
        editor_entry.set_text(self.settings.get("editor", "nano"))
        editor_entry.set_hexpand(True)
        erow.pack_start(editor_entry, True, True, 0)
        indent(erow)

        term_chk = Gtk.CheckButton(
            label="Runs inside the terminal (nano, vim, micro\u2026)")
        term_chk.set_active(self.settings.get("editor_in_terminal", True))
        indent(term_chk)

        prow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
        prow.pack_start(Gtk.Label(label="Presets:", xalign=0), False, False, 0)
        for name, in_term in [("nano", True), ("vim", True), ("micro", True),
                              ("gedit", False), ("xed", False), ("code", False)]:
            b = Gtk.Button(label=name)

            def make(n, t):
                return lambda *_: (editor_entry.set_text(n),
                                   term_chk.set_active(t))
            b.connect("clicked", make(name, in_term))
            prow.pack_start(b, False, False, 0)
        indent(prow)

        # ---- File tree ----
        heading("File tree")
        hidden_chk = Gtk.CheckButton(
            label="Show hidden files / folders")
        hidden_chk.set_active(self.settings.get("show_hidden", True))
        indent(hidden_chk)

        # ---- Screenshots ----
        heading("Screenshots")
        srow = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        srow.pack_start(Gtk.Label(label="Command", xalign=0), False, False, 0)
        shot_entry = Gtk.Entry()
        shot_entry.set_text(self.settings.get("screenshot_cmd", ""))
        shot_entry.set_placeholder_text("blank = automatic")
        shot_entry.set_hexpand(True)
        srow.pack_start(shot_entry, True, True, 0)
        indent(srow)
        shint = Gtk.Label()
        shint.set_markup(
            "<small>Leave blank to auto-detect. Use <tt>{path}</tt>, e.g. "
            "<tt>grim {path}</tt>.</small>")
        shint.set_xalign(0.0)
        shint.get_style_context().add_class("tt-muted")
        indent(shint)

        # ---- History & session ----
        heading("History & session")
        restore_chk = Gtk.CheckButton(
            label="Reopen last session's tabs and layout on launch")
        restore_chk.set_active(self.settings.get("restore_session", True))
        indent(restore_chk)
        persist_chk = Gtk.CheckButton(
            label="Remember command history between sessions")
        persist_chk.set_active(self.settings.get("persist_history", True))
        indent(persist_chk)
        redact_chk = Gtk.CheckButton(
            label="Redact inline secrets from history (passwords, tokens\u2026)")
        redact_chk.set_active(self.settings.get("redact_secrets", False))
        indent(redact_chk)
        rhint = Gtk.Label()
        rhint.set_markup(
            "<small>Passwords typed at a prompt are never recorded. This also "
            "masks secrets written on the command line, e.g. "
            "<tt>--password=\u2026</tt> or <tt>TOKEN=\u2026</tt>.</small>")
        rhint.set_xalign(0.0)
        rhint.set_line_wrap(True)
        rhint.get_style_context().add_class("tt-muted")
        indent(rhint)

        # ---- About ----
        outer.pack_start(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL),
                         False, False, 8)
        about = Gtk.Label()
        about.set_markup(
            "<small><b>termg %s</b> \u2014 a tabbed &amp; tiling terminal\n"
            "Created by Grun. Inspired by, but not connected to, "
            "Ginger Bill.</small>" % __version__)
        about.set_xalign(0.0)
        about.set_line_wrap(True)
        about.get_style_context().add_class("tt-muted")
        outer.pack_start(about, False, False, 0)

        dlg.show_all()
        dlg.run()
        # theme already applied live; persist the rest
        self.settings["editor"] = editor_entry.get_text().strip() or "nano"
        self.settings["editor_in_terminal"] = term_chk.get_active()
        self.settings["show_hidden"] = hidden_chk.get_active()
        self.settings["screenshot_cmd"] = shot_entry.get_text().strip()
        self.settings["restore_session"] = restore_chk.get_active()
        self.settings["persist_history"] = persist_chk.get_active()
        self.settings["redact_secrets"] = redact_chk.get_active()
        self._save_settings()
        if getattr(self, "_tree_populated", False):
            self._reload_tree()
        self.status("Settings saved.")
        dlg.destroy()

    # ---- misc ------------------------------------------------------------
    def _save_dialog(self, title, default_name, filters=None):
        dlg = Gtk.FileChooserDialog(
            title=title, parent=self.window,
            action=Gtk.FileChooserAction.SAVE)
        dlg.add_buttons(Gtk.STOCK_CANCEL, Gtk.ResponseType.CANCEL,
                        Gtk.STOCK_SAVE, Gtk.ResponseType.OK)
        dlg.set_do_overwrite_confirmation(True)
        dlg.set_current_name(default_name)
        if filters:
            for name, pattern in filters:
                f = Gtk.FileFilter()
                f.set_name(name)
                f.add_pattern(pattern)
                dlg.add_filter(f)
        path = None
        if dlg.run() == Gtk.ResponseType.OK:
            path = dlg.get_filename()
        dlg.destroy()
        return path

    def status(self, msg):
        """Show a transient message in the status bar."""
        self.status_label.set_text(msg)
        if self._status_timeout:
            GLib.source_remove(self._status_timeout)
        self._status_timeout = GLib.timeout_add_seconds(6, self._clear_status)

    def _clear_status(self):
        self.status_label.set_text("")
        self._status_timeout = 0
        return False


def main():
    if len(sys.argv) > 1 and sys.argv[1] in ("--version", "-V"):
        print("%s %s" % (APP_NAME, __version__))
        return
    try:
        Termg()
    except Exception as e:
        sys.stderr.write("Failed to start %s: %s\n" % (APP_NAME, e))
        sys.stderr.write(
            "Make sure the dependencies are installed:\n"
            "  sudo apt install python3-gi gir1.2-gtk-3.0 gir1.2-vte-2.91\n")
        sys.exit(1)
    Gtk.main()


if __name__ == "__main__":
    main()
