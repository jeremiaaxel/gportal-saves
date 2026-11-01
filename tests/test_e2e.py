#!/usr/bin/env python3
"""End-to-end smoke test with an in-process fake FTP server.

Tests: connect, MLSD listing, game detection, recursive pull into snapshot
store, verify checklist, push back to server, client copy.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ftp_ops
import games as games_mod
import store


class FakeFtpHandler:
    """Minimal FTP command loop over a socket, backed by a local dict tree."""

    def __init__(self, conn, tree):
        self.conn = conn
        self.tree = tree  # {"/": {dirs...}} nested dicts; leaf value None = file
        self.cwd = "/"
        self.rfile = conn.makefile("rb")
        self.wfile = conn.makefile("wb")
        self.pasv_sock = None  # listening socket from PASV

    def send(self, line: str):
        self.wfile.write((line + "\r\n").encode())
        self.wfile.flush()

    def node(self, path):
        parts = [p for p in path.split("/") if p]
        node = self.tree
        for p in parts:
            if not isinstance(node, dict) or p not in node:
                return None
            node = node[p]
        return node

    def handle(self):
        self.send("220 fake")
        while True:
            line = self.rfile.readline()
            if not line:
                break
            cmd = line.decode().strip()
            try:
                if not self.dispatch(cmd):
                    break
            except Exception as e:  # keep session alive on errors
                self.send(f"500 {e}")
        self.conn.close()

    def dispatch(self, cmd: str) -> bool:
        verb, _, arg = cmd.partition(" ")
        v = verb.upper()
        if v == "USER":
            self.send("331 ok"); self.user = arg; return True
        if v == "PASS":
            self.send("230 ok"); return True
        if v == "SYST":
            self.send("215 UNIX Type: L8"); return True
        if v == "TYPE":
            self.send("200 ok"); return True
        if v == "AUTH":
            self.send("334 fake-no-tls"); return True
        if v in ("PBSZ", "PROT"):
            self.send("200 ok"); return True
        if v == "PWD":
            self.send(f'257 "{self.cwd}"'); return True
        if v == "PASV":
            import socket as s

            self.pasv_srv = s.socket()
            self.pasv_srv.setsockopt(s.SOL_SOCKET, s.SO_REUSEADDR, 1)
            self.pasv_srv.bind(("127.0.0.1", 0))
            self.pasv_srv.listen(1)
            p = self.pasv_srv.getsockname()[1]
            self.send(f"227 Entering Passive Mode (127,0,0,1,{p // 256},{p % 256})")
            return True
        if v == "EPSV":
            # respond like PASV; ftplib EPSV parse expects (|||port|)
            self.send("229 Entering Extended Passive Mode (|||0|)"); return True
        if v == "CWD":
            n = self.node(arg if arg.startswith("/") else self._join(arg))
            if n is None or not isinstance(n, dict):
                self.send("550 no such dir"); return True
            self.cwd = arg if arg.startswith("/") else self._join(arg)
            self.send("250 ok"); return True
        if v == "MKD":
            parent = self.node(self._dirname(arg if arg.startswith("/") else self._join(arg)))
            name = arg.rstrip("/").split("/")[-1]
            if isinstance(parent, dict):
                parent[name] = {}
            self.send("257 ok"); return True
        if v == "MLSD":
            path = arg if arg.startswith("/") else self._join(arg)
            n = self.node(path)
            if n is None or not isinstance(n, dict):
                self.send("550 no such dir"); return True
            self.send("150 here comes")
            data_sock = self.pasv_srv.accept()[0]
            lines = []
            for name, child in n.items():
                if isinstance(child, dict):
                    lines.append(f"type=dir;size=0; {name}")
                else:
                    lines.append(f"type=file;size={len(child)}; {name}")
            data_sock.sendall(("\r\n".join(lines) + "\r\n").encode())
            data_sock.close(); self.pasv_srv.close()
            self.send("226 done"); return True
        if v == "RETR":
            path = arg if arg.startswith("/") else self._join(arg)
            n = self.node(path)
            if n is None or isinstance(n, dict):
                self.send("550 no such file"); return True
            self.send("150 sending")
            data_sock = self.pasv_srv.accept()[0]
            data_sock.sendall(n if isinstance(n, bytes) else n.encode())
            data_sock.close(); self.pasv_srv.close()
            self.send("226 done"); return True
        if v == "STOR":
            path = arg if arg.startswith("/") else self._join(arg)
            self.send("150 ready")
            data_sock = self.pasv_srv.accept()[0]
            chunks = []
            while True:
                chunk = data_sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
            data_sock.close(); self.pasv_srv.close()
            data = b"".join(chunks)
            parent = self.node(self._dirname(path))
            if isinstance(parent, dict):
                parent[path.split("/")[-1]] = data
            self.send("226 done"); return True
        if v == "QUIT":
            self.send("221 bye"); return False
        self.send(f"502 {v} not implemented")
        return True

    def _join(self, rel):
        return (self.cwd.rstrip("/") + "/" + rel) if self.cwd != "/" else "/" + rel

    def _dirname(self, path):
        d = path.rsplit("/", 1)[0]
        return d or "/"


def make_fake_tree(tmp: Path) -> dict:
    prospect = json.dumps({"prospectId": "test-prospect", "meta": {"region": "Olympus"}}).encode()
    world_sav = b"W" * 4096
    player = b"P" * 1024
    accolades = b"A" * 97
    settings = b'{"DayLength": 45}'
    return {
        "Icarus": {
            "Config": {
                "Saved": {
                    "PlayerData": {
                        "DedicatedServer": {
                            "Accolades.json": accolades,
                            "Prospects": {
                                "TestProspect.json": prospect,
                                "TestProspect.sav": world_sav,
                            },
                        }
                    },
                    "Config": {"WindowsServer": {"ServerSettings.ini": settings}},
                }
            }
        }
    }


def serve_fake(tree) -> tuple[socket, threading.Thread]:
    import socket as s

    srv = s.socket()
    srv.setsockopt(s.SOL_SOCKET, s.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(4)
    conns = []

    def loop():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            conns.append(conn)
            threading.Thread(target=FakeFtpHandler(conn, tree).handle, daemon=True).start()

    t = threading.Thread(target=loop, daemon=True)
    t.start()
    return srv, conns


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="gportal-saves-test-"))
    os.environ["GPORTAL_SAVES_STORE"] = str(tmp / "store")
    import importlib

    importlib.reload(store)

    tree = make_fake_tree(tmp)
    srv, _ = serve_fake(tree)
    port = srv.getsockname()[1]

    # 1. connect + detect
    ftp = ftp_ops.connect("127.0.0.1", port, "u", "p")
    root = ftp_ops.mlsd(ftp, "/")
    spec = games_mod.detect_game_from_dirs(ftp)
    assert spec is not None, "ICARUS should be detected"
    print(f"OK detect: {spec.title}")

    # 2. walk
    files: list[ftp_ops.FtpFile] = []
    ftp_ops.walk(ftp, spec.root, files)
    fnames = sorted(f.path for f in files if not f.is_dir)
    print(f"OK walk: {fnames}")
    assert any(p.endswith("TestProspect.json") for p in fnames)
    assert any("DedicatedServer/Accolades.json" in p for p in fnames)

    # 3. pull into snapshot store
    snap = store.new_snapshot_dir(spec.game_id)
    for f in [x for x in files if not x.is_dir]:
        rel = f.path.removeprefix(spec.root + "/")
        ftp_ops.download(ftp, f.path, str(snap / rel))
    rec = store.snapshot_record(spec.game_id, snap, spec)
    print(f"OK pull: {rec['files']} files, {rec['bytes']} bytes")
    assert rec["files"] == 4, f"expected 4 files, got {rec['files']}"
    critical_ok = all(i["ok"] for i in rec["checklist"] if i["critical"])
    assert critical_ok, f"checklist failed: {rec['checklist']}"
    store.update_manifest(spec.game_id, snap.name, rec)
    print("OK checklist:", json.dumps(rec["checklist"], indent=1))

    # 4. push back to (fake) server — STOR path
    for lf in snap.rglob("*"):
        if lf.is_file():
            rel = lf.relative_to(snap).as_posix()
            ftp_ops.upload(ftp, str(lf), spec.root + "/" + rel)
    print("OK push back to server")

    # 5. client copy semantics (copy Prospects to a 'client' dir)
    client_dir = tmp / "client" / "Icarus" / "Saved"
    for lf in snap.rglob("*"):
        if lf.is_file() and "/Prospects/" in str(lf):
            rel = lf.relative_to(snap).as_posix()
            target = client_dir / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(lf.read_bytes())
    assert (client_dir / "PlayerData/DedicatedServer/Prospects/TestProspect.json").exists()
    print("OK client copy")

    ftp_ops.close(ftp)
    print("\nALL SMOKE TESTS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
