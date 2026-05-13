#!/usr/bin/env python3
"""
Joplin Task Sync GUI
GTK4 settings panel and manual sync trigger for joplin_task_sync.py

Launch:
    python3 ~/Applications/MeetingGui/joplin_task_sync_gui.py
"""

import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, Gio, GLib

import os
import sys
import json
import subprocess
import threading
from pathlib import Path

# ── Paths ──────────────────────────────────────────────────────────────────
SCRIPT_DIR  = Path(__file__).parent
SYNC_SCRIPT = SCRIPT_DIR / "joplin_task_sync.py"
STATE_DIR   = Path.home() / ".local" / "share" / "joplin-task-sync"
CONFIG_FILE = STATE_DIR / "config.json"
LOG_FILE    = STATE_DIR / "sync.log"

# ── Import sync module for API access ──────────────────────────────────────
sys.path.insert(0, str(SCRIPT_DIR))
try:
    from joplin_task_sync import (
        JoplinClient,
        build_notebook_map,
        JOPLIN_BASE_URL,
        TASK_LIST_TITLE,
        KANBAN_TITLE,
        WEEKLY_BOARD_TITLE,
        EXCLUDED_ROOT_NOTEBOOKS,
    )
    SYNC_MODULE_OK = True
    SYNC_MODULE_ERROR = ""
except Exception as e:
    SYNC_MODULE_OK    = False
    SYNC_MODULE_ERROR = str(e)
    JOPLIN_BASE_URL   = "http://localhost:41184"
    EXCLUDED_ROOT_NOTEBOOKS = {"Personal", "Trash"}

# ── Config helpers ─────────────────────────────────────────────────────────

def read_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text())
        except Exception:
            pass
    return {"excluded_notebooks": sorted(EXCLUDED_ROOT_NOTEBOOKS)}


def write_config(excluded: set) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.write_text(
        json.dumps({"excluded_notebooks": sorted(excluded)}, indent=2)
    )


# ── Main window ───────────────────────────────────────────────────────────

class MainWindow(Gtk.ApplicationWindow):

    def __init__(self, app: Gtk.Application) -> None:
        super().__init__(application=app, title="Joplin Task Sync")
        self.set_default_size(500, 660)

        self._sync_running     = False
        self._nb_rows: dict    = {}   # title → (Gtk.CheckButton, nb_id)
        self._prev_excluded: set = set()

        self._build_ui()
        self._load_notebooks()
        self._refresh_log()

    # ── UI construction ───────────────────────────────────────────────────

    def _build_ui(self) -> None:
        # Header bar
        header = Gtk.HeaderBar()
        self.set_titlebar(header)

        self._spinner = Gtk.Spinner()
        header.pack_end(self._spinner)

        self._sync_btn = Gtk.Button(label="🔄  Sync Now")
        self._sync_btn.add_css_class("suggested-action")
        self._sync_btn.connect("clicked", self._on_sync_clicked)
        header.pack_start(self._sync_btn)

        # Root container
        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        self.set_child(root)

        # ── Notebook section ──────────────────────────────────────────────
        nb_frame = Gtk.Frame(
            label=" Root Notebooks ",
            margin_start=12, margin_end=12,
            margin_top=12,   margin_bottom=0,
        )
        root.append(nb_frame)

        nb_outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        nb_frame.set_child(nb_outer)

        nb_scroll = Gtk.ScrolledWindow(vexpand=False)
        nb_scroll.set_min_content_height(180)
        nb_scroll.set_max_content_height(280)
        nb_outer.append(nb_scroll)

        self._nb_list = Gtk.Box(
            orientation=Gtk.Orientation.VERTICAL, spacing=2,
            margin_start=10, margin_end=10,
            margin_top=8,    margin_bottom=8,
        )
        nb_scroll.set_child(self._nb_list)

        # Notebook action row
        nb_actions = Gtk.Box(
            orientation=Gtk.Orientation.HORIZONTAL, spacing=8,
            margin_start=10, margin_end=10,
            margin_top=4,    margin_bottom=10,
        )
        nb_outer.append(nb_actions)

        refresh_btn = Gtk.Button(label="↺  Refresh")
        refresh_btn.connect("clicked", lambda _: self._load_notebooks())
        nb_actions.append(refresh_btn)

        self._save_btn = Gtk.Button(label="💾  Save")
        self._save_btn.add_css_class("suggested-action")
        self._save_btn.connect("clicked", self._on_save_clicked)
        nb_actions.append(self._save_btn)

        self._status_lbl = Gtk.Label(label="", hexpand=True, xalign=1.0)
        self._status_lbl.add_css_class("dim-label")
        nb_actions.append(self._status_lbl)

        # ── Log section ───────────────────────────────────────────────────
        log_frame = Gtk.Frame(
            label=" Sync Log ",
            margin_start=12, margin_end=12,
            margin_top=10,   margin_bottom=12,
            vexpand=True,
        )
        root.append(log_frame)

        log_scroll = Gtk.ScrolledWindow(vexpand=True)
        log_frame.set_child(log_scroll)

        self._log_view = Gtk.TextView(
            editable=False,
            monospace=True,
            wrap_mode=Gtk.WrapMode.WORD_CHAR,
            margin_start=8, margin_end=8,
            margin_top=6,   margin_bottom=6,
        )
        log_scroll.set_child(self._log_view)
        self._log_scroll = log_scroll

    # ── Notebook list ─────────────────────────────────────────────────────

    def _load_notebooks(self) -> None:
        """Fetch root notebooks from Joplin and populate checkbox list."""
        self._clear_nb_list()

        config   = read_config()
        excluded = set(config.get("excluded_notebooks", []))
        self._prev_excluded = set(excluded)

        if not SYNC_MODULE_OK:
            self._nb_list.append(
                Gtk.Label(label=f"Cannot import sync module:\n{SYNC_MODULE_ERROR}")
            )
            return

        token = os.environ.get("JOPLIN_TOKEN", "")
        if not token:
            self._nb_list.append(
                Gtk.Label(label="JOPLIN_TOKEN not set in environment.")
            )
            return

        try:
            client  = JoplinClient(JOPLIN_BASE_URL, token)
            nb_map  = build_notebook_map(client)
            roots   = sorted(
                [(nb["title"], nid)
                 for nid, nb in nb_map.items()
                 if not nb.get("parent_id")],
                key=lambda x: x[0].lower(),
            )
            for title, nb_id in roots:
                chk = Gtk.CheckButton(
                    label=title,
                    active=(title not in excluded),
                )
                self._nb_list.append(chk)
                self._nb_rows[title] = (chk, nb_id)

            self._set_status(f"{len(roots)} notebooks loaded.")
        except Exception as e:
            self._nb_list.append(Gtk.Label(label=f"Error: {e}"))

    def _clear_nb_list(self) -> None:
        while (child := self._nb_list.get_first_child()):
            self._nb_list.remove(child)
        self._nb_rows.clear()

    # ── Save / delete management notes ───────────────────────────────────

    def _on_save_clicked(self, _btn) -> None:
        now_excluded = {
            title for title, (chk, _) in self._nb_rows.items()
            if not chk.get_active()
        }
        write_config(now_excluded)

        # Delete management notes for notebooks that just got excluded
        newly_excluded = now_excluded - self._prev_excluded
        if newly_excluded:
            self._save_btn.set_sensitive(False)
            self._set_status("Deleting management notes…")
            threading.Thread(
                target=self._delete_mgmt_notes_thread,
                args=(newly_excluded,),
                daemon=True,
            ).start()
        else:
            self._prev_excluded = now_excluded
            self._set_status("Saved.")

    def _delete_mgmt_notes_thread(self, nb_titles: set) -> None:
        token = os.environ.get("JOPLIN_TOKEN", "")
        if not token:
            GLib.idle_add(self._set_status, "Error: JOPLIN_TOKEN not set.")
            GLib.idle_add(self._save_btn.set_sensitive, True)
            return

        mgmt = [TASK_LIST_TITLE, KANBAN_TITLE, WEEKLY_BOARD_TITLE]

        try:
            client = JoplinClient(JOPLIN_BASE_URL, token)
            nb_map = build_notebook_map(client)
            id_by_title = {
                nb["title"]: nid
                for nid, nb in nb_map.items()
                if not nb.get("parent_id")
            }

            for nb_title in nb_titles:
                nb_id = id_by_title.get(nb_title)
                if not nb_id:
                    continue
                for mgmt_title in mgmt:
                    for note in client.search_notes_by_title(mgmt_title):
                        if note.get("parent_id") == nb_id:
                            try:
                                client.delete_note(note["id"])
                                GLib.idle_add(
                                    self._append_log,
                                    f"Deleted '{mgmt_title}' from '{nb_title}'",
                                )
                            except Exception as e:
                                GLib.idle_add(
                                    self._append_log,
                                    f"Could not delete '{mgmt_title}' "
                                    f"from '{nb_title}': {e}",
                                )

            GLib.idle_add(self._set_status, "Saved and cleaned up.")
        except Exception as e:
            GLib.idle_add(self._set_status, f"Cleanup error: {e}")
        finally:
            GLib.idle_add(self._save_btn.set_sensitive, True)
            # Reload notebook list so checkboxes reflect new state
            GLib.idle_add(self._load_notebooks)

    # ── Sync ─────────────────────────────────────────────────────────────

    def _on_sync_clicked(self, _btn) -> None:
        if self._sync_running:
            return
        self._set_syncing(True)
        threading.Thread(target=self._sync_thread, daemon=True).start()

    def _sync_thread(self) -> None:
        try:
            proc = subprocess.Popen(
                [sys.executable, str(SYNC_SCRIPT)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=os.environ.copy(),
            )
            for line in proc.stdout:
                GLib.idle_add(self._append_log, line.rstrip())
            proc.wait()
            GLib.idle_add(self._on_sync_done, proc.returncode)
        except Exception as e:
            GLib.idle_add(self._append_log, f"ERROR launching sync: {e}")
            GLib.idle_add(self._set_syncing, False)

    def _on_sync_done(self, code: int) -> None:
        msg = "✓ Sync complete." if code == 0 else f"✗ Sync failed (exit {code})."
        self._set_status(msg)
        self._set_syncing(False)

    # ── Helpers ───────────────────────────────────────────────────────────

    def _set_syncing(self, running: bool) -> None:
        self._sync_running = running
        self._sync_btn.set_sensitive(not running)
        if running:
            self._spinner.start()
        else:
            self._spinner.stop()

    def _set_status(self, msg: str) -> None:
        self._status_lbl.set_text(msg)

    def _append_log(self, line: str) -> None:
        buf = self._log_view.get_buffer()
        buf.insert(buf.get_end_iter(), line + "\n")
        GLib.idle_add(self._scroll_log_to_bottom)

    def _scroll_log_to_bottom(self) -> bool:
        adj = self._log_scroll.get_vadjustment()
        adj.set_value(adj.get_upper() - adj.get_page_size())
        return False

    def _refresh_log(self) -> None:
        if not LOG_FILE.exists():
            return
        try:
            lines = LOG_FILE.read_text().splitlines()[-80:]
            self._log_view.get_buffer().set_text("\n".join(lines))
            GLib.idle_add(self._scroll_log_to_bottom)
        except Exception:
            pass


# ── Application ───────────────────────────────────────────────────────────

class App(Gtk.Application):

    def __init__(self) -> None:
        super().__init__(
            application_id="com.fractionalmedtech.joplin-task-sync-gui",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )

    def do_activate(self) -> None:
        MainWindow(self).present()


def main() -> None:
    App().run(sys.argv)


if __name__ == "__main__":
    main()
