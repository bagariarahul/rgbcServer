"""
RGBC Drive — Master Node Local API Server

A lightweight FastAPI server that mirrors the Node.js backend's file
endpoints so that Slave nodes (Android/iOS/Python) can connect directly
to the Master using the EXACT SAME Retrofit/requests code — they just
swap the base URL from api.bagariaa.in to the Master's tunnel URL.

Endpoints (match Node.js backend 1:1):
  GET  /health                          — connectivity test
  GET  /api/server-info                 — Master disk/memory/uptime
  GET  /api/files/list                  — file manifest from SQLite
  GET  /api/files/check-hash/{sha256}   — deduplicate by checksum
  POST /api/files/upload                — receive file from Slave
  GET  /api/files/download/{file_id}    — stream file to Slave

Authentication:
  Same dual-auth as the Node.js verifyToken.js middleware:
    1. X-API-Key header → constant-time compare against M2M_API_KEY
    2. Authorization: Bearer <jwt> → verify signature against JWT_SECRET

  The Master does NOT issue tokens. The API Gateway is the single
  source of truth for auth. The Master simply validates credentials.

Integration:
  Runs on uvicorn in a daemon thread. Does NOT block the main thread
  (which runs pystray). Shares the SyncDatabase instance with the
  scanner, watcher, and poller (thread-safe via WAL mode).
"""

import os
import re
import hmac
import hashlib
import logging
import platform
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional
import sys as _sys


from fastapi import FastAPI, Request, UploadFile, File, HTTPException, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("RGBCDrive.MasterAPI")

# ── These will be injected by rgbc_drive.py at startup ───────────────────
_sync_root: str = ""
_db = None  # SyncDatabase instance
_api_key: str = ""
_jwt_secret: str = ""
_scanner = None  # DirectoryScanner instance


def create_master_api(
    sync_root: str,
    db,
    api_key: str,
    jwt_secret: str = "",
    scanner=None,
) -> FastAPI:
    """
    Factory function that creates the FastAPI app with injected dependencies.
    Called once from rgbc_drive.py at startup.
    """
    global _sync_root, _db, _api_key, _jwt_secret, _scanner
    _sync_root = os.path.normpath(sync_root)
    _db = db
    _api_key = api_key
    _jwt_secret = jwt_secret
    _scanner = scanner

    app = FastAPI(
        title="RGBC Drive Master API",
        version="2.0",
        docs_url=None,      # Disable Swagger UI in production
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ── Register all routes ──────────────────────────────────────────
    app.include_router(_build_router())

    return app


# ═══════════════════════════════════════════════════════════════════════
# AUTHENTICATION DEPENDENCY
# ═══════════════════════════════════════════════════════════════════════

async def verify_auth(request: Request) -> dict:
    """
    FastAPI dependency that validates incoming requests.
    Mirrors the Node.js verifyToken.js dual-auth logic.
    """
    import hashlib
 
    # ── Path 1: X-API-Key header ─────────────────────────────────────
    api_key_header = request.headers.get("x-api-key")
    if api_key_header:
        if not _api_key:
            raise HTTPException(status_code=500, detail="M2M API key not configured on Master")
 
        if hmac.compare_digest(api_key_header.strip(), _api_key.strip()):
            return {"id": "M2M_CLIENT", "email": "admin@m2m", "auth_method": "API_KEY"}
 
        raise HTTPException(status_code=401, detail="Invalid API key")
 
    # ── Path 2: JWT Bearer token ─────────────────────────────────────
    auth_header = request.headers.get("authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header[7:].strip()
 
        if not _jwt_secret:
            raise HTTPException(
                status_code=500,
                detail="JWT_SECRET not configured on Master. Set it in .env."
            )
 
        try:
            import jwt as pyjwt
 
            # CRITICAL: Strip the secret and encode to bytes explicitly.
            # Node.js jsonwebtoken uses Buffer.from(secret) which is UTF-8.
            # python-dotenv and Node dotenv parse .env values slightly
            # differently — trailing newlines, spaces, or BOM characters
            # can cause HMAC mismatches even when the .env files look identical.
            secret_bytes = _jwt_secret.strip().encode("utf-8")
 
            # Log the secret fingerprint ONCE for debugging
            # (safe to log — only shows length and a hash, not the actual secret)
            secret_hash = hashlib.sha256(secret_bytes).hexdigest()[:12]
            logger.debug(
                f"JWT verification using secret: length={len(secret_bytes)}, "
                f"fingerprint={secret_hash}"
            )
 
            decoded = pyjwt.decode(
                token,
                secret_bytes,
                algorithms=["HS256"],
                options={
                    # Node.js generateTokens() sets expiresIn: '1h' which adds
                    # an exp claim. We verify it but allow 60s clock skew.
                    "verify_exp": True,
 
                    # Node.js does NOT set iss or aud claims.
                    # PyJWT's default is to require these if present in the token.
                    # Setting to False prevents spurious failures.
                    "verify_iss": False,
                    "verify_aud": False,
 
                    # iat is set manually by Node. Verify it exists but don't
                    # reject based on value (clock skew).
                    "verify_iat": False,
                },
                # Allow 120 seconds of clock skew between the Node.js server
                # (which signs the JWT) and this Master PC (which verifies it).
                # This is generous but safe — prevents failures when the
                # Android phone and Master PC clocks are slightly out of sync.
                leeway=120,
            )
 
            return {
                "id": decoded.get("userId", "unknown"),
                "email": decoded.get("email", "unknown"),
                "sessionId": decoded.get("sessionId"),
                "auth_method": "JWT",
            }
 
        except ImportError:
            raise HTTPException(
                status_code=500,
                detail="PyJWT not installed. Run: pip install PyJWT"
            )
 
        except Exception as e:
            error_type = type(e).__name__
            error_msg = str(e)
 
            # Provide specific error messages for each failure mode
            if "ExpiredSignatureError" in error_type or "expired" in error_msg.lower():
                logger.warning(f"JWT expired: {error_msg}")
                raise HTTPException(
                    status_code=401,
                    detail="JWT expired. The Android token is only valid for 1 hour. "
                           "Log out and log back in to get a fresh token."
                )
 
            if "InvalidSignatureError" in error_type or "Signature" in error_msg:
                # This is the critical error — log everything for debugging
                logger.error(
                    f"JWT SIGNATURE MISMATCH!\n"
                    f"  Error: {error_msg}\n"
                    f"  Secret length: {len(_jwt_secret.strip())}\n"
                    f"  Secret fingerprint: {hashlib.sha256(_jwt_secret.strip().encode('utf-8')).hexdigest()[:16]}\n"
                    f"  Token header: {token.split('.')[0] if '.' in token else 'INVALID'}\n"
                    f"  Hint: Run this on your Node.js server to compare fingerprints:\n"
                    f"    node -e \"const c=require('crypto');console.log(c.createHash('sha256').update(process.env.JWT_SECRET).digest('hex').substring(0,16))\"\n"
                )
                raise HTTPException(
                    status_code=401,
                    detail=f"JWT signature mismatch. The Master's JWT_SECRET doesn't match the Gateway's. "
                           f"Check .env files. Secret fingerprint: {hashlib.sha256(_jwt_secret.strip().encode('utf-8')).hexdigest()[:16]}"
                )
 
            if "DecodeError" in error_type:
                raise HTTPException(status_code=401, detail=f"Malformed JWT: {error_msg}")
 
            logger.error(f"JWT verification failed ({error_type}): {error_msg}")
            raise HTTPException(status_code=401, detail=f"JWT verification failed: {error_msg}")
 
    # Neither auth method provided
    raise HTTPException(
        status_code=401,
        detail="Authentication required. Provide X-API-Key or Authorization: Bearer <jwt>"
    )




# ═══════════════════════════════════════════════════════════════════════
# ROUTE BUILDER
# ═══════════════════════════════════════════════════════════════════════

def _build_router():
    from fastapi import APIRouter

    router = APIRouter()

    # ── GET /health ──────────────────────────────────────────────────
    @router.get("/health")
    async def health():
        return {"status": "healthy", "node": "MASTER", "timestamp": datetime.now(timezone.utc).isoformat()}

    # ── GET /api/server-info ─────────────────────────────────────────
    @router.get("/api/server-info", dependencies=[Depends(verify_auth)])
    @router.get("/api/server-info", dependencies=[Depends(verify_auth)])
    async def server_info():
        import sys as _sys
 
        disk = shutil.disk_usage(_sync_root)
        mem_total = mem_free = mem_used = mem_pct = 0
        try:
            import psutil
            mem = psutil.virtual_memory()
            mem_total = mem.total
            mem_free = mem.available
            mem_used = mem.used
            mem_pct = int(mem.percent)
        except ImportError:
            pass
 
        uptime_seconds = int(time.time() - _boot_time)
        days = uptime_seconds // 86400
        hours = (uptime_seconds % 86400) // 3600
        minutes = (uptime_seconds % 3600) // 60
        parts = []
        if days > 0: parts.append(f"{days}d")
        if hours > 0: parts.append(f"{hours}h")
        if minutes > 0: parts.append(f"{minutes}m")
        formatted = " ".join(parts) if parts else "< 1m"
 
        stats = _db.get_stats() if _db else {}
 
        return {
            "status": "online",
            "uptime": {"seconds": uptime_seconds, "formatted": formatted},
            "os": {
                "type": platform.system(),
                "platform": platform.platform(),
                "release": platform.release(),
                "arch": platform.machine(),
                "hostname": platform.node(),
            },
            "memory": {
                "total": mem_total, "free": mem_free,
                "used": mem_used, "usedPercent": mem_pct,
            },
            "disk": {
                "total": disk.total, "free": disk.free,
                "used": disk.used,
                "usedPercent": int((disk.used / disk.total) * 100) if disk.total else 0,
            },
            "node": {
                "version": f"Python {_sys.version.split()[0]}",
                "env": "master",
            },
            "sync": stats,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── GET /api/files/list ──────────────────────────────────────────
    @router.get("/api/files/list", dependencies=[Depends(verify_auth)])
    async def list_files(limit: int = 100, offset: int = 0):
        """
        Return the file manifest from the local SQLite DB.
        Response shape matches the Node.js backend exactly.
        """
        conn = _db._conn

        total_row = conn.execute(
            "SELECT COUNT(*) as cnt FROM local_files WHERE sync_status != 'DELETED_LOCAL'"
        ).fetchone()
        total = total_row["cnt"] if total_row else 0

        rows = conn.execute("""
            SELECT relative_path, sha256, size, mtime_ns, server_file_id,
                   sync_status, last_synced_at, created_at
            FROM local_files
            WHERE sync_status != 'DELETED_LOCAL'
            ORDER BY created_at DESC
            LIMIT ? OFFSET ?
        """, (limit, offset)).fetchall()

        files = []
        for row in rows:
            files.append({
                "id": row["relative_path"],
                "originalName": os.path.basename(row["relative_path"]),
                "fileSize": row["size"],
                "mimeType": _guess_mime(row["relative_path"]),
                "checksum": row["sha256"],
                "uploadStatus": "completed",
                "backupStatus": row["sync_status"],
                "isEncrypted": False,
                "uploadedAt": row["last_synced_at"] or row["created_at"],
                "relativePath": row["relative_path"],
            })

        return {
            "files": files,
            "pagination": {
                "total": total,
                "limit": limit,
                "offset": offset,
                "totalPages": max(1, -(-total // limit)),
                "currentPage": (offset // limit) + 1,
            },
        }

    # ── GET /api/files/check-hash/{sha256} ───────────────────────────
    @router.get("/api/files/check-hash/{sha256}", dependencies=[Depends(verify_auth)])
    async def check_hash(sha256: str):
        """Check if a file with this SHA-256 exists on the Master."""
        if not re.match(r'^[a-f0-9]{64}$', sha256, re.IGNORECASE):
            raise HTTPException(status_code=400, detail="SHA-256 must be 64 hex characters")

        row = _db._conn.execute(
            "SELECT * FROM local_files WHERE sha256 = ? AND sync_status != 'DELETED_LOCAL' LIMIT 1",
            (sha256.lower(),)
        ).fetchone()

        if row:
            return {
                "exists": True,
                "file": {
                    "id": row["relative_path"],
                    "originalName": os.path.basename(row["relative_path"]),
                    "fileSize": row["size"],
                    "uploadedAt": row["last_synced_at"] or row["created_at"],
                },
            }

        return {"exists": False}

    # ── POST /api/files/upload ───────────────────────────────────────
    @router.post("/api/files/upload", dependencies=[Depends(verify_auth)])
    async def upload_file(file: UploadFile = File(...)):
        """
        Receive a file from a Slave and save it directly to SYNC_ROOT.
        Then index it in the local SQLite DB.
        Response shape matches the Node.js backend exactly.
        """
        if not file.filename:
            raise HTTPException(status_code=400, detail="No filename provided")

        # Sanitize the filename — prevent path traversal
        safe_name = _sanitize_filename(file.filename)
        dest_path = os.path.join(_sync_root, safe_name)

        # If a file with the same name already exists, add a suffix
        if os.path.exists(dest_path):
            base, ext = os.path.splitext(safe_name)
            timestamp = int(time.time())
            safe_name = f"{base}_{timestamp}{ext}"
            dest_path = os.path.join(_sync_root, safe_name)

        # Ensure parent directories exist
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        # Stream the upload to disk + compute SHA-256 simultaneously
        sha = hashlib.sha256()
        total_bytes = 0

        try:
            with open(dest_path, "wb") as f:
                while True:
                    chunk = await file.read(65536)
                    if not chunk:
                        break
                    f.write(chunk)
                    sha.update(chunk)
                    total_bytes += len(chunk)
        except Exception as e:
            # Clean up partial file
            if os.path.exists(dest_path):
                os.unlink(dest_path)
            raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

        checksum = sha.hexdigest()

        # Index in SQLite
        stat = os.stat(dest_path)
        rel_path = str(PurePosixPath(Path(os.path.relpath(dest_path, _sync_root))))

        _db.upsert_local_file(
            relative_path=rel_path,
            sha256=checksum,
            size=total_bytes,
            mtime_ns=stat.st_mtime_ns,
            sync_status="SYNCED",
        )

        # Trigger a re-scan so other components pick up the new file
        if _scanner:
            try:
                _scanner.scan()
            except Exception:
                pass  # Non-critical — will be caught by periodic scan

        file_id = rel_path
        logger.info(f"📥 Received from Slave: {safe_name} ({total_bytes:,} bytes, SHA-256: {checksum[:16]}...)")

        return JSONResponse(
            status_code=201,
            content={
                "message": "File uploaded successfully",
                "file": {
                    "id": file_id,
                    "originalName": safe_name,
                    "fileName": safe_name,
                    "fileSize": total_bytes,
                    "mimeType": _guess_mime(safe_name),
                    "checksum": checksum,
                    "uploadStatus": "completed",
                    "backupStatus": "SYNCED",
                    "isEncrypted": False,
                    "uploadedAt": datetime.now(timezone.utc).isoformat(),
                },
            },
        )

    # ── GET /api/files/download/{file_id:path} ──────────────────────
    @router.get("/api/files/download/{file_id:path}", dependencies=[Depends(verify_auth)])
    async def download_file(file_id: str):
        """
        Stream a file from the Master's SYNC_ROOT to the Slave.
        file_id is the relative_path (forward slashes).
        """
        # Validate: prevent path traversal
        if ".." in file_id or file_id.startswith("/"):
            raise HTTPException(status_code=400, detail="Invalid file path")

        abs_path = os.path.normpath(os.path.join(_sync_root, file_id))

        # Security: ensure the resolved path is still inside SYNC_ROOT
        if not abs_path.startswith(_sync_root):
            raise HTTPException(status_code=403, detail="Access denied: path outside sync root")

        if not os.path.isfile(abs_path):
            raise HTTPException(status_code=404, detail=f"File not found: {file_id}")

        filename = os.path.basename(abs_path)
        media_type = _guess_mime(filename) or "application/octet-stream"

        return FileResponse(
            path=abs_path,
            filename=filename,
            media_type=media_type,
        )

    return router


# ═══════════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════════

_boot_time = time.time()


def _sanitize_filename(filename: str) -> str:
    """
    Remove dangerous characters from a filename.
    Preserves subdirectory structure (forward slashes) but blocks traversal.
    """
    # Remove null bytes
    filename = filename.replace("\x00", "")
    # Normalize path separators to forward slashes
    filename = filename.replace("\\", "/")
    # Remove leading slashes and dots
    filename = filename.lstrip("/.")
    # Remove any path traversal
    parts = [p for p in filename.split("/") if p and p != ".."]
    if not parts:
        return f"upload_{uuid.uuid4().hex[:8]}"
    return "/".join(parts)


def _guess_mime(filename: str) -> str:
    """Guess MIME type from filename extension."""
    import mimetypes
    mt, _ = mimetypes.guess_type(filename)
    return mt or "application/octet-stream"


# ═══════════════════════════════════════════════════════════════════════
# SERVER LIFECYCLE (called from rgbc_drive.py)
# ═══════════════════════════════════════════════════════════════════════

def start_master_api(
    sync_root: str,
    db,
    api_key: str,
    jwt_secret: str = "",
    scanner=None,
    host: str = "0.0.0.0",
    port: int = 8741,
) -> None:
    """
    Start the FastAPI server on a daemon thread.
    Does NOT block the calling thread.

    Usage from rgbc_drive.py:
        from master_api import start_master_api
        start_master_api(SYNC_ROOT, db, API_KEY, JWT_SECRET, scanner)
    """
    import threading
    import uvicorn

    app = create_master_api(
        sync_root=sync_root,
        db=db,
        api_key=api_key,
        jwt_secret=jwt_secret,
        scanner=scanner,
    )

    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="warning",  # Suppress uvicorn's default INFO spam
        access_log=False,
    )
    server = uvicorn.Server(config)

    # Override uvicorn's signal handlers — we manage shutdown via pystray
    server.install_signal_handlers = lambda: None

    thread = threading.Thread(
        target=server.run,
        daemon=True,
        name="MasterAPI",
    )
    thread.start()

    logger.info(f"🌐 Master API started on http://{host}:{port}")
    logger.info(f"   Slaves can connect via the Cloudflare Tunnel URL")