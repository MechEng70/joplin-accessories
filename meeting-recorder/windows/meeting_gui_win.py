#!/usr/bin/env python3
"""
meeting_gui_win.py  —  Windows port of meeting_gui.py
v1.09-win

Changes from Linux version:
  GTK4           → tkinter / ttk
  PulseAudio     → pyaudiowpatch (WASAPI loopback, in-process thread)
  notify-send    → plyer
  ~/.local/share → %APPDATA%\\MeetingRecorder
  SIGINT ffmpeg  → threading.Event stop
  xdg-open       → webbrowser.open
  .sh launcher   → meeting_recorder_launch.bat

Unchanged:
  faster-whisper, Anthropic API, requests/Joplin — all pure Python
  Business logic: job queue, pending/crash recovery, profile system,
  sidecar files, GPU monitoring, log management, resume pending

Dependencies:
    pip install faster-whisper anthropic requests pynvml pyaudiowpatch plyer numpy
    ffmpeg  (in PATH — used only by ffprobe for audio duration query)
"""

# ── Standard library ──────────────────────────────────────────────────────────
import os
import sys
import signal
import subprocess
import shutil
import wave
import threading
import queue
import datetime
import json
import warnings
import logging
import traceback
import webbrowser
import time
from pathlib import Path

# ── Data paths (Windows) ──────────────────────────────────────────────────────
# Use %APPDATA%\MeetingRecorder so data survives reinstalls and respects
# Windows conventions.  Mirrors ~/.local/share/meeting-recorder/ on Linux.
DATA_DIR = Path(os.environ.get("APPDATA", Path.home())) / "MeetingRecorder"
DATA_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE          = DATA_DIR / "meeting_recorder.log"
PENDING_DIR       = DATA_DIR / "pending"
PROFILES_FILE     = DATA_DIR / "profiles.json"
LOG_SIZE_WARN_MB  = 10

logging.basicConfig(
    filename=str(LOG_FILE),
    level=logging.DEBUG,
    format="%(asctime)s  %(levelname)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("meeting_recorder")

# ── tkinter ───────────────────────────────────────────────────────────────────
import tkinter as tk
from tkinter import ttk

# ── requests ──────────────────────────────────────────────────────────────────
try:
    import requests
except ImportError:
    print("ERROR: pip install requests")
    sys.exit(1)

# ── numpy (needed for PCM mixing in recording thread) ─────────────────────────
try:
    import numpy as np
except ImportError:
    print("ERROR: pip install numpy")
    sys.exit(1)

# ── pyaudiowpatch (WASAPI loopback) ───────────────────────────────────────────
try:
    import pyaudiowpatch as pyaudio
    PYAUDIO_AVAILABLE = True
except ImportError:
    PYAUDIO_AVAILABLE = False
    log.warning("pyaudiowpatch not installed — recording will not work.  pip install pyaudiowpatch")


def get_wasapi_input_devices() -> list[dict]:
    """
    Return a list of WASAPI input devices (non-loopback, maxInputChannels > 0).
    Each entry: {"index": int, "name": str, "is_default": bool}
    Returns [] if pyaudiowpatch is unavailable.
    """
    if not PYAUDIO_AVAILABLE:
        return []
    pa      = pyaudio.PyAudio()
    devices = []
    try:
        wasapi_api   = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        host_api_idx = int(wasapi_api["index"])
        default_idx  = int(wasapi_api["defaultInputDevice"])

        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if (
                int(dev.get("hostApi", -1)) == host_api_idx
                and int(dev.get("maxInputChannels", 0)) > 0
                and not dev.get("isLoopbackDevice", False)
            ):
                devices.append({
                    "index":      i,
                    "name":       dev["name"],
                    "is_default": (i == default_idx),
                })
    except Exception as e:
        log.warning("Could not enumerate WASAPI input devices: %s", e)
    finally:
        pa.terminate()
    return devices


def get_wasapi_loopback_devices() -> list[dict]:
    """
    Return a list of WASAPI loopback devices (isLoopbackDevice=True).
    These mirror output devices — one per speaker/output.
    Each entry: {"index": int, "name": str, "is_default": bool}
    The default is whichever loopback corresponds to the WASAPI default output.
    Returns [] if pyaudiowpatch is unavailable.
    """
    if not PYAUDIO_AVAILABLE:
        return []
    pa      = pyaudio.PyAudio()
    devices = []
    try:
        wasapi_api    = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        host_api_idx  = int(wasapi_api["index"])
        default_out   = pa.get_device_info_by_index(
            int(wasapi_api["defaultOutputDevice"])
        )
        default_name  = default_out.get("name", "")

        for i in range(pa.get_device_count()):
            dev = pa.get_device_info_by_index(i)
            if (
                int(dev.get("hostApi", -1)) == host_api_idx
                and dev.get("isLoopbackDevice", False)
            ):
                # Strip the " [Loopback]" suffix for display.
                display_name = dev["name"].replace(" [Loopback]", "").strip()
                is_default   = default_name in dev["name"]
                devices.append({
                    "index":        i,
                    "name":         display_name,
                    "raw_name":     dev["name"],
                    "is_default":   is_default,
                    "channels":     int(dev.get("maxInputChannels", 2)) or 2,
                })
    except Exception as e:
        log.warning("Could not enumerate WASAPI loopback devices: %s", e)
    finally:
        pa.terminate()
    return devices


# ── Version ───────────────────────────────────────────────────────────────────
APP_VERSION = "1.09-win"

# ── Configuration ─────────────────────────────────────────────────────────────
CONFIG = {
    "joplin_token":           os.environ.get("JOPLIN_TOKEN", ""),
    "anthropic_api_key":      os.environ.get("ANTHROPIC_API_KEY", ""),
    "joplin_host":            "http://localhost:41184",
    "whisper_model":          "large-v3",
    "whisper_device":         "cuda",
    "whisper_compute_type":   "float16",
    "mic_device_index":       -1,   # -1 = WASAPI communications default
    "loopback_device_index":  -1,   # -1 = loopback of WASAPI default output
}

SETTINGS_FILE = DATA_DIR / "settings.json"

def load_settings():
    """Load persisted settings (mic/loopback selection, etc.) into CONFIG."""
    if not SETTINGS_FILE.exists():
        return
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if "mic_device_index" in data:
            CONFIG["mic_device_index"] = int(data["mic_device_index"])
        if "loopback_device_index" in data:
            CONFIG["loopback_device_index"] = int(data["loopback_device_index"])
    except Exception as e:
        log.warning("Could not load settings: %s", e)

def save_settings():
    """Persist user-configurable settings to disk."""
    try:
        SETTINGS_FILE.write_text(json.dumps({
            "mic_device_index":      CONFIG["mic_device_index"],
            "loopback_device_index": CONFIG["loopback_device_index"],
        }, indent=2), encoding="utf-8")
    except Exception as e:
        log.warning("Could not save settings: %s", e)

# ── GPU monitoring ────────────────────────────────────────────────────────────
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


# ── App state ─────────────────────────────────────────────────────────────────
class AppState:
    def __init__(self):
        self.recording     = False
        self.processing    = False
        self.stop_event    = None
        self.record_thread = None
        self.audio_path    = None
        self.lock          = threading.Lock()
        self.job_queue     = queue.Queue()
        # Audio level meters — written by recording thread, read by GUI poll.
        # Single float assignment is GIL-atomic; no lock needed.
        self.mic_level  = 0.0   # 0.0 – 1.0 RMS normalized
        self.loop_level = 0.0

state = AppState()

# ── Notifications (plyer) ─────────────────────────────────────────────────────
def notify(title: str, body: str):
    """Toast notification via plyer.  Non-fatal on failure."""
    try:
        from plyer import notification
        notification.notify(
            title=title, message=body,
            app_name="Meeting Recorder", timeout=5,
        )
    except Exception:
        pass   # plyer is optional; notification failure must never crash the app


# ── Pending recordings store ──────────────────────────────────────────────────
def pending_dir() -> Path:
    PENDING_DIR.mkdir(parents=True, exist_ok=True)
    return PENDING_DIR


def write_sidecar(wav_path: Path, status: str, transcript_path: str = "") -> Path:
    sidecar = wav_path.with_suffix(".json")
    sidecar.write_text(json.dumps({
        "timestamp":       datetime.datetime.now().isoformat(),
        "audio_path":      str(wav_path),
        "transcript_path": transcript_path,
        "status":          status,
    }, indent=2))
    return sidecar


def pid_file_for(wav_path: Path) -> Path:
    """Retained for API compatibility; PID files are no longer written on Windows."""
    return wav_path.with_suffix(".ffmpeg.pid")


def write_pid_file(wav_path: Path, pid: int):
    """No-op on Windows — recording runs in-process, no orphan subprocess."""
    pass


def kill_orphan_ffmpeg(wav_path: Path):
    """
    No-op on Windows.  Recording uses an in-process thread, so there is no
    ffmpeg subprocess to orphan.  Any stale .ffmpeg.pid file from a Linux
    session is quietly removed.
    """
    pid_file = pid_file_for(wav_path)
    pid_file.unlink(missing_ok=True)


def kill_all_orphan_ffmpeg():
    """
    Clean up any leftover PID files (e.g. from a cross-platform migration).
    No processes are killed on Windows.
    """
    pd = pending_dir()
    for pid_file in pd.glob("*.ffmpeg.pid"):
        pid_file.unlink(missing_ok=True)


def save_pending(audio_path: str, transcript: str = "") -> Path:
    pd       = pending_dir()
    src      = Path(audio_path)
    wav_dest = src if src.parent == pd else None

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
    pd      = pending_dir()
    records = []
    for sidecar in sorted(pd.glob("*.json")):
        try:
            data = json.loads(sidecar.read_text())
            data["sidecar_path"] = str(sidecar)
            if Path(data["audio_path"]).exists():
                records.append(data)
            else:
                sidecar.unlink(missing_ok=True)
                tp = data.get("transcript_path", "")
                if tp:
                    Path(tp).unlink(missing_ok=True)
        except Exception:
            pass
    return records


def discard_pending(record: dict):
    Path(record["audio_path"]).unlink(missing_ok=True)
    Path(record["sidecar_path"]).unlink(missing_ok=True)


def _format_transcript_note(audio_path: str, transcript: str) -> str:
    """
    Format a raw transcript as a Joplin note body.
    Used by the Transcribe Only profile — no AI summarization involved.
    Duration is estimated from word count at ~130 wpm.
    """
    date_str   = datetime.datetime.now().strftime("%Y-%m-%d")
    time_str   = datetime.datetime.now().strftime("%H:%M")
    filename   = Path(audio_path).name
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
    msg = str(exc).lower()
    return "out of memory" in msg or "cuda error" in msg or "cudaerroroutofmemory" in msg


# ── Audio recording — Windows (pyaudiowpatch WASAPI loopback) ─────────────────

def _recording_worker(wav_path: Path, stop_event: threading.Event):
    """
    Records mic + system audio (WASAPI loopback) to wav_path at 16 kHz stereo.

    WASAPI requires the stream to open at the device's native sample rate —
    hardcoding 16000 Hz fails on most consumer devices (Errno -9997).
    Each stream is opened at its native rate and resampled to TARGET_RATE
    (16000 Hz) per chunk using linear interpolation before mixing.

    WAV is written progressively so the file is partially valid on crash.
    Stereo layout: L = mic, R = system loopback (or mic copy if no loopback).
    """
    if not PYAUDIO_AVAILABLE:
        log.error("pyaudiowpatch not available — cannot record.")
        return

    CHUNK       = 1024          # frames to read per call at the native rate
    TARGET_RATE = 16000         # WAV output rate (what faster-whisper expects)
    FMT         = pyaudio.paInt16
    SAMPSIZE    = 2

    def resample(arr: np.ndarray, from_rate: int, to_rate: int) -> np.ndarray:
        """Linear interpolation resample of a mono int16 array."""
        if from_rate == to_rate:
            return arr
        n_out = max(1, int(len(arr) * to_rate / from_rate))
        return np.interp(
            np.linspace(0, len(arr), n_out, endpoint=False),
            np.arange(len(arr)),
            arr.astype(np.float32),
        ).astype(np.int16)

    pa            = pyaudio.PyAudio()
    mic_stream    = None
    loop_stream   = None
    loop_channels = 1
    mic_rate      = TARGET_RATE
    loop_rate     = TARGET_RATE

    try:
        # ── Mic stream ────────────────────────────────────────────────────────
        mic_dev    = None
        chosen_idx = CONFIG.get("mic_device_index", -1)
        try:
            wasapi_api = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            if chosen_idx >= 0:
                mic_dev = pa.get_device_info_by_index(chosen_idx)
            else:
                comm_idx = int(wasapi_api["defaultInputDevice"])
                if comm_idx >= 0:
                    mic_dev = pa.get_device_info_by_index(comm_idx)
        except Exception:
            pass

        if mic_dev is None:
            mic_dev = pa.get_default_input_device_info()

        # Open at the device's native rate to avoid Errno -9997.
        mic_rate = int(mic_dev.get("defaultSampleRate", TARGET_RATE)) or TARGET_RATE
        mic_stream = pa.open(
            format=FMT, channels=1, rate=mic_rate,
            input=True, input_device_index=int(mic_dev["index"]),
            frames_per_buffer=CHUNK,
        )
        log.info(
            "Mic opened: %s (index %d, native %d Hz)",
            mic_dev.get("name", "?"), int(mic_dev["index"]), mic_rate,
        )

        # ── Loopback stream ───────────────────────────────────────────────────
        try:
            wasapi_api   = pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            chosen_loop  = CONFIG.get("loopback_device_index", -1)
            loopback_dev = None

            if chosen_loop >= 0:
                dev = pa.get_device_info_by_index(chosen_loop)
                if dev.get("isLoopbackDevice"):
                    loopback_dev = dev
                else:
                    log.warning(
                        "Configured loopback index %d is not a loopback device — "
                        "falling back to default output loopback.", chosen_loop
                    )

            if loopback_dev is None:
                default_out  = pa.get_device_info_by_index(
                    int(wasapi_api["defaultOutputDevice"])
                )
                default_name = default_out.get("name", "")
                for i in range(pa.get_device_count()):
                    dev = pa.get_device_info_by_index(i)
                    if dev.get("isLoopbackDevice") and default_name in dev["name"]:
                        loopback_dev = dev
                        break

            if loopback_dev:
                loop_channels = int(loopback_dev.get("maxInputChannels", 2)) or 2
                loop_rate     = int(loopback_dev.get("defaultSampleRate", TARGET_RATE)) or TARGET_RATE
                loop_stream   = pa.open(
                    format=FMT,
                    channels=loop_channels,
                    rate=loop_rate,
                    input=True,
                    input_device_index=int(loopback_dev["index"]),
                    frames_per_buffer=CHUNK,
                )
                log.info(
                    "WASAPI loopback opened: %s (%d ch, native %d Hz)",
                    loopback_dev.get("name", "?"), loop_channels, loop_rate,
                )
            else:
                log.warning("No WASAPI loopback device found. Recording mic only.")
        except Exception as e:
            log.warning("Could not open WASAPI loopback: %s — mic only.", e)

        # ── Write WAV progressively ───────────────────────────────────────────
        with wave.open(str(wav_path), "wb") as wf:
            wf.setnchannels(2)
            wf.setsampwidth(SAMPSIZE)
            wf.setframerate(TARGET_RATE)

            while not stop_event.is_set():
                mic_raw = mic_stream.read(CHUNK, exception_on_overflow=False)
                mic_arr = np.frombuffer(mic_raw, dtype=np.int16)
                mic_arr = resample(mic_arr, mic_rate, TARGET_RATE)

                if loop_stream:
                    try:
                        loop_raw = loop_stream.read(CHUNK, exception_on_overflow=False)
                        loop_arr = np.frombuffer(loop_raw, dtype=np.int16)
                        # Downmix stereo loopback to mono by averaging channels.
                        if loop_channels > 1:
                            loop_arr = (
                                loop_arr.reshape(-1, loop_channels)
                                        .mean(axis=1)
                                        .astype(np.int16)
                            )
                        loop_arr = resample(loop_arr, loop_rate, TARGET_RATE)
                    except Exception:
                        loop_arr = np.zeros(len(mic_arr), dtype=np.int16)
                else:
                    # No loopback: duplicate mic to both channels.
                    loop_arr = mic_arr.copy()

                # Align lengths (should already match, but guard against edge cases).
                n        = min(len(mic_arr), len(loop_arr))
                mic_arr  = mic_arr[:n]
                loop_arr = loop_arr[:n]

                # Interleave: [L0, R0, L1, R1, ...] where L=mic, R=system.
                stereo = np.column_stack([mic_arr, loop_arr]).astype(np.int16).flatten()
                wf.writeframes(stereo.tobytes())

                # Publish RMS levels for the GUI meters.
                # Normalise against int16 max (32768). Clamp to 1.0.
                mic_rms  = float(np.sqrt(np.mean(mic_arr.astype(np.float32) ** 2))) / 32768.0
                loop_rms = float(np.sqrt(np.mean(loop_arr.astype(np.float32) ** 2))) / 32768.0
                state.mic_level  = min(mic_rms,  1.0)
                state.loop_level = min(loop_rms, 1.0)

        log.info("Recording finished: %s", wav_path.name)

    except Exception:
        log.error("Recording worker error:\n%s", traceback.format_exc())
    finally:
        if mic_stream:
            try:
                mic_stream.stop_stream()
                mic_stream.close()
            except Exception:
                pass
        if loop_stream:
            try:
                loop_stream.stop_stream()
                loop_stream.close()
            except Exception:
                pass
        pa.terminate()


def start_recording() -> bool:
    """
    Begin recording directly into the pending directory.
    Sidecar is written immediately so crash recovery can locate the file.
    Returns False if pyaudiowpatch is unavailable.
    """
    if not PYAUDIO_AVAILABLE:
        notify("Recording Error", "pyaudiowpatch is not installed.  pip install pyaudiowpatch")
        return False

    pd        = pending_dir()
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    wav_path  = pd / f"recording_{timestamp}.wav"

    write_sidecar(wav_path, "recording")

    stop_event     = threading.Event()
    record_thread  = threading.Thread(
        target=_recording_worker,
        args=(wav_path, stop_event),
        daemon=True,
    )
    record_thread.start()

    with state.lock:
        state.recording     = True
        state.stop_event    = stop_event
        state.record_thread = record_thread
        state.audio_path    = str(wav_path)

    log.info("Recording started: %s", wav_path.name)
    return True


def stop_recording() -> str | None:
    """
    Signal the recording thread to stop and wait for it to flush the WAV.
    Returns the audio file path, or None if not recording.
    """
    with state.lock:
        if not state.recording:
            return None
        stop_event    = state.stop_event
        record_thread = state.record_thread
        audio_path    = state.audio_path
        state.recording     = False
        state.stop_event    = None
        state.record_thread = None
        state.audio_path    = None

    stop_event.set()
    # Wait up to 10 s for the thread to finish writing the WAV trailer.
    record_thread.join(timeout=10)
    if record_thread.is_alive():
        log.warning("Recording thread did not finish within 10 s — WAV may be incomplete.")

    pid_file_for(Path(audio_path)).unlink(missing_ok=True)
    return audio_path


# ── Audio duration ─────────────────────────────────────────────────────────────
def get_audio_duration(audio_path: str) -> float | None:
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_format", audio_path],
            capture_output=True, text=True, check=True,
        )
        return float(json.loads(result.stdout)["format"]["duration"])
    except Exception:
        return None


# ── Transcription (unchanged) ──────────────────────────────────────────────────
def transcribe(audio_path: str, progress_cb=None) -> str:
    from faster_whisper import WhisperModel
    try:
        model = WhisperModel(
            CONFIG["whisper_model"],
            device=CONFIG["whisper_device"],
            compute_type=CONFIG["whisper_compute_type"],
        )
    except Exception:
        model = WhisperModel(CONFIG["whisper_model"], device="cpu", compute_type="int8")

    duration        = get_audio_duration(audio_path)
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


# ── Summary profiles (unchanged from Linux) ───────────────────────────────────
BUILTIN_PROFILES = [
    {
        "id": "medical_device_meeting", "name": "Medical Device Meeting", "builtin": True,
        "system": (
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
        "focus": "FDA regulatory strategy, design controls, R&D consulting",
        "sections": ["summary", "decisions", "action_items", "open_questions"],
        "include_attendees": True, "include_location": True,
    },
    {
        "id": "general_meeting", "name": "General Meeting", "builtin": True,
        "system": (
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
        "focus": "General professional meeting",
        "sections": ["summary", "decisions", "action_items", "open_questions"],
        "include_attendees": True, "include_location": True,
    },
    {
        "id": "video_content_summary", "name": "Video / Content Summary", "builtin": True,
        "system": (
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
        "focus": "Video and content summarization",
        "sections": ["summary", "key_takeaways", "notable_details", "open_questions"],
        "include_attendees": False, "include_location": False,
    },
    {
        "id": "client_call", "name": "Client Call", "builtin": True,
        "system": (
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
        "focus": "Client relationship management and call documentation",
        "sections": ["summary", "commitments", "action_items", "relationship_notes", "follow_up"],
        "include_attendees": True, "include_location": True,
    },
    {
        "id": "linkedin_medtech", "name": "LinkedIn — MedTech", "builtin": True,
        "system": (
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
        "focus": "MedTech, FDA regulatory strategy, design controls, medical device consulting",
        "sections": ["summary"],
        "include_attendees": False, "include_location": False,
    },
    {
        "id": "linkedin_general", "name": "LinkedIn — General", "builtin": True,
        "system": (
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
        "focus": "Leadership, business building, technology, personal insights",
        "sections": ["summary"],
        "include_attendees": False, "include_location": False,
    },
    {
        "id": "transcribe_only", "name": "Transcribe Only", "builtin": True,
        "transcribe_only": True,
        "system": "", "prompt": "",
        "focus": "Raw transcription — no AI summarization",
        "sections": [],
        "include_attendees": False, "include_location": False,
    },
    {
        "id": "ip_assessment", "name": "IP Assessment", "builtin": True,
        "system": (
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
        "focus": "IP evaluation, patent landscape, commercialization strategy",
        "sections": ["summary", "technology_description", "ip_landscape",
                     "commercialization", "next_steps", "open_questions"],
        "include_attendees": True, "include_location": True,
    },
]


def load_profiles() -> list[dict]:
    PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    if not PROFILES_FILE.exists():
        _write_profiles(BUILTIN_PROFILES)
        return list(BUILTIN_PROFILES)
    try:
        saved = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except Exception:
        saved = []
    builtin_ids = {p["id"] for p in BUILTIN_PROFILES}
    custom      = [p for p in saved if p["id"] not in builtin_ids and not p.get("builtin")]
    merged      = list(BUILTIN_PROFILES) + custom
    return merged


def save_profiles(profiles: list[dict]):
    custom = [p for p in profiles if not p.get("builtin")]
    _write_profiles(BUILTIN_PROFILES + custom)


def _write_profiles(profiles: list[dict]):
    PROFILES_FILE.parent.mkdir(parents=True, exist_ok=True)
    PROFILES_FILE.write_text(json.dumps(profiles, indent=2), encoding="utf-8")


def profile_to_form(p: dict) -> dict:
    return {
        "name":              p.get("name", ""),
        "focus":             p.get("focus", ""),
        "sections":          p.get("sections", ["summary", "action_items"]),
        "include_attendees": p.get("include_attendees", True),
        "include_location":  p.get("include_location", True),
    }


def form_to_prompts(form: dict) -> tuple[str, str]:
    focus    = form.get("focus", "professional consulting")
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
        "The current date is {date}.", "", "---", "", "**Date:** {date}", "",
    ]
    if inc_att:
        lines += [
            "**Attendees:** Extract all names mentioned or identifiable from the transcript. "
            "Include organization or role where determinable. "
            'If not identifiable, write "Not identified."', "",
        ]
    if inc_loc:
        lines += [
            "**Location:** Extract from transcript context. For virtual meetings, note the "
            'platform if mentioned. If not determinable, write "Not identified."', "",
        ]
    lines += ["**Agenda / Topics:**", "- Bullet list of topics discussed, in the order they arose.", ""]

    section_templates = {
        "summary":            "## Summary\nA concise paragraph (3-5 sentences) describing the purpose and key outcomes.",
        "decisions":          "## Decisions\n- Bullet list of concrete decisions made, with enough context to stand alone.",
        "action_items":       "## Action Items\n- [ ] Action description (Owner: Name if identifiable, Due: date if mentioned)",
        "open_questions":     "## Open Questions\n- Unresolved items, questions raised without resolution, or topics for follow-up.",
        "key_takeaways":      "## Key Takeaways\n- Bullet list of the most important points, insights, or conclusions.",
        "notable_details":    "## Notable Details\n- Specific facts, figures, references, or examples worth capturing.",
        "commitments":        "## Commitments Made\n- Explicit commitments or deliverables agreed to by either party.",
        "relationship_notes": "## Relationship Notes\n- Context worth remembering: concerns, preferences, key priorities.",
        "follow_up":          "## Follow-up Required\n- Items needing follow-up before the next interaction.",
        "technology_description": "## Technology Description\n- Core innovation, stage of development, key differentiators.",
        "ip_landscape":       "## IP Landscape\n- Patents, freedom to operate, gaps or risks identified.",
        "commercialization":  "## Commercialization Potential\n- Target markets, potential partners, barriers discussed.",
        "next_steps":         "## Recommended Next Steps\n- [ ] Action description (Owner: Name if identifiable, Due: date if mentioned)",
    }
    for sec in sections:
        if sec in section_templates:
            lines.append(section_templates[sec])
            lines.append("")

    lines += ["---", "TRANSCRIPT:", "{transcript}"]
    return system, "\n".join(lines)


# ── Summarize (unchanged) ──────────────────────────────────────────────────────
def summarize(transcript: str, profile: dict | None = None) -> str:
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


# ── Joplin API (unchanged) ─────────────────────────────────────────────────────
def get_notebooks() -> list[dict]:
    notebooks, page = [], 1
    while True:
        resp = requests.get(
            f"{CONFIG['joplin_host']}/folders",
            params={"token": CONFIG["joplin_token"], "fields": "id,title,parent_id",
                    "limit": 100, "page": page},
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
        [{"id": nb["id"], "title": nb["title"], "depth": depth(nb),
          "_sort": sort_path(nb)} for nb in notebooks],
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
        requests.get(
            f"{CONFIG['joplin_host']}/ping",
            params={"token": CONFIG["joplin_token"]},
            timeout=3,
        ).raise_for_status()
        return True
    except Exception:
        return False


def missing_env_vars() -> list[str]:
    labels = {"joplin_token": "JOPLIN_TOKEN", "anthropic_api_key": "ANTHROPIC_API_KEY"}
    return [labels[k] for k in labels if not CONFIG[k]]


# ── Response type constants (replaces Gtk.ResponseType) ───────────────────────
class RT:
    OK     = "ok"
    CANCEL = "cancel"
    REJECT = "reject"
    HELP   = "help"
    CLOSE  = "close"


# ── Section choices (unchanged) ───────────────────────────────────────────────
SECTION_CHOICES = [
    ("summary",               "Summary"),
    ("decisions",             "Decisions"),
    ("action_items",          "Action Items"),
    ("open_questions",        "Open Questions"),
    ("key_takeaways",         "Key Takeaways"),
    ("notable_details",       "Notable Details"),
    ("commitments",           "Commitments Made"),
    ("relationship_notes",    "Relationship Notes"),
    ("follow_up",             "Follow-up Required"),
    ("technology_description","Technology Description"),
    ("ip_landscape",          "IP Landscape"),
    ("commercialization",     "Commercialization Potential"),
    ("next_steps",            "Recommended Next Steps"),
]


# ── InfoDialog ─────────────────────────────────────────────────────────────────
class InfoDialog(tk.Toplevel):
    """
    Modal dialog with configurable buttons.
    Mimics the Linux InfoDialog(Gtk.Window) connect_response / present API.

    buttons: list of (label_str, response_constant) where response_constant
             is one of RT.OK, RT.CANCEL, RT.REJECT, RT.HELP, RT.CLOSE, or any str.
    """

    def __init__(self, parent, title: str, body: str, buttons: list,
                 application=None):
        super().__init__(parent)
        self.title(title)
        self.resizable(False, False)
        if parent:
            self.transient(parent)
        self.grab_set()
        self._response_cb = None

        # ── Content ───────────────────────────────────────────────────────────
        content = ttk.Frame(self, padding=(20, 16, 20, 8))
        content.pack(fill="both", expand=True)

        ttk.Label(
            content, text=title,
            font=("Segoe UI", 10, "bold"), anchor="w",
        ).pack(fill="x", pady=(0, 6))

        ttk.Label(
            content, text=body,
            wraplength=420, justify="left", anchor="w",
        ).pack(fill="x")

        ttk.Separator(self, orient="horizontal").pack(fill="x", pady=(8, 0))

        btn_frame = ttk.Frame(self, padding=(12, 8))
        btn_frame.pack(fill="x")

        # Buttons packed right-to-left so the primary action ends up on the right.
        for label, rid in reversed(buttons):
            ttk.Button(
                btn_frame, text=label,
                command=lambda r=rid: self._on_btn(r),
            ).pack(side="right", padx=4)

        self.update_idletasks()
        self._center()

    def _center(self):
        self.update_idletasks()
        w = self.winfo_reqwidth()
        h = self.winfo_reqheight()
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"+{(sw - w) // 2}+{(sh - h) // 2}")

    def connect_response(self, cb):
        self._response_cb = cb

    def _on_btn(self, response_id):
        if self._response_cb:
            self._response_cb(self, response_id)

    def present(self):
        self.deiconify()
        self.focus_set()


# ── PendingPicker ─────────────────────────────────────────────────────────────
class PendingPicker(tk.Toplevel):
    """
    Modal window listing saved pending recordings.
    response_cb(dialog, response, record):
        RT.OK      → resume selected record
        RT.REJECT  → discard selected record
        RT.CANCEL  → dismiss
    """

    def __init__(self, parent, records: list[dict]):
        super().__init__(parent)
        self.title("Pending Recordings")
        self.transient(parent)
        self.grab_set()
        self.resizable(True, True)
        self.geometry("480x360")
        self._response_cb = None
        self._records     = records

        ttk.Label(
            self,
            text="Saved recordings waiting to be processed",
            font=("Segoe UI", 10, "bold"), anchor="w",
        ).pack(fill="x", padx=16, pady=(12, 2))

        ttk.Label(
            self,
            text="Select a recording, then Resume or Discard.",
            anchor="w",
        ).pack(fill="x", padx=16, pady=(0, 6))

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=(0, 4))

        scrollbar = ttk.Scrollbar(frame, orient="vertical")
        self._listbox = tk.Listbox(
            frame,
            yscrollcommand=scrollbar.set,
            selectmode="single",
            font=("Courier New", 9),
            activestyle="dotbox",
        )
        scrollbar.config(command=self._listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self._listbox.pack(side="left", fill="both", expand=True)

        status_labels = {
            "recording":    "interrupted mid-recording",
            "transcribing": "interrupted mid-transcription",
            "summarizing":  "interrupted mid-summarization",
            "failed":       "failed",
        }
        for rec in records:
            ts = rec.get("timestamp", "Unknown time")
            try:
                dt = datetime.datetime.fromisoformat(ts)
                ts = dt.strftime("%Y-%m-%d  %H:%M:%S")
            except Exception:
                pass
            wav    = Path(rec["audio_path"])
            size   = f"{wav.stat().st_size / 1024 / 1024:.1f} MB" if wav.exists() else "?"
            status = rec.get("status", "failed")
            sstr   = status_labels.get(status, status)
            self._listbox.insert("end", f"{ts}   {size}  [{sstr}]")

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        btn_frame = ttk.Frame(self, padding=(12, 8))
        btn_frame.pack(fill="x")

        ttk.Button(
            btn_frame, text="Discard",
            command=lambda: self._emit(RT.REJECT),
        ).pack(side="left", padx=4)

        ttk.Button(
            btn_frame, text="Cancel",
            command=lambda: self._emit(RT.CANCEL),
        ).pack(side="right", padx=4)

        ttk.Button(
            btn_frame, text="Resume",
            command=lambda: self._emit(RT.OK),
        ).pack(side="right", padx=4)

        self._center()

    def _center(self):
        self.update_idletasks()
        w, h  = 480, 360
        sw    = self.winfo_screenwidth()
        sh    = self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    def connect_response(self, cb):
        self._response_cb = cb

    def _selected_record(self) -> dict | None:
        sel = self._listbox.curselection()
        return self._records[sel[0]] if sel else None

    def _emit(self, response_id):
        if self._response_cb:
            self._response_cb(self, response_id, self._selected_record())

    def present(self):
        self.deiconify()
        self.focus_set()


# ── ProfileEditorWindow ───────────────────────────────────────────────────────
class ProfileEditorWindow(tk.Toplevel):
    """Create or edit a custom profile.  Simple form by default; Advanced checkbox
    reveals raw system-prompt and user-prompt editors."""

    def __init__(self, parent, profile: dict | None = None, on_save=None):
        super().__init__(parent)
        self.title("Edit Profile" if profile else "New Profile")
        self.transient(parent)
        self.grab_set()
        self.geometry("580x640")
        self.resizable(True, True)
        self._on_save  = on_save
        self._editing  = dict(profile) if profile else {}
        self._advanced = False

        # Outer frame + scrollable canvas for the form
        outer = ttk.Frame(self)
        outer.pack(fill="both", expand=True)

        canvas    = tk.Canvas(outer, highlightthickness=0)
        scrollbar = ttk.Scrollbar(outer, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        self._form_frame = ttk.Frame(canvas, padding=(16, 12, 16, 8))
        self._canvas_window = canvas.create_window(
            (0, 0), window=self._form_frame, anchor="nw"
        )
        self._form_frame.bind(
            "<Configure>",
            lambda e: canvas.configure(scrollregion=canvas.bbox("all"))
        )
        canvas.bind(
            "<Configure>",
            lambda e: canvas.itemconfig(self._canvas_window, width=e.width)
        )
        self._canvas = canvas

        self._build_form()

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        btn_frame = ttk.Frame(self, padding=(12, 8))
        btn_frame.pack(fill="x")
        ttk.Button(btn_frame, text="Cancel", command=self.destroy).pack(side="left")
        ttk.Button(btn_frame, text="Save Profile",
                   command=self._on_save_clicked).pack(side="right")

    def _build_form(self):
        f    = self._form_frame
        form = self._editing

        # Clear existing widgets
        for w in f.winfo_children():
            w.destroy()

        row = 0

        # ── Transcribe Only toggle ────────────────────────────────────────────
        self._var_transcribe_only = tk.BooleanVar(value=form.get("transcribe_only", False))
        ttk.Checkbutton(
            f, text="Transcribe Only (no AI summarization)",
            variable=self._var_transcribe_only,
            command=self._on_transcribe_only_toggled,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(0, 8))
        row += 1

        ttk.Label(f, text="Profile Name", anchor="w").grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        self._ent_name = ttk.Entry(f, width=50)
        self._ent_name.insert(0, form.get("name", ""))
        self._ent_name.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        row += 1

        ttk.Label(f, text="Focus Area", anchor="w").grid(
            row=row, column=0, columnspan=2, sticky="w")
        row += 1
        self._ent_focus = ttk.Entry(f, width=50)
        self._ent_focus.insert(0, form.get("focus", ""))
        self._ent_focus.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        row += 1

        # Attendees / Location toggles
        self._var_att = tk.BooleanVar(value=form.get("include_attendees", True))
        self._var_loc = tk.BooleanVar(value=form.get("include_location", True))
        ttk.Checkbutton(f, text="Include Attendees",
                        variable=self._var_att,
                        command=self._on_form_changed).grid(
            row=row, column=0, sticky="w")
        ttk.Checkbutton(f, text="Include Location",
                        variable=self._var_loc,
                        command=self._on_form_changed).grid(
            row=row, column=1, sticky="w", padx=(8, 0))
        row += 1

        ttk.Label(f, text="Output Sections", anchor="w").grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(8, 2))
        row += 1

        selected = set(form.get("sections", ["summary", "action_items"]))
        self._section_vars = {}
        sec_frame = ttk.Frame(f)
        sec_frame.grid(row=row, column=0, columnspan=2, sticky="ew")
        for i, (key, label) in enumerate(SECTION_CHOICES):
            var = tk.BooleanVar(value=(key in selected))
            var.trace_add("write", lambda *a: self._on_form_changed())
            self._section_vars[key] = var
            ttk.Checkbutton(sec_frame, text=label, variable=var).grid(
                row=i // 2, column=i % 2, sticky="w", padx=(0, 12), pady=2)
        row += 1

        # Advanced toggle
        self._var_adv = tk.BooleanVar(value=self._advanced)
        ttk.Checkbutton(
            f, text="Advanced (edit raw prompts)",
            variable=self._var_adv,
            command=self._on_advanced_toggled,
        ).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 4))
        row += 1

        # Advanced section (hidden by default)
        self._adv_frame = ttk.Frame(f)
        self._adv_frame.grid(row=row, column=0, columnspan=2, sticky="ew")
        row += 1

        ttk.Label(self._adv_frame, text="System Prompt", anchor="w").pack(
            fill="x", pady=(4, 2))
        sys_scroll = ttk.Frame(self._adv_frame)
        sys_scroll.pack(fill="x")
        self._txt_system = tk.Text(sys_scroll, height=6, wrap="word",
                                   font=("Consolas", 9))
        sys_sb = ttk.Scrollbar(sys_scroll, command=self._txt_system.yview)
        self._txt_system.configure(yscrollcommand=sys_sb.set)
        sys_sb.pack(side="right", fill="y")
        self._txt_system.pack(side="left", fill="both", expand=True)
        self._txt_system.insert("1.0", form.get("system", ""))

        ttk.Label(
            self._adv_frame,
            text="User Prompt Template  (use {transcript} and {date})",
            anchor="w",
        ).pack(fill="x", pady=(8, 2))
        prompt_scroll = ttk.Frame(self._adv_frame)
        prompt_scroll.pack(fill="both", expand=True)
        self._txt_prompt = tk.Text(prompt_scroll, height=10, wrap="word",
                                   font=("Consolas", 9))
        prompt_sb = ttk.Scrollbar(prompt_scroll, command=self._txt_prompt.yview)
        self._txt_prompt.configure(yscrollcommand=prompt_sb.set)
        prompt_sb.pack(side="right", fill="y")
        self._txt_prompt.pack(side="left", fill="both", expand=True)
        self._txt_prompt.insert("1.0", form.get("prompt", ""))

        self._adv_frame.pack_forget()  # hidden by default
        if self._advanced:
            self._adv_frame.pack(fill="both", expand=True)

        f.columnconfigure(0, weight=1)
        f.columnconfigure(1, weight=1)

        self._ent_name.bind("<KeyRelease>", lambda e: self._on_form_changed())
        self._ent_focus.bind("<KeyRelease>", lambda e: self._on_form_changed())

    def _on_transcribe_only_toggled(self):
        """Grey out all AI-related fields when Transcribe Only is checked."""
        is_to  = self._var_transcribe_only.get()
        state_ = "disabled" if is_to else "normal"
        for w in (self._ent_focus,):
            w.config(state=state_)
        # Toggle section checkboxes
        sec_frame = self._ent_focus.master  # same parent frame
        for key in self._section_vars:
            try:
                # Find the checkbutton widget and toggle it
                self._section_vars[key].set(False if is_to else self._section_vars[key].get())
            except Exception:
                pass
        # The simplest approach: rebuild won't lose state since _read_form captures it.
        # Just ensure the AI fields are visually muted.

    def _on_advanced_toggled(self):
        self._advanced = self._var_adv.get()
        if self._advanced:
            self._adv_frame.pack(fill="both", expand=True)
            self._sync_form_to_advanced()
        else:
            self._adv_frame.pack_forget()
        self._canvas.configure(scrollregion=self._canvas.bbox("all"))

    def _on_form_changed(self):
        if self._advanced:
            self._sync_form_to_advanced()

    def _sync_form_to_advanced(self):
        form = self._read_form()
        system, prompt = form_to_prompts(form)
        cur_sys = self._txt_system.get("1.0", "end-1c").strip()
        cur_prompt = self._txt_prompt.get("1.0", "end-1c").strip()
        if not cur_sys:
            self._txt_system.delete("1.0", "end")
            self._txt_system.insert("1.0", system)
        if not cur_prompt:
            self._txt_prompt.delete("1.0", "end")
            self._txt_prompt.insert("1.0", prompt)

    def _read_form(self) -> dict:
        return {
            "name":              self._ent_name.get().strip(),
            "focus":             self._ent_focus.get().strip(),
            "include_attendees": self._var_att.get(),
            "include_location":  self._var_loc.get(),
            "sections":          [k for k, _ in SECTION_CHOICES if self._section_vars[k].get()],
            "transcribe_only":   self._var_transcribe_only.get(),
        }

    def _on_save_clicked(self):
        form = self._read_form()
        if not form["name"]:
            return
        if self._advanced:
            system = self._txt_system.get("1.0", "end-1c").strip()
            prompt = self._txt_prompt.get("1.0", "end-1c").strip()
        else:
            system, prompt = form_to_prompts(form)

        profile = {
            "id":      self._editing.get("id") or f"custom_{form['name'].lower().replace(' ','_')}",
            "name":    form["name"],
            "builtin": False,
            **form,
            "system":           system,
            "prompt":           prompt,
            "transcribe_only":  form.get("transcribe_only", False),
        }
        if self._on_save:
            self._on_save(profile)
        self.destroy()

    def present(self):
        self.deiconify()
        self.focus_set()


# ── ProfileManagerWindow ──────────────────────────────────────────────────────
class ProfileManagerWindow(tk.Toplevel):
    """List all profiles; add, edit, duplicate, delete custom ones."""

    def __init__(self, parent, profiles: list[dict], on_reload=None):
        super().__init__(parent)
        self.title("Manage Profiles")
        self.transient(parent)
        self.grab_set()
        self.geometry("520x440")
        self.resizable(True, True)
        self._profiles  = list(profiles)
        self._closed    = False
        self._on_reload = on_reload   # callable — main window's _reload_profiles

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=8)

        scrollbar = ttk.Scrollbar(frame, orient="vertical")
        self._listbox = tk.Listbox(
            frame,
            yscrollcommand=scrollbar.set,
            selectmode="single",
            font=("Segoe UI", 9),
            activestyle="dotbox",
        )
        scrollbar.config(command=self._listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self._listbox.pack(side="left", fill="both", expand=True)
        self._populate_list()

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        btn_frame = ttk.Frame(self, padding=(12, 8))
        btn_frame.pack(fill="x")

        ttk.Button(btn_frame, text="+ New Profile",
                   command=self._on_new).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Edit",
                   command=self._on_edit_selected).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Duplicate",
                   command=self._on_duplicate_selected).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Delete",
                   command=self._on_delete_selected).pack(side="left", padx=4)
        ttk.Button(btn_frame, text="Close",
                   command=self._on_close).pack(side="right", padx=4)

    def _populate_list(self):
        self._listbox.delete(0, "end")
        for p in self._profiles:
            tag = " [built-in]" if p.get("builtin") else ""
            self._listbox.insert("end", f"{p['name']}{tag}")

    def _selected_profile(self) -> dict | None:
        sel = self._listbox.curselection()
        return self._profiles[sel[0]] if sel else None

    def _save_and_refresh(self):
        save_profiles(self._profiles)
        self._populate_list()
        # Immediately refresh the main window dropdown so changes are visible
        # without having to close the manager first.
        if self._on_reload:
            self._on_reload()

    def _on_new(self):
        ProfileEditorWindow(self, on_save=self._add_profile).present()

    def _on_edit_selected(self):
        p = self._selected_profile()
        if not p or p.get("builtin"):
            return
        ProfileEditorWindow(
            self, profile=p,
            on_save=lambda updated: self._update_profile(updated),
        ).present()

    def _on_duplicate_selected(self):
        p = self._selected_profile()
        if not p:
            return
        import copy
        dup            = copy.deepcopy(p)
        dup["id"]      = f"custom_{int(time.time())}"
        dup["name"]    = f"{p['name']} (copy)"
        dup["builtin"] = False
        self._profiles.append(dup)
        self._save_and_refresh()

    def _on_delete_selected(self):
        p = self._selected_profile()
        if not p or p.get("builtin"):
            return
        dialog = InfoDialog(
            self, "Delete Profile",
            f"Delete \"{p['name']}\"?\n\nThis cannot be undone.",
            [("Cancel", RT.CANCEL), ("Delete", RT.OK)],
        )
        def on_response(dlg, response):
            dlg.destroy()
            if response == RT.OK:
                self._profiles = [x for x in self._profiles if x["id"] != p["id"]]
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

    def _on_close(self):
        if not self._closed:
            self._closed = True
            self.destroy()

    def present(self):
        self.deiconify()
        self.focus_set()


# ── NotebookPicker ─────────────────────────────────────────────────────────────
class NotebookPicker(tk.Toplevel):
    """Select a Joplin notebook from a hierarchically indented list."""

    def __init__(self, parent, tree: list[dict]):
        super().__init__(parent)
        self.title("Select Notebook")
        self.transient(parent)
        self.grab_set()
        self.geometry("380x500")
        self.resizable(True, True)
        self.selected_id  = None
        self._response_cb = None
        self._tree        = tree

        frame = ttk.Frame(self)
        frame.pack(fill="both", expand=True, padx=8, pady=8)

        scrollbar = ttk.Scrollbar(frame, orient="vertical")
        self._listbox = tk.Listbox(
            frame,
            yscrollcommand=scrollbar.set,
            selectmode="single",
            font=("Segoe UI", 9),
            activestyle="dotbox",
        )
        scrollbar.config(command=self._listbox.yview)
        scrollbar.pack(side="right", fill="y")
        self._listbox.pack(side="left", fill="both", expand=True)

        for nb in tree:
            indent = "  " * nb["depth"]
            prefix = "\u21b3 " if nb["depth"] > 0 else ""
            self._listbox.insert("end", f"{indent}{prefix}{nb['title']}")

        self._listbox.bind("<<ListboxSelect>>", self._on_select)
        # Double-click submits, single-click just selects.
        self._listbox.bind("<Double-Button-1>", lambda e: self._emit(RT.OK))

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        btn_frame = ttk.Frame(self, padding=(12, 8))
        btn_frame.pack(fill="x")
        ttk.Button(btn_frame, text="Cancel",
                   command=lambda: self._emit(RT.CANCEL)).pack(side="right", padx=4)
        ttk.Button(btn_frame, text="Select",
                   command=lambda: self._emit(RT.OK)).pack(side="right", padx=4)

    def _on_select(self, event):
        sel = self._listbox.curselection()
        if sel:
            self.selected_id = self._tree[sel[0]]["id"]

    def connect_response(self, cb):
        self._response_cb = cb

    def _emit(self, response_id):
        if self._response_cb:
            self._response_cb(self, response_id)

    def get_selected_id(self) -> str | None:
        sel = self._listbox.curselection()
        if sel:
            return self._tree[sel[0]]["id"]
        return self.selected_id

    def present(self):
        self.deiconify()
        self.focus_set()


# ── Main window ───────────────────────────────────────────────────────────────
class MeetingRecorderWindow(tk.Tk):
    """
    Floating always-on-top window.  Direct replacement for the GTK4
    MeetingRecorderWindow / MeetingRecorderApp pair.

    Threading note: all GUI mutations in worker-thread callbacks go through
    self.after(0, ...) — the only thread-safe tkinter operation.
    """

    def __init__(self):
        super().__init__()
        self.title(f"Meeting Recorder  v{APP_VERSION}")
        self.resizable(False, False)
        self.wm_attributes("-topmost", True)

        self._setup_style()
        self._build_ui()
        self._set_status("idle")

        self._timer_start  = None
        self._timer_after  = None
        self._gpu_after    = None
        self._pulse_after  = None
        self._pulse_step   = 0

        # Start persistent worker that drains the job queue.
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()

        if NVML_AVAILABLE:
            self._start_idle_gpu_polling()

        # Check for pending recordings after the event loop is running.
        self.after(300, self._startup_pending_check)

    # ── Style ─────────────────────────────────────────────────────────────────
    def _setup_style(self):
        style = ttk.Style(self)
        # 'clam' honours custom foreground/background on ttk widgets.
        style.theme_use("clam")
        style.configure("Status.TLabel",
                        font=("Segoe UI", 9, "bold"), padding=(12, 4))
        style.configure("Timer.TLabel",
                        font=("Courier New", 14, "bold"), foreground="#ff6b6b")
        style.configure("GPU.TLabel",
                        font=("Courier New", 8), foreground="#aaaaaa")
        style.configure("Version.TLabel",
                        font=("Segoe UI", 7), foreground="#555555")
        # Coloured buttons: use tk.Button (not ttk) for reliable colouring.

    # ── Build UI ──────────────────────────────────────────────────────────────
    def _build_ui(self):
        pad = {"padx": 10, "pady": 3}

        self._status_label = ttk.Label(self, text="Idle", style="Status.TLabel",
                                       anchor="center", width=40)
        self._status_label.pack(fill="x", **pad)

        self._timer_label = ttk.Label(self, text="", style="Timer.TLabel",
                                      anchor="center")
        self._timer_label.pack(fill="x", padx=10)

        # ── Audio level meters (mic + system loopback) ────────────────────────
        # Shown only during recording. Two thin canvas bars with peak hold.
        meter_frame = ttk.Frame(self)
        meter_frame.pack(fill="x", padx=10, pady=(2, 0))

        METER_H = 10   # bar height in pixels

        mic_row = ttk.Frame(meter_frame)
        mic_row.pack(fill="x", pady=(0, 2))
        ttk.Label(mic_row, text="Mic  ", font=("Segoe UI", 7),
                  foreground="#aaaaaa", width=5, anchor="e").pack(side="left")
        self._mic_canvas = tk.Canvas(mic_row, height=METER_H,
                                     bg="#1e1e1e", highlightthickness=0)
        self._mic_canvas.pack(side="left", fill="x", expand=True)

        sys_row = ttk.Frame(meter_frame)
        sys_row.pack(fill="x")
        ttk.Label(sys_row, text="Sys  ", font=("Segoe UI", 7),
                  foreground="#aaaaaa", width=5, anchor="e").pack(side="left")
        self._sys_canvas = tk.Canvas(sys_row, height=METER_H,
                                     bg="#1e1e1e", highlightthickness=0)
        self._sys_canvas.pack(side="left", fill="x", expand=True)

        self._meter_frame    = meter_frame
        self._meter_after    = None
        self._mic_peak       = 0.0   # peak-hold value
        self._sys_peak       = 0.0
        self._mic_peak_decay = 0     # frames since last peak reset
        self._sys_peak_decay = 0
        self._hide_meters()

        # Progress bar + text label pair.
        prog_frame = ttk.Frame(self)
        prog_frame.pack(fill="x", padx=10, pady=(2, 0))
        self._progress_bar = ttk.Progressbar(
            prog_frame, orient="horizontal",
            mode="determinate", maximum=100,
        )
        self._progress_bar.pack(fill="x")
        self._progress_label = ttk.Label(prog_frame, text="", anchor="center",
                                         font=("Segoe UI", 8))
        self._progress_label.pack(fill="x")
        self._show_progress(False)

        self._gpu_label = ttk.Label(self, text="", style="GPU.TLabel",
                                    anchor="center")
        self._gpu_label.pack(fill="x", padx=10)
        if not NVML_AVAILABLE:
            self._gpu_label.pack_forget()

        # Mic selector row.
        mic_frame = ttk.Frame(self)
        mic_frame.pack(fill="x", padx=10, pady=(4, 0))
        ttk.Label(mic_frame, text="Mic:").pack(side="left")

        self._mic_devices  = get_wasapi_input_devices()
        mic_names          = [d["name"] for d in self._mic_devices]
        default_mic_name   = next(
            (d["name"] for d in self._mic_devices if d["is_default"]), ""
        )
        # If a saved mic index is in the device list, pre-select it.
        saved_idx  = CONFIG.get("mic_device_index", -1)
        saved_name = next(
            (d["name"] for d in self._mic_devices if d["index"] == saved_idx),
            default_mic_name,
        )
        self._mic_var  = tk.StringVar(value=saved_name or (mic_names[0] if mic_names else ""))
        self._mic_combo = ttk.Combobox(
            mic_frame,
            textvariable=self._mic_var,
            values=mic_names,
            state="readonly",
            width=30,
        )
        self._mic_combo.pack(side="left", padx=(4, 0), fill="x", expand=True)
        self._mic_combo.bind("<<ComboboxSelected>>", self._on_mic_selected)

        # Speaker / loopback selector row.
        spk_frame = ttk.Frame(self)
        spk_frame.pack(fill="x", padx=10, pady=(2, 0))
        ttk.Label(spk_frame, text="Spk:").pack(side="left")

        self._loop_devices   = get_wasapi_loopback_devices()
        loop_names           = [d["name"] for d in self._loop_devices]
        default_loop_name    = next(
            (d["name"] for d in self._loop_devices if d["is_default"]), ""
        )
        saved_loop_idx  = CONFIG.get("loopback_device_index", -1)
        saved_loop_name = next(
            (d["name"] for d in self._loop_devices if d["index"] == saved_loop_idx),
            default_loop_name,
        )
        self._loop_var   = tk.StringVar(value=saved_loop_name or (loop_names[0] if loop_names else ""))
        self._loop_combo = ttk.Combobox(
            spk_frame,
            textvariable=self._loop_var,
            values=loop_names,
            state="readonly",
            width=30,
        )
        self._loop_combo.pack(side="left", padx=(4, 0), fill="x", expand=True)
        self._loop_combo.bind("<<ComboboxSelected>>", self._on_loop_selected)

        # Profile selector row.
        prof_frame = ttk.Frame(self)
        prof_frame.pack(fill="x", padx=10, pady=(4, 2))
        ttk.Label(prof_frame, text="Profile:").pack(side="left")
        self._profiles      = load_profiles()
        self._profile_names = [p["name"] for p in self._profiles]
        self._profile_var   = tk.StringVar(value=self._profile_names[0])
        self._profile_combo = ttk.Combobox(
            prof_frame,
            textvariable=self._profile_var,
            values=self._profile_names,
            state="readonly",
            width=28,
        )
        self._profile_combo.pack(side="left", padx=(4, 0), fill="x", expand=True)
        ttk.Button(prof_frame, text="\u2699",
                   command=self._on_manage_profiles, width=3).pack(side="left", padx=(4, 0))

        # Start / Stop row.
        btn_row = ttk.Frame(self)
        btn_row.pack(fill="x", padx=10, pady=4)
        self._btn_start = tk.Button(
            btn_row, text="\u23fa  Start",
            bg="#48c774", fg="#1a1a1a",
            font=("Segoe UI", 9, "bold"),
            relief="flat", bd=0, padx=10,
            command=self._on_start,
        )
        self._btn_start.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self._btn_stop = tk.Button(
            btn_row, text="\u23f9  Stop",
            bg="#dc3545", fg="white",
            font=("Segoe UI", 9, "bold"),
            relief="flat", bd=0, padx=10,
            state="disabled",
            command=self._on_stop,
        )
        self._btn_stop.pack(side="left", fill="x", expand=True)

        # Import + Resume row.
        aux_row = ttk.Frame(self)
        aux_row.pack(fill="x", padx=10, pady=(2, 2))

        self._btn_import = tk.Button(
            aux_row, text="\U0001f4c2  Import Audio",
            bg="#7b5ea7", fg="white",
            font=("Segoe UI", 9, "bold"),
            relief="flat", bd=0, padx=10, pady=4,
            command=self._on_import,
        )
        self._btn_import.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self._btn_resume = tk.Button(
            aux_row, text="\u23ee  Resume Pending",
            bg="#4a90d9", fg="white",
            font=("Segoe UI", 9, "bold"),
            relief="flat", bd=0, padx=10, pady=4,
            command=self._on_resume,
        )
        self._btn_resume.pack(side="left", fill="x", expand=True)

        self._btn_clear_log = tk.Button(
            self, text="\U0001f5d1  Clear Log",
            bg="#555555", fg="#cccccc",
            font=("Segoe UI", 8),
            relief="flat", bd=0, padx=8, pady=3,
            command=self._on_clear_log,
        )
        self._btn_clear_log.pack(fill="x", padx=10, pady=(0, 2))

        self._refresh_resume_button()

        ttk.Label(self, text=f"v{APP_VERSION}", style="Version.TLabel",
                  anchor="e").pack(fill="x", padx=12, pady=(0, 4))

        # Center the window on screen.
        self.update_idletasks()
        w  = 380
        h  = self.winfo_reqheight() + 10
        sw = self.winfo_screenwidth()
        sh = self.winfo_screenheight()
        self.geometry(f"{w}x{h}+{(sw - w) // 2}+{(sh - h) // 2}")

    # ── Timer ─────────────────────────────────────────────────────────────────
    def _start_timer(self):
        self._timer_start = time.monotonic()
        self._tick_timer()

    def _tick_timer(self):
        if self._timer_start is None:
            return
        elapsed = int(time.monotonic() - self._timer_start)
        h, rem  = divmod(elapsed, 3600)
        m, s    = divmod(rem, 60)
        self._timer_label.config(text=f"{h:02d}:{m:02d}:{s:02d}")
        self._timer_after = self.after(1000, self._tick_timer)

    def _stop_timer(self):
        if self._timer_after:
            self.after_cancel(self._timer_after)
            self._timer_after = None
        self._timer_start = None
        self._timer_label.config(text="")

    # ── Audio level meters ────────────────────────────────────────────────────
    def _show_meters(self):
        self._meter_frame.pack(fill="x", padx=10, pady=(2, 0))
        self._mic_peak = self._sys_peak = 0.0
        self._mic_peak_decay = self._sys_peak_decay = 0
        self._poll_meters()

    def _hide_meters(self):
        if self._meter_after:
            self.after_cancel(self._meter_after)
            self._meter_after = None
        self._meter_frame.pack_forget()
        state.mic_level  = 0.0
        state.loop_level = 0.0

    def _poll_meters(self):
        """Redraw both level bars at ~15 fps while recording."""
        self._draw_meter(self._mic_canvas, state.mic_level,
                         "_mic_peak", "_mic_peak_decay")
        self._draw_meter(self._sys_canvas, state.loop_level,
                         "_sys_peak", "_sys_peak_decay")
        self._meter_after = self.after(66, self._poll_meters)

    def _draw_meter(self, canvas: tk.Canvas, level: float,
                    peak_attr: str, decay_attr: str):
        """
        Draw a single level bar with colour gradient and peak-hold tick.

        Colour zones (linear RMS, not dB):
          0.00 – 0.50  →  green   (#48c774)
          0.50 – 0.75  →  yellow  (#ffdd57)
          0.75 – 1.00  →  red     (#dc3545)
        """
        canvas.update_idletasks()
        W = canvas.winfo_width()
        H = canvas.winfo_height()
        if W < 2:
            return

        canvas.delete("all")

        # Slightly boost display level so quiet speech is still visible.
        display = min(level * 2.5, 1.0)

        fill_w = int(display * W)

        # Gradient: split fill into up to three colour zones.
        green_end  = int(0.50 * W)
        yellow_end = int(0.75 * W)

        if fill_w > 0:
            # Green segment
            g_w = min(fill_w, green_end)
            canvas.create_rectangle(0, 0, g_w, H, fill="#48c774", outline="")

        if fill_w > green_end:
            # Yellow segment
            y_w = min(fill_w, yellow_end) - green_end
            canvas.create_rectangle(green_end, 0, green_end + y_w, H,
                                    fill="#ffdd57", outline="")

        if fill_w > yellow_end:
            # Red segment
            r_w = fill_w - yellow_end
            canvas.create_rectangle(yellow_end, 0, yellow_end + r_w, H,
                                    fill="#dc3545", outline="")

        # Peak-hold tick: advance or decay.
        peak     = getattr(self, peak_attr)
        decay    = getattr(self, decay_attr)
        HOLD_FRAMES = 20   # ~1.3 s at 15 fps before decay begins

        if display >= peak:
            setattr(self, peak_attr, display)
            setattr(self, decay_attr, 0)
        else:
            if decay < HOLD_FRAMES:
                setattr(self, decay_attr, decay + 1)
            else:
                new_peak = max(peak - 0.025, 0.0)
                setattr(self, peak_attr, new_peak)

        peak = getattr(self, peak_attr)
        if peak > 0.01:
            px = min(int(peak * W), W - 2)
            canvas.create_rectangle(px, 0, px + 2, H, fill="white", outline="")

    # ── GPU polling ───────────────────────────────────────────────────────────
    def _start_gpu_polling(self):
        """1-second fast poll during transcription."""
        if not NVML_AVAILABLE:
            return
        self._stop_gpu_polling(restart=False)
        self._gpu_poll_interval = 1000
        self._gpu_poll_tick()

    def _start_idle_gpu_polling(self):
        """5-second slow poll when idle."""
        if not NVML_AVAILABLE or self._gpu_after:
            return
        self._gpu_poll_interval = 5000
        self._gpu_poll_tick()

    def _gpu_poll_tick(self):
        stats = get_gpu_stats()
        if stats:
            self._gpu_label.config(text=stats)
        interval = getattr(self, "_gpu_poll_interval", 5000)
        self._gpu_after = self.after(interval, self._gpu_poll_tick)

    def _stop_gpu_polling(self, restart: bool = True):
        if self._gpu_after:
            self.after_cancel(self._gpu_after)
            self._gpu_after = None
        if restart:
            self._start_idle_gpu_polling()

    # ── Progress bar ──────────────────────────────────────────────────────────
    def _show_progress(self, visible: bool):
        if visible:
            self._progress_bar.pack(fill="x")
            self._progress_label.pack(fill="x")
        else:
            self._progress_bar.pack_forget()
            self._progress_label.pack_forget()
            self._progress_bar.configure(mode="determinate")
            self._progress_bar["value"] = 0
            self._progress_label.config(text="")

    def _start_pulse(self):
        """Indeterminate animation for operations without real progress (e.g. summarization)."""
        self._show_progress(True)
        self._progress_bar.configure(mode="indeterminate")
        self._progress_bar.start(120)
        self._progress_label.config(text="Processing...")

    def _stop_pulse(self):
        self._progress_bar.stop()
        self._progress_bar.configure(mode="determinate")

    def _update_progress(self, fraction: float, seg_end: float, duration: float):
        self._progress_bar.configure(mode="determinate")
        self._progress_bar["value"] = int(fraction * 100)
        remaining  = max(0.0, duration - seg_end)
        mins, secs = divmod(int(remaining), 60)
        self._progress_label.config(
            text=f"{int(fraction * 100)}%  (~{mins}m {secs:02d}s remaining)"
        )

    # ── Status ────────────────────────────────────────────────────────────────
    def _set_status(self, status: str, detail: str = ""):
        labels = {
            "idle":       "\u25cf Idle",
            "recording":  "\u25cf Recording...",
            "processing": "\u25cf Processing...",
        }
        text = labels.get(status, status)
        if detail:
            text += f"  {detail}"

        queued = state.job_queue.qsize()
        if queued > 0 and status in ("processing", "recording"):
            text += f"  [{queued} queued]"

        self._status_label.config(text=text)

        # Derive button states from actual recording state, not the status string.
        # This prevents the processing pipeline (which calls _set_status("processing")
        # or _set_status("idle") when a job finishes) from disabling Stop while a
        # concurrent second recording is still active.
        actually_recording = state.recording
        self._btn_start.config(state="normal" if not actually_recording else "disabled")
        self._btn_stop.config(state="normal" if actually_recording else "disabled")
        # Import is available any time we are not actively recording.
        self._btn_import.config(state="normal" if not actually_recording else "disabled")

        is_idle = (status == "idle") and not actually_recording
        self._btn_resume.config(state="normal" if is_idle else "disabled")
        self._btn_clear_log.config(state="normal" if is_idle else "disabled")
        if is_idle:
            self._refresh_resume_button()

    def _refresh_resume_button(self):
        pending = list_pending()
        n       = len(pending)
        if n > 0:
            self._btn_resume.config(text=f"\u23ee  Resume Pending ({n})")
        else:
            self._btn_resume.config(text="\u23ee  Resume Pending")
        self._refresh_log_button()

    def _refresh_log_button(self):
        try:
            size_mb = LOG_FILE.stat().st_size / 1024 / 1024 if LOG_FILE.exists() else 0
        except Exception:
            size_mb = 0

        if size_mb >= LOG_SIZE_WARN_MB:
            self._btn_clear_log.config(
                bg="#8b0000", fg="#ff9999",
                font=("Segoe UI", 8, "bold"),
                text=f"\U0001f5d1  Clear Log ({size_mb:.1f} MB)",
            )
        else:
            label = "\U0001f5d1  Clear Log"
            if size_mb > 0:
                label += f" ({size_mb:.1f} MB)"
            self._btn_clear_log.config(
                bg="#555555", fg="#cccccc",
                font=("Segoe UI", 8),
                text=label,
            )

    # ── Button handlers ───────────────────────────────────────────────────────
    def _on_start(self):
        ok = start_recording()
        if ok:
            self._set_status("recording")
            self._start_timer()
            self._show_meters()
            notify("Meeting Recorder", "Recording started.")

    def _on_stop(self):
        with state.lock:
            if not state.recording:
                return
        self._stop_timer()
        self._hide_meters()
        profile    = self._get_selected_profile()
        audio_path = stop_recording()
        if audio_path:
            queued = state.job_queue.qsize()
            state.job_queue.put((audio_path, profile))
            if queued > 0:
                notify(
                    "Meeting Recorder",
                    f"Recording queued (position {queued + 1}). You can start the next meeting.",
                )
                self.after(0, lambda: self._set_status(
                    "recording" if state.recording else "processing", "Queued"
                ))
            else:
                self.after(0, lambda: self._set_status("processing", "Transcribing..."))

    def _on_clear_log(self):
        dialog = InfoDialog(
            self, "Clear Log File",
            f"This will delete all contents of the log file.\n\n{LOG_FILE}\n\n"
            "This cannot be undone.",
            [("Cancel", RT.CANCEL), ("Clear", RT.OK)],
        )
        def on_response(dlg, response):
            dlg.destroy()
            if response == RT.OK:
                try:
                    LOG_FILE.write_text("", encoding="utf-8")
                    log.info("Log file cleared by user.")
                    notify("Meeting Recorder", "Log file cleared.")
                    self._refresh_log_button()
                except Exception as e:
                    notify("Meeting Recorder", f"Could not clear log: {e}")
        dialog.connect_response(on_response)
        dialog.present()

    def _on_manage_profiles(self):
        editor = ProfileManagerWindow(self, self._profiles, on_reload=self._reload_profiles)
        editor.protocol("WM_DELETE_WINDOW", lambda: (self._reload_profiles(), editor.destroy()))
        editor.present()

    def _on_mic_selected(self, event=None):
        """Persist the chosen mic device index to CONFIG and settings file."""
        name = self._mic_var.get()
        dev  = next((d for d in self._mic_devices if d["name"] == name), None)
        if dev:
            CONFIG["mic_device_index"] = dev["index"]
            save_settings()
            log.info("Mic selection changed: %s (index %d)", dev["name"], dev["index"])

    def _on_loop_selected(self, event=None):
        """Persist the chosen loopback device index to CONFIG and settings file."""
        name = self._loop_var.get()
        dev  = next((d for d in self._loop_devices if d["name"] == name), None)
        if dev:
            CONFIG["loopback_device_index"] = dev["index"]
            save_settings()
            log.info(
                "Loopback selection changed: %s (index %d)", dev["name"], dev["index"]
            )

    def _get_selected_profile(self) -> dict:
        name = self._profile_var.get()
        for p in self._profiles:
            if p["name"] == name:
                return p
        return self._profiles[0]

    def _reload_profiles(self):
        self._profiles      = load_profiles()
        self._profile_names = [p["name"] for p in self._profiles]
        self._profile_combo.configure(values=self._profile_names)
        self._profile_var.set(self._profile_names[0])

    # ── Worker (job queue drain) ───────────────────────────────────────────────
    def _worker_loop(self):
        while True:
            job = state.job_queue.get()
            audio_path, profile = job if isinstance(job, tuple) else (job, None)
            try:
                self._process(audio_path, profile=profile)
            except Exception as e:
                log.error("Worker loop unhandled exception: %s", e)
            finally:
                state.job_queue.task_done()
                remaining = state.job_queue.qsize()
                if remaining > 0:
                    self.after(0, lambda n=remaining: self._set_status(
                        "processing", f"Transcribing...  [{n} queued]"
                    ))

    # ── Processing pipeline ───────────────────────────────────────────────────
    def _process(self, audio_path: str, profile: dict | None = None):
        transcript_cache = None
        try:
            # Guard: recording worker may have failed before writing any data.
            # The WAV file must exist and be non-empty before we attempt anything.
            audio_file = Path(audio_path)
            if not audio_file.exists() or audio_file.stat().st_size == 0:
                log.error(
                    "Audio file missing or empty: %s — recording worker likely "
                    "failed before writing frames. Check log for 'Recording worker error'.",
                    audio_path,
                )
                self.after(0, lambda: self._show_error_dialog(
                    "Recording Failed",
                    "The recording file is missing or empty.\n\n"
                    "This usually means the microphone could not be opened.\n"
                    "Check the mic selector dropdown and try a different device.",
                ))
                # Clean up the orphan sidecar if present.
                audio_file.with_suffix(".json").unlink(missing_ok=True)
                return

            queued = state.job_queue.qsize()
            label  = f"Transcribing...  [{queued} queued]" if queued > 0 else "Transcribing..."
            self.after(0, lambda: self._show_progress(True))
            self.after(0, self._start_gpu_polling)
            self.after(0, lambda: self._set_status("processing", label))
            notify("Meeting Recorder", "Transcribing audio...")

            _update_sidecar_status(audio_path, "transcribing")

            def on_progress(fraction, seg_end, duration):
                self.after(0, lambda: self._update_progress(fraction, seg_end, duration))

            transcript = transcribe(audio_path, progress_cb=on_progress)

            self.after(0, self._stop_pulse)
            self.after(0, lambda: self._show_progress(False))
            self.after(0, self._stop_gpu_polling)

            if not transcript.strip():
                notify("Meeting Recorder", "Transcript was empty. Check audio routing.")
                self.after(0, lambda: self._set_status("idle"))
                return

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
                self.after(0, lambda: self._set_status("processing", "Filing transcript..."))
                note_body = _format_transcript_note(audio_path, transcript)
                self.after(0, lambda nb=note_body: self._pick_notebook_and_save(nb))
                return

            _update_sidecar_status(
                audio_path, "summarizing",
                str(transcript_cache) if transcript_cache else "",
            )

            self.after(0, lambda: self._set_status("processing", "Summarizing..."))
            notify("Meeting Recorder", "Summarizing with Claude...")

            auto_retry_count = 0
            MAX_AUTO_RETRIES = 3

            while True:
                try:
                    summary = summarize(transcript, profile=profile)
                    break

                except Exception as e:
                    err     = str(e).lower()
                    err_str = str(e)

                    if any(k in err for k in ("credit balance", "too low", "billing", "payment", "402")):
                        retry_event = threading.Event()
                        self.after(0, lambda re=retry_event: self._show_funds_dialog(re, audio_path))
                        retry_event.wait()
                        if getattr(retry_event, "cancelled", False):
                            self.after(0, lambda: self._set_status("idle"))
                            return
                        self.after(0, lambda: self._set_status("processing", "Retrying..."))
                        auto_retry_count = 0
                        continue

                    is_server_error = any(k in err for k in (
                        "500", "502", "503", "529",
                        "internal server error", "overloaded",
                        "service unavailable", "bad gateway",
                    ))

                    if is_server_error and auto_retry_count < MAX_AUTO_RETRIES:
                        auto_retry_count += 1
                        wait = 10 * auto_retry_count
                        log.warning(
                            "Anthropic server error (attempt %d/%d), retrying in %ds: %s",
                            auto_retry_count, MAX_AUTO_RETRIES, wait, err_str,
                        )
                        self.after(0, lambda w=wait, n=auto_retry_count: self._set_status(
                            "processing", f"API error, retrying in {w}s... ({n}/{MAX_AUTO_RETRIES})"
                        ))
                        time.sleep(wait)
                        continue

                    if is_server_error:
                        retry_event = threading.Event()
                        self.after(0, lambda re=retry_event, tc=transcript_cache, es=err_str:
                                   self._show_api_error_dialog(re, audio_path, tc, es))
                        retry_event.wait()
                        if getattr(retry_event, "cancelled", False):
                            self.after(0, lambda: self._set_status("idle"))
                            return
                        auto_retry_count = 0
                        self.after(0, lambda: self._set_status("processing", "Retrying..."))
                        continue

                    raise

            self.after(0, lambda s=summary: self._pick_notebook_and_save(s))

        except Exception as e:
            self.after(0, self._stop_pulse)
            self.after(0, lambda: self._show_progress(False))
            self.after(0, self._stop_gpu_polling)

            log.error("Processing failed:\n%s", traceback.format_exc())

            if is_cuda_oom(e):
                try:
                    cached_transcript = ""
                    if transcript_cache and transcript_cache.exists():
                        cached_transcript = transcript_cache.read_text(encoding="utf-8")
                        transcript_cache.unlink(missing_ok=True)
                    save_pending(audio_path, transcript=cached_transcript)
                    self.after(0, lambda: self._set_status("idle"))
                    self.after(0, self._show_oom_dialog)
                    return
                except Exception as save_err:
                    log.error("save_pending failed: %s", save_err)
                    self.after(0, lambda: self._show_error_dialog(
                        "Save Failed",
                        f"GPU out of memory and recording could not be saved.\n\n"
                        f"See log for details:\n{LOG_FILE}",
                    ))
            else:
                try:
                    cached = ""
                    if transcript_cache and transcript_cache.exists():
                        cached = transcript_cache.read_text(encoding="utf-8")
                        transcript_cache.unlink(missing_ok=True)
                    if cached:
                        save_pending(audio_path, transcript=cached)
                        self.after(0, self._refresh_resume_button)
                        log.info("Transcript saved to pending after error.")
                except Exception as save_err:
                    log.error("Could not save pending after error: %s", save_err)

                short = str(e).split("\n")[0][:200]
                self.after(0, lambda msg=short: self._show_error_dialog(
                    "Processing Error",
                    f"{msg}\n\n"
                    "Your transcript has been saved to the pending queue.\n"
                    "Use the Resume button when ready.\n\n"
                    f"Full details logged to:\n{LOG_FILE}",
                ))

            self.after(0, lambda: self._set_status("idle"))

        finally:
            with state.lock:
                state.processing = False
            try:
                wav     = Path(audio_path)
                sidecar = wav.with_suffix(".json")
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
            if state.job_queue.empty():
                # If a second recording started while this job was processing,
                # restore the recording status rather than slamming to idle.
                if state.recording:
                    self.after(0, lambda: self._set_status("recording"))
                else:
                    self.after(0, lambda: self._set_status("idle"))

    # ── Dialog methods ─────────────────────────────────────────────────────────
    def _show_error_dialog(self, title: str, message: str):
        dialog = InfoDialog(self, title, message, [("OK", RT.OK)])
        dialog.connect_response(lambda d, r: d.destroy())
        dialog.present()

    def _show_oom_dialog(self):
        dialog = InfoDialog(
            self, "GPU Out of Memory",
            "The GPU ran out of VRAM to run transcription.\n\n"
            "Your recording has been saved and can be resumed once VRAM is free.\n\n"
            "To free VRAM:\n"
            "  - Close other GPU-using apps (Ollama, Open WebUI, etc.)\n"
            "  - Open Task Manager > Performance > GPU to see what is using VRAM\n\n"
            "Use the Resume button when ready.",
            [("OK", RT.OK)],
        )
        dialog.connect_response(lambda d, r: d.destroy())
        dialog.present()

    def _show_api_error_dialog(
        self,
        retry_event: threading.Event,
        audio_path: str,
        transcript_cache,
        error: str,
    ):
        self.after(0, lambda: self._set_status("processing", "API error"))
        notify("Meeting Recorder", "Anthropic API error. Transcript saved — you can retry later.")

        dialog = InfoDialog(
            self, "Anthropic API Error",
            f"The Anthropic API returned a server error after 3 retry attempts.\n\n"
            f"Error: {error[:120]}\n\n"
            "Your transcript has been saved. Options:\n"
            "  Retry - try the API call again now.\n"
            "  Save & Quit - save transcript to pending queue\n"
            "  and resume later from the Resume button.",
            [("Save & Quit", RT.CANCEL), ("Retry", RT.OK)],
        )
        def on_response(dlg, response):
            dlg.destroy()
            if response == RT.OK:
                retry_event.set()
            else:
                retry_event.cancelled = True
                retry_event.set()
                try:
                    cached = ""
                    if transcript_cache and Path(transcript_cache).exists():
                        cached = Path(transcript_cache).read_text(encoding="utf-8")
                        Path(transcript_cache).unlink(missing_ok=True)
                    save_pending(audio_path, transcript=cached)
                    notify("Meeting Recorder", "Transcript saved. Use Resume when ready.")
                    self.after(0, self._refresh_resume_button)
                except Exception as ex:
                    log.error("save_pending after API error: %s", ex)
                    notify("Meeting Recorder", "Could not save pending. Check log.")
        dialog.connect_response(on_response)
        dialog.present()

    def _show_funds_dialog(self, retry_event: threading.Event, audio_path: str):
        self.after(0, lambda: self._set_status("processing", "Insufficient funds"))
        notify("Meeting Recorder", "Anthropic API: insufficient credits. Add funds then retry.")

        dialog = InfoDialog(
            self, "Anthropic API: Insufficient Credits",
            "Your Anthropic API credit balance is too low to complete the summary.\n\n"
            "Your recording transcript is safe — nothing has been lost.\n\n"
            "Add credits at console.anthropic.com/settings/billing,\n"
            "then click Retry to continue.",
            [("Open Billing", RT.HELP), ("Cancel", RT.CANCEL), ("Retry", RT.OK)],
        )
        def on_response(dlg, response):
            if response == RT.HELP:
                webbrowser.open("https://console.anthropic.com/settings/billing")
                return
            dlg.destroy()
            if response == RT.OK:
                retry_event.set()
            else:
                retry_event.cancelled = True
                retry_event.set()
                notify("Meeting Recorder", "Summarization cancelled.")
        dialog.connect_response(on_response)
        dialog.present()

    # ── Notebook picker and Joplin save ───────────────────────────────────────
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
            if response != RT.OK or not notebook_id:
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
            [("Discard Note", RT.CANCEL), ("Retry", RT.OK)],
        )
        def on_response(dlg, response):
            dlg.destroy()
            if response == RT.OK:
                if joplin_is_reachable():
                    self._pick_notebook_and_save(summary)
                else:
                    self._show_joplin_mid_error(summary, "Still unreachable.")
            else:
                notify("Meeting Recorder", "Note discarded.")
                self._set_status("idle")
        dialog.connect_response(on_response)
        dialog.present()

    # ── Import Audio ──────────────────────────────────────────────────────────
    def _on_import(self):
        """Open a file picker then an import confirmation dialog."""
        from tkinter import filedialog
        filetypes = [
            ("Audio files", "*.wav *.mp3 *.m4a *.mp4 *.ogg *.flac *.aac *.wma *.webm *.mkv"),
            ("All files", "*.*"),
        ]
        path = filedialog.askopenfilename(
            parent=self,
            title="Select Audio File",
            filetypes=filetypes,
        )
        if path:
            self._show_import_dialog(path)

    def _show_import_dialog(self, file_path: str):
        """Confirm profile selection before queuing the imported file."""
        filename = Path(file_path).name
        try:
            size_mb = Path(file_path).stat().st_size / 1024 / 1024
        except Exception:
            size_mb = 0.0

        profiles      = load_profiles()
        profile_names = [p["name"] for p in profiles]

        win = tk.Toplevel(self)
        win.title("Import Audio File")
        win.transient(self)
        win.grab_set()
        win.resizable(False, False)

        content = ttk.Frame(win, padding=(20, 16, 20, 8))
        content.pack(fill="both", expand=True)

        ttk.Label(content, text="Import Audio File",
                  font=("Segoe UI", 10, "bold"), anchor="w").pack(fill="x", pady=(0, 4))
        ttk.Label(content, text=f"{filename}  ({size_mb:.1f} MB)",
                  anchor="w").pack(fill="x", pady=(0, 8))
        ttk.Label(content, text="Summarization Profile:", anchor="w").pack(fill="x")

        # Default to the same profile currently selected in main window.
        current_name = self._profile_var.get()
        current_idx  = next((i for i, p in enumerate(profiles) if p["name"] == current_name), 0)
        profile_var  = tk.StringVar(value=profile_names[current_idx])
        combo = ttk.Combobox(content, textvariable=profile_var,
                             values=profile_names, state="readonly", width=40)
        combo.pack(fill="x", pady=(2, 0))

        ttk.Separator(win, orient="horizontal").pack(fill="x", pady=(10, 0))

        btn_frame = ttk.Frame(win, padding=(12, 8))
        btn_frame.pack(fill="x")

        ttk.Button(btn_frame, text="Cancel",
                   command=win.destroy).pack(side="left")

        def on_queue():
            name    = profile_var.get()
            profile = next((p for p in profiles if p["name"] == name), profiles[0])
            win.destroy()
            self._queue_import(file_path, profile)

        ttk.Button(btn_frame, text="Queue for Processing",
                   command=on_queue).pack(side="right")

        # Center over main window
        win.update_idletasks()
        w  = win.winfo_reqwidth()
        h  = win.winfo_reqheight()
        mx = self.winfo_x() + (self.winfo_width()  - w) // 2
        my = self.winfo_y() + (self.winfo_height() - h) // 2
        win.geometry(f"+{mx}+{my}")

    def _queue_import(self, file_path: str, profile: dict):
        """
        Copy the imported file into the pending directory, write a sidecar,
        and enqueue it exactly like a normal recording.
        The original file is NOT moved or modified — only a copy goes to pending.
        """
        pd        = pending_dir()
        timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        dest      = pd / f"import_{timestamp}_{Path(file_path).name}"

        try:
            shutil.copy2(file_path, dest)
            write_sidecar(dest, "transcribing")
            log.info("Imported: %s -> %s", file_path, dest.name)
        except Exception as e:
            notify("Import Error", f"Could not copy file: {e}")
            log.error("Import failed: %s", e)
            return

        queued = state.job_queue.qsize()
        state.job_queue.put((str(dest), profile))

        if queued > 0:
            notify("Meeting Recorder", f"Import queued (position {queued + 1}).")
        else:
            self.after(0, lambda: self._set_status("processing", "Transcribing import..."))
            notify("Meeting Recorder", f"Processing: {Path(file_path).name}")

    # ── Resume pending ─────────────────────────────────────────────────────────
    def _on_resume(self):
        records = list_pending()
        if not records:
            notify("Meeting Recorder", "No pending recordings found.")
            return
        self._show_pending_picker(records)

    def _show_pending_picker(self, records: list[dict]):
        picker = PendingPicker(self, records)

        def on_response(dlg, response, record):
            dlg.destroy()
            if response == RT.OK and record:
                with state.lock:
                    state.processing = True
                Path(record["sidecar_path"]).unlink(missing_ok=True)

                transcript_path = record.get("transcript_path", "")
                if transcript_path and Path(transcript_path).exists():
                    saved_transcript = Path(transcript_path).read_text(encoding="utf-8")
                    Path(transcript_path).unlink(missing_ok=True)
                    self._set_status("processing", "Resuming from transcript...")
                    t = threading.Thread(
                        target=self._resume_from_transcript,
                        args=(record["audio_path"], saved_transcript),
                        daemon=True,
                    )
                else:
                    self._set_status("processing", "Resuming (re-transcribing)...")
                    t = threading.Thread(
                        target=self._process,
                        args=(record["audio_path"],),
                        daemon=True,
                    )
                t.start()

            elif response == RT.REJECT and record:
                discard_pending(record)
                pid_file_for(Path(record["audio_path"])).unlink(missing_ok=True)
                notify("Meeting Recorder", "Pending recording discarded.")
                self._refresh_resume_button()
                remaining = list_pending()
                if remaining:
                    self._show_pending_picker(remaining)

        picker.connect_response(on_response)
        picker.present()

    def _resume_from_transcript(self, audio_path: str, transcript: str,
                                 profile: dict | None = None):
        try:
            notify("Meeting Recorder", "Resuming from saved transcript...")
            self.after(0, lambda: self._set_status("processing", "Summarizing..."))

            while True:
                try:
                    summary = summarize(transcript, profile=profile)
                    break
                except Exception as e:
                    err = str(e).lower()
                    if any(k in err for k in ("credit balance", "too low", "billing", "payment", "402")):
                        retry_event = threading.Event()
                        self.after(0, lambda re=retry_event: self._show_funds_dialog(re, audio_path))
                        retry_event.wait()
                        if getattr(retry_event, "cancelled", False):
                            self.after(0, lambda: self._set_status("idle"))
                            return
                        self.after(0, lambda: self._set_status("processing", "Retrying..."))
                        continue
                    else:
                        raise

            self.after(0, lambda s=summary: self._pick_notebook_and_save(s))

        except Exception as e:
            log.error("Resume from transcript failed:\n%s", traceback.format_exc())
            short = str(e).split("\n")[0][:200]
            self.after(0, lambda msg=short: self._show_error_dialog(
                "Resume Error", f"{msg}\n\nFull details logged to:\n{LOG_FILE}"
            ))
            self.after(0, lambda: self._set_status("idle"))
        finally:
            with state.lock:
                state.processing = False
            Path(audio_path).unlink(missing_ok=True)

    # ── Startup pending check ──────────────────────────────────────────────────
    def _startup_pending_check(self):
        """Run after the event loop is up — matches the Linux post-window pending check."""
        pending = list_pending()
        if pending:
            self._show_pending_picker(pending)


# ── Entry point ───────────────────────────────────────────────────────────────
def _show_startup_error(title: str, body: str):
    """Show a plain tkinter error dialog before the main window exists."""
    root = tk.Tk()
    root.withdraw()
    InfoDialog(root, title, body, [("Quit", RT.CLOSE)]).present()
    root.mainloop()


def main():
    missing = missing_env_vars()
    if missing:
        var_list = "\n".join(f"  - {v}" for v in missing)
        _show_startup_error(
            "Missing Environment Variables",
            f"The following environment variables are not set:\n\n{var_list}\n\n"
            "Edit meeting_recorder_launch.bat and relaunch.",
        )
        return

    if not joplin_is_reachable():
        # Retry loop handled by simple dialog before main window.
        root = tk.Tk()
        root.withdraw()

        def try_launch(attempt=0):
            if joplin_is_reachable():
                root.destroy()
                _launch_main_window()
            else:
                dialog = InfoDialog(
                    root, "Cannot Connect to Joplin",
                    "The app could not reach Joplin on localhost:41184.\n\n"
                    "To fix this:\n"
                    "  1. Open Joplin.\n"
                    "  2. Go to Tools -> Options -> Web Clipper.\n"
                    "  3. Enable the Web Clipper service.\n"
                    "  4. Click Retry below.",
                    [("Quit", RT.CANCEL), ("Retry", RT.OK)],
                )
                def on_response(dlg, response):
                    dlg.destroy()
                    if response == RT.OK:
                        try_launch(attempt + 1)
                    else:
                        root.destroy()
                dialog.connect_response(on_response)
                dialog.present()
        try_launch()
        root.mainloop()
        return

    _launch_main_window()


def _launch_main_window():
    load_settings()
    kill_all_orphan_ffmpeg()
    win = MeetingRecorderWindow()
    win.mainloop()


if __name__ == "__main__":
    main()
