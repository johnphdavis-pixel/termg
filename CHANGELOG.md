# Changelog

All notable changes to termg are documented here. The format is loosely based
on [Keep a Changelog](https://keepachangelog.com/), and the project aims to
follow [Semantic Versioning](https://semver.org/).

## [0.4.2]

### Added
- A **dropdown caret next to the Cast button** opens the target chooser directly
  (no need to know about right-click).

## [0.4.1]

### Changed
- **Cast (broadcast) now works in both tabbed and tiled view**, and you can
  **choose which tabs/tiles receive it**. Right-click the Cast button for a
  checklist (plus “all” / “none”). Target tabs are tinted orange and target
  tiles get the orange border; the tab you're typing in is the source and isn't
  marked. New tabs opened while casting join automatically.

## [0.4.0]

### Added
- **Command history is remembered between sessions.** It's saved to
  `~/.config/termg/history.jsonl` and reloaded on launch. Turn it off with
  “Remember command history between sessions” in Settings, or wipe it via the
  history panel's right-click → “Clear history…”.

### Changed
- The history panel is now a single **global** log across all tabs (previously
  per-tab). Commands from earlier days show their date alongside the time.

## [0.3.2]

### Fixed
- **Session restore now reopens each tab in its real working directory.** It
  previously depended on the shell emitting an OSC 7 escape to track the
  directory, so tabs frequently came back in your home folder. The directory is
  now read straight from the shell process (`/proc`), which always works.

## [0.3.1]

### Fixed
- Reloading the file tree now keeps the open folders, the selected item and the
  scroll position, instead of collapsing back to the root.

### Added
- The file tree auto-refreshes when the active tab changes directory.

## [0.3.0]

### Added
- **Clipboard history panel** (“Clipboard” toolbar button), styled like the
  History panel and sitting just below it. While it's open it records the text
  you copy (newest first), with a search box, **double-click to copy an entry
  back**, and a right-click menu to paste into the terminal, save to a file, or
  remove entries. Clipboard contents are kept **in memory only** — never written
  to disk — and capture only runs while the panel is open. Its open/closed state
  is remembered across launches like the other panels.

## [0.2.1]

### Changed
- The file-tree and history panels now remember whether they're open and how
  wide they are, and restore that on launch.
- Moved the **Cast** (broadcast) button to sit just after **Sudo**
  (Sudo │ Cast │ Copy…).

## [0.2.0]

### Added
- **Clickable URLs** — Ctrl+click a link in the output to open it (regex
  matching plus OSC 8 hyperlinks).
- **Find in scrollback** — Ctrl+Shift+F opens a find bar with next/previous
  (Enter / Shift+Enter), Esc to close.
- **File tree → Insert path into terminal** (right-click): drops the item's
  shell-quoted path at the cursor without running anything.
- **Broadcast input** (toolbar “Cast”): mirror typing to every tile. Acts only
  in tiled view with 2+ panes, only from the active pane (no feedback loop), and
  marks every pane with an orange border while on.
- **Session restore**: reopens the last session's tabs, working directories and
  layout (tabbed/tiled) on launch. Restored shells start in their saved
  directory (no `cd` in history). Toggle in Settings.
- **Remembered font zoom** across launches.
- **Optional secret redaction**: masks inline secrets in history (e.g.
  `--password=`, `TOKEN=`, mysql `-p`, bearer tokens). Off by default; toggle in
  Settings.

### Changed
- Command-history recording now keys off the terminal's **foreground process
  group** — only input typed at the shell prompt is recorded. This is the
  general solution that supersedes the 0.1.x password heuristics: it keeps out
  passwords (including sudo pwfeedback) *and* stops full-screen apps (vim, less,
  htop, REPLs) from polluting the history.

## [0.1.2]

### Fixed
- Passwords entered at a **sudo `pwfeedback`** prompt (the kind that shows
  `*` per character) were still recorded, because that path reads in raw mode
  rather than canonical mode. Password entry is now detected by the on-screen
  prompt as well as the terminal mode, covering both. Commands that merely
  contain the word “password” are still recorded normally.
- Screenshots were saved with the old `tileterm-` filename prefix; they now use
  `termg-`.

## [0.1.1]

### Security
- Command history no longer records text typed at silent password prompts
  (sudo, ssh, su, passwd, `read -s`, etc.). These are detected as canonical
  reads with echo disabled, and their keystrokes are skipped entirely.

### Added
- `termg --version` / `-V`, and the version is shown in Settings → About.

## [0.1.0] — first public release

Initial release.

### Added
- Real PTY-backed terminal tabs (GTK 3 + VTE 2.91).
- Optional tiled (split) layout, stacking rows first then columns, with an
  always-visible tab switcher that works in both tabbed and tiled modes.
- Tabs auto-named from the working directory; double-click to rename,
  right-click to rename / pin / close, pinned tabs hide their close button.
- Resizable file-tree browser: double-click a folder to `cd`, a file to edit;
  right-click for Run / Run with sudo / Run in new tab / Run with sudo in new
  tab / Edit / Open in file manager / Copy path.
- Resizable, searchable command-history panel (substring or `.*` regex) with a
  clock-only time column; double-click to re-run, right-click to run in this/new
  tab, copy, or save (`.sh` with shebang + exec bit, or `.txt`).
- Toolbar: Copy selection / Copy view / Copy all / Save journal, Paste and
  paste line-by-line, screenshots, and a script Edit / Run / Sudo.
- Self-contained light/dark theme (chosen in Settings) that does not depend on
  the system GTK theme.
- Screenshots: built-in capture on X11, automatic external-tool fallback on
  Wayland, or a custom command.
- User-local installer (`install.sh`), `.desktop` entry, and scalable icon.

### Known limitations
- Command history is reconstructed from the keystroke stream, so Tab-completed
  commands are not captured perfectly.
- The built-in screenshot is X11-only; Wayland uses an external tool.
