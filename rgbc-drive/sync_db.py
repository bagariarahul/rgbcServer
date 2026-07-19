"""
RGBC Drive — Local SQLite State Manager

This is the "source of truth" for the sync engine. It tracks every file
in the local RGBC_Drive folder and avoids rehashing unchanged files.

Schema:
  local_files   — tracks local filesystem state (path, mtime, sha256, sync status)
  server_files  — cached manifest of what the server has (for bidirectional pull)
  sync_meta     — key-value store for global sync state (last poll time, etc.)

Design decisions:
  - Uses relative paths (forward slashes) so the DB is portable across machines
  - Stores mtime as nanoseconds (os.stat_result.st_mtime_ns) for sub-second precision
  - WAL journal mode for concurrent read/write safety (watchdog + scanner + poller)
"""

import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# ── Sync status constants ────────────────────────────────────────────────
SYNCED = "SYNCED"                   # Local file matches server
PENDING_UPLOAD = "PENDING_UPLOAD"   # Local file needs to be uploaded
PENDING_DOWNLOAD = "PENDING_DOWNLOAD"  # Server file needs to be downloaded
CONFLICT = "CONFLICT"               # Both local and server changed
DELETED_LOCAL = "DELETED_LOCAL"     # File was deleted locally, pending server delete


class SyncDatabase:
    """
    Thread-safe SQLite database for tracking sync state.

    Usage:
        db = SyncDatabase("/path/to/RGBC_Drive/.rgbc_sync.db")
        db.upsert_local_file("docs/report.pdf", sha256="abc...", size=1024, mtime_ns=...)
        record = db.get_local_file("docs/report.pdf")
    """

    # ── Schema version — bump this when tables change ────────────────
    SCHEMA_VERSION = 2

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._local = threading.local()
        self._init_db()

    @property
    def _conn(self) -> sqlite3.Connection:
        """One connection per thread (SQLite requirement for WAL mode)."""
        if not hasattr(self._local, "conn") or self._local.conn is None:
            conn = sqlite3.connect(self.db_path, timeout=30)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=5000")
            self._local.conn = conn
        return self._local.conn

    def _init_db(self):
        """Create tables if they don't exist."""
        c = self._conn
        c.executescript("""
            -- Tracks every file inside the local RGBC_Drive folder.
            -- Primary key is the relative path (forward slashes, case-sensitive).
            CREATE TABLE IF NOT EXISTS local_files (
                relative_path   TEXT PRIMARY KEY,
                sha256          TEXT NOT NULL,
                size            INTEGER NOT NULL,
                mtime_ns        INTEGER NOT NULL,
                server_file_id  TEXT,
                sync_status     TEXT NOT NULL DEFAULT 'PENDING_UPLOAD',
                last_synced_at  TEXT,
                created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
            );

            -- Index for finding files that need uploading
            CREATE INDEX IF NOT EXISTS idx_local_files_status
                ON local_files(sync_status);

            -- Index for looking up by server ID (used during pull/download)
            CREATE INDEX IF NOT EXISTS idx_local_files_server_id
                ON local_files(server_file_id);

            -- Cached manifest of what the server has.
            -- Populated by polling GET /api/files/list.
            -- Used to detect "server has file X that I don't have locally".
            CREATE TABLE IF NOT EXISTS server_files (
                server_file_id  TEXT PRIMARY KEY,
                original_name   TEXT NOT NULL,
                file_size       INTEGER NOT NULL,
                checksum        TEXT,
                uploaded_at     TEXT,
                pulled          INTEGER NOT NULL DEFAULT 0
            );

            -- Key-value store for global sync metadata.
         -- Key-value store for global sync metadata.
            CREATE TABLE IF NOT EXISTS sync_meta (
                key     TEXT PRIMARY KEY,
                value   TEXT
            );

            -- ── Sprint 3.3: in-progress chunked uploads ─────────────
            -- One row per upload session. Created by /upload/init,
            -- updated as chunks arrive, deleted on finalize/expiry.
            CREATE TABLE IF NOT EXISTS uploads_in_progress (
                upload_id        TEXT PRIMARY KEY,
                owner_user_id    TEXT NOT NULL,
                relative_path    TEXT NOT NULL,
                original_name    TEXT NOT NULL,
                total_size       INTEGER NOT NULL,
                total_sha256     TEXT NOT NULL,
                chunk_size       INTEGER NOT NULL,
                total_chunks     INTEGER NOT NULL,
                received_mask    BLOB NOT NULL,
                temp_dir         TEXT NOT NULL,
                mime_type        TEXT,
                created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')),
                expires_at       TEXT NOT NULL,
                last_chunk_at    TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_uploads_in_progress_expiry
                ON uploads_in_progress(expires_at);
            CREATE INDEX IF NOT EXISTS idx_uploads_in_progress_owner
                ON uploads_in_progress(owner_user_id);
        """)

        # Set schema version if not already set
        existing = self.get_meta("schema_version")
        if existing is None:
            self.set_meta("schema_version", str(self.SCHEMA_VERSION))

        c.commit()

    # ═══════════════════════════════════════════════════════════════════
    # LOCAL FILES CRUD
    # ═══════════════════════════════════════════════════════════════════

    def get_local_file(self, relative_path: str) -> Optional[sqlite3.Row]:
        """Get a local file record by relative path."""
        row = self._conn.execute(
            "SELECT * FROM local_files WHERE relative_path = ?",
            (relative_path,)
        ).fetchone()
        return row

    def upsert_local_file(
        self,
        relative_path: str,
        sha256: str,
        size: int,
        mtime_ns: int,
        server_file_id: Optional[str] = None,
        sync_status: str = PENDING_UPLOAD
    ) -> None:
        """Insert or update a local file record."""
        self._conn.execute("""
            INSERT INTO local_files (relative_path, sha256, size, mtime_ns, server_file_id, sync_status)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(relative_path) DO UPDATE SET
                sha256 = excluded.sha256,
                size = excluded.size,
                mtime_ns = excluded.mtime_ns,
                server_file_id = COALESCE(excluded.server_file_id, local_files.server_file_id),
                sync_status = excluded.sync_status
        """, (relative_path, sha256, size, mtime_ns, server_file_id, sync_status))
        self._conn.commit()

    def mark_synced(self, relative_path: str, server_file_id: str) -> None:
        """Mark a file as successfully synced."""
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute("""
            UPDATE local_files
            SET sync_status = ?, server_file_id = ?, last_synced_at = ?
            WHERE relative_path = ?
        """, (SYNCED, server_file_id, now, relative_path))
        self._conn.commit()

    def mark_deleted(self, relative_path: str) -> None:
        """Mark a file as deleted locally (pending server-side delete)."""
        self._conn.execute("""
            UPDATE local_files SET sync_status = ? WHERE relative_path = ?
        """, (DELETED_LOCAL, relative_path))
        self._conn.commit()

    def remove_local_file(self, relative_path: str) -> None:
        """Permanently remove a file record from the database."""
        self._conn.execute(
            "DELETE FROM local_files WHERE relative_path = ?",
            (relative_path,)
        )
        self._conn.commit()

    def get_pending_uploads(self) -> list[sqlite3.Row]:
        """Get all files that need to be uploaded."""
        return self._conn.execute(
            "SELECT * FROM local_files WHERE sync_status = ? ORDER BY size ASC",
            (PENDING_UPLOAD,)
        ).fetchall()

    def get_all_local_paths(self) -> set[str]:
        """Get a set of all tracked relative paths."""
        rows = self._conn.execute(
            "SELECT relative_path FROM local_files WHERE sync_status != ?",
            (DELETED_LOCAL,)
        ).fetchall()
        return {row["relative_path"] for row in rows}

    def get_local_file_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM local_files WHERE sync_status != ?",
            (DELETED_LOCAL,)
        ).fetchone()
        return row["cnt"] if row else 0

    def get_synced_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM local_files WHERE sync_status = ?",
            (SYNCED,)
        ).fetchone()
        return row["cnt"] if row else 0

    def get_pending_count(self) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) as cnt FROM local_files WHERE sync_status = ?",
            (PENDING_UPLOAD,)
        ).fetchone()
        return row["cnt"] if row else 0

    # ═══════════════════════════════════════════════════════════════════
    # SERVER FILES (cached manifest for bidirectional sync)
    # ═══════════════════════════════════════════════════════════════════

    def upsert_server_file(
        self,
        server_file_id: str,
        original_name: str,
        file_size: int,
        checksum: Optional[str] = None,
        uploaded_at: Optional[str] = None
    ) -> None:
        """Insert or update a server file record."""
        self._conn.execute("""
            INSERT INTO server_files (server_file_id, original_name, file_size, checksum, uploaded_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(server_file_id) DO UPDATE SET
                original_name = excluded.original_name,
                file_size = excluded.file_size,
                checksum = COALESCE(excluded.checksum, server_files.checksum),
                uploaded_at = COALESCE(excluded.uploaded_at, server_files.uploaded_at)
        """, (server_file_id, original_name, file_size, checksum, uploaded_at))
        self._conn.commit()

    def get_unpulled_server_files(self) -> list[sqlite3.Row]:
        """Get server files we haven't downloaded yet."""
        return self._conn.execute(
            "SELECT * FROM server_files WHERE pulled = 0 ORDER BY file_size ASC"
        ).fetchall()

    def mark_server_file_pulled(self, server_file_id: str) -> None:
        self._conn.execute(
            "UPDATE server_files SET pulled = 1 WHERE server_file_id = ?",
            (server_file_id,)
        )
        self._conn.commit()

    def get_server_file_ids(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT server_file_id FROM server_files"
        ).fetchall()
        return {row["server_file_id"] for row in rows}

    # ═══════════════════════════════════════════════════════════════════
    # SYNC METADATA (key-value store)
    # ═══════════════════════════════════════════════════════════════════

    def get_meta(self, key: str) -> Optional[str]:
        row = self._conn.execute(
            "SELECT value FROM sync_meta WHERE key = ?", (key,)
        ).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self._conn.execute("""
            INSERT INTO sync_meta (key, value) VALUES (?, ?)
            ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """, (key, value))
        self._conn.commit()

    # ═══════════════════════════════════════════════════════════════════
    # STATISTICS (for tray tooltip / logging)
    # ═══════════════════════════════════════════════════════════════════

    def get_stats(self) -> dict:
        """Return a summary dict for display."""
        total = self.get_local_file_count()
        synced = self.get_synced_count()
        pending = self.get_pending_count()
        total_size = self._conn.execute(
            "SELECT COALESCE(SUM(size), 0) as s FROM local_files WHERE sync_status != ?",
            (DELETED_LOCAL,)
        ).fetchone()["s"]

        return {
            "total_files": total,
            "synced": synced,
            "pending_upload": pending,
            "total_size_bytes": total_size,
        }
    
    def create_upload(
        self,
        upload_id: str,
        owner_user_id: str,
        relative_path: str,
        original_name: str,
        total_size: int,
        total_sha256: str,
        chunk_size: int,
        total_chunks: int,
        temp_dir: str,
        expires_at: str,
        mime_type: Optional[str] = None,
    ) -> None:
        """Create a new in-progress upload session."""
        # Bitmap with all bits = 0 (no chunks received yet).
        # Size in bytes = ceil(total_chunks / 8). For a 1 GB file with 50 MB
        # chunks that's 21 chunks → 3 bytes. Fits comfortably in a BLOB.
        mask_bytes = (total_chunks + 7) // 8
        received_mask = bytes(mask_bytes)
 
        self._conn.execute(
            """
            INSERT INTO uploads_in_progress (
                upload_id, owner_user_id, relative_path, original_name,
                total_size, total_sha256, chunk_size, total_chunks,
                received_mask, temp_dir, mime_type, expires_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (upload_id, owner_user_id, relative_path, original_name,
             total_size, total_sha256, chunk_size, total_chunks,
             received_mask, temp_dir, mime_type, expires_at),
        )
        self._conn.commit()
 
    def get_upload(self, upload_id: str) -> Optional[sqlite3.Row]:
        """Get an upload session by ID. Returns None if missing or expired."""
        row = self._conn.execute(
            "SELECT * FROM uploads_in_progress WHERE upload_id = ?",
            (upload_id,),
        ).fetchone()
        if not row:
            return None
        # Lazy expiry check — caller treats None as 404
        try:
            expires = datetime.fromisoformat(row["expires_at"].replace("Z", "+00:00"))
            if datetime.now(timezone.utc) > expires:
                return None
        except (ValueError, AttributeError):
            pass
        return row
 

    def find_upload_by_hash(self, sha256: str, owner_user_id: str) -> Optional[sqlite3.Row]:
        """
        Sprint 3.8: find an IN-PROGRESS upload session for this content hash + owner.
        Used by upload_init to dedup against sessions already underway — the missing
        check that let one file spawn N sessions (12,167 duplicate groups). Excludes
        expired sessions so a dead session doesn't block a fresh retry.
        """
        now_iso = datetime.now(timezone.utc).isoformat()
        return self._conn.execute(
            "SELECT * FROM uploads_in_progress "
            "WHERE total_sha256 = ? AND owner_user_id = ? AND expires_at >= ? "
            "LIMIT 1",
            (sha256, owner_user_id, now_iso),
        ).fetchone()
    
    def mark_chunk_received(self, upload_id: str, chunk_index: int) -> None:
        """Set bit `chunk_index` in the received_mask bitmap."""
        row = self._conn.execute(
            "SELECT received_mask FROM uploads_in_progress WHERE upload_id = ?",
            (upload_id,),
        ).fetchone()
        if not row:
            return
        mask = bytearray(row["received_mask"])
        byte_idx = chunk_index // 8
        bit_idx = chunk_index % 8
        if byte_idx < len(mask):
            mask[byte_idx] |= (1 << bit_idx)
        now = datetime.now(timezone.utc).isoformat()
        self._conn.execute(
            "UPDATE uploads_in_progress SET received_mask = ?, last_chunk_at = ? WHERE upload_id = ?",
            (bytes(mask), now, upload_id),
        )
        self._conn.commit()
 
    @staticmethod
    def get_missing_chunks(received_mask: bytes, total_chunks: int) -> list[int]:
        """Return list of chunk indices NOT yet received."""
        missing = []
        for i in range(total_chunks):
            byte_idx = i // 8
            bit_idx = i % 8
            if byte_idx >= len(received_mask) or not (received_mask[byte_idx] & (1 << bit_idx)):
                missing.append(i)
        return missing
 
    def delete_upload(self, upload_id: str) -> None:
        """Remove an upload session (called on finalize success or abort)."""
        self._conn.execute(
            "DELETE FROM uploads_in_progress WHERE upload_id = ?",
            (upload_id,),
        )
        self._conn.commit()
 
    def get_expired_uploads(self) -> list[sqlite3.Row]:
        """Return upload sessions past their TTL — for the cleanup task."""
        now_iso = datetime.now(timezone.utc).isoformat()
        return self._conn.execute(
            "SELECT * FROM uploads_in_progress WHERE expires_at < ?",
            (now_iso,),
        ).fetchall()

    def close(self):
        """Close the current thread's connection."""
        if hasattr(self._local, "conn") and self._local.conn:
            self._local.conn.close()
            self._local.conn = None