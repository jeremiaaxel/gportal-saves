"""Credential/config storage — no repo files, ever.

Order of preference:
  1. libsecret / Secret Service API (KDE ksecretd, gnome-keyring) via secret-tool
  2. XDG state file, chmod 600 (~/.local/state/gportal-saves/config.json)
  3. process memory only (non-persistent; warns via `storage` field)

The API is a tiny dict store: load() -> dict, save(dict) -> None.
Env vars (GPORTAL_FTP_*) still override for headless/CI use.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

_SERVICE_ATTR = ("gportal-saves", "config-v1")
_LABEL = "gportal-saves FTP configuration"

# attributes must be exactly what we look up later
STORE_ATTRS = {_SERVICE_ATTR[0]: _SERVICE_ATTR[1]}


def _state_file() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))
    return Path(xdg) / "gportal-saves" / "config.json"


def _secret_tool() -> str | None:
    return shutil.which("secret-tool")


def keyring_available() -> bool:
    st = _secret_tool()
    if not st:
        return False
    try:
        r = subprocess.run(
            [st, "search", "--all", *sum(([k, v] for k, v in STORE_ATTRS.items()), [])],
            capture_output=True, timeout=5,
        )
        # search returning 0 items is fine — the service answered
        return r.returncode in (0, 1)
    except Exception:
        return False


def _keyring_read() -> dict | None:
    st = _secret_tool()
    if not st:
        return None
    try:
        r = subprocess.run(
            [st, "lookup", *sum(([k, v] for k, v in STORE_ATTRS.items()), [])],
            capture_output=True, timeout=5,
        )
        if r.returncode != 0 or not r.stdout.strip():
            return None
        return json.loads(r.stdout.decode())
    except Exception:
        return None


def _keyring_write(data: dict) -> bool:
    st = _secret_tool()
    if not st:
        return False
    try:
        r = subprocess.run(
            [st, "store", "--label", _LABEL,
             *sum(([k, v] for k, v in STORE_ATTRS.items()), [])],
            input=json.dumps(data).encode(), capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


def _file_read() -> dict | None:
    p = _state_file()
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:
        return None


def _file_write(data: dict) -> bool:
    p = _state_file()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(data, indent=2))
        os.chmod(p, 0o600)
        return True
    except Exception:
        return False


def load() -> dict:
    """Merged config: env overrides > keyring > state file. Never raises."""
    base: dict = {}
    stored = _keyring_read() or _file_read()
    if stored:
        base.update(stored)
    env_map = {
        "host": "GPORTAL_FTP_HOST",
        "user": "GPORTAL_FTP_USER",
        "password": "GPORTAL_FTP_PASSWORD",
        "client_dir": "GPORTAL_CLIENT_DIR",
    }
    for key, env in env_map.items():
        if os.environ.get(env):
            base[key] = os.environ[env]
    if os.environ.get("GPORTAL_FTP_PORT"):
        try:
            base["port"] = int(os.environ["GPORTAL_FTP_PORT"])
        except ValueError:
            pass
    return base


def save(data: dict) -> str:
    """Persist; returns backend used: 'keyring' | 'file' | 'memory'."""
    if _keyring_write(data):
        # best-effort: remove any stale file copy now that the keyring has it
        try:
            _state_file().unlink(missing_ok=True)
        except Exception:
            pass
        return "keyring"
    if _file_write(data):
        return "file"
    return "memory"


def storage_kind() -> str:
    if os.environ.get("GPORTAL_FTP_HOST"):
        return "env"
    if keyring_available():
        return "keyring"
    if _secret_tool() is None:
        return "file"
    return "file"
