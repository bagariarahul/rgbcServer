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

# ── Sprint 3.3: chunked upload constants ─────────────────────────────
# Files <= this size go through the single-shot /api/files/upload fast path.
# Larger files MUST use the chunked /api/files/upload/init flow.
# 50 MiB leaves headroom under Cloudflare's 100 MB request body limit
# for HTTP framing, headers, and any future reverse-proxy growth.
SMALL_FILE_THRESHOLD = 50 * 1024 * 1024
CHUNK_HARD_MAX = 80 * 1024 * 1024  # individual chunk hard cap (defense-in-depth vs. CF 100MB limit)
UPLOAD_TTL_HOURS = 24
UPLOAD_TEMP_DIR_NAME = ".rgbc_uploads"
MAX_TOTAL_FILE_SIZE = 100 * 1024 * 1024 * 1024  # 100 GB hard cap per upload session

# ── Sprint 3.5: dashboard plumbing ───────────────────────────────────
# Set by rgbc_drive.py via set_dashboard_callbacks(...) after the watcher
# and poller exist. Dashboard endpoints invoke these to trigger pause/
# resume/scan and the account-switch flow without reaching back into
# the rgbc_drive.py module scope.
_dashboard_callbacks: dict = {}
_dashboard_static_dir: str = ""  # absolute path to dashboard/ folder
_app_version: str = "3.5"

# ── Sprint 3.5: tunnel URL provider ──────────────────────────────────
# rgbc_drive.py registers a no-arg callable that returns the current
# tunnel URL (or None). The dashboard's status endpoint calls it.
_tunnel_getter = None

# ── Sprint 3.5: heartbeat tracking (for "last heartbeat" display) ────
_last_heartbeat_at: float = 0.0


def set_dashboard_callbacks(
    static_dir: str,
    pause_sync=None,
    resume_sync=None,
    force_scan=None,
    switch_account=None,
    tunnel_getter=None,
    app_version: str = "3.5",
) -> None:
    """
    Wire dashboard control-plane callbacks. Called from rgbc_drive.py
    after the watcher/poller/tunnel exist. None values are tolerated —
    the dashboard endpoints respond with 503 if the corresponding
    callback isn't registered.
    """
    global _dashboard_callbacks, _dashboard_static_dir, _tunnel_getter, _app_version
    _dashboard_static_dir = static_dir
    _dashboard_callbacks = {
        "pause": pause_sync,
        "resume": resume_sync,
        "scan": force_scan,
        "switch_account": switch_account,
    }
    _tunnel_getter = tunnel_getter
    _app_version = app_version
    logger.info(f"📊 Dashboard callbacks registered (static dir: {static_dir})")


def record_heartbeat() -> None:
    """Called by rgbc_drive.py's heartbeat loop after each successful ping."""
    global _last_heartbeat_at
    _last_heartbeat_at = time.time()


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

    # ── POST /api/files/upload (small-file fast path) ─────────────────
    # Sprint 3.3: This endpoint is now ONLY for files <= SMALL_FILE_THRESHOLD.
    # Large files MUST use the chunked /api/files/upload/init flow.
    @router.post("/api/files/upload", dependencies=[Depends(verify_auth)])
    async def upload_file(request: Request, file: UploadFile = File(...)):
        if not file.filename:
            raise HTTPException(status_code=400, detail="No filename provided")

        # Reject files exceeding the small-file threshold. Trust the
        # Content-Length header here for the early reject; the streaming
        # write below also short-circuits if more bytes arrive than allowed.
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                cl = int(content_length)
                if cl > SMALL_FILE_THRESHOLD:
                    raise HTTPException(
                        status_code=413,
                        detail=(
                            f"File too large for single-shot upload "
                            f"({cl:,} bytes > {SMALL_FILE_THRESHOLD:,}). "
                            f"Use POST /api/files/upload/init for chunked upload."
                        ),
                    )
            except ValueError:
                pass

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
                    # Guard against client sending more bytes than declared
                    if total_bytes > SMALL_FILE_THRESHOLD:
                        raise HTTPException(
                            status_code=413,
                            detail="Upload exceeded small-file threshold mid-stream",
                        )
        except HTTPException:
            if os.path.exists(dest_path):
                os.unlink(dest_path)
            raise
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

    # ═══════════════════════════════════════════════════════════════════
    # SPRINT 3.3: CHUNKED UPLOAD ENDPOINTS
    # ═══════════════════════════════════════════════════════════════════

    # ── POST /api/files/upload/init ───────────────────────────────────
    @router.post("/api/files/upload/init", dependencies=[Depends(verify_auth)])
    async def upload_init(request: Request, body: dict, auth=Depends(verify_auth)):
        """
        Begin a chunked upload session.

        Body: {
            "originalName": "IMG_2024.mp4",
            "totalSize": 1572864000,
            "totalSha256": "abc123...",
            "chunkSize": 52428800,
            "totalChunks": 30,
            "mimeType": "video/mp4",
            "relativePath": "Camera/IMG_2024.mp4"   // optional; defaults to originalName
        }

        Returns: { "uploadId": "...", "expiresAt": "...", "skip": false }
        Or, if a file with this sha256 is already present:
                 { "skip": true, "reason": "deduped", "existingPath": "..." }
        """
        # ── Validate body ────────────────────────────────────────────
        try:
            original_name = str(body["originalName"]).strip()
            total_size = int(body["totalSize"])
            total_sha256 = str(body["totalSha256"]).lower().strip()
            chunk_size = int(body["chunkSize"])
            total_chunks = int(body["totalChunks"])
        except (KeyError, TypeError, ValueError) as e:
            raise HTTPException(status_code=400, detail=f"Invalid init payload: {e}")

        mime_type = body.get("mimeType")
        relative_path_hint = body.get("relativePath") or original_name

        # ── Sanity checks ────────────────────────────────────────────
        if not original_name:
            raise HTTPException(status_code=400, detail="originalName is required")
        if total_size <= 0 or total_size > MAX_TOTAL_FILE_SIZE:
            raise HTTPException(
                status_code=400,
                detail=f"totalSize must be 1..{MAX_TOTAL_FILE_SIZE}",
            )
        if chunk_size <= 0 or chunk_size > CHUNK_HARD_MAX:
            raise HTTPException(
                status_code=400,
                detail=f"chunkSize must be 1..{CHUNK_HARD_MAX}",
            )
        if total_chunks <= 0 or total_chunks > 200_000:
            raise HTTPException(status_code=400, detail="totalChunks out of range")
        # Verify the chunk math is internally consistent
        expected_chunks = (total_size + chunk_size - 1) // chunk_size
        if total_chunks != expected_chunks:
            raise HTTPException(
                status_code=400,
                detail=f"totalChunks={total_chunks} doesn't match ceil(totalSize/chunkSize)={expected_chunks}",
            )
        if not re.fullmatch(r"[0-9a-f]{64}", total_sha256):
            raise HTTPException(status_code=400, detail="totalSha256 must be 64 hex chars")

        # ── Dedup check — if we already have this content, skip ──────
        existing = _db._conn.execute(
            "SELECT relative_path FROM local_files "
            "WHERE sha256 = ? AND sync_status != 'DELETED_LOCAL' LIMIT 1",
            (total_sha256,),
        ).fetchone()
        if existing:
            logger.info(f"⚡ Dedup: client wants to upload sha256={total_sha256[:16]}... "
                        f"already at {existing['relative_path']}")
            return {
                "skip": True,
                "reason": "deduped",
                "existingPath": existing["relative_path"],
            }

        # ── Resolve safe relative path ───────────────────────────────
        safe_rel = _sanitize_relpath(relative_path_hint)

        # If destination already exists with different content, the
        # finalize step will rename. Don't fail init — let the upload
        # proceed and decide at the end.

        # ── Create temp dir and DB row ───────────────────────────────
        upload_id = uuid.uuid4().hex
        temp_dir = os.path.join(_sync_root, UPLOAD_TEMP_DIR_NAME, upload_id)
        try:
            os.makedirs(temp_dir, exist_ok=False)
        except OSError as e:
            raise HTTPException(status_code=500, detail=f"Failed to create temp dir: {e}")

        from datetime import timedelta
        expires_at = (datetime.now(timezone.utc) + timedelta(hours=UPLOAD_TTL_HOURS)).isoformat()

        try:
            _db.create_upload(
                upload_id=upload_id,
                owner_user_id=auth["id"],
                relative_path=safe_rel,
                original_name=original_name,
                total_size=total_size,
                total_sha256=total_sha256,
                chunk_size=chunk_size,
                total_chunks=total_chunks,
                temp_dir=temp_dir,
                expires_at=expires_at,
                mime_type=mime_type,
            )
        except Exception as e:
            shutil.rmtree(temp_dir, ignore_errors=True)
            raise HTTPException(status_code=500, detail=f"Failed to create upload session: {e}")

        logger.info(
            f"📦 Upload init: {original_name} ({total_size:,} bytes, "
            f"{total_chunks} chunks of {chunk_size:,}) → upload_id={upload_id}"
        )
        return {
            "uploadId": upload_id,
            "expiresAt": expires_at,
            "skip": False,
        }

    # ── PUT /api/files/upload/{upload_id}/chunk/{chunk_index} ─────────
    @router.put(
        "/api/files/upload/{upload_id}/chunk/{chunk_index}",
        dependencies=[Depends(verify_auth)],
    )
    async def upload_chunk(
        upload_id: str,
        chunk_index: int,
        request: Request,
        auth=Depends(verify_auth),
    ):
        """
        Upload a single chunk. Body is raw bytes (not multipart).
        Required header: X-Chunk-Sha256 (lowercase hex of this chunk).
        """
        # ── Look up the session ──────────────────────────────────────
        session = _db.get_upload(upload_id)
        if not session:
            raise HTTPException(status_code=404, detail="Upload session not found or expired")

        # ── Owner check (defense in depth — verify_auth already gated
        #    on master-owner; this checks the per-session owner too) ──
        if session["owner_user_id"] != auth["id"]:
            logger.warning(
                f"🚨 Chunk access attempt: upload_id={upload_id} "
                f"session owner={session['owner_user_id']} != caller={auth['id']}"
            )
            raise HTTPException(status_code=403, detail="Access denied")

        # ── Validate chunk_index ─────────────────────────────────────
        if chunk_index < 0 or chunk_index >= session["total_chunks"]:
            raise HTTPException(
                status_code=400,
                detail=f"chunk_index must be 0..{session['total_chunks'] - 1}",
            )

        # ── Validate hash header ─────────────────────────────────────
        expected_chunk_sha = request.headers.get("x-chunk-sha256", "").lower().strip()
        if not re.fullmatch(r"[0-9a-f]{64}", expected_chunk_sha):
            raise HTTPException(
                status_code=400,
                detail="X-Chunk-Sha256 header missing or malformed",
            )

        # ── Compute expected size for this chunk ─────────────────────
        # All chunks are session.chunk_size except possibly the last.
        is_last = (chunk_index == session["total_chunks"] - 1)
        if is_last:
            expected_size = session["total_size"] - (chunk_index * session["chunk_size"])
        else:
            expected_size = session["chunk_size"]

        # ── Stream-write the chunk to disk while hashing ─────────────
        chunk_path = os.path.join(session["temp_dir"], f"chunk_{chunk_index:06d}")
        chunk_path_tmp = chunk_path + ".tmp"
        sha = hashlib.sha256()
        bytes_received = 0

        try:
            with open(chunk_path_tmp, "wb") as f:
                async for piece in request.stream():
                    if not piece:
                        continue
                    bytes_received += len(piece)
                    if bytes_received > expected_size:
                        raise HTTPException(
                            status_code=400,
                            detail=f"Chunk {chunk_index} oversize: got >{expected_size} bytes",
                        )
                    f.write(piece)
                    sha.update(piece)
        except HTTPException:
            if os.path.exists(chunk_path_tmp):
                os.unlink(chunk_path_tmp)
            raise
        except Exception as e:
            if os.path.exists(chunk_path_tmp):
                os.unlink(chunk_path_tmp)
            raise HTTPException(status_code=500, detail=f"Chunk write failed: {e}")

        # ── Verify size and hash ─────────────────────────────────────
        if bytes_received != expected_size:
            os.unlink(chunk_path_tmp)
            raise HTTPException(
                status_code=400,
                detail=f"Chunk {chunk_index} size mismatch: expected {expected_size}, got {bytes_received}",
            )

        actual_sha = sha.hexdigest()
        if actual_sha != expected_chunk_sha:
            os.unlink(chunk_path_tmp)
            raise HTTPException(
                status_code=400,
                detail=f"Chunk {chunk_index} hash mismatch",
            )

        # ── Atomic rename + DB update ────────────────────────────────
        os.replace(chunk_path_tmp, chunk_path)
        _db.mark_chunk_received(upload_id, chunk_index)

        # Re-read session to compute remaining chunks
        session = _db.get_upload(upload_id)
        remaining = _db.get_missing_chunks(session["received_mask"], session["total_chunks"])

        return {
            "received": chunk_index,
            "remaining": remaining,
            "complete": len(remaining) == 0,
        }

    # ── POST /api/files/upload/{upload_id}/finalize ───────────────────
    @router.post(
        "/api/files/upload/{upload_id}/finalize",
        dependencies=[Depends(verify_auth)],
    )
    async def upload_finalize(upload_id: str, auth=Depends(verify_auth)):
        """
        Concatenate all chunks, verify whole-file hash, atomic-move into
        sync root, update local_files, delete temp dir.
        """
        session = _db.get_upload(upload_id)
        if not session:
            raise HTTPException(status_code=404, detail="Upload session not found or expired")
        if session["owner_user_id"] != auth["id"]:
            raise HTTPException(status_code=403, detail="Access denied")

        missing = _db.get_missing_chunks(session["received_mask"], session["total_chunks"])
        if missing:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "Cannot finalize: chunks missing",
                    "remaining": missing,
                },
            )

        # ── Resolve final destination, handling collisions ───────────
        safe_rel = session["relative_path"]
        dest_path = os.path.join(_sync_root, safe_rel)
        if os.path.exists(dest_path):
            base, ext = os.path.splitext(safe_rel)
            ts = int(time.time())
            safe_rel = f"{base}_{ts}{ext}"
            dest_path = os.path.join(_sync_root, safe_rel)

        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        # ── Concatenate + hash + write atomically ────────────────────
        # Write to .partial then os.replace into final name. Crash mid-write
        # leaves a .partial that the cleanup task removes; never leaves
        # a half-formed file at the real path.
        partial_path = dest_path + ".partial"
        sha = hashlib.sha256()
        total_written = 0

        try:
            with open(partial_path, "wb") as out:
                for i in range(session["total_chunks"]):
                    chunk_path = os.path.join(session["temp_dir"], f"chunk_{i:06d}")
                    with open(chunk_path, "rb") as cf:
                        while True:
                            buf = cf.read(1024 * 1024)  # 1 MiB read buffer
                            if not buf:
                                break
                            out.write(buf)
                            sha.update(buf)
                            total_written += len(buf)
        except Exception as e:
            if os.path.exists(partial_path):
                os.unlink(partial_path)
            raise HTTPException(status_code=500, detail=f"Reassembly failed: {e}")

        # ── Verify size and whole-file hash ──────────────────────────
        if total_written != session["total_size"]:
            os.unlink(partial_path)
            raise HTTPException(
                status_code=500,
                detail=f"Reassembled size mismatch: expected {session['total_size']}, got {total_written}",
            )

        actual_sha = sha.hexdigest()
        if actual_sha != session["total_sha256"]:
            os.unlink(partial_path)
            raise HTTPException(
                status_code=400,
                detail=(
                    f"Whole-file hash mismatch: "
                    f"expected {session['total_sha256'][:16]}..., got {actual_sha[:16]}..."
                ),
            )

        # ── Atomic rename into final path ────────────────────────────
        os.replace(partial_path, dest_path)

        # ── Index in local_files, clean up temp ──────────────────────
        stat = os.stat(dest_path)
        _db.upsert_local_file(
            relative_path=safe_rel,
            sha256=actual_sha,
            size=total_written,
            mtime_ns=stat.st_mtime_ns,
            sync_status="SYNCED",
        )
        _db.delete_upload(upload_id)
        shutil.rmtree(session["temp_dir"], ignore_errors=True)

        if _scanner:
            try:
                _scanner.scan()
            except Exception:
                pass

        logger.info(
            f"📥 Chunked upload finalized: {safe_rel} "
            f"({total_written:,} bytes, sha256={actual_sha[:16]}...)"
        )

        # Response shape matches the single-shot /upload response so
        # callers can handle both uniformly.
        return JSONResponse(
            status_code=201,
            content={
                "message": "File uploaded successfully",
                "file": {
                    "id": safe_rel,
                    "originalName": session["original_name"],
                    "fileName": safe_rel,
                    "fileSize": total_written,
                    "mimeType": session["mime_type"] or _guess_mime(safe_rel),
                    "checksum": actual_sha,
                    "uploadStatus": "completed",
                    "backupStatus": "SYNCED",
                    "isEncrypted": False,
                    "uploadedAt": datetime.now(timezone.utc).isoformat(),
                },
            },
        )

    # ── DELETE /api/files/upload/{upload_id} ─────────────────────────
    # Lets the client abort a session voluntarily (e.g., user cancelled).
    @router.delete(
        "/api/files/upload/{upload_id}",
        dependencies=[Depends(verify_auth)],
    )
    async def upload_abort(upload_id: str, auth=Depends(verify_auth)):
        session = _db.get_upload(upload_id)
        if not session:
            return {"aborted": False, "reason": "not_found"}
        if session["owner_user_id"] != auth["id"]:
            raise HTTPException(status_code=403, detail="Access denied")

        shutil.rmtree(session["temp_dir"], ignore_errors=True)
        _db.delete_upload(upload_id)
        logger.info(f"🗑️ Upload aborted: {upload_id}")
        return {"aborted": True}

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

    # ═══════════════════════════════════════════════════════════════════
    # SPRINT 3.5: DASHBOARD
    #
    # All dashboard endpoints are gated by verify_localhost — they reject
    # any request not originating from 127.0.0.1 / ::1. The browser-based
    # dashboard runs on the same machine as the master, so it always hits
    # the loopback. Cloudflare Tunnel does NOT route requests to /dashboard
    # because it's path-routed via FastAPI, and the localhost gate is the
    # secondary defense if someone tries.
    # ═══════════════════════════════════════════════════════════════════

    async def verify_localhost(request: Request):
        """
        Reject any request not originating from loopback. The dashboard
        is intentionally local-only; remote dashboard access would expand
        the threat model significantly and is deferred to a later sprint.
        """
        client = request.client.host if request.client else ""
        # IPv4 loopback, IPv6 loopback, and IPv4-mapped IPv6 loopback
        allowed = {"127.0.0.1", "::1", "::ffff:127.0.0.1"}
        if client not in allowed:
            logger.warning(f"🚫 Non-localhost dashboard access attempt from {client}")
            raise HTTPException(status_code=403, detail="Dashboard is local-only")

    # ── GET /dashboard/  (index.html) ─────────────────────────────────
    @router.get("/dashboard/", dependencies=[Depends(verify_localhost)])
    async def dashboard_index():
        path = os.path.join(_dashboard_static_dir, "index.html")
        if not os.path.isfile(path):
            raise HTTPException(status_code=500, detail="Dashboard files not bundled")
        return FileResponse(path, media_type="text/html")

    # ── GET /dashboard/static/{file}  ─────────────────────────────────
    @router.get("/dashboard/static/{filename}", dependencies=[Depends(verify_localhost)])
    async def dashboard_static(filename: str):
        # Guard against path traversal in static asset names
        if "/" in filename or "\\" in filename or filename.startswith("."):
            raise HTTPException(status_code=400, detail="Invalid asset path")
        path = os.path.join(_dashboard_static_dir, filename)
        if not os.path.isfile(path):
            raise HTTPException(status_code=404, detail="Asset not found")
        ext = os.path.splitext(filename)[1].lower()
        media_types = {
            ".css": "text/css",
            ".js": "application/javascript",
            ".html": "text/html",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
        }
        return FileResponse(path, media_type=media_types.get(ext, "application/octet-stream"))

    # ── GET /api/dashboard/status  ────────────────────────────────────
    @router.get("/api/dashboard/status", dependencies=[Depends(verify_localhost)])
    async def dashboard_status():
        disk = shutil.disk_usage(_sync_root)
        stats = _db.get_stats() if _db else {}
        tunnel_url = None
        try:
            if _tunnel_getter is not None:
                tunnel_url = _tunnel_getter()
        except Exception:
            pass

        last_hb_age = (time.time() - _last_heartbeat_at) if _last_heartbeat_at else None
        return {
            "appVersion": _app_version,
            "ownerUserId": _owner_user_id or None,
            "deviceName": platform.node(),
            "osPlatform": sys.platform,
            "syncRoot": _sync_root,
            "tunnelUrl": tunnel_url,
            "tunnelOnline": tunnel_url is not None,
            "lastHeartbeatSecondsAgo": int(last_hb_age) if last_hb_age else None,
            "diskFreeBytes": disk.free,
            "diskTotalBytes": disk.total,
            "diskUsedBytes": disk.used,
            "stats": {
                "totalFiles": stats.get("total_files", 0),
                "syncedFiles": stats.get("synced", 0),
                "pendingUpload": stats.get("pending_upload", 0),
                "totalSizeBytes": stats.get("total_size_bytes", 0),
            },
            "controls": {
                "pauseAvailable": _dashboard_callbacks.get("pause") is not None,
                "resumeAvailable": _dashboard_callbacks.get("resume") is not None,
                "scanAvailable": _dashboard_callbacks.get("scan") is not None,
                "switchAccountAvailable": _dashboard_callbacks.get("switch_account") is not None,
            },
        }

    # ── GET /api/dashboard/files  ─────────────────────────────────────
    @router.get("/api/dashboard/files", dependencies=[Depends(verify_localhost)])
    async def dashboard_files(limit: int = 50, offset: int = 0, status: Optional[str] = None):
        # Bound the limit so a buggy frontend can't request a million rows
        limit = max(1, min(int(limit), 500))
        offset = max(0, int(offset))

        where = ""
        params: list = []
        if status:
            where = "WHERE sync_status = ?"
            params.append(status.upper())

        rows = _db._conn.execute(
            f"""
            SELECT relative_path, sha256, size, mtime_ns, server_file_id,
                   sync_status, last_synced_at, created_at
            FROM local_files
            {where}
            ORDER BY COALESCE(last_synced_at, created_at) DESC
            LIMIT ? OFFSET ?
            """,
            (*params, limit, offset),
        ).fetchall()

        total_row = _db._conn.execute(
            f"SELECT COUNT(*) AS c FROM local_files {where}", tuple(params)
        ).fetchone()
        total = total_row["c"] if total_row else 0

        return {
            "files": [
                {
                    "relativePath": r["relative_path"],
                    "sha256": r["sha256"][:16] + "…" if r["sha256"] else None,
                    "size": r["size"],
                    "mtimeNs": r["mtime_ns"],
                    "serverFileId": r["server_file_id"],
                    "syncStatus": r["sync_status"],
                    "lastSyncedAt": r["last_synced_at"],
                    "createdAt": r["created_at"],
                }
                for r in rows
            ],
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    # ── GET /api/dashboard/recent  ────────────────────────────────────
    @router.get("/api/dashboard/recent", dependencies=[Depends(verify_localhost)])
    async def dashboard_recent():
        rows = _db._conn.execute(
            """
            SELECT relative_path, size, sync_status, last_synced_at, created_at
            FROM local_files
            WHERE sync_status = 'SYNCED' AND last_synced_at IS NOT NULL
            ORDER BY last_synced_at DESC
            LIMIT 50
            """
        ).fetchall()
        return {
            "items": [
                {
                    "relativePath": r["relative_path"],
                    "size": r["size"],
                    "lastSyncedAt": r["last_synced_at"],
                }
                for r in rows
            ]
        }

    # ── GET /api/dashboard/uploads  ───────────────────────────────────
    @router.get("/api/dashboard/uploads", dependencies=[Depends(verify_localhost)])
    async def dashboard_uploads():
        rows = _db._conn.execute(
            """
            SELECT upload_id, original_name, total_size, total_chunks,
                   chunk_size, received_mask, created_at, expires_at, last_chunk_at
            FROM uploads_in_progress
            ORDER BY created_at DESC
            """
        ).fetchall()

        out = []
        for r in rows:
            mask = r["received_mask"] or b""
            received = []
            for i in range(r["total_chunks"]):
                byte_idx = i // 8
                bit_idx = i % 8
                if byte_idx < len(mask) and (mask[byte_idx] & (1 << bit_idx)):
                    received.append(i)
            out.append({
                "uploadId": r["upload_id"],
                "originalName": r["original_name"],
                "totalSize": r["total_size"],
                "totalChunks": r["total_chunks"],
                "chunkSize": r["chunk_size"],
                "chunksReceived": len(received),
                "receivedIndices": received,
                "createdAt": r["created_at"],
                "expiresAt": r["expires_at"],
                "lastChunkAt": r["last_chunk_at"],
            })
        return {"uploads": out}

    # ── POST /api/dashboard/scan  ─────────────────────────────────────
    @router.post("/api/dashboard/scan", dependencies=[Depends(verify_localhost)])
    async def dashboard_scan():
        cb = _dashboard_callbacks.get("scan")
        if cb is None:
            raise HTTPException(status_code=503, detail="Scan callback not registered")
        try:
            cb()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Scan failed: {e}")
        return {"ok": True, "message": "Scan triggered"}

    # ── POST /api/dashboard/pause  ────────────────────────────────────
    @router.post("/api/dashboard/pause", dependencies=[Depends(verify_localhost)])
    async def dashboard_pause():
        cb = _dashboard_callbacks.get("pause")
        if cb is None:
            raise HTTPException(status_code=503, detail="Pause callback not registered")
        try:
            cb()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Pause failed: {e}")
        return {"ok": True, "message": "Sync paused"}

    # ── POST /api/dashboard/resume  ───────────────────────────────────
    @router.post("/api/dashboard/resume", dependencies=[Depends(verify_localhost)])
    async def dashboard_resume():
        cb = _dashboard_callbacks.get("resume")
        if cb is None:
            raise HTTPException(status_code=503, detail="Resume callback not registered")
        try:
            cb()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Resume failed: {e}")
        return {"ok": True, "message": "Sync resumed"}

    # ── POST /api/dashboard/switch-account  ───────────────────────────
    # Accepts a destructive action: clears owner binding + JWT cache so
    # the next launch runs OAuth fresh. Doesn't touch user files in the
    # sync folder. Caller is expected to display a confirmation modal.
    @router.post("/api/dashboard/switch-account", dependencies=[Depends(verify_localhost)])
    async def dashboard_switch_account(request: Request):
        body = {}
        try:
            body = await request.json()
        except Exception:
            pass
        if not body.get("confirmed"):
            raise HTTPException(
                status_code=400,
                detail="Must include {'confirmed': true} to switch account",
            )

        cb = _dashboard_callbacks.get("switch_account")
        if cb is None:
            raise HTTPException(status_code=503, detail="Switch-account not available")
        try:
            cb()
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Switch failed: {e}")
        return {
            "ok": True,
            "message": "Owner binding cleared. Restart RGBC Drive to sign in with a different account.",
        }

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


# Sprint 3.3: like _sanitize_filename but explicitly preserves directory
# structure (e.g., "Camera/IMG_2024.mp4"). Reject anything that would
# escape the sync root or contain control characters.
def _sanitize_relpath(rel: str) -> str:
    if not rel:
        return f"upload_{uuid.uuid4().hex[:8]}"
    rel = rel.replace("\x00", "")
    rel = rel.replace("\\", "/")
    rel = rel.lstrip("/")
    # Reject control chars across the whole path
    if any(ord(c) < 32 for c in rel):
        raise HTTPException(status_code=400, detail="relativePath contains control characters")
    parts = []
    for p in rel.split("/"):
        p = p.strip()
        if not p or p == "." or p == "..":
            continue
        # On Windows, reserved device names; cheap to reject everywhere
        if p.upper() in {"CON", "PRN", "AUX", "NUL"} or re.fullmatch(r"COM[1-9]|LPT[1-9]", p.upper()):
            raise HTTPException(status_code=400, detail=f"Reserved filename component: {p}")
        # Reject characters illegal on Windows filesystems
        if re.search(r'[<>:"|?*]', p):
            raise HTTPException(status_code=400, detail=f"Invalid filename character in: {p}")
        parts.append(p)
    if not parts:
        return f"upload_{uuid.uuid4().hex[:8]}"
    # Length cap so a path bomb can't blow out the OS limit
    out = "/".join(parts)
    if len(out) > 500:
        raise HTTPException(status_code=400, detail="relativePath too long (max 500 chars)")
    return out


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
    Sprint 3.3:  Spawns a background cleanup task for expired chunked uploads.

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

    # Sprint 3.3: cleanup task for orphaned chunked-upload temp dirs.
    # Runs every 30 minutes, deletes any upload session past its TTL.
    def _cleanup_loop():
        import time as _time
        while True:
            _time.sleep(30 * 60)
            try:
                for row in db.get_expired_uploads():
                    try:
                        shutil.rmtree(row["temp_dir"], ignore_errors=True)
                        db.delete_upload(row["upload_id"])
                        logger.info(f"🧹 Cleaned expired upload: {row['upload_id']} "
                                    f"({row['original_name']})")
                    except Exception as e:
                        logger.warning(f"Cleanup error for {row['upload_id']}: {e}")
            except Exception as e:
                logger.warning(f"Upload cleanup loop error: {e}")

    threading.Thread(target=_cleanup_loop, daemon=True, name="UploadCleanup").start()

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