"""
RGBC Drive — First-Run Setup Wizard (Sprint 3, revised)

Replaces the old wizard that asked the user for a "Gateway URL". The
gateway is api.bagariaa.in, operated by RGBC. Users never type it in,
and they should not be able to point the app at a fake gateway.

What the wizard does now:
  1. Pick a sync folder (default: ~/RGBC_Drive)
  2. Pick a friendly device name (default: hostname)

What it does NOT ask (auto-handled):
  - Gateway URL                  hardcoded constant (GATEWAY_URL)
  - JWT / session secrets        gateway issues RS256, master only verifies
  - Cloudflare tunnel token      quick tunnel by default; named-tunnel
                                 provisioning will come from the gateway
                                 in a later sprint
  - Device ID                    generated lazily by rgbc_drive.py

Public surface (matches what rgbc_drive.py imports):
  needs_setup(env_path: str) -> bool
  run_setup_wizard(env_path: str) -> bool

Returns True from run_setup_wizard on save, False on cancel/window-close.
"""

import logging
import os
import platform
from datetime import datetime, timezone

logger = logging.getLogger("RGBCDrive.SetupWizard")

# ─── HARDCODED CONSTANT — never user-configurable in the wizard ──────
# Pinning this in code (not just in the generated .env) means a
# tampered .env cannot redirect OAuth to an attacker-controlled host.
# rgbc_drive.py should also fall back to this value if SERVER_URL
# is missing from the environment.
GATEWAY_URL = "https://api.bagariaa.in"

DEFAULT_SYNC_DIR_NAME = "RGBC_Drive"
DEFAULT_MASTER_PORT = 8741
MAX_PATH_LEN = 260           # Windows MAX_PATH; sane upper bound elsewhere
MAX_DEVICE_NAME_LEN = 64


# ═════════════════════════════════════════════════════════════════════
# Public surface used by rgbc_drive.py
# ═════════════════════════════════════════════════════════════════════

def needs_setup(env_path: str) -> bool:
    """
    Return True if .env is missing, unreadable, or doesn't contain the
    minimum keys required to start RGBC Drive.

    A half-written .env (e.g. crash mid-save) is treated as "needs setup"
    because we use SYNC_ROOT presence as the completion sentinel.
    """
    if not os.path.isfile(env_path):
        return True
    try:
        with open(env_path, "r", encoding="utf-8") as f:
            content = f.read()
    except (OSError, UnicodeDecodeError) as e:
        logger.warning(f"Could not read {env_path}: {e} — treating as fresh install.")
        return True

    # SYNC_ROOT is the sentinel that the wizard ran to completion.
    return "SYNC_ROOT=" not in content


def run_setup_wizard(env_path: str) -> bool:
    """
    Open the customtkinter wizard window. Blocks until the user clicks
    Save & Launch (returns True) or closes the window (returns False).
    """
    try:
        import customtkinter as ctk
        from tkinter import filedialog
    except ImportError as e:
        logger.error(
            f"customtkinter not available: {e}\n"
            "  Run: pip install customtkinter==5.2.2\n"
            "  This should not happen inside the bundled .exe."
        )
        return False

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    saved = {"value": False}

    root = ctk.CTk()
    root.title("RGBC Drive — Setup")
    root.geometry("560x540")
    root.resizable(False, False)

    container = ctk.CTkFrame(root, fg_color="transparent")
    container.pack(fill="both", expand=True, padx=32, pady=24)

    # ── Header ───────────────────────────────────────────────────────
    ctk.CTkLabel(
        container,
        text="Welcome to RGBC Drive",
        font=ctk.CTkFont(size=24, weight="bold"),
        anchor="w",
    ).pack(fill="x", pady=(0, 4))

    ctk.CTkLabel(
        container,
        text=(
            "One folder on this PC, one app on your phone — "
            "synced automatically once you sign in."
        ),
        font=ctk.CTkFont(size=12),
        text_color="gray70",
        anchor="w",
        justify="left",
        wraplength=480,
    ).pack(fill="x", pady=(0, 24))

    # ── Sync folder ──────────────────────────────────────────────────
    ctk.CTkLabel(
        container,
        text="Sync Folder",
        font=ctk.CTkFont(size=14, weight="bold"),
        anchor="w",
    ).pack(fill="x", pady=(0, 4))

    ctk.CTkLabel(
        container,
        text=(
            "Files placed here are backed up. New files from your phone "
            "will appear here too."
        ),
        font=ctk.CTkFont(size=11),
        text_color="gray60",
        anchor="w",
        justify="left",
        wraplength=480,
    ).pack(fill="x", pady=(0, 8))

    folder_row = ctk.CTkFrame(container, fg_color="transparent")
    folder_row.pack(fill="x", pady=(0, 20))

    default_folder = os.path.join(os.path.expanduser("~"), DEFAULT_SYNC_DIR_NAME)
    folder_var = ctk.StringVar(value=default_folder)

    folder_entry = ctk.CTkEntry(folder_row, textvariable=folder_var, height=36)
    folder_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))

    def pick_folder():
        chosen = filedialog.askdirectory(
            initialdir=folder_var.get(),
            title="Choose your RGBC Drive sync folder",
        )
        if chosen:
            folder_var.set(chosen)

    ctk.CTkButton(
        folder_row, text="Browse", command=pick_folder, width=88, height=36
    ).pack(side="right")

    # ── Device name ──────────────────────────────────────────────────
    ctk.CTkLabel(
        container,
        text="Device Name",
        font=ctk.CTkFont(size=14, weight="bold"),
        anchor="w",
    ).pack(fill="x", pady=(0, 4))

    ctk.CTkLabel(
        container,
        text="Shown in your phone app when picking which PC to back up to.",
        font=ctk.CTkFont(size=11),
        text_color="gray60",
        anchor="w",
        justify="left",
    ).pack(fill="x", pady=(0, 8))

    name_var = ctk.StringVar(value=platform.node() or "My PC")
    ctk.CTkEntry(container, textvariable=name_var, height=36).pack(
        fill="x", pady=(0, 16)
    )

    # ── Status / error line ──────────────────────────────────────────
    status_var = ctk.StringVar(value="")
    ctk.CTkLabel(
        container,
        textvariable=status_var,
        text_color="#ff6b6b",
        font=ctk.CTkFont(size=12),
        anchor="w",
        wraplength=480,
        justify="left",
    ).pack(fill="x", pady=(0, 8))

    # ── Save action ──────────────────────────────────────────────────
    def validate_and_save():
        sync_folder = folder_var.get().strip()
        device_name = name_var.get().strip()

        # ── Validation ────────────────────────────────────────────
        if not sync_folder:
            status_var.set("Sync folder is required.")
            return
        if len(sync_folder) > MAX_PATH_LEN:
            status_var.set(
                f"Sync folder path is too long (max {MAX_PATH_LEN} characters)."
            )
            return
        if not device_name:
            status_var.set("Device name is required.")
            return
        if len(device_name) > MAX_DEVICE_NAME_LEN:
            status_var.set(
                f"Device name must be {MAX_DEVICE_NAME_LEN} characters or fewer."
            )
            return
        # Block control chars and newlines in device name (would corrupt .env
        # parsing and could be reflected back in the gateway UI).
        if any(ord(c) < 32 for c in device_name):
            status_var.set("Device name contains invalid characters.")
            return

        # ── Folder writability check ──────────────────────────────
        try:
            os.makedirs(sync_folder, exist_ok=True)
            test_file = os.path.join(sync_folder, ".rgbc_write_test")
            with open(test_file, "w", encoding="utf-8") as tf:
                tf.write("ok")
            os.unlink(test_file)
        except OSError as e:
            status_var.set(f"Cannot write to that folder: {e}")
            return

        # ── Persist .env atomically ───────────────────────────────
        try:
            _write_env(env_path, sync_folder=sync_folder, device_name=device_name)
        except OSError as e:
            status_var.set(f"Failed to save .env: {e}")
            return

        saved["value"] = True
        root.destroy()

    ctk.CTkButton(
        container,
        text="Save & Launch RGBC Drive",
        command=validate_and_save,
        height=44,
        font=ctk.CTkFont(size=14, weight="bold"),
    ).pack(fill="x", pady=(0, 8))

    ctk.CTkLabel(
        container,
        text=(
            "You can edit the .env file later to change settings. "
            "You'll sign in with Google after this."
        ),
        font=ctk.CTkFont(size=11),
        text_color="gray50",
        wraplength=480,
        justify="center",
    ).pack()

    root.bind("<Return>", lambda _: validate_and_save())
    root.protocol("WM_DELETE_WINDOW", root.destroy)  # X button = cancel

    root.mainloop()
    return saved["value"]


# ═════════════════════════════════════════════════════════════════════
# .env writer
# ═════════════════════════════════════════════════════════════════════

def _write_env(env_path: str, *, sync_folder: str, device_name: str) -> None:
    """
    Write the .env file. Atomic (temp + os.replace) so a crash mid-write
    cannot leave a half-written file that needs_setup() then accepts.
    """
    sync_folder = os.path.normpath(sync_folder)
    device_name = device_name.replace("\n", " ").replace("\r", " ")

    content = (
        "# RGBC Drive configuration\n"
        "# Generated by the first-run setup wizard.\n"
        f"# Created: {_utc_now()}\n"
        "\n"
        "# ── Gateway (do not change in production) ─────────────────\n"
        f"SERVER_URL={GATEWAY_URL}\n"
        "\n"
        "# ── User settings ─────────────────────────────────────────\n"
        f"SYNC_ROOT={sync_folder}\n"
        f"DEVICE_NAME={device_name}\n"
        "\n"
        "# ── Master node (advanced) ────────────────────────────────\n"
        "NODE_ROLE=MASTER\n"
        f"MASTER_PORT={DEFAULT_MASTER_PORT}\n"
        "HEARTBEAT_INTERVAL=30\n"
        "\n"
        "# ── Cloudflare Tunnel ─────────────────────────────────────\n"
        "# TUNNEL_ENABLED must be 'true' for phone-to-PC sync to work.\n"
        "# CLOUDFLARED_TOKEN is provisioned by the gateway in a later release.\n"
        "TUNNEL_ENABLED=true\n"
        "CLOUDFLARED_TOKEN=\n"
        "TUNNEL_URL=\n"
        "\n"
        "# ── Sync timing ───────────────────────────────────────────\n"
        "POLL_INTERVAL=60\n"
        "FULL_SCAN_INTERVAL=300\n"
        "\n"
        "# ── Logging ───────────────────────────────────────────────\n"
        "LOG_LEVEL=INFO\n"
    )

    tmp_path = env_path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(content)
    os.replace(tmp_path, env_path)

    # On Unix, restrict perms. On Windows, %LOCALAPPDATA% is already
    # per-user; no portable equivalent of chmod 600.
    if os.name != "nt":
        try:
            os.chmod(env_path, 0o600)
        except OSError:
            pass


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")