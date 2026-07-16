"""
RGBC Drive — Sprint 3.3 schema migration

One-shot script to add the uploads_in_progress table to an existing
.rgbc_sync.db that was created under schema v1 (pre-chunked-uploads).

Usage:
    Stop RGBC Drive (right-click tray icon → Quit).
    Then run from the rgbc-drive directory:

        python migrate_v1_to_v2.py "C:\\Users\\<You>\\RGBC_Drive\\.rgbc_sync.db"

    Or just:

        python migrate_v1_to_v2.py

    (auto-detects the default location).

Idempotent — safe to run more than once.
"""

import os
import sqlite3
import sys
from datetime import datetime, timezone


def find_default_db() -> str:
    candidates = [
        os.path.join(os.path.expanduser("~"), "RGBC_Drive", ".rgbc_sync.db"),
        os.path.join("C:\\Users", os.environ.get("USERNAME", ""), "RGBC_Drive", ".rgbc_sync.db"),
    ]
    for c in candidates:
        if os.path.isfile(c):
            return c
    return ""


def migrate(db_path: str) -> int:
    if not os.path.isfile(db_path):
        print(f"❌ Database not found: {db_path}")
        return 1

    print(f"📂 Opening: {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")

    # ── Read current version ────────────────────────────────────────
    try:
        row = conn.execute(
            "SELECT value FROM sync_meta WHERE key = 'schema_version'"
        ).fetchone()
        current = int(row["value"]) if row else 1
    except sqlite3.OperationalError:
        # sync_meta itself missing — very old DB
        current = 0

    print(f"📊 Current schema version: {current}")

    # ── Idempotent table create ─────────────────────────────────────
    print("🔧 Creating uploads_in_progress table (if not exists)…")
    conn.executescript("""
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

    # ── Bump schema_version ─────────────────────────────────────────
    conn.execute("""
        INSERT INTO sync_meta (key, value) VALUES ('schema_version', '2')
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
    """)
    conn.commit()

    # ── Verify ──────────────────────────────────────────────────────
    tables = {
        r["name"] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if "uploads_in_progress" not in tables:
        print("❌ Migration finished without error but table is still missing!")
        return 2

    cols = [r["name"] for r in conn.execute("PRAGMA table_info(uploads_in_progress)")]
    print(f"✅ uploads_in_progress columns: {len(cols)}")
    print(f"   {', '.join(cols)}")

    new_version = conn.execute(
        "SELECT value FROM sync_meta WHERE key = 'schema_version'"
    ).fetchone()["value"]
    print(f"📊 Schema version now: {new_version}")
    print(f"⏰ Migrated at: {datetime.now(timezone.utc).isoformat()}")

    conn.close()
    return 0


if __name__ == "__main__":
    if len(sys.argv) > 1:
        db = sys.argv[1]
    else:
        db = find_default_db()
        if not db:
            print("❌ Could not auto-detect .rgbc_sync.db. Pass the path as an argument:")
            print('    python migrate_v1_to_v2.py "C:\\path\\to\\.rgbc_sync.db"')
            sys.exit(1)
        print(f"🔍 Auto-detected: {db}")

    sys.exit(migrate(db))