"""
RGBC Drive — Main Entry Point (Sprint 3: Generic Distributable)

Key changes from Sprint 2:
  - PyInstaller _MEIPASS support: bundled assets vs external config
  - First-run setup wizard if .env is missing
  - Google OAuth replaces M2M API key
  - RS256 JWT verification replaces HS256

Lifecycle:
  1. Resolve internal (bundled) vs external (config) paths
  2. Check for .env — launch Setup Wizard if missing
  3. Load .env from EXTERNAL directory (user-editable)
  4. Google OAuth login (browser flow, cached for 30 days)
  5. Create sync root, init SQLite, test Gateway
  6. Start Master API (FastAPI on :8741, RS256 auth)
  7. Start Cloudflare Tunnel
  8. Register + heartbeat with Gateway
  9. Start watchdog + poller + periodic scan
  10. System tray icon (blocks main thread)
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
from typing import Optional

# ── Path resolution (must come before any file access) ───────────────
from paths import get_internal_dir, get_external_dir, get_env_path, get_public_key_path, is_frozen

INTERNAL_DIR = get_internal_dir()
EXTERNAL_DIR = get_external_dir()
ENV_PATH = get_env_path()

# ── First-run setup wizard ───────────────────────────────────────────
from setup_wizard import needs_setup, run_setup_wizard

if needs_setup(str(ENV_PATH)):
    print("═" * 50)
    print("  RGBC Drive — First Run Detected")
    print(f"  No .env found at: {ENV_PATH}")
    print("═" * 50)

    created = run_setup_wizard(str(ENV_PATH))
    if not created:
        print("\n❌ Setup cancelled. Cannot start without configuration.")
        print(f"   Create a .env file at: {ENV_PATH}")
        input("Press Enter to exit...")
        sys.exit(1)

    print(f"\n✅ Configuration saved to: {ENV_PATH}")
    print("   Starting RGBC Drive...\n")

# ── Load .env from EXTERNAL directory (user config, not bundled) ─────
from dotenv import load_dotenv
load_dotenv(str(ENV_PATH))

# ── Configuration ────────────────────────────────────────────────────
# Defense-in-depth: hardcoded fallback prevents a tampered .env from
# redirecting OAuth to an attacker-controlled gateway. Override only
# allowed for explicit dev (e.g., SERVER_URL=http://localhost:3000).
SERVER_URL = os.getenv("SERVER_URL", "").rstrip("/") or "https://api.bagariaa.in"
SYNC_ROOT = os.getenv("SYNC_ROOT", os.path.join(os.path.expanduser("~"), "RGBC_Drive"))
POLL_INTERVAL = int(os.getenv("POLL_INTERVAL", "60"))
FULL_SCAN_INTERVAL = int(os.getenv("FULL_SCAN_INTERVAL", "300"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

NODE_ROLE = os.getenv("NODE_ROLE", "MASTER").upper()
MASTER_PORT = int(os.getenv("MASTER_PORT", "8741"))
DEVICE_NAME = os.getenv("DEVICE_NAME", platform.node())
DEVICE_ID = os.getenv("DEVICE_ID", "")
HEARTBEAT_INTERVAL = int(os.getenv("HEARTBEAT_INTERVAL", "30"))

TUNNEL_URL = os.getenv("TUNNEL_URL", "")
CLOUDFLARED_TOKEN = os.getenv("CLOUDFLARED_TOKEN", "")
TUNNEL_ENABLED = os.getenv("TUNNEL_ENABLED", "true").lower() in ("true", "1", "yes")

# ── Logging (write to EXTERNAL dir so logs survive updates) ──────────
log_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(log_fmt)
log_file = EXTERNAL_DIR / "rgbc_drive.log"
file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setFormatter(log_fmt)
root_logger = logging.getLogger("RGBCDrive")
root_logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
root_logger.addHandler(console_handler)
root_logger.addHandler(file_handler)
logger = logging.getLogger("RGBCDrive.Main")

# ── Import components ────────────────────────────────────────────────
from sync_db import SyncDatabase
from scanner import DirectoryScanner
from api_client import APIClient
from master_oauth import MasterOAuth
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


def get_or_claim_owner(oauth_user_id: str) -> Optional[str]:
    """
    Returns the master's owner userId.

    On first run: claims the master for the OAuth-authenticated user
    by writing .rgbc_owner to the EXTERNAL dir (alongside .env, NOT
    inside SYNC_ROOT — sync folder may be on a removable drive).

    On subsequent runs: refuses to start if the OAuth user differs
    from the persisted owner. This is the user-facing arm of the
    cross-user access defense; verify_auth() in master_api.py is the
    network-facing arm.

    Returns the canonical owner userId on success, None on mismatch
    or unrecoverable error.
    """
    owner_file = EXTERNAL_DIR / ".rgbc_owner"

    if not oauth_user_id:
        logger.error(
            "❌ OAuth flow did not return a userId claim. Cannot establish "
            "owner binding. Master will not start."
        )
        return None

    if owner_file.exists():
        try:
            existing = owner_file.read_text(encoding="utf-8").strip()
        except OSError as e:
            logger.error(f"❌ Cannot read owner file {owner_file}: {e}")
            return None

        if existing == oauth_user_id:
            logger.info(f"🔒 Master owned by userId: {existing}")
            return existing

        logger.error(
            "❌ This RGBC Drive is claimed by a different Google account.\n"
            f"   Claimed by:    userId={existing}\n"
            f"   You signed in: userId={oauth_user_id}\n"
            "\n"
            "   To re-claim this device for a new user, delete BOTH:\n"
            f"     {owner_file}\n"
            f"     {os.path.join(SYNC_ROOT, '.rgbc_oauth_cache.json')}\n"
            "   then restart RGBC Drive.\n"
            "\n"
            "   ⚠️  Only do this if you actually intend to change the device "
            "owner — this is a security boundary."
        )
        return None

    # First run — claim the master atomically
    try:
        tmp = str(owner_file) + ".tmp"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(oauth_user_id)
        os.replace(tmp, str(owner_file))
        if os.name != "nt":
            try:
                os.chmod(str(owner_file), 0o600)
            except OSError:
                pass
    except OSError as e:
        logger.error(f"❌ Cannot write owner file {owner_file}: {e}")
        return None

    logger.info(f"🔒 Master claimed by userId: {oauth_user_id}")
    return oauth_user_id


def register_with_gateway(api: APIClient, device_id: str, db: SyncDatabase, tunnel_getter=None) -> bool:
    try:
        stats = db.get_stats()
        disk = shutil.disk_usage(SYNC_ROOT)
        tunnel_url = tunnel_getter() if tunnel_getter else None

        payload = {
            "device_id": device_id, "device_name": DEVICE_NAME, "device_type": "DESKTOP",
            "role": NODE_ROLE, "tunnel_url": tunnel_url, "local_ip": get_local_ip(),
            "local_port": MASTER_PORT, "os_platform": sys.platform, "os_version": platform.release(),
            "app_version": "3.0", "file_count": stats.get("total_files", 0), "disk_free_bytes": disk.free,
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
    ║         RGBC Drive v3.0                  ║
    ║    P2P Master Node — Generic Build       ║
    ╚══════════════════════════════════════════╝
    """)

    # ── Validate config ──────────────────────────────────────────
    if not SERVER_URL:
        logger.error("❌ SERVER_URL not set. Run the setup wizard or edit .env.")
        input("Press Enter to exit...")
        sys.exit(1)

    os.makedirs(SYNC_ROOT, exist_ok=True)
    logger.info(f"Mode:         {'PyInstaller bundle' if is_frozen() else 'Development'}")
    logger.info(f"Internal dir: {INTERNAL_DIR}")
    logger.info(f"External dir: {EXTERNAL_DIR}")
    logger.info(f"Config:       {ENV_PATH}")
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

    # ── Google OAuth (Sprint 3) ──────────────────────────────────
    # Resolve client_secret.json from external dir (user provides it)
# ── Google OAuth (Sprint 3.1) ────────────────────────────────
    # Resolve client_secret.json with fallback chain:
    #   1. EXTERNAL_DIR/client_secret.json — dev override or per-install rotation
    #   2. INTERNAL_DIR/client_secret.json — bundled in the .exe (production)
    # Per Google's installed-app guidance, the desktop client secret is
    # not confidential and ships inside the bundle.
    _external_cs = EXTERNAL_DIR / "client_secret.json"
    _internal_cs = INTERNAL_DIR / "client_secret.json"
    if _external_cs.is_file():
        client_secret = str(_external_cs)
        logger.info(f"🔐 Using external client_secret.json: {_external_cs}")
    elif _internal_cs.is_file():
        client_secret = str(_internal_cs)
        logger.info(f"🔐 Using bundled client_secret.json")
    else:
        logger.error(
            f"❌ client_secret.json not found in either:\n"
            f"   {_external_cs}\n"
            f"   {_internal_cs}\n"
            "   Rebuild the .exe with client_secret.json next to the .spec file."
        )
        input("Press Enter to exit...")
        sys.exit(1)
    oauth_cache = os.path.join(SYNC_ROOT, ".rgbc_oauth_cache.json")

    oauth = MasterOAuth(
        gateway_url=SERVER_URL,
        client_secret_path=client_secret,
        cache_path=oauth_cache,
    )

    if not oauth.jwt:
        logger.error("❌ Failed to authenticate. Check client_secret.json and Gateway availability.")
        input("Press Enter to exit...")
        sys.exit(1)

    logger.info(f"🔐 Authenticated as: {oauth.email}")

    # ── Owner binding (Sprint 3.1 — cross-user access defense) ───
    # The first OAuth login on this machine claims it. Subsequent
    # OAuth flows must match the claimed userId or the master refuses
    # to start. This is enforced again at the network boundary by
    # verify_auth() in master_api.py.
    owner_user_id = get_or_claim_owner(oauth.user_id)
    if not owner_user_id:
        input("Press Enter to exit...")
        sys.exit(1)

    # ── API Client (dynamic Bearer JWT via OAuth adapter) ────────
    api = APIClient(server_url=SERVER_URL, oauth_manager=oauth)

    logger.info("Testing API Gateway connectivity...")
    if api.health_check():
        logger.info("✅ API Gateway is reachable")
    else:
        logger.warning("⚠️ API Gateway is offline — will retry in background")

    logger.info("Running initial directory scan...")
    scanner = DirectoryScanner(SYNC_ROOT, db)
    scan_result = scanner.scan()
    logger.info(f"Initial scan: {scan_result.new_files} new, {scan_result.unchanged_files} unchanged, {scan_result.deleted_files} deleted")

    # ── Start Master API (RS256, owner-bound) ────────────────────
    if NODE_ROLE == "MASTER":
        # Set PUBLIC_KEY_PATH env var so master_api.py finds it
        os.environ["PUBLIC_KEY_PATH"] = get_public_key_path()
        start_master_api(
            sync_root=SYNC_ROOT,
            db=db,
            scanner=scanner,
            owner_user_id=owner_user_id,
            port=MASTER_PORT,
        )
        logger.info(f"🌐 Master API listening on http://0.0.0.0:{MASTER_PORT}")

    # ── Cloudflare Tunnel ────────────────────────────────────────
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
                logger.warning(f"⚠️ Tunnel URL not discovered. LAN: http://{get_local_ip()}:{MASTER_PORT}")
        else:
            logger.info(f"🔗 Using pre-configured tunnel: {TUNNEL_URL}")

        tunnel_getter = lambda: tunnel.get_url(timeout=0)

    elif NODE_ROLE == "MASTER" and not TUNNEL_ENABLED:
        logger.info(f"🔗 Tunnel disabled. LAN-only: http://{get_local_ip()}:{MASTER_PORT}")

    # ── Register + Heartbeat ─────────────────────────────────────
    register_with_gateway(api, DEVICE_ID, db, tunnel_getter=tunnel_getter)

    heartbeat_stop = threading.Event()
    threading.Thread(
        target=heartbeat_loop, args=(api, DEVICE_ID, db, heartbeat_stop),
        kwargs={"tunnel_getter": tunnel_getter}, daemon=True, name="Heartbeat",
    ).start()
    logger.info(f"💓 Heartbeat started (every {HEARTBEAT_INTERVAL}s)")

    # ── Tray callbacks ───────────────────────────────────────────
    poller_ref = {"obj": None}
    watcher_ref = {"obj": None}

    def on_force_sync():
        if poller_ref["obj"]: poller_ref["obj"].force_sync()

    def on_pause():
        if poller_ref["obj"]: poller_ref["obj"].stop()
        if watcher_ref["obj"]: watcher_ref["obj"].stop()

    def on_resume():
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

    # ── System tray ──────────────────────────────────────────────
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

    tray.run()
    logger.info("Goodbye!")


if __name__ == "__main__":
    main()