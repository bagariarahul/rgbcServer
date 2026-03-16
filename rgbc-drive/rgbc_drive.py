"""
RGBC Drive — Main Entry Point (Sprint 2.3: Tunnel Integration)

Lifecycle:
  1. Load config from .env
  2. Create SYNC_ROOT folder
  3. Initialize SQLite database
  4. Test API Gateway connectivity
  5. Run initial full directory scan
  6. Start Master API server (FastAPI on :8741)
  7. Start Cloudflare Tunnel (cloudflared → internet)
  8. Wait for tunnel URL to be discovered
  9. Register with API Gateway as MASTER (with tunnel URL)
  10. Start heartbeat thread (every 30s, includes dynamic tunnel URL)
  11. Start watchdog file watcher
  12. Start bidirectional sync poller
  13. Start system tray icon (blocks main thread)
  14. On quit: stop tunnel, stop everything
"""

import os
import sys
import logging
import platform
import shutil
import socket
import threading
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv

_script_dir = Path(os.path.dirname(os.path.abspath(sys.argv[0])))
load_dotenv(_script_dir / ".env")

# ── Configuration ────────────────────────────────────────────────────────
SERVER_URL = os.getenv("SERVER_URL", "https://api.bagariaa.in").rstrip("/")
API_KEY = os.getenv("API_KEY", "")
JWT_SECRET = os.getenv("JWT_SECRET", "")
SYNC_ROOT = os.getenv("SYNC_ROOT", os.path.join(os.path.expanduser("~"), "RGBC_Drive"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
FULL_SCAN_INTERVAL = int(os.getenv("FULL_SCAN_INTERVAL", "300"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

NODE_ROLE = os.getenv("NODE_ROLE", "MASTER").upper()
MASTER_PORT = int(os.getenv("MASTER_PORT", "8741"))
DEVICE_NAME = os.getenv("DEVICE_NAME", platform.node())
DEVICE_ID = os.getenv("DEVICE_ID", "")
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "30"))

# Sprint 2.3: Tunnel configuration
TUNNEL_URL = os.getenv("TUNNEL_URL", "")
CLOUDFLARED_TOKEN = os.getenv("CLOUDFLARED_TOKEN", "")
TUNNEL_ENABLED = os.getenv("TUNNEL_ENABLED", "true").lower() in ("true", "1", "yes")

# ── Logging ──────────────────────────────────────────────────────────────
log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_fmt)
log_file = _script_dir / "rgbc_drive.log"
file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setFormatter(log_fmt)
root_logger = logging.getLogger("RGBCDrive")
root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)
logger = logging.getLogger("RGBCDrive.Main")

# ── Import components ────────────────────────────────────────────────────
from sync_db import SyncDatabase
from scanner import DirectoryScanner
from api_client import APIClient
from watcher import FileWatcher
from poller import SyncPoller
from master_api import start_master_api
from tunnel_manager import TunnelManager
from tray import TrayIcon, STATUS_SYNCING, STATUS_UP_TO_DATE, STATUS_OFFLINE, STATUS_ERROR


def get_local_ip() -> str:
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def get_or_create_device_id() -> str:
    id_file = Path(SYNC_ROOT) / ".rgbc_device_id"
    if id_file.exists():
        return id_file.read_text().strip()
    new_id = f"{platform.node()}_{uuid.uuid4().hex[:12]}"
    id_file.write_text(new_id)
    return new_id


def register_with_gateway(api: APIClient, device_id: str, db: SyncDatabase, tunnel_getter=None) -> bool:
    try:
        stats = db.get_stats()
        disk = shutil.disk_usage(SYNC_ROOT)
        tunnel_url = tunnel_getter() if tunnel_getter else None

        payload = {
            "device_id": device_id, "device_name": DEVICE_NAME, "device_type": "DESKTOP",
            "role": NODE_ROLE, "tunnel_url": tunnel_url, "local_ip": get_local_ip(),
            "local_port": MASTER_PORT, "os_platform": sys.platform, "os_version": platform.release(),
            "app_version": "2.0", "file_count": stats.get("total_files", 0), "disk_free_bytes": disk.free,
        }

        resp = api.session.post(f"{api.server_url}/api/devices/register", json=payload, timeout=(10, 30))
        if resp.status_code == 200:
            data = resp.json()
            logger.info(f"✅ Registered with Gateway as {NODE_ROLE}: {data.get('message', '')}")
            if tunnel_url:
                logger.info(f"   Tunnel URL: {tunnel_url}")
            return True
        else:
            logger.warning(f"⚠️ Gateway registration failed: {resp.status_code} — {resp.text[:200]}")
            return False
    except Exception as e:
        logger.warning(f"⚠️ Gateway registration error: {e}")
        return False


def heartbeat_loop(api, device_id, db, stop_event, tunnel_getter=None):
    while not stop_event.is_set():
        try:
            stats = db.get_stats()
            disk = shutil.disk_usage(SYNC_ROOT)
            tunnel_url = tunnel_getter() if tunnel_getter else None

            payload = {
                "device_id": device_id, "status": "online", "tunnel_url": tunnel_url,
                "local_ip": get_local_ip(), "file_count": stats.get("total_files", 0),
                "disk_free_bytes": disk.free,
            }
            resp = api.session.post(f"{api.server_url}/api/devices/heartbeat", json=payload, timeout=(5, 10))
            if resp.status_code != 200:
                logger.debug(f"Heartbeat response: {resp.status_code}")
        except Exception as e:
            logger.debug(f"Heartbeat failed (will retry): {e}")

        for _ in range(HEARTBEAT_INTERVAL):
            if stop_event.is_set():
                return
            time.sleep(1)


def main():
    print(r"""
    ╔══════════════════════════════════════════╗
    ║         RGBC Drive v2.0                  ║
    ║    P2P Master Node — Self-Hosted Sync    ║
    ╚══════════════════════════════════════════╝
    """)

    if not API_KEY:
        logger.error("❌ API_KEY not set in .env — cannot authenticate")
        input("Press Enter to exit...")
        sys.exit(1)

    os.makedirs(SYNC_ROOT, exist_ok=True)
    logger.info(f"Sync folder:  {SYNC_ROOT}")
    logger.info(f"Gateway:      {SERVER_URL}")
    logger.info(f"Role:         {NODE_ROLE}")
    logger.info(f"Master Port:  {MASTER_PORT}")
    logger.info(f"LAN IP:       {get_local_ip()}")

    global DEVICE_ID
    DEVICE_ID = DEVICE_ID or get_or_create_device_id()
    logger.info(f"Device ID:    {DEVICE_ID}")

    db_path = os.path.join(SYNC_ROOT, ".rgbc_sync.db")
    db = SyncDatabase(db_path)
    logger.info(f"Database:     {db_path}")

    api = APIClient(SERVER_URL, API_KEY)

    logger.info("Testing API Gateway connectivity...")
    if api.health_check():
        logger.info("✅ API Gateway is reachable")
    else:
        logger.warning("⚠️ API Gateway is offline — will retry in background")

    logger.info("Running initial directory scan...")
    scanner = DirectoryScanner(SYNC_ROOT, db)
    scan_result = scanner.scan()
    logger.info(f"Initial scan: {scan_result.new_files} new, {scan_result.unchanged_files} unchanged, {scan_result.deleted_files} deleted")

    # ── Start Master API server ──────────────────────────────────────
    if NODE_ROLE == "MASTER":
        start_master_api(sync_root=SYNC_ROOT, db=db, api_key=API_KEY, jwt_secret=JWT_SECRET, scanner=scanner, port=MASTER_PORT)
        logger.info(f"🌐 Master API listening on http://0.0.0.0:{MASTER_PORT}")

    # ── Start Cloudflare Tunnel ──────────────────────────────────────
    tunnel = None
    tunnel_getter = lambda: None

    if NODE_ROLE == "MASTER" and TUNNEL_ENABLED:
        tunnel = TunnelManager(
            local_port=MASTER_PORT,
            token=CLOUDFLARED_TOKEN,
            tunnel_url_override=TUNNEL_URL,
        )
        tunnel.start()

        if not TUNNEL_URL:
            logger.info("⏳ Waiting for Cloudflare Tunnel URL (up to 30s)...")
            url = tunnel.get_url(timeout=30)
            if url:
                logger.info(f"🔗 Tunnel active: {url}")
            else:
                logger.warning(
                    "⚠️ Tunnel URL not discovered within 30s.\n"
                    f"   LAN access still works: http://{get_local_ip()}:{MASTER_PORT}\n"
                    "   Remote Slaves will connect once the tunnel comes up."
                )
        else:
            logger.info(f"🔗 Using pre-configured tunnel: {TUNNEL_URL}")

        # tunnel_getter always returns the LATEST URL (handles restarts)
        tunnel_getter = lambda: tunnel.get_url(timeout=0)

    elif NODE_ROLE == "MASTER" and not TUNNEL_ENABLED:
        logger.info(f"🔗 Tunnel disabled. LAN-only: http://{get_local_ip()}:{MASTER_PORT}")

    # ── Register with API Gateway ────────────────────────────────────
    register_with_gateway(api, DEVICE_ID, db, tunnel_getter=tunnel_getter)

    # ── Start heartbeat ──────────────────────────────────────────────
    heartbeat_stop = threading.Event()
    heartbeat_thread = threading.Thread(
        target=heartbeat_loop, args=(api, DEVICE_ID, db, heartbeat_stop),
        kwargs={"tunnel_getter": tunnel_getter}, daemon=True, name="Heartbeat",
    )
    heartbeat_thread.start()
    logger.info(f"💓 Heartbeat started (every {HEARTBEAT_INTERVAL}s)")

    # ── Tray callbacks ───────────────────────────────────────────────
    poller_ref = {"obj": None}
    watcher_ref = {"obj": None}

    def on_force_sync():
        logger.info("🔄 Force sync triggered")
        if poller_ref["obj"]: poller_ref["obj"].force_sync()

    def on_pause():
        logger.info("⏸️ Sync paused")
        if poller_ref["obj"]: poller_ref["obj"].stop()
        if watcher_ref["obj"]: watcher_ref["obj"].stop()

    def on_resume():
        logger.info("▶️ Sync resumed")
        if poller_ref["obj"]: poller_ref["obj"].start()
        if watcher_ref["obj"]: watcher_ref["obj"].start()

    def on_quit():
        logger.info("🛑 Shutting down RGBC Drive")
        heartbeat_stop.set()
        if tunnel: tunnel.stop()
        if poller_ref["obj"]: poller_ref["obj"].stop()
        if watcher_ref["obj"]: watcher_ref["obj"].stop()
        db.close()
        logger.info("🛑 Shutdown complete")

    # ── Start file watcher ───────────────────────────────────────────
    def on_upload_complete():
        stats = db.get_stats()
        tray.set_stats(total=stats["total_files"], synced=stats["synced"],
                        pending=stats["pending_upload"], size_bytes=stats["total_size_bytes"])

    watcher = FileWatcher(sync_root=SYNC_ROOT, db=db, api_client=api, upload_callback=on_upload_complete)
    watcher.start()
    watcher_ref["obj"] = watcher

    poller = SyncPoller(sync_root=SYNC_ROOT, db=db, api_client=api, poll_interval=POLL_INTERVAL, status_callback=lambda t: None)
    poller.start()
    poller_ref["obj"] = poller

    # ── Periodic re-scan ─────────────────────────────────────────────
    def periodic_scan_loop():
        while True:
            time.sleep(FULL_SCAN_INTERVAL)
            try:
                scanner.scan()
                stats = db.get_stats()
                tray.set_stats(total=stats["total_files"], synced=stats["synced"],
                                pending=stats["pending_upload"], size_bytes=stats["total_size_bytes"])
            except Exception as e:
                logger.error(f"Periodic scan error: {e}")

    threading.Thread(target=periodic_scan_loop, daemon=True, name="PeriodicScan").start()

    # ── System tray ──────────────────────────────────────────────────
    stats = db.get_stats()
    tray = TrayIcon(sync_root=SYNC_ROOT, on_force_sync=on_force_sync, on_pause=on_pause, on_resume=on_resume, on_quit=on_quit)

    current_tunnel = tunnel_getter() if tunnel_getter else None
    if current_tunnel:
        role_label = f"Master — {current_tunnel}"
    elif NODE_ROLE == "MASTER":
        role_label = f"Master — LAN ({get_local_ip()}:{MASTER_PORT})"
    else:
        role_label = "Slave"

    tray.set_status(STATUS_UP_TO_DATE, f"Ready ({role_label})")
    tray.set_stats(total=stats["total_files"], synced=stats["synced"],
                    pending=stats["pending_upload"], size_bytes=stats["total_size_bytes"])

    tray.run()  # Blocks until Quit
    logger.info("Goodbye!")


if __name__ == "__main__":
    main()