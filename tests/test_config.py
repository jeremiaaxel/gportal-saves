#!/usr/bin/env python3
"""Unit tests: config_store backends + FTP URL parsing (no network)."""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def test_parse_url():
    # import without triggering server startup
    server = importlib.import_module("server")

    # full URL
    p = server.parse_ftp_url("ftp://bob:hunter2@ch.g-portal.com:2121")
    assert p == {"ok": True, "scheme": "ftp", "host": "ch.g-portal.com",
                 "port": 2121, "user": "bob", "password": "hunter2"}, p

    # bare host
    p = server.parse_ftp_url("ch.g-portal.com")
    assert p["ok"] and p["host"] == "ch.g-portal.com" and "port" not in p

    # host:port
    p = server.parse_ftp_url("ch.g-portal.com:21")
    assert p["ok"] and p["port"] == 21

    # user@host (no password)
    p = server.parse_ftp_url("ftp://bob@ch.g-portal.com")
    assert p["ok"] and p["user"] == "bob" and "password" not in p

    # URL-encoded chars
    p = server.parse_ftp_url("ftp://bob:p%40ss@ch.g-portal.com")
    assert p["password"] == "p@ss", p

    # trailing path ignored
    p = server.parse_ftp_url("ftp://bob:pw@ch.g-portal.com/some/path")
    assert p["ok"] and p["host"] == "ch.g-portal.com" and "some" not in str(p.get("host"))

    # sftp warning
    p = server.parse_ftp_url("sftp://bob:pw@ch.g-portal.com")
    assert p["ok"] and "warning" in p

    # garbage
    p = server.parse_ftp_url("")
    assert not p["ok"]
    p = server.parse_ftp_url("ftp://")
    assert not p["ok"]

    print("OK parse_ftp_url: 9 cases")


def test_config_store_fallback():
    tmp = tempfile.mkdtemp(prefix="gps-cfg-")
    os.environ["XDG_STATE_HOME"] = tmp
    # neutralize keyring for determinism
    import config_store
    cs = importlib.reload(config_store)
    cs._secret_tool = lambda: None  # force file backend

    cs.save({"host": "h", "port": 21, "user": "u", "password": "p"})
    f = Path(tmp) / "gportal-saves" / "config.json"
    assert f.exists(), "file backend should write the state file"
    mode = f.stat().st_mode & 0o777
    assert mode == 0o600, f"expected 600, got {oct(mode)}"
    got = cs.load()
    assert got["host"] == "h" and got["password"] == "p"

    # env override wins
    os.environ["GPORTAL_FTP_HOST"] = "env-host"
    got = cs.load()
    assert got["host"] == "env-host"
    del os.environ["GPORTAL_FTP_HOST"]

    print("OK config_store file fallback: save/perm 600/load/env-override")


def test_keyring_roundtrip():
    """Only runs if a Secret Service is actually reachable."""
    import config_store
    # undo the file-backend monkey-patch from the previous test
    import shutil
    real_which = shutil.which
    config_store._secret_tool = lambda: real_which("secret-tool")
    cs = config_store
    if not cs.keyring_available():
        print("SKIP keyring roundtrip: no Secret Service")
        return
    data = {"host": "kr-test-host", "port": 21, "user": "kr-user", "password": "kr-pass"}
    backend = cs.save(data)
    assert backend == "keyring", f"expected keyring, got {backend}"
    got = cs.load()
    assert got["host"] == "kr-test-host" and got["password"] == "kr-pass"
    # clean up the test secret
    subprocess = cs.subprocess
    subprocess.run(["secret-tool", "clear",
                    *sum(([k, v] for k, v in cs.STORE_ATTRS.items()), [])],
                   capture_output=True, timeout=5)
    print("OK keyring roundtrip (real Secret Service)")


if __name__ == "__main__":
    test_parse_url()
    test_config_store_fallback()
    test_keyring_roundtrip()
    print("\nALL UNIT TESTS PASSED")
