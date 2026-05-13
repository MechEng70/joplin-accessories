#!/usr/bin/env python3
"""
gmail_joplin_sync.py
Syncs Gmail threads to Joplin notes with AI summarization via Claude API.

Location: ~/Applications/MeetingGui/gmail_joplin_sync.py

Dependencies:
    pip install google-auth-oauthlib google-auth-httplib2 google-api-python-client anthropic requests

Requires:
    - credentials.json from Google Cloud Console at ~/.config/gmail-to-joplin/credentials.json
    - Joplin Web Clipper running (Joplin > Settings > Web Clipper) on port 41184
    - ANTHROPIC_API_KEY environment variable set
"""

import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
from gi.repository import Gtk, Adw, GLib, Pango

import os
import json
import base64
import threading
import requests
import anthropic
from pathlib import Path
from datetime import datetime

# --- Constants ---
APP_ID = "com.medtechcto.GmailJoplinSync"
CONFIG_DIR = Path.home() / ".config" / "gmail-to-joplin"
STATE_DIR = Path.home() / ".local" / "share" / "gmail-to-joplin"
CONFIG_FILE = CONFIG_DIR / "config.json"
STATE_FILE = STATE_DIR / "state.json"
TOKEN_FILE = CONFIG_DIR / "token.json"
CREDENTIALS_FILE = CONFIG_DIR / "credentials.json"
GMAIL_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
JOPLIN_PORT = 41184
CLAUDE_MODEL = "claude-sonnet-4-20250514"
CLAUDE_MAX_CHARS = 12_000  # ~3k tokens; truncate thread text beyond this

# Gmail system labels to exclude from the user-facing list
GMAIL_SYSTEM_LABELS = {
    "INBOX", "SENT", "DRAFTS", "SPAM", "TRASH", "UNREAD", "STARRED",
    "IMPORTANT", "CATEGORY_PERSONAL", "CATEGORY_SOCIAL", "CATEGORY_PROMOTIONS",
    "CATEGORY_UPDATES", "CATEGORY_FORUMS",
}


# ---------------------------------------------------------------------------
# Config / State helpers
# ---------------------------------------------------------------------------

def ensure_dirs():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    ensure_dirs()
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE) as f:
            return json.load(f)
    return {
        "joplin_token": "",
        "label_mappings": {},
        "always_include": [],
        "skip_keywords": ["invitation", "unsubscribe", "out of office", "no-reply"],
    }


def save_config(config: dict):
    ensure_dirs()
    with open(CONFIG_FILE, "w") as f:
        json.dump(config, f, indent=2)


def load_state() -> dict:
    ensure_dirs()
    if STATE_FILE.exists():
        with open(STATE_FILE) as f:
            return json.load(f)
    return {"processed_threads": {}, "last_run": None}


def save_state(state: dict):
    ensure_dirs()
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ---------------------------------------------------------------------------
# Gmail helpers
# ---------------------------------------------------------------------------

def get_gmail_service():
    """Return an authenticated Gmail API service, refreshing/creating token as needed."""
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow
    from google.auth.transport.requests import Request
    from googleapiclient.discovery import build

    creds = None
    if TOKEN_FILE.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_FILE), GMAIL_SCOPES)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            if not CREDENTIALS_FILE.exists():
                raise FileNotFoundError(
                    f"Google credentials not found at {CREDENTIALS_FILE}.\n"
                    "Download credentials.json from Google Cloud Console and place it there."
                )
            flow = InstalledAppFlow.from_client_secrets_file(str(CREDENTIALS_FILE), GMAIL_SCOPES)
            creds = flow.run_local_server(port=0)
        with open(TOKEN_FILE, "w") as f:
            f.write(creds.to_json())

    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=creds)


def get_user_labels(service) -> list:
    """Return user-created Gmail labels, filtered of all system labels."""
    result = service.users().labels().list(userId="me").execute()
    return [
        lbl for lbl in result.get("labels", [])
        if lbl["name"] not in GMAIL_SYSTEM_LABELS
        and not lbl["name"].startswith("CATEGORY_")
    ]


def get_threads_for_label(service, label_id: str) -> list:
    """Page through all threads for a given label ID."""
    threads, page_token = [], None
    while True:
        resp = service.users().threads().list(
            userId="me", labelIds=[label_id], pageToken=page_token
        ).execute()
        threads.extend(resp.get("threads", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return threads


def get_thread_detail(service, thread_id: str) -> dict:
    return service.users().threads().get(
        userId="me", id=thread_id, format="full"
    ).execute()


def extract_headers(message: dict) -> dict:
    return {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}


def extract_body_text(message: dict) -> str:
    """Extract plain-text body from a Gmail message, handling multipart."""
    payload = message.get("payload", {})
    parts = payload.get("parts", [])
    body = ""

    if not parts:
        data = payload.get("body", {}).get("data", "")
        if data:
            body = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    else:
        for part in parts:
            if part.get("mimeType") == "text/plain":
                data = part.get("body", {}).get("data", "")
                if data:
                    body += base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            # Handle nested multipart
            elif part.get("mimeType", "").startswith("multipart/"):
                for subpart in part.get("parts", []):
                    if subpart.get("mimeType") == "text/plain":
                        data = subpart.get("body", {}).get("data", "")
                        if data:
                            body += base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    return body.strip()


def extract_attachments(message: dict) -> list:
    """
    Return a list of attachment metadata dicts from a Gmail message.
    Each dict: {filename, mime_type, size_bytes, message_id}
    Does NOT download attachment data — metadata only.
    """
    attachments = []
    message_id = message.get("id", "")
    payload = message.get("payload", {})

    def _scan_parts(parts):
        for part in parts:
            filename = part.get("filename", "")
            mime = part.get("mimeType", "")
            # A part is an attachment if it has a filename, or if it has an
            # attachment-style disposition and isn't plain text or HTML.
            is_attachment = bool(filename) or (
                mime not in ("text/plain", "text/html")
                and not mime.startswith("multipart/")
                and part.get("body", {}).get("attachmentId")
            )
            if is_attachment and mime not in ("text/plain", "text/html"):
                size = part.get("body", {}).get("size", 0)
                attachments.append({
                    "filename": filename or f"attachment.{mime.split('/')[-1]}",
                    "mime_type": mime,
                    "size_bytes": size,
                    "message_id": message_id,
                })
            # Recurse into nested multipart
            if part.get("parts"):
                _scan_parts(part["parts"])

    _scan_parts(payload.get("parts", []))
    return attachments


def _format_size(size_bytes: int) -> str:
    """Human-readable file size."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / 1024 ** 2:.1f} MB"


def gmail_thread_url(thread_id: str) -> str:
    return f"https://mail.google.com/mail/u/0/#all/{thread_id}"


def format_messages_for_claude(messages: list, max_chars: int = CLAUDE_MAX_CHARS) -> tuple[str, bool, list]:
    """
    Render a list of Gmail message dicts into a readable thread transcript.
    Returns (text, truncated, all_attachments).
    - truncated: True if older messages were dropped to fit the context limit.
    - all_attachments: flat list of attachment metadata dicts across all messages.
    Truncation keeps the most recent messages (tail), discarding older ones first.
    """
    segments = []
    all_attachments = []

    for msg in messages:
        h = extract_headers(msg)
        header = f"--- From: {h.get('From', 'Unknown')} | Date: {h.get('Date', '')} ---"
        body = extract_body_text(msg)

        attachments = extract_attachments(msg)
        all_attachments.extend(attachments)

        seg_lines = [header, body if body else "(no plain-text body)"]
        if attachments:
            seg_lines.append(
                "[Attachments: "
                + ", ".join(
                    f"{a['filename']} ({_format_size(a['size_bytes'])})" for a in attachments
                )
                + "]"
            )
        segments.append("\n".join(seg_lines) + "\n")

    full_text = "\n".join(segments)
    if len(full_text) <= max_chars:
        return full_text, False, all_attachments

    # Keep as many tail segments as fit, prepend a truncation notice
    kept = []
    budget = max_chars - 120
    for seg in reversed(segments):
        if len(seg) <= budget:
            kept.insert(0, seg)
            budget -= len(seg)
        else:
            break
    notice = (
        f"[NOTE: Thread truncated to most recent {len(kept)} of {len(segments)} "
        "messages to fit context limit.]\n\n"
    )
    return notice + "\n".join(kept), True, all_attachments


# ---------------------------------------------------------------------------
# Joplin REST API helpers
# ---------------------------------------------------------------------------

def _joplin_url(endpoint: str) -> str:
    return f"http://localhost:{JOPLIN_PORT}{endpoint}"


def joplin_get(endpoint: str, token: str) -> dict:
    resp = requests.get(_joplin_url(endpoint), params={"token": token}, timeout=5)
    resp.raise_for_status()
    return resp.json()


def joplin_post(endpoint: str, token: str, data: dict) -> dict:
    resp = requests.post(_joplin_url(endpoint), params={"token": token}, json=data, timeout=5)
    resp.raise_for_status()
    return resp.json()


def joplin_put(endpoint: str, token: str, data: dict) -> dict:
    resp = requests.put(_joplin_url(endpoint), params={"token": token}, json=data, timeout=5)
    resp.raise_for_status()
    return resp.json()


def joplin_ping(token: str) -> bool:
    """Return True if Joplin Web Clipper is reachable."""
    try:
        joplin_get("/notes?limit=1", token)
        return True
    except Exception:
        return False


def get_joplin_notebooks(token: str) -> list:
    notebooks, page = [], 1
    while True:
        result = joplin_get(f"/folders?page={page}", token)
        notebooks.extend(result.get("items", []))
        if not result.get("has_more"):
            break
        page += 1
    return notebooks


def get_joplin_tags(token: str) -> list:
    tags, page = [], 1
    while True:
        result = joplin_get(f"/tags?page={page}", token)
        tags.extend(result.get("items", []))
        if not result.get("has_more"):
            break
        page += 1
    return tags


def ensure_joplin_tag(token: str, tag_name: str, existing_tags: list) -> str:
    """Return the ID of an existing tag or create a new one."""
    match = next((t for t in existing_tags if t["title"] == tag_name), None)
    if match:
        return match["id"]
    new_tag = joplin_post("/tags", token, {"title": tag_name})
    return new_tag["id"]


def create_joplin_note(token: str, title: str, body: str, notebook_id: str, tags: list = None) -> dict:
    note = joplin_post("/notes", token, {"title": title, "body": body, "parent_id": notebook_id})
    if tags and note.get("id"):
        existing_tags = get_joplin_tags(token)
        for tag_name in tags:
            tag_id = ensure_joplin_tag(token, tag_name, existing_tags)
            joplin_post(f"/tags/{tag_id}/notes", token, {"id": note["id"]})
    return note


def update_joplin_note_body(token: str, note_id: str, new_body: str):
    joplin_put(f"/notes/{note_id}", token, {"body": new_body})


def get_joplin_note_body(token: str, note_id: str) -> str:
    note = joplin_get(f"/notes/{note_id}?fields=id,title,body", token)
    return note.get("body", "")


# ---------------------------------------------------------------------------
# Claude summarization
# ---------------------------------------------------------------------------

def summarize_thread(thread_text: str, subject: str, label_name: str) -> dict:
    """
    Send thread content to Claude and return a structured analysis dict.
    Returns keys: summary, asks_to_jason, jasons_responses, next_steps,
                  decisions_made, key_contacts.
    """
    client = anthropic.Anthropic()
    prompt = f"""You are analyzing an email thread for Jason Glithero, a fractional CTO and \
medical device R&D consultant (Fractional MedTech LLC). Jason's role is the consultant/advisor; \
other parties are clients, vendors, or collaborators.

Thread subject: {subject}
Gmail label / context: {label_name}

Full thread:
---
{thread_text}
---

Return ONLY a JSON object with exactly these keys (no preamble, no markdown fences):
{{
  "summary": "2-3 sentence overview of the thread and its current status",
  "asks_to_jason": ["action items or requests explicitly directed at Jason"],
  "jasons_responses": ["how Jason responded to asks, if captured in this thread"],
  "next_steps": ["unresolved items or pending follow-ups as of the last message"],
  "decisions_made": ["concrete decisions or agreements reached"],
  "key_contacts": ["other people in the thread with role if apparent, e.g. 'Linda Braddon (SecureBME)'"]
}}

If a section has no entries, use an empty list. Be concise and specific."""

    response = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=1000,
        messages=[{"role": "user", "content": prompt}],
    )
    raw = response.content[0].text.strip()
    # Strip accidental code fences
    if raw.startswith("```"):
        raw = raw.split("```")[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip()
    return json.loads(raw)


def build_note_body(analysis: dict, subject: str, date_str: str, label_name: str,
                    is_update: bool = False, existing_body: str = "",
                    attachments: list = None, thread_id: str = "") -> str:
    """Render a Claude analysis dict as a Joplin markdown note body."""
    attachments = attachments or []
    gmail_url = gmail_thread_url(thread_id) if thread_id else ""
    lines = []

    if not is_update:
        lines += [
            f"# {subject}",
            f"**Label:** {label_name}  ",
            f"**Created:** {date_str}  ",
        ]
        contacts = analysis.get("key_contacts", [])
        if contacts:
            lines.append(f"**Contacts:** {', '.join(contacts)}  ")
        if gmail_url:
            lines.append(f"**[View thread in Gmail →]({gmail_url})**  ")
        lines += ["", "## Summary", analysis.get("summary", ""), ""]
    else:
        header_lines = ["", "---", f"## Update — {date_str}", "", analysis.get("summary", ""), ""]
        if gmail_url:
            header_lines.insert(2, f"[View in Gmail →]({gmail_url})  ")
        lines += header_lines

    if analysis.get("asks_to_jason"):
        lines.append("## Asks / Action Items")
        for item in analysis["asks_to_jason"]:
            lines.append(f"- [ ] {item}")
        lines.append("")

    if analysis.get("jasons_responses"):
        lines.append("## Jason's Responses")
        for item in analysis["jasons_responses"]:
            lines.append(f"- {item}")
        lines.append("")

    if analysis.get("next_steps"):
        lines.append("## Next Steps")
        for item in analysis["next_steps"]:
            lines.append(f"- [ ] {item}")
        lines.append("")

    if analysis.get("decisions_made"):
        lines.append("## Decisions Made")
        for item in analysis["decisions_made"]:
            lines.append(f"- {item}")
        lines.append("")

    if attachments:
        lines.append("## Attachments")
        seen = set()
        for a in attachments:
            key = (a["filename"], a["size_bytes"])
            if key in seen:
                continue
            seen.add(key)
            size_str = _format_size(a["size_bytes"]) if a["size_bytes"] else "unknown size"
            mime_str = a["mime_type"] or "unknown type"
            link = f"[Open in Gmail]({gmail_thread_url(a['message_id'])})" if a.get("message_id") else ""
            lines.append(f"- **{a['filename']}** ({mime_str}, {size_str}) {link}")
        lines.append("")

    new_section = "\n".join(lines)
    return (existing_body + new_section) if is_update else new_section


# ---------------------------------------------------------------------------
# Processing pipeline
# ---------------------------------------------------------------------------

def process_labels(selected_labels: list, config: dict, log_fn, progress_fn=None):
    """
    Main pipeline. For each selected label:
      - Preflight: fetch thread ID lists to get a total count for the progress bar
      - Detect new or updated threads via state.json (thread_id + last_message_id)
      - Summarize with Claude
      - Create or append-update Joplin note
      - Persist state per thread

    progress_fn(fraction: float, status: str) — called with 0.0–1.0 and a status string.
    """
    def prog(fraction: float, status: str):
        if progress_fn:
            progress_fn(fraction, status)

    state = load_state()
    joplin_token = config.get("joplin_token", "")
    label_mappings = config.get("label_mappings", {})

    try:
        service = get_gmail_service()
    except Exception as e:
        log_fn(f"ERROR: Gmail service failed: {e}")
        prog(0.0, "Gmail connection failed.")
        return

    # ------------------------------------------------------------------
    # Phase 1: Preflight — fetch thread ID lists for all mapped labels
    # so we know the total count before touching any message content.
    # ------------------------------------------------------------------
    prog(0.0, "Preflight: counting threads…")
    label_threads: dict = {}   # label_name -> list of thread stubs
    total_threads = 0

    for label in selected_labels:
        label_name = label["name"]
        if not label_mappings.get(label_name):
            log_fn(f"SKIP '{label_name}': no Joplin notebook mapped.")
            continue
        prog(0.0, f"Fetching thread list for {label_name}…")
        try:
            threads = get_threads_for_label(service, label["id"])
            label_threads[label_name] = threads
            total_threads += len(threads)
            log_fn(f"  {label_name}: {len(threads)} thread(s) found.")
        except Exception as e:
            log_fn(f"  ERROR fetching threads for '{label_name}': {e}")

    if total_threads == 0:
        log_fn("No threads to process.")
        prog(1.0, "Nothing to process.")
        return

    log_fn(f"Preflight complete. {total_threads} total thread(s) across {len(label_threads)} label(s).")

    # ------------------------------------------------------------------
    # Phase 2: Process each thread — progress bar advances per thread
    # ------------------------------------------------------------------
    done = 0
    grand_new = grand_updated = grand_skipped = grand_errors = 0

    for label in selected_labels:
        label_name = label["name"]
        if label_name not in label_threads:
            continue

        notebook_id = label_mappings[label_name]
        threads = label_threads[label_name]
        label_new = label_updated = label_skipped = label_errors = 0

        log_fn(f"--- {label_name} ({len(threads)} threads) ---")

        for idx, t in enumerate(threads, start=1):
            thread_id = t["id"]
            thread_num_str = f"{label_name} · thread {idx}/{len(threads)}"

            # Fetch full thread
            prog(done / total_threads, f"Fetching: {thread_num_str}")
            try:
                thread_data = get_thread_detail(service, thread_id)
            except Exception as e:
                log_fn(f"  ERROR fetching thread {thread_id}: {e}")
                label_errors += 1
                done += 1
                continue

            messages = thread_data.get("messages", [])
            if not messages:
                done += 1
                continue

            last_msg_id = messages[-1]["id"]
            stored = state["processed_threads"].get(thread_id)

            if stored and stored.get("last_message_id") == last_msg_id:
                label_skipped += 1
                done += 1
                prog(done / total_threads, f"Skipped (no new activity): {thread_num_str}")
                continue

            is_update = bool(stored)
            subject = extract_headers(messages[0]).get("Subject", "(no subject)")
            short_subj = subject[:52] + "…" if len(subject) > 52 else subject

            # Keyword filter — checked on every run so keyword list changes take effect
            # immediately. Filtered threads are NOT written to state so they re-evaluate
            # on the next run (cheap: no API cost, no Claude call).
            skip_keywords = [k.lower().strip() for k in config.get("skip_keywords", []) if k.strip()]
            if skip_keywords and any(kw in subject.lower() for kw in skip_keywords):
                matched = next(kw for kw in skip_keywords if kw in subject.lower())
                log_fn(f"  FILTER '{short_subj}' (matched keyword: '{matched}')")
                label_skipped += 1
                done += 1
                continue

            if is_update:
                seen_ids = set(stored.get("all_message_ids", []))
                new_msgs = [m for m in messages if m["id"] not in seen_ids]
                thread_text, truncated, attachments = format_messages_for_claude(new_msgs)
                log_fn(f"  UPDATE '{short_subj}' ({len(new_msgs)} new msg(s))")
            else:
                thread_text, truncated, attachments = format_messages_for_claude(messages)
                log_fn(f"  NEW    '{short_subj}'")

            if truncated:
                log_fn(f"    WARNING: Truncated to {CLAUDE_MAX_CHARS} chars; oldest messages dropped.")

            # Claude summarization
            prog(done / total_threads, f"Analyzing ({idx}/{len(threads)}): {short_subj}")
            try:
                analysis = summarize_thread(thread_text, subject, label_name)
            except Exception as e:
                log_fn(f"    ERROR: Claude failed: {e}")
                label_errors += 1
                done += 1
                continue

            date_str = datetime.now().strftime("%Y-%m-%d")

            # Joplin write
            prog(done / total_threads, f"Writing to Joplin ({idx}/{len(threads)}): {short_subj}")
            try:
                if is_update:
                    # Attempt to fetch the existing note. If it was deleted from Joplin,
                    # catch the 404 and fall back to creating a fresh note.
                    try:
                        existing_body = get_joplin_note_body(joplin_token, stored["joplin_note_id"])
                        new_body = build_note_body(
                            analysis, subject, date_str, label_name,
                            is_update=True, existing_body=existing_body,
                            attachments=attachments, thread_id=thread_id,
                        )
                        update_joplin_note_body(joplin_token, stored["joplin_note_id"], new_body)
                        joplin_note_id = stored["joplin_note_id"]
                        log_fn(f"    Joplin note updated.")
                        label_updated += 1
                    except requests.HTTPError as e:
                        if e.response is not None and e.response.status_code == 404:
                            log_fn(f"    Note was deleted from Joplin — recreating.")
                            body = build_note_body(
                                analysis, subject, date_str, label_name,
                                attachments=attachments, thread_id=thread_id,
                            )
                            note = create_joplin_note(
                                joplin_token, f"{subject} — {date_str}", body, notebook_id,
                                tags=[label_name],
                            )
                            joplin_note_id = note["id"]
                            log_fn(f"    Joplin note recreated.")
                            label_new += 1
                        else:
                            raise
                else:
                    body = build_note_body(
                        analysis, subject, date_str, label_name,
                        attachments=attachments, thread_id=thread_id,
                    )
                    note = create_joplin_note(
                        joplin_token, f"{subject} — {date_str}", body, notebook_id,
                        tags=[label_name],
                    )
                    joplin_note_id = note["id"]
                    log_fn(f"    Joplin note created{' ('+str(len(attachments))+' attachment(s) noted)' if attachments else ''}.")
                    label_new += 1
            except Exception as e:
                log_fn(f"    ERROR: Joplin write failed: {e}")
                label_errors += 1
                done += 1
                continue

            state["processed_threads"][thread_id] = {
                "processed_at": datetime.now().isoformat(),
                "last_message_id": last_msg_id,
                "all_message_ids": [m["id"] for m in messages],
                "label": label_name,
                "joplin_note_id": joplin_note_id,
                "subject": subject,
            }
            save_state(state)
            done += 1

        # Per-label summary
        parts = []
        if label_new:       parts.append(f"{label_new} new")
        if label_updated:   parts.append(f"{label_updated} updated")
        if label_skipped:   parts.append(f"{label_skipped} skipped")
        if label_errors:    parts.append(f"{label_errors} error(s)")
        log_fn(f"  {label_name} done: {', '.join(parts) or 'nothing to do'}.")

        grand_new += label_new
        grand_updated += label_updated
        grand_skipped += label_skipped
        grand_errors += label_errors

    state["last_run"] = datetime.now().isoformat()
    save_state(state)

    summary = (
        f"=== Complete: {grand_new} new, {grand_updated} updated, "
        f"{grand_skipped} skipped, {grand_errors} error(s). ==="
    )
    log_fn(summary)
    prog(1.0, summary)


# ---------------------------------------------------------------------------
# GTK4: Settings dialog (Joplin token)
# ---------------------------------------------------------------------------

def show_settings_dialog(parent, config: dict, on_save):
    dialog = Gtk.Dialog(title="Settings", transient_for=parent)
    dialog.set_default_size(480, 340)
    dialog.add_button("Cancel", Gtk.ResponseType.CANCEL)
    save_btn = dialog.add_button("Save", Gtk.ResponseType.OK)
    save_btn.add_css_class("suggested-action")

    content = dialog.get_content_area()
    content.set_spacing(14)
    content.set_margin_top(16)
    content.set_margin_bottom(16)
    content.set_margin_start(16)
    content.set_margin_end(16)

    # --- Joplin token ---
    token_lbl = Gtk.Label(label="Joplin Web Clipper Token")
    token_lbl.add_css_class("heading")
    token_lbl.set_halign(Gtk.Align.START)
    content.append(token_lbl)

    token_sub = Gtk.Label(label="Found in Joplin → Tools → Options → Web Clipper")
    token_sub.add_css_class("dim-label")
    token_sub.set_halign(Gtk.Align.START)
    content.append(token_sub)

    token_entry = Gtk.Entry()
    token_entry.set_text(config.get("joplin_token", ""))
    token_entry.set_visibility(False)
    token_entry.set_input_purpose(Gtk.InputPurpose.PASSWORD)
    token_entry.set_placeholder_text("Paste token here…")
    content.append(token_entry)

    content.append(Gtk.Separator())

    # --- Skip keywords ---
    kw_lbl = Gtk.Label(label="Subject Skip Keywords")
    kw_lbl.add_css_class("heading")
    kw_lbl.set_halign(Gtk.Align.START)
    content.append(kw_lbl)

    kw_sub = Gtk.Label(label="One keyword per line. Case-insensitive. Any match skips the thread.")
    kw_sub.add_css_class("dim-label")
    kw_sub.set_halign(Gtk.Align.START)
    kw_sub.set_wrap(True)
    content.append(kw_sub)

    kw_scroll = Gtk.ScrolledWindow()
    kw_scroll.set_min_content_height(100)
    kw_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
    kw_buffer = Gtk.TextBuffer()
    existing_keywords = config.get("skip_keywords", [])
    kw_buffer.set_text("\n".join(existing_keywords))
    kw_view = Gtk.TextView(buffer=kw_buffer)
    kw_view.set_monospace(True)
    kw_view.set_left_margin(6)
    kw_view.set_right_margin(6)
    kw_view.set_top_margin(4)
    kw_scroll.set_child(kw_view)

    kw_frame = Gtk.Frame()
    kw_frame.set_child(kw_scroll)
    content.append(kw_frame)

    def on_response(d, response):
        if response == Gtk.ResponseType.OK:
            config["joplin_token"] = token_entry.get_text().strip()
            raw_kw = kw_buffer.get_text(
                kw_buffer.get_start_iter(), kw_buffer.get_end_iter(), False
            )
            config["skip_keywords"] = [
                k.strip() for k in raw_kw.splitlines() if k.strip()
            ]
            save_config(config)
            on_save()
        d.destroy()

    dialog.connect("response", on_response)
    dialog.present()


# ---------------------------------------------------------------------------
# GTK4: Map Labels dialog
# ---------------------------------------------------------------------------

class MapLabelsDialog(Gtk.Dialog):
    """
    Two-column list: Gmail label | Joplin notebook dropdown | Always-include checkbox.
    Accepts pre-fetched labels from MainWindow to avoid redundant API calls.
    """

    def __init__(self, parent, config: dict, cached_labels: list = None):
        super().__init__(title="Map Gmail Labels → Joplin Notebooks", transient_for=parent)
        self.config = config
        self.set_default_size(720, 520)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        save_btn = self.add_button("Save Mappings", Gtk.ResponseType.OK)
        save_btn.add_css_class("suggested-action")

        self.gmail_labels: list = cached_labels or []
        self.joplin_notebooks: list = []
        self.row_widgets: dict = {}  # label_name -> (Gtk.DropDown, Gtk.CheckButton)

        content = self.get_content_area()
        content.set_spacing(10)
        content.set_margin_top(14)
        content.set_margin_bottom(14)
        content.set_margin_start(14)
        content.set_margin_end(14)

        self.status_label = Gtk.Label(label="Loading labels and notebooks…")
        self.status_label.set_halign(Gtk.Align.START)
        content.append(self.status_label)

        # Column headers
        header_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
        header_row.set_margin_start(8)
        header_row.set_margin_end(8)
        for text, expand, width in [("Gmail Label", True, -1), ("Joplin Notebook", False, 220), ("Always", False, -1)]:
            lbl = Gtk.Label(label=text)
            lbl.add_css_class("heading")
            lbl.set_xalign(0)
            if expand:
                lbl.set_hexpand(True)
            if width > 0:
                lbl.set_size_request(width, -1)
            header_row.append(lbl)
        content.append(header_row)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.add_css_class("boxed-list")
        scroll.set_child(self.list_box)
        content.append(scroll)

        threading.Thread(target=self._load_data, daemon=True).start()

    def _load_data(self):
        # Only fetch labels if not pre-supplied by MainWindow
        if not self.gmail_labels:
            GLib.idle_add(self.status_label.set_text, "Fetching Gmail labels…")
            try:
                service = get_gmail_service()
                self.gmail_labels = get_user_labels(service)
            except Exception as e:
                GLib.idle_add(self.status_label.set_text, f"Gmail error: {e}")
                return
        else:
            GLib.idle_add(
                self.status_label.set_text,
                f"{len(self.gmail_labels)} label(s) loaded. Fetching Joplin notebooks…"
            )

        GLib.idle_add(
            self.status_label.set_text,
            f"{len(self.gmail_labels)} label(s) found. Loading Joplin notebooks…"
        )
        token = self.config.get("joplin_token", "")
        try:
            self.joplin_notebooks = get_joplin_notebooks(token)
        except Exception as e:
            GLib.idle_add(self.status_label.set_text, f"Joplin error: {e}")
            return

        GLib.idle_add(self._build_rows)

    def _build_rows(self):
        if not self.gmail_labels:
            self.status_label.set_text("No user labels found in Gmail.")
            return

        self.status_label.set_text(
            f"{len(self.gmail_labels)} label(s) found. "
            "Map each to a Joplin notebook and check 'Always' to pre-select on every run."
        )

        label_mappings = self.config.get("label_mappings", {})
        always_include = self.config.get("always_include", [])
        notebook_titles = ["(none)"] + [n["title"] for n in self.joplin_notebooks]

        for label in sorted(self.gmail_labels, key=lambda x: x["name"]):
            label_name = label["name"]

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=12)
            row.set_margin_top(6)
            row.set_margin_bottom(6)
            row.set_margin_start(8)
            row.set_margin_end(8)

            name_label = Gtk.Label(label=label_name)
            name_label.set_xalign(0)
            name_label.set_hexpand(True)
            name_label.set_ellipsize(Pango.EllipsizeMode.END)
            row.append(name_label)

            store = Gtk.StringList.new(notebook_titles)
            dropdown = Gtk.DropDown.new(store, None)
            dropdown.set_size_request(220, -1)

            mapped_id = label_mappings.get(label_name)
            if mapped_id:
                nb = next((n for n in self.joplin_notebooks if n["id"] == mapped_id), None)
                if nb and nb["title"] in notebook_titles:
                    dropdown.set_selected(notebook_titles.index(nb["title"]))
            row.append(dropdown)

            always_check = Gtk.CheckButton()
            always_check.set_active(label_name in always_include)
            always_check.set_halign(Gtk.Align.CENTER)
            row.append(always_check)

            self.row_widgets[label_name] = (dropdown, always_check)

            list_row = Gtk.ListBoxRow()
            list_row.set_child(row)
            self.list_box.append(list_row)

    def get_updated_mappings(self):
        """Return (label_mappings dict, always_include list) from current widget state."""
        notebook_titles = [n["title"] for n in self.joplin_notebooks]
        label_mappings, always_include = {}, []

        for label_name, (dropdown, always_check) in self.row_widgets.items():
            idx = dropdown.get_selected()
            if idx > 0:  # 0 = "(none)"
                title = notebook_titles[idx - 1]
                nb = next((n for n in self.joplin_notebooks if n["title"] == title), None)
                if nb:
                    label_mappings[label_name] = nb["id"]
            if always_check.get_active():
                always_include.append(label_name)

        return label_mappings, always_include


# ---------------------------------------------------------------------------
# GTK4: Label selector dialog (run-time pick before processing)
# ---------------------------------------------------------------------------

class SelectLabelsDialog(Gtk.Dialog):
    """
    Checklist of Gmail labels. Pre-checks 'always_include' labels.
    Labels without a Joplin mapping are shown but disabled with a note.
    Accepts pre-fetched labels from MainWindow to avoid redundant API calls.
    """

    def __init__(self, parent, config: dict, cached_labels: list = None):
        super().__init__(title="Select Labels to Process", transient_for=parent)
        self.config = config
        self.set_default_size(500, 420)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        go_btn = self.add_button("Process Selected", Gtk.ResponseType.OK)
        go_btn.add_css_class("suggested-action")

        self.gmail_labels: list = cached_labels or []
        self.check_widgets: dict = {}  # label_name -> (Gtk.CheckButton, label dict)

        content = self.get_content_area()
        content.set_spacing(10)
        content.set_margin_top(14)
        content.set_margin_bottom(14)
        content.set_margin_start(14)
        content.set_margin_end(14)

        self.status_label = Gtk.Label(label="Fetching Gmail labels…")
        self.status_label.set_halign(Gtk.Align.START)
        content.append(self.status_label)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        self.list_box = Gtk.ListBox()
        self.list_box.set_selection_mode(Gtk.SelectionMode.NONE)
        self.list_box.add_css_class("boxed-list")
        scroll.set_child(self.list_box)
        content.append(scroll)

        threading.Thread(target=self._load_labels, daemon=True).start()

    def _load_labels(self):
        if not self.gmail_labels:
            GLib.idle_add(self.status_label.set_text, "Fetching Gmail labels…")
            try:
                service = get_gmail_service()
                self.gmail_labels = get_user_labels(service)
            except Exception as e:
                GLib.idle_add(self.status_label.set_text, f"Error: {e}")
                return
        GLib.idle_add(self._build_rows)

    def _build_rows(self):
        always_include = self.config.get("always_include", [])
        label_mappings = self.config.get("label_mappings", {})
        mapped = sum(1 for l in self.gmail_labels if l["name"] in label_mappings)
        self.status_label.set_text(
            f"{len(self.gmail_labels)} label(s) available, {mapped} mapped. "
            "Dimmed = no notebook mapped."
        )

        for label in sorted(self.gmail_labels, key=lambda x: x["name"]):
            label_name = label["name"]
            has_mapping = label_name in label_mappings

            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.set_margin_top(5)
            row.set_margin_bottom(5)
            row.set_margin_start(8)
            row.set_margin_end(8)

            check = Gtk.CheckButton(label=label_name)
            check.set_active(label_name in always_include and has_mapping)
            check.set_sensitive(has_mapping)
            check.set_hexpand(True)
            row.append(check)

            if not has_mapping:
                badge = Gtk.Label(label="no mapping")
                badge.add_css_class("dim-label")
                row.append(badge)

            self.check_widgets[label_name] = (check, label)

            list_row = Gtk.ListBoxRow()
            list_row.set_child(row)
            self.list_box.append(list_row)

    def get_selected_labels(self) -> list:
        return [lbl for name, (check, lbl) in self.check_widgets.items() if check.get_active()]


# ---------------------------------------------------------------------------
# GTK4: Main window
# ---------------------------------------------------------------------------

class MainWindow(Adw.ApplicationWindow):

    def __init__(self, app):
        super().__init__(application=app, title="Gmail → Joplin Sync")
        self.set_default_size(660, 500)
        self.config = load_config()
        self._processing = False
        self._pulse_timer_active = False
        self.cached_labels: list = []       # refreshed every boot and after auth
        self._gmail_service = None          # cached service instance
        self._build_ui()
        self._refresh_status()

    def _build_ui(self):
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)

        # --- Header bar ---
        header = Adw.HeaderBar()
        settings_btn = Gtk.Button(icon_name="preferences-system-symbolic")
        settings_btn.set_tooltip_text("Set Joplin API token")
        settings_btn.connect("clicked", self._on_settings)
        header.pack_end(settings_btn)

        reset_btn = Gtk.Button(icon_name="edit-clear-symbolic")
        reset_btn.set_tooltip_text("Reset processed-thread state")
        reset_btn.connect("clicked", self._on_reset_state)
        header.pack_end(reset_btn)
        root.append(header)

        # --- Content ---
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        content.set_margin_top(18)
        content.set_margin_bottom(18)
        content.set_margin_start(22)
        content.set_margin_end(22)

        # Status strip
        status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=24)
        self.gmail_status_lbl = Gtk.Label(label="Gmail: —")
        self.gmail_status_lbl.add_css_class("dim-label")
        self.joplin_status_lbl = Gtk.Label(label="Joplin: —")
        self.joplin_status_lbl.add_css_class("dim-label")
        self.api_key_lbl = Gtk.Label(label="Claude: —")
        self.api_key_lbl.add_css_class("dim-label")
        last_run_state = load_state().get("last_run")
        last_run_text = f"Last run: {last_run_state[:16]}" if last_run_state else "Last run: never"
        self.last_run_lbl = Gtk.Label(label=last_run_text)
        self.last_run_lbl.add_css_class("dim-label")
        self.last_run_lbl.set_hexpand(True)
        self.last_run_lbl.set_xalign(1)
        status_box.append(self.gmail_status_lbl)
        status_box.append(self.joplin_status_lbl)
        status_box.append(self.api_key_lbl)
        status_box.append(self.last_run_lbl)
        content.append(status_box)

        # Separator
        content.append(Gtk.Separator())

        # Action buttons
        btn_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        btn_box.set_halign(Gtk.Align.CENTER)

        auth_btn = Gtk.Button(label="Authenticate Gmail")
        auth_btn.connect("clicked", self._on_authenticate)
        btn_box.append(auth_btn)

        map_btn = Gtk.Button(label="Map Labels")
        map_btn.connect("clicked", self._on_map_labels)
        btn_box.append(map_btn)

        self.process_btn = Gtk.Button(label="Process Emails")
        self.process_btn.add_css_class("suggested-action")
        self.process_btn.connect("clicked", self._on_process)
        btn_box.append(self.process_btn)

        content.append(btn_box)

        # Operation status label — shows current step in plain English
        self.op_status_lbl = Gtk.Label(label="")
        self.op_status_lbl.set_halign(Gtk.Align.START)
        self.op_status_lbl.add_css_class("dim-label")
        self.op_status_lbl.set_ellipsize(Pango.EllipsizeMode.END)
        self.op_status_lbl.set_visible(False)
        content.append(self.op_status_lbl)

        # Progress bar (hidden when idle)
        self.progress = Gtk.ProgressBar()
        self.progress.set_pulse_step(0.08)
        self.progress.set_visible(False)
        content.append(self.progress)

        # Log view
        log_frame = Gtk.Frame()
        log_scroll = Gtk.ScrolledWindow()
        log_scroll.set_vexpand(True)
        log_scroll.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        self.log_buffer = Gtk.TextBuffer()
        self.log_view = Gtk.TextView(buffer=self.log_buffer)
        self.log_view.set_editable(False)
        self.log_view.set_monospace(True)
        self.log_view.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
        self.log_view.set_left_margin(6)
        self.log_view.set_right_margin(6)
        log_scroll.set_child(self.log_view)
        log_frame.set_child(log_scroll)
        content.append(log_frame)

        root.append(content)
        self.set_content(root)

    # --- Helpers ---

    def _log(self, message: str):
        def _do():
            ts = datetime.now().strftime("%H:%M:%S")
            end = self.log_buffer.get_end_iter()
            self.log_buffer.insert(end, f"[{ts}] {message}\n")
            # Auto-scroll to bottom
            adj = self.log_view.get_vadjustment()
            adj.set_value(adj.get_upper() - adj.get_page_size())
        GLib.idle_add(_do)

    def _set_gmail_status(self, text: str):
        GLib.idle_add(self.gmail_status_lbl.set_text, f"Gmail: {text}")

    def _set_joplin_status(self, text: str):
        GLib.idle_add(self.joplin_status_lbl.set_text, f"Joplin: {text}")

    def _set_api_key_status(self, text: str):
        GLib.idle_add(self.api_key_lbl.set_text, f"Claude: {text}")

    def _set_progress(self, fraction: float, status: str):
        """Update the operation status label and progress bar from any thread."""
        def _do():
            self.op_status_lbl.set_text(status)
            self.op_status_lbl.set_visible(bool(status))
            if 0.0 < fraction < 1.0:
                # Switch to determinate mode once we have a real fraction
                self.progress.set_fraction(fraction)
            elif fraction >= 1.0:
                self.progress.set_fraction(1.0)
            # fraction == 0.0 keeps the pulse going (preflight phase)
        GLib.idle_add(_do)

    def _refresh_status(self):
        def _check():
            # Anthropic API key — fail fast rather than mid-processing
            if os.environ.get("ANTHROPIC_API_KEY"):
                self._set_api_key_status("key set")
            else:
                self._set_api_key_status("NO KEY SET")
                GLib.idle_add(self._log, "WARNING: ANTHROPIC_API_KEY is not set. Processing will fail.")

            # Gmail — fetch and cache service + labels on every boot
            if TOKEN_FILE.exists():
                try:
                    self._gmail_service = get_gmail_service()
                    self.cached_labels = get_user_labels(self._gmail_service)
                    self._set_gmail_status(f"connected ({len(self.cached_labels)} labels)")
                except Exception:
                    self._gmail_service = None
                    self.cached_labels = []
                    self._set_gmail_status("token invalid")
            else:
                self._set_gmail_status("not authenticated")

            # Joplin
            token = self.config.get("joplin_token", "")
            if not token:
                self._set_joplin_status("no token set")
            elif joplin_ping(token):
                self._set_joplin_status("connected")
            else:
                self._set_joplin_status("not reachable")

        threading.Thread(target=_check, daemon=True).start()

    def _pulse_progress(self):
        if self._processing:
            self.progress.pulse()
            return True
        self._pulse_timer_active = False
        return False

    # --- Button handlers ---

    def _on_settings(self, _btn):
        def on_save():
            self._log("Joplin token saved.")
            self._refresh_status()
        show_settings_dialog(self, self.config, on_save)

    def _on_authenticate(self, _btn):
        def _auth():
            self._set_gmail_status("authenticating…")
            try:
                self._gmail_service = get_gmail_service()
                self.cached_labels = get_user_labels(self._gmail_service)
                self._set_gmail_status(f"connected ({len(self.cached_labels)} labels)")
                self._log(f"Gmail authenticated. {len(self.cached_labels)} label(s) loaded.")
            except Exception as e:
                self._gmail_service = None
                self.cached_labels = []
                self._set_gmail_status("failed")
                self._log(f"Gmail auth error: {e}")
        threading.Thread(target=_auth, daemon=True).start()

    def _on_map_labels(self, _btn):
        dialog = MapLabelsDialog(self, self.config, cached_labels=self.cached_labels)

        def on_response(d, response):
            if response == Gtk.ResponseType.OK:
                mappings, always = dialog.get_updated_mappings()
                self.config["label_mappings"] = mappings
                self.config["always_include"] = always
                save_config(self.config)
                self._log(
                    f"Mappings saved: {len(mappings)} label(s) mapped. "
                    f"Always-include: {always or 'none'}."
                )
            d.destroy()

        dialog.connect("response", on_response)
        dialog.present()

    def _on_reset_state(self, _btn=None):
        """Prompt to clear state for a specific label or all labels."""
        state = load_state()
        thread_count = len(state.get("processed_threads", {}))

        confirm = Adw.MessageDialog.new(
            self,
            "Reset Processed State",
            f"This will clear state for all {thread_count} processed thread(s), "
            "causing them to be re-processed on the next run.\n\nThis cannot be undone.",
        )
        confirm.add_response("cancel", "Cancel")
        confirm.add_response("reset", "Reset All")
        confirm.set_response_appearance("reset", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_confirm(d, response):
            if response == "reset":
                save_state({"processed_threads": {}, "last_run": None})
                GLib.idle_add(self.last_run_lbl.set_text, "Last run: never")
                self._log(f"State reset. {thread_count} thread record(s) cleared.")
            d.destroy()

        confirm.connect("response", on_confirm)
        confirm.present()

    def _on_process(self, _btn):
        dialog = SelectLabelsDialog(self, self.config, cached_labels=self.cached_labels)

        def on_response(d, response):
            d.destroy()
            if response != Gtk.ResponseType.OK:
                return

            selected = dialog.get_selected_labels()
            if not selected:
                self._log("No labels selected.")
                return

            label_names = [l["name"] for l in selected]
            self._log(f"Starting processing for: {label_names}")
            self.process_btn.set_sensitive(False)
            self.progress.set_visible(True)
            self.op_status_lbl.set_visible(True)
            self._processing = True
            if not self._pulse_timer_active:
                self._pulse_timer_active = True
                GLib.timeout_add(120, self._pulse_progress)

            def _run():
                process_labels(selected, self.config, self._log, progress_fn=self._set_progress)
                self._processing = False
                state = load_state()
                lr = state.get("last_run", "")
                GLib.idle_add(self.last_run_lbl.set_text, f"Last run: {lr[:16]}" if lr else "Last run: never")
                GLib.idle_add(self.process_btn.set_sensitive, True)
                GLib.idle_add(self.progress.set_visible, False)
                GLib.idle_add(self.op_status_lbl.set_visible, False)
                GLib.idle_add(self.op_status_lbl.set_text, "")

            threading.Thread(target=_run, daemon=True).start()

        dialog.connect("response", on_response)
        dialog.present()


# ---------------------------------------------------------------------------
# App entry point
# ---------------------------------------------------------------------------

class GmailJoplinApp(Adw.Application):
    def __init__(self):
        super().__init__(application_id=APP_ID)
        self.connect("activate", self._on_activate)

    def _on_activate(self, app):
        win = MainWindow(app)
        win.present()


def main():
    app = GmailJoplinApp()
    app.run()


if __name__ == "__main__":
    main()
