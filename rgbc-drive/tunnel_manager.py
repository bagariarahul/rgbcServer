"""
RGBC Drive — Cloudflare Tunnel Manager

Manages the cloudflared process lifecycle. See Sprint 2.3 for full docs.

PATCH (Sprint 2.4): Binary search now prioritizes a bundled bin/ directory
next to the executable BEFORE checking the system PATH. This means we can
ship cloudflared.exe inside bin/ and the user never opens a terminal.

Binary search order:
  1. bin/cloudflared.exe  (bundled — highest priority)
  2. System PATH           (winget/brew install)
  3. Platform-specific dirs (%ProgramFiles%, /opt/homebrew, etc.)
  4. Same directory as script (fallback)
"""

import os
import re
import sys
import logging
import platform
import shutil
import subprocess
import threading
import time
from typing import Optional
from paths import get_cloudflared_dir
logger = logging.getLogger("RGBCDrive.Tunnel")

_URL_PATTERNS = [
    re.compile(r'https://[a-zA-Z0-9._-]+\.trycloudflare\.com'),
    re.compile(r'https://[a-zA-Z0-9._-]+\.[a-zA-Z]{2,}'),
    re.compile(r'(https://[a-zA-Z0-9._-]+\.[a-zA-Z0-9.-]+)'),
]


class TunnelManager:
    def __init__(
        self,
        local_port: int = 8741,
        token: str = "",
        tunnel_url_override: str = "",
    ):
        self.local_port = local_port
        self.token = token
        self.tunnel_url_override = tunnel_url_override.rstrip("/") if tunnel_url_override else ""

        self._url: Optional[str] = None
        self._url_event = threading.Event()
        self._process: Optional[subprocess.Popen] = None
        self._monitor_thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

    def get_url(self, timeout: float = 60) -> Optional[str]:
        if self.tunnel_url_override:
            return self.tunnel_url_override
        if self._url:
            return self._url
        self._url_event.wait(timeout=timeout)
        return self._url

    @property
    def is_running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def start(self) -> None:
        if self.tunnel_url_override:
            self._url = self.tunnel_url_override
            self._url_event.set()
            logger.info(f"🔗 Using pre-configured tunnel URL: {self._url}")
            return

        binary = self._find_binary()
        if not binary:
            logger.error(
                "❌ cloudflared binary not found.\n"
                "   Place it in the bin/ folder next to this executable,\n"
                "   or install it:\n"
                "     Windows: winget install cloudflare.cloudflared\n"
                "     macOS:   brew install cloudflared\n"
                "     Linux:   sudo apt install cloudflared\n"
                "\n"
                "   Or set TUNNEL_URL in .env to skip auto-start."
            )
            return

        logger.info(f"Found cloudflared: {binary}")

        self._stop_event.clear()
        self._monitor_thread = threading.Thread(
            target=self._monitor_loop, args=(binary,),
            daemon=True, name="TunnelMonitor",
        )
        self._monitor_thread.start()

    def stop(self) -> None:
        if self.tunnel_url_override:
            return
        self._stop_event.set()
        self._kill_process()
        if self._monitor_thread:
            self._monitor_thread.join(timeout=10)
        logger.info("🔗 Tunnel stopped")

    # ── Process lifecycle ────────────────────────────────────────────

    def _monitor_loop(self, binary: str) -> None:
        restart_count = 0
        max_restarts = 10
        backoff = 5

        while not self._stop_event.is_set() and restart_count < max_restarts:
            try:
                self._start_process(binary)
                self._read_output()

                exit_code = self._process.poll() if self._process else -1
                if self._stop_event.is_set():
                    return

                restart_count += 1
                logger.warning(
                    f"cloudflared exited (code {exit_code}). "
                    f"Restarting in {backoff}s (attempt {restart_count}/{max_restarts})"
                )
                self._url = None
                self._url_event.clear()

                for _ in range(backoff):
                    if self._stop_event.is_set():
                        return
                    time.sleep(1)

            except Exception as e:
                logger.error(f"Tunnel monitor error: {e}")
                restart_count += 1
                time.sleep(backoff)

        if restart_count >= max_restarts:
            logger.error(f"cloudflared crashed {max_restarts} times. Giving up.")

    def _start_process(self, binary: str) -> None:
        if self.token:
            cmd = [binary, "tunnel", "--no-autoupdate", "run", "--token", self.token]
            logger.info("🔗 Starting named tunnel (persistent URL)")
        else:
            cmd = [binary, "tunnel", "--no-autoupdate", "--url", f"http://localhost:{self.local_port}"]
            logger.info("🔗 Starting quick tunnel (random URL)")

        self._process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )
        logger.info(f"cloudflared started (PID: {self._process.pid})")

    def _read_output(self) -> None:
        if not self._process or not self._process.stdout:
            return

        for line in self._process.stdout:
            if self._stop_event.is_set():
                break
            line = line.strip()
            if not line:
                continue
            logger.debug(f"[cloudflared] {line}")

            if not self._url:
                url = self._extract_url(line)
                if url:
                    with self._lock:
                        self._url = url
                    self._url_event.set()
                    logger.info(f"🔗 Tunnel URL discovered: {url}")

        if self._process:
            self._process.wait()

    def _extract_url(self, line: str) -> Optional[str]:
        for pattern in _URL_PATTERNS:
            match = pattern.search(line)
            if match:
                url = match.group(0) if match.lastindex is None else match.group(1)
                if "trycloudflare.com" in url or "bagariaa.in" in url:
                    return url
                if "Registered" in line or "|" in line or "url=" in line:
                    return url
        return None

    def _kill_process(self) -> None:
        if self._process:
            try:
                self._process.terminate()
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait(timeout=3)
            except Exception:
                pass
            self._process = None

    # ═══════════════════════════════════════════════════════════════════
    # BINARY SEARCH — prioritizes bundled bin/ directory
    # ═══════════════════════════════════════════════════════════════════

    @staticmethod
    def _find_binary() -> Optional[str]:
        """
        Find the cloudflared binary. Search order:

        1. bin/ directory next to the script (bundled binary — HIGHEST PRIORITY)
        2. System PATH (shutil.which)
        3. Platform-specific install locations
        4. Same directory as the script (flat layout fallback)

        This order ensures the user never needs to install cloudflared
        manually — we ship it inside bin/ alongside the .exe.
        """
        script_dir = os.path.dirname(os.path.abspath(sys.argv[0]))
        exe_name = "cloudflared.exe" if sys.platform == "win32" else "cloudflared"

       # ── 1. Bundled bin/ directory (highest priority) ─────────────
        # Sprint 3.5c: this used to be os.path.join(script_dir, "bin", ...),
        # where script_dir is dirname(sys.argv[0]) — i.e. dist/RGBCDrive/.
        # But PyInstaller puts `datas` under _internal/, so the bundled
        # binary lives at dist/RGBCDrive/_internal/bin/cloudflared.exe and
        # this check never matched. It failed silently, fell through to the
        # %ProgramFiles% probe, and found the winget-installed copy — so it
        # looked fine on any dev box and was broken on every clean machine.
        # paths.get_cloudflared_dir() already knows the correct layout.
        bundled = str(get_cloudflared_dir() / exe_name)
        if os.path.isfile(bundled):
            # On Unix, ensure it's executable
            if sys.platform != "win32" and not os.access(bundled, os.X_OK):
                try:
                    os.chmod(bundled, 0o755)
                except OSError:
                    pass
            if os.access(bundled, os.X_OK) or sys.platform == "win32":
                return bundled

        # ── 2. System PATH ───────────────────────────────────────────
        found = shutil.which("cloudflared")
        if found:
            return found

        # ── 3. Platform-specific common locations ────────────────────
        candidates = []

        if sys.platform == "win32":
            candidates = [
                os.path.expandvars(r"%ProgramFiles%\cloudflared\cloudflared.exe"),
                os.path.expandvars(r"%ProgramFiles(x86)%\cloudflared\cloudflared.exe"),
                os.path.expandvars(r"%LOCALAPPDATA%\cloudflared\cloudflared.exe"),
                os.path.expanduser(r"~\cloudflared.exe"),
            ]
        elif sys.platform == "darwin":
            candidates = [
                "/opt/homebrew/bin/cloudflared",
                "/usr/local/bin/cloudflared",
            ]
        else:
            candidates = [
                "/usr/local/bin/cloudflared",
                "/usr/bin/cloudflared",
                "/snap/bin/cloudflared",
            ]

        # ── 4. Same directory as script (flat layout fallback) ───────
        candidates.append(os.path.join(script_dir, exe_name))

        for path in candidates:
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path

        return None