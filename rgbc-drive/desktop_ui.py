"""
RGBC Drive — Desktop UI (Sprint 3.5b)

customtkinter main window. Replaces the browser-based dashboard. The
master_api.py JSON endpoints stay the same — this UI just renders the
data natively instead of in HTML.

Architecture:
  - DesktopUI owns the Tk root and runs the mainloop on the main thread
  - Each "pane" is a CTkFrame subclass, swapped in/out on sidebar nav clicks
  - All API calls run on a background ThreadPoolExecutor so we never
    block the Tk event loop
  - UI updates from background threads use root.after(0, callback) — Tk
    is NOT thread-safe and any direct widget call from a worker thread
    will eventually cause a crash that's hellish to reproduce

Threading rules — read these once and internalize them:
  * NEVER call widget.configure() / widget.set() / similar from a
    background thread.
  * To update a widget after a network call, wrap the assignment in
    self.dispatch(callable) which posts it to the Tk main thread.
  * The polling refresh loop uses self.after(ms, ...) which IS safe
    because after() callbacks run on the main thread.
"""

from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Callable, Optional

import tkinter as tk

import customtkinter as ctk

from ui_client import (
    FileItem,
    FilesPage,
    RecentItem,
    StatusResponse,
    UIClient,
    UploadItem,
)

logger = logging.getLogger("RGBCDrive.UI")

# ── Theme constants — mirror the dashboard's CSS variables ──────────
COLOR_BG = "#1a1a1a"
COLOR_CARD = "#242424"
COLOR_CARD_HOVER = "#2c2c2c"
COLOR_BORDER = "#3a3a3a"
COLOR_TEXT = "#e0e0e0"
COLOR_MUTED = "#888888"
COLOR_FAINT = "#666666"
COLOR_ACCENT = "#1f6aa5"
COLOR_ACCENT_HOVER = "#2880c4"
COLOR_SUCCESS = "#4ade80"
COLOR_WARNING = "#fbbf24"
COLOR_DANGER = "#ef4444"
COLOR_INFO = "#60a5fa"

# Refresh cadence
STATUS_POLL_MS = 5_000   # status pane
ACTIVE_POLL_MS = 2_000   # uploads pane (shorter — chunks land fast)
FILES_POLL_MS = 15_000   # files list (longer — table doesn't change often)


# ═════════════════════════════════════════════════════════════════════
# Helpers
# ═════════════════════════════════════════════════════════════════════

def fmt_bytes(n: int) -> str:
    if n is None or n <= 0:
        return "0 B"
    units = ["B", "KB", "MB", "GB", "TB"]
    i = 0
    v = float(n)
    while v >= 1024 and i < len(units) - 1:
        v /= 1024
        i += 1
    return f"{v:.1f} {units[i]}" if (v < 10 and i > 0) else f"{v:.0f} {units[i]}"


def fmt_uptime(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        h = seconds // 3600
        m = (seconds % 3600) // 60
        return f"{h}h {m}m"
    d = seconds // 86400
    h = (seconds % 86400) // 3600
    return f"{d}d {h}h"


def fmt_relative(iso: Optional[str]) -> str:
    if not iso:
        return "—"
    try:
        # Tolerate trailing Z or offset
        s = iso.replace("Z", "+00:00")
        t = datetime.fromisoformat(s)
        diff = (datetime.now(timezone.utc) - t).total_seconds()
        if diff < 0:
            return t.strftime("%Y-%m-%d %H:%M")
        if diff < 60:
            return "just now"
        if diff < 3600:
            return f"{int(diff // 60)}m ago"
        if diff < 86400:
            return f"{int(diff // 3600)}h ago"
        return f"{int(diff // 86400)}d ago"
    except Exception:
        return "—"


# ═════════════════════════════════════════════════════════════════════
# Pane base class
# ═════════════════════════════════════════════════════════════════════

class Pane(ctk.CTkFrame):
    """
    Base class for every pane (Status, Files, Activity, Uploads, Settings).
    Provides:
      - self.client: UIClient (already wired)
      - self.dispatch(fn, *args): post `fn(*args)` to the Tk main thread
      - self.run_async(target, on_result, on_error): run target() on a
        background thread and dispatch its result back to the main thread
      - on_show() / on_hide() lifecycle hooks (override in subclasses)
    """

    def __init__(self, parent, ui: "DesktopUI", **kw):
        super().__init__(parent, fg_color=COLOR_BG, **kw)
        self.ui = ui
        self.client = ui.client
        self._is_active = False

    def dispatch(self, fn: Callable, *args, **kwargs) -> None:
        """Schedule fn(*args, **kwargs) on the Tk main thread."""
        try:
            self.ui.root.after(0, lambda: fn(*args, **kwargs))
        except Exception:
            # Tk root has been destroyed — drop the callback silently
            pass

    def run_async(
        self,
        target: Callable,
        on_result: Optional[Callable] = None,
        on_error: Optional[Callable] = None,
    ) -> None:
        """
        Run `target()` on a worker thread. on_result/on_error run on the
        Tk main thread automatically. Either may be None.
        """
        def _wrapper():
            try:
                result = target()
            except Exception as e:
                if on_error:
                    self.dispatch(on_error, e)
                else:
                    logger.exception(f"Background task failed: {e}")
                return
            if on_result:
                self.dispatch(on_result, result)

        self.ui.executor.submit(_wrapper)

    # Lifecycle hooks
    def on_show(self) -> None:
        self._is_active = True

    def on_hide(self) -> None:
        self._is_active = False


# ═════════════════════════════════════════════════════════════════════
# Status pane (first complete pane — others land in turn 2)
# ═════════════════════════════════════════════════════════════════════

class StatusPane(Pane):
    """The Status tab. Shows owner, sync stats, tunnel, action buttons."""

    def __init__(self, parent, ui: "DesktopUI"):
        super().__init__(parent, ui)
        self._poll_after_id: Optional[str] = None
        self._build()

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)

        # ── Header ────────────────────────────────────────────────
        header = ctk.CTkFrame(self, fg_color=COLOR_CARD, corner_radius=8)
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(24, 12))
        header.grid_columnconfigure(0, weight=1)

        title_row = ctk.CTkFrame(header, fg_color="transparent")
        title_row.grid(row=0, column=0, sticky="ew", padx=20, pady=16)
        title_row.grid_columnconfigure(0, weight=1)

        title_text = ctk.CTkFrame(title_row, fg_color="transparent")
        title_text.grid(row=0, column=0, sticky="w")

        ctk.CTkLabel(
            title_text, text="RGBC Drive",
            font=ctk.CTkFont(size=22, weight="bold"),
            text_color=COLOR_TEXT, anchor="w",
        ).pack(anchor="w")

        self.subtitle = ctk.CTkLabel(
            title_text, text="Loading…",
            font=ctk.CTkFont(size=12), text_color=COLOR_MUTED, anchor="w",
        )
        self.subtitle.pack(anchor="w", pady=(2, 0))

        self.badge = ctk.CTkLabel(
            title_row, text="—",
            font=ctk.CTkFont(size=11, weight="bold"),
            fg_color=COLOR_BORDER, text_color=COLOR_MUTED,
            corner_radius=12, padx=12, pady=4,
        )
        self.badge.grid(row=0, column=1, sticky="e")

        # ── Stats grid ────────────────────────────────────────────
        self.stats_frame = ctk.CTkFrame(header, fg_color="transparent")
        self.stats_frame.grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 16))
        self._stat_labels: dict[str, tuple[ctk.CTkLabel, ctk.CTkLabel]] = {}
        self._build_stat_cells()

        # ── Action bar ────────────────────────────────────────────
        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.grid(row=1, column=0, sticky="ew", padx=24, pady=(4, 12))

        self.btn_scan = ctk.CTkButton(
            actions, text="Scan now", width=110, height=32,
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            command=self._on_scan,
        )
        self.btn_scan.pack(side="left", padx=(0, 8))

        self.btn_pause = ctk.CTkButton(
            actions, text="Pause sync", width=110, height=32,
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            command=self._on_pause,
        )
        self.btn_pause.pack(side="left", padx=(0, 8))

        self.btn_resume = ctk.CTkButton(
            actions, text="Resume sync", width=110, height=32,
            fg_color=COLOR_ACCENT, hover_color=COLOR_ACCENT_HOVER,
            command=self._on_resume,
        )
        # Hidden initially; toggled in _render()
        self.btn_resume.pack_forget()

        self.btn_switch = ctk.CTkButton(
            actions, text="Switch account…", width=140, height=32,
            fg_color=COLOR_DANGER, hover_color="#dc2626",
            command=self._on_switch,
        )
        self.btn_switch.pack(side="right")

        self.action_status = ctk.CTkLabel(
            actions, text="",
            font=ctk.CTkFont(size=11), text_color=COLOR_MUTED,
        )
        self.action_status.pack(side="left", padx=(12, 0))

    def _build_stat_cells(self) -> None:
        """Create the 7 stat cells in a 4-column grid."""
        labels = [
            ("sync_root", "Sync folder"),
            ("tunnel_url", "Tunnel URL"),
            ("files", "Files"),
            ("pending", "Pending upload"),
            ("disk_free", "Disk free"),
            ("uptime", "Uptime"),
            ("app_version", "App version"),
        ]
        cols = 4
        for i in range(cols):
            self.stats_frame.grid_columnconfigure(i, weight=1, uniform="stats")

        for i, (key, label) in enumerate(labels):
            r, c = divmod(i, cols)
            cell = ctk.CTkFrame(self.stats_frame, fg_color="transparent")
            cell.grid(row=r, column=c, sticky="nw", padx=4, pady=6)

            label_w = ctk.CTkLabel(
                cell, text=label.upper(),
                font=ctk.CTkFont(size=10, weight="bold"),
                text_color=COLOR_FAINT, anchor="w",
            )
            label_w.pack(anchor="w")

            value_w = ctk.CTkLabel(
                cell, text="—",
                font=ctk.CTkFont(size=13),
                text_color=COLOR_TEXT, anchor="w", justify="left",
                wraplength=240,
            )
            value_w.pack(anchor="w", pady=(2, 0))

            self._stat_labels[key] = (label_w, value_w)

    # ── Lifecycle ────────────────────────────────────────────────────

    def on_show(self) -> None:
        super().on_show()
        self._refresh_now()
        self._schedule_poll()

    def on_hide(self) -> None:
        super().on_hide()
        if self._poll_after_id:
            try:
                self.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None

    def _schedule_poll(self) -> None:
        if not self._is_active:
            return
        self._poll_after_id = self.after(STATUS_POLL_MS, self._poll_tick)

    def _poll_tick(self) -> None:
        self._poll_after_id = None
        if self._is_active:
            self._refresh_now()
            self._schedule_poll()

    def _refresh_now(self) -> None:
        self.run_async(
            target=self.client.get_status,
            on_result=self._render,
            on_error=self._render_error,
        )

    # ── Render ───────────────────────────────────────────────────────

    def _render(self, status: StatusResponse) -> None:
        # No owner_email in master_api yet — show truncated user_id as
        # an identity hint. If you add `ownerEmail` to /api/dashboard/status
        # later, swap this in.
        owner_label = (
            f"User {status.owner_user_id[:8]}…" if status.owner_user_id
            else "Not signed in"
        )
        self.subtitle.configure(
            text=f"{owner_label}  ·  {status.device_name or 'this device'}"
        )

        # We track sync_paused on the UI side (master_api doesn't expose it).
        # The actual button visibility is updated by the action handlers.
        if status.tunnel_online:
            self.badge.configure(text="ONLINE", fg_color="#14532d", text_color=COLOR_SUCCESS)
        else:
            self.badge.configure(text="LAN ONLY", fg_color="#78350f", text_color=COLOR_WARNING)

        self._stat_labels["sync_root"][1].configure(text=status.sync_root or "—")
        self._stat_labels["tunnel_url"][1].configure(text=status.tunnel_url or "Not connected")
        self._stat_labels["files"][1].configure(
            text=f"{status.total_files:,}  ({fmt_bytes(status.total_size_bytes)})"
        )
        self._stat_labels["pending"][1].configure(text=f"{status.pending_upload:,}")
        self._stat_labels["disk_free"][1].configure(
            text=fmt_bytes(status.disk_free_bytes) if status.disk_free_bytes else "—"
        )
        # Use last-heartbeat as a rough uptime/health signal since master_api
        # doesn't expose process uptime separately.
        if status.last_heartbeat_seconds_ago is not None:
            self._stat_labels["uptime"][1].configure(
                text=f"Heartbeat {status.last_heartbeat_seconds_ago}s ago"
            )
        else:
            self._stat_labels["uptime"][1].configure(text="—")
        self._stat_labels["app_version"][1].configure(text=status.app_version or "—")

    def _render_error(self, exc: Exception) -> None:
        msg = str(exc)
        self.subtitle.configure(text=f"Master unreachable: {msg[:120]}")
        self.badge.configure(text="OFFLINE", fg_color="#7f1d1d", text_color=COLOR_DANGER)

    # ── Action handlers ──────────────────────────────────────────────

    def _set_action_status(self, text: str, color: str = COLOR_MUTED) -> None:
        self.action_status.configure(text=text, text_color=color)

    def _on_scan(self) -> None:
        self._set_action_status("Scanning…")
        self.run_async(
            self.client.trigger_scan,
            on_result=lambda _r: self._set_action_status("Scan triggered", COLOR_SUCCESS),
            on_error=lambda e: self._set_action_status(f"Scan failed: {e}", COLOR_DANGER),
        )

    def _on_pause(self) -> None:
        self._set_action_status("Pausing sync…")
        self.run_async(
            self.client.pause_sync,
            on_result=lambda _r: (self._set_action_status("Sync paused", COLOR_SUCCESS), self._refresh_now()),
            on_error=lambda e: self._set_action_status(f"Pause failed: {e}", COLOR_DANGER),
        )

    def _on_resume(self) -> None:
        self._set_action_status("Resuming sync…")
        self.run_async(
            self.client.resume_sync,
            on_result=lambda _r: (self._set_action_status("Sync resumed", COLOR_SUCCESS), self._refresh_now()),
            on_error=lambda e: self._set_action_status(f"Resume failed: {e}", COLOR_DANGER),
        )

    def _on_switch(self) -> None:
        SwitchAccountDialog(self.ui.root, on_confirm=self._do_switch)

    def _do_switch(self) -> None:
        self._set_action_status("Switching account…")
        self.run_async(
            target=self.client.switch_account,
            on_result=lambda _r: self._set_action_status(
                "Account binding cleared. Restart RGBC Drive to sign in again.",
                COLOR_SUCCESS,
            ),
            on_error=lambda e: self._set_action_status(f"Switch failed: {e}", COLOR_DANGER),
        )


# ═════════════════════════════════════════════════════════════════════
# Polling pane base
# ═════════════════════════════════════════════════════════════════════

class PollingPane(Pane):
    """
    A Pane that refreshes on a timer while it is the visible pane.

    Subclasses set POLL_MS and implement fetch() (runs on a worker
    thread) and render(data) / render_error(exc) (run on the Tk main
    thread). Polling stops the moment the pane is hidden, so the four
    panes never compete for the executor's 4 workers.

    NOTE (Sprint 3.7): StatusPane predates this base and carries its own
    copy of the poll machinery. Collapse it onto PollingPane during the
    cleanup pass — it works, so it isn't worth churning right now.
    """

    POLL_MS = 10_000

    def __init__(self, parent, ui: "DesktopUI"):
        super().__init__(parent, ui)
        self._poll_after_id: Optional[str] = None
        self._inflight = False

    # ── Subclass contract ────────────────────────────────────────────

    def fetch(self):
        """Runs on a worker thread. Return whatever render() expects."""
        raise NotImplementedError

    def render(self, data) -> None:
        """Runs on the Tk main thread."""
        raise NotImplementedError

    def render_error(self, exc: Exception) -> None:
        """Runs on the Tk main thread. Override for custom error UI."""
        logger.debug(f"{type(self).__name__} refresh failed: {exc}")

    # ── Lifecycle ────────────────────────────────────────────────────

    def on_show(self) -> None:
        super().on_show()
        self.refresh_now()
        self._schedule_poll()

    def on_hide(self) -> None:
        super().on_hide()
        if self._poll_after_id:
            try:
                self.after_cancel(self._poll_after_id)
            except Exception:
                pass
            self._poll_after_id = None

    def _schedule_poll(self) -> None:
        if not self._is_active:
            return
        try:
            self._poll_after_id = self.after(self.POLL_MS, self._poll_tick)
        except Exception:
            # Tk root destroyed mid-flight
            self._poll_after_id = None

    def _poll_tick(self) -> None:
        self._poll_after_id = None
        if self._is_active:
            self.refresh_now()
            self._schedule_poll()

    def refresh_now(self) -> None:
        # Never stack requests — a slow master would otherwise queue up
        # one worker per tick until the pool starves.
        if self._inflight:
            return
        self._inflight = True

        def _done(result):
            self._inflight = False
            if self._is_active:
                self.render(result)

        def _failed(exc):
            self._inflight = False
            if self._is_active:
                self.render_error(exc)

        self.run_async(self.fetch, on_result=_done, on_error=_failed)


# ═════════════════════════════════════════════════════════════════════
# Shared row widgets
# ═════════════════════════════════════════════════════════════════════

# sync_status → (background, foreground). Mirrors the pill palette the
# browser dashboard used, so the two never drift visually.
STATUS_COLORS = {
    "SYNCED":         ("#14532d", COLOR_SUCCESS),
    "PENDING_UPLOAD": ("#78350f", COLOR_WARNING),
    "UPLOADING":      ("#1e3a8a", COLOR_INFO),
    "ERROR":          ("#7f1d1d", COLOR_DANGER),
    "DELETED_LOCAL":  (COLOR_BORDER, COLOR_FAINT),
}


def status_pill(parent, status: str) -> ctk.CTkLabel:
    bg, fg = STATUS_COLORS.get(status.upper(), (COLOR_BORDER, COLOR_MUTED))
    return ctk.CTkLabel(
        parent, text=status.replace("_", " ").title(),
        font=ctk.CTkFont(size=10, weight="bold"),
        fg_color=bg, text_color=fg,
        corner_radius=10, padx=8, pady=2,
    )


def empty_state(parent, title: str, subtitle: str) -> ctk.CTkFrame:
    f = ctk.CTkFrame(parent, fg_color="transparent")
    ctk.CTkLabel(
        f, text=title, font=ctk.CTkFont(size=14, weight="bold"),
        text_color=COLOR_MUTED,
    ).pack(pady=(60, 4))
    ctk.CTkLabel(
        f, text=subtitle, font=ctk.CTkFont(size=12), text_color=COLOR_FAINT,
    ).pack()
    return f


# ═════════════════════════════════════════════════════════════════════
# Files pane
# ═════════════════════════════════════════════════════════════════════

class FilesPane(PollingPane):
    """
    Paginated view of local_files.

    Filtering split:
      - status  → server-side. /api/dashboard/files takes a `status` param.
      - search  → client-side, CURRENT PAGE ONLY. master_api.py has no
                  `q` param, and adding one is a server change we're not
                  making mid-sprint. The header says so explicitly rather
                  than letting the user believe they searched everything.
    """

    POLL_MS = FILES_POLL_MS
    PAGE_SIZE = 50
    SEARCH_DEBOUNCE_MS = 200

    STATUS_CHOICES = [
        "All statuses", "SYNCED", "PENDING_UPLOAD", "UPLOADING",
        "ERROR", "DELETED_LOCAL",
    ]

    def __init__(self, parent, ui: "DesktopUI"):
        super().__init__(parent, ui)
        self._offset = 0
        self._page: Optional[FilesPage] = None
        self._search = ""
        self._search_after_id: Optional[str] = None
        self._last_signature = None
        self._row_widgets: list = []
        self._build()

    # ── Layout ───────────────────────────────────────────────────────

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        # Header
        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(24, 12))
        header.grid_columnconfigure(1, weight=1)

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            title_box, text="Files", font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COLOR_TEXT, anchor="w",
        ).pack(anchor="w")
        self.subtitle = ctk.CTkLabel(
            title_box, text="Loading…", font=ctk.CTkFont(size=11),
            text_color=COLOR_MUTED, anchor="w",
        )
        self.subtitle.pack(anchor="w", pady=(2, 0))

        controls = ctk.CTkFrame(header, fg_color="transparent")
        controls.grid(row=0, column=2, sticky="e")

        self.search_entry = ctk.CTkEntry(
            controls, placeholder_text="Filter this page…", width=200, height=30,
            fg_color=COLOR_CARD, border_color=COLOR_BORDER,
        )
        self.search_entry.pack(side="left", padx=(0, 8))
        self.search_entry.bind("<KeyRelease>", self._on_search_key)

        self.status_menu = ctk.CTkOptionMenu(
            controls, values=self.STATUS_CHOICES, width=160, height=30,
            fg_color=COLOR_CARD, button_color=COLOR_BORDER,
            button_hover_color=COLOR_CARD_HOVER,
            command=self._on_status_change,
        )
        self.status_menu.set("All statuses")
        self.status_menu.pack(side="left")

        # Column headings
        heads = ctk.CTkFrame(self, fg_color="transparent", height=24)
        heads.grid(row=0, column=0, sticky="sew", padx=32, pady=(0, 0))
        for i, (text, weight, width) in enumerate([
            ("NAME", 1, None), ("SIZE", 0, 90),
            ("STATUS", 0, 130), ("LAST SYNCED", 0, 110),
        ]):
            heads.grid_columnconfigure(i, weight=weight, minsize=width or 0)
            ctk.CTkLabel(
                heads, text=text, font=ctk.CTkFont(size=10, weight="bold"),
                text_color=COLOR_FAINT, anchor="w",
            ).grid(row=0, column=i, sticky="w", padx=(0, 8))

        # Scrollable body
        self.body = ctk.CTkScrollableFrame(self, fg_color=COLOR_CARD, corner_radius=8)
        self.body.grid(row=1, column=0, sticky="nsew", padx=24, pady=(6, 8))
        self.body.grid_columnconfigure(0, weight=1)
        self._empty = empty_state(self.body, "No files", "Nothing matches this view.")

        # Footer / pagination
        footer = ctk.CTkFrame(self, fg_color="transparent")
        footer.grid(row=2, column=0, sticky="ew", padx=24, pady=(0, 20))

        self.btn_prev = ctk.CTkButton(
            footer, text="← Previous", width=100, height=30,
            fg_color=COLOR_CARD, hover_color=COLOR_CARD_HOVER,
            command=self._on_prev,
        )
        self.btn_prev.pack(side="left")

        self.btn_next = ctk.CTkButton(
            footer, text="Next →", width=100, height=30,
            fg_color=COLOR_CARD, hover_color=COLOR_CARD_HOVER,
            command=self._on_next,
        )
        self.btn_next.pack(side="left", padx=(8, 0))

        self.page_label = ctk.CTkLabel(
            footer, text="", font=ctk.CTkFont(size=11), text_color=COLOR_MUTED,
        )
        self.page_label.pack(side="left", padx=(12, 0))

    # ── Controls ─────────────────────────────────────────────────────

    def _selected_status(self) -> Optional[str]:
        v = self.status_menu.get()
        return None if v == "All statuses" else v

    def _on_status_change(self, _value: str) -> None:
        self._offset = 0
        self._last_signature = None
        self.refresh_now()

    def _on_search_key(self, _event=None) -> None:
        # Debounce so every keystroke doesn't rebuild 50 rows.
        if self._search_after_id:
            try:
                self.after_cancel(self._search_after_id)
            except Exception:
                pass
        self._search_after_id = self.after(self.SEARCH_DEBOUNCE_MS, self._apply_search)

    def _apply_search(self) -> None:
        self._search_after_id = None
        self._search = self.search_entry.get().strip().lower()
        self._last_signature = None
        if self._page:
            self.render(self._page)

    def _on_prev(self) -> None:
        if self._offset <= 0:
            return
        self._offset = max(0, self._offset - self.PAGE_SIZE)
        self._last_signature = None
        self.refresh_now()

    def _on_next(self) -> None:
        if not self._page:
            return
        if self._offset + self.PAGE_SIZE >= self._page.total:
            return
        self._offset += self.PAGE_SIZE
        self._last_signature = None
        self.refresh_now()

    # ── Data ─────────────────────────────────────────────────────────

    def fetch(self) -> FilesPage:
        return self.client.get_files(
            limit=self.PAGE_SIZE,
            offset=self._offset,
            status=self._selected_status(),
        )

    def render(self, page: FilesPage) -> None:
        self._page = page

        visible = [
            f for f in page.items
            if not self._search or self._search in f.relative_path.lower()
        ]

        # Rebuilding 50 rows × ~5 widgets on every 15s tick makes the list
        # flicker and drops the scroll position. Only redraw when something
        # actually moved.
        signature = (
            self._search, self._offset, page.total,
            tuple((f.relative_path, f.sync_status, f.size, f.last_synced_at)
                  for f in visible),
        )
        if signature == self._last_signature:
            self._update_footer(page, len(visible))
            return
        self._last_signature = signature

        for w in self._row_widgets:
            w.destroy()
        self._row_widgets = []
        self._empty.grid_forget()

        if not visible:
            self._empty.grid(row=0, column=0, sticky="nsew")
        else:
            for i, item in enumerate(visible):
                self._row_widgets.append(self._build_row(i, item))

        self._update_footer(page, len(visible))

    def _build_row(self, index: int, item: FileItem) -> ctk.CTkFrame:
        row = ctk.CTkFrame(
            self.body,
            fg_color=COLOR_BG if index % 2 else "transparent",
            corner_radius=4,
        )
        row.grid(row=index, column=0, sticky="ew", padx=6, pady=1)
        for i, (weight, width) in enumerate([(1, 0), (0, 90), (0, 130), (0, 110)]):
            row.grid_columnconfigure(i, weight=weight, minsize=width)

        name = item.relative_path or "(unnamed)"
        label = ctk.CTkLabel(
            row, text=name, font=ctk.CTkFont(size=12),
            text_color=COLOR_TEXT, anchor="w", justify="left",
        )
        label.grid(row=0, column=0, sticky="w", padx=(8, 8), pady=6)

        ctk.CTkLabel(
            row, text=fmt_bytes(item.size), font=ctk.CTkFont(size=11),
            text_color=COLOR_MUTED, anchor="w",
        ).grid(row=0, column=1, sticky="w", padx=(0, 8))

        status_pill(row, item.sync_status).grid(row=0, column=2, sticky="w", padx=(0, 8))

        ctk.CTkLabel(
            row, text=fmt_relative(item.last_synced_at or item.created_at),
            font=ctk.CTkFont(size=11), text_color=COLOR_FAINT, anchor="w",
        ).grid(row=0, column=3, sticky="w", padx=(0, 8))

        return row

    def _update_footer(self, page: FilesPage, shown: int) -> None:
        first = page.offset + 1 if page.total else 0
        last = min(page.offset + len(page.items), page.total)

        if self._search:
            self.page_label.configure(
                text=f"{shown} of {len(page.items)} on this page match “{self._search}”"
            )
            self.subtitle.configure(
                text=(
                    f"Filtering page {first}–{last} only — search does not cover "
                    f"all {page.total:,} files"
                )
            )
        else:
            self.page_label.configure(
                text=f"Showing {first}–{last} of {page.total:,}" if page.total else "No files"
            )
            status = self._selected_status()
            self.subtitle.configure(
                text=f"{page.total:,} files" + (f" · {status.replace('_', ' ').title()}"
                                                if status else "")
            )

        at_start = page.offset <= 0
        at_end = page.offset + self.PAGE_SIZE >= page.total
        self.btn_prev.configure(state="disabled" if at_start else "normal")
        self.btn_next.configure(state="disabled" if at_end else "normal")

    def render_error(self, exc: Exception) -> None:
        self.subtitle.configure(text=f"Could not load files: {str(exc)[:100]}")


# ═════════════════════════════════════════════════════════════════════
# Activity pane
# ═════════════════════════════════════════════════════════════════════

class ActivityPane(PollingPane):
    """Last 50 successfully synced files, newest first."""

    POLL_MS = 5_000

    def __init__(self, parent, ui: "DesktopUI"):
        super().__init__(parent, ui)
        self._last_signature = None
        self._row_widgets: list = []
        self._build()

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(24, 12))
        ctk.CTkLabel(
            header, text="Recent activity",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COLOR_TEXT, anchor="w",
        ).pack(anchor="w")
        self.subtitle = ctk.CTkLabel(
            header, text="Loading…", font=ctk.CTkFont(size=11),
            text_color=COLOR_MUTED, anchor="w",
        )
        self.subtitle.pack(anchor="w", pady=(2, 0))

        self.body = ctk.CTkScrollableFrame(self, fg_color=COLOR_CARD, corner_radius=8)
        self.body.grid(row=1, column=0, sticky="nsew", padx=24, pady=(0, 20))
        self.body.grid_columnconfigure(0, weight=1)
        self._empty = empty_state(
            self.body, "Nothing synced yet",
            "Files appear here once they finish syncing.",
        )

    def fetch(self) -> "list[RecentItem]":
        return self.client.get_recent()

    def render(self, items: "list[RecentItem]") -> None:
        signature = tuple((i.relative_path, i.last_synced_at, i.size) for i in items)
        if signature == self._last_signature:
            return
        self._last_signature = signature

        for w in self._row_widgets:
            w.destroy()
        self._row_widgets = []
        self._empty.grid_forget()

        self.subtitle.configure(
            text=f"Last {len(items)} synced file{'s' if len(items) != 1 else ''}"
            if items else "No sync activity yet"
        )

        if not items:
            self._empty.grid(row=0, column=0, sticky="nsew")
            return

        for i, item in enumerate(items):
            row = ctk.CTkFrame(
                self.body,
                fg_color=COLOR_BG if i % 2 else "transparent",
                corner_radius=4,
            )
            row.grid(row=i, column=0, sticky="ew", padx=6, pady=1)
            row.grid_columnconfigure(1, weight=1)

            ctk.CTkLabel(
                row, text="✓", font=ctk.CTkFont(size=13, weight="bold"),
                text_color=COLOR_SUCCESS, width=20,
            ).grid(row=0, column=0, padx=(10, 6), pady=7)

            ctk.CTkLabel(
                row, text=item.relative_path, font=ctk.CTkFont(size=12),
                text_color=COLOR_TEXT, anchor="w", justify="left",
            ).grid(row=0, column=1, sticky="w")

            ctk.CTkLabel(
                row, text=fmt_bytes(item.size), font=ctk.CTkFont(size=11),
                text_color=COLOR_MUTED, anchor="e", width=80,
            ).grid(row=0, column=2, sticky="e", padx=(8, 8))

            ctk.CTkLabel(
                row, text=fmt_relative(item.last_synced_at),
                font=ctk.CTkFont(size=11), text_color=COLOR_FAINT,
                anchor="e", width=90,
            ).grid(row=0, column=3, sticky="e", padx=(0, 10))

            self._row_widgets.append(row)

    def render_error(self, exc: Exception) -> None:
        self.subtitle.configure(text=f"Could not load activity: {str(exc)[:100]}")


# ═════════════════════════════════════════════════════════════════════
# Uploads pane
# ═════════════════════════════════════════════════════════════════════

class UploadsPane(PollingPane):
    """
    Live view of uploads_in_progress. One card per session; click a card
    to expand its chunk grid.

    The grid is drawn on a tk.Canvas rather than as one widget per chunk.
    master_api.py allows up to 200,000 chunks per session — a 4,000-chunk
    upload (100 GB at 25 MiB) would mean 4,000 CTkFrames, which locks Tk
    for seconds on every 2s poll. Canvas rectangles redraw instantly, and
    MAX_GRID_CELLS caps the drawing anyway.
    """

    POLL_MS = ACTIVE_POLL_MS
    CELL = 12          # px per chunk square
    GAP = 2
    MAX_GRID_CELLS = 400

    def __init__(self, parent, ui: "DesktopUI"):
        super().__init__(parent, ui)
        self._expanded: set = set()   # upload_ids the user has opened
        self._cards: dict = {}        # upload_id -> widget refs
        self._last_ids: tuple = ()
        self._build()

    def _build(self) -> None:
        self.grid_columnconfigure(0, weight=1)
        self.grid_rowconfigure(1, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(24, 12))
        header.grid_columnconfigure(0, weight=1)

        title_box = ctk.CTkFrame(header, fg_color="transparent")
        title_box.grid(row=0, column=0, sticky="w")
        ctk.CTkLabel(
            title_box, text="Uploads in progress",
            font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COLOR_TEXT, anchor="w",
        ).pack(anchor="w")
        self.subtitle = ctk.CTkLabel(
            title_box, text="Loading…", font=ctk.CTkFont(size=11),
            text_color=COLOR_MUTED, anchor="w",
        )
        self.subtitle.pack(anchor="w", pady=(2, 0))

        self.count_pill = ctk.CTkLabel(
            header, text="0", font=ctk.CTkFont(size=11, weight="bold"),
            fg_color=COLOR_BORDER, text_color=COLOR_MUTED,
            corner_radius=12, padx=12, pady=4,
        )
        self.count_pill.grid(row=0, column=1, sticky="e")

        self.body = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self.body.grid(row=1, column=0, sticky="nsew", padx=24, pady=(0, 20))
        self.body.grid_columnconfigure(0, weight=1)
        self._empty = empty_state(
            self.body, "No uploads in progress",
            "Chunked uploads from your phone show up here in real time.",
        )
        self._empty.grid(row=0, column=0, sticky="nsew")

    def fetch(self) -> "list[UploadItem]":
        return self.client.get_uploads()

    def render(self, uploads: "list[UploadItem]") -> None:
        self.count_pill.configure(
            text=str(len(uploads)),
            fg_color=COLOR_ACCENT if uploads else COLOR_BORDER,
            text_color="#ffffff" if uploads else COLOR_MUTED,
        )
        self.subtitle.configure(
            text=f"{len(uploads)} active session{'s' if len(uploads) != 1 else ''}"
            if uploads else "Nothing uploading right now"
        )

        ids = tuple(u.upload_id for u in uploads)

        # Cards are only torn down when the *set* of sessions changes.
        # In steady state we just update progress in place, so an expanded
        # chunk grid doesn't collapse under the user every 2 seconds.
        if ids != self._last_ids:
            for refs in self._cards.values():
                refs["frame"].destroy()
            self._cards = {}
            self._expanded &= set(ids)
            self._last_ids = ids

            self._empty.grid_forget()
            if not uploads:
                self._empty.grid(row=0, column=0, sticky="nsew")
            else:
                for i, u in enumerate(uploads):
                    self._cards[u.upload_id] = self._build_card(i, u)

        for u in uploads:
            refs = self._cards.get(u.upload_id)
            if refs:
                self._update_card(refs, u)

    def _build_card(self, index: int, u: UploadItem) -> dict:
        card = ctk.CTkFrame(self.body, fg_color=COLOR_CARD, corner_radius=8)
        card.grid(row=index, column=0, sticky="ew", pady=(0, 10))
        card.grid_columnconfigure(0, weight=1)

        head = ctk.CTkFrame(card, fg_color="transparent")
        head.grid(row=0, column=0, sticky="ew", padx=16, pady=(14, 6))
        head.grid_columnconfigure(0, weight=1)

        name = ctk.CTkLabel(
            head, text=u.original_name or "(unnamed)",
            font=ctk.CTkFont(size=13, weight="bold"),
            text_color=COLOR_TEXT, anchor="w",
        )
        name.grid(row=0, column=0, sticky="w")

        toggle = ctk.CTkButton(
            head, text="▸ chunks", width=90, height=24,
            font=ctk.CTkFont(size=11),
            fg_color="transparent", hover_color=COLOR_CARD_HOVER,
            text_color=COLOR_MUTED,
            command=lambda uid=u.upload_id: self._toggle(uid),
        )
        toggle.grid(row=0, column=1, sticky="e")

        meta = ctk.CTkLabel(
            card, text="", font=ctk.CTkFont(size=11),
            text_color=COLOR_MUTED, anchor="w",
        )
        meta.grid(row=1, column=0, sticky="w", padx=16)

        bar = ctk.CTkProgressBar(
            card, height=6, corner_radius=3,
            fg_color=COLOR_BORDER, progress_color=COLOR_ACCENT,
        )
        bar.grid(row=2, column=0, sticky="ew", padx=16, pady=(8, 0))
        bar.set(0)

        grid_wrap = ctk.CTkFrame(card, fg_color="transparent")
        grid_wrap.grid(row=3, column=0, sticky="ew", padx=16, pady=(10, 0))
        grid_wrap.grid_columnconfigure(0, weight=1)
        grid_wrap.grid_remove()

        canvas = tk.Canvas(
            grid_wrap, bg=COLOR_CARD, highlightthickness=0, height=1,
        )
        canvas.grid(row=0, column=0, sticky="ew")

        note = ctk.CTkLabel(
            grid_wrap, text="", font=ctk.CTkFont(size=10),
            text_color=COLOR_FAINT, anchor="w",
        )
        note.grid(row=1, column=0, sticky="w", pady=(4, 0))

        spacer = ctk.CTkFrame(card, fg_color="transparent", height=14)
        spacer.grid(row=4, column=0)

        refs = {
            "frame": card, "name": name, "toggle": toggle, "meta": meta,
            "bar": bar, "grid_wrap": grid_wrap, "canvas": canvas, "note": note,
            "drawn": None, "upload": None,
        }

        # Tk only gives the canvas a real width once it has been mapped, so
        # the first <Configure> is what triggers the initial paint. Polling
        # winfo_width() on a timer instead would race card teardown (the
        # canvas can be destroyed before the retry fires) and would never
        # reflow the grid when the window is resized. <Configure> handles
        # both; _paint_grid's signature check absorbs the resize-drag storm.
        canvas.bind("<Configure>", lambda _e, r=refs: self._paint_grid(r))

        return refs

    def _update_card(self, refs: dict, u: UploadItem) -> None:
        refs["upload"] = u
        refs["bar"].set(u.progress)
        refs["meta"].configure(
            text=(
                f"{fmt_bytes(u.total_size)} · "
                f"{u.chunks_received}/{u.total_chunks} chunks of {fmt_bytes(u.chunk_size)} · "
                f"{int(u.progress * 100)}% · last chunk {fmt_relative(u.last_chunk_at)}"
            )
        )

        expanded = u.upload_id in self._expanded
        refs["toggle"].configure(text="▾ chunks" if expanded else "▸ chunks")
        if expanded:
            refs["grid_wrap"].grid()
            self._paint_grid(refs)
        else:
            refs["grid_wrap"].grid_remove()

    def _toggle(self, upload_id: str) -> None:
        if upload_id in self._expanded:
            self._expanded.discard(upload_id)
        else:
            self._expanded.add(upload_id)

        refs = self._cards.get(upload_id)
        if refs is not None and refs.get("upload") is not None:
            # Expanding is a pure UI state change — repaint from the state
            # we already hold. Routing it through refresh_now() would make
            # the click a no-op for up to POLL_MS whenever a poll happened
            # to be in flight (refresh_now() drops re-entrant calls).
            refs["drawn"] = None
            self._update_card(refs, refs["upload"])
        else:
            self.refresh_now()

    def _paint_grid(self, refs: dict) -> None:
        u = refs.get("upload")
        if u is None:
            return

        canvas = refs["canvas"]
        try:
            width = canvas.winfo_width()
        except tk.TclError:
            return  # card was destroyed mid-flight
        if width <= 1:
            return  # not laid out yet — <Configure> will call us back

        state = u.chunks_state()
        capped = state[: self.MAX_GRID_CELLS]

        step = self.CELL + self.GAP
        cols = max(1, width // step)
        rows = max(1, -(-len(capped) // cols))

        # Guards both the 2s poll and the per-pixel <Configure> storm during
        # a window drag: cols is in the signature, so we only repaint when
        # the layout or the chunk state actually changed.
        signature = (tuple(capped), cols)
        if refs["drawn"] == signature:
            return
        refs["drawn"] = signature

        try:
            canvas.delete("all")
            canvas.configure(height=rows * step)
            for i, received in enumerate(capped):
                r, c = divmod(i, cols)
                x0 = c * step
                y0 = r * step
                canvas.create_rectangle(
                    x0, y0, x0 + self.CELL, y0 + self.CELL,
                    fill=COLOR_ACCENT if received else COLOR_BORDER,
                    outline="",
                )
        except tk.TclError:
            return

        if len(state) > self.MAX_GRID_CELLS:
            refs["note"].configure(
                text=f"Showing first {self.MAX_GRID_CELLS:,} of {len(state):,} chunks"
            )
        else:
            refs["note"].configure(text="")

    def render_error(self, exc: Exception) -> None:
        self.subtitle.configure(text=f"Could not load uploads: {str(exc)[:100]}")


# ═════════════════════════════════════════════════════════════════════
# Settings pane — still a stub
# ═════════════════════════════════════════════════════════════════════

class SettingsPane(Pane):
    """
    Deferred to Sprint 3.7. The toggles need somewhere to persist, which
    means GET/POST /api/dashboard/preferences on master_api.py plus
    get/set helpers on sync_db's sync_meta table — a server change, not
    a UI one, so it isn't landing in the middle of a UI sprint.
    """

    def __init__(self, parent, ui: "DesktopUI"):
        super().__init__(parent, ui)
        self.grid_columnconfigure(0, weight=1)

        header = ctk.CTkFrame(self, fg_color="transparent")
        header.grid(row=0, column=0, sticky="ew", padx=24, pady=(24, 12))
        ctk.CTkLabel(
            header, text="Settings", font=ctk.CTkFont(size=20, weight="bold"),
            text_color=COLOR_TEXT, anchor="w",
        ).pack(anchor="w")

        card = ctk.CTkFrame(self, fg_color=COLOR_CARD, corner_radius=8)
        card.grid(row=1, column=0, sticky="ew", padx=24)
        ctk.CTkLabel(
            card, text="Coming in Sprint 3.7",
            font=ctk.CTkFont(size=14, weight="bold"),
            text_color=COLOR_TEXT, anchor="w",
        ).pack(anchor="w", padx=20, pady=(18, 6))
        ctk.CTkLabel(
            card,
            text=(
                "Open at startup  ·  Minimize to tray  ·  Log level  ·  Sync folder\n\n"
                "These need a preferences endpoint on the master before the "
                "toggles have anywhere to save to."
            ),
            font=ctk.CTkFont(size=12), text_color=COLOR_MUTED,
            anchor="w", justify="left",
        ).pack(anchor="w", padx=20, pady=(0, 18))


# ═════════════════════════════════════════════════════════════════════
# Switch-account dialog
# ═════════════════════════════════════════════════════════════════════

class SwitchAccountDialog(ctk.CTkToplevel):
    """Modal dialog asking the user to confirm switching Google account."""

    def __init__(self, parent, on_confirm: Callable[[], None]):
        super().__init__(parent)
        self.title("Switch Google account")
        self.geometry("460x240")
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()
        self._on_confirm = on_confirm

        body = ctk.CTkFrame(self, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=24, pady=20)

        ctk.CTkLabel(
            body, text="Switch Google account",
            font=ctk.CTkFont(size=16, weight="bold"),
            text_color=COLOR_TEXT, anchor="w",
        ).pack(anchor="w")

        ctk.CTkLabel(
            body,
            text=(
                "Sign out the current account so you can sign in with a different "
                "Google account when you next launch RGBC Drive. Files in your sync "
                "folder are never deleted."
            ),
            font=ctk.CTkFont(size=12), text_color=COLOR_MUTED,
            wraplength=400, justify="left", anchor="w",
        ).pack(anchor="w", pady=(8, 16))

        btn_row = ctk.CTkFrame(body, fg_color="transparent")
        btn_row.pack(fill="x", side="bottom")

        ctk.CTkButton(
            btn_row, text="Cancel", width=100,
            fg_color=COLOR_CARD, hover_color=COLOR_CARD_HOVER,
            command=self.destroy,
        ).pack(side="right", padx=(8, 0))

        ctk.CTkButton(
            btn_row, text="Switch account", width=140,
            fg_color=COLOR_DANGER, hover_color="#dc2626",
            command=self._confirm,
        ).pack(side="right")

    def _confirm(self) -> None:
        self.destroy()
        self._on_confirm()


# ═════════════════════════════════════════════════════════════════════
# Main window
# ═════════════════════════════════════════════════════════════════════

class DesktopUI:
    """
    Owns the Tk root, the sidebar, the pane container, and the
    background executor used for HTTP calls.

    Lifecycle:
        ui = DesktopUI(port=MASTER_PORT)
        ui.start()      # blocks the calling thread until window closes

    Hide-to-tray (when the user clicks X with minimize_to_tray=True):
        Does NOT destroy the root; just withdraws it. Tray's
        "Show RGBC Drive" calls ui.show() to restore.

    Real shutdown:
        ui.shutdown() — destroys the window. Called from the tray's
        Quit menu or from the on_quit callback in rgbc_drive.py.
    """

    PANES = [
        ("status", "Status", StatusPane),
        ("files", "Files", FilesPane),
        ("activity", "Activity", ActivityPane),
        ("uploads", "Uploads", UploadsPane),
        ("settings", "Settings", SettingsPane),
    ]

    def __init__(
        self,
        port: int,
        on_close_to_tray: Optional[Callable[[], None]] = None,
        on_quit: Optional[Callable[[], None]] = None,
    ):
        self.port = port
        self._on_close_to_tray = on_close_to_tray
        self._on_quit = on_quit

        self.client = UIClient(port=port)
        self.executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="UI")
        self._shutdown = False

        ctk.set_appearance_mode("dark")
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk()
        self.root.title("RGBC Drive")
        self.root.geometry("1280x800")
        self.root.minsize(1024, 640)
        self.root.configure(fg_color=COLOR_BG)

        # Window-close behavior depends on user preference, fetched at
        # close-time. Default is "minimize to tray" (preserves background sync).
        self.root.protocol("WM_DELETE_WINDOW", self._on_window_close)

        self._panes: dict[str, Pane] = {}
        self._sidebar_buttons: dict[str, ctk.CTkButton] = {}
        self._active_key: Optional[str] = None
        self._build()

    # ── Layout ──────────────────────────────────────────────────────

    def _build(self) -> None:
        self.root.grid_columnconfigure(1, weight=1)
        self.root.grid_rowconfigure(0, weight=1)

        # Sidebar
        sidebar = ctk.CTkFrame(self.root, width=200, corner_radius=0, fg_color=COLOR_CARD)
        sidebar.grid(row=0, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        sidebar.grid_rowconfigure(99, weight=1)

        # Sidebar header
        ctk.CTkLabel(
            sidebar, text="RGBC Drive",
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color=COLOR_TEXT, anchor="w",
        ).grid(row=0, column=0, sticky="ew", padx=20, pady=(20, 4))

        ctk.CTkLabel(
            sidebar, text="P2P Master",
            font=ctk.CTkFont(size=10),
            text_color=COLOR_FAINT, anchor="w",
        ).grid(row=1, column=0, sticky="ew", padx=20, pady=(0, 16))

        # Sidebar nav buttons
        for i, (key, label, cls) in enumerate(self.PANES):
            btn = ctk.CTkButton(
                sidebar, text=f"  {label}",
                anchor="w", height=36, corner_radius=4,
                fg_color="transparent", text_color=COLOR_MUTED,
                hover_color=COLOR_CARD_HOVER,
                font=ctk.CTkFont(size=13),
                command=lambda k=key: self.show_pane(k),
            )
            btn.grid(row=2 + i, column=0, sticky="ew", padx=10, pady=2)
            self._sidebar_buttons[key] = btn

        # Pane container
        container = ctk.CTkFrame(self.root, fg_color=COLOR_BG, corner_radius=0)
        container.grid(row=0, column=1, sticky="nsew")
        container.grid_columnconfigure(0, weight=1)
        container.grid_rowconfigure(0, weight=1)
        self._container = container

        # Instantiate every pane up front (cheap, lets us swap quickly)
        for key, _, cls in self.PANES:
            pane = cls(container, self)
            pane.grid(row=0, column=0, sticky="nsew")
            pane.grid_remove()
            self._panes[key] = pane

        # Show the default pane
        self.show_pane("status")

    # ── Pane navigation ─────────────────────────────────────────────

    def show_pane(self, key: str) -> None:
        if key == self._active_key:
            return

        # Hide previous
        if self._active_key:
            prev = self._panes[self._active_key]
            prev.on_hide()
            prev.grid_remove()
            self._sidebar_buttons[self._active_key].configure(
                fg_color="transparent", text_color=COLOR_MUTED,
            )

        # Show new
        pane = self._panes[key]
        pane.grid()
        pane.on_show()
        self._sidebar_buttons[key].configure(
            fg_color=COLOR_ACCENT, text_color="#ffffff",
        )
        self._active_key = key

    # ── Window lifecycle ────────────────────────────────────────────

    def _on_window_close(self) -> None:
        """User clicked the X button. Hide to tray (default) or quit."""
        # Default: minimize to tray. The Settings pane (turn 2) will let
        # the user opt out of this and exit on close instead.
        if self._on_close_to_tray is not None:
            self.hide()
            self._on_close_to_tray()
        else:
            # No tray callback registered — really exit
            self.shutdown()

    def show(self) -> None:
        """Restore from tray. Safe to call from any thread."""
        try:
            self.root.after(0, self._do_show)
        except Exception:
            pass

    def _do_show(self) -> None:
        self.root.deiconify()
        self.root.lift()
        self.root.focus_force()

    def hide(self) -> None:
        try:
            self.root.withdraw()
        except Exception:
            pass

    def start(self) -> None:
        """Run the Tk mainloop. Blocks the calling thread."""
        try:
            self.root.mainloop()
        finally:
            self._teardown()

    def shutdown(self) -> None:
        """Hard exit. Safe to call from any thread."""
        if self._shutdown:
            return
        self._shutdown = True
        try:
            self.root.after(0, self._do_shutdown)
        except Exception:
            self._teardown()

    def _do_shutdown(self) -> None:
        try:
            if self._on_quit:
                self._on_quit()
        except Exception:
            logger.exception("on_quit callback raised")
        try:
            self.root.quit()
            self.root.destroy()
        except Exception:
            pass

    def _teardown(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
        try:
            self.executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass