#!/usr/bin/env python3
"""
Real-time Folder Sync Application
Monitors a source folder and syncs all file changes to a destination folder.
Supports system tray for background running.
"""

import os
import sys
import time
import json
import queue
import shutil
import argparse
import threading
from pathlib import Path
from datetime import datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, scrolledtext

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

import pystray
from PIL import Image, ImageDraw

# ── constants ──────────────────────────────────────────────────────────────
SETTINGS_FILE = Path(__file__).parent / "sync_settings.json"
IGNORE_PATTERNS = {"Thumbs.db", ".DS_Store", "desktop.ini", "~$.*", "*.tmp", ".sync_*"}

# ── helpers ────────────────────────────────────────────────────────────────
CHUNK_SIZE = 16 * 1024 * 1024  # 16 MB per read/write chunk (for large files)
MIN_DEBOUNCE_SECONDS = 2  # don't re-sync same file within this window

def _should_ignore(name: str) -> bool:
    import fnmatch
    for pat in IGNORE_PATTERNS:
        if fnmatch.fnmatch(name, pat):
            return True
    return False


def _ensure_dir(path: Path):
    """Create directory if it doesn't exist."""
    if not path.exists():
        path.mkdir(parents=True, exist_ok=True)


def _full_copy_file(src: Path, dst: Path) -> int:
    """Copy entire file in chunks. Returns final file size. Retries on lock errors."""
    _ensure_dir(dst.parent)
    for attempt in range(5):
        try:
            with open(src, "rb") as fsrc, open(dst, "wb") as fdst:
                total = 0
                while True:
                    chunk = fsrc.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    fdst.write(chunk)
                    total += len(chunk)
            shutil.copystat(src, dst)
            return total
        except (PermissionError, OSError):
            if attempt == 4:
                raise
            time.sleep(0.3 * (2 ** attempt))


def _delta_append(src: Path, dst: Path, offset: int) -> int:
    """Append bytes from src[offset:] to dst. Returns new total size. Retries on lock."""
    for attempt in range(5):
        try:
            with open(src, "rb") as fsrc, open(dst, "ab") as fdst:
                fsrc.seek(offset)
                written = 0
                while True:
                    chunk = fsrc.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    fdst.write(chunk)
                    written += len(chunk)
            shutil.copystat(src, dst)
            return offset + written
        except (PermissionError, OSError):
            if attempt == 4:
                raise
            time.sleep(0.3 * (2 ** attempt))


def _deep_copy_dir(src: Path, dst: Path):
    """Deep copy entire directory tree (no symlinks)."""
    if dst.exists():
        # Remove existing destination so copytree can replace it
        shutil.rmtree(dst, ignore_errors=True)
    shutil.copytree(
        src, dst,
        symlinks=False,
        copy_function=shutil.copy2,
        ignore=lambda d, names: {n for n in names if _should_ignore(n)},
        dirs_exist_ok=True,
    )


def _deep_delete(path: Path):
    """Delete a file or directory (real delete, not recycle bin)."""
    if not path.exists():
        return
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)


# ── settings persistence ───────────────────────────────────────────────────
def load_settings() -> dict:
    if SETTINGS_FILE.exists():
        try:
            return json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_settings(settings: dict):
    SETTINGS_FILE.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")


# ── sync engine ────────────────────────────────────────────────────────────
class SyncEngine:
    """Handles the actual file operations for syncing.

    Uses delta-copy for files that grow via appends (logs, downloads, recordings):
    only the new bytes are copied, not the entire file. Files modified in-place
    or truncated trigger a full re-copy. A per-file debounce prevents redundant
    syncs from rapid successive watchdog events.
    """

    def __init__(self, src: str, dst: str, sync_deletions: bool = False, log_queue: queue.Queue = None):
        self.src = Path(src).resolve()
        self.dst = Path(dst).resolve()
        self.sync_deletions = sync_deletions
        self.log_queue = log_queue
        self._lock = threading.Lock()
        # Tracks last-synced state per source file: {str(absolute_path): {"size": int, "mtime": float}}
        self._file_state: dict = {}
        # Debounce timestamps: {str(absolute_path): monotonic_seconds}
        self._last_sync_time: dict = {}

    def _log(self, msg: str):
        ts = datetime.now().strftime("%H:%M:%S")
        line = f"[{ts}] {msg}"
        if self.log_queue:
            self.log_queue.put(line)
        print(line)

    def _record_state(self, src_path: Path, size: int, mtime: float):
        self._file_state[str(src_path)] = {"size": size, "mtime": mtime}

    def _clear_state(self, src_path: Path):
        key = str(src_path)
        self._file_state.pop(key, None)
        self._last_sync_time.pop(key, None)

    def _should_debounce(self, src_path: Path) -> bool:
        """Return True if this file was synced too recently."""
        now = time.monotonic()
        last = self._last_sync_time.get(str(src_path), 0)
        return (now - last) < MIN_DEBOUNCE_SECONDS

    def _mark_synced(self, src_path: Path):
        self._last_sync_time[str(src_path)] = time.monotonic()

    # ── smart copy ──────────────────────────────────────────────────────
    def _sync_file(self, src_file: Path, dst_file: Path):
        """Copy src_file -> dst_file using delta-copy when possible."""
        src_key = str(src_file)

        if self._should_debounce(src_file):
            return False  # skipped due to debounce

        if not src_file.exists():
            return False

        st = src_file.stat()
        cur_size, cur_mtime = st.st_size, st.st_mtime

        prev = self._file_state.get(src_key)
        self._mark_synced(src_file)

        if prev is None or not dst_file.exists():
            # First sync or dest missing: full copy
            self._full_copy_and_record(src_file, dst_file, cur_size, cur_mtime)
            return True

        prev_size = prev["size"]

        if cur_size > prev_size and cur_mtime >= prev.get("mtime", 0):
            # File grew (append-only): copy only the delta
            delta = cur_size - prev_size
            new_size = _delta_append(src_file, dst_file, prev_size)
            self._record_state(src_file, new_size, cur_mtime)
            return True
        elif cur_size != prev_size or cur_mtime > prev.get("mtime", 0):
            # In-place modification or truncation: full re-copy
            self._full_copy_and_record(src_file, dst_file, cur_size, cur_mtime)
            return True
        # else: no change (debounce/duplicate event)

    def _full_copy_and_record(self, src_file: Path, dst_file: Path, size: int, mtime: float):
        final_size = _full_copy_file(src_file, dst_file)
        self._record_state(src_file, final_size, mtime)

    # ── initial sync ────────────────────────────────────────────────────
    def initial_sync(self):
        """Perform a full one-way sync from src to dst."""
        self._log("Starting initial full sync…")
        if not self.src.exists():
            self._log(f"ERROR: Source folder does not exist: {self.src}")
            return
        _ensure_dir(self.dst)

        with self._lock:
            for root, dirs, files in os.walk(self.src):
                root_path = Path(root)
                rel = root_path.relative_to(self.src)
                dst_root = self.dst / rel
                _ensure_dir(dst_root)

                for fname in files:
                    if _should_ignore(fname):
                        continue
                    src_file = root_path / fname
                    dst_file = dst_root / fname
                    try:
                        st = src_file.stat()
                        if not dst_file.exists() or st.st_mtime != dst_file.stat().st_mtime:
                            _full_copy_file(src_file, dst_file)
                            self._record_state(src_file, st.st_size, st.st_mtime)
                            self._log(f"COPY  {rel / fname}")
                    except Exception as e:
                        self._log(f"ERROR copying {rel / fname}: {e}")

        if self.sync_deletions:
            with self._lock:
                self._remove_orphans_from_dest()
        self._log("Initial sync complete.")

    def _remove_orphans_from_dest(self):
        """Remove files/dirs in dest that don't exist in source."""
        for root, dirs, files in os.walk(self.dst, topdown=False):
            root_path = Path(root)
            rel = root_path.relative_to(self.dst)
            src_root = self.src / rel

            for fname in files:
                if _should_ignore(fname):
                    continue
                if not (src_root / fname).exists():
                    try:
                        _deep_delete(root_path / fname)
                        self._clear_state(src_root / fname)
                        self._log(f"DEL   {rel / fname}")
                    except Exception as e:
                        self._log(f"ERROR deleting {rel / fname}: {e}")

            for dname in dirs:
                if _should_ignore(dname):
                    continue
                if not (src_root / dname).exists():
                    try:
                        _deep_delete(root_path / dname)
                        self._clear_state(src_root / dname)
                        self._log(f"DEL   {rel / dname}")
                    except Exception as e:
                        self._log(f"ERROR deleting dir {rel / dname}: {e}")

    # ── event handler ───────────────────────────────────────────────────
    def handle_event(self, event_type: str, src_path: str, is_dir: bool, dest_path: str = ""):
        """Handle a file system event from watchdog."""
        sp = Path(src_path).resolve()
        try:
            rel = sp.relative_to(self.src)
        except ValueError:
            return
        if _should_ignore(sp.name):
            return

        dp = self.dst / rel

        if event_type == "moved":
            with self._lock:
                self._handle_moved(sp, dp, rel, is_dir, dest_path)
        elif event_type == "created":
            if not sp.exists():
                return
            if is_dir:
                _ensure_dir(dp)
                self._log(f"NEWDIR  {rel}")
            else:
                try:
                    st = sp.stat()
                    self._full_copy_and_record(sp, dp, st.st_size, st.st_mtime)
                    self._mark_synced(sp)
                    self._log(f"NEW  {rel}")
                except Exception as e:
                    self._log(f"ERROR {rel}: {e}")
        elif event_type == "modified":
            if is_dir or not sp.exists():
                return
            try:
                copied = self._sync_file(sp, dp)
                if copied:
                    self._log(f"SYNC  {rel}")
            except Exception as e:
                self._log(f"ERROR {rel}: {e}")
        elif event_type == "deleted":
            if self.sync_deletions and dp.exists():
                with self._lock:
                    try:
                        _deep_delete(dp)
                        self._clear_state(sp)
                        self._log(f"DEL   {rel}")
                    except Exception as e:
                        self._log(f"ERROR deleting {rel}: {e}")

    def _handle_moved(self, sp: Path, dp: Path, rel, is_dir: bool, dest_path: str):
        """Handle move/rename events (called under lock)."""
        try:
            if dest_path:
                dp_new = self.dst / Path(dest_path).resolve().relative_to(self.src)
            else:
                dp_new = None
            if sp.exists():
                if is_dir:
                    _ensure_dir(dp)
                else:
                    _full_copy_file(sp, dp)
            if dp.exists() and self.sync_deletions:
                _deep_delete(dp)
                self._clear_state(sp)
                self._log(f"MOVED  {rel} -> {dp_new.relative_to(self.dst) if dp_new else '?'}")
            if dp_new and dp_new != dp and Path(dest_path).exists():
                if Path(dest_path).is_dir():
                    _ensure_dir(dp_new)
                else:
                    _full_copy_file(Path(dest_path), dp_new)
        except Exception as e:
            self._log(f"ERROR in move {rel}: {e}")


# ── watchdog handler ───────────────────────────────────────────────────────
class SyncEventHandler(FileSystemEventHandler):
    """Watchdog event handler that forwards events to the sync engine."""

    def __init__(self, engine: SyncEngine):
        super().__init__()
        self.engine = engine

    def on_created(self, event):
        self.engine.handle_event("created", event.src_path, event.is_directory)

    def on_modified(self, event):
        # Skip directory modified events (they fire whenever a file inside changes)
        if event.is_directory:
            return
        self.engine.handle_event("modified", event.src_path, False)

    def on_deleted(self, event):
        self.engine.handle_event("deleted", event.src_path, event.is_directory)

    def on_moved(self, event):
        self.engine.handle_event("moved", event.src_path, event.is_directory, event.dest_path)


# ── sync monitor (observer thread) ─────────────────────────────────────────
class SyncMonitor:
    """Manages the watchdog observer in a background thread."""

    def __init__(self, engine: SyncEngine):
        self.engine = engine
        self.observer: Observer = None
        self._running = False

    def start(self):
        if self._running:
            return
        self.observer = Observer()
        handler = SyncEventHandler(self.engine)
        self.observer.schedule(handler, str(self.engine.src), recursive=True)
        self.observer.start()
        self._running = True

    def stop(self):
        self._running = False
        if self.observer:
            self.observer.stop()
            self.observer.join(timeout=3)
            self.observer = None


# ── tray icon ──────────────────────────────────────────────────────────────
def _make_tray_icon_image():
    """Draw a sync icon — two curved arrows forming a circle."""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    # Circular arrow outline (clockwise)
    draw.arc([6, 6, size - 6, size - 6], start=220, end=360, fill="#4CAF50", width=5)
    draw.arc([6, 6, size - 6, size - 6], start=40, end=180, fill="#4CAF50", width=5)
    # Arrowhead at top-right
    draw.polygon([(size - 8, 16), (size - 4, 6), (size - 16, 12)], fill="#4CAF50")
    # Arrowhead at bottom-left
    draw.polygon([(8, size - 16), (4, size - 6), (16, size - 12)], fill="#4CAF50")
    # Center dot
    draw.ellipse([26, 26, 38, 38], fill="#4CAF50")
    return img


class TrayIcon:
    """System tray icon for background operation."""

    def __init__(self, app):
        self.app = app
        self.icon: pystray.Icon = None

    def _menu(self):
        return pystray.Menu(
            pystray.MenuItem("Show", self._on_show, default=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Exit", self._on_exit),
        )

    def _on_show(self):
        self.app.show_window()

    def _on_exit(self):
        self.app.quit_app()

    def start(self):
        self.icon = pystray.Icon(
            "sync_folder",
            _make_tray_icon_image(),
            "Folder Sync",
            menu=self._menu(),
        )
        t = threading.Thread(target=self.icon.run, daemon=True)
        t.start()

    def stop(self):
        if self.icon:
            self.icon.stop()


# ── main GUI window ────────────────────────────────────────────────────────
class MainWindow:
    """The tkinter GUI for the folder sync application."""

    def __init__(self, root: tk.Tk, app):
        self.root = root
        self.app = app
        self._build_ui()
        self._load_saved_settings()

    def _build_ui(self):
        self.root.title("Folder Sync")
        self.root.geometry("680x520")
        self.root.minsize(580, 400)

        # Style
        style = ttk.Style()
        if "clam" in style.theme_names():
            style.theme_use("clam")

        # ── main frame ──
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill=tk.BOTH, expand=True)

        # ── source folder ──
        ttk.Label(main, text="Source Folder:", font=("", 10, "bold")).grid(
            row=0, column=0, sticky="w", pady=(0, 2)
        )
        src_frame = ttk.Frame(main)
        src_frame.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        src_frame.columnconfigure(0, weight=1)
        self.src_var = tk.StringVar()
        self.src_entry = ttk.Entry(src_frame, textvariable=self.src_var)
        self.src_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(src_frame, text="Browse…", command=self._browse_src).grid(
            row=0, column=1, padx=(6, 0)
        )

        # ── destination folder ──
        ttk.Label(main, text="Destination Folder:", font=("", 10, "bold")).grid(
            row=2, column=0, sticky="w", pady=(0, 2)
        )
        dst_frame = ttk.Frame(main)
        dst_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        dst_frame.columnconfigure(0, weight=1)
        self.dst_var = tk.StringVar()
        self.dst_entry = ttk.Entry(dst_frame, textvariable=self.dst_var)
        self.dst_entry.grid(row=0, column=0, sticky="ew")
        ttk.Button(dst_frame, text="Browse…", command=self._browse_dst).grid(
            row=0, column=1, padx=(6, 0)
        )

        # ── options ──
        opts_frame = ttk.Frame(main)
        opts_frame.grid(row=4, column=0, sticky="w", pady=(0, 8))

        self.sync_deletions_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(
            opts_frame,
            text="Sync deletions (remove files in dest that were deleted in source)",
            variable=self.sync_deletions_var,
            command=self._on_deletions_toggle,
        ).pack(anchor="w")

        # ── control buttons ──
        ctrl_frame = ttk.Frame(main)
        ctrl_frame.grid(row=5, column=0, sticky="w", pady=(0, 10))

        self.start_btn = ttk.Button(ctrl_frame, text="Start Sync", command=self._on_start)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.stop_btn = ttk.Button(ctrl_frame, text="Stop Sync", command=self._on_stop, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, padx=(0, 8))

        self.hide_btn = ttk.Button(ctrl_frame, text="Hide to Tray", command=self._hide_to_tray)
        self.hide_btn.pack(side=tk.LEFT)

        # ── status indicator ──
        status_frame = ttk.Frame(main)
        status_frame.grid(row=6, column=0, sticky="w", pady=(0, 8))

        self.status_canvas = tk.Canvas(status_frame, width=14, height=14, highlightthickness=0)
        self.status_canvas.pack(side=tk.LEFT)
        self.status_dot = self.status_canvas.create_oval(2, 2, 12, 12, fill="#9E9E9E", outline="")

        self.status_label = ttk.Label(status_frame, text="Stopped", font=("", 9))
        self.status_label.pack(side=tk.LEFT, padx=(6, 0))

        # ── log area ──
        ttk.Label(main, text="Sync Log:", font=("", 10, "bold")).grid(
            row=7, column=0, sticky="w", pady=(0, 2)
        )
        log_frame = ttk.Frame(main)
        log_frame.grid(row=8, column=0, columnspan=2, sticky="nsew")
        log_frame.rowconfigure(0, weight=1)
        log_frame.columnconfigure(0, weight=1)

        self.log_text = tk.Text(
            log_frame,
            height=14,
            wrap=tk.WORD,
            font=("Consolas", 9),
            bg="#1E1E1E",
            fg="#D4D4D4",
            insertbackground="#D4D4D4",
            state=tk.DISABLED,
        )
        log_scroll = ttk.Scrollbar(log_frame, orient=tk.VERTICAL, command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll.grid(row=0, column=1, sticky="ns")

        # ── clear log button ──
        clear_btn = ttk.Button(main, text="Clear Log", command=self._clear_log)
        clear_btn.grid(row=9, column=0, sticky="w", pady=(4, 0))

        # ── signature ──
        sig_label = ttk.Label(main, text="v1.0  by.PigGeorge", font=("", 8), foreground="#888888")
        sig_label.grid(row=9, column=1, sticky="e", pady=(4, 0))

        # Configure grid weights
        main.rowconfigure(8, weight=1)
        main.columnconfigure(0, weight=1)

        # ── window close → minimize to tray ──
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        # ── log queue for thread-safe log updates ──
        self.log_queue = queue.Queue()
        self._poll_log_queue()

    def _poll_log_queue(self):
        """Poll the log queue for new messages from background threads."""
        try:
            while True:
                msg = self.log_queue.get_nowait()
                self._append_log(msg)
        except queue.Empty:
            pass
        self.root.after(200, self._poll_log_queue)

    def _append_log(self, msg: str):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.insert(tk.END, msg + "\n")
        self.log_text.see(tk.END)
        self.log_text.configure(state=tk.DISABLED)

    def _clear_log(self):
        self.log_text.configure(state=tk.NORMAL)
        self.log_text.delete("1.0", tk.END)
        self.log_text.configure(state=tk.DISABLED)

    # ── folder browsing ──
    def _browse_src(self):
        path = filedialog.askdirectory(title="Select Source Folder")
        if path:
            self.src_var.set(path)
            self._save_settings()

    def _browse_dst(self):
        path = filedialog.askdirectory(title="Select Destination Folder")
        if path:
            self.dst_var.set(path)
            self._save_settings()

    # ── settings ──
    def _load_saved_settings(self):
        s = load_settings()
        self.src_var.set(s.get("src", ""))
        self.dst_var.set(s.get("dst", ""))
        self.sync_deletions_var.set(s.get("sync_deletions", False))

    def _save_settings(self):
        save_settings({
            "src": self.src_var.get(),
            "dst": self.dst_var.get(),
            "sync_deletions": self.sync_deletions_var.get(),
        })

    def _on_deletions_toggle(self):
        self._save_settings()
        if self.app.engine:
            self.app.engine.sync_deletions = self.sync_deletions_var.get()

    # ── start / stop ──
    def _on_start(self):
        src = self.src_var.get().strip()
        dst = self.dst_var.get().strip()

        if not src:
            messagebox.showwarning("Missing Folder", "Please select a source folder.")
            return
        if not dst:
            messagebox.showwarning("Missing Folder", "Please select a destination folder.")
            return

        src_path = Path(src)
        if not src_path.exists() or not src_path.is_dir():
            messagebox.showerror("Invalid Folder", f"Source folder does not exist:\n{src}")
            return

        # Prevent syncing to a subfolder of source (would cause infinite loops)
        try:
            src_resolved = src_path.resolve()
            dst_resolved = Path(dst).resolve()
            if dst_resolved.is_relative_to(src_resolved):
                messagebox.showerror(
                    "Invalid Destination",
                    "Destination folder cannot be inside the source folder.\n"
                    "This would cause an infinite sync loop."
                )
                return
        except Exception:
            pass

        self._save_settings()
        self.app.start_sync(src, dst, self.sync_deletions_var.get(), self.log_queue)
        self._set_status(True)

    def _on_stop(self):
        self.app.stop_sync()
        self._set_status(False)

    def _set_status(self, running: bool):
        if running:
            self.status_canvas.itemconfig(self.status_dot, fill="#4CAF50")
            self.status_label.configure(text="Syncing…")
            self.start_btn.configure(state=tk.DISABLED)
            self.stop_btn.configure(state=tk.NORMAL)
        else:
            self.status_canvas.itemconfig(self.status_dot, fill="#9E9E9E")
            self.status_label.configure(text="Stopped")
            self.start_btn.configure(state=tk.NORMAL)
            self.stop_btn.configure(state=tk.DISABLED)

    # ── window management ──
    def _on_window_close(self):
        """Minimize to tray instead of closing."""
        self.root.withdraw()

    def _hide_to_tray(self):
        """Hide window to system tray."""
        self.root.withdraw()

    def show(self):
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self):
        self.root.withdraw()


# ── application coordinator ────────────────────────────────────────────────
class App:
    """Top-level application coordinator."""

    def __init__(self, start_in_tray: bool = False, auto_start: bool = False):
        self.root = tk.Tk()
        self.engine: SyncEngine = None
        self.monitor: SyncMonitor = None
        self.tray: TrayIcon = None
        self._tray_started = False
        self.window = MainWindow(self.root, self)

        # Always create tray icon so we can minimize to it
        self.start_tray()

        if start_in_tray:
            self.root.withdraw()  # hide window on start
        if auto_start:
            self._auto_start_sync()

    def _auto_start_sync(self):
        """Try to auto-start sync using saved settings."""
        settings = load_settings()
        src = settings.get("src", "").strip()
        dst = settings.get("dst", "").strip()
        sync_del = settings.get("sync_deletions", False)
        if src and dst and Path(src).exists():
            self.start_sync(src, dst, sync_del, self.window.log_queue)
            self.window._set_status(True)

    def start_sync(self, src: str, dst: str, sync_deletions: bool, log_queue: queue.Queue):
        """Start monitoring and syncing."""
        # Stop any existing sync first
        self.stop_sync()

        self.engine = SyncEngine(src, dst, sync_deletions, log_queue)
        self.monitor = SyncMonitor(self.engine)

        # Run initial sync in background
        t = threading.Thread(target=self.engine.initial_sync, daemon=True)
        t.start()

        # Start real-time monitoring
        self.monitor.start()

    def stop_sync(self):
        """Stop monitoring."""
        if self.monitor:
            self.monitor.stop()
            self.monitor = None
        self.engine = None

    def start_tray(self):
        """Create and show the system tray icon."""
        if self.tray:
            return
        self.tray = TrayIcon(self)
        self.tray.start()

    def show_window(self):
        self.window.show()

    def quit_app(self):
        """Fully exit the application."""
        self.stop_sync()
        if self.tray:
            self.tray.stop()
            self.tray = None
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description="Real-time Folder Sync")
    parser.add_argument("--tray", action="store_true",
                        help="Start minimized to system tray (no window)")
    parser.add_argument("--autostart", action="store_true",
                        help="Auto-start syncing with last saved settings")
    args = parser.parse_args()

    app = App(start_in_tray=args.tray, auto_start=args.autostart)
    try:
        app.root.mainloop()
    except KeyboardInterrupt:
        app.quit_app()


if __name__ == "__main__":
    main()
