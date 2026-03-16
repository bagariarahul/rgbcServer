"""
RGBC Tally Sync Client — Headless Windows Background Service

Mirrors the Android app's backup pipeline:
  1. Watch local Tally directory for file changes (watchdog)
  2. Compute SHA-256 checksum of new/changed files
  3. Check backend if hash already exists (GET /api/files/check-hash/:sha256)
  4. Upload only if missing (POST /api/files/upload)

Authentication: M2M API Key via X-API-Key header.

Usage:
  pip install -r requirements.txt
  python tally_sync.py

Or compile to .exe:
  pyinstaller --onefile --name TallySync tally_sync.py
"""

import os
import sys
import time
import hashlib
import logging
import threading
from pathlib import Path
from datetime import datetime

import requests
from dotenv import load_dotenv
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ── Load configuration ───────────────────────────────────────────────────
load_dotenv()

SERVER_URL = os.getenv("SERVER_URL", "https://api.bagariaa.in").rstrip("/")
API_KEY = os.getenv("API_KEY", "")
WATCH_DIR = os.getenv("WATCH_DIR", "")
FULL_SCAN_INTERVAL = int(os.getenv("FULL_SCAN_INTERVAL", "300"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

# ── Logging setup ────────────────────────────────────────────────────────
log_formatter = logging.Formatter(
    "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
)

console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_formatter)

# Also log to file next to the script/exe
log_dir = Path(os.path.dirname(os.path.abspath(sys.argv[0])))
file_handler = logging.FileHandler(log_dir / "tally_sync.log", encoding="utf-8")
file_handler.setFormatter(log_formatter)

logger = logging.getLogger("TallySync")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logger.addHandler(console_handler)
logger.addHandler(file_handler)


# ── SHA-256 Hashing ──────────────────────────────────────────────────────

def compute_sha256(filepath: str) -> str | None:
    """Compute SHA-256 of a file. Returns hex digest or None on error."""
    try:
        sha = hashlib.sha256()
        with open(filepath, "rb") as f:
            while chunk := f.read(8192):
                sha.update(chunk)
        return sha.hexdigest()
    except Exception as e:
        logger.error(f"Hash failed for {filepath}: {e}")
        return None


# ── API Client ───────────────────────────────────────────────────────────

class BackupClient:
    """HTTP client for the RGBC backend with M2M API key auth."""

    def __init__(self, server_url: str, api_key: str):
        self.server_url = server_url
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": api_key,
            "Accept": "application/json",
        })
        # Timeout: 30s connect, 300s read (large files over slow uplinks)
        self.timeout = (30, 300)

    def health_check(self) -> bool:
        """Test connectivity to the server."""
        try:
            resp = self.session.get(
                f"{self.server_url}/health", timeout=(10, 10)
            )
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Health check failed: {e}")
            return False

    def check_hash_exists(self, sha256: str) -> bool:
        """Check if a file with this SHA-256 already exists on the server."""
        try:
            resp = self.session.get(
                f"{self.server_url}/api/files/check-hash/{sha256}",
                timeout=self.timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("exists"):
                    logger.info(
                        f"  Hash exists on server: {data['file']['originalName']} "
                        f"(uploaded {data['file'].get('uploadedAt', 'unknown')})"
                    )
                    return True
                return False
            else:
                logger.warning(f"  Hash check returned {resp.status_code}")
                return False
        except Exception as e:
            logger.error(f"  Hash check error: {e}")
            return False

    def upload_file(self, filepath: str) -> bool:
        """Upload a file to the backend. Returns True on success."""
        try:
            filename = os.path.basename(filepath)
            file_size = os.path.getsize(filepath)

            logger.info(f"  Uploading: {filename} ({file_size:,} bytes)")

            with open(filepath, "rb") as f:
                resp = self.session.post(
                    f"{self.server_url}/api/files/upload",
                    files={"file": (filename, f, "application/octet-stream")},
                    timeout=self.timeout,
                )

            if resp.status_code in (200, 201):
                data = resp.json()
                server_id = data.get("file", {}).get("id", "?")
                logger.info(f"  ✅ Uploaded successfully (server ID: {server_id})")
                return True
            else:
                logger.error(f"  ❌ Upload failed: {resp.status_code} — {resp.text[:200]}")
                return False

        except requests.exceptions.ConnectionError as e:
            logger.error(f"  ❌ Connection error: {e}")
            return False
        except Exception as e:
            logger.error(f"  ❌ Upload exception: {e}")
            return False


# ── File Processing ──────────────────────────────────────────────────────

# Track files we've already processed to avoid re-uploading on every
# watchdog event. Key = filepath, Value = sha256 hash.
_processed_hashes: dict[str, str] = {}
_processing_lock = threading.Lock()


def process_file(client: BackupClient, filepath: str) -> None:
    """Hash → check → upload pipeline for a single file."""
    try:
        # Skip files that are still being written (size = 0 or locked)
        if not os.path.isfile(filepath):
            return
        file_size = os.path.getsize(filepath)
        if file_size == 0:
            return

        # Skip temporary/system files
        basename = os.path.basename(filepath)
        if basename.startswith(".") or basename.startswith("~") or basename.endswith(".tmp"):
            return

        logger.info(f"Processing: {basename} ({file_size:,} bytes)")

        # 1. Compute SHA-256
        sha256 = compute_sha256(filepath)
        if not sha256:
            return

        # 2. Check if we already processed this exact hash locally
        with _processing_lock:
            if _processed_hashes.get(filepath) == sha256:
                logger.debug(f"  Skipped (unchanged since last process): {basename}")
                return

        # 3. Check if server already has this hash
        if client.check_hash_exists(sha256):
            logger.info(f"  Skipped (already on server): {basename}")
            with _processing_lock:
                _processed_hashes[filepath] = sha256
            return

        # 4. Upload
        # Wait briefly in case the file is still being written
        # (Tally often writes in bursts)
        time.sleep(2)

        # Re-verify size hasn't changed (file still being written)
        new_size = os.path.getsize(filepath)
        if new_size != file_size:
            logger.debug(f"  File still changing, deferring: {basename}")
            return

        success = client.upload_file(filepath)
        if success:
            with _processing_lock:
                _processed_hashes[filepath] = sha256

    except Exception as e:
        logger.error(f"Error processing {filepath}: {e}")


# ── Watchdog Event Handler ───────────────────────────────────────────────

class TallyFileHandler(FileSystemEventHandler):
    """Handles filesystem events from watchdog."""

    def __init__(self, client: BackupClient):
        super().__init__()
        self.client = client

    def on_created(self, event):
        if not event.is_directory:
            # Delay slightly to let file writes finish
            threading.Timer(3.0, process_file, args=(self.client, event.src_path)).start()

    def on_modified(self, event):
        if not event.is_directory:
            # Delay slightly for write completion
            threading.Timer(3.0, process_file, args=(self.client, event.src_path)).start()


# ── Full Directory Scan ──────────────────────────────────────────────────

def full_scan(client: BackupClient, watch_dir: str) -> None:
    """Walk the entire directory tree and process every file."""
    logger.info(f"🔍 Starting full scan of: {watch_dir}")
    file_count = 0
    upload_count = 0

    for root, dirs, files in os.walk(watch_dir):
        # Skip hidden directories
        dirs[:] = [d for d in dirs if not d.startswith(".")]

        for filename in files:
            filepath = os.path.join(root, filename)
            try:
                before_count = len([v for v in _processed_hashes.values()])
                process_file(client, filepath)
                after_count = len([v for v in _processed_hashes.values()])
                file_count += 1
                if after_count > before_count:
                    upload_count += 1
            except Exception as e:
                logger.error(f"Scan error for {filepath}: {e}")

    logger.info(f"🔍 Full scan complete: {file_count} files checked, {upload_count} new uploads")


# ── Main Entry Point ─────────────────────────────────────────────────────

def main():
    print(r"""
    ╔══════════════════════════════════════════╗
    ║     RGBC Tally Sync Client v1.0          ║
    ║     Secure Cloud Backup for Tally        ║
    ╚══════════════════════════════════════════╝
    """)

    # Validate configuration
    if not API_KEY:
        logger.error("❌ API_KEY not set in .env file. Cannot authenticate.")
        sys.exit(1)

    if not WATCH_DIR or not os.path.isdir(WATCH_DIR):
        logger.error(f"❌ WATCH_DIR is not a valid directory: '{WATCH_DIR}'")
        logger.error("   Set WATCH_DIR in your .env file to a valid path.")
        sys.exit(1)

    logger.info(f"Server:    {SERVER_URL}")
    logger.info(f"Watch Dir: {WATCH_DIR}")
    logger.info(f"Scan Interval: {FULL_SCAN_INTERVAL}s")

    # Initialize API client
    client = BackupClient(SERVER_URL, API_KEY)

    # Test connectivity
    logger.info("Testing server connectivity...")
    if client.health_check():
        logger.info("✅ Server is reachable")
    else:
        logger.warning("⚠️ Server is not reachable — will retry on file events")

    # Run initial full scan
    full_scan(client, WATCH_DIR)

    # Start watchdog observer for real-time file monitoring
    event_handler = TallyFileHandler(client)
    observer = Observer()
    observer.schedule(event_handler, WATCH_DIR, recursive=True)
    observer.start()
    logger.info(f"👁️ Watching for file changes in: {WATCH_DIR}")

    # Main loop: periodic full re-scans + keep alive
    try:
        while True:
            time.sleep(FULL_SCAN_INTERVAL)
            full_scan(client, WATCH_DIR)
    except KeyboardInterrupt:
        logger.info("🛑 Shutdown requested")
        observer.stop()
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        observer.stop()

    observer.join()
    logger.info("Goodbye!")


if __name__ == "__main__":
    main()