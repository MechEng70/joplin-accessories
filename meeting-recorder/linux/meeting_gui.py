#!/usr/bin/env python3
"""
meeting_gui.py

Compact GTK4 floating window for the meeting recorder.
Always-on-top, minimal footprint, works natively on Wayland/COSMIC.

Features:
  - Record mic + system audio via PipeWire/ffmpeg
  - Transcribe with faster-whisper (large-v3, CUDA)
  - Real transcription progress bar (ffprobe duration + segment timestamps)
  - Live GPU utilization and VRAM display via pynvml
  - Summarize via Anthropic API with structured meeting note schema
  - Save to Joplin via Web Clipper API with full notebook tree picker
  - Error dialogs for Joplin unreachable, missing env vars, insufficient funds

Dependencies:
    python3-gobject (system)
    pip install faster-whisper anthropic requests pynvml
    sudo dnf install libnotify ffmpeg
"""

import os
import sys
import signal
import subprocess
import tempfile
import threading
import queue
import datetime
import json
import warnings
from pathlib import Path

# Logging — full tracebacks go to ~/.local/share/meeting-recorder/meeting_recorder.log
import logging
import traceback

LOG_FILE = Path.home() / ".local" / "share" / "meeting-recorder" / "meeting_recorder.log"
LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
LOG_SIZE_WARN_MB = 10   # Warn when log exceeds this size

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("meeting_recorder")

# GTK4
try:
    import gi
    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, GLib, Gio
except ImportError:
    print("ERROR: python3-gobject is required.  sudo dnf install python3-gobject")
    sys.exit(1)

try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

# Pending recordings store
PENDING_DIR = Path.home() / ".local" / "share" / "meeting-recorder" / "pending"

def pending_dir() -> Path:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    return PENDING_DIR

def write_sidecar(wav_path: Path, status: str, transcript_path: str = "") -> Path:
    """
    Write or overwrite a JSON sidecar for wav_path.
    status: "recording" | "transcribing" | "summarizing" | "failed"
    Returns the sidecar path.
    """
    sidecar = wav_path.with_suffix(".json")
    sidecar.write_text(json.dumps({
        "timestamp":       datetime.datetime.now().isoformat(),
        "audio_path":      str(wav_path),
        "transcript_path": transcript_path,
        "status":          status,
    }, indent=2))
    return sidecar


def pid_file_for(wav_path: Path) -> Path:
    """Return the PID file path for a given WAV."""
    return wav_path.with_suffix(".ffmpeg.pid")


def write_pid_file(wav_path: Path, pid: int):
    """Write ffmpeg PID alongside the WAV so a crash recovery can kill it."""
    pid_file_for(wav_path).write_text(str(pid))


def kill_orphan_ffmpeg(wav_path: Path):
    """
    If a PID file exists for wav_path, check if that process is still running
    and if so kill it. Cleans up the PID file regardless.
    """
    pid_file = pid_file_for(wav_path)
    if not pid_file.exists():
        return
    try:
        pid = int(pid_file.read_text().strip())
        # Check if process is still alive before killing.
        os.kill(pid, 0)          # raises OSError if not running
        os.kill(pid, signal.SIGINT)
        log.info("Killed orphan ffmpeg PID %d for %s", pid, wav_path.name)
    except (OSError, ProcessLookupError):
        pass   # process already dead
    except Exception as e:
        log.warning("Could not kill orphan ffmpeg: %s", e)
    finally:
        pid_file.unlink(missing_ok=True)


def kill_all_orphan_ffmpeg():
    """
    Scan the pending directory for any leftover PID files and kill them.
    Called at startup before the main window opens.
    """
    pd = pending_dir()
    for pid_file in pd.glob("*.ffmpeg.pid"):
        wav_path = pid_file.with_suffix("").with_suffix(".wav")
        kill_orphan_ffmpeg(wav_path)


def save_pending(audio_path: str, transcript: str = "") -> Path:
    """
    Move audio to pending dir and write a JSON sidecar with status=failed.
    Used when an error occurs mid-pipeline for an audio file not already
    in the pending directory.
    Returns the sidecar path.
    """
    pd       = pending_dir()
    src      = Path(audio_path)
    wav_dest = src if src.parent == pd else None

    # Only move if the file isn't already in the pending dir.
    if wav_dest is None:
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        wav_dest  = pd / f"recording_{timestamp}.wav"
        src.rename(wav_dest)

    transcript_path = ""
    if transcript:
        t_dest = wav_dest.with_suffix(".txt")
        t_dest.write_text(transcript, encoding="utf-8")
        transcript_path = str(t_dest)

    return write_sidecar(wav_dest, "failed", transcript_path)

def list_pending() -> list[dict]:
    """
    Return list of pending recording metadata dicts, sorted oldest first.

    Handles three cases:
    1. Normal: .wav + .json sidecar exist.
    2. Transcript-only: .txt exists but no .wav (API failed after transcription).
       A synthetic sidecar is created on the fly so the item appears in the picker.
    3. Orphaned sidecar: .json exists but .wav is gone — cleaned up silently.

    Each dict has: timestamp, audio_path, transcript_path, status, sidecar_path.
    """
    pd      = pending_dir()
    records = []

    # ── Normal path: sidecar-driven records ──────────────────────────────────
    for sidecar in sorted(pd.glob("*.json")):
        try:
            data = json.loads(sidecar.read_text())
            data["sidecar_path"] = str(sidecar)
            if Path(data["audio_path"]).exists():
                records.append(data)
            else:
                # WAV is gone — check if a transcript txt survives.
                tp = data.get("transcript_path", "")
                if tp and Path(tp).exists():
                    # Promote to transcript-only record.
                    data["status"]       = "summarizing"
                    data["audio_path"]   = tp   # point at txt so discard works
                    records.append(data)
                else:
                    sidecar.unlink(missing_ok=True)
                    if tp:
                        Path(tp).unlink(missing_ok=True)
        except Exception:
            pass

    # ── Orphan path: .txt files with no matching .json sidecar ───────────────
    # This happens when the API fails after transcription and the error handler
    # saved only the transcript, not a sidecar (e.g. the recording_YYYYMMDD.txt
    # pattern used by the recovery script).
    sidecar_txts = {
        json.loads(Path(s).read_text()).get("transcript_path", "")
        for s in pd.glob("*.json")
        if (pd / s).exists()
    }
    for txt_file in sorted(pd.glob("*.txt")):
        if str(txt_file) in sidecar_txts:
            continue   # already covered by a sidecar
        # Also skip .transcript.txt files (mid-process cache, not standalone)
        if txt_file.name.endswith(".transcript.txt"):
            continue
        try:
            # Build a synthetic sidecar dict and write it to disk so subsequent
            # launches don't re-detect this as an orphan.
            ts       = datetime.datetime.fromtimestamp(txt_file.stat().st_mtime)
            ts_iso   = ts.isoformat()
            sidecar  = txt_file.with_suffix(".json")
            data     = {
                "timestamp":       ts_iso,
                "audio_path":      str(txt_file),   # no WAV; point at txt
                "transcript_path": str(txt_file),
                "status":          "summarizing",
                "sidecar_path":    str(sidecar),
            }
            sidecar.write_text(json.dumps(data, indent=2))
            records.append(data)
            log.info("Created synthetic sidecar for orphan transcript: %s", txt_file.name)
        except Exception as e:
            log.warning("Could not process orphan transcript %s: %s", txt_file.name, e)

    return records

def discard_pending(record: dict):
    """Delete a pending recording's WAV and sidecar."""
    Path(record["audio_path"]).unlink(missing_ok=True)
    Path(record["sidecar_path"]).unlink(missing_ok=True)

def _format_transcript_note(audio_path: str, transcript: str) -> str:
    """
    Format a raw transcript as a Joplin note body.
    Used by the transcribe-only profile — no AI summarization involved.
    """
    from pathlib import Path as _P
    date_str  = datetime.datetime.now().strftime("%Y-%m-%d")
    time_str  = datetime.datetime.now().strftime("%H:%M")
    filename  = _P(audio_path).name

    # Estimate duration from word count (rough: ~130 words/minute)
    word_count = len(transcript.split())
    mins       = word_count // 130
    secs       = (word_count % 130) * 60 // 130
    duration   = f"~{mins}m {secs:02d}s" if mins > 0 else f"~{secs}s"

    return (
        f"**Date:** {date_str}\n"
        f"**Time:** {time_str}\n"
        f"**Source:** {filename}\n"
        f"**Estimated Duration:** {duration}\n"
        f"**Words:** {word_count:,}\n\n"
        "---\n\n"
        "## Transcript\n\n"
        f"{transcript}\n"
    )


def _update_sidecar_status(audio_path: str, status: str, transcript_path: str = ""):
    """Update the status field in an existing sidecar, preserving other fields."""
    try:
        sidecar = Path(audio_path).with_suffix(".json")
        if sidecar.exists():
            data = json.loads(sidecar.read_text())
        else:
            data = {"timestamp": datetime.datetime.now().isoformat(), "audio_path": audio_path}
        data["status"] = status
        if transcript_path:
            data["transcript_path"] = transcript_path
        sidecar.write_text(json.dumps(data, indent=2))
    except Exception as e:
        log.warning("Could not update sidecar status: %s", e)


def is_cuda_oom(exc: Exception) -> bool:
    """Return True if exception is a CUDA out-of-memory error."""
    msg = str(exc).lower()
    return "out of memory" in msg or "cuda error" in msg or "cudaerroroutofmemory" in msg


# Version
APP_VERSION = "1.11"

# Configuration
CONFIG = {
    "joplin_token":         os.environ.get("JOPLIN_TOKEN", ""),
    "anthropic_api_key":    os.environ.get("ANTHROPIC_API_KEY", ""),
    "joplin_host":          "http://localhost:41184",
    "whisper_model":        "large-v3",
    "whisper_device":       "cuda",
    "whisper_compute_type": "float16",
}

# GPU monitoring
def _init_nvml() -> bool:
    try:
        import pynvml
        pynvml.nvmlInit()
        pynvml.nvmlDeviceGetHandleByIndex(0)
        return True
    except Exception:
        return False

NVML_AVAILABLE = _init_nvml()

def get_gpu_stats() -> str | None:
    if not NVML_AVAILABLE:
        return None
    try:
        import pynvml
        handle = pynvml.nvmlDeviceGetHandleByIndex(0)
        util   = pynvml.nvmlDeviceGetUtilizationRates(handle)
        mem    = pynvml.nvmlDeviceGetMemoryInfo(handle)
        used   = mem.used  / 1024 ** 3
        total  = mem.total / 1024 ** 3
        return f"GPU {util.gpu}%   VRAM {used:.1f}/{total:.1f} GB"
    except Exception:
        return None

# App state
class AppState:
    def __init__(self):
        self.recording   = False
        self.processing  = False
        self.ffmpeg_proc = None
        self.audio_path  = None
        self.lock        = threading.Lock()
        self.job_queue   = queue.Queue()   # queued (audio_path,) tuples

state = AppState()

# Notifications
def notify(title: str, body: str):
    try:
        subprocess.run(["notify-send", "-a", "Meeting Recorder", title, body], check=False)
    except FileNotFoundError:
        pass

# Audio recording
def get_default_sink_monitor() -> str:
    result = subprocess.run(["pactl", "get-default-sink"], capture_output=True, text=True, check=True)
    return result.stdout.strip() + ".monitor"

def start_recording() -> bool:
    """
    Begin recording directly into the pending directory.
    Writing to a persistent location from the very start means the WAV
    survives a system crash, power cut, or kernel panic — not just
    controlled failures in the processing pipeline.
    A sidecar with status="recording" is written immediately so a crash
    recovery scan on next launch can find the file.
    """
    try:
        monitor = get_default_sink_monitor()
    except subprocess.CalledProcessError:
        notify("Recording Error", "Could not determine audio sink via pactl.")
        return False

    pd        = pending_dir()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    wav_path  = pd / f"recording_{timestamp}.wav"

    # Write sidecar immediately — if we crash during recording this file
    # is how the recovery scan knows the WAV exists and is incomplete.
    write_sidecar(wav_path, "recording")

    cmd = [
        "ffmpeg", "-y",
        "-f", "pulse", "-i", "default",
        "-f", "pulse", "-i", monitor,
        "-filter_complex", "[0][1]amerge=inputs=2,pan=stereo|c0<c0+c2|c1<c1+c3",
        "-ac", "2", "-ar", "16000",
        str(wav_path),
    ]
    proc = subprocess.Popen(cmd, stderr=subprocess.DEVNULL)

    # Write PID file immediately so a crash recovery can kill this ffmpeg.
    write_pid_file(wav_path, proc.pid)
    log.info("Recording started: %s (ffmpeg PID %d)", wav_path.name, proc.pid)

    with state.lock:
        state.recording   = True
        state.ffmpeg_proc = proc
        state.audio_path  = str(wav_path)
    return True

def stop_recording() -> str | None:
    with state.lock:
        if not state.recording:
            return None
        proc       = state.ffmpeg_proc
        audio_path = state.audio_path
        state.recording   = False
        state.ffmpeg_proc = None
        state.audio_path  = None

    proc.send_signal(signal.SIGINT)
    proc.wait()

    # Clean up PID file on normal stop.
    pid_file_for(Path(audio_path)).unlink(missing_ok=True)
    return audio_path

# Audio duration
def get_audio_duration(audio_path: str) -> float | None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", audio_path],
            capture_output=True, text=True, check=True,
        )
        return float(json.loads(result.stdout)["format"]["duration"])
    except Exception:
        return None

# Transcription
def transcribe(audio_path: str, progress_cb=None) -> str:
    """
    Transcribe with faster-whisper (large-v3, CUDA).
    progress_cb(fraction, seg_end, duration) fires per segment.
    Falls back to CPU int8 if CUDA unavailable.
    """
    from faster_whisper import WhisperModel

    try:
        model = WhisperModel(
            CONFIG["whisper_model"],
            device=CONFIG["whisper_device"],
            compute_type=CONFIG["whisper_compute_type"],
        )
    except Exception:
        model = WhisperModel(CONFIG["whisper_model"], device="cpu", compute_type="int8")

    duration = get_audio_duration(audio_path)
    raw_segments, info = model.transcribe(audio_path, beam_size=5)

    if duration is None and info.duration:
        duration = info.duration

    segments = []
    for seg in raw_segments:
        segments.append(seg)
        if progress_cb and duration and duration > 0:
            fraction = min(seg.end / duration, 1.0)
            progress_cb(fraction, seg.end, duration)

    return " ".join(seg.text.strip() for seg in segments)

# ── Summary profiles ──────────────────────────────────────────────────────────

PROFILES_FILE = Path.home() / ".local" / "share" / "meeting-recorder" / "profiles.json"

BUILTIN_PROFILES = [
    {
        "id":       "medical_device_meeting",
        "name":     "Medical Device Meeting",
        "builtin":  True,
        "system":   (
            "You are a professional meeting notes assistant for a medical device consultant "
            "specializing in FDA regulatory strategy, design controls, and R&D consulting. "
            "Write with precision. Omit filler. Do not pad sections. "
            "Never fabricate details not present in the transcript. "
            "If a field cannot be determined from the transcript, write 'Not identified'."
        ),
        "prompt": """\
Produce structured meeting notes in Markdown from the transcript below.
The current date is {date}.

Use exactly this structure. Omit Decisions, Action Items, or Open Questions only \
if genuinely empty. All other fields are always required.

---

**Date:** {date}

**Attendees:** Extract all names mentioned or identifiable from the transcript. \
Include organization or role where determinable. If not identifiable, write "Not identified."

**Location:** Extract from transcript context. For virtual meetings, note the platform \
if mentioned (e.g., "Virtual - Google Meet"). If not determinable, write "Not identified."

**Agenda / Topics:**
- Bullet list of topics discussed, in the order they arose.

## Summary
A concise paragraph (3-5 sentences) describing the purpose and key outcomes.

## Decisions
- Bullet list of concrete decisions made, with enough context to stand alone.

## Action Items
- [ ] Action description (Owner: Name if identifiable, Due: date if mentioned)

## Open Questions
- Unresolved items, questions raised without resolution, or topics for follow-up.

---
TRANSCRIPT:
{transcript}
""",
        "focus":    "FDA regulatory strategy, design controls, R&D consulting",
        "sections": ["summary", "decisions", "action_items", "open_questions"],
        "include_attendees": True,
        "include_location":  True,
    },
    {
        "id":       "general_meeting",
        "name":     "General Meeting",
        "builtin":  True,
        "system":   (
            "You are a professional meeting notes assistant. "
            "Write with precision. Omit filler. Do not pad sections. "
            "Never fabricate details not present in the transcript. "
            "If a field cannot be determined from the transcript, write 'Not identified'."
        ),
        "prompt": """\
Produce structured meeting notes in Markdown from the transcript below.
The current date is {date}.

---

**Date:** {date}

**Attendees:** Extract all names mentioned or identifiable from the transcript. \
Include organization or role where determinable. If not identifiable, write "Not identified."

**Location:** Extract from transcript context. For virtual meetings, note the platform \
if mentioned. If not determinable, write "Not identified."

**Agenda / Topics:**
- Bullet list of topics discussed, in the order they arose.

## Summary
A concise paragraph (3-5 sentences) describing the purpose and key outcomes.

## Decisions
- Bullet list of concrete decisions made, with enough context to stand alone.

## Action Items
- [ ] Action description (Owner: Name if identifiable, Due: date if mentioned)

## Open Questions
- Unresolved items, questions raised without resolution, or topics for follow-up.

---
TRANSCRIPT:
{transcript}
""",
        "focus":    "General professional meeting",
        "sections": ["summary", "decisions", "action_items", "open_questions"],
        "include_attendees": True,
        "include_location":  True,
    },
    {
        "id":       "video_content_summary",
        "name":     "Video / Content Summary",
        "builtin":  True,
        "system":   (
            "You are a content summarization assistant. "
            "Summarize the key ideas, arguments, and takeaways from the provided transcript. "
            "Write with clarity and precision. Do not fabricate details. "
            "Omit filler. Do not pad sections."
        ),
        "prompt": """\
Produce a structured content summary in Markdown from the transcript below.
The current date is {date}.

---

**Date:** {date}

**Content Type:** Identify whether this is a lecture, interview, tutorial, presentation, \
or other format. If not determinable, write "Not identified."

**Topics Covered:**
- Bullet list of main topics, in the order they arose.

## Summary
A concise paragraph (3-5 sentences) describing the content and its main thesis or purpose.

## Key Takeaways
- Bullet list of the most important points, insights, or conclusions.

## Notable Details
- Specific facts, figures, references, or examples worth capturing.

## Open Questions
- Questions raised but not fully answered; areas worth further investigation.

---
TRANSCRIPT:
{transcript}
""",
        "focus":    "Video and content summarization",
        "sections": ["summary", "key_takeaways", "notable_details", "open_questions"],
        "include_attendees": False,
        "include_location":  False,
    },
    {
        "id":       "client_call",
        "name":     "Client Call",
        "builtin":  True,
        "system":   (
            "You are a professional client relationship assistant. "
            "Capture the key outcomes, commitments, and relationship context from client calls. "
            "Write with precision. Omit filler. Do not fabricate details. "
            "If a field cannot be determined, write 'Not identified'."
        ),
        "prompt": """\
Produce structured client call notes in Markdown from the transcript below.
The current date is {date}.

---

**Date:** {date}

**Client / Attendees:** Extract all names, companies, and roles mentioned. \
If not identifiable, write "Not identified."

**Call Type:** Discovery, check-in, deliverable review, strategic discussion, etc. \
Infer from context or write "Not identified."

**Topics Discussed:**
- Bullet list of topics covered, in the order they arose.

## Summary
A concise paragraph (3-5 sentences) describing the call purpose and outcomes.

## Commitments Made
- Bullet list of explicit commitments, promises, or deliverables agreed to by either party. \
Include owner and timeline if mentioned.

## Action Items
- [ ] Action description (Owner: Name if identifiable, Due: date if mentioned)

## Relationship Notes
- Context worth remembering: client concerns, preferences, upcoming events, key priorities.

## Follow-up Required
- Items needing follow-up before the next interaction.

---
TRANSCRIPT:
{transcript}
""",
        "focus":    "Client relationship management and call documentation",
        "sections": ["summary", "commitments", "action_items", "relationship_notes", "follow_up"],
        "include_attendees": True,
        "include_location":  True,
    },
    {
        "id":       "linkedin_medtech",
        "name":     "LinkedIn — MedTech",
        "builtin":  True,
        "system":   (
            "You are a professional LinkedIn ghostwriter specializing in medical device, "
            "FDA regulatory strategy, design controls, and MedTech consulting. "
            "You write in Jason's voice: direct, experienced, confident, and grounded in "
            "20+ years of hands-on device development. No corporate jargon. No filler. "
            "Insight-driven. Human. You know the difference between a 510(k) and a De Novo "
            "and you write for an audience that does too."
        ),
        "prompt": """The following is a spoken transcript of someone sharing thoughts on a MedTech topic.
Transform it into a single polished LinkedIn post in first person.

Guidelines:
- Suggest a working TITLE at the top (for the author's reference, not published).
- Open with a strong HOOK: 1-2 sentences that stop the scroll. Lead with an insight, a counterintuitive observation, or a concrete situation. Never start with "I".
- Body: 3-5 short paragraphs or a mix of paragraphs and tight bullets. Use line breaks generously — LinkedIn readers skim.
- Tone: direct, experienced, human. Peer-to-peer. Not a lecture. Not a press release.
- Keep total post length under 1,300 characters (before hashtags).
- End with a single thought-provoking question to drive engagement.
- Do NOT include hashtags — the author adds those manually.
- Do NOT fabricate details not present in the transcript.
- If the transcript is vague or rambling, distill the core insight and build around that.

TRANSCRIPT:
{transcript}
""",
        "focus":    "MedTech, FDA regulatory strategy, design controls, medical device consulting",
        "sections": ["summary"],
        "include_attendees": False,
        "include_location":  False,
    },
    {
        "id":       "linkedin_general",
        "name":     "LinkedIn — General",
        "builtin":  True,
        "system":   (
            "You are a professional LinkedIn ghostwriter for a founder and consultant. "
            "You write in a direct, confident, first-person voice. Posts are insight-driven, "
            "concise, and structured for LinkedIn's format. No corporate buzzwords. No filler. "
            "The author is real, curious, and occasionally sarcastic. Write like a clever friend "
            "sharing something genuinely worth reading."
        ),
        "prompt": """The following is a spoken transcript of someone sharing their thoughts on a topic.
Transform it into a single polished LinkedIn post in first person.

Guidelines:
- Suggest a working TITLE at the top (for the author's reference, not published).
- Open with a strong HOOK: 1-2 sentences that stop the scroll. Lead with an insight, a story, or a counterintuitive take. Never start with "I".
- Body: 3-5 short paragraphs or a mix of paragraphs and tight bullets. Use line breaks generously — LinkedIn readers skim.
- Tone: direct, human, occasionally wry. Smart but approachable. Never preachy.
- Keep total post length under 1,300 characters (before hashtags).
- End with a single thought-provoking question or observation to drive engagement.
- Do NOT include hashtags — the author adds those manually.
- Do NOT fabricate details not present in the transcript.
- If the transcript is vague or rambling, distill the core insight and build around that.

TRANSCRIPT:
{transcript}
""",
        "focus":    "Leadership, business building, technology, personal insights",
        "sections": ["summary"],
        "include_attendees": False,
        "include_location":  False,
    },
    {
        "id":       "beekeeping_inspection",
        "name":     "Meeting - Bee General",
        "builtin":  True,
        "system":   (
            "You are an expert beekeeping inspection assistant. Your job is to take a spoken "
            "transcript from a beekeeper conducting hive inspections and transform it into "
            "clean, structured inspection notes that are detailed enough for the beekeeper "
            "to reference later and clear enough for another experienced beekeeper to read "
            "and fully understand the state of each hive. "
            "Listen carefully for hive names (proper names, location descriptors like "
            "'front hive', 'top bar', 'the nuc', etc.) and frame references. "
            "If a hive name cannot be determined, number them in inspection order: "
            "Hive 1, Hive 2, etc. "
            "Never fabricate observations not present in the transcript. "
            "If a standard field was not mentioned, write 'Not recorded.' "
            "Use precise beekeeping terminology where appropriate."
        ),
        "prompt": """Produce structured beekeeping inspection notes in Markdown from the transcript below.
The inspection date is {date}.

---

## Inspection Overview

**Date:** {date}

**Inspector:** Extract name if mentioned. If not, write "Not recorded."

**Environment:**
- **Weather:** Extract if mentioned (sunny, overcast, rainy, etc.). If not, write "Not recorded."
- **Temperature:** Extract if mentioned. If not, write "Not recorded."
- **Time of Day:** Extract if mentioned (morning, afternoon, etc.). If not, write "Not recorded."
- **Season / Phenology:** Extract any seasonal references (nectar flow, dearth, spring buildup, etc.). If not, write "Not recorded."

**Hives Inspected:** List all hive names or numbers identified in the transcript.

---

For EACH hive identified in the transcript, create a section using the structure below.
If multiple hives are present, repeat this entire section for each one.
If a hive name cannot be determined from context, label it Hive 1, Hive 2, etc. in order of appearance.

---

## [Hive Name or Number]

**Overall Assessment:** A one to two sentence summary of this hive's current health and status.

**Queen Status:**
- Observed directly, or evidence of queen (eggs, young larvae, capped brood, etc.)
- Note any concerns (queen cells, laying workers, queenlessness signs)
- If not mentioned, write "Not recorded."

**Brood Pattern:**
- Solid, spotty, or other description
- Brood stages observed (eggs, open larvae, capped)
- Approximate percentage of frames with brood if mentioned
- If not mentioned, write "Not recorded."

**Honey and Food Stores:**
- Capped honey, nectar, pollen observations
- Adequacy of stores (sufficient, low, surplus, etc.)
- If not mentioned, write "Not recorded."

**Hive Population and Temperament:**
- Estimate of population size if mentioned (strong, moderate, small, etc.)
- Bee temperament during inspection (calm, defensive, aggressive, etc.)
- If not mentioned, write "Not recorded."

**Pest and Disease Observations:**
- Varroa mite levels or treatment status
- Small hive beetles, wax moths, or other pests
- Any signs of disease (chalkbrood, sacbrood, AFB, EFB, etc.)
- If not mentioned, write "Not recorded."

**Frame-by-Frame Observations:**
If the inspector called out specific frames, list each one with its observations.
If no specific frames were mentioned, provide a general summary of frame contents instead.
- Frame [N]: [observation]

**Actions Taken During Inspection:**
- List anything done during the inspection (added super, removed frame, treated for mites, requeened, combined, etc.)
- If nothing was done, write "None recorded."

**Follow-up Needed:**
- [ ] Follow-up item (Due: date if mentioned)
- List anything the inspector flagged for future attention
- If nothing flagged, write "None recorded."

---
TRANSCRIPT:
{transcript}
""",
        "focus":    "Beekeeping hive inspection documentation",
        "sections": ["summary", "action_items", "open_questions"],
        "include_attendees": False,
        "include_location":  False,
    },
    {
        "id":       "transcribe_only",
        "name":     "Transcribe Only",
        "builtin":  True,
        "transcribe_only": True,
        "system":   "",
        "prompt":   "",
        "focus":    "Raw transcription — no AI summarization",
        "sections": [],
        "include_attendees": False,
        "include_location":  False,
    },
    {
        "id":       "ip_assessment",
        "name":     "IP Assessment",
        "builtin":  True,
        "system":   (
            "You are a technology and intellectual property assessment assistant with expertise "
            "in evaluating inventions, patents, and commercialization potential. "
            "Write with precision. Omit filler. Do not fabricate details. "
            "If a field cannot be determined from the transcript, write 'Not identified'."
        ),
        "prompt": """\
Produce a structured IP and technology assessment in Markdown from the transcript below.
The current date is {date}.

---

**Date:** {date}

**Technology / Invention:** Identify the technology or invention being discussed. \
Include inventors or institution if mentioned.

**Attendees:** Extract all names, organizations, and roles. \
If not identifiable, write "Not identified."

**Topics Discussed:**
- Bullet list of topics covered, in the order they arose.

## Summary
A concise paragraph (3-5 sentences) describing the technology and the purpose of the discussion.

## Technology Description
- Core innovation and how it works.
- Stage of development (concept, prototype, validated, etc.).
- Key differentiators from existing solutions.

## IP Landscape
- Known patents, patent applications, or trade secrets discussed.
- Freedom to operate considerations raised.
- Gaps or risks identified.

## Commercialization Potential
- Target markets and applications identified.
- Potential licensees or partners mentioned.
- Barriers to commercialization discussed.

## Recommended Next Steps
- [ ] Action description (Owner: Name if identifiable, Due: date if mentioned)

## Open Questions
- Unresolved technical, legal, or commercial questions requiring follow-up.

---
TRANSCRIPT:
{transcript}
""",
        "focus":    "IP evaluation, patent landscape, commercialization strategy",
        "sections": ["summary", "technology_description", "ip_landscape", "commercialization", "next_steps", "open_questions"],
        "include_attendees": True,
        "include_location":  True,
    },
]


def load_profiles() -> list[dict]:
    """
    Load profiles from PROFILES_FILE. If the file does not exist, create it
    with the built-in defaults and return those. Merges built-ins so they are
    always present even if the file predates a new built-in being added.
    """
    PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)

    if not PROFILES_FILE.exists():
        _write_profiles(BUILTIN_PROFILES)
        return list(BUILTIN_PROFILES)

    try:
        saved = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except Exception:
        saved = []

    # Ensure all built-ins are present, then append any custom profiles.
    # Compare against builtin IDs — not saved IDs — so custom profiles
    # whose IDs don't clash with built-ins are always included.
    builtin_ids = {p["id"] for p in BUILTIN_PROFILES}
    custom      = [p for p in saved if p["id"] not in builtin_ids and not p.get("builtin")]
    merged      = list(BUILTIN_PROFILES) + custom
    return merged


def save_profiles(profiles: list[dict]):
    """Persist only custom profiles; built-ins are always regenerated from code."""
    custom = [p for p in profiles if not p.get("builtin")]
    _write_profiles(BUILTIN_PROFILES + custom)


def _write_profiles(profiles: list[dict]):
    PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILES_FILE.write_text(json.dumps(profiles, indent=2), encoding="utf-8")


def profile_to_form(p: dict) -> dict:
    """Extract simple-form fields from a profile dict."""
    return {
        "name":              p.get("name", ""),
        "focus":             p.get("focus", ""),
        "sections":          p.get("sections", ["summary", "action_items"]),
        "include_attendees": p.get("include_attendees", True),
        "include_location":  p.get("include_location", True),
    }


def form_to_prompts(form: dict) -> tuple[str, str]:
    """
    Generate system prompt and user prompt template from simple-form fields.
    Returns (system_prompt, user_prompt_template).
    """
    focus   = form.get("focus", "professional consulting")
    sections = form.get("sections", [])
    inc_att  = form.get("include_attendees", True)
    inc_loc  = form.get("include_location", True)

    system = (
        f"You are a professional meeting notes and summary assistant specializing in {focus}. "
        "Write with precision. Omit filler. Do not pad sections. "
        "Never fabricate details not present in the transcript. "
        "If a field cannot be determined from the transcript, write 'Not identified'."
    )

    lines = [
        "Produce structured notes in Markdown from the transcript below.",
        "The current date is {date}.",
        "",
        "---",
        "",
        "**Date:** {date}",
        "",
    ]

    if inc_att:
        lines += [
            "**Attendees:** Extract all names mentioned or identifiable from the transcript. "
            "Include organization or role where determinable. "
            'If not identifiable, write "Not identified."',
            "",
        ]
    if inc_loc:
        lines += [
            "**Location:** Extract from transcript context. For virtual meetings, note the "
            'platform if mentioned. If not determinable, write "Not identified."',
            "",
        ]

    lines += ["**Agenda / Topics:**", "- Bullet list of topics discussed, in the order they arose.", ""]

    section_templates = {
        "summary":           "## Summary\nA concise paragraph (3-5 sentences) describing the purpose and key outcomes.",
        "decisions":         "## Decisions\n- Bullet list of concrete decisions made, with enough context to stand alone.",
        "action_items":      "## Action Items\n- [ ] Action description (Owner: Name if identifiable, Due: date if mentioned)",
        "open_questions":    "## Open Questions\n- Unresolved items, questions raised without resolution, or topics for follow-up.",
        "key_takeaways":     "## Key Takeaways\n- Bullet list of the most important points, insights, or conclusions.",
        "notable_details":   "## Notable Details\n- Specific facts, figures, references, or examples worth capturing.",
        "commitments":       "## Commitments Made\n- Explicit commitments or deliverables agreed to by either party.",
        "relationship_notes":"## Relationship Notes\n- Context worth remembering: concerns, preferences, key priorities.",
        "follow_up":         "## Follow-up Required\n- Items needing follow-up before the next interaction.",
        "technology_description": "## Technology Description\n- Core innovation, stage of development, key differentiators.",
        "ip_landscape":      "## IP Landscape\n- Patents, freedom to operate, gaps or risks identified.",
        "commercialization": "## Commercialization Potential\n- Target markets, potential partners, barriers discussed.",
        "next_steps":        "## Recommended Next Steps\n- [ ] Action description (Owner: Name if identifiable, Due: date if mentioned)",
    }

    for sec in sections:
        if sec in section_templates:
            lines.append(section_templates[sec])
            lines.append("")

    lines += ["---", "TRANSCRIPT:", "{transcript}"]
    prompt = "\n".join(lines)
    return system, prompt


def summarize(transcript: str, profile: dict | None = None) -> str:
    """
    Summarize transcript using the given profile.
    Falls back to the Medical Device Meeting profile if none provided.
    """
    import anthropic

    if profile is None:
        profiles = load_profiles()
        profile  = next((p for p in profiles if p["id"] == "medical_device_meeting"), profiles[0])

    date_str      = datetime.datetime.now().strftime("%Y-%m-%d")
    system_prompt = profile.get("system", "")
    user_prompt   = profile.get("prompt", "{transcript}").format(
        transcript=transcript, date=date_str,
    )

    client  = anthropic.Anthropic(api_key=CONFIG["anthropic_api_key"])
    message = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )
    return message.content[0].text.strip()


# Joplin API
def get_notebooks() -> list[dict]:
    notebooks, page = [], 1
    while True:
        resp = requests.get(
            f"{CONFIG['joplin_host']}/folders",
            params={"token": CONFIG["joplin_token"], "fields": "id,title,parent_id", "limit": 100, "page": page},
            timeout=5,
        )
        resp.raise_for_status()
        data = resp.json()
        notebooks.extend(data.get("items", []))
        if not data.get("has_more", False):
            break
        page += 1
    return notebooks

def build_display_tree(notebooks: list[dict]) -> list[dict]:
    by_id = {nb["id"]: nb for nb in notebooks}

    def depth(nb):
        d, cur, vis = 0, nb, set()
        while cur.get("parent_id"):
            pid = cur["parent_id"]
            if pid in vis or pid not in by_id:
                break
            vis.add(pid); cur = by_id[pid]; d += 1
        return d

    def sort_path(nb):
        parts, cur, vis = [], nb, set()
        while True:
            parts.append(cur["title"].lower())
            pid = cur.get("parent_id")
            if not pid or pid not in by_id or pid in vis:
                break
            vis.add(pid); cur = by_id[pid]
        return "/".join(reversed(parts))

    return sorted(
        [{"id": nb["id"], "title": nb["title"], "depth": depth(nb), "_sort": sort_path(nb)} for nb in notebooks],
        key=lambda x: x["_sort"],
    )

def create_joplin_note(title: str, body: str, notebook_id: str) -> str:
    resp = requests.post(
        f"{CONFIG['joplin_host']}/notes",
        params={"token": CONFIG["joplin_token"]},
        json={"title": title, "body": body, "parent_id": notebook_id},
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()["id"]

def joplin_is_reachable() -> bool:
    try:
        requests.get(f"{CONFIG['joplin_host']}/ping", params={"token": CONFIG["joplin_token"]}, timeout=3).raise_for_status()
        return True
    except Exception:
        return False

def missing_env_vars() -> list[str]:
    labels = {"joplin_token": "JOPLIN_TOKEN", "anthropic_api_key": "ANTHROPIC_API_KEY"}
    return [labels[k] for k in labels if not CONFIG[k]]

# InfoDialog
class InfoDialog(Gtk.Window):
    def __init__(self, parent, title: str, body: str, buttons: list, application=None):
        super().__init__()
        self.set_title(title)
        self.set_resizable(False)
        self.set_default_size(440, -1)
        if application:
            self.set_application(application)
        if parent:
            self.set_transient_for(parent)
            self.set_modal(True)

        self._response_cb = None

        outer   = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_top(20)
        content.set_margin_bottom(16)
        content.set_margin_start(20)
        content.set_margin_end(20)

        t = Gtk.Label()
        t.set_markup(f"<b>{title}</b>")
        t.set_xalign(0)
        content.append(t)

        b = Gtk.Label(label=body)
        b.set_xalign(0)
        b.set_wrap(True)
        b.set_max_width_chars(54)
        content.append(b)

        outer.append(content)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)
        btn_row.set_margin_top(10)
        btn_row.set_margin_bottom(10)
        btn_row.set_margin_end(12)

        for label, response_id in buttons:
            btn = Gtk.Button(label=label)
            if response_id == Gtk.ResponseType.OK:
                btn.add_css_class("suggested-action")
            btn.connect("clicked", self._on_btn, response_id)
            btn_row.append(btn)

        outer.append(btn_row)
        self.set_child(outer)

    def connect_response(self, cb):
        self._response_cb = cb

    def _on_btn(self, btn, response_id):
        if self._response_cb:
            self._response_cb(self, response_id)

# PendingPicker
class PendingPicker(Gtk.Window):
    """
    Modal window listing saved pending recordings.
    User can select one to Resume or Discard.
    response_cb(dialog, response, record):
        response == Gtk.ResponseType.OK      → resume selected record
        response == Gtk.ResponseType.REJECT  → discard selected record
        response == Gtk.ResponseType.CANCEL  → dismiss
    """

    def __init__(self, parent, records: list[dict]):
        super().__init__(title="Pending Recordings")
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(460, 360)
        self._response_cb = None
        self._records     = records

        outer  = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        # Header
        header = Gtk.Label()
        header.set_markup("<b>Saved recordings waiting to be processed</b>")
        header.set_xalign(0)
        header.set_margin_top(16)
        header.set_margin_bottom(8)
        header.set_margin_start(16)
        header.set_margin_end(16)
        outer.append(header)

        sub = Gtk.Label(label="Select a recording, then Resume or Discard.")
        sub.set_xalign(0)
        sub.set_margin_bottom(8)
        sub.set_margin_start(16)
        outer.append(sub)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_margin_start(8)
        scroll.set_margin_end(8)
        scroll.set_margin_bottom(4)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        list_box.set_show_separators(True)

        for rec in records:
            row  = Gtk.ListBoxRow()
            ts   = rec.get("timestamp", "Unknown time")
            try:
                dt  = datetime.datetime.fromisoformat(ts)
                ts  = dt.strftime("%Y-%m-%d  %H:%M:%S")
            except Exception:
                pass
            wav    = Path(rec["audio_path"])
            size   = f"{wav.stat().st_size / 1024 / 1024:.1f} MB" if wav.exists() else "?"
            status = rec.get("status", "failed")
            status_labels = {
                "recording":    "⚠ interrupted mid-recording",
                "transcribing": "⚠ interrupted mid-transcription",
                "summarizing":  "⚠ interrupted mid-summarization",
                "failed":       "failed",
            }
            sstr = f"  [{status_labels.get(status, status)}]"

            label = Gtk.Label(label=f"{ts}   {size}{sstr}", xalign=0)
            label.set_margin_top(8)
            label.set_margin_bottom(8)
            label.set_margin_start(12)
            row.set_child(label)
            row._record = rec
            list_box.append(row)

        self._list_box = list_box
        scroll.set_child(list_box)
        outer.append(scroll)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_margin_top(10)
        btn_row.set_margin_bottom(10)
        btn_row.set_margin_start(12)
        btn_row.set_margin_end(12)

        discard_btn = Gtk.Button(label="Discard")
        discard_btn.add_css_class("destructive-action")
        discard_btn.connect("clicked", lambda b: self._emit(Gtk.ResponseType.REJECT))
        btn_row.append(discard_btn)

        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        btn_row.append(spacer)

        cancel_btn = Gtk.Button(label="Cancel")
        cancel_btn.connect("clicked", lambda b: self._emit(Gtk.ResponseType.CANCEL))
        btn_row.append(cancel_btn)

        resume_btn = Gtk.Button(label="Resume")
        resume_btn.add_css_class("suggested-action")
        resume_btn.connect("clicked", lambda b: self._emit(Gtk.ResponseType.OK))
        btn_row.append(resume_btn)

        outer.append(btn_row)
        self.set_child(outer)

    def connect_response(self, cb):
        self._response_cb = cb

    def _selected_record(self) -> dict | None:
        row = self._list_box.get_selected_row()
        return row._record if row else None

    def _emit(self, response_id):
        if self._response_cb:
            self._response_cb(self, response_id, self._selected_record())


# ── Profile Manager ───────────────────────────────────────────────────────────

SECTION_CHOICES = [
    ("summary",              "Summary"),
    ("decisions",            "Decisions"),
    ("action_items",         "Action Items"),
    ("open_questions",       "Open Questions"),
    ("key_takeaways",        "Key Takeaways"),
    ("notable_details",      "Notable Details"),
    ("commitments",          "Commitments Made"),
    ("relationship_notes",   "Relationship Notes"),
    ("follow_up",            "Follow-up Required"),
    ("technology_description","Technology Description"),
    ("ip_landscape",         "IP Landscape"),
    ("commercialization",    "Commercialization Potential"),
    ("next_steps",           "Recommended Next Steps"),
]


class ProfileEditorWindow(Gtk.Window):
    """
    Create or edit a custom profile.
    Simple form by default; Advanced checkbox reveals raw prompt editors.
    """

    def __init__(self, parent, profile: dict | None = None, on_save=None):
        super().__init__()
        self.set_title("Edit Profile" if profile else "New Profile")
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(560, 600)
        self._on_save  = on_save
        self._editing  = dict(profile) if profile else {}
        self._advanced = False

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)

        self._form_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self._form_box.set_margin_top(16)
        self._form_box.set_margin_bottom(8)
        self._form_box.set_margin_start(16)
        self._form_box.set_margin_end(16)

        self._build_form()
        scroll.set_child(self._form_box)
        outer.append(scroll)

        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_margin_top(10)
        btn_row.set_margin_bottom(10)
        btn_row.set_margin_start(12)
        btn_row.set_margin_end(12)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda b: self.destroy())
        btn_row.append(cancel)

        spacer = Gtk.Box(); spacer.set_hexpand(True)
        btn_row.append(spacer)

        save = Gtk.Button(label="Save Profile")
        save.add_css_class("suggested-action")
        save.connect("clicked", self._on_save_clicked)
        btn_row.append(save)

        outer.append(btn_row)
        self.set_child(outer)

    def _build_form(self):
        box = self._form_box

        # Clear existing children
        child = box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            box.remove(child)
            child = nxt

        form = self._editing

        # Name
        box.append(Gtk.Label(label="Profile Name", xalign=0))
        self._ent_name = Gtk.Entry()
        self._ent_name.set_text(form.get("name", ""))
        self._ent_name.set_placeholder_text("e.g. OrthoPreserve Design Review")
        box.append(self._ent_name)

        # Focus area
        box.append(Gtk.Label(label="Focus Area", xalign=0))
        self._ent_focus = Gtk.Entry()
        self._ent_focus.set_text(form.get("focus", ""))
        self._ent_focus.set_placeholder_text("e.g. orthopedic implant design controls")
        box.append(self._ent_focus)

        # Transcribe only toggle
        self._chk_transcribe_only = Gtk.CheckButton(label="Transcribe Only (skip AI summarization)")
        self._chk_transcribe_only.set_active(form.get("transcribe_only", False))
        self._chk_transcribe_only.connect("toggled", self._on_transcribe_only_toggled)
        box.append(self._chk_transcribe_only)

        # Attendees / Location toggles
        opts_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=16)
        self._chk_att = Gtk.CheckButton(label="Include Attendees")
        self._chk_att.set_active(form.get("include_attendees", True))
        opts_row.append(self._chk_att)
        self._chk_loc = Gtk.CheckButton(label="Include Location")
        self._chk_loc.set_active(form.get("include_location", True))
        opts_row.append(self._chk_loc)
        box.append(opts_row)

        # Sections
        box.append(Gtk.Label(label="Output Sections", xalign=0))
        selected = set(form.get("sections", ["summary", "action_items"]))
        self._section_checks = {}
        grid = Gtk.Grid()
        grid.set_column_spacing(16)
        grid.set_row_spacing(4)
        for i, (key, label) in enumerate(SECTION_CHOICES):
            chk = Gtk.CheckButton(label=label)
            chk.set_active(key in selected)
            chk.connect("toggled", self._on_form_changed)
            self._section_checks[key] = chk
            grid.attach(chk, i % 2, i // 2, 1, 1)
        box.append(grid)

        # Advanced toggle
        self._chk_advanced = Gtk.CheckButton(label="Advanced (edit raw prompts)")
        self._chk_advanced.set_active(self._advanced)
        self._chk_advanced.connect("toggled", self._on_advanced_toggled)
        box.append(self._chk_advanced)

        # Advanced section (hidden by default)
        self._advanced_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._advanced_box.set_visible(self._advanced)

        self._advanced_box.append(Gtk.Label(label="System Prompt", xalign=0))
        self._txt_system = Gtk.TextView()
        self._txt_system.set_wrap_mode(Gtk.WrapMode.WORD)
        self._txt_system.get_buffer().set_text(form.get("system", ""))
        sys_scroll = Gtk.ScrolledWindow()
        sys_scroll.set_min_content_height(100)
        sys_scroll.set_child(self._txt_system)
        self._advanced_box.append(sys_scroll)

        self._advanced_box.append(Gtk.Label(label="User Prompt Template  (use {transcript} and {date})", xalign=0))
        self._txt_prompt = Gtk.TextView()
        self._txt_prompt.set_wrap_mode(Gtk.WrapMode.WORD)
        self._txt_prompt.get_buffer().set_text(form.get("prompt", ""))
        prompt_scroll = Gtk.ScrolledWindow()
        prompt_scroll.set_min_content_height(180)
        prompt_scroll.set_child(self._txt_prompt)
        self._advanced_box.append(prompt_scroll)

        box.append(self._advanced_box)

        # Connect form change signals to update advanced view
        self._ent_name.connect("changed", self._on_form_changed)
        self._ent_focus.connect("changed", self._on_form_changed)
        self._chk_att.connect("toggled", self._on_form_changed)
        self._chk_loc.connect("toggled", self._on_form_changed)

    def _on_transcribe_only_toggled(self, chk):
        """When transcribe-only is checked, grey out AI-related fields."""
        is_only = chk.get_active()
        self._chk_att.set_sensitive(not is_only)
        self._chk_loc.set_sensitive(not is_only)
        for c in self._section_checks.values():
            c.set_sensitive(not is_only)
        self._chk_advanced.set_sensitive(not is_only)

    def _on_advanced_toggled(self, chk):
        self._advanced = chk.get_active()
        self._advanced_box.set_visible(self._advanced)
        if self._advanced:
            # Sync form → advanced on first open
            self._sync_form_to_advanced()

    def _on_form_changed(self, widget):
        if self._advanced:
            self._sync_form_to_advanced()

    def _sync_form_to_advanced(self):
        form = self._read_form()
        system, prompt = form_to_prompts(form)
        # Only update if advanced hasn't been manually edited yet
        # (check if current content matches what form_to_prompts would generate)
        cur_sys = self._txt_system.get_buffer().get_text(
            self._txt_system.get_buffer().get_start_iter(),
            self._txt_system.get_buffer().get_end_iter(), False)
        cur_prompt = self._txt_prompt.get_buffer().get_text(
            self._txt_prompt.get_buffer().get_start_iter(),
            self._txt_prompt.get_buffer().get_end_iter(), False)
        # Update only if empty or matches generated (don't overwrite manual edits)
        if not cur_sys.strip():
            self._txt_system.get_buffer().set_text(system)
        if not cur_prompt.strip():
            self._txt_prompt.get_buffer().set_text(prompt)

    def _read_form(self) -> dict:
        return {
            "name":              self._ent_name.get_text().strip(),
            "focus":             self._ent_focus.get_text().strip(),
            "transcribe_only":   self._chk_transcribe_only.get_active(),
            "include_attendees": self._chk_att.get_active(),
            "include_location":  self._chk_loc.get_active(),
            "sections":          [k for k, _ in SECTION_CHOICES if self._section_checks[k].get_active()],
        }

    def _on_save_clicked(self, btn):
        form = self._read_form()
        if not form["name"]:
            return   # silently block empty name

        if self._advanced:
            system = self._txt_system.get_buffer().get_text(
                self._txt_system.get_buffer().get_start_iter(),
                self._txt_system.get_buffer().get_end_iter(), False).strip()
            prompt = self._txt_prompt.get_buffer().get_text(
                self._txt_prompt.get_buffer().get_start_iter(),
                self._txt_prompt.get_buffer().get_end_iter(), False).strip()
        else:
            system, prompt = form_to_prompts(form)

        profile = {
            "id":      self._editing.get("id") or f"custom_{form['name'].lower().replace(' ','_')}",
            "name":    form["name"],
            "builtin": False,
            **form,
            "system":  system,
            "prompt":  prompt,
        }

        if self._on_save:
            self._on_save(profile)
        self.destroy()


class ProfileManagerWindow(Gtk.Window):
    """List all profiles; add, edit, duplicate, delete custom ones."""

    def __init__(self, parent, profiles: list[dict]):
        super().__init__(title="Manage Profiles")
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(480, 400)
        self._profiles = list(profiles)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)

        self._list_box = Gtk.ListBox()
        self._list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self._list_box.set_show_separators(True)
        scroll.set_child(self._list_box)
        outer.append(scroll)

        self._populate_list()

        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_margin_top(10)
        btn_row.set_margin_bottom(10)
        btn_row.set_margin_start(12)
        btn_row.set_margin_end(12)

        new_btn = Gtk.Button(label="+ New Profile")
        new_btn.add_css_class("suggested-action")
        new_btn.connect("clicked", self._on_new)
        btn_row.append(new_btn)

        spacer = Gtk.Box(); spacer.set_hexpand(True)
        btn_row.append(spacer)

        close_btn = Gtk.Button(label="Close")
        close_btn.connect("clicked", lambda b: self.destroy())
        btn_row.append(close_btn)

        outer.append(btn_row)
        self.set_child(outer)

    def _populate_list(self):
        child = self._list_box.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._list_box.remove(child)
            child = nxt

        for p in self._profiles:
            row  = Gtk.ListBoxRow()
            hbox = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            hbox.set_margin_top(8)
            hbox.set_margin_bottom(8)
            hbox.set_margin_start(12)
            hbox.set_margin_end(8)

            label = Gtk.Label(xalign=0)
            tag   = " 🔒" if p.get("builtin") else ""
            label.set_markup(f"<b>{p['name']}</b>{tag}")
            label.set_hexpand(True)
            hbox.append(label)

            # Duplicate button (all profiles)
            dup = Gtk.Button(label="Duplicate")
            dup.connect("clicked", self._on_duplicate, p)
            hbox.append(dup)

            if not p.get("builtin"):
                edit_btn = Gtk.Button(label="Edit")
                edit_btn.connect("clicked", self._on_edit, p)
                hbox.append(edit_btn)

                del_btn = Gtk.Button(label="Delete")
                del_btn.add_css_class("destructive-action")
                del_btn.connect("clicked", self._on_delete, p)
                hbox.append(del_btn)

            row.set_child(hbox)
            row._profile = p
            self._list_box.append(row)

    def _save_and_refresh(self):
        # Write to disk first so the JSON is always current.
        save_profiles(self._profiles)
        self._populate_list()
        # Immediately reload the main window dropdown so changes are
        # visible without needing to close the manager first.
        parent = self.get_transient_for()
        if parent and hasattr(parent, "_reload_profiles"):
            GLib.idle_add(parent._reload_profiles)

    def _on_new(self, btn):
        ProfileEditorWindow(self, on_save=self._add_profile).present()

    def _on_edit(self, btn, profile):
        ProfileEditorWindow(self, profile=profile, on_save=lambda p: self._update_profile(p)).present()

    def _on_duplicate(self, btn, profile):
        import copy, time
        dup = copy.deepcopy(profile)
        dup["id"]      = f"custom_{int(time.time())}"
        dup["name"]    = f"{profile['name']} (copy)"
        dup["builtin"] = False
        self._profiles.append(dup)
        self._save_and_refresh()

    def _on_delete(self, btn, profile):
        dialog = InfoDialog(
            self, "Delete Profile",
            f"Delete \"{profile['name']}\"?\n\nThis cannot be undone.",
            [("Cancel", Gtk.ResponseType.CANCEL), ("Delete", Gtk.ResponseType.OK)],
        )
        def on_response(dlg, response):
            dlg.destroy()
            if response == Gtk.ResponseType.OK:
                self._profiles = [p for p in self._profiles if p["id"] != profile["id"]]
                self._save_and_refresh()
        dialog.connect_response(on_response)
        dialog.present()

    def _add_profile(self, profile):
        self._profiles.append(profile)
        self._save_and_refresh()

    def _update_profile(self, profile):
        for i, p in enumerate(self._profiles):
            if p["id"] == profile["id"]:
                self._profiles[i] = profile
                break
        self._save_and_refresh()


# NotebookPicker
class NotebookPicker(Gtk.Window):
    def __init__(self, parent, tree: list[dict]):
        super().__init__(title="Select Notebook")
        self.set_transient_for(parent)
        self.set_modal(True)
        self.set_default_size(360, 500)
        self.selected_id  = None
        self._response_cb = None

        outer  = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        scroll = Gtk.ScrolledWindow()
        scroll.set_vexpand(True)
        scroll.set_margin_top(8)
        scroll.set_margin_bottom(4)
        scroll.set_margin_start(8)
        scroll.set_margin_end(8)

        list_box = Gtk.ListBox()
        list_box.set_selection_mode(Gtk.SelectionMode.SINGLE)
        list_box.set_show_separators(True)

        for nb in tree:
            row    = Gtk.ListBoxRow()
            indent = "  " * nb["depth"]
            prefix = "\u21b3 " if nb["depth"] > 0 else ""
            label  = Gtk.Label(label=f"{indent}{prefix}{nb['title']}", xalign=0)
            label.set_margin_top(6)
            label.set_margin_bottom(6)
            label.set_margin_start(8)
            row.set_child(label)
            row._notebook_id = nb["id"]
            list_box.append(row)

        # row-activated fires on double-click or Enter — just select, don't submit.
        list_box.connect("row-activated", self._on_row_selected)
        self._list_box = list_box
        scroll.set_child(list_box)
        outer.append(scroll)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_halign(Gtk.Align.END)
        btn_row.set_margin_top(10)
        btn_row.set_margin_bottom(10)
        btn_row.set_margin_end(12)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda b: self._emit(Gtk.ResponseType.CANCEL))
        btn_row.append(cancel)

        select = Gtk.Button(label="Select")
        select.add_css_class("suggested-action")
        select.connect("clicked", lambda b: self._emit(Gtk.ResponseType.OK))
        btn_row.append(select)

        outer.append(btn_row)
        self.set_child(outer)

    def connect_response(self, cb):
        self._response_cb = cb

    def _emit(self, response_id):
        if self._response_cb:
            self._response_cb(self, response_id)

    def _on_row_selected(self, lb, row):
        # Single-click or Enter just highlights the row; does not submit.
        self.selected_id = row._notebook_id

    def get_selected_id(self) -> str | None:
        row = self._list_box.get_selected_row()
        return row._notebook_id if row else self.selected_id

# Main window
class MeetingRecorderWindow(Gtk.ApplicationWindow):

    def __init__(self, app):
        super().__init__(application=app, title=f"Meeting Recorder  v{APP_VERSION}")
        self.set_default_size(360, 280)
        self.set_resizable(False)
        self._setup_css()
        self._build_ui()
        self._set_status("idle")

        # Start persistent worker thread that drains the job queue.
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

        # Start idle GPU polling so stats show from the moment the window opens.
        if NVML_AVAILABLE:
            self._start_idle_gpu_polling()

    def _worker_loop(self):
        """
        Persistent background thread that drains state.job_queue.
        Each job is a (audio_path, profile) tuple.
        """
        while True:
            job = state.job_queue.get()   # blocks until a job arrives
            audio_path, profile = job if isinstance(job, tuple) else (job, None)
            try:
                self._process(audio_path, profile=profile)
            except Exception as e:
                log.error("Worker loop unhandled exception: %s", e)
            finally:
                state.job_queue.task_done()
                remaining = state.job_queue.qsize()
                if remaining > 0:
                    GLib.idle_add(
                        self._set_status, "processing",
                        f"Transcribing...  [{remaining} queued]"
                    )

    def _setup_css(self):
        css = b"""
        .status-bar { border-radius: 4px; padding: 6px 12px; font-size: 0.85em; font-weight: bold; }
        .timer-label { font-size: 1.4em; font-weight: bold; font-family: monospace; color: #ff6b6b; }
        .gpu-label { font-size: 0.78em; font-family: monospace; color: #aaaaaa; }
        .btn-start { background-color: #48c774; color: #1a1a1a; font-weight: bold; border-radius: 6px; }
        .btn-stop { background-color: #dc3545; color: white; font-weight: bold; border-radius: 6px; }
        .btn-start:disabled, .btn-stop:disabled { opacity: 0.4; }
        .btn-resume { background-color: #4a90d9; color: white; font-weight: bold; border-radius: 6px; }
        .btn-resume:disabled { opacity: 0.4; }
        .btn-import { background-color: #7b5ea7; color: white; font-weight: bold; border-radius: 6px; }
        .btn-import:disabled { opacity: 0.4; }
        .btn-clear-log { background-color: #555555; color: #cccccc; border-radius: 6px; font-size: 0.8em; }
        .btn-clear-log:disabled { opacity: 0.4; }
        .btn-clear-log-warn { background-color: #8b0000; color: #ff9999; border-radius: 6px; font-size: 0.8em; font-weight: bold; }
        .btn-clear-log-warn:disabled { opacity: 0.4; }
        .version-label { font-size: 0.7em; color: #555555; }
        """
        provider = Gtk.CssProvider()
        provider.load_from_data(css)
        Gtk.StyleContext.add_provider_for_display(self.get_display(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

    def _build_ui(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(12)
        box.set_margin_bottom(12)
        box.set_margin_start(12)
        box.set_margin_end(12)

        self._status_label = Gtk.Label(label="Idle")
        self._status_label.add_css_class("status-bar")
        self._status_label.set_hexpand(True)
        box.append(self._status_label)

        self._timer_label = Gtk.Label(label="")
        self._timer_label.add_css_class("timer-label")
        self._timer_label.set_hexpand(True)
        box.append(self._timer_label)

        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_hexpand(True)
        self._progress_bar.set_show_text(True)
        self._progress_bar.set_visible(False)
        box.append(self._progress_bar)

        self._gpu_label = Gtk.Label(label="")
        self._gpu_label.add_css_class("gpu-label")
        self._gpu_label.set_hexpand(True)
        self._gpu_label.set_visible(NVML_AVAILABLE)
        box.append(self._gpu_label)

        # Profile selector row
        profile_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)

        profile_label = Gtk.Label(label="Profile:")
        profile_label.set_xalign(0)
        profile_row.append(profile_label)

        self._profiles      = load_profiles()
        self._profile_names = [p["name"] for p in self._profiles]

        self._profile_combo = Gtk.DropDown.new_from_strings(self._profile_names)
        self._profile_combo.set_hexpand(True)
        self._profile_combo.set_selected(0)
        profile_row.append(self._profile_combo)

        btn_manage = Gtk.Button(label="\u2699")
        btn_manage.set_tooltip_text("Manage profiles")
        btn_manage.connect("clicked", self._on_manage_profiles)
        profile_row.append(btn_manage)

        box.append(profile_row)

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_homogeneous(True)

        self._btn_start = Gtk.Button(label="\u23fa  Start")
        self._btn_start.add_css_class("btn-start")
        self._btn_start.connect("clicked", self._on_start)

        self._btn_stop = Gtk.Button(label="\u23f9  Stop")
        self._btn_stop.add_css_class("btn-stop")
        self._btn_stop.connect("clicked", self._on_stop)
        self._btn_stop.set_sensitive(False)

        btn_row.append(self._btn_start)
        btn_row.append(self._btn_stop)
        box.append(btn_row)


        # Import + Resume row
        aux_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        aux_row.set_homogeneous(False)

        self._btn_import = Gtk.Button(label="📂  Import Audio")
        self._btn_import.add_css_class("btn-import")
        self._btn_import.connect("clicked", self._on_import)
        self._btn_import.set_tooltip_text("Import an existing audio file for transcription and summarization.")
        self._btn_import.set_hexpand(True)
        aux_row.append(self._btn_import)

        self._btn_resume = Gtk.Button(label="⏮  Resume Pending")
        self._btn_resume.add_css_class("btn-resume")
        self._btn_resume.connect("clicked", self._on_resume)
        self._btn_resume.set_tooltip_text("Resume a previously saved recording that failed due to GPU OOM.")
        self._btn_resume.set_hexpand(True)
        aux_row.append(self._btn_resume)

        box.append(aux_row)

        # Clear log button — must be created before _refresh_resume_button() is called
        self._btn_clear_log = Gtk.Button(label="\U0001f5d1  Clear Log")
        self._btn_clear_log.add_css_class("btn-clear-log")
        self._btn_clear_log.connect("clicked", self._on_clear_log)
        self._btn_clear_log.set_tooltip_text(f"Clear the log file at {LOG_FILE}")
        box.append(self._btn_clear_log)

        self._refresh_resume_button()

        # Version label
        ver_label = Gtk.Label(label=f"v{APP_VERSION}")
        ver_label.add_css_class("version-label")
        ver_label.set_halign(Gtk.Align.END)
        box.append(ver_label)

        self.set_child(box)

        self._timer_start  = None
        self._timer_source = None
        self._gpu_source   = None

    # Timer
    def _start_timer(self):
        import time
        self._timer_start = time.monotonic()
        def tick():
            import time
            if self._timer_start is None:
                return False
            elapsed = int(time.monotonic() - self._timer_start)
            h, rem  = divmod(elapsed, 3600)
            m, s    = divmod(rem, 60)
            self._timer_label.set_label(f"{h:02d}:{m:02d}:{s:02d}")
            return True
        self._timer_source = GLib.timeout_add(1000, tick)

    def _stop_timer(self):
        if self._timer_source is not None:
            GLib.source_remove(self._timer_source)
            self._timer_source = None
        self._timer_start = None
        self._timer_label.set_label("")

    # GPU polling
    def _start_gpu_polling(self):
        """Start 1-second fast polling during transcription. Cancels idle poll first."""
        if not NVML_AVAILABLE:
            return
        # Cancel any existing idle poll before starting fast poll.
        if self._gpu_source is not None:
            GLib.source_remove(self._gpu_source)
            self._gpu_source = None

        def poll():
            stats = get_gpu_stats()
            if stats:
                self._gpu_label.set_label(stats)
            return True

        self._gpu_source = GLib.timeout_add(1000, poll)
        poll()

    def _stop_gpu_polling(self):
        """Stop intensive polling. GPU label stays visible with last reading."""
        if self._gpu_source is not None:
            GLib.source_remove(self._gpu_source)
            self._gpu_source = None
        # Don't hide — label stays visible at idle with a slower poll.
        self._start_idle_gpu_polling()

    def _start_idle_gpu_polling(self):
        """Slow 5-second poll when idle so GPU stats stay current without hammering pynvml."""
        if not NVML_AVAILABLE or self._gpu_source is not None:
            return

        def poll():
            stats = get_gpu_stats()
            if stats:
                self._gpu_label.set_label(stats)
            return True

        self._gpu_source = GLib.timeout_add(5000, poll)
        poll()   # immediate reading

    def _start_pulse(self):
        """Pulse the progress bar while waiting on a long operation."""
        self._progress_bar.set_visible(True)
        self._progress_bar.set_text("Processing...")
        self._pulse_source = GLib.timeout_add(120, self._do_pulse)

    def _do_pulse(self):
        self._progress_bar.pulse()
        return True

    def _stop_pulse(self):
        if hasattr(self, "_pulse_source") and self._pulse_source:
            GLib.source_remove(self._pulse_source)
            self._pulse_source = None

    # Progress bar
    def _show_progress(self, visible: bool):
        self._progress_bar.set_visible(visible)
        if not visible:
            self._progress_bar.set_fraction(0.0)
            self._progress_bar.set_text("")

    def _update_progress(self, fraction: float, seg_end: float, duration: float):
        self._progress_bar.set_fraction(fraction)
        remaining  = max(0.0, duration - seg_end)
        mins, secs = divmod(int(remaining), 60)
        self._progress_bar.set_text(f"{int(fraction * 100)}%  (~{mins}m {secs:02d}s remaining)")

    # Status
    def _refresh_resume_button(self):
        """Show resume button only when pending recordings exist."""
        pending = list_pending()
        has_pending = len(pending) > 0
        self._btn_resume.set_visible(has_pending)
        if has_pending:
            n = len(pending)
            self._btn_resume.set_label(f"\u23ee  Resume Pending ({n})")
        self._refresh_log_button()

    def _refresh_log_button(self):
        """Update the Clear Log button — red text if log exceeds LOG_SIZE_WARN_MB."""
        try:
            size_mb = LOG_FILE.stat().st_size / 1024 / 1024 if LOG_FILE.exists() else 0
        except Exception:
            size_mb = 0

        # Swap CSS class based on size threshold.
        if size_mb >= LOG_SIZE_WARN_MB:
            self._btn_clear_log.remove_css_class("btn-clear-log")
            self._btn_clear_log.add_css_class("btn-clear-log-warn")
            self._btn_clear_log.set_label(f"\U0001f5d1  Clear Log ({size_mb:.1f} MB)")
            self._btn_clear_log.set_tooltip_text(
                f"Log file is {size_mb:.1f} MB — consider clearing it.\n{LOG_FILE}"
            )
        else:
            self._btn_clear_log.remove_css_class("btn-clear-log-warn")
            self._btn_clear_log.add_css_class("btn-clear-log")
            label = f"\U0001f5d1  Clear Log"
            if size_mb > 0:
                label += f" ({size_mb:.1f} MB)"
            self._btn_clear_log.set_label(label)
            self._btn_clear_log.set_tooltip_text(f"Clear the log file at {LOG_FILE}")

    def _set_status(self, status: str, detail: str = ""):
        labels = {"idle": "\u25cf Idle", "recording": "\u25cf Recording...", "processing": "\u25cf Processing..."}
        text   = labels.get(status, status)
        if detail:
            text += f"  {detail}"

        # Show queue depth when processing so user knows jobs are lined up.
        queued = state.job_queue.qsize()
        if queued > 0 and status in ("processing", "recording"):
            text += f"  [{queued} queued]"

        self._status_label.set_label(text)

        # Always check actual recording state — not just the status string.
        # This prevents the Stop button being disabled when a second recording
        # starts while the first job is still processing in the background.
        with state.lock:
            actually_recording = state.recording

        self._btn_start.set_sensitive(not actually_recording)
        self._btn_stop.set_sensitive(actually_recording)

        # Idle buttons: available when not recording AND not processing.
        # Allow import/resume/clear-log if we are idle even if the queue
        # has items (worker handles them independently).
        is_idle = (status == "idle") and not actually_recording
        self._btn_import.set_sensitive(not actually_recording)
        self._btn_resume.set_sensitive(is_idle)
        self._btn_clear_log.set_sensitive(is_idle)
        if is_idle:
            self._refresh_resume_button()

    # Handlers
    def _on_clear_log(self, btn):
        """Confirm then truncate the log file."""
        dialog = InfoDialog(
            self,
            "Clear Log File",
            f"This will delete all contents of the log file.\n\n{LOG_FILE}\n\n"
            "This cannot be undone.",
            [
                ("Cancel", Gtk.ResponseType.CANCEL),
                ("Clear",  Gtk.ResponseType.OK),
            ],
        )

        def on_response(dlg, response):
            dlg.destroy()
            if response == Gtk.ResponseType.OK:
                try:
                    LOG_FILE.write_text("", encoding="utf-8")
                    log.info("Log file cleared by user.")
                    notify("Meeting Recorder", "Log file cleared.")
                    self._refresh_log_button()
                except Exception as e:
                    notify("Meeting Recorder", f"Could not clear log: {e}")

        dialog.connect_response(on_response)
        dialog.present()

    def _on_start(self, btn):
        ok = start_recording()
        if ok:
            GLib.idle_add(self._set_status, "recording")
            self._start_timer()
            notify("Meeting Recorder", "Recording started.")

    def _get_selected_profile(self) -> dict:
        """Return the currently selected profile dict."""
        idx = self._profile_combo.get_selected()
        if 0 <= idx < len(self._profiles):
            return self._profiles[idx]
        return self._profiles[0]

    def _reload_profiles(self):
        """
        Reload profiles from disk and rebuild the dropdown widget.
        GTK4's DropDown does not reliably re-render on model swap,
        so we replace the widget entirely.
        """
        self._profiles      = load_profiles()
        self._profile_names = [p["name"] for p in self._profiles]

        # Find the profile row box (parent of the combo).
        old_combo = self._profile_combo
        parent    = old_combo.get_parent()

        # Build a fresh DropDown.
        new_combo = Gtk.DropDown.new_from_strings(self._profile_names)
        new_combo.set_hexpand(True)
        new_combo.set_selected(0)

        # Swap in the new widget at the same position.
        if parent:
            # Insert new combo before the gear button (second child).
            parent.remove(old_combo)
            # Re-insert at position 1 (after the "Profile:" label).
            first = parent.get_first_child()   # the label
            if first:
                parent.insert_child_after(new_combo, first)
            else:
                parent.append(new_combo)

        self._profile_combo = new_combo

    def _on_manage_profiles(self, btn):
        editor = ProfileManagerWindow(self, self._profiles)
        def on_close(w):
            # Use idle_add so the destroy signal fully completes before
            # we reload — ensures the JSON write has flushed.
            GLib.idle_add(self._reload_profiles)
        editor.connect("destroy", on_close)
        editor.present()

    def _on_stop(self, btn):
        with state.lock:
            if not state.recording:
                return
        self._stop_timer()
        profile    = self._get_selected_profile()
        audio_path = stop_recording()
        if audio_path:
            queued = state.job_queue.qsize()
            state.job_queue.put((audio_path, profile))
            if queued > 0:
                notify(
                    "Meeting Recorder",
                    f"Recording queued (position {queued + 1}). You can start the next meeting."
                )
                GLib.idle_add(self._set_status, "recording" if state.recording else "processing", "Queued")
            else:
                GLib.idle_add(self._set_status, "processing", "Transcribing...")

    # Processing pipeline
    def _process(self, audio_path: str, profile: dict | None = None):
        transcript_cache = None   # defined here so finally block can always reference it
        try:
            queued = state.job_queue.qsize()
            label  = f"Transcribing...  [{queued} queued]" if queued > 0 else "Transcribing..."
            GLib.idle_add(self._show_progress, True)
            GLib.idle_add(self._start_gpu_polling)
            GLib.idle_add(self._set_status, "processing", label)
            notify("Meeting Recorder", "Transcribing audio...")

            # Update sidecar: recording is now in transcription.
            _update_sidecar_status(audio_path, "transcribing")

            def on_progress(fraction, seg_end, duration):
                GLib.idle_add(self._update_progress, fraction, seg_end, duration)

            transcript = transcribe(audio_path, progress_cb=on_progress)

            GLib.idle_add(self._stop_pulse)
            GLib.idle_add(self._show_progress, False)
            GLib.idle_add(self._stop_gpu_polling)

            if not transcript.strip():
                notify("Meeting Recorder", "Transcript was empty. Check audio routing.")
                GLib.idle_add(self._set_status, "idle")
                return

            # Persist transcript to a temp file immediately after Whisper
            # completes. If diarization or summarization crashes the process,
            # the transcript is recoverable from the pending directory.
            transcript_cache = Path(audio_path).with_suffix(".transcript.txt")
            try:
                transcript_cache.write_text(transcript, encoding="utf-8")
                log.info("Transcript cached to %s", transcript_cache)
            except Exception as e:
                log.warning("Could not cache transcript: %s", e)
                transcript_cache = None

            # Check for transcribe-only profile — skip summarization entirely.
            if profile and profile.get("transcribe_only"):
                log.info("Transcribe-only profile — skipping summarization.")
                GLib.idle_add(self._set_status, "processing", "Filing transcript...")
                note_body = _format_transcript_note(audio_path, transcript)
                GLib.idle_add(self._pick_notebook_and_save, note_body)
                return

            # Update sidecar: transcription done, moving to summarization.
            _update_sidecar_status(audio_path, "summarizing", str(transcript_cache) if transcript_cache else "")

            GLib.idle_add(self._set_status, "processing", "Summarizing...")
            notify("Meeting Recorder", "Summarizing with Claude...")

            import time
            auto_retry_count = 0
            MAX_AUTO_RETRIES = 3

            while True:
                try:
                    summary = summarize(transcript, profile=profile)
                    break

                except Exception as e:
                    err     = str(e).lower()
                    err_str = str(e)

                    # ── Insufficient funds ────────────────────────────────────
                    if any(k in err for k in ("credit balance", "too low", "billing", "payment", "402")):
                        retry_event = threading.Event()
                        GLib.idle_add(self._show_funds_dialog, retry_event, audio_path)
                        retry_event.wait()
                        if getattr(retry_event, "cancelled", False):
                            GLib.idle_add(self._set_status, "idle")
                            return
                        GLib.idle_add(self._set_status, "processing", "Retrying...")
                        auto_retry_count = 0
                        continue

                    # ── Transient server error (500/502/503/529) ──────────────
                    # These are Anthropic-side blips. Retry with backoff up to
                    # MAX_AUTO_RETRIES times before asking the user what to do.
                    is_server_error = any(k in err for k in (
                        "500", "502", "503", "529",
                        "internal server error", "overloaded",
                        "service unavailable", "bad gateway",
                    ))

                    if is_server_error and auto_retry_count < MAX_AUTO_RETRIES:
                        auto_retry_count += 1
                        wait = 10 * auto_retry_count   # 10s, 20s, 30s
                        log.warning(
                            "Anthropic server error (attempt %d/%d), retrying in %ds: %s",
                            auto_retry_count, MAX_AUTO_RETRIES, wait, err_str
                        )
                        GLib.idle_add(
                            self._set_status, "processing",
                            f"API error, retrying in {wait}s... ({auto_retry_count}/{MAX_AUTO_RETRIES})"
                        )
                        time.sleep(wait)
                        continue

                    # ── Persistent server error — ask user ────────────────────
                    if is_server_error:
                        retry_event = threading.Event()
                        GLib.idle_add(
                            self._show_api_error_dialog, retry_event,
                            audio_path, transcript_cache, err_str
                        )
                        retry_event.wait()
                        if getattr(retry_event, "cancelled", False):
                            GLib.idle_add(self._set_status, "idle")
                            return
                        auto_retry_count = 0
                        GLib.idle_add(self._set_status, "processing", "Retrying...")
                        continue

                    # ── Any other error — re-raise to outer handler ────────────
                    raise

            GLib.idle_add(self._pick_notebook_and_save, summary)

        except Exception as e:
            GLib.idle_add(self._stop_pulse)
            GLib.idle_add(self._show_progress, False)
            GLib.idle_add(self._stop_gpu_polling)

            # Always log the full traceback to the log file.
            log.error("Processing failed:\n%s", traceback.format_exc())

            if is_cuda_oom(e):
                try:
                    # Pass transcript if we have it so summarization can
                    # resume without re-running Whisper.
                    cached_transcript = ""
                    if transcript_cache and transcript_cache.exists():
                        cached_transcript = transcript_cache.read_text(encoding="utf-8")
                        transcript_cache.unlink(missing_ok=True)
                    save_pending(audio_path, transcript=cached_transcript)
                    GLib.idle_add(self._set_status, "idle")
                    GLib.idle_add(self._show_oom_dialog)
                    return
                except Exception as save_err:
                    log.error("save_pending failed: %s", save_err)
                    GLib.idle_add(
                        self._show_error_dialog,
                        "Save Failed",
                        f"GPU out of memory and recording could not be saved.\n\n"
                        f"See log for details:\n{LOG_FILE}",
                    )
            else:
                # Save transcript to pending before showing the error so
                # nothing is lost — the user can resume from the Resume button.
                try:
                    cached = ""
                    if transcript_cache and transcript_cache.exists():
                        cached = transcript_cache.read_text(encoding="utf-8")
                        transcript_cache.unlink(missing_ok=True)
                    if cached:
                        save_pending(audio_path, transcript=cached)
                        GLib.idle_add(self._refresh_resume_button)
                        log.info("Transcript saved to pending after error.")
                except Exception as save_err:
                    log.error("Could not save pending after error: %s", save_err)

                short = str(e).split("\n")[0][:200]
                GLib.idle_add(
                    self._show_error_dialog,
                    "Processing Error",
                    f"{short}\n\n"
                    "Your transcript has been saved to the pending queue.\n"
                    "Use the Resume button when ready.\n\n"
                    f"Full details logged to:\n{LOG_FILE}",
                )

            GLib.idle_add(self._set_status, "idle")
        finally:
            with state.lock:
                state.processing = False
            # Clean up all files for this job.
            # The WAV and sidecar now live in the pending dir — delete them
            # on success. On error paths, save_pending keeps them intentionally.
            try:
                wav = Path(audio_path)
                sidecar = wav.with_suffix(".json")
                # Only delete if sidecar is NOT in a failed/pending state —
                # error handlers set status to "failed" before we get here.
                if sidecar.exists():
                    data = json.loads(sidecar.read_text())
                    if data.get("status") not in ("failed",):
                        wav.unlink(missing_ok=True)
                        sidecar.unlink(missing_ok=True)
                else:
                    wav.unlink(missing_ok=True)
            except Exception:
                pass
            try:
                if transcript_cache and transcript_cache.exists():
                    transcript_cache.unlink(missing_ok=True)
            except Exception:
                pass
            # Only return to idle if no more jobs are waiting.
            if state.job_queue.empty():
                GLib.idle_add(self._set_status, "idle")

    # API server error dialog (500/502/503 after retries exhausted)
    def _show_api_error_dialog(
        self,
        retry_event: threading.Event,
        audio_path: str,
        transcript_cache,
        error: str,
    ):
        GLib.idle_add(self._set_status, "processing", "API error")
        notify("Meeting Recorder", "Anthropic API error. Transcript saved — you can retry later.")

        dialog = InfoDialog(
            self,
            "Anthropic API Error",
            f"The Anthropic API returned a server error after 3 retry attempts.\n\n"
            f"Error: {error[:120]}\n\n"
            "Your transcript has been saved. Options:\n"
            "  Retry - try the API call again now.\n"
            "  Save & Quit - save transcript to pending queue\n"
            "  and resume later from the Resume button.",
            [
                ("Save & Quit", Gtk.ResponseType.CANCEL),
                ("Retry",       Gtk.ResponseType.OK),
            ],
        )

        def on_response(dlg, response):
            dlg.destroy()
            if response == Gtk.ResponseType.OK:
                retry_event.set()
            else:
                # Save transcript to pending so it can be resumed later.
                retry_event.cancelled = True
                retry_event.set()
                try:
                    cached = ""
                    if transcript_cache and Path(transcript_cache).exists():
                        cached = Path(transcript_cache).read_text(encoding="utf-8")
                        Path(transcript_cache).unlink(missing_ok=True)
                    save_pending(audio_path, transcript=cached)
                    notify("Meeting Recorder", "Transcript saved. Use Resume when ready.")
                    GLib.idle_add(self._refresh_resume_button)
                except Exception as ex:
                    log.error("save_pending after API error: %s", ex)
                    notify("Meeting Recorder", "Could not save pending. Check log.")

        dialog.connect_response(on_response)
        dialog.present()

    # Funds dialog
    def _show_funds_dialog(self, retry_event: threading.Event, audio_path: str):
        GLib.idle_add(self._set_status, "processing", "Insufficient funds")
        notify("Meeting Recorder", "Anthropic API: insufficient credits. Add funds then retry.")
        dialog = InfoDialog(
            self, "Anthropic API: Insufficient Credits",
            "Your Anthropic API credit balance is too low to complete the summary.\n\n"
            "Your recording transcript is safe - nothing has been lost.\n\n"
            "Add credits at console.anthropic.com/settings/billing,\n"
            "then click Retry to continue.",
            [("Open Billing Page", Gtk.ResponseType.HELP), ("Cancel", Gtk.ResponseType.CANCEL), ("Retry", Gtk.ResponseType.OK)],
        )
        def on_response(dlg, response):
            if response == Gtk.ResponseType.HELP:
                try:
                    subprocess.Popen(["xdg-open", "https://console.anthropic.com/settings/billing"])
                except FileNotFoundError:
                    pass
                return
            dlg.destroy()
            if response == Gtk.ResponseType.OK:
                retry_event.set()
            else:
                retry_event.cancelled = True
                retry_event.set()
                notify("Meeting Recorder", "Summarization cancelled.")
        dialog.connect_response(on_response)
        dialog.present()

    # Notebook picker
    def _pick_notebook_and_save(self, summary: str):
        try:
            notebooks = get_notebooks()
        except Exception as e:
            self._show_joplin_mid_error(summary, str(e))
            return

        picker = NotebookPicker(self, build_display_tree(notebooks))

        def on_response(dlg, response):
            notebook_id = dlg.get_selected_id()
            dlg.destroy()
            if response != Gtk.ResponseType.OK or not notebook_id:
                notify("Meeting Recorder", "No notebook selected. Note not saved.")
                self._set_status("idle")
                return
            timestamp  = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
            note_title = f"Meeting Notes - {timestamp}"
            try:
                create_joplin_note(note_title, summary, notebook_id)
                notify("Meeting Recorder", f'Note created: "{note_title}"')
            except Exception as e:
                notify("Joplin Error", str(e))
            finally:
                self._set_status("idle")

        picker.connect_response(on_response)
        picker.present()

    # Mid-session Joplin error
    def _show_joplin_mid_error(self, summary: str, error: str):
        notify("Joplin Error", "Cannot reach Joplin. Open Joplin and click Retry.")
        dialog = InfoDialog(
            self, "Cannot Connect to Joplin",
            f"The meeting summary was created but Joplin is not reachable.\n\n"
            f"Error: {error}\n\n"
            "Your summary has NOT been lost.\n\n"
            "  1. Open Joplin.\n"
            "  2. Confirm Web Clipper is enabled (Tools -> Options -> Web Clipper).\n"
            "  3. Click Retry to save the note.",
            [("Discard Note", Gtk.ResponseType.CANCEL), ("Retry", Gtk.ResponseType.OK)],
        )
        def on_response(dlg, response):
            dlg.destroy()
            if response == Gtk.ResponseType.OK:
                if joplin_is_reachable():
                    self._pick_notebook_and_save(summary)
                else:
                    self._show_joplin_mid_error(summary, "Still unreachable.")
            else:
                notify("Meeting Recorder", "Note discarded.")
                self._set_status("idle")
        dialog.connect_response(on_response)
        dialog.present()

    # ── OOM dialog (shown after saving pending) ───────────────────────────────

    def _resume_from_transcript(self, audio_path: str, transcript: str, profile: dict | None = None):
        """
        Resume a pending recording using a saved transcript, skipping Whisper.
        Goes straight to summarization (or diarization if requested and VRAM available).
        """
        try:
            notify("Meeting Recorder", "Resuming from saved transcript...")
            GLib.idle_add(self._set_status, "processing", "Summarizing...")

            while True:
                try:
                    summary = summarize(transcript, profile=profile)
                    break
                except Exception as e:
                    err = str(e).lower()
                    if any(k in err for k in ("credit balance", "too low", "billing", "payment", "402")):
                        retry_event = threading.Event()
                        GLib.idle_add(self._show_funds_dialog, retry_event, audio_path)
                        retry_event.wait()
                        if getattr(retry_event, "cancelled", False):
                            GLib.idle_add(self._set_status, "idle")
                            return
                        GLib.idle_add(self._set_status, "processing", "Retrying...")
                        continue
                    else:
                        raise

            GLib.idle_add(self._pick_notebook_and_save, summary)

        except Exception as e:
            log.error("Resume from transcript failed:\n%s", traceback.format_exc())
            short = str(e).split("\n")[0][:200]
            GLib.idle_add(self._show_error_dialog, "Resume Error",
                          f"{short}\n\nFull details logged to:\n{LOG_FILE}")
            GLib.idle_add(self._set_status, "idle")
        finally:
            with state.lock:
                state.processing = False
            Path(audio_path).unlink(missing_ok=True)

    def _show_error_dialog(self, title: str, message: str):
        """Show a visible error dialog — used instead of silent notify-send."""
        dialog = InfoDialog(
            self, title, message,
            [("OK", Gtk.ResponseType.OK)],
        )
        dialog.connect_response(lambda d, r: d.destroy())
        dialog.present()

    def _show_oom_dialog(self):
        dialog = InfoDialog(
            self,
            "GPU Out of Memory",
            "The GPU ran out of VRAM to run transcription.\n\n"
            "Your recording has been saved and can be resumed once VRAM is free.\n\n"
            "To free VRAM:\n"
            "  - Close other GPU-using apps (Ollama, Open WebUI, etc.)\n"
            "  - Run: nvidia-smi   to see what is using VRAM\n\n"
            "Use the Resume button when ready.",
            [("OK", Gtk.ResponseType.OK)],
        )
        dialog.connect_response(lambda d, r: d.destroy())
        dialog.present()

    # ── Resume button ─────────────────────────────────────────────────────────

    def _on_import(self, btn):
        """Open a file picker then an import confirmation dialog."""
        dialog = Gtk.FileDialog()
        dialog.set_title("Select Audio File")

        # Build a filter for common audio formats Whisper handles.
        audio_filter = Gtk.FileFilter()
        audio_filter.set_name("Audio files")
        for pat in ["*.wav", "*.mp3", "*.m4a", "*.mp4", "*.ogg",
                    "*.flac", "*.aac", "*.wma", "*.webm", "*.mkv"]:
            audio_filter.add_pattern(pat)

        filters = Gio.ListStore.new(Gtk.FileFilter)
        filters.append(audio_filter)

        all_filter = Gtk.FileFilter()
        all_filter.set_name("All files")
        all_filter.add_pattern("*")
        filters.append(all_filter)

        dialog.set_filters(filters)

        def on_open(d, result):
            try:
                file = d.open_finish(result)
                if file:
                    path = file.get_path()
                    self._show_import_dialog(path)
            except Exception:
                pass   # user cancelled

        dialog.open(self, None, on_open)

    def _show_import_dialog(self, file_path: str):
        """Confirm profile selection before queuing the imported file."""
        import os
        filename = os.path.basename(file_path)
        size_mb  = os.path.getsize(file_path) / 1024 / 1024

        # Profile selector
        profiles      = load_profiles()
        profile_names = [p["name"] for p in profiles]

        win = Gtk.Window(title="Import Audio File")
        win.set_transient_for(self)
        win.set_modal(True)
        win.set_default_size(420, -1)

        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        content.set_margin_top(20)
        content.set_margin_bottom(12)
        content.set_margin_start(20)
        content.set_margin_end(20)

        title_lbl = Gtk.Label()
        title_lbl.set_markup("<b>Import Audio File</b>")
        title_lbl.set_xalign(0)
        content.append(title_lbl)

        file_lbl = Gtk.Label(label=f"{filename}  ({size_mb:.1f} MB)")
        file_lbl.set_xalign(0)
        content.append(file_lbl)

        profile_lbl = Gtk.Label(label="Summarization Profile:", xalign=0)
        content.append(profile_lbl)

        combo = Gtk.DropDown.new_from_strings(profile_names)
        # Default to the same profile currently selected in the main window.
        current_idx = self._profile_combo.get_selected()
        combo.set_selected(current_idx)
        content.append(combo)

        outer.append(content)
        outer.append(Gtk.Separator(orientation=Gtk.Orientation.HORIZONTAL))

        btn_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        btn_row.set_margin_top(10)
        btn_row.set_margin_bottom(10)
        btn_row.set_margin_start(12)
        btn_row.set_margin_end(12)

        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda b: win.destroy())
        btn_row.append(cancel)

        spacer = Gtk.Box(); spacer.set_hexpand(True)
        btn_row.append(spacer)

        queue_btn = Gtk.Button(label="Queue for Processing")
        queue_btn.add_css_class("suggested-action")

        def on_queue(b):
            idx     = combo.get_selected()
            profile = profiles[idx] if 0 <= idx < len(profiles) else profiles[0]
            win.destroy()
            self._queue_import(file_path, profile)

        queue_btn.connect("clicked", on_queue)
        btn_row.append(queue_btn)

        outer.append(btn_row)
        win.set_child(outer)
        win.present()

    def _queue_import(self, file_path: str, profile: dict):
        """
        Copy the imported file into the pending directory, write a sidecar,
        and enqueue it for processing exactly like a normal recording.
        The original file is NOT moved or deleted — only a copy goes to pending.
        """
        import shutil
        pd        = pending_dir()
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        wav_dest  = pd / f"import_{timestamp}_{Path(file_path).name}"

        try:
            shutil.copy2(file_path, wav_dest)
            write_sidecar(wav_dest, "transcribing")
            log.info("Imported: %s -> %s", file_path, wav_dest.name)
        except Exception as e:
            notify("Import Error", f"Could not copy file: {e}")
            log.error("Import failed: %s", e)
            return

        queued = state.job_queue.qsize()
        state.job_queue.put((str(wav_dest), profile))

        if queued > 0:
            notify("Meeting Recorder",
                   f"Import queued (position {queued + 1}).")
        else:
            GLib.idle_add(self._set_status, "processing", "Transcribing import...")
            notify("Meeting Recorder", f"Processing: {Path(file_path).name}")

    def _on_resume(self, btn):
        records = list_pending()
        if not records:
            notify("Meeting Recorder", "No pending recordings found.")
            return
        self._show_pending_picker(records)

    def _show_pending_picker(self, records: list[dict]):
        """Show a list of pending recordings; user picks one to resume or discard."""
        picker = PendingPicker(self, records)

        def on_response(dlg, response, record):
            dlg.destroy()
            if response == Gtk.ResponseType.OK and record:
                with state.lock:
                    state.processing = True
                Path(record["sidecar_path"]).unlink(missing_ok=True)

                # Determine resume path:
                # 1. Transcript txt exists → skip straight to summarization.
                # 2. audio_path IS a txt file (orphan, no WAV) → same.
                # 3. Only WAV exists → re-transcribe.
                transcript_path   = record.get("transcript_path", "")
                audio_path        = record["audio_path"]
                is_txt_only       = audio_path.endswith(".txt") and not audio_path.endswith(".transcript.txt")
                has_transcript    = (transcript_path and Path(transcript_path).exists())

                if is_txt_only or has_transcript:
                    # Load transcript from whichever path has it.
                    src = audio_path if is_txt_only else transcript_path
                    saved_transcript = Path(src).read_text(encoding="utf-8")
                    if not is_txt_only:
                        Path(transcript_path).unlink(missing_ok=True)
                    self._set_status("processing", "Resuming from transcript...")
                    t = threading.Thread(
                        target=self._resume_from_transcript,
                        args=(audio_path, saved_transcript, record.get("profile")),
                        daemon=True,
                    )
                else:
                    # No saved transcript — re-transcribe from audio.
                    self._set_status("processing", "Resuming (re-transcribing)...")
                    t = threading.Thread(
                        target=self._process,
                        args=(audio_path,),
                        daemon=True,
                    )
                t.start()
            elif response == Gtk.ResponseType.REJECT and record:
                discard_pending(record)
                # For txt-only records audio_path is the txt itself,
                # already deleted by discard_pending. Just clean up any PID file.
                from pathlib import Path as _P
                _ap = _P(record["audio_path"])
                if _ap.suffix != ".txt":
                    pid_file_for(_ap).unlink(missing_ok=True)
                notify("Meeting Recorder", "Pending recording discarded.")
                # Always refresh the button — catches the case where the
                # last pending item was just discarded.
                self._refresh_resume_button()
                remaining = list_pending()
                if remaining:
                    self._show_pending_picker(remaining)

        picker.connect_response(on_response)
        picker.present()


# Application
class MeetingRecorderApp(Gtk.Application):
    def __init__(self):
        super().__init__(
            application_id="com.medtechcto.meetingrecorder",
            flags=Gio.ApplicationFlags.NON_UNIQUE,
        )

    def do_activate(self):
        missing = missing_env_vars()
        if missing:
            self._show_env_error(missing)
            return
        if not joplin_is_reachable():
            self._show_joplin_startup_error()
            return

        # Kill any orphan ffmpeg processes left over from a previous crash.
        # This runs before the window opens so the PipeWire sink is freed
        # before the user can attempt a new recording.
        kill_all_orphan_ffmpeg()

        win = MeetingRecorderWindow(self)
        win.present()
        # After window is up, check for pending recordings from previous OOM.
        pending = list_pending()
        if pending:
            GLib.idle_add(win._show_pending_picker, pending)

    def _show_env_error(self, missing: list[str]):
        var_list = "\n".join(f"  - {v}" for v in missing)
        dialog   = InfoDialog(
            None, "Missing Environment Variables",
            f"The following environment variables are not set:\n\n{var_list}\n\n"
            "Edit your launch script:\n\n"
            "  ~/Applications/MeetingGui/meeting_recorder_launch.sh\n\n"
            "Then relaunch the app.",
            [("Quit", Gtk.ResponseType.CLOSE)],
            application=self,
        )
        dialog.connect_response(lambda d, r: (d.destroy(), self.quit()))
        dialog.present()

    def _show_joplin_startup_error(self):
        dialog = InfoDialog(
            None, "Cannot Connect to Joplin",
            "The app could not reach Joplin on localhost:41184.\n\n"
            "To fix this:\n"
            "  1. Open Joplin.\n"
            "  2. Go to Tools -> Options -> Web Clipper.\n"
            "  3. Enable the Web Clipper service.\n"
            "  4. Click Retry below.",
            [("Quit", Gtk.ResponseType.CANCEL), ("Retry", Gtk.ResponseType.OK)],
            application=self,
        )
        def on_response(dlg, response):
            dlg.destroy()
            if response == Gtk.ResponseType.OK:
                if joplin_is_reachable():
                    MeetingRecorderWindow(self).present()
                else:
                    self._show_joplin_startup_error()
            else:
                self.quit()
        dialog.connect_response(on_response)
        dialog.present()

def main():
    MeetingRecorderApp().run(sys.argv)

if __name__ == "__main__":
    main()
