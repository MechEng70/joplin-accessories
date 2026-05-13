#!/usr/bin/env bash
# gmail_joplin_launch.sh
# Launch the Gmail → Joplin sync app.
# Location: ~/Applications/MeetingGui/gmail_joplin_launch.sh

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Ensure ANTHROPIC_API_KEY is available (load from ~/.bashrc or ~/.zshrc if not set)
if [[ -z "$ANTHROPIC_API_KEY" ]]; then
    # Attempt to source shell config — adjust path if your key is set elsewhere
    for rc in "$HOME/.zshrc" "$HOME/.bashrc" "$HOME/.profile"; do
        [[ -f "$rc" ]] && source "$rc" 2>/dev/null && break
    done
fi

if [[ -z "$ANTHROPIC_API_KEY" ]]; then
    echo "WARNING: ANTHROPIC_API_KEY is not set. Claude summarization will fail."
fi

exec python3 "$SCRIPT_DIR/gmail_joplin_sync.py" "$@"
