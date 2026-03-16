"""
RGBC Drive — Smart Directory Scanner

Walks the local RGBC_Drive folder and updates the SQLite state database.
The critical optimization: it only computes SHA-256 for files that are
NEW or whose mtime has changed since the last scan.

Performance model for 50GB / 10,000 files:
  - Full mtime comparison: ~2 seconds (just os.stat per file)
  - SHA-256 only for changed files: O(changed_bytes), not O(total_bytes)
  - First-ever scan of 50GB: ~5-10 minutes (one-time cost)
  - Subsequent scan of 50GB with 3 changed files: ~3 seconds

Usage:
    from scanner import DirectoryScanner
    from sync_db import SyncDatabase

    db = SyncDatabase(".rgbc_sync.db")
    scanner = DirectoryScanner(sync_root="C:/RGBC_Drive", db=db)
    result = scanner.scan()
    print(f"New: {result.new}, Changed: {result.changed}, Deleted: {result.deleted}")
"""

import os
import hashlib
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Optional

from sync_db import SyncDatabase, PENDING_UPLOAD, SYNCED, DELETED_LOCAL

logger = logging.getLogger("RGBCDrive.Scanner")

# Files/directories to always skip
IGNORED_NAMES = {
    ".rgbc_sync.db",
    ".rgbc_sync.db-wal",
    ".rgbc_sync.db-shm",
    ".rgbc_drive.conf",
    "desktop.ini",
    "Thumbs.db",
    ".DS_Store",
}

IGNORED_PREFIXES = ("~$", ".")   # Office temp files, hidden files
IGNORED_SUFFIXES = (".tmp", ".crdownload", ".partial", ".swp")

# Max file size for SHA-256 content hashing (2GB)
# Files larger than this get a metadata hash
MAX_HASH_BYTES = 2 * 1024 * 1024 * 1024


@dataclass
class ScanResult:
    """Summary of a directory scan."""
    new_files: int = 0
    changed_files: int = 0
    unchanged_files: int = 0
    deleted_files: int = 0
    errors: int = 0
    elapsed_seconds: float = 0.0
    bytes_hashed: int = 0


class DirectoryScanner:
    """
    Scans the local RGBC_Drive folder and updates the SyncDatabase.

    Algorithm:
    1. Walk every file in sync_root (os.walk)
    2. Compute relative path (forward slashes, for portability)
    3. os.stat() to get mtime_ns and size
    4. Look up existing record in DB by relative_path
       a. If no record → NEW file → hash it → insert as PENDING_UPLOAD
       b. If record exists AND mtime_ns matches → UNCHANGED → skip
       c. If record exists AND mtime_ns differs → CHANGED → rehash → update status
    5. After walk completes, find DB records with no matching file on disk → DELETED
    """

    def __init__(self, sync_root: str, db: SyncDatabase):
        self.sync_root = os.path.normpath(sync_root)
        self.db = db

    def scan(self) -> ScanResult:
        """
        Run a full directory scan. Thread-safe (uses DB's per-thread connections).
        Returns a ScanResult with counts.
        """
        start = time.monotonic()
        result = ScanResult()

        logger.info(f"Scanning: {self.sync_root}")

        # Collect all relative paths currently on disk
        disk_paths: set[str] = set()

        for dirpath, dirnames, filenames in os.walk(self.sync_root):
            # Prune ignored directories IN-PLACE (modifying dirnames
            # prevents os.walk from descending into them)
            dirnames[:] = [
                d for d in dirnames
                if not d.startswith(".") and d not in IGNORED_NAMES
            ]

            for filename in filenames:
                if self._should_skip(filename):
                    continue

                abs_path = os.path.join(dirpath, filename)
                rel_path = self._to_relative(abs_path)
                if rel_path is None:
                    continue

                disk_paths.add(rel_path)

                try:
                    stat = os.stat(abs_path)
                    size = stat.st_size
                    mtime_ns = stat.st_mtime_ns

                    if size == 0:
                        continue  # Skip empty files

                    # Look up existing DB record
                    existing = self.db.get_local_file(rel_path)

                    if existing is None:
                        # ── NEW file ─────────────────────────────────
                        sha256 = self._compute_hash(abs_path, size)
                        if sha256:
                            self.db.upsert_local_file(
                                relative_path=rel_path,
                                sha256=sha256,
                                size=size,
                                mtime_ns=mtime_ns,
                                sync_status=PENDING_UPLOAD,
                            )
                            result.new_files += 1
                            result.bytes_hashed += size
                            logger.debug(f"  NEW: {rel_path}")
                        else:
                            result.errors += 1

                    elif existing["mtime_ns"] != mtime_ns or existing["size"] != size:
                        # ── CHANGED file (mtime or size differs) ─────
                        sha256 = self._compute_hash(abs_path, size)
                        if sha256:
                            # Only mark as changed if content actually differs
                            if sha256 != existing["sha256"]:
                                self.db.upsert_local_file(
                                    relative_path=rel_path,
                                    sha256=sha256,
                                    size=size,
                                    mtime_ns=mtime_ns,
                                    sync_status=PENDING_UPLOAD,
                                )
                                result.changed_files += 1
                                logger.debug(f"  CHANGED: {rel_path}")
                            else:
                                # mtime changed but content is identical
                                # (e.g., file was touched/copied). Update mtime only.
                                self.db.upsert_local_file(
                                    relative_path=rel_path,
                                    sha256=sha256,
                                    size=size,
                                    mtime_ns=mtime_ns,
                                    sync_status=existing["sync_status"],
                                )
                                result.unchanged_files += 1
                            result.bytes_hashed += size
                        else:
                            result.errors += 1

                    else:
                        # ── UNCHANGED (same mtime_ns AND size) ───────
                        result.unchanged_files += 1

                except PermissionError:
                    logger.warning(f"  Permission denied: {abs_path}")
                    result.errors += 1
                except OSError as e:
                    logger.warning(f"  OS error for {abs_path}: {e}")
                    result.errors += 1

        # ── Detect DELETED files (in DB but not on disk) ─────────────
        db_paths = self.db.get_all_local_paths()
        deleted_paths = db_paths - disk_paths

        for rel_path in deleted_paths:
            existing = self.db.get_local_file(rel_path)
            if existing and existing["sync_status"] != DELETED_LOCAL:
                self.db.mark_deleted(rel_path)
                result.deleted_files += 1
                logger.debug(f"  DELETED: {rel_path}")

        result.elapsed_seconds = time.monotonic() - start

        logger.info(
            f"Scan complete in {result.elapsed_seconds:.1f}s: "
            f"{result.new_files} new, {result.changed_files} changed, "
            f"{result.unchanged_files} unchanged, {result.deleted_files} deleted, "
            f"{result.errors} errors "
            f"({result.bytes_hashed / (1024*1024):.1f} MB hashed)"
        )

        return result

    def _to_relative(self, abs_path: str) -> Optional[str]:
        """
        Convert an absolute path to a forward-slash relative path.
        Returns None if the path is outside the sync root.
        """
        try:
            rel = os.path.relpath(abs_path, self.sync_root)
            # Reject paths that escape the root (e.g., "../../etc/passwd")
            if rel.startswith(".."):
                return None
            # Normalize to forward slashes for cross-platform portability
            return str(PurePosixPath(Path(rel)))
        except ValueError:
            # Different drives on Windows (e.g., C: vs D:)
            return None

    def _should_skip(self, filename: str) -> bool:
        """Check if a file should be ignored."""
        if filename in IGNORED_NAMES:
            return True
        if any(filename.startswith(p) for p in IGNORED_PREFIXES):
            return True
        if any(filename.endswith(s) for s in IGNORED_SUFFIXES):
            return True
        return False

    def _compute_hash(self, filepath: str, size: int) -> Optional[str]:
        """
        Compute SHA-256 of a file's content.
        Falls back to metadata hash for very large files (>2GB).
        Returns hex digest or None on error.
        """
        try:
            if size > MAX_HASH_BYTES:
                # Metadata hash for huge files to avoid 10+ minute hashing
                logger.debug(f"  Large file ({size / (1024**3):.1f} GB), using metadata hash")
                return self._metadata_hash(filepath, size)

            sha = hashlib.sha256()
            with open(filepath, "rb") as f:
                while True:
                    chunk = f.read(65536)  # 64KB reads for throughput
                    if not chunk:
                        break
                    sha.update(chunk)
            return sha.hexdigest()

        except PermissionError:
            logger.warning(f"  Hash permission denied: {filepath}")
            return None
        except OSError as e:
            logger.warning(f"  Hash error for {filepath}: {e}")
            return None

    @staticmethod
    def _metadata_hash(filepath: str, size: int) -> str:
        """Fast fallback hash: SHA-256 of (filename + size + mtime)."""
        stat = os.stat(filepath)
        meta = f"{os.path.basename(filepath)}_{size}_{stat.st_mtime_ns}"
        return hashlib.sha256(meta.encode()).hexdigest()