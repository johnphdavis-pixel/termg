#!/usr/bin/env bash
# Installs termg for the current user (no root needed).
set -e
APP=termg
SRC="$(cd "$(dirname "$0")" && pwd)"

LIB="$HOME/.local/share/$APP"
BIN="$HOME/.local/bin"
ICON="$HOME/.local/share/icons/hicolor/scalable/apps"
DESK="$HOME/.local/share/applications"
mkdir -p "$LIB" "$BIN" "$ICON" "$DESK"

# Remove any previous installs under this app's old names.
for old in tilde tileterm; do
  rm -f "$BIN/$old" "$DESK/$old.desktop" "$ICON/$old.svg" \
        "$HOME/.local/share/$old/$old.py" 2>/dev/null || true
  rmdir "$HOME/.local/share/$old" 2>/dev/null || true
done

install -m 644 "$SRC/termg.py"  "$LIB/termg.py"
install -m 644 "$SRC/termg.svg" "$ICON/$APP.svg"

cat > "$BIN/$APP" <<EOF2
#!/usr/bin/env bash
exec python3 "$LIB/termg.py" "\$@"
EOF2
chmod +x "$BIN/$APP"

cat > "$DESK/$APP.desktop" <<EOF2
[Desktop Entry]
Type=Application
Name=termg
GenericName=Terminal
Comment=Tabbed & tiling terminal with command history, file tree and a script runner
Exec=$BIN/$APP
Icon=$APP
Terminal=false
Categories=System;TerminalEmulator;Utility;
Keywords=terminal;shell;console;command;tiling;tabs;
StartupNotify=true
EOF2

update-desktop-database "$DESK" 2>/dev/null || true
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

echo "Installed termg."
echo "If 'termg' isn't found, add ~/.local/bin to your PATH:"
echo '  echo '\''export PATH="$HOME/.local/bin:$PATH"'\'' >> ~/.bashrc && source ~/.bashrc'
echo "Then launch it from the menu, or run (all lowercase):  termg"
