"""
RGBC Drive — Server API Client

Handles all HTTP communication with the RGBC backend.
Authenticates via X-API-Key header (M2M API Key).

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

logger = logging.getLogger("RGBCDrive.API")


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


class APIClient:
    """
    Thread-safe HTTP client for the RGBC backend.

    The requests.Session is thread-safe per the requests docs:
    "Session objects can safely be used from multiple threads."
    """

    def __init__(self, server_url: str, api_key: str):
        self.server_url = server_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({
            "X-API-Key": api_key,
            "Accept": "application/json",
        })
        # Timeouts: (connect, read)
        # Upload/download use longer read timeouts
        self._fast_timeout = (10, 30)
        self._transfer_timeout = (30, 600)  # 10min read for large files

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

                # Check if there are more pages
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
            # Ensure parent directory exists
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

            # Stream to disk in chunks
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
            # Clean up partial file
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