"""
RGBC Drive — Path Resolution for PyInstaller Bundles

Two path contexts:
  INTERNAL (bundled assets): icon files, public.pem template, cloudflared binary.
    → Resolved via sys._MEIPASS (PyInstaller) or script directory (dev mode).
    → These ship INSIDE the bundle and are read-only.

  EXTERNAL (user config): .env, client_secret.json, oauth cache, logs.
    → Always resolved relative to the EXECUTABLE's location, not _MEIPASS.
    → The user creates/edits these files next to RGBCDrive.exe.

Why the distinction matters:
  In one-file mode, _MEIPASS is a temp directory that's deleted on exit.
  User config MUST live outside it or it vanishes between runs.
  In one-folder mode, _MEIPASS and the exe dir are the same — but we
  still separate the concepts for correctness.
"""

import os
import sys
from pathlib import Path


def get_internal_dir() -> Path:
    """
    Directory containing bundled assets (icon, public.pem, bin/).
    In PyInstaller bundle: sys._MEIPASS
    In dev mode: directory containing rgbc_drive.py
    """
    if getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS'):
        return Path(sys._MEIPASS)
    return Path(os.path.dirname(os.path.abspath(sys.argv[0])))


def get_external_dir() -> Path:
    """
    Directory for user-editable config files (.env, logs, cache).
    Always the directory containing the actual executable, never _MEIPASS.
    In PyInstaller bundle: directory containing RGBCDrive.exe
    In dev mode: directory containing rgbc_drive.py
    """
    if getattr(sys, 'frozen', False):
        return Path(os.path.dirname(sys.executable))
    return Path(os.path.dirname(os.path.abspath(sys.argv[0])))


def get_internal_path(filename: str) -> Path:
    """Resolve a bundled asset path (read-only)."""
    return get_internal_dir() / filename


def get_external_path(filename: str) -> Path:
    """Resolve a user config file path (read-write)."""
    return get_external_dir() / filename


def get_env_path() -> Path:
    """Path to the .env file (external, user-editable)."""
    return get_external_path(".env")


def get_public_key_path() -> str:
    """
    Resolve public.pem: check external dir first (user override),
    then fall back to internal bundled copy.
    """
    external = get_external_path("public.pem")
    if external.is_file():
        return str(external)

    internal = get_internal_path("public.pem")
    if internal.is_file():
        return str(internal)

    return str(external)  # Return expected path for error messages


def get_cloudflared_dir() -> Path:
    """Directory containing the bundled cloudflared binary."""
    internal_bin = get_internal_dir() / "bin"
    if internal_bin.is_dir():
        return internal_bin

    external_bin = get_external_dir() / "bin"
    if external_bin.is_dir():
        return external_bin

    return internal_bin


def is_frozen() -> bool:
    """True if running inside a PyInstaller bundle."""
    return getattr(sys, 'frozen', False)