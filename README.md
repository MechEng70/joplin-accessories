# Meeting Recorder

AI-powered meeting recorder with transcription, summarization, and Joplin integration. Supports back-to-back job queuing, crash recovery, summary profiles, and GPU-accelerated transcription via faster-whisper (large-v3).

---

## Platforms

| Platform | UI | Audio | Python |
|---|---|---|---|
| Linux (Fedora 43 / COSMIC) | GTK4 / Wayland | PipeWire / ffmpeg | 3.x |
| Windows | tkinter / ttk | WASAPI loopback (pyaudiowpatch) | 3.11 or 3.12 |

---

## Prerequisites

### Both platforms
- NVIDIA GPU with CUDA 12.x (required for faster-whisper large-v3)
- Anthropic API key
- Joplin desktop app with Web Clipper enabled (Tools > Web Clipper, port 41184)
- HuggingFace token (for faster-whisper model download)

### Linux only
- GTK4: `sudo dnf install python3-gobject gtk4`
- ffmpeg: `sudo dnf install ffmpeg`

### Windows only
- Python 3.11 or 3.12 (ctranslate2 4.7.1 does not support Python 3.14)
- CUDA 12.x toolkit (not 13.x)

---

## Installation

### Linux
```bash
cd linux
chmod +x install.sh
./install.sh
```

### Windows
```powershell
cd windows
.\install.ps1
```

Both installers will create a `.env` file at the app location and prompt you to fill in your keys.

---

## Configuration

Copy `.env.example` to `.env` in the app directory and populate:

```
ANTHROPIC_API_KEY=...
JOPLIN_TOKEN=...
HF_TOKEN=...
```

The `.env` file is loaded at launch. It is excluded from version control via `.gitignore`.

> **Note:** The Python scripts use `python-dotenv` to load `.env` on startup.  
> On Linux, the generated `meeting_recorder_launch.sh` also sources `.env` directly  
> for COSMIC launcher compatibility (COSMIC does not source `~/.zshrc`).

---

## Summary Profiles

`profiles.json` ships with 5 built-in locked profiles:

- Medical Device Meeting
- Client Call
- IP Assessment
- LinkedIn Post (Standard)
- LinkedIn Post (Technical)

Custom profiles can be added via the UI and are stored in the same file.

---

## Data Locations

| Platform | Path |
|---|---|
| Linux | `~/.local/share/meeting-recorder/` |
| Windows | `%APPDATA%\MeetingRecorder\` |

Pending jobs (WAV + JSON sidecars) are stored in the `pending/` subdirectory and are excluded from version control.

---

## Known Issues

- **Windows / ctranslate2:** CUDA 13.x is not supported. Use CUDA 12.x with ctranslate2 4.7.1.
- **Windows / transcription hang:** If transcription stalls near completion, ensure `condition_on_previous_text=False` is set in the `model.transcribe()` call.
- **Linux / COSMIC launcher:** `StartupWMClass` in the `.desktop` file must exactly match the `application_id` in `Gtk.Application`. After editing, run `update-desktop-database ~/.local/share/applications/`.
- **Speaker diarization:** pyannote.audio was evaluated and removed due to unresolvable telemetry hang issues.

---

## License

MIT
