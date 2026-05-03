"""
RGBC Drive — Server API Client (Sprint 3)

Handles all HTTP communication with the RGBC backend.

Sprint 3 BREAKING CHANGES:
  1. M2M API Key (X-API-Key) REMOVED
  2. Authentication via MasterOAuth: a requests.auth.AuthBase adapter
     calls oauth_manager.get_auth_headers() on EVERY request, so the
     Bearer JWT is always fresh — even if the token was refreshed
     mid-session by the OAuth cache expiry logic.
  3. Backward-compatible: if no oauth_manager is provided, falls back
     to a static api_key (for testing/migration only).

The AuthBase adapter means ALL calls through self.session — including
direct api.session.post() in rgbc_drive.py — automatically get the
correct Authorization header. No call site changes needed.

Endpoints used:
  GET  /health                        — connectivity test
  GET  /api/files/check-hash/:sha256  — deduplicate before upload
  POST /api/files/upload              — multipart file upload
  GET  /api/files/list                — server manifest for pull sync
  GET  /api/files/download/:fileId    — download a remote file
  GET  /api/server-info               — server diagnostics
"""

import os
import logging
from typing import Optional
from dataclasses import dataclass

import requests
from requests.auth import AuthBase

logger = logging.getLogger("RGBCDrive.API")


# ═══════════════════════════════════════════════════════════════════════
# DYNAMIC BEARER TOKEN AUTH ADAPTER
#
# Injected into requests.Session.auth so EVERY request — whether via
# self.session.get(), self.session.post(), or the high-level methods —
# gets the current Bearer JWT from the MasterOAuth manager.
#
# If the cached JWT expired and MasterOAuth refreshed it between calls,
# the next request automatically picks up the new token.
# ═══════════════════════════════════════════════════════════════════════

class _OAuthBearerAuth(AuthBase):
    """requests AuthBase adapter that reads the JWT dynamically."""

    def __init__(self, oauth_manager):
        self._oauth = oauth_manager

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        headers = self._oauth.get_auth_headers()
        for key, value in headers.items():
            r.headers[key] = value
        return r


class _StaticKeyAuth(AuthBase):
    """Legacy fallback: static X-API-Key header (Sprint 2 compat)."""

    def __init__(self, api_key: str):
        self._key = api_key

    def __call__(self, r: requests.PreparedRequest) -> requests.PreparedRequest:
        if self._key:
            r.headers["X-API-Key"] = self._key
        return r


# ═══════════════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class UploadResult:
    success: bool
    server_file_id: Optional[str] = None
    checksum: Optional[str] = None
    error: Optional[str] = None


@dataclass
class RemoteFile:
    """A file record as returned by GET /api/files/list."""
    server_id: str
    original_name: str
    file_size: int
    checksum: Optional[str]
    uploaded_at: Optional[str]


# ═══════════════════════════════════════════════════════════════════════
# API CLIENT
# ═══════════════════════════════════════════════════════════════════════

class APIClient:
    """
    Thread-safe HTTP client for the RGBC backend.

    Sprint 3 usage (recommended):
        from master_oauth import MasterOAuth
        oauth = MasterOAuth(gateway_url=SERVER_URL, ...)
        api = APIClient(server_url=SERVER_URL, oauth_manager=oauth)

    Legacy usage (Sprint 2 backward-compat, NOT recommended):
        api = APIClient(server_url=SERVER_URL, api_key="...")
    """

    def __init__(
        self,
        server_url: str,
        api_key: str = "",
        oauth_manager=None,
    ):
        self.server_url = server_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "Accept": "application/json",
        })

        # ── Attach auth adapter ──────────────────────────────────
        if oauth_manager is not None:
            self.session.auth = _OAuthBearerAuth(oauth_manager)
            logger.info("🔐 API client using OAuth Bearer JWT (RS256)")
        elif api_key:
            self.session.auth = _StaticKeyAuth(api_key)
            logger.warning("🔐 API client using static API key (DEPRECATED)")
        else:
            logger.warning("🔐 API client has NO authentication configured")

        # Timeouts: (connect, read)
        self._fast_timeout = (10, 30)
        self._transfer_timeout = (30, 600)

    # ═══════════════════════════════════════════════════════════════════
    # HEALTH CHECK
    # ═══════════════════════════════════════════════════════════════════

    def health_check(self) -> bool:
        """Test connectivity. Returns True if server is reachable."""
        try:
            resp = self.session.get(
                f"{self.server_url}/health",
                timeout=self._fast_timeout,
            )
            return resp.status_code == 200
        except Exception as e:
            logger.debug(f"Health check failed: {e}")
            return False

    # ═══════════════════════════════════════════════════════════════════
    # CHECK HASH (deduplicate before upload)
    # ═══════════════════════════════════════════════════════════════════

    def check_hash_exists(self, sha256: str) -> Optional[dict]:
        """
        Check if a file with this SHA-256 already exists on the server.
        Returns the file dict if found, None otherwise.
        """
        try:
            resp = self.session.get(
                f"{self.server_url}/api/files/check-hash/{sha256}",
                timeout=self._fast_timeout,
            )
            if resp.status_code == 200:
                data = resp.json()
                if data.get("exists"):
                    return data.get("file")
            return None
        except Exception as e:
            logger.warning(f"Hash check error: {e}")
            return None

    # ═══════════════════════════════════════════════════════════════════
    # UPLOAD
    # ═══════════════════════════════════════════════════════════════════

    def upload_file(self, filepath: str, original_name: Optional[str] = None) -> UploadResult:
        """
        Upload a file to the server via multipart POST.
        Returns an UploadResult with server_file_id on success.
        """
        if not os.path.isfile(filepath):
            return UploadResult(success=False, error=f"File not found: {filepath}")

        filename = original_name or os.path.basename(filepath)
        file_size = os.path.getsize(filepath)

        try:
            logger.info(f"Uploading: {filename} ({file_size:,} bytes)")

            with open(filepath, "rb") as f:
                resp = self.session.post(
                    f"{self.server_url}/api/files/upload",
                    files={"file": (filename, f, "application/octet-stream")},
                    timeout=self._transfer_timeout,
                )

            if resp.status_code in (200, 201):
                data = resp.json()
                file_info = data.get("file", {})
                server_id = str(file_info.get("id", ""))
                checksum = file_info.get("checksum")

                logger.info(f"✅ Uploaded: {filename} → server ID {server_id}")
                return UploadResult(
                    success=True,
                    server_file_id=server_id,
                    checksum=checksum,
                )
            else:
                error_msg = f"HTTP {resp.status_code}: {resp.text[:200]}"
                logger.error(f"❌ Upload failed: {filename} — {error_msg}")
                return UploadResult(success=False, error=error_msg)

        except requests.exceptions.ConnectionError as e:
            return UploadResult(success=False, error=f"Connection error: {e}")
        except requests.exceptions.Timeout:
            return UploadResult(success=False, error="Upload timed out")
        except Exception as e:
            return UploadResult(success=False, error=str(e))

    # ═══════════════════════════════════════════════════════════════════
    # LIST FILES (server manifest for bidirectional sync)
    # ═══════════════════════════════════════════════════════════════════

    def list_files(self, limit: int = 100, offset: int = 0) -> list[RemoteFile]:
        """
        Fetch the server's file manifest. Returns a list of RemoteFile.
        Paginates automatically to get ALL files.
        """
        all_files: list[RemoteFile] = []

        try:
            current_offset = offset
            while True:
                resp = self.session.get(
                    f"{self.server_url}/api/files/list",
                    params={"limit": limit, "offset": current_offset},
                    timeout=self._fast_timeout,
                )

                if resp.status_code != 200:
                    logger.error(f"List files failed: HTTP {resp.status_code}")
                    break

                data = resp.json()
                files = data.get("files", [])
                pagination = data.get("pagination", {})

                for f in files:
                    all_files.append(RemoteFile(
                        server_id=str(f.get("id", "")),
                        original_name=f.get("originalName", "unknown"),
                        file_size=f.get("fileSize", 0),
                        checksum=f.get("checksum"),
                        uploaded_at=f.get("uploadedAt"),
                    ))

                total = pagination.get("total", 0)
                current_offset += limit
                if current_offset >= total or not files:
                    break

            logger.debug(f"Listed {len(all_files)} files from server")
            return all_files

        except Exception as e:
            logger.error(f"List files error: {e}")
            return all_files

    # ═══════════════════════════════════════════════════════════════════
    # DOWNLOAD (pull remote file to local disk)
    # ═══════════════════════════════════════════════════════════════════

    def download_file(self, server_file_id: str, dest_path: str) -> bool:
        """
        Download a file from the server and save to dest_path.
        Creates parent directories if needed.
        Returns True on success.
        """
        try:
            os.makedirs(os.path.dirname(dest_path), exist_ok=True)

            logger.info(f"Downloading: server ID {server_file_id} → {dest_path}")

            resp = self.session.get(
                f"{self.server_url}/api/files/download/{server_file_id}",
                stream=True,
                timeout=self._transfer_timeout,
            )

            if resp.status_code != 200:
                logger.error(f"❌ Download failed: HTTP {resp.status_code}")
                return False

            total_bytes = 0
            with open(dest_path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        f.write(chunk)
                        total_bytes += len(chunk)

            if total_bytes == 0:
                logger.warning(f"Downloaded file is empty: {dest_path}")
                try:
                    os.unlink(dest_path)
                except OSError:
                    pass
                return False

            logger.info(f"✅ Downloaded: {os.path.basename(dest_path)} ({total_bytes:,} bytes)")
            return True

        except requests.exceptions.ConnectionError as e:
            logger.error(f"❌ Download connection error: {e}")
            return False
        except Exception as e:
            logger.error(f"❌ Download error: {e}")
            try:
                if os.path.exists(dest_path):
                    os.unlink(dest_path)
            except OSError:
                pass
            return False

    # ═══════════════════════════════════════════════════════════════════
    # SERVER INFO
    # ═══════════════════════════════════════════════════════════════════

    def get_server_info(self) -> Optional[dict]:
        """Fetch server diagnostics (uptime, disk, memory)."""
        try:
            resp = self.session.get(
                f"{self.server_url}/api/server-info",
                timeout=self._fast_timeout,
            )
            if resp.status_code == 200:
                return resp.json()
            return None
        except Exception:
            return None