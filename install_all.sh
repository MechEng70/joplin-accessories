#!/usr/bin/env bash
# install_all.sh - Install all Joplin Accessories tools
# Run from the repo root: ./install_all.sh
# Pass tool names to install selectively: ./install_all.sh meeting gmail tasks
set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INSTALL_MEETING=false
INSTALL_GMAIL=false
INSTALL_TASKS=false

# Parse arguments - default to all if none provided
if [ $# -eq 0 ]; then
    INSTALL_MEETING=true
    INSTALL_GMAIL=true
    INSTALL_TASKS=true
else
    for arg in "$@"; do
        case "$arg" in
            meeting) INSTALL_MEETING=true ;;
            gmail)   INSTALL_GMAIL=true ;;
            tasks)   INSTALL_TASKS=true ;;
            *)       echo "Unknown tool: $arg. Valid options: meeting, gmail, tasks"; exit 1 ;;
        esac
    done
fi

echo "=== Joplin Accessories Installer ==="
echo ""

# Make all install scripts executable
find "$REPO_ROOT" -name "install.sh" -exec chmod +x {} \;

if $INSTALL_MEETING; then
    echo "──────────────────────────────────────"
    bash "$REPO_ROOT/meeting-recorder/linux/install.sh"
    echo ""
fi

if $INSTALL_GMAIL; then
    echo "──────────────────────────────────────"
    bash "$REPO_ROOT/gmail-joplin-sync/install.sh"
    echo ""
fi

if $INSTALL_TASKS; then
    echo "──────────────────────────────────────"
    bash "$REPO_ROOT/joplin-task-sync/install.sh"
    echo ""
fi

echo "══════════════════════════════════════"
echo "All selected tools installed."
echo ""
echo "Next steps:"
echo "  1. Edit ~/Applications/MeetingGui/.env and add your API keys."
if $INSTALL_GMAIL; then
echo "  2. Place credentials.json at ~/.config/gmail-to-joplin/credentials.json"
fi
echo "  3. Log out and back in if app icons don't appear in the launcher."
echo "     (Or run: update-desktop-database ~/.local/share/applications)"
