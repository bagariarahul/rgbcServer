"""
RGBC Drive — Master Node Local API Server (Sprint 3)

A lightweight FastAPI server that mirrors the Node.js backend's file
endpoints so Slave nodes can connect directly to the Master using the
EXACT SAME Retrofit/requests code — they just swap the base URL.

Endpoints (match Node.js backend 1:1):
  GET  /health                          — connectivity test
  GET  /api/server-info                 — Master disk/memory/uptime
  GET  /api/files/list                  — file manifest from SQLite
  GET  /api/files/check-hash/{sha256}   — deduplicate by checksum
  POST /api/files/upload                — receive file from Slave
  GET  /api/files/download/{file_id}    — stream file to Slave

Authentication (Sprint 3 — RS256 ONLY):
  Authorization: Bearer <jwt> → verified with RSA public key (public.pem)

  The Master holds ONLY the public key. It can verify tokens issued by
  the Gateway but can NEVER forge new ones. This eliminates the lateral
  escalation vulnerability from the Sprint 2 threat model.

  M2M API Key and HS256 JWT_SECRET are fully REMOVED.

Integration:
  Runs on uvicorn in a daemon thread. Does NOT block the main thread
  (which runs pystray). Shares the SyncDatabase instance with the
  scanner, watcher, and poller (thread-safe via WAL mode).
"""

import os
import re
import sys
import hashlib
import logging
import platform
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Optional

from fastapi import FastAPI, Request, UploadFile, File, HTTPException, Depends
from fastapi.responses import FileResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware

logger = logging.getLogger("RGBCDrive.MasterAPI")

# ── Injected by rgbc_drive.py at startup ─────────────────────────────
_sync_root: str = ""
_db = None       # SyncDatabase instance
_scanner = None  # DirectoryScanner instance

# ── RS256 public key (loaded once at startup) ────────────────────────
_public_key: str = ""
_public_key_path: str = ""

# ── Owner binding (Sprint 3.1 — cross-user access defense) ───────────
# Set by create_master_api() from rgbc_drive.py. Any inbound JWT whose
# `userId` claim does not match this value is rejected with 403.
# Empty string means "unbound" (insecure — only used during legacy
# upgrades; logs a warning at startup).
_owner_user_id: str = ""


def _load_public_key() -> None:
    """Load the RSA public key from disk. Called during create_master_api()."""
    global _public_key, _public_key_path

    # Check env var first, then default to public.pem next to the script
    _public_key_path = os.getenv("PUBLIC_KEY_PATH", "")
    if not _public_key_path:
        script_dir = Path(os.path.dirname(os.path.abspath(
            sys.argv[0] if sys.argv[0] else __file__
        )))
        _public_key_path = str(script_dir / "public.pem")

    try:
        with open(_public_key_path, 'r') as f:
            _public_key = f.read()
        logger.info(f"✅ RS256 public key loaded from {_public_key_path}")
    except FileNotFoundError:
        logger.error(
            f"❌ RS256 public key not found at: {_public_key_path}\n"
            "   Copy public.pem from the Gateway's src/keys/ directory.\n"
            "   Slave connections will be rejected until this is fixed."
        )


# ═══════════════════════════════════════════════════════════════════════
# FACTORY
# ═══════════════════════════════════════════════════════════════════════

def create_master_api(
    sync_root: str,
    db,
    scanner=None,
    owner_user_id: str = "",
) -> FastAPI:
    """
    Factory function that creates the FastAPI app with injected dependencies.
    Called once from rgbc_drive.py at startup.

    Sprint 3:    api_key and jwt_secret parameters REMOVED.
                 Authentication uses RS256 public key only.
    Sprint 3.1:  owner_user_id parameter ADDED.
                 Master rejects any JWT whose userId != owner_user_id,
                 closing the cross-user access vulnerability.
    """
    global _sync_root, _db, _scanner, _owner_user_id
    _sync_root = os.path.normpath(sync_root)
    _db = db
    _scanner = scanner
    _owner_user_id = str(owner_user_id or "").strip()

    if not _owner_user_id:
        logger.warning(
            "⚠️ Master started WITHOUT owner binding. Any valid Gateway JWT "
            "will be accepted — INSECURE. Pass owner_user_id from rgbc_drive.py."
        )
    else:
        logger.info(f"🔒 Master locked to owner userId: {_owner_user_id}")

    # Load the RS256 public key
    _load_public_key()

    app = FastAPI(
        title="RGBC Drive Master API",
        version="3.0",
        docs_url=None,
        redoc_url=None,
    )

    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(_build_router())

    return app


# ═══════════════════════════════════════════════════════════════════════
# AUTHENTICATION — RS256 ONLY
# ═══════════════════════════════════════════════════════════════════════

async def verify_auth(request: Request) -> dict:
    """
    Sprint 3: RS256 JWT verification using RSA public key.

    The Master node holds ONLY the public key. It can verify tokens
    issued by the Gateway but can never forge new ones.

    X-API-Key path is REMOVED.
    HS256 JWT_SECRET path is REMOVED.
    """
    auth_header = request.headers.get("authorization", "")

    if not auth_header.startswith("Bearer "):
        raise HTTPException(
            status_code=401,
            detail="Authentication required. Provide: Authorization: Bearer <jwt>"
        )

    token = auth_header[7:].strip()

    if not _public_key:
        raise HTTPException(
            status_code=500,
            detail=(
                f"RS256 public key not loaded on Master. "
                f"Expected at: {_public_key_path}. "
                f"Copy public.pem from the Gateway's src/keys/ directory."
            )
        )

    try:
        import jwt as pyjwt

        decoded = pyjwt.decode(
            token,
            _public_key,
            algorithms=["RS256"],
            options={
                "verify_exp": True,
                "verify_iss": False,
                "verify_aud": False,
                "verify_iat": False,
                "require": ["exp", "userId"],
            },
            leeway=30,
        )

        # ── Owner binding check (Sprint 3.1) ──────────────────────
        # Reject any JWT not issued for this master's owner. This is
        # the defense against an attacker using their own valid JWT
        # against another user's tunnel URL after discovery via CT
        # logs, screenshots, etc.
        token_user_id = str(decoded.get("userId") or "").strip()
        if not token_user_id:
            logger.warning("JWT missing userId claim — rejecting")
            raise HTTPException(
                status_code=401,
                detail="JWT missing required userId claim",
            )

        if _owner_user_id and token_user_id != _owner_user_id:
            # Logged with both IDs for forensics, but the response is
            # a generic 403 so we don't leak which user owns this master.
            logger.warning(
                f"🚨 Cross-user access attempt: token userId={token_user_id} "
                f"!= owner userId={_owner_user_id} — rejecting"
            )
            raise HTTPException(status_code=403, detail="Access denied")

        return {
            "id": token_user_id,
            "email": decoded.get("email", "unknown"),
            "sessionId": decoded.get("sessionId"),
            "auth_method": "RS256_JWT",
        }

    except HTTPException:
        # Re-raise our own intentional rejections without re-wrapping
        raise

    except ImportError:
        raise HTTPException(
            status_code=500,
            detail="PyJWT[crypto] not installed. Run: pip install PyJWT[crypto]"
        )

    except Exception as e:
        error_type = type(e).__name__

        if "ExpiredSignatureError" in error_type:
            raise HTTPException(
                status_code=401,
                detail="JWT expired. Log out and back in to get a fresh 30-day token."
            )

        if "InvalidSignatureError" in error_type or "Signature" in str(e):
            logger.error(f"RS256 signature mismatch: {e}")
            raise HTTPException(
                status_code=401,
                detail=(
                    "JWT signature verification failed. "
                    "Ensure public.pem on the Master matches private.pem on the Gateway."
                )
            )

        if "DecodeError" in error_type:
            raise HTTPException(status_code=401, detail=f"Malformed JWT: {e}")

        logger.error(f"JWT verification failed ({error_type}): {e}")
        raise HTTPException(status_code=401, detail=f"JWT verification failed: {e}")


# ═══════════════════════════════════════════════════════════════════════
# ROUTE BUILDER
# ═══════════════════════════════════════════════════════════════════════

def _build_router():
    from fastapi import APIRouter

    router = APIRouter()

    # ── GET /health ──────────────────────────────────────────────────
    @router.get("/health")
    async def health():
        return {
            "status": "healthy",
            "node": "MASTER",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── GET /api/server-info ─────────────────────────────────────────
    # Sprint 2.5 fix: "node" is a dict matching Android NodeInfo(version, env)
    @router.get("/api/server-info", dependencies=[Depends(verify_auth)])
    async def server_info():
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
                "version": f"Python {sys.version.split()[0]}",
                "env": "master",
            },
            "sync": stats,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    # ── GET /api/files/list ──────────────────────────────────────────
    @router.get("/api/files/list", dependencies=[Depends(verify_auth)])
    async def list_files(limit: int = 100, offset: int = 0):
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
        if not file.filename:
            raise HTTPException(status_code=400, detail="No filename provided")

        safe_name = _sanitize_filename(file.filename)
        dest_path = os.path.join(_sync_root, safe_name)

        if os.path.exists(dest_path):
            base, ext = os.path.splitext(safe_name)
            timestamp = int(time.time())
            safe_name = f"{base}_{timestamp}{ext}"
            dest_path = os.path.join(_sync_root, safe_name)

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

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
            if os.path.exists(dest_path):
                os.unlink(dest_path)
            raise HTTPException(status_code=500, detail=f"Upload failed: {e}")

        checksum = sha.hexdigest()

        stat = os.stat(dest_path)
        rel_path = str(PurePosixPath(Path(os.path.relpath(dest_path, _sync_root))))

        _db.upsert_local_file(
            relative_path=rel_path,
            sha256=checksum,
            size=total_bytes,
            mtime_ns=stat.st_mtime_ns,
            sync_status="SYNCED",
        )

        if _scanner:
            try:
                _scanner.scan()
            except Exception:
                pass

        logger.info(f"📥 Received from Slave: {safe_name} ({total_bytes:,} bytes, SHA-256: {checksum[:16]}...)")

        return JSONResponse(
            status_code=201,
            content={
                "message": "File uploaded successfully",
                "file": {
                    "id": rel_path,
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
        if ".." in file_id or file_id.startswith("/"):
            raise HTTPException(status_code=400, detail="Invalid file path")

        abs_path = os.path.normpath(os.path.join(_sync_root, file_id))

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
    filename = filename.replace("\x00", "")
    filename = filename.replace("\\", "/")
    filename = filename.lstrip("/.")
    parts = [p for p in filename.split("/") if p and p != ".."]
    if not parts:
        return f"upload_{uuid.uuid4().hex[:8]}"
    return "/".join(parts)


def _guess_mime(filename: str) -> str:
    import mimetypes
    mt, _ = mimetypes.guess_type(filename)
    return mt or "application/octet-stream"


# ═══════════════════════════════════════════════════════════════════════
# SERVER LIFECYCLE
# ═══════════════════════════════════════════════════════════════════════

def start_master_api(
    sync_root: str,
    db,
    scanner=None,
    owner_user_id: str = "",
    host: str = "0.0.0.0",
    port: int = 8741,
) -> None:
    """
    Start the FastAPI server on a daemon thread.
    Does NOT block the calling thread.

    Sprint 3:    api_key and jwt_secret parameters REMOVED.
    Sprint 3.1:  owner_user_id parameter ADDED — required for safe operation.

    Usage from rgbc_drive.py:
        from master_api import start_master_api
        start_master_api(SYNC_ROOT, db, scanner=scanner,
                         owner_user_id=oauth.user_id)
    """
    import threading
    import uvicorn

    app = create_master_api(
        sync_root=sync_root,
        db=db,
        scanner=scanner,
        owner_user_id=owner_user_id,
    )

    config = uvicorn.Config(
        app=app,
        host=host,
        port=port,
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)

    server.install_signal_handlers = lambda: None

    thread = threading.Thread(
        target=server.run,
        daemon=True,
        name="MasterAPI",
    )
    thread.start()

    logger.info(f"🌐 Master API started on http://{host}:{port}")
    logger.info(f"   Auth: RS256 public key verification (public.pem)")