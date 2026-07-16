"""
RGBC Drive — Master Node Google OAuth (Sprint 3.5b: PKCE)

Sprint 3.5b: Eliminated the user-supplied client_secret.json. The
Desktop OAuth credentials are now resolved from oauth_config.py — a
gitignored module compiled into the .exe at build time. End users
download an installer, click "Sign in with Google", and are done. They
never see a credential.

What changed and why:
  - No client_secret.json anywhere. Nothing to download, nothing to
    place next to the .exe, no Google Cloud Console for end users.
  - Credentials are baked into the build, not the source tree and not
    the user's .env. See oauth_config.example.py for the full rationale.
  - PKCE (RFC 7636) protects the authorization code in transit. Note
    that Google's /token endpoint STILL requires client_secret for
    Desktop clients — PKCE is defence-in-depth here, not a replacement.
  - The Gateway accepts id_tokens whose audience matches either the
    Android or Desktop client_id (VALID_CLIENT_IDS in googleAuth.js).

Threat model, honestly stated:
  Anyone holding the .exe can extract the client_secret. Google's
  installed-app guidance says this is expected and acceptable — the
  secret "is obviously not treated as a secret" for native apps. The
  blast radius of extraction is that an attacker can build an app whose
  consent screen carries RGBC branding. They cannot mint user tokens
  without that user completing Google's consent flow, and they cannot
  bypass the Gateway's audience check or the master's owner binding.
  Sprint 5 (gateway-mediated OAuth) closes even this gap.

Flow:
  1. Check for cached credentials in .rgbc_oauth_cache.json
  2. If expired/missing → open browser for Google Sign-In (PKCE)
  3. Extract the id_token from the Google credential
  4. POST id_token to Gateway's /api/auth/google-signin
  5. Gateway validates the audience, returns RS256 JWT (30-day TTL)
  6. Cache the JWT locally for subsequent runs
  7. Master uses this JWT for register/heartbeat/all API calls

Requirements:
  pip install google-auth google-auth-oauthlib

Setup (once per build machine, by the developer):
  copy oauth_config.example.py oauth_config.py
  Fill in the two values. Confirm oauth_config.py is in .gitignore.

Setup (by end users):
  Nothing.
"""

import json
import logging
import os
import time
from pathlib import Path
from typing import Optional

import requests
import jwt as pyjwt

os.environ['OAUTHLIB_RELAX_TOKEN_SCOPE'] = '1'

logger = logging.getLogger("RGBCDrive.OAuth")

# ═══════════════════════════════════════════════════════════════════════
# Desktop OAuth credential resolution (Sprint 3.5b)
#
# The client_id and client_secret are NOT hardcoded here — GitHub's push
# protection blocks Google OAuth secret patterns, and a credential
# committed to a public repo is leaked permanently. They live in
# oauth_config.py, which is gitignored and bundled into the .exe by
# PyInstaller (Analysis follows the import below).
#
# Resolution order:
#   1. oauth_config.py     — production. Present on the build machine,
#                            compiled into the bundle, absent from git.
#   2. Environment vars    — fallback for a fresh clone with no
#                            oauth_config.py yet, or CI. NOT a shipping
#                            path: end users never have these set, because
#                            setup_wizard.py's .env has user settings only.
#
# On the security posture: Google's installed-app guidance states the
# desktop client_secret is "obviously not treated as a secret" — it ships
# inside every copy of the binary. PKCE, user consent, and the Gateway's
# audience check (VALID_CLIENT_IDS in googleAuth.js) are what provide
# actual security. Keeping it out of git isn't about hiding it from
# someone holding the .exe; it's about not handing it to every scanner
# that indexes public repos. Different exposure class, free to avoid.
#
# Rotation: add a new secret in Google Cloud Console, update
# oauth_config.py, rebuild. If the client_id changes too, the Gateway's
# .env GOOGLE_DESKTOP_CLIENT_ID must be updated in lockstep or desktop
# sign-in returns 401.
#
# Sprint 5 removes this entirely — gateway-mediated OAuth keeps the
# secret server-side and the desktop holds no credential at all.
# ═══════════════════════════════════════════════════════════════════════

_PLACEHOLDER_PREFIX = "PASTE_"


def _resolve_oauth_credentials() -> tuple:
    """
    Returns (client_id, client_secret, source_label).
    Empty strings + source 'none' if nothing usable was found.

    Resolved lazily (from MasterOAuth.__init__, not at module import) so
    the result is never frozen before load_dotenv() has run.
    """
    # ── 1. Bundled build config (production path) ────────────────────
    try:
        import oauth_config  # gitignored; bundled by PyInstaller
        cid = str(getattr(oauth_config, "GOOGLE_DESKTOP_CLIENT_ID", "") or "").strip()
        csec = str(getattr(oauth_config, "GOOGLE_DESKTOP_CLIENT_SECRET", "") or "").strip()
        # Guard against someone copying the .example file without editing it
        if cid and csec and not cid.startswith(_PLACEHOLDER_PREFIX) \
                and not csec.startswith(_PLACEHOLDER_PREFIX):
            return cid, csec, "oauth_config.py"
        if cid.startswith(_PLACEHOLDER_PREFIX) or csec.startswith(_PLACEHOLDER_PREFIX):
            logger.warning(
                "oauth_config.py still contains template placeholders — "
                "fill in the real values from Google Cloud Console."
            )
    except ImportError:
        pass

    # ── 2. Environment (fresh clone / CI only — never end users) ─────
    cid = (os.getenv("GOOGLE_DESKTOP_CLIENT_ID") or "").strip()
    csec = (os.getenv("GOOGLE_DESKTOP_CLIENT_SECRET") or "").strip()
    if cid and csec:
        return cid, csec, "environment"

    return "", "", "none"


_MISSING_CREDS_HELP = (
    "Google Desktop OAuth credentials not found.\n"
    "  This build has no oauth_config.py and no GOOGLE_DESKTOP_CLIENT_ID /\n"
    "  GOOGLE_DESKTOP_CLIENT_SECRET in the environment.\n"
    "\n"
    "  If you just cloned the repo:\n"
    "    1. copy oauth_config.example.py oauth_config.py\n"
    "    2. Fill in both values from Google Cloud Console →\n"
    "       APIs & Services → Credentials → 'RGBC Master' (Desktop)\n"
    "    3. Rebuild:  pyinstaller .\\rgbc_drive_1.spec --clean --noconfirm\n"
    "\n"
    "  If you are an end user seeing this: the .exe was built incorrectly.\n"
    "  Please report it — you should never need to configure credentials."
)


class MasterOAuth:
    """
    Manages Google OAuth authentication for the Master Node using PKCE.
    Caches the Gateway JWT locally so the browser flow only runs once
    (or when the 30-day token expires).
    """

    SCOPES = ['openid', 'email', 'profile']

    def __init__(
        self,
        gateway_url: str,
        cache_path: str = ".rgbc_oauth_cache.json",
        client_id: Optional[str] = None,
        client_secret: Optional[str] = None,
    ):
        """
        Sprint 3.5b: client_secret_path REMOVED — no JSON file is read.
        Credentials resolve from oauth_config.py (bundled) or the
        environment. Pass client_id/client_secret explicitly only when
        testing against an alternate OAuth project.
        """
        self.gateway_url = gateway_url.rstrip("/")
        self.cache_path = cache_path

        resolved_id, resolved_secret, source = _resolve_oauth_credentials()
        self.client_id = (client_id or resolved_id or "").strip()
        self.client_secret = (client_secret or resolved_secret or "").strip()
        self._creds_source = "explicit" if (client_id and client_secret) else source

        if self.client_id and self.client_secret:
            logger.debug(f"OAuth credentials loaded from: {self._creds_source}")

        self._jwt: Optional[str] = None
        self._jwt_expires_at: float = 0
        self._email: Optional[str] = None
        self._user_id: Optional[str] = None

    @property
    def jwt(self) -> Optional[str]:
        """Get a valid JWT, refreshing if necessary."""
        if self._jwt and time.time() < self._jwt_expires_at:
            return self._jwt

        # Try loading from cache
        if self._load_cache():
            return self._jwt

        # Need fresh login
        logger.info("No valid JWT — initiating Google OAuth flow (PKCE)...")
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
        The authenticated user's canonical Gateway userId
        (from the JWT 'userId' claim). Required by rgbc_drive.py
        for owner binding.
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

    # ── OAuth Browser Flow (PKCE) ────────────────────────────────────

    def _do_oauth_flow(self) -> Optional[str]:
        """
        Run the full Google OAuth → Gateway JWT exchange.
        Sprint 3.5b: PKCE flow, no client_secret file required.
        """
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError:
            logger.error(
                "google-auth-oauthlib not installed.\n"
                "  Run: pip install google-auth google-auth-oauthlib"
            )
            return None

        if not self.client_id or not self.client_secret:
            logger.error(_MISSING_CREDS_HELP)
            return None

        try:
            # Build a client config dict in-memory. No file involved.
            #
            # Sprint 3.5b note: Google's /token endpoint requires
            # client_secret even for Desktop clients using PKCE — an empty
            # string returns "(invalid_request) client_secret is missing".
            # PKCE is defence-in-depth alongside the secret here, not a
            # replacement for it. Both values come from oauth_config.py.
            client_config = {
                "installed": {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }

            flow = InstalledAppFlow.from_client_config(
                client_config,
                scopes=self.SCOPES,
            )

            credentials = flow.run_local_server(
                port=8742,
                prompt='consent',
                success_message=(
                    "RGBC Drive authenticated! You can close this tab.\n"
                    "Return to RGBC Drive to continue."
                ),
            )

            if not credentials or not credentials.id_token:
                logger.error("Google OAuth flow completed but no id_token received.")
                return None

            id_token = credentials.id_token
            logger.info("✅ Google id_token obtained (PKCE flow)")

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

                # Gateway response shape (Sprint 3.1):
                # { user: {...}, tokens: { accessToken, refreshToken, expiresIn } }
                tokens = data.get("tokens") or {}
                jwt_token = tokens.get("accessToken")
                user_obj = data.get("user") or {}
                email = user_obj.get("email")

                # Hard-validate before assigning so we never cache a None JWT.
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

                # Sprint 3.1: Extract userId for owner binding.
                # No signature verification needed here — TLS to api.bagariaa.in
                # already established trust; master_api.verify_auth() does the
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

            elif resp.status_code == 401:
                logger.error(
                    "🚫 Gateway rejected the Google id_token. Most likely cause: "
                    "the Gateway's GOOGLE_DESKTOP_CLIENT_ID env var doesn't match "
                    f"this build's client_id ({self.client_id[:32]}...)."
                )
                return None
            elif resp.status_code == 403:
                logger.error(
                    "🚫 Access denied by Gateway. Check the gateway logs — "
                    "your Google account may not be permitted."
                )
                return None
            else:
                logger.error(
                    f"Gateway JWT exchange failed: {resp.status_code} — {resp.text[:200]}"
                )
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

            # Reject corrupt/incomplete cache entries.
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
        self._user_id = None
        self._email = None
        try:
            if os.path.isfile(self.cache_path):
                os.unlink(self.cache_path)
        except Exception:
            pass
        logger.info("OAuth cache cleared — will re-authenticate on next request")