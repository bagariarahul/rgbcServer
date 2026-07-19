"""
RGBC Drive — Bidirectional Sync Poller

Background thread that periodically:
  1. Fetches the server's file manifest (GET /api/files/list)
  2. Compares against the local server_files cache in SQLite
  3. Downloads any NEW remote files into the local RGBC_Drive folder
  4. Uploads any pending local files that haven't been synced yet

This completes the bidirectional loop:
  Local changes  → Watcher → Upload  (push)
  Remote changes → Poller  → Download (pull)
"""

import os
import hashlib
import logging
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from sync_db import SyncDatabase, PENDING_UPLOAD, SYNCED
from api_client import APIClient

logger = logging.getLogger("RGBCDrive.Poller")


class SyncPoller:
    """
    Background poller for bidirectional sync.

    Runs on a daemon thread with configurable interval.
    Thread-safe via SyncDatabase's per-thread connections.
    """

    def __init__(
        self,
        sync_root: str,
        db: SyncDatabase,
        api_client: APIClient,
        poll_interval: int = 60,
        status_callback=None,
    ):
        self.sync_root = os.path.normpath(sync_root)
        self.db = db
        self.api = api_client
        self.poll_interval = poll_interval
        self.status_callback = status_callback  # Called with status string

        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._is_syncing = False

    @property
    def is_syncing(self) -> bool:
        return self._is_syncing

    def start(self):
        """Start the poller background thread."""
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True, name="SyncPoller")
        self._thread.start()
        logger.info(f"🔄 Poller started (interval: {self.poll_interval}s)")

    def stop(self):
        """Stop the poller gracefully."""
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=10)
        logger.info("🔄 Poller stopped")

    def force_sync(self):
        """Trigger an immediate sync cycle (non-blocking)."""
        threading.Thread(target=self._sync_cycle, daemon=True, name="ForceSync").start()

    def _run_loop(self):
        """Main poller loop: sync → sleep → repeat."""
        while not self._stop_event.is_set():
            try:
                self._sync_cycle()
            except Exception as e:
                logger.error(f"Poller cycle error: {e}")

            # Sleep in 1-second increments so we can exit quickly
            for _ in range(self.poll_interval):
                if self._stop_event.is_set():
                    return
                time.sleep(1)

    def _sync_cycle(self):
        """One full sync cycle: push pending → pull new remote files."""
        self._is_syncing = True
        self._set_status("Syncing...")

        try:
            # # ── Phase 1: PUSH — upload any pending local files ───────
            # pending = self.db.get_pending_uploads()
            # if pending:
            #     logger.info(f"📤 Pushing {len(pending)} pending files")
            #     for row in pending:
            #         if self._stop_event.is_set():
            #             return

            #         rel_path = row["relative_path"]
            #         abs_path = os.path.join(self.sync_root, rel_path.replace("/", os.sep))

            #         if not os.path.isfile(abs_path):
            #             # File was deleted between scan and upload
            #             self.db.remove_local_file(rel_path)
            #             continue

            #         sha256 = row["sha256"]

            #         # Deduplicate: check if server already has this hash
            #         remote = self.api.check_hash_exists(sha256)
            #         if remote:
            #             server_id = str(remote.get("id", ""))
            #             self.db.mark_synced(rel_path, server_id)
            #             logger.info(f"  ⚡ Dedup: {rel_path}")
            #             continue

            #         # Upload
            #         result = self.api.upload_file(
            #             abs_path,
            #             original_name=os.path.basename(abs_path),
            #         )
            #         if result.success and result.server_file_id:
            #             self.db.mark_synced(rel_path, result.server_file_id)
            #         else:
            #             logger.warning(f"  ❌ Upload failed: {rel_path} — {result.error}")
            # ── Phase 1: PUSH — DISABLED (Sprint 3.8) ────────────────
            # RGBC is pure peer-to-peer: files flow phone → this master and stop
            # here. The master does NOT push its files up to the gateway — that
            # would make the gateway a central store, which is exactly the
            # "we never see your files" guarantee we're keeping. This push phase
            # was causing the master to hammer the gateway (HTTP 429 loop) and
            # was re-filling the server with files after every P2P upload.
            #
            # DO NOT re-enable without the opt-in redundant-server design
            # (Sprint 99: custom/Dropbox-style backup target). The code is
            # preserved in git history if that feature is built.
            #
            # Pull (Phase 2 below) stays ON — that's how phone-pushed files are
            # reconciled and how restore works.
            pass

            # ── Phase 2: PULL — fetch server manifest and download ───
            logger.debug("📥 Fetching server file manifest")
            remote_files = self.api.list_files(limit=100)

            if not remote_files:
                logger.debug("No remote files or server unreachable")
                self._set_status("Up to date")
                return

            # Get set of server IDs we already know about
            known_server_ids = self.db.get_server_file_ids()
            # Get set of local checksums for dedup
            local_checksums = self._get_local_checksums()

            new_count = 0
            for rf in remote_files:
                if self._stop_event.is_set():
                    return

                # Cache in server_files table
                self.db.upsert_server_file(
                    server_file_id=rf.server_id,
                    original_name=rf.original_name,
                    file_size=rf.file_size,
                    checksum=rf.checksum,
                    uploaded_at=rf.uploaded_at,
                )

                # Skip if we already pulled this file
                if rf.server_id in known_server_ids:
                    continue

                # Skip if we already have a local file with the same checksum
                # (uploaded from this machine — no need to download our own files)
                if rf.checksum and rf.checksum in local_checksums:
                    self.db.mark_server_file_pulled(rf.server_id)
                    continue

                # ── New remote file: download it ─────────────────────
                dest_path = os.path.join(self.sync_root, rf.original_name)

                # Avoid overwriting existing files with different content
                if os.path.exists(dest_path):
                    existing_hash = self._hash_file(dest_path)
                    if existing_hash == rf.checksum:
                        # Same content already exists locally
                        self.db.mark_server_file_pulled(rf.server_id)
                        continue
                    else:
                        # Conflict: rename the download
                        base, ext = os.path.splitext(rf.original_name)
                        dest_path = os.path.join(
                            self.sync_root,
                            f"{base}_server{ext}"
                        )
                        logger.warning(f"  ⚠️ Conflict: downloading as {os.path.basename(dest_path)}")

                success = self.api.download_file(rf.server_id, dest_path)
                if success:
                    self.db.mark_server_file_pulled(rf.server_id)

                    # Index the downloaded file in local_files
                    stat = os.stat(dest_path)
                    dl_hash = self._hash_file(dest_path)
                    if dl_hash:
                        rel_path = rf.original_name
                        self.db.upsert_local_file(
                            relative_path=rel_path,
                            sha256=dl_hash,
                            size=stat.st_size,
                            mtime_ns=stat.st_mtime_ns,
                            server_file_id=rf.server_id,
                            sync_status=SYNCED,
                        )
                    new_count += 1
                    logger.info(f"  📥 Pulled: {rf.original_name}")

            # Update last poll time
            self.db.set_meta(
                "last_poll_time",
                datetime.now(timezone.utc).isoformat(),
            )

            # Set final status
            stats = self.db.get_stats()
            pending_count = stats["pending_upload"]
            if pending_count > 0:
                self._set_status(f"{pending_count} pending upload(s)")
            else:
                self._set_status("Up to date")

            if new_count > 0:
                logger.info(f"📥 Pulled {new_count} new file(s) from server")

        except Exception as e:
            logger.error(f"Sync cycle error: {e}")
            self._set_status("Sync error")
        finally:
            self._is_syncing = False

    def _get_local_checksums(self) -> set[str]:
        """Get all SHA-256 hashes from local_files table."""
        rows = self.db._conn.execute(
            "SELECT sha256 FROM local_files"
        ).fetchall()
        return {row["sha256"] for row in rows}

    @staticmethod
    def _hash_file(filepath: str) -> str | None:
        """Compute SHA-256 of a file."""
        try:
            sha = hashlib.sha256()
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(65536)
                    if not chunk:
                        break
                    sha.update(chunk)
            return sha.hexdigest()
        except Exception:
            return None

    def _set_status(self, status: str):
        """Update status via callback (used by tray icon)."""
        if self.status_callback:
            self.status_callback(status)