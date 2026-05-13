#!/usr/bin/env python3
"""
joplin_task_sync.py

Standalone Joplin task synchronization script.
Does NOT modify or depend on record_meeting.py.

Design philosophy:
  The "00 - Task List" note is a DERIVED VIEW rebuilt from scratch on every
  sync. This eliminates all issues caused by Joplin's editors stripping
  HTML comments (<!-- tid:xxx -->) when a checkbox is toggled.

Workflow:
  1. Sweep recently-updated source notes. Inject stable task IDs into any
     task line that lacks one. Register new tasks in state.
  2. Scan the existing task list(s) for CHECKED items. For each one found,
     propagate the [x] back to the source note (tid or text match).
     Mark those tasks completed in state.
  3. Read every source note that has active tasks (from state) to confirm
     current checked state. Any task now [x] in its source note is marked
     completed in state.
  4. Rebuild each "00 - Task List" note from scratch: only unchecked tasks,
     sorted by source note title DESCENDING (most recent meeting at top).
  5. Write all dirty notes to Joplin.

Bidirectional completion:
  - Check a task in the SOURCE NOTE → removed from task list on next sync.
  - Check a task in the TASK LIST  → propagated to source note, then removed.
  Both paths work regardless of whether Joplin's editor strips the tid comment.

State file: ~/.local/share/joplin-task-sync/state.json
Log file  : ~/.local/share/joplin-task-sync/sync.log

Usage:
  python3 joplin_task_sync.py              # sweep notes updated since last run
  python3 joplin_task_sync.py --reset      # clear last_run, rescan everything
  python3 joplin_task_sync.py --dry-run    # log only, write nothing
  python3 joplin_task_sync.py --note-id X  # targeted sync for one note

Environment:
  JOPLIN_TOKEN   Joplin Web Clipper API token (must be exported)
"""

import argparse
import json
import logging
import os
import random
import re
import string
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

import requests

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

JOPLIN_BASE_URL = "http://localhost:41184"
JOPLIN_TOKEN    = os.environ.get("JOPLIN_TOKEN", "")

STATE_DIR  = Path.home() / ".local" / "share" / "joplin-task-sync"
STATE_FILE = STATE_DIR / "state.json"
LOG_FILE   = STATE_DIR / "sync.log"
CONFIG_FILE = STATE_DIR / "config.json"

TASK_LIST_TITLE = "00 - Task List"
KANBAN_TITLE    = "00 - Kanban Board"
KANBAN_DONE_COLUMN = "Done"          # Cards dragged here are marked complete

WEEKLY_BOARD_TITLE    = "00 - Weekly Board"
TASK_LIST_AT_ROOT = True

EXCLUDED_ROOT_NOTEBOOKS = {"Personal", "Trash"}
EXCLUDED_NOTE_TITLES    = {"Todo", "To Do List", "00 - To Do List"}

TASK_RE = re.compile(
    r'^(?P<indent>\s*)- \[(?P<checked>[ x])\] (?P<text>.+?)(?:\s*<!-- tid:(?P<tid>[a-z0-9]{6}) -->)?$'
)

# Owner / due-date extraction patterns.
# Priority: explicit (Owner: X) field > **Name:** bold prefix > Name: plain prefix.
OWNER_EXPLICIT_RE = re.compile(r'\(Owner:\s*([^,;)\n]+)', re.IGNORECASE)
OWNER_BOLD_RE     = re.compile(r'^\*\*([^*]+?):?\*\*')   # handles **Name:** and **Name**:
OWNER_PLAIN_RE    = re.compile(r'^([A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?)\s*:')
DUE_RE            = re.compile(r'Due:\s*([^,;)\n]+)', re.IGNORECASE)

# Owner values that map to the "Unassigned" column
_UNASSIGNED_OWNERS = {
    "not specified", "not identified", "not mentioned", "n/a",
    "speaker 1", "speaker 2", "speaker 3", "participant",
    "team", "consultant", "not stated", "various",
}

# Regex to match the full owner specification inside a parenthetical,
# stopping before ", Due:" or the closing ")".
# Handles multi-owner fields: (Owner: Speaker 2 drafts, Speaker 1 structures, Due: ...)
OWNER_REPLACE_RE = re.compile(
    r'\(Owner:\s*.+?(?=,\s*[Dd]ue:|\))',
    re.DOTALL,
)


def _is_unassigned_name(name: str) -> bool:
    """
    Return True if this owner name should map to the 'Unassigned' column.
    Handles exact matches AND "Speaker N..." variants produced by meeting
    transcription (e.g. "Speaker 2 drafts", "Speaker 1 structures").
    """
    n = name.lower().strip()
    if n in _UNASSIGNED_OWNERS:
        return True
    # Catch "Speaker 1 something", "Speaker 2 drafts", etc.
    if re.match(r'^speaker\s+\d', n):
        return True
    return False

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logging() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
        handlers=[
            logging.FileHandler(LOG_FILE),
            logging.StreamHandler(sys.stdout),
        ],
    )

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state() -> dict:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.error(f"State load failed: {e}. Starting fresh.")
    return {
        "last_run": None,
        "notebook_task_lists":    {},
        "notebook_kanban_boards": {},
        "notebook_weekly_boards": {},
        "tasks":     {},
        "conflicts": [],
    }

def save_state(state: dict) -> None:
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f, indent=2)
    except OSError as e:
        log.error(f"State save failed: {e}")


def load_config() -> dict:
    """Load runtime config (exclusion list) written by the GUI."""
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"excluded_notebooks": sorted(EXCLUDED_ROOT_NOTEBOOKS)}


def save_config(config: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)

# ---------------------------------------------------------------------------
# Joplin REST API client
# ---------------------------------------------------------------------------

class JoplinClient:
    def __init__(self, base_url: str, token: str):
        self.base_url = base_url.rstrip("/")
        self.token    = token
        self.session  = requests.Session()
        self.session.headers.update({"Content-Type": "application/json"})

    def _p(self, extra: dict = None) -> dict:
        p = {"token": self.token}
        if extra:
            p.update(extra)
        return p

    def ping(self) -> bool:
        try:
            return self.session.get(f"{self.base_url}/ping", params=self._p(), timeout=5).status_code == 200
        except requests.RequestException:
            return False

    def get_note(self, nid: str) -> dict:
        r = self.session.get(f"{self.base_url}/notes/{nid}",
                             params=self._p({"fields": "id,title,body,parent_id,updated_time"}))
        r.raise_for_status()
        return r.json()

    def update_note(self, nid: str, body: str) -> None:
        self.session.put(f"{self.base_url}/notes/{nid}",
                         params=self._p(), json={"body": body}).raise_for_status()

    def create_note(self, title: str, body: str, parent_id: str) -> dict:
        r = self.session.post(f"{self.base_url}/notes",
                              params=self._p(), json={"title": title, "body": body, "parent_id": parent_id})
        r.raise_for_status()
        return r.json()

    def get_all_notebooks(self) -> list[dict]:
        results, page = [], 1
        while True:
            r = self.session.get(f"{self.base_url}/folders",
                                 params=self._p({"page": page, "fields": "id,title,parent_id"}))
            r.raise_for_status()
            data = r.json()
            results.extend(data.get("items", []))
            if not data.get("has_more"):
                break
            page += 1
        return results

    def get_all_notes(self, since_ms: Optional[int] = None) -> list[dict]:
        results, page = [], 1
        while True:
            r = self.session.get(
                f"{self.base_url}/notes",
                params=self._p({"page": page, "fields": "id,title,body,parent_id,updated_time",
                                "order_by": "updated_time", "order_dir": "DESC"}))
            r.raise_for_status()
            data  = r.json()
            items = data.get("items", [])
            if since_ms is not None:
                fresh = [n for n in items if n.get("updated_time", 0) > since_ms]
                results.extend(fresh)
                if len(fresh) < len(items):
                    break
            else:
                results.extend(items)
            if not data.get("has_more"):
                break
            page += 1
        return results

    def delete_note(self, nid: str) -> None:
        self.session.delete(f"{self.base_url}/notes/{nid}",
                            params=self._p()).raise_for_status()

    def search_notes_by_title(self, title: str) -> list[dict]:
        r = self.session.get(f"{self.base_url}/search",
                             params=self._p({"query": f'title:"{title}"',
                                            "fields": "id,title,parent_id,body"}))
        r.raise_for_status()
        return r.json().get("items", [])

# ---------------------------------------------------------------------------
# Notebook helpers
# ---------------------------------------------------------------------------

def build_notebook_map(client: JoplinClient) -> dict:
    return {nb["id"]: nb for nb in client.get_all_notebooks()}

def get_root_notebook(nb_id: str, nb_map: dict) -> dict:
    cur = nb_map.get(nb_id)
    if cur is None:
        return {}
    while cur.get("parent_id"):
        parent = nb_map.get(cur["parent_id"])
        if parent is None:
            break
        cur = parent
    return cur

def get_sub_path(nb_id: str, root_id: str, nb_map: dict) -> str:
    path, cur = [], nb_id
    while cur and cur != root_id:
        nb = nb_map.get(cur)
        if nb is None:
            break
        path.append(nb["title"])
        cur = nb.get("parent_id", "")
    path.reverse()
    return " > ".join(path)

# ---------------------------------------------------------------------------
# Task ID generation
# ---------------------------------------------------------------------------

_TID_CHARS = string.ascii_lowercase + string.digits

def gen_tid(existing: set) -> str:
    while True:
        tid = "".join(random.choices(_TID_CHARS, k=6))
        if tid not in existing:
            existing.add(tid)
            return tid

# ---------------------------------------------------------------------------
# Task parsing and manipulation
# ---------------------------------------------------------------------------

def parse_tasks(body: str) -> list[dict]:
    out = []
    for i, line in enumerate(body.splitlines()):
        m = TASK_RE.match(line)
        if m:
            out.append({
                "line_index": i,
                "indent":     m.group("indent"),
                "checked":    m.group("checked") == "x",
                "text":       m.group("text").strip(),
                "tid":        m.group("tid"),
                "raw":        line,
            })
    return out

def _norm(text: str) -> str:
    """Normalize for fuzzy text matching (strip bold markers, lower, strip)."""
    return re.sub(r'\*+', '', text).strip().lower()

def _task_line(indent: str, checked: bool, text: str, tid: str) -> str:
    return f"{indent}- [{'x' if checked else ' '}] {text} <!-- tid:{tid} -->"

def inject_tids(body: str, existing: set) -> tuple[str, bool]:
    lines, changed = body.splitlines(), False
    for i, line in enumerate(lines):
        m = TASK_RE.match(line)
        if m and m.group("tid") is None:
            lines[i] = _task_line(m.group("indent"), m.group("checked") == "x",
                                   m.group("text").strip(), gen_tid(existing))
            changed = True
    return ("\n".join(lines), changed)

def mark_checked_in_body(body: str, tid: str, fallback_text: str = "") -> tuple[str, bool]:
    """Mark a task [x] in body. Matches by tid first, then by text."""
    lines, changed = body.splitlines(), False
    for i, line in enumerate(lines):
        m = TASK_RE.match(line)
        if m and not m.group("checked") == "x":
            match = (m.group("tid") == tid) if tid else False
            if not match and fallback_text:
                match = _norm(m.group("text")) == _norm(fallback_text)
            if match:
                lines[i] = _task_line(m.group("indent"), True, m.group("text").strip(),
                                       m.group("tid") or tid)
                changed = True
                break
    return ("\n".join(lines), changed)


def update_task_line_in_body(body: str, tid: str, new_text: str, new_checked: bool) -> tuple[str, bool]:
    """
    Find the task line with the given tid and update its text and checked state.
    Returns (updated_body, was_modified).
    """
    lines, changed = body.splitlines(), False
    for i, line in enumerate(lines):
        m = TASK_RE.match(line)
        if m and m.group("tid") == tid:
            new_line = _task_line(m.group("indent"), new_checked, new_text, tid)
            if new_line != line:
                lines[i] = new_line
                changed   = True
            break
    return ("\n".join(lines), changed)

# ---------------------------------------------------------------------------
# Task list note helpers
# ---------------------------------------------------------------------------

def get_or_create_task_list(client: JoplinClient, root_nb_id: str,
                             root_nb_name: str, state: dict) -> str:
    """Return the note ID of the task list for this root notebook, creating if needed."""
    cached = state["notebook_task_lists"].get(root_nb_id)
    if cached:
        try:
            client.get_note(cached)
            return cached
        except Exception:
            log.warning(f"Cached task list {cached} for '{root_nb_name}' gone. Searching...")

    for c in client.search_notes_by_title(TASK_LIST_TITLE):
        if c.get("parent_id") == root_nb_id and c.get("title") == TASK_LIST_TITLE:
            state["notebook_task_lists"][root_nb_id] = c["id"]
            log.info(f"Found existing '{TASK_LIST_TITLE}' in '{root_nb_name}'.")
            return c["id"]

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    note    = client.create_note(
        TASK_LIST_TITLE,
        f"# {root_nb_name} — Task List\n*Last synced: {now_str}*\n\n---\n",
        root_nb_id,
    )
    state["notebook_task_lists"][root_nb_id] = note["id"]
    log.info(f"Created '{TASK_LIST_TITLE}' in '{root_nb_name}'.")
    return note["id"]

def build_task_list_body(root_nb_name: str, source_groups: dict, now_str: str) -> str:
    """
    Build the task list note body from scratch.

    source_groups: {source_note_id: {title, sub_path, tasks: [{tid, text}]}}
    Sections are sorted by source note title DESCENDING (newest date first).
    """
    body = (
        f"# {root_nb_name} — Task List\n"
        f"*Last synced: {now_str}*\n\n"
        "---\n"
    )
    date_str = datetime.now().strftime("%Y-%m-%d")
    for src_nid, group in sorted(source_groups.items(), key=lambda x: x[1]["title"], reverse=True):
        sub_part = f" — {group['sub_path']}" if group["sub_path"] else ""
        body += f"\n## [{group['title']}](:/{src_nid}){sub_part} — {date_str}\n"
        for task in group["tasks"]:
            body += f"- [ ] {task['text']} <!-- tid:{task['tid']} -->\n"
    return body

# ---------------------------------------------------------------------------
# Owner / due-date extraction
# ---------------------------------------------------------------------------

def extract_owners(text: str) -> list[str]:
    """
    Return a list of owner names parsed from task text.
    Falls back to ["Unassigned"] when no owner can be determined.
    Tasks with multiple owners ("Vipul / Jason") produce one entry per owner
    so the card appears in each person's kanban column.
    """
    # 1. Explicit (Owner: X, ...) field — most reliable source
    m = OWNER_EXPLICIT_RE.search(text)
    if m:
        raw = m.group(1).strip()
        if _is_unassigned_name(raw):
            return ["Unassigned"]
        parts = re.split(r'\s*/\s*|\s+and\s+', raw)
        owners = [
            p.strip() for p in parts
            if p.strip() and not _is_unassigned_name(p.strip())
        ]
        if owners:
            return owners
        return ["Unassigned"]

    # 2. **Name:** bold prefix at line start
    m = OWNER_BOLD_RE.match(text)
    if m:
        name = m.group(1).strip()
        if not _is_unassigned_name(name):
            return [name]

    # 3. Name: plain prefix (single or two-word proper name starting with uppercase)
    m = OWNER_PLAIN_RE.match(text)
    if m:
        name = m.group(1).strip()
        if not _is_unassigned_name(name):
            return [name]

    return ["Unassigned"]


def extract_due(text: str) -> str:
    """Return the Due value from task text, or empty string."""
    m = DUE_RE.search(text)
    if m:
        due = m.group(1).strip()
        if due.lower() not in _UNASSIGNED_OWNERS and due.lower() not in {
            "not specified", "not identified", "not mentioned", "n/a",
        }:
            return due
    return ""


# ---------------------------------------------------------------------------
# Kanban read-back helpers
# ---------------------------------------------------------------------------

_KANBAN_TID_RE = re.compile(r'<!-- tid:([a-z0-9]{6}) -->')
_KANBAN_DUE_RE = re.compile(r'^\*\*Due:\*\*\s*(.+)$')


def parse_kanban_columns(body: str) -> dict[str, list[dict]]:
    """
    Parse a YesYouKan kanban body into a column → cards mapping.

    Returns:
        {column_name: [{"tid": str|None, "due": str}, ...]}

    H1 headings = columns.  H2 headings = card titles.
    Card description lines carry <!-- tid:xxx --> and **Due:** fields.
    Lines before the first real H1 (board title, last-synced) are skipped.
    The kanban-settings code block at the end is ignored.
    """
    columns: dict[str, list[dict]] = {}
    current_col:  str | None       = None
    current_card: dict | None      = None
    in_code_block                  = False

    for line in body.splitlines():
        stripped = line.strip()

        # Ignore the ```kanban-settings``` block
        if stripped.startswith("```"):
            in_code_block = not in_code_block
            continue
        if in_code_block:
            continue

        # H1 = column
        if stripped.startswith("# ") and not stripped.startswith("## "):
            col_name = stripped[2:].strip()
            # Skip any old-format board-title H1 that slipped through
            if "kanban board" in col_name.lower():
                continue
            # Strip leading emoji + space added by _column_label()
            # e.g. "🔵 Jason" → "Jason", "📥 Unassigned" → "Unassigned"
            col_name = re.sub(r'^[^\w\s]+\s+', '', col_name).strip()
            current_col  = col_name
            current_card = None
            if current_col not in columns:
                columns[current_col] = []

        # H2 = card title
        elif stripped.startswith("## ") and current_col is not None:
            current_card = {"tid": None, "due": ""}
            columns[current_col].append(current_card)

        # Card description lines
        elif current_card is not None:
            m = _KANBAN_TID_RE.search(stripped)
            if m:
                current_card["tid"] = m.group(1)
            m = _KANBAN_DUE_RE.match(stripped)
            if m:
                current_card["due"] = m.group(1).strip()

    return columns


def update_owner_in_text(text: str, new_owner: str) -> str:
    """
    Replace the owner specification in task text with new_owner.

    Uses OWNER_REPLACE_RE which captures the full owner portion up to
    ", Due:" or ")" — handles single owners, slash-separated owners, and
    comma-separated multi-owner fields like:
        (Owner: Speaker 2 drafts, Speaker 1 structures, Due: Not identified)
        → (Owner: Jonathan, Due: Not identified)

    If no explicit (Owner: ...) field exists, appends one.
    """
    if OWNER_REPLACE_RE.search(text):
        return OWNER_REPLACE_RE.sub(f"(Owner: {new_owner}", text, count=1)
    return f"{text.rstrip()} (Owner: {new_owner})"


def update_due_in_text(text: str, new_due: str) -> str:
    """
    Replace the Due value in task text with new_due.
    If no Due field exists but an Owner field does, appends Due inside it.
    If neither exists, appends a standalone (Due: ...) suffix.
    """
    if DUE_RE.search(text):
        return DUE_RE.sub(f"Due: {new_due}", text, count=1)
    # Try to inject inside existing (Owner: ...) parenthetical
    m = re.search(r'\(Owner:[^)]+\)', text)
    if m:
        # Replace closing paren with ", Due: new_due)"
        span = m.span()
        return text[:span[1] - 1] + f", Due: {new_due})"+ text[span[1]:]
    return f"{text.rstrip()} (Due: {new_due})"


def _card_title(text: str) -> str:
    """
    Produce a concise Kanban card title from full task text.
    Strips owner prefix and trailing (Owner: X, Due: Y) metadata, caps at 80 chars.
    """
    t = text
    t = re.sub(r'^\*\*[^*]+\*\*\s*', '', t)                        # **Name:** prefix (colon inside or after)
    t = re.sub(r'^[A-Z][a-zA-Z]+(?:\s+[A-Z][a-zA-Z]+)?\s*:\s*', '', t)  # Name: prefix
    t = re.sub(r'\s*\(Owner:[^)]*\)', '', t)                         # (Owner: ...) suffix
    t = t.strip()
    return (t[:77] + "...") if len(t) > 80 else (t or text[:80])


# ---------------------------------------------------------------------------
# Kanban board note helpers  (YesYouKan format)
# ---------------------------------------------------------------------------

def get_or_create_kanban_note(
    client: JoplinClient,
    root_nb_id: str,
    root_nb_name: str,
    state: dict,
) -> str:
    """Return the note ID of '00 - Kanban Board' for this root notebook."""
    boards = state.setdefault("notebook_kanban_boards", {})
    cached = boards.get(root_nb_id)
    if cached:
        try:
            client.get_note(cached)
            return cached
        except Exception:
            log.warning(f"Cached kanban {cached} for '{root_nb_name}' gone. Searching...")

    for c in client.search_notes_by_title(KANBAN_TITLE):
        if c.get("parent_id") == root_nb_id and c.get("title") == KANBAN_TITLE:
            boards[root_nb_id] = c["id"]
            log.info(f"Found existing '{KANBAN_TITLE}' in '{root_nb_name}'.")
            return c["id"]

    note = client.create_note(KANBAN_TITLE, _empty_kanban(root_nb_name), root_nb_id)
    boards[root_nb_id] = note["id"]
    log.info(f"Created '{KANBAN_TITLE}' in '{root_nb_name}'.")
    return note["id"]


_KANBAN_SETTINGS_RE = re.compile(r'```kanban-settings.*?```', re.DOTALL)

# Emojis assigned to each person column on first encounter, cycling through
# this list so every owner gets a visually distinct colour indicator.
_OWNER_EMOJIS = ["🔵", "🟢", "🟡", "🟠", "🟣", "🔴", "⚪", "🟤"]


def extract_kanban_settings(body: str) -> str:
    """
    Extract the ```kanban-settings``` block verbatim from an existing kanban body.
    Returns the full block (including fences) so user-assigned column and card
    colours survive each rebuild.  Falls back to the default empty block if not found.
    """
    m = _KANBAN_SETTINGS_RE.search(body)
    if m:
        return m.group(0)
    return "```kanban-settings\n# Do not remove this block\n```"


def _urgency_prefix(due_str: str) -> str:
    """
    Return an emoji prefix based on how soon the due date is.
      ⚠️  overdue
      🔥  due within 3 days
      📅  due within 7 days
      (empty string) further out or unparseable
    """
    if not due_str:
        return ""
    today = date.today()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%B %d, %Y", "%b %d, %Y",
                "%b. %d, %Y", "%d %B %Y", "%d %b %Y"):
        try:
            due = datetime.strptime(due_str.strip(), fmt).date()
            delta = (due - today).days
            if delta < 0:
                return "⚠️ "
            if delta <= 3:
                return "🔥 "
            if delta <= 7:
                return "📅 "
            return ""
        except ValueError:
            continue
    return ""


def _normalize_date(due_str: str) -> Optional[str]:
    """
    Parse a due date string in any supported format and return it as a
    canonical YYYY-MM-DD string.  Returns None if the string is empty,
    unparseable, or clearly not a real date ("Not specified", etc.).

    Handles formats produced by the meeting recorder and by manual entry:
        5/3/2026   (M/D/YYYY  — meeting recorder default)
        2026-05-03 (ISO)
        May 3, 2026 / May 3 2026 / May. 3, 2026
    """
    if not due_str:
        return None
    for fmt in (
        "%Y-%m-%d",
        "%m/%d/%Y",
        "%B %d, %Y",
        "%B %d %Y",
        "%b %d, %Y",
        "%b. %d, %Y",
        "%d %B %Y",
        "%d %b %Y",
    ):
        try:
            return datetime.strptime(due_str.strip(), fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return None



def _column_label(owner: str, emoji_map: dict) -> str:
    """
    Return a column heading with a consistent person emoji for each unique owner.
    Special columns get fixed emojis; person columns cycle through _OWNER_EMOJIS.
    """
    if owner == "Unassigned":
        return f"📥 {owner}"
    if owner.lower() == KANBAN_DONE_COLUMN.lower():
        return f"✅ {owner}"
    if owner not in emoji_map:
        emoji_map[owner] = _OWNER_EMOJIS[len(emoji_map) % len(_OWNER_EMOJIS)]
    return f"{emoji_map[owner]} {owner}"



def _empty_kanban(root_nb_name: str) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        f"**{root_nb_name} — Kanban Board**\n"
        f"*Last synced: {now_str}*\n\n"
        "*No active tasks.*\n\n"
        "```kanban-settings\n"
        "# Do not remove this block\n"
        "```\n"
    )


def build_kanban_body(
    root_nb_name: str,
    source_groups: dict,
    now_str: str,
    existing_settings: str = None,
) -> str:
    """
    Build a YesYouKan-compatible Kanban board body from active task data.

    Visual improvements:
      - Column headers include a consistent emoji per owner (🔵 Jason, 🟢 Harish…)
        with fixed emojis for special columns (📥 Unassigned, ✅ Done).
      - Card titles are prefixed with an urgency indicator:
          ⚠️  overdue  |  🔥 due ≤3 days  |  📅 due ≤7 days
      - existing_settings: the raw ```kanban-settings``` block extracted from the
        previous kanban body so user-assigned colours survive the rebuild.
    """
    owner_cards: dict[str, list] = {}

    for src_nid, group in source_groups.items():
        for task in group["tasks"]:
            owners = extract_owners(task["text"])
            due    = extract_due(task["text"])
            card   = {
                "title":        _card_title(task["text"]),
                "due":          due,
                "urgency":      _urgency_prefix(due),
                "source_title": group["title"],
                "source_nid":   src_nid,
                "sub_path":     group.get("sub_path", ""),
                "tid":          task["tid"],
            }
            for owner in owners:
                owner_cards.setdefault(owner, []).append(card)

    if not owner_cards:
        return _empty_kanban(root_nb_name)

    sorted_owners = sorted(
        owner_cards.keys(),
        key=lambda x: ("\xff" + x) if x == "Unassigned" else x.lower(),
    )

    # Stable emoji assignment: build map in sorted order so same owner always
    # gets the same emoji across rebuilds.
    emoji_map: dict[str, str] = {}

    lines = [
        f"**{root_nb_name} — Kanban Board**",
        f"*Last synced: {now_str}*",
        "",
    ]

    for owner in sorted_owners:
        col_label = _column_label(owner, emoji_map)
        lines.append(f"# {col_label}")
        cards = sorted(owner_cards[owner], key=lambda c: c["source_title"], reverse=True)
        for card in cards:
            sub_part   = f" — {card['sub_path']}" if card["sub_path"] else ""
            card_title = f"{card['urgency']}{card['title']}"
            lines.append(f"## {card_title}")
            if card["due"]:
                lines.append(f"**Due:** {card['due']}")
            lines.append(
                f"**Source:** [{card['source_title']}](:/{card['source_nid']}){sub_part}"
            )
            lines.append(f"<!-- tid:{card['tid']} -->")
            lines.append("")

    # Done column — always present as a drag target for completion
    lines += ["", f"# ✅ {KANBAN_DONE_COLUMN}", ""]

    # Preserve user-assigned colours from the previous build
    settings = existing_settings or "```kanban-settings\n# Do not remove this block\n```"
    lines += [settings, ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Weekly board helpers  (00 - Weekly Board, one global note)
# ---------------------------------------------------------------------------

def _date_column_name(d: date) -> str:
    """Format a date as a YesYouKan column name, e.g. '2026-05-07 Wed'."""
    return d.strftime("%Y-%m-%d %a")


def _parse_column_date(col_name: str) -> Optional[str]:
    """
    Extract the YYYY-MM-DD portion from a weekly board column name.
    Returns None for non-date columns (No Due Date, Done, etc.).
    """
    m = re.match(r'(\d{4}-\d{2}-\d{2})', col_name.strip())
    return m.group(1) if m else None


def _weekly_card_lines(card: dict) -> list[str]:
    """Render card lines for the weekly board."""
    sub_part = f" — {card['sub_path']}" if card["sub_path"] else ""
    lines    = [f"## {card['urgency']}{card['title']}"]
    if card["due"]:
        lines.append(f"**Due:** {card['due']}")
    if card["owner"]:
        lines.append(f"**Owner:** {card['owner']}")
    lines.append(
        f"**Source:** [{card['source_title']}](:/{card['source_nid']}){sub_part}"
    )
    lines.append(f"<!-- tid:{card['tid']} -->")
    lines.append("")
    return lines


def _empty_weekly_board(root_nb_name: str) -> str:
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    return (
        f"**{root_nb_name} — Weekly Board**\n"
        f"*Last synced: {now_str}*\n\n"
        "*No active tasks.*\n\n"
        "```kanban-settings\n"
        "# Do not remove this block\n"
        "```\n"
    )


def get_or_create_weekly_board(
    client: JoplinClient, root_nb_id: str, root_nb_name: str, state: dict
) -> str:
    """
    Find or create the '00 - Weekly Board' note inside root_nb_id.
    Mirrors get_or_create_kanban_note — one board per root notebook.
    """
    boards = state.setdefault("notebook_weekly_boards", {})

    cached = boards.get(root_nb_id)
    if cached:
        try:
            client.get_note(cached)
            return cached
        except Exception:
            log.warning(f"Cached weekly board {cached} gone. Searching...")

    for c in client.search_notes_by_title(WEEKLY_BOARD_TITLE):
        if c.get("parent_id") == root_nb_id and c.get("title") == WEEKLY_BOARD_TITLE:
            boards[root_nb_id] = c["id"]
            log.info(f"Found existing '{WEEKLY_BOARD_TITLE}' in '{root_nb_name}'.")
            return c["id"]

    note = client.create_note(WEEKLY_BOARD_TITLE, _empty_weekly_board(root_nb_name), root_nb_id)
    boards[root_nb_id] = note["id"]
    log.info(f"Created '{WEEKLY_BOARD_TITLE}' in '{root_nb_name}'.")
    return note["id"]


def build_weekly_board_body(
    root_nb_name: str,
    source_groups: dict,
    now_str: str,
    existing_settings: str = None,
) -> str:
    """
    Build a per-notebook weekly board body from active task data.

    Column layout (left → right):
      📥 No Due Date   — undated tasks, overdue tasks, and tasks > 14 days out
      YYYY-MM-DD ddd   — one column per day for the rolling 14-day window
      ✅ Done          — drag target for completion

    Mirrors build_kanban_body but organises by due date instead of owner.
    Within each column, cards are sorted by source note title.
    """
    today      = date.today()
    window     = [today + timedelta(days=i) for i in range(14)]
    window_set = {d.strftime("%Y-%m-%d") for d in window}

    dated:   dict[str, list] = {_date_column_name(d): [] for d in window}
    overdue: list             = []
    no_date: list             = []

    for src_nid, group in source_groups.items():
        for task in group["tasks"]:
            due    = extract_due(task["text"])
            due_n  = _normalize_date(due)
            owners = extract_owners(task["text"])
            owner  = owners[0] if owners and owners[0] != "Unassigned" else ""

            card = {
                "tid":          task["tid"],
                "title":        _card_title(task["text"]),
                "due":          due,
                "urgency":      _urgency_prefix(due),
                "owner":        owner,
                "source_title": group["title"],
                "source_nid":   src_nid,
                "sub_path":     group.get("sub_path", ""),
            }

            if due_n:
                due_date = date.fromisoformat(due_n)
                if due_date < today:
                    overdue.append(card)
                elif due_n in window_set:
                    dated[_date_column_name(due_date)].append(card)
                else:
                    no_date.append(card)   # far future (>14 days)
            else:
                no_date.append(card)

    if not any(dated.values()) and not overdue and not no_date:
        return _empty_weekly_board(root_nb_name)

    lines = [
        f"**{root_nb_name} — Weekly Board**",
        f"*Last synced: {now_str}*",
        "",
    ]

    # Overdue column — past-due tasks, always leftmost so they're impossible to miss
    lines.append("# ⚠️ Overdue")
    for card in sorted(overdue, key=lambda c: (c["due"], c["source_title"])):
        lines += _weekly_card_lines(card)
    lines.append("")

    # No Due Date holding area — undated and far-future tasks
    lines.append("# 📥 No Due Date")
    for card in sorted(no_date, key=lambda c: c["source_title"]):
        lines += _weekly_card_lines(card)
    lines.append("")

    # 14-day date columns
    for d in window:
        col = _date_column_name(d)
        lines.append(f"# {col}")
        for card in sorted(dated.get(col, []), key=lambda c: c["source_title"]):
            lines += _weekly_card_lines(card)
        lines.append("")

    lines += [f"# ✅ {KANBAN_DONE_COLUMN}", ""]

    settings = existing_settings or "```kanban-settings\n# Do not remove this block\n```"
    lines += [settings, ""]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Core sync
# ---------------------------------------------------------------------------

def run_sync(client: JoplinClient, state: dict, target_note_id: Optional[str] = None) -> None:
    now_iso = datetime.now(timezone.utc).isoformat()
    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

    # Read exclusion list from config file (may have been updated by GUI)
    config = load_config()
    excluded_root_nbs = set(config.get("excluded_notebooks", list(EXCLUDED_ROOT_NOTEBOOKS)))

    nb_map = build_notebook_map(client)
    excluded_root_ids = {
        nid for nid, nb in nb_map.items()
        if not nb.get("parent_id") and nb["title"] in excluded_root_nbs
    }

    body_cache: dict[str, str] = {}
    dirty:      set[str]       = set()

    def read_body(nid: str) -> str:
        if nid not in body_cache:
            body_cache[nid] = client.get_note(nid)["body"]
        return body_cache[nid]

    def write_body(nid: str, body: str) -> None:
        body_cache[nid] = body
        dirty.add(nid)

    existing_tids: set = set(state["tasks"].keys())

    # -----------------------------------------------------------------------
    # Phase 1: Sweep updated source notes — inject tids, register new tasks
    # -----------------------------------------------------------------------
    if target_note_id:
        raw_notes = [client.get_note(target_note_id)]
        log.info(f"Targeted sync for note {target_note_id}.")
    else:
        since_ms = None
        if state["last_run"]:
            since_ms = int(datetime.fromisoformat(state["last_run"]).timestamp() * 1000)
        raw_notes = client.get_all_notes(since_ms=since_ms)
        log.info(f"Sweep: {len(raw_notes)} notes to process.")

    for note in raw_notes:
        nid        = note["id"]
        title      = note.get("title", "Untitled")
        nb_id      = note.get("parent_id", "")

        if title in {TASK_LIST_TITLE, KANBAN_TITLE, WEEKLY_BOARD_TITLE} or title in EXCLUDED_NOTE_TITLES:
            continue

        root_nb = get_root_notebook(nb_id, nb_map)
        root_id = root_nb.get("id", "")
        if root_id in excluded_root_ids:
            continue

        body_cache[nid] = note.get("body", "")

        # Inject missing tids
        new_body, modified = inject_tids(body_cache[nid], existing_tids)
        if modified:
            write_body(nid, new_body)
            log.info(f"Injected task IDs into '{title}'.")

        tl_nb_id = root_id if TASK_LIST_AT_ROOT else nb_id
        sub_path = get_sub_path(nb_id, root_id, nb_map)

        for task in parse_tasks(body_cache[nid]):
            tid = task["tid"]
            if tid is None:
                continue

            if tid not in state["tasks"]:
                state["tasks"][tid] = {
                    "source_note_id":    nid,
                    "source_note_title": title,
                    "source_notebook_id": nb_id,
                    "sub_notebook_path": sub_path,
                    "root_notebook_id":  tl_nb_id,
                    "text":              task["text"],
                    "completed":         task["checked"],
                }
                if task["checked"]:
                    log.info(f"  Skipping already-completed [{tid}]: {task['text'][:60]}")
                else:
                    log.info(f"  New task [{tid}]: {task['text'][:70]}")
            else:
                # Update text and completion from source (source is authoritative)
                rec = state["tasks"][tid]
                rec["source_notebook_id"] = nb_id   # keep current; handles re-parenting
                if task["checked"] and not rec.get("completed"):
                    log.info(f"  Task [{tid}] checked in source note. Marking completed.")
                    rec["completed"] = True
                if not task["checked"]:
                    rec["text"] = task["text"]  # propagate text edits from source

        # Detect deleted tasks: tids in state from this note no longer present
        note_tids = {t["tid"] for t in parse_tasks(body_cache[nid]) if t["tid"]}
        for tid, rec in state["tasks"].items():
            if rec.get("source_note_id") == nid and tid not in note_tids:
                if not rec.get("completed"):
                    log.info(f"  Task [{tid}] removed from source note. Marking completed.")
                    rec["completed"] = True

    # -----------------------------------------------------------------------
    # Phase 2: Scan existing task lists for CHECKED items.
    # Propagate completion back to source notes. Mark tasks completed in state.
    # -----------------------------------------------------------------------
    processed_tl_ids: set = set()
    for root_nb_id in set(r["root_notebook_id"] for r in state["tasks"].values()):
        tl_nid = state["notebook_task_lists"].get(root_nb_id)
        if not tl_nid or tl_nid in processed_tl_ids:
            continue
        processed_tl_ids.add(tl_nid)

        try:
            tl_body = read_body(tl_nid)
        except Exception:
            continue

        for tl_task in parse_tasks(tl_body):
            if not tl_task["checked"]:
                continue

            # Find matching state record by tid or text (WYSIWYG may have stripped tid)
            matched_tid = None
            if tl_task["tid"] and tl_task["tid"] in state["tasks"]:
                matched_tid = tl_task["tid"]
            else:
                norm = _norm(tl_task["text"])
                for tid, rec in state["tasks"].items():
                    if not rec.get("completed") and _norm(rec["text"]) == norm:
                        matched_tid = tid
                        log.info(f"  Text-matched checked task (tid stripped): '{tl_task['text'][:50]}'")
                        break

            if matched_tid is None:
                continue

            rec = state["tasks"][matched_tid]
            if rec.get("completed"):
                continue

            # Propagate [x] to source note
            src_nid = rec["source_note_id"]
            try:
                src_body = read_body(src_nid)
                new_src, changed = mark_checked_in_body(src_body, matched_tid, rec["text"])
                if changed:
                    write_body(src_nid, new_src)
                    log.info(f"  Propagated completion to source note for [{matched_tid}].")
            except Exception as e:
                log.warning(f"  Could not update source note for [{matched_tid}]: {e}")

            rec["completed"] = True
            log.info(f"Task [{matched_tid}] completed via task list check.")

    # -----------------------------------------------------------------------
    # Phase 2b: Scan kanban boards for drag-and-drop changes
    #
    # Detection strategy: compare where each card IS NOW (current H1 column)
    # against where it WAS in the last rebuild (rec["kanban_columns"]).
    # This is more reliable than re-parsing extract_owners(rec["text"]) because
    # it doesn't depend on text format consistency and handles new/renamed columns.
    #
    # Operations detected:
    #   a) Card moved to a different owner column → update (Owner: X) in source note
    #   b) Card moved to the Done column         → mark task complete
    #   c) Due date edited in card description    → update Due in source note
    # -----------------------------------------------------------------------
    kanban_synced_tids: set[str] = set()   # tids Phase 2b has already handled;
                                           # Phase 3 skips these to avoid overwrite

    for root_nb_id, kb_nid in state.get("notebook_kanban_boards", {}).items():
        try:
            kb_body = read_body(kb_nid)
        except Exception as e:
            log.warning(f"Cannot read kanban note {kb_nid}: {e}")
            continue

        columns = parse_kanban_columns(kb_body)
        if not columns:
            log.debug(f"No columns parsed from kanban {kb_nid} — skipping.")
            continue

        for col_name, cards in columns.items():
            for card in cards:
                tid = card.get("tid")
                if not tid or tid not in state["tasks"]:
                    continue

                rec = state["tasks"][tid]
                if rec.get("completed"):
                    continue

                # Where was this card in the last rebuild?
                last_cols: list = rec.get("kanban_columns", [])

                # --- Completion: card dragged to Done column ---
                if col_name.lower() == KANBAN_DONE_COLUMN.lower():
                    src_nid = rec["source_note_id"]
                    try:
                        src_body = read_body(src_nid)
                        new_src, changed = mark_checked_in_body(src_body, tid, rec["text"])
                        if changed:
                            write_body(src_nid, new_src)
                    except Exception as e:
                        log.warning(f"Could not mark source note for [{tid}]: {e}")
                    rec["completed"] = True
                    kanban_synced_tids.add(tid)
                    log.info(f"Task [{tid}] completed via kanban '{KANBAN_DONE_COLUMN}' column.")
                    continue

                # --- Owner change: card is NOT in a column it was assigned to ---
                # last_cols is empty on first run (no previous rebuild recorded it);
                # fall back to extract_owners so first-run drags still work.
                if last_cols:
                    column_changed = col_name not in last_cols
                else:
                    column_changed = col_name not in extract_owners(rec["text"])

                changed_text = rec["text"]
                text_dirty   = False

                if column_changed and col_name != "Unassigned":
                    changed_text = update_owner_in_text(changed_text, col_name)
                    text_dirty   = True
                    log.info(
                        f"Task [{tid}] owner changed to '{col_name}' via kanban "
                        f"(was in {last_cols or 'unknown'})."
                    )
                elif column_changed and col_name == "Unassigned":
                    changed_text = update_owner_in_text(changed_text, "Not specified")
                    text_dirty   = True
                    log.info(f"Task [{tid}] moved to Unassigned via kanban.")

                # --- Due date change: user edited **Due:** line in card ---
                card_due    = card.get("due", "")
                current_due = extract_due(rec["text"])
                if card_due and card_due.lower() not in {
                    "not specified", "not identified", "n/a",
                } and card_due != current_due:
                    changed_text = update_due_in_text(changed_text, card_due)
                    text_dirty   = True
                    log.info(f"Task [{tid}] due date updated to '{card_due}' via kanban.")

                if text_dirty:
                    src_nid = rec["source_note_id"]
                    try:
                        src_body = read_body(src_nid)
                        new_src, src_changed = update_task_line_in_body(
                            src_body, tid, changed_text, False
                        )
                        if src_changed:
                            write_body(src_nid, new_src)
                            log.info(f"  Source note updated for [{tid}].")
                        else:
                            log.warning(
                                f"  Task [{tid}] not found in source note {src_nid} "
                                f"— cannot propagate kanban change."
                            )
                    except Exception as e:
                        log.warning(f"Could not update source note for [{tid}]: {e}")
                    # Update state regardless so Phase 3 doesn't revert it
                    rec["text"] = changed_text
                    kanban_synced_tids.add(tid)

    # -----------------------------------------------------------------------
    # Phase 2c: Scan weekly boards for drag-and-drop date changes
    # -----------------------------------------------------------------------
    for wb_nid in state.get("notebook_weekly_boards", {}).values():
        try:
            wb_body    = read_body(wb_nid)
            wb_columns = parse_kanban_columns(wb_body)

            for col_name, cards in wb_columns.items():
                for card in cards:
                    tid = card.get("tid")
                    if not tid or tid not in state["tasks"]:
                        continue

                    rec = state["tasks"][tid]
                    if rec.get("completed"):
                        continue

                    # --- Completion ---
                    if col_name.lower() == KANBAN_DONE_COLUMN.lower():
                        src_nid = rec["source_note_id"]
                        try:
                            src_body = read_body(src_nid)
                            new_src, changed = mark_checked_in_body(src_body, tid, rec["text"])
                            if changed:
                                write_body(src_nid, new_src)
                        except Exception as e:
                            log.warning(f"Could not mark source for [{tid}]: {e}")
                        rec["completed"] = True
                        kanban_synced_tids.add(tid)
                        log.info(f"Task [{tid}] completed via weekly board.")
                        continue

                    last_cols    = rec.get("calendar_columns", [])
                    col_date     = _parse_column_date(col_name)  # YYYY-MM-DD or None
                    changed_text = rec["text"]
                    text_dirty   = False

                    if col_name == "No Due Date":
                        # Card moved to holding area — clear due date if it had one
                        if last_cols and "No Due Date" not in last_cols:
                            if extract_due(rec["text"]):
                                changed_text = update_due_in_text(changed_text, "Not specified")
                                text_dirty   = True
                                log.info(f"Task [{tid}] due date cleared via weekly board.")
                    elif col_date and (not last_cols or col_date not in last_cols):
                        # Card moved to a new date column
                        changed_text = update_due_in_text(changed_text, col_date)
                        text_dirty   = True
                        log.info(f"Task [{tid}] due date → '{col_date}' via weekly board.")

                    if text_dirty:
                        src_nid = rec["source_note_id"]
                        try:
                            src_body = read_body(src_nid)
                            new_src, src_changed = update_task_line_in_body(
                                src_body, tid, changed_text, False
                            )
                            if src_changed:
                                write_body(src_nid, new_src)
                                log.info(f"  Source updated for [{tid}] (weekly board).")
                            else:
                                log.warning(
                                    f"  Task [{tid}] not found in source note — "
                                    f"cannot propagate weekly board date change."
                                )
                        except Exception as e:
                            log.warning(f"Could not update source for [{tid}]: {e}")
                        rec["text"] = changed_text
                        kanban_synced_tids.add(tid)

        except Exception as e:
            log.warning(f"Cannot scan weekly board {wb_nid}: {e}")
    # -----------------------------------------------------------------------
    # Phase 3: Re-verify and reconcile all active tasks against source notes.
    #
    # For normal tasks (not kanban-synced): source note is authoritative.
    # For kanban-synced tasks: state is authoritative (Phase 2b made a change);
    #   if the source note text doesn't match state, write it now. This serves
    #   as a retry if Phase 2b's write_body call didn't fully propagate.
    # -----------------------------------------------------------------------
    for tid, rec in state["tasks"].items():
        if rec.get("completed"):
            continue
        src_nid = rec["source_note_id"]
        try:
            src_body = read_body(src_nid)
        except Exception:
            log.warning(f"Source note {src_nid} for [{tid}] unreachable. Marking completed.")
            rec["completed"] = True
            continue

        task_map = {t["tid"]: t for t in parse_tasks(src_body) if t["tid"]}

        if tid not in task_map:
            log.info(f"Task [{tid}] no longer in source note. Marking completed.")
            rec["completed"] = True
        elif task_map[tid]["checked"]:
            log.info(f"Task [{tid}] is checked in source note. Marking completed.")
            rec["completed"] = True
        elif tid in kanban_synced_tids:
            # State is authoritative here (Phase 2b changed it).
            # If source note doesn't have the updated text yet, write it now.
            if task_map[tid]["text"] != rec["text"]:
                new_src, src_changed = update_task_line_in_body(
                    src_body, tid, rec["text"], False
                )
                if src_changed:
                    write_body(src_nid, new_src)
                    log.info(
                        f"Phase 3 push: source note updated for [{tid}] "
                        f"(kanban change not yet reflected in note)."
                    )
        else:
            # Source note is authoritative for non-kanban changes.
            rec["text"] = task_map[tid]["text"]

    # -----------------------------------------------------------------------
    # Phase 4: Rebuild task lists from scratch
    # -----------------------------------------------------------------------
    # Backfill source_notebook_id for any tasks registered before this field
    # existed, or tasks in notes that haven't been swept recently.
    # A lightweight metadata-only fetch (no body) per affected task.
    for tid, rec in state["tasks"].items():
        if rec.get("completed"):
            continue
        if not rec.get("source_notebook_id"):
            try:
                note_meta = client.get_note(rec["source_note_id"])
                nb_id = note_meta.get("parent_id", "")
                if nb_id:
                    rec["source_notebook_id"] = nb_id
                    log.info(
                        f"Backfilled source_notebook_id for [{tid}]: {nb_id}"
                    )
            except Exception as e:
                log.warning(f"Could not fetch note metadata for [{tid}]: {e}")
    # task so Phase 2b can detect drag-and-drop changes on the next run.
    active_by_root: dict[str, dict] = {}
    for tid, rec in state["tasks"].items():
        if rec.get("completed"):
            continue

        # Re-resolve root notebook live from nb_map on every run.
        # If a notebook was moved to root level (or re-parented), the cached
        # rec["root_notebook_id"] would still point to the old root. Walking
        # nb_map here ensures the task lands under the correct root notebook
        # and updates state so subsequent runs stay consistent.
        src_nid       = rec["source_note_id"]
        src_nb_id     = rec.get("source_notebook_id", "")
        resolved_root = get_root_notebook(src_nb_id, nb_map) if src_nb_id else None
        if resolved_root and resolved_root["id"] != rec.get("root_notebook_id"):
            log.info(
                f"Task [{tid}] root notebook changed: "
                f"'{rec.get('root_notebook_id')}' → '{resolved_root['id']}' "
                f"({resolved_root['title']}). Updating state."
            )
            rec["root_notebook_id"] = resolved_root["id"]

        root_nb_id = rec["root_notebook_id"]
        src_nid    = rec["source_note_id"]
        active_by_root.setdefault(root_nb_id, {})
        active_by_root[root_nb_id].setdefault(src_nid, {
            "title":    rec["source_note_title"],
            "sub_path": rec.get("sub_notebook_path", ""),
            "tasks":    [],
        })
        active_by_root[root_nb_id][src_nid]["tasks"].append({"tid": tid, "text": rec["text"]})

        # Record which owner column(s) this task will appear in after rebuild.
        # Phase 2b uses this on the NEXT run to detect column changes.
        rec["kanban_columns"] = extract_owners(rec["text"])

    # Write (or clear) task list AND kanban board for every known root notebook.
    # all_root_nb_ids includes notebooks from the live API so that newly promoted
    # root notebooks (moved out from under another notebook) automatically get
    # their three management notes created on the first sync after the move.
    all_root_nb_ids = (
        set(nb_map[nid]["id"] for nid in nb_map
            if not nb_map[nid].get("parent_id")
            and nb_map[nid]["title"] not in excluded_root_nbs)
        | set(active_by_root.keys())
        | set(state["notebook_task_lists"].keys())
        | set(state.get("notebook_kanban_boards", {}).keys())
        | set(state.get("notebook_weekly_boards", {}).keys())
    )

    for root_nb_id in all_root_nb_ids:
        root_nb_name  = nb_map.get(root_nb_id, {}).get("title", "Unknown")
        tl_nid        = get_or_create_task_list(client, root_nb_id, root_nb_name, state)
        kb_nid        = get_or_create_kanban_note(client, root_nb_id, root_nb_name, state)
        source_groups = active_by_root.get(root_nb_id, {})

        if source_groups:
            task_count = sum(len(g["tasks"]) for g in source_groups.values())

            tl_body = build_task_list_body(root_nb_name, source_groups, now_str)
            log.info(f"Rebuilding task list for '{root_nb_name}': {task_count} active task(s).")

            # Preserve any colours the user assigned via the YesYouKan UI
            # by extracting the existing kanban-settings block before overwriting.
            try:
                existing_kb_body = read_body(kb_nid)
                kb_settings = extract_kanban_settings(existing_kb_body)
            except Exception:
                kb_settings = None

            kb_body = build_kanban_body(root_nb_name, source_groups, now_str, kb_settings)
            log.info(f"Rebuilding kanban board for '{root_nb_name}'.")

            wb_nid = get_or_create_weekly_board(client, root_nb_id, root_nb_name, state)
            try:
                existing_wb_body = read_body(wb_nid)
                wb_settings = extract_kanban_settings(existing_wb_body)
            except Exception:
                wb_settings = None
            wb_body = build_weekly_board_body(root_nb_name, source_groups, now_str, wb_settings)
            log.info(f"Rebuilding weekly board for '{root_nb_name}'.")
        else:
            tl_body = (
                f"# {root_nb_name} — Task List\n"
                f"*Last synced: {now_str}*\n\n"
                "---\n\n"
                "*No active tasks.*\n"
            )
            kb_body = _empty_kanban(root_nb_name)
            wb_nid  = get_or_create_weekly_board(client, root_nb_id, root_nb_name, state)
            wb_body = _empty_weekly_board(root_nb_name)
            log.info(f"Task list, kanban and weekly board for '{root_nb_name}' are now empty.")

        write_body(tl_nid, tl_body)
        write_body(kb_nid, kb_body)
        write_body(wb_nid, wb_body)

    # -----------------------------------------------------------------------
    # Phase 4c: Update calendar_columns in state for all active tasks
    # (Phase 2c uses this on the next run to detect weekly board drag-and-drop)
    # -----------------------------------------------------------------------
    window_set = {
        (date.today() + timedelta(days=i)).strftime("%Y-%m-%d") for i in range(14)
    }
    today = date.today()
    for tid, rec in state["tasks"].items():
        if rec.get("completed"):
            continue
        due   = extract_due(rec["text"])
        due_n = _normalize_date(due)
        if due_n:
            due_date = date.fromisoformat(due_n)
            if due_date < today:
                rec["calendar_columns"] = ["Overdue"]
            elif due_n in window_set:
                rec["calendar_columns"] = [due_n]
            else:
                rec["calendar_columns"] = ["No Due Date"]
        else:
            rec["calendar_columns"] = ["No Due Date"]

    # -----------------------------------------------------------------------
    # Phase 5: Flush all dirty notes to Joplin
    # -----------------------------------------------------------------------
    if dirty:
        log.info(f"Writing {len(dirty)} modified note(s) to Joplin.")
    for nid in dirty:
        try:
            client.update_note(nid, body_cache[nid])
        except Exception as e:
            log.error(f"Failed to write note {nid}: {e}")

    state["last_run"] = now_iso
    log.info("Sync complete.")

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description="Joplin Task Sync")
    p.add_argument("--note-id", metavar="ID",    default=None)
    p.add_argument("--token",   metavar="TOKEN", default=None)
    p.add_argument("--reset",   action="store_true")
    p.add_argument("--dry-run", action="store_true")
    args = p.parse_args()

    setup_logging()

    token = args.token or JOPLIN_TOKEN
    if not token:
        log.error("No JOPLIN_TOKEN found. Export it in your environment.")
        sys.exit(1)

    client = JoplinClient(JOPLIN_BASE_URL, token)
    if not client.ping():
        log.error(f"Cannot reach Joplin at {JOPLIN_BASE_URL}. "
                  "Ensure Joplin is running with Web Clipper enabled.")
        sys.exit(1)

    state = load_state()

    if args.reset:
        state["last_run"] = None
        log.info("last_run cleared. Next sweep will reprocess all notes.")

    if args.dry_run:
        log.info("DRY RUN: no writes.")
        client.update_note = lambda *a, **kw: None
        client.create_note = lambda *a, **kw: {"id": "dryrun"}

    try:
        run_sync(client, state, target_note_id=args.note_id)
    except KeyboardInterrupt:
        log.info("Interrupted.")
    except Exception as e:
        log.exception(f"Unhandled error: {e}")
    finally:
        if not args.dry_run:
            save_state(state)
        log.info("State saved.")

if __name__ == "__main__":
    main()
