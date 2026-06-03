# Folder Sync

![Version](https://img.shields.io/badge/version-1.1-blue)
[![CI](https://github.com/Pig-George/FolderSync/actions/workflows/build.yml/badge.svg)](https://github.com/Pig-George/FolderSync/actions/workflows/build.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT)

Real-time folder synchronization tool with GUI, system tray support, and delta-copy for large files.

## Features

- **Real-time monitoring** via `watchdog` — syncs file changes (create/modify/delete/move) instantly
- **Delta-copy for large files** — appending files (logs, recordings, downloads) only transfer new bytes
- **System tray** — close window to hide to tray, sync continues in background
- **Sync-deletion toggle** — optionally mirror deletions from source to destination
- **Settings persistence** — last used folders and preferences auto-saved
- **Deep copy only** — never creates symlinks or shortcuts

## Quick Start

### Prerequisites
- Python 3.9+

### Setup
```bash
# Create virtual environment
python -m venv venv

# Activate (Windows)
venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

### Run from source
```bash
pythonw sync_app.py          # Windows (no console)
python sync_app.py           # Linux / macOS
```

### Run the packaged exe
```
dist/FolderSync.exe
```

### Background / auto-start
```powershell
# Start minimized to tray + auto-resume last sync
FolderSync.exe --tray --autostart
```

Add a shortcut with these flags to `shell:startup` for boot-time auto-start.

## Implementation Principles

### Architecture

```
┌─────────────┐     events      ┌──────────────────┐
│  watchdog   │ ───────────────>│ SyncEventHandler │
│  Observer   │   (file system) │ (on_created,      │
│  (thread)   │                 │  on_modified,     │
└─────────────┘                 │  on_deleted,      │
                                │  on_moved)        │
                                └────────┬─────────┘
                                         │
                                         v
                                ┌──────────────────┐
                                │   SyncEngine     │
                                │  - smart copy    │
                                │  - state tracking│
                                │  - debounce      │
                                └────────┬─────────┘
                                         │
                              ┌──────────┴──────────┐
                              │                     │
                              v                     v
                     ┌──────────────┐     ┌────────────────┐
                     │ _full_copy   │     │ _delta_append  │
                     │ (16MB chunks)│     │ (seek+append)  │
                     └──────────────┘     └────────────────┘
```

### Core Components

**1. File Monitor (watchdog)**

`SyncMonitor` runs a `watchdog.Observer` in a background thread, watching the source folder recursively. File system events are dispatched to `SyncEventHandler`, which forwards them to `SyncEngine`.

**2. Sync Engine**

`SyncEngine` is the core logic layer with four key mechanisms:

- **Smart copy decision** — on each file modification, compares current `(size, mtime)` against the last-synced state stored in `_file_state`:

| Condition | Strategy | Use case |
|---|---|---|
| First sync or dest missing | Full copy (chunked) | New files |
| `cur_size > last_size` AND `mtime >= last_mtime` | Delta-append | Appending logs, recordings, downloads |
| `cur_size != last_size` OR `mtime > last_mtime` | Full re-copy | In-place modification, truncation |

- **Chunked I/O** — both `_full_copy_file` and `_delta_append` use 16 MB read/write buffers. A 100 GB file never loads entirely into memory.

- **Retry with exponential backoff** — on `PermissionError` (file locked by another process), retries up to 5 times with delays of 0.3s → 0.6s → 1.2s → 2.4s → 4.8s.

- **Per-file debounce** — watchdog fires multiple events during active writes. Files are synced at most once per 2 seconds, based on `_last_sync_time` tracking.

**3. File Copy (deep copy)**

All copies use `open() + read/write` in binary mode — never `shutil.copy2` shortcuts:

```python
# Full copy
with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
    while chunk := fsrc.read(16 * 1024 * 1024):
        fdst.write(chunk)
shutil.copystat(src, dst)   # preserve timestamps

# Delta append
with open(src, "rb") as fsrc, open(dst, "ab") as fdst:
    fsrc.seek(last_synced_offset)
    while chunk := fsrc.read(16 * 1024 * 1024):
        fdst.write(chunk)
```

**4. System Tray**

`pystray` runs in a daemon thread with a PIL-generated icon. tkinter's `WM_DELETE_WINDOW` is intercepted to `withdraw()` instead of `destroy()`. The tray icon `--tray` flag starts directly in the background.

**5. Thread Safety**

- `SyncEngine._lock` serializes initial sync and delete/move operations
- File-level copy operations run without the lock (no shared mutable state)
- Log messages pass through `queue.Queue`, polled by tkinter's `after()` timer (200ms) for thread-safe GUI updates

### Initial Sync Flow

```
1. Walk source tree → for each file:
     if dst missing OR mtime differs → full copy
     record (size, mtime) to _file_state
2. If sync_deletions ON:
     Walk dest tree bottom-up → remove orphans
3. Start watchdog observer for real-time events
```

### Real-time Event Flow

```
watchdog event → handle_event()
  ├─ created  → full copy → record state
  ├─ modified → debounce check → smart copy (delta vs full)
  ├─ deleted  → if sync_deletions: rm dest → clear state
  └─ moved    → handle as delete(old) + copy(new)
```

## Project Structure

```
FolderSync/
├── sync_app.py                     # Main application (GUI + engine + tray)
├── run_sync_app.bat                # Normal launcher
├── run_sync_app_bg.bat             # Background launcher (--tray --autostart)
├── requirements.txt                # Python dependencies
├── FolderSync.spec                 # PyInstaller build spec
├── .gitignore
├── .github/workflows/build.yml     # CI/CD (build & release)
├── LICENSE                         # MIT License
└── README.md
```

## Dependencies

See `requirements.txt`:

- `watchdog` — file system monitoring
- `pystray` — system tray icon
- `Pillow` — tray icon generation
- `tkinter` — GUI (bundled with Python)
- `pyinstaller` — (optional) for building standalone exe

## Build

```bash
pip install -r requirements.txt
pyinstaller --onefile --windowed --name FolderSync \
  --hidden-import watchdog.observers \
  --hidden-import pystray._win32 \
  sync_app.py
```

Output: `dist/FolderSync.exe`

## License

This project is licensed under the MIT License — see the [LICENSE](LICENSE) file for details.

---

v1.1  by.PigGeorge
