"""
RGBC Drive — Real-time Filesystem Watcher

Uses the `watchdog` library to catch file create/modify/delete events
inside the RGBC_Drive folder and queues them for processing.

Design decisions:
  - Debounces rapid events (e.g., large file writes trigger multiple
    MODIFIED events — we wait 3 seconds after the last event before
    processing).
  - Runs the hash+upload in a background thread so the watchdog
    observer thread is never blocked.
  - Uses the same scanner logic to hash only changed files and the
    same API client to check-then-upload.
"""

import os
import logging
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Optional

from watchdog.observers import Observer
from watchdog.events import (
    FileSystemEventHandler,
    FileCreatedEvent,
    FileModifiedEvent,
    FileDeletedEvent,
    FileMovedEvent,
)

from sync_db import SyncDatabase, PENDING_UPLOAD, DELETED_LOCAL
from scanner import DirectoryScanner
from api_client import APIClient

logger = logging.getLogger("RGBCDrive.Watcher")

# Files to ignore (same as scanner.py)
_IGNORED_NAMES = {
    ".rgbc_sync.db", ".rgbc_sync.db-wal", ".rgbc_sync.db-shm",
    ".rgbc_drive.conf", "desktop.ini", "Thumbs.db", ".DS_Store",
}
_IGNORED_PREFIXES = ("~$", ".")
_IGNORED_SUFFIXES = (".tmp", ".crdownload", ".partial", ".swp")


class _DebouncedAction:
    """
    Debounce mechanism: waits `delay` seconds after the LAST call
    before executing the action. If called again before the timer
    fires, the timer resets.
    """

    def __init__(self, delay: float, callback):
        self.delay = delay
        self.callback = callback
        self._timer: Optional[threading.Timer] = None
        self._lock = threading.Lock()

    def trigger(self, *args, **kwargs):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
            self._timer = threading.Timer(
                self.delay, self.callback, args=args, kwargs=kwargs
            )
            self._timer.daemon = True
            self._timer.start()

    def cancel(self):
        with self._lock:
            if self._timer is not None:
                self._timer.cancel()
                self._timer = None


class SyncEventHandler(FileSystemEventHandler):
    """
    Handles filesystem events from watchdog and feeds them into
    the RGBC Drive sync pipeline.

    For each file event:
      CREATE/MODIFY → debounce 3s → hash → check server → upload if needed
      DELETE         → mark as DELETED_LOCAL in DB
      MOVE           → treat as DELETE(src) + CREATE(dst)
    """

    def __init__(
        self,
        sync_root: str,
        db: SyncDatabase,
        api_client: APIClient,
        upload_callback=None,
    ):
        super().__init__()
        self.sync_root = os.path.normpath(sync_root)
        self.db = db
        self.api_client = api_client
        self.upload_callback = upload_callback  # Called after successful upload

        # Per-file debounce timers: {abs_path: _DebouncedAction}
        self._debounce_map: dict[str, _DebouncedAction] = {}
        self._debounce_lock = threading.Lock()

    def on_created(self, event: FileCreatedEvent):
        if event.is_directory:
            return
        self._schedule_process(event.src_path)

    def on_modified(self, event: FileModifiedEvent):
        if event.is_directory:
            return
        self._schedule_process(event.src_path)

    def on_deleted(self, event: FileDeletedEvent):
        if event.is_directory:
            return
        self._handle_delete(event.src_path)

    def on_moved(self, event: FileMovedEvent):
        if event.is_directory:
            return
        self._handle_delete(event.src_path)
        self._schedule_process(event.dest_path)

    # ── Internal methods ─────────────────────────────────────────────

    def _to_relative(self, abs_path: str) -> Optional[str]:
        try:
            rel = os.path.relpath(abs_path, self.sync_root)
            if rel.startswith(".."):
                return None
            return str(PurePosixPath(Path(rel)))
        except ValueError:
            return None

    def _should_skip(self, abs_path: str) -> bool:
        basename = os.path.basename(abs_path)
        if basename in _IGNORED_NAMES:
            return True
        if any(basename.startswith(p) for p in _IGNORED_PREFIXES):
            return True
        if any(basename.endswith(s) for s in _IGNORED_SUFFIXES):
            return True
        return False

    def _schedule_process(self, abs_path: str):
        """Debounce file processing by 3 seconds."""
        if self._should_skip(abs_path):
            return

        with self._debounce_lock:
            if abs_path not in self._debounce_map:
                self._debounce_map[abs_path] = _DebouncedAction(
                    delay=3.0,
                    callback=self._process_file,
                )
            self._debounce_map[abs_path].trigger(abs_path)

    def _process_file(self, abs_path: str):
        """Hash a single file and upload if needed. Runs in timer thread."""
        try:
            if not os.path.isfile(abs_path):
                return

            rel_path = self._to_relative(abs_path)
            if rel_path is None:
                return

            stat = os.stat(abs_path)
            size = stat.st_size
            mtime_ns = stat.st_mtime_ns

            if size == 0:
                return

            # Compute SHA-256
            import hashlib
            sha = hashlib.sha256()
            with open(abs_path, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    sha.update(chunk)
            sha256 = sha.hexdigest()

            # Check if content actually changed vs what's in DB
            existing = self.db.get_local_file(rel_path)
            if existing and existing["sha256"] == sha256 and existing["sync_status"] == "SYNCED":
                # Same content, just mtime changed (e.g. file was touched)
                self.db.upsert_local_file(
                    relative_path=rel_path,
                    sha256=sha256,
                    size=size,
                    mtime_ns=mtime_ns,
                    sync_status=existing["sync_status"],
                )
                return

            # Mark as pending upload in DB
            self.db.upsert_local_file(
                relative_path=rel_path,
                sha256=sha256,
                size=size,
                mtime_ns=mtime_ns,
                sync_status=PENDING_UPLOAD,
            )

            # Check if server already has this hash (deduplicate)
            remote = self.api_client.check_hash_exists(sha256)
            if remote:
                server_id = str(remote.get("id", ""))
                self.db.mark_synced(rel_path, server_id)
                logger.info(f"⚡ Deduplicated (server already has): {rel_path}")
                return

            # Upload
            result = self.api_client.upload_file(abs_path, original_name=os.path.basename(abs_path))
            if result.success and result.server_file_id:
                self.db.mark_synced(rel_path, result.server_file_id)
                logger.info(f"✅ Uploaded via watcher: {rel_path}")
                if self.upload_callback:
                    self.upload_callback()
            else:
                logger.error(f"❌ Upload failed via watcher: {rel_path} — {result.error}")

        except PermissionError:
            logger.debug(f"File locked, will retry on next event: {abs_path}")
        except Exception as e:
            logger.error(f"Watcher process error for {abs_path}: {e}")

        finally:
            # Clean up debounce entry
            with self._debounce_lock:
                self._debounce_map.pop(abs_path, None)

    def _handle_delete(self, abs_path: str):
        """Mark a deleted file in the database."""
        if self._should_skip(abs_path):
            return

        rel_path = self._to_relative(abs_path)
        if rel_path is None:
            return

        existing = self.db.get_local_file(rel_path)
        if existing:
            self.db.mark_deleted(rel_path)
            logger.info(f"🗑️ Marked deleted: {rel_path}")


class FileWatcher:
    """
    High-level wrapper that creates the watchdog Observer and
    SyncEventHandler, and provides start/stop lifecycle.

    Usage:
        watcher = FileWatcher(sync_root, db, api_client)
        watcher.start()
        # ... later ...
        watcher.stop()
    """

    def __init__(
        self,
        sync_root: str,
        db: SyncDatabase,
        api_client: APIClient,
        upload_callback=None,
    ):
        self.sync_root = sync_root
        self.handler = SyncEventHandler(
            sync_root=sync_root,
            db=db,
            api_client=api_client,
            upload_callback=upload_callback,
        )
        self.observer = Observer()
        self._running = False

    def start(self):
        """Start watching the directory tree."""
        if self._running:
            return
        self.observer.schedule(self.handler, self.sync_root, recursive=True)
        self.observer.start()
        self._running = True
        logger.info(f"👁️ Watcher started: {self.sync_root}")

    def stop(self):
        """Stop watching."""
        if not self._running:
            return
        self.observer.stop()
        self.observer.join(timeout=5)
        self._running = False
        logger.info("👁️ Watcher stopped")

    @property
    def is_running(self) -> bool:
        return self._running