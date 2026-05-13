#!/usr/bin/env bash
# install.sh - Meeting Recorder Linux installer (Fedora 43 / COSMIC)
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
APP_DIR="$HOME/Applications/MeetingGui"
DATA_DIR="$HOME/.local/share/meeting-recorder"
DESKTOP_DIR="$HOME/.local/share/applications"
ICON_DIR="$HOME/.local/share/icons/hicolor/scalable/apps"
ENV_SOURCE="$REPO_ROOT/.env.example"

echo "=== Meeting Recorder - Linux Installer ==="

# 1. System dependencies
echo "[1/7] Checking system dependencies..."
if ! python3 -c "import gi" &>/dev/null; then
    echo "  Installing GTK4 Python bindings..."
    sudo dnf install -y python3-gobject gtk4 || {
        echo "ERROR: Run: sudo dnf install python3-gobject gtk4"; exit 1
    }
fi
if ! command -v ffmpeg &>/dev/null; then
    echo "  Installing ffmpeg..."
    sudo dnf install -y ffmpeg || {
        echo "ERROR: Run: sudo dnf install ffmpeg"; exit 1
    }
fi

# 2. Python dependencies
echo "[2/7] Installing Python dependencies..."
pip install --user -r "$SCRIPT_DIR/requirements.txt"

# 3. App directory and files
echo "[3/7] Copying application files..."
mkdir -p "$APP_DIR" "$DATA_DIR/pending"
cp "$SCRIPT_DIR/meeting_gui.py" "$APP_DIR/"
[ -f "$REPO_ROOT/meeting-recorder/profiles.json" ] && \
    cp "$REPO_ROOT/meeting-recorder/profiles.json" "$APP_DIR/"

# 4. Icon
echo "[4/7] Installing icon..."
mkdir -p "$ICON_DIR"
cp "$REPO_ROOT/assets/icons/com.fractionalmedtech.meeting-recorder.svg" "$ICON_DIR/"
gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true

# 5. Environment file
echo "[5/7] Configuring environment..."
if [ ! -f "$APP_DIR/.env" ]; then
    cp "$ENV_SOURCE" "$APP_DIR/.env"
    echo "  Created $APP_DIR/.env -- fill in your API keys before running."
else
    echo "  .env already exists, skipping."
fi

# 6. Launch script
echo "[6/7] Writing launch script..."
cat > "$APP_DIR/meeting_recorder_launch.sh" << 'EOF'
#!/usr/bin/env bash
set -a
source "$(dirname "$0")/.env"
set +a
exec python3 "$(dirname "$0")/meeting_gui.py" "$@"
EOF
chmod +x "$APP_DIR/meeting_recorder_launch.sh"

# 7. Desktop entry
echo "[7/7] Installing desktop entry..."
mkdir -p "$DESKTOP_DIR"
sed "s|APP_DIR|$APP_DIR|g" \
    "$REPO_ROOT/meeting-recorder/meeting-recorder.desktop" \
    > "$DESKTOP_DIR/meeting-recorder.desktop"
update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true

echo ""
echo "=== Done ==="
echo "Edit $APP_DIR/.env and add your API keys, then launch from the app launcher."
