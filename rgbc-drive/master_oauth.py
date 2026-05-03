"""
RGBC Drive — Master Node Google OAuth

Sprint 3: Replaces the M2M_API_KEY with a proper Google OAuth flow.
The Master now authenticates as a real user (the admin) and receives
an RS256 JWT from the API Gateway — identical to the Android flow.

Flow:
  1. Check for cached credentials in .rgbc_oauth_cache.json
  2. If expired/missing → open browser for Google Sign-In
  3. Extract the id_token from the Google credential
  4. POST id_token to Gateway's /api/auth/google-signin
  5. Gateway validates, returns RS256 JWT (30-day TTL)
  6. Cache the JWT locally for subsequent runs
  7. Master uses this JWT for register/heartbeat/all API calls

Requirements:
  pip install google-auth google-auth-oauthlib

Setup:
  1. Go to Google Cloud Console → APIs & Services → Credentials
  2. Create an OAuth 2.0 Client ID of type "Desktop application"
  3. Download the JSON and save it as client_secret.json next to rgbc_drive.py
  4. The GOOGLE_CLIENT_ID in the Gateway .env must match the Web client ID
     (Google issues both Desktop + Web IDs — the id_token audience must match)
"""

import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import Optional

import requests
import jwt as pyjwt  # Sprint 3.1: extract userId from JWT for owner-binding

os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

logger = logging.getLogger("RGBCDrive.OAuth")


class MasterOAuth:
    """
    Manages Google OAuth authentication for the Master Node.
    Caches the Gateway JWT locally so the browser flow only runs once
    (or when the 30-day token expires).
    """

    SCOPES = ['openid', 'email', 'profile']

    def __init__(
        self,
        gateway_url: str,
        client_secret_path: str = "client_secret.json",
        cache_path: str = ".rgbc_oauth_cache.json",
    ):
        self.gateway_url = gateway_url.rstrip("/")
        self.client_secret_path = client_secret_path
        self.cache_path = cache_path
        self._jwt: Optional[str] = None
        self._jwt_expires_at: float = 0
        self._email: Optional[str] = None
        self._user_id: Optional[str] = None  # Sprint 3.1

    @property
    def jwt(self) -> Optional[str]:
        """Get a valid JWT, refreshing if necessary."""
        if self._jwt and time.time() < self._jwt_expires_at:
            return self._jwt

        # Try loading from cache
        cached = self._load_cache()
        if cached:
            return self._jwt

        # Need fresh login
        logger.info("No valid JWT — initiating Google OAuth flow...")
        return self._do_oauth_flow()

    @property
    def email(self) -> Optional[str]:
        """The authenticated user's email."""
        if not self._email:
            self.jwt  # Triggers load/refresh
        return self._email
    @property
    def user_id(self) -> Optional[str]:
        """
        The authenticated user's canonical Gateway userId (from JWT 'userId' claim).
        Required by rgbc_drive.py for owner binding.
        """
        if not self._user_id:
            self.jwt  # Triggers load/refresh
        return self._user_id

    def get_auth_headers(self) -> dict:
        """Returns headers dict with Bearer token for API calls."""
        token = self.jwt
        if token:
            return {"Authorization": f"Bearer {token}"}
        return {}

    # ── OAuth Browser Flow ───────────────────────────────────────────

    def _do_oauth_flow(self) -> Optional[str]:
        """Run the full Google OAuth → Gateway JWT exchange."""
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError:
            logger.error(
                "google-auth-oauthlib not installed.\n"
                "  Run: pip install google-auth google-auth-oauthlib"
            )
            return None

        if not os.path.isfile(self.client_secret_path):
            logger.error(
                f"OAuth client_secret.json not found at: {self.client_secret_path}\n"
                "  Download it from Google Cloud Console → APIs & Services → Credentials\n"
                "  → OAuth 2.0 Client ID (Desktop application) → Download JSON"
            )
            return None

        try:
            # Step 1: Browser-based Google login
            flow = InstalledAppFlow.from_client_secrets_file(
                self.client_secret_path,
                scopes=self.SCOPES,
            )
            credentials = flow.run_local_server(
                port=8742,
                prompt='consent',
                success_message=(
                    "RGBC Drive authenticated! You can close this tab.\n"
                    "Return to the terminal to continue setup."
                ),
            )

            if not credentials or not credentials.id_token:
                logger.error("Google OAuth flow completed but no id_token received.")
                return None

            id_token = credentials.id_token
            logger.info("✅ Google id_token obtained")

            # Step 2: Exchange id_token for Gateway JWT
            return self._exchange_for_jwt(id_token)

        except Exception as e:
            logger.error(f"OAuth flow failed: {e}")
            return None

    def _exchange_for_jwt(self, id_token: str) -> Optional[str]:
        """Send Google id_token to Gateway, receive RS256 JWT."""
        try:
            import platform
            resp = requests.post(
                f"{self.gateway_url}/api/auth/google-signin",
                json={
                    "idToken": id_token,
                    "deviceId": self._get_device_id(),
                    "deviceName": platform.node(),
                    "deviceType": "DESKTOP",
                },
                timeout=(10, 30),
            )

            if resp.status_code == 200:
                data = resp.json()

                # Sprint 3.1: Gateway response shape is nested per OAuth 2.0
                # convention: { user: {...}, tokens: { accessToken, refreshToken,
                # expiresIn } }. Reading top-level "accessToken" silently
                # produced None and corrupted the cache.
                tokens = data.get("tokens") or {}
                jwt_token = tokens.get("accessToken")
                user_obj = data.get("user") or {}
                email = user_obj.get("email")

                # Hard-validate before assigning. If the gateway's response
                # shape changes again, fail loudly here instead of caching
                # garbage and lying about success.
                if not isinstance(jwt_token, str) or not jwt_token:
                    logger.error(
                        f"Gateway returned 200 but no accessToken in response. "
                        f"Top-level keys: {list(data.keys())}; "
                        f"tokens keys: {list(tokens.keys()) if isinstance(tokens, dict) else type(tokens).__name__}"
                    )
                    return None
                if not email:
                    logger.warning("Gateway response missing user.email — continuing")

                # expiresIn may be int (correct) or str (legacy). Coerce.
                raw_expires = tokens.get("expiresIn", 30 * 24 * 3600)
                try:
                    expires_in = int(raw_expires)
                except (TypeError, ValueError):
                    logger.warning(
                        f"Gateway returned non-numeric expiresIn={raw_expires!r}; "
                        "defaulting to 30 days"
                    )
                    expires_in = 30 * 24 * 3600

                self._jwt = jwt_token
                self._email = email
                self._jwt_expires_at = time.time() + expires_in - 3600  # 1h safety margin

                # Sprint 3.1: Extract userId for owner binding. No signature
                # verification needed here — TLS to api.bagariaa.in already
                # established trust; master_api.verify_auth() does the
                # cryptographic check on every inbound request.
                try:
                    claims = pyjwt.decode(
                        self._jwt,
                        options={"verify_signature": False},
                    )
                    self._user_id = str(
                        claims.get("userId") or claims.get("sub") or ""
                    ).strip() or None
                    if not self._user_id:
                        logger.error(
                            "JWT decoded but contained no userId/sub claim. "
                            "Gateway must include userId in the access token."
                        )
                        return None
                except Exception as e:
                    logger.error(f"Could not extract userId from JWT: {e}")
                    self._user_id = None
                    return None

                self._save_cache()

                logger.info(
                    f"✅ Gateway JWT obtained for {self._email} "
                    f"(RS256, expires in {expires_in // 86400}d, userId={self._user_id})"
                )
                return self._jwt

            elif resp.status_code == 403:
                logger.error(
                    "🚫 Access denied by Gateway. Check the gateway logs — "
                    "your Google account may not be permitted."
                )
                return None
            else:
                logger.error(f"Gateway JWT exchange failed: {resp.status_code} — {resp.text[:200]}")
                return None

        except Exception as e:
            logger.error(f"Gateway JWT exchange error: {e}")
            return None

    # ── Cache Management ─────────────────────────────────────────────

    def _save_cache(self):
        """Cache JWT + metadata to disk."""
        try:
            cache_data = {
                "jwt": self._jwt,
                "email": self._email,
                "user_id": self._user_id,
                "expires_at": self._jwt_expires_at,
                "saved_at": time.time(),
            }
            with open(self.cache_path, 'w') as f:
                json.dump(cache_data, f)
            logger.debug("JWT cached to disk")
        except Exception as e:
            logger.warning(f"Failed to cache JWT: {e}")

    def _load_cache(self) -> bool:
        """Load cached JWT if valid."""
        try:
            if not os.path.isfile(self.cache_path):
                return False

            with open(self.cache_path, 'r') as f:
                cache_data = json.load(f)

            # Sprint 3.1: Reject corrupt/incomplete cache entries (e.g. one
            # written by an earlier broken exchange where jwt ended up null).
            cached_jwt = cache_data.get("jwt")
            if not isinstance(cached_jwt, str) or not cached_jwt:
                logger.info("Cache entry has no usable JWT — discarding and re-authenticating")
                try:
                    os.unlink(self.cache_path)
                except OSError:
                    pass
                return False

            expires_at = cache_data.get("expires_at", 0)
            if time.time() >= expires_at:
                logger.info("Cached JWT expired, need re-authentication")
                return False

            self._jwt = cached_jwt
            self._email = cache_data.get("email")
            self._user_id = cache_data.get("user_id")
            self._jwt_expires_at = expires_at

            logger.info(f"✅ Loaded cached JWT for {self._email}")
            return True

        except Exception as e:
            logger.warning(f"Cache load failed: {e}")
            return False

    def _get_device_id(self) -> str:
        """Read or generate device ID (reuse from rgbc_drive.py pattern)."""
        import platform
        import uuid
        sync_root = os.getenv("SYNC_ROOT", os.path.join(os.path.expanduser("~"), "RGBC_Drive"))
        id_file = Path(sync_root) / ".rgbc_device_id"
        if id_file.exists():
            return id_file.read_text().strip()
        new_id = f"{platform.node()}_{uuid.uuid4().hex[:12]}"
        os.makedirs(sync_root, exist_ok=True)
        id_file.write_text(new_id)
        return new_id

    def invalidate(self):
        """Force re-authentication on next access."""
        self._jwt = None
        self._jwt_expires_at = 0
        self._user_id = None  # Sprint 3.1
        self._email = None
        try:
            if os.path.isfile(self.cache_path):
                os.unlink(self.cache_path)
        except Exception:
            pass
        logger.info("OAuth cache cleared — will re-authenticate on next request")