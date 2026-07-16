"""
RGBC Drive — UI HTTP Client (Sprint 3.5b)

Thin wrapper around `requests` for the customtkinter UI to talk to its
own master_api.py. The UI calls the master over HTTP even though both
run in the same process — this preserves the API contract that future
iOS/macOS clients will reuse, and keeps the UI decoupled from internal
data structures.

All endpoints used here are localhost-only (verify_localhost dependency
on master_api.py). No JWT needed.

Threading:
  - These methods are blocking and intended to be called from background
    worker threads dispatched by DesktopUI.
  - NEVER call them from the Tk main thread — they would freeze the UI.

Field shapes are taken directly from master_api.py's dashboard endpoints
(camelCase JSON). If you add fields to those endpoints, update the
corresponding dataclass here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Optional

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

logger = logging.getLogger("RGBCDrive.UIClient")


class UIClientError(Exception):
    """Raised when an API call fails. Carries HTTP status if available."""

    def __init__(self, message: str, status: Optional[int] = None):
        super().__init__(message)
        self.status = status


@dataclass
class StatusResponse:
    """Parsed from /api/dashboard/status. All fields have safe defaults."""

    app_version: str
    owner_user_id: Optional[str]
    device_name: str
    os_platform: str
    sync_root: str
    tunnel_url: Optional[str]
    tunnel_online: bool
    last_heartbeat_seconds_ago: Optional[int]
    disk_free_bytes: int
    disk_total_bytes: int
    disk_used_bytes: int
    total_files: int
    synced_files: int
    pending_upload: int
    total_size_bytes: int
    pause_available: bool
    resume_available: bool
    scan_available: bool
    switch_account_available: bool

    @classmethod
    def from_json(cls, data: dict) -> "StatusResponse":
        stats = data.get("stats") or {}
        controls = data.get("controls") or {}
        last_hb = data.get("lastHeartbeatSecondsAgo")
        return cls(
            app_version=str(data.get("appVersion") or ""),
            owner_user_id=data.get("ownerUserId"),
            device_name=str(data.get("deviceName") or ""),
            os_platform=str(data.get("osPlatform") or ""),
            sync_root=str(data.get("syncRoot") or ""),
            tunnel_url=data.get("tunnelUrl"),
            tunnel_online=bool(data.get("tunnelOnline")),
            last_heartbeat_seconds_ago=int(last_hb) if last_hb is not None else None,
            disk_free_bytes=int(data.get("diskFreeBytes") or 0),
            disk_total_bytes=int(data.get("diskTotalBytes") or 0),
            disk_used_bytes=int(data.get("diskUsedBytes") or 0),
            total_files=int(stats.get("totalFiles") or 0),
            synced_files=int(stats.get("syncedFiles") or 0),
            pending_upload=int(stats.get("pendingUpload") or 0),
            total_size_bytes=int(stats.get("totalSizeBytes") or 0),
            pause_available=bool(controls.get("pauseAvailable", True)),
            resume_available=bool(controls.get("resumeAvailable", True)),
            scan_available=bool(controls.get("scanAvailable", True)),
            switch_account_available=bool(controls.get("switchAccountAvailable", True)),
        )


@dataclass
class FileItem:
    """One row from /api/dashboard/files."""
    relative_path: str
    sha256: Optional[str]   # may be truncated by server
    size: int
    server_file_id: Optional[str]
    sync_status: str
    last_synced_at: Optional[str]
    created_at: Optional[str]

    @classmethod
    def from_json(cls, d: dict) -> "FileItem":
        return cls(
            relative_path=str(d.get("relativePath") or ""),
            sha256=d.get("sha256"),
            size=int(d.get("size") or 0),
            server_file_id=d.get("serverFileId"),
            sync_status=str(d.get("syncStatus") or "UNKNOWN"),
            last_synced_at=d.get("lastSyncedAt"),
            created_at=d.get("createdAt"),
        )


@dataclass
class FilesPage:
    items: list[FileItem]
    total: int
    limit: int
    offset: int

    @classmethod
    def from_json(cls, d: dict) -> "FilesPage":
        return cls(
            items=[FileItem.from_json(f) for f in (d.get("files") or [])],
            total=int(d.get("total") or 0),
            limit=int(d.get("limit") or 50),
            offset=int(d.get("offset") or 0),
        )


@dataclass
class RecentItem:
    """One row from /api/dashboard/recent."""
    relative_path: str
    size: int
    last_synced_at: Optional[str]

    @classmethod
    def from_json(cls, d: dict) -> "RecentItem":
        return cls(
            relative_path=str(d.get("relativePath") or ""),
            size=int(d.get("size") or 0),
            last_synced_at=d.get("lastSyncedAt"),
        )


@dataclass
class UploadItem:
    """One in-progress chunked upload from /api/dashboard/uploads."""
    upload_id: str
    original_name: str
    total_size: int
    total_chunks: int
    chunk_size: int
    chunks_received: int
    received_indices: list[int]
    created_at: Optional[str]
    expires_at: Optional[str]
    last_chunk_at: Optional[str]

    @property
    def progress(self) -> float:
        """Fraction received in [0.0, 1.0]."""
        if self.total_chunks <= 0:
            return 0.0
        return self.chunks_received / self.total_chunks

    def chunks_state(self) -> list[bool]:
        """
        Convert received_indices into a fixed-length boolean array,
        indexed by chunk number. Used by the chunk grid in UploadsPane.
        """
        state = [False] * self.total_chunks
        for i in self.received_indices:
            if 0 <= i < self.total_chunks:
                state[i] = True
        return state

    @classmethod
    def from_json(cls, d: dict) -> "UploadItem":
        return cls(
            upload_id=str(d.get("uploadId") or ""),
            original_name=str(d.get("originalName") or ""),
            total_size=int(d.get("totalSize") or 0),
            total_chunks=int(d.get("totalChunks") or 0),
            chunk_size=int(d.get("chunkSize") or 0),
            chunks_received=int(d.get("chunksReceived") or 0),
            received_indices=list(d.get("receivedIndices") or []),
            created_at=d.get("createdAt"),
            expires_at=d.get("expiresAt"),
            last_chunk_at=d.get("lastChunkAt"),
        )


# ═══════════════════════════════════════════════════════════════════════
# Client
# ═══════════════════════════════════════════════════════════════════════

class UIClient:
    """
    HTTP client for the customtkinter UI. Holds one keep-alive session
    so polling every few seconds doesn't churn TCP connections.
    """

    def __init__(self, port: int = 8741, host: str = "127.0.0.1"):
        self.base_url = f"http://{host}:{port}"
        self._session = requests.Session()

        # Retry only on connection-level failures (not on 4xx/5xx — those
        # are application errors we want to surface, not silently retry).
        retry = Retry(
            total=2,
            backoff_factor=0.2,
            status_forcelist=(),
            allowed_methods=frozenset(["GET", "POST"]),
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=2, pool_maxsize=4)
        self._session.mount("http://", adapter)

    def close(self) -> None:
        try:
            self._session.close()
        except Exception:
            pass

    # ── Generic request helpers ──────────────────────────────────────

    def _get(self, path: str, params: Optional[dict] = None, timeout: float = 5.0) -> Any:
        url = self.base_url + path
        try:
            resp = self._session.get(url, params=params, timeout=timeout)
        except requests.RequestException as e:
            raise UIClientError(f"Network error: {e}") from e
        return self._handle(resp)

    def _post(self, path: str, body: Optional[dict] = None, timeout: float = 10.0) -> Any:
        url = self.base_url + path
        try:
            resp = self._session.post(url, json=body, timeout=timeout)
        except requests.RequestException as e:
            raise UIClientError(f"Network error: {e}") from e
        return self._handle(resp)

    @staticmethod
    def _handle(resp: requests.Response) -> Any:
        if resp.status_code >= 400:
            try:
                detail = resp.json().get("detail") or resp.text[:200]
            except ValueError:
                detail = resp.text[:200] or "(no body)"
            raise UIClientError(f"HTTP {resp.status_code}: {detail}", status=resp.status_code)
        try:
            return resp.json()
        except ValueError as e:
            raise UIClientError(f"Invalid JSON response: {e}") from e

    # ── Status ───────────────────────────────────────────────────────

    def get_status(self) -> StatusResponse:
        return StatusResponse.from_json(self._get("/api/dashboard/status"))

    # ── Files ────────────────────────────────────────────────────────

    def get_files(
        self,
        limit: int = 50,
        offset: int = 0,
        status: Optional[str] = None,
    ) -> FilesPage:
        """
        master_api.py's /api/dashboard/files does NOT support a server-side
        search param. The Files pane filters its current page client-side.
        """
        params: dict = {"limit": limit, "offset": offset}
        if status:
            params["status"] = status
        return FilesPage.from_json(self._get("/api/dashboard/files", params=params))

    # ── Recent activity ──────────────────────────────────────────────

    def get_recent(self) -> list[RecentItem]:
        data = self._get("/api/dashboard/recent")
        return [RecentItem.from_json(r) for r in (data.get("items") or [])]

    # ── In-progress uploads ──────────────────────────────────────────

    def get_uploads(self) -> list[UploadItem]:
        data = self._get("/api/dashboard/uploads")
        return [UploadItem.from_json(u) for u in (data.get("uploads") or [])]

    # ── Actions ──────────────────────────────────────────────────────

    def trigger_scan(self) -> dict:
        return self._post("/api/dashboard/scan")

    def pause_sync(self) -> dict:
        return self._post("/api/dashboard/pause")

    def resume_sync(self) -> dict:
        return self._post("/api/dashboard/resume")

    def switch_account(self) -> dict:
        """
        Triggers the master's switch-account flow. master_api.py requires
        an explicit {confirmed: true} body; the rgbc_drive.py callback
        decides what to do (clear owner binding + OAuth cache, exit, etc.).

        Note: the existing master_api.py `switch_account` callback takes
        NO arguments, so the wipe-DB option from our original design is
        not supported in this build. Adding it would require:
          - master_api.py: pass `wipe_db` to the callback
          - rgbc_drive.py: handle the new arg in on_dashboard_switch_account
        """
        return self._post("/api/dashboard/switch-account", body={"confirmed": True})