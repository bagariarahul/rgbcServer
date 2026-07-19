"""
RGBC Drive — System Tray Icon

Lightweight Windows taskbar presence using pystray.
Shows sync status and provides a right-click context menu.

Menu items:
  - Status line (disabled, shows current state)
  - File count line (disabled, shows stats)
  - Force Sync Now
  - Open RGBC Drive Folder
  - Pause / Resume Sync
  - Quit
"""

import os
import sys
import logging
import threading
from typing import Callable, Optional

from PIL import Image, ImageDraw
import pystray

logger = logging.getLogger("RGBCDrive.Tray")

# Status constants
STATUS_SYNCING = "syncing"
STATUS_UP_TO_DATE = "up_to_date"
STATUS_OFFLINE = "offline"
STATUS_PAUSED = "paused"
STATUS_ERROR = "error"


class TrayIcon:
    """
    System tray icon for RGBC Drive.

    Usage:
        tray = TrayIcon(
            sync_root="C:/RGBC_Drive",
            on_force_sync=lambda: poller.force_sync(),
            on_pause=lambda: ...,
            on_resume=lambda: ...,
            on_quit=lambda: ...,
        )
        tray.set_status(STATUS_UP_TO_DATE, "Up to date — 42 files synced")
        tray.run()  # Blocks the calling thread
    """

    def __init__(
        self,
        sync_root: str,
        on_force_sync: Callable,
        on_pause: Callable,
        on_resume: Callable,
        on_quit: Callable,
        on_open_dashboard: Optional[Callable] = None,  # Sprint 3.5
    ):
        self.sync_root = sync_root
        self._on_force_sync = on_force_sync
        self._on_pause = on_pause
        self._on_resume = on_resume
        self._on_quit = on_quit
        self._on_open_dashboard = on_open_dashboard  # Sprint 3.5

        self._status = STATUS_OFFLINE
        self._status_text = "Starting..."
        self._stats_text = ""
        self._paused = False
        self._icon: Optional[pystray.Icon] = None

    def set_status(self, status: str, text: str = ""):
        """Update the tray icon status. Thread-safe."""
        self._status = status
        self._status_text = text or self._status_label(status)

        if self._icon:
            self._icon.icon = self._create_icon(status)
            self._icon.title = f"RGBC Drive — {self._status_text}"
            self._icon.update_menu()

    def set_stats(self, total: int, synced: int, pending: int, size_bytes: int):
        """Update the stats line. Thread-safe."""
        size_mb = size_bytes / (1024 * 1024)
        self._stats_text = f"{synced}/{total} synced • {size_mb:.0f} MB • {pending} pending"
        if self._icon:
            self._icon.update_menu()

    def run(self):
        """Start the tray icon. Blocks the calling thread."""
        self._icon = pystray.Icon(
            name="RGBC Drive",
            icon=self._create_icon(self._status),
            title=f"RGBC Drive — {self._status_text}",
            menu=self._build_menu(),
        )
        logger.info("🔔 System tray icon started")
        self._icon.run()

    def stop(self):
        """Stop the tray icon."""
        if self._icon:
            self._icon.stop()

    # ── Menu builder ─────────────────────────────────────────────────

    def _build_menu(self) -> pystray.Menu:
        return pystray.Menu(
            pystray.MenuItem(
                lambda _: f"Status: {self._status_text}",
                action=None,
                enabled=False,
            ),
            pystray.MenuItem(
                lambda _: self._stats_text if self._stats_text else "No files tracked",
                action=None,
                enabled=False,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Force Sync Now",
                action=lambda icon, item: self._on_force_sync(),
            ),
               pystray.MenuItem(
                "Open RGBC Drive Folder",
                action=lambda icon, item: self._open_folder(),
            ),
            pystray.MenuItem(
                "Show RGBC Drive",
                action=lambda icon, item: self._open_dashboard(),
                visible=lambda _: self._on_open_dashboard is not None,
                # Sprint 3.5c: default=True binds this to left/double-click on
                # the icon itself. Without it the window is unreachable once
                # closed — right-clicking a hidden overflow icon is not a UX.
                default=True,
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                lambda _: "Resume Sync" if self._paused else "Pause Sync",
                action=lambda icon, item: self._toggle_pause(),
            ),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem(
                "Quit RGBC Drive",
                action=lambda icon, item: self._quit(),
            ),
        )

    def _toggle_pause(self):
        self._paused = not self._paused
        if self._paused:
            self._on_pause()
            self.set_status(STATUS_PAUSED, "Paused")
        else:
            self._on_resume()
            self.set_status(STATUS_UP_TO_DATE, "Resumed")

    def _open_dashboard(self):
        """Open the local dashboard in the default browser. Sprint 3.5."""
        if self._on_open_dashboard is None:
            return
        try:
            self._on_open_dashboard()
        except Exception as e:
            logger.error(f"Failed to open dashboard: {e}")

    def _quit(self):
        """Quit the application."""
        logger.info("🛑 Quit requested from tray")
        self._on_quit()
        self.stop()

    # ── Icon generation ──────────────────────────────────────────────

    @staticmethod
    def _create_icon(status: str) -> Image.Image:
        """
        Generate a 64x64 tray icon with color based on status.
        Uses Pillow to draw a simple circle — no external icon files needed.
        """
        size = 64
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)

        # Status colors
        colors = {
            STATUS_SYNCING: "#2196F3",     # Blue
            STATUS_UP_TO_DATE: "#4CAF50",  # Green
            STATUS_OFFLINE: "#9E9E9E",     # Grey
            STATUS_PAUSED: "#FF9800",      # Orange
            STATUS_ERROR: "#F44336",        # Red
        }
        color = colors.get(status, "#9E9E9E")

        # Draw filled circle
        margin = 4
        draw.ellipse(
            [margin, margin, size - margin, size - margin],
            fill=color,
            outline="white",
            width=2,
        )

        # Draw a "cloud" shape in the center (simplified)
        cx, cy = size // 2, size // 2
        draw.ellipse([cx - 12, cy - 6, cx + 12, cy + 6], fill="white")
        draw.ellipse([cx - 8, cy - 12, cx + 4, cy], fill="white")
        draw.ellipse([cx + 2, cy - 10, cx + 14, cy + 2], fill="white")

        return img

    @staticmethod
    def _status_label(status: str) -> str:
        labels = {
            STATUS_SYNCING: "Syncing...",
            STATUS_UP_TO_DATE: "Up to date",
            STATUS_OFFLINE: "Offline",
            STATUS_PAUSED: "Paused",
            STATUS_ERROR: "Error",
        }
        return labels.get(status, "Unknown")