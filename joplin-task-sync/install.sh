#!/usr/bin/env bash
# install.sh - Joplin Task Sync installer (Fedora 43 / COSMIC)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
APP_DIR="$HOME/Applications/MeetingGui"
STATE_DIR="$HOME/.local/share/joplin-task-sync"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
ENV_SOURCE="$REPO_ROOT/.env.example"

echo "=== Joplin Task Sync Installer ==="

# 1. System dependencies
echo "[1/6] Checking system dependencies..."
if ! python3 -c "import gi" &>/dev/null; then
    echo "  Installing GTK4 Python bindings..."
    sudo dnf install -y python3-gobject gtk4 || {
        echo "ERROR: Run: sudo dnf install python3-gobject gtk4"; exit 1
    }
fi

# 2. Python dependencies
echo "[2/6] Installing Python dependencies..."
pip install --user -r "$SCRIPT_DIR/requirements.txt"

# 3. App directory and files
echo "[3/6] Copying application files..."
mkdir -p "$APP_DIR" "$STATE_DIR"
cp "$SCRIPT_DIR/joplin_task_sync.py" "$APP_DIR/"
cp "$SCRIPT_DIR/joplin_task_sync_gui.py" "$APP_DIR/"

# 4. Icon
echo "[4/6] Installing icon..."
mkdir -p "$ICON_DIR"
cp "$REPO_ROOT/assets/icons/com.fractionalmedtech.joplin-task-sync-gui.svg" "$ICON_DIR/"
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

# 5. Environment file
echo "[5/6] Configuring environment..."
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$ENV_SOURCE" "$APP_DIR/.env"
    echo "  Created $APP_DIR/.env -- fill in your API keys before running."
else
    echo "  .env already exists, skipping."
fi

# Launch script
cat > "$APP_DIR/joplin_task_sync_launch.sh" << 'EOF'
#!/usr/bin/env bash
set -a
source "$(dirname "$0")/.env"
set +a
exec python3 "$(dirname "$0")/joplin_task_sync_gui.py" "$@"
EOF
chmod +x "$APP_DIR/joplin_task_sync_launch.sh"

# 6. Desktop entry
echo "[6/6] Installing desktop entry..."
mkdir -p "$DESKTOP_DIR"
sed "s|APP_DIR|$APP_DIR|g" \
    "$SCRIPT_DIR/joplin-task-sync-gui.desktop" \
    > "$DESKTOP_DIR/joplin-task-sync-gui.desktop"
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo ""
echo "=== Done ==="
echo "Confirm JOPLIN_TOKEN and ANTHROPIC_API_KEY are set in $APP_DIR/.env before running."
