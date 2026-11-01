#!/usr/bin/env python3
"""gportal-saves — one-command backup/restore for GPORTAL game servers.

Local web app (localhost only). Stdlib only.

Usage:
  python3 server.py            # serves http://127.0.0.1:8777
  python3 server.py --port N
"""

from __future__ import annotations

import argparse
import ftplib
import json
import os
import posixpath
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# allow `python3 -I server.py` (isolated mode drops the script dir from sys.path)
sys.path.insert(0, str(Path(__file__).resolve().parent))

VERSION = "0.3.0"  # bump when behavior changes; /api/state exposes it so a
                   # stale long-running process is obvious in the UI

import ftp_ops
import games as games_mod
import store
import config_store

STATE_LOCK = threading.Lock()
JOBS: dict[str, dict] = {}  # job_id -> {status, log, result, error, started, ended}


def new_job(kind: str) -> str:
    job_id = f"{kind}-{int(time.time()*1000)}"
    JOBS[job_id] = {
        "kind": kind,
        "status": "running",
        "log": [],
        "result": None,
        "error": None,
        "started": time.time(),
    }
    return job_id


def log(job_id: str, msg: str) -> None:
    print(f"[{job_id}] {msg}")
    JOBS[job_id]["log"].append(msg)


def get_creds() -> dict:
    """Read config via config_store (keyring > XDG state > env)."""
    return config_store.load()


def save_creds(creds: dict) -> str:
    """Persist config; returns backend used."""
    backend = config_store.save(creds)
    log_to_all(f"Config saved via {backend}")
    return backend


def log_to_all(msg: str) -> None:
    print(msg)


def parse_ftp_url(raw: str) -> dict:
    """Parse an FTP URL into connection fields.

    Accepts: ftp://user:pass@host:port  |  ftp://user@host  |  host[:port] —
    also tolerates sftp:// (parsed but flagged), URL-encoded chars, and a
    trailing path (ignored). GPORTAL's Status page shows credentials that can
    be concatenated into exactly this shape.
    """
    from urllib.parse import unquote, urlparse

    s = (raw or "").strip()
    if not s:
        return {"ok": False, "error": "Nothing pasted."}
    if "://" not in s:
        s = "ftp://" + s  # allow bare host:port
    try:
        u = urlparse(s)
    except Exception as e:
        return {"ok": False, "error": f"Could not parse: {e}"}
    scheme = (u.scheme or "ftp").lower()
    host = u.hostname or ""
    if not host:
        return {"ok": False, "error": "No host found in that URL."}
    out: dict = {"ok": True, "scheme": scheme, "host": host}
    if scheme == "sftp":
        out["warning"] = ("sftp:// detected — this tool speaks FTP/FTPS; "
                          "GPORTAL uses plain FTP, so keep the port but expect the scheme to differ.")
    if u.port:
        out["port"] = u.port
    if u.username:
        out["user"] = unquote(u.username)
    if u.password:
        out["password"] = unquote(u.password)
    return out


def _public_config() -> dict:
    c = get_creds()
    return {
        "configured": bool(c.get("host")),
        "host": c.get("host", ""),
        "port": c.get("port", 21),
        "user": c.get("user", ""),
        "has_password": bool(c.get("password")),
        "client_dir": c.get("client_dir", ""),
        "storage": config_store.storage_kind(),
        "version": VERSION,
    }


def validate_creds(host: str, port: int, user: str, password: str, timeout: int = 20) -> dict:
    """Connect + detect game + probe save paths. Returns result dict.

    Bounded: connect timeout, a hard socket floor so a stalled control
    connection can't hang the request forever, and one retry on transient
    network errors so a flaky moment reads as 'retry', not 'broken'.
    """
    last_err: Exception | None = None
    for attempt in (1, 2):
        ftp = None
        try:
            ftp = ftp_ops.connect(host, port, user, password, timeout=timeout)
            ftp.sock.settimeout(timeout)
            spec = games_mod.detect_game_from_dirs(ftp)
            result: dict = {"connected": True, "game": spec.game_id if spec else None,
                            "title": spec.title if spec else None}
            if spec:
                probes = []
                for p in spec.paths:
                    full = posixpath.join(spec.root, p.rel_path)
                    try:
                        entries = ftp_ops.walk(ftp, full)
                        probes.append({
                            "role": p.role,
                            "path": full,
                            "found": True,
                            "files": sum(1 for e in entries if not e.is_dir),
                            "bytes": sum(e.size for e in entries if not e.is_dir),
                            "critical": p.critical,
                        })
                    except ftplib.error_perm:
                        probes.append({"role": p.role, "path": full, "found": False,
                                       "files": 0, "bytes": 0, "critical": p.critical})
                result["probes"] = probes
            return result
        except (socket.timeout, TimeoutError, OSError) as e:
            last_err = e
        finally:
            if ftp is not None:
                ftp_ops.close(ftp)
    raise TimeoutError(f"FTP did not respond within {timeout}s (retried once): {last_err}")


def run_pull(job_id: str) -> None:
    creds = get_creds()
    if not creds.get("host"):
        JOBS[job_id].update(status="error", error="No FTP credentials configured — finish the setup wizard first.")
        return
    log(job_id, f"Connecting to FTP {creds['host']}:{creds.get('port', 21)}…")
    ftp = ftp_ops.connect(creds["host"], creds["port"], creds["user"], creds["password"])
    try:
        spec = games_mod.detect_game_from_dirs(ftp)
        if spec is None:
            JOBS[job_id].update(
                status="error",
                error="Could not auto-detect a known game from the FTP root. "
                "Please check the path mapping in games.py (GPORTAL may mount FTP root at '/' with the game folder under it).",
            )
            return
        log(job_id, f"Detected game: {spec.title} (root: {spec.root})")
        snap_dir = store.new_snapshot_dir(spec.game_id)
        log(job_id, f"Snapshot dir: {snap_dir}")
        files: list[ftp_ops.FtpFile] = []
        for p in spec.paths:
            full = posixpath.join(spec.root, p.rel_path)
            log(job_id, f"Listing {full}…")
            try:
                ftp_ops.walk(ftp, full, files)
            except ftplib.error_perm as e:
                if p.critical:
                    JOBS[job_id].update(status="error", error=f"Missing critical path {full}: {e}")
                    return
                log(job_id, f"(optional) {full} not found — skipping")
        # paths can nest (Prospects inside DedicatedServer) — dedupe by FTP path
        seen: set[str] = set()
        files = [f for f in files if not (f.path in seen or seen.add(f.path))]
        to_fetch = [f for f in files if not f.is_dir]
        total_bytes = sum(f.size for f in to_fetch)
        log(job_id, f"{len(to_fetch)} files, {total_bytes/1e6:.1f} MB to download")
        done = 0
        started = time.time()
        for f in to_fetch:
            rel = posixpath.relpath(f.path, spec.root)
            local = snap_dir / rel
            got = ftp_ops.download(ftp, f.path, str(local))
            done += got
            rate = done / max(time.time() - started, 0.1) / 1024
            log(job_id, f"  {rel} ({got/1024:.0f} KiB) — {done/1e6:.1f}/{total_bytes/1e6:.1f} MB, {rate:.0f} KiB/s")
        rec = store.snapshot_record(spec.game_id, snap_dir, spec)
        store.update_manifest(spec.game_id, snap_dir.name, rec)
        pruned = store.prune_snapshots(spec.game_id)
        if pruned:
            log(job_id, f"Pruned {len(pruned)} old snapshot(s)")
        log(job_id, f"✔ Snapshot {snap_dir.name}: {rec['files']} files, {rec['bytes']/1e6:.1f} MB")
        JOBS[job_id].update(
            status="done",
            result={
                "game": spec.game_id,
                "snapshot": snap_dir.name,
                "files": rec["files"],
                "bytes": rec["bytes"],
                "checklist": rec["checklist"],
            },
        )
    finally:
        ftp_ops.close(ftp)


def run_push(job_id: str, snap_name: str | None, target: str = "server") -> None:
    """target: 'server' (FTP push back to GPORTAL) or 'client' (copy to a local dir)."""
    creds = get_creds()
    game_id_hint = "icarus"  # single-game for now; multi-game will pick from manifest keys
    spec = games_mod.GAMES[game_id_hint]
    snaps = store.snapshots(spec.game_id)
    if not snaps:
        JOBS[job_id].update(status="error", error="No local snapshots exist yet. Do a Backup first.")
        return
    snap_dir = (store.game_dir(spec.game_id) / "snapshots" / snap_name) if snap_name else snaps[0]
    if not snap_dir.exists():
        JOBS[job_id].update(status="error", error=f"Snapshot {snap_name} not found.")
        return
    if target == "server":
        if not creds.get("host"):
            JOBS[job_id].update(status="error", error="No FTP credentials configured.")
            return
        log(job_id, f"Restoring snapshot {snap_dir.name} to server…")
        ftp = ftp_ops.connect(creds["host"], creds["port"], creds["user"], creds["password"])
        try:
            for p in spec.paths:
                full = posixpath.join(spec.root, p.rel_path)
                local_base = snap_dir / p.rel_path
                if not local_base.exists():
                    log(job_id, f"(optional) {p.rel_path} not in snapshot — skipping")
                    continue
                local_files = [f for f in snap_dir.rglob("*") if f.is_file() and str(f).startswith(str(local_base))]
                total = len(local_files)
                log(job_id, f"Uploading {total} files to {full}…")
                for i, lf in enumerate(local_files, 1):
                    rel = lf.relative_to(snap_dir).as_posix()
                    remote = posixpath.join(spec.root, rel)
                    ftp_ops.upload(ftp, str(lf), remote)
                    log(job_id, f"  [{i}/{total}] {rel}")
            log(job_id, "✔ Restore to server complete. Restart the server in the GPORTAL web UI, then connect.")
        finally:
            ftp_ops.close(ftp)
    else:  # client
        dest = Path(get_creds().get("client_dir", "")) if get_creds().get("client_dir") else None
        if not dest:
            JOBS[job_id].update(status="error", error="Set the client save folder in ⚙ Settings first (it's where your Icarus client keeps local saves).")
            return
        log(job_id, f"Copying snapshot {snap_dir.name} to {dest}…")
        copied = 0
        for lf in snap_dir.rglob("*"):
            if lf.is_file():
                rel = lf.relative_to(snap_dir).as_posix()
                target_path = dest / rel
                target_path.parent.mkdir(parents=True, exist_ok=True)
                import shutil

                shutil.copy2(lf, target_path)
                copied += 1
                log(job_id, f"  {rel}")
        log(job_id, f"✔ Copied {copied} files to client dir. See Icarus notes for in-game loading steps.")
    JOBS[job_id].update(status="done", result={"game": spec.game_id, "snapshot": snap_dir.name, "target": target})


def run_verify(job_id: str) -> None:
    """Offline integrity check: does the latest snapshot contain all critical parts?"""
    spec = games_mod.GAMES["icarus"]
    snaps = store.snapshots(spec.game_id)
    if not snaps:
        JOBS[job_id].update(status="error", error="No snapshots yet.")
        return
    rec = store.snapshot_record(spec.game_id, snaps[0], spec)
    log(job_id, f"Verifying latest snapshot {snaps[0].name}…")
    ok = all(i["ok"] for i in rec["checklist"] if i["critical"])
    for item in rec["checklist"]:
        state = "OK " if item["ok"] else ("MISSING" if item["critical"] else "warn")
        log(job_id, f"  [{state}] {item['role']}: {item['files']} files ({item['bytes']/1e6:.1f} MB)")
    log(job_id, "✔ All critical components present." if ok else "✘ Some critical components are MISSING — re-run Backup.")
    JOBS[job_id].update(
        status="done",
        result={"game": spec.game_id, "snapshot": snaps[0].name, "ok": ok, "checklist": rec["checklist"]},
    )


def run_status(job_id: str) -> None:
    creds = get_creds()
    if not creds.get("host"):
        JOBS[job_id].update(status="error", error="No FTP credentials configured.")
        return
    ftp = ftp_ops.connect(creds["host"], creds["port"], creds["user"], creds["password"])
    try:
        root = ftp_ops.mlsd(ftp, "/")
        spec = games_mod.detect_game_from_dirs(ftp)
        log(job_id, f"FTP root: {[e.path for e in root]}")
        log(job_id, f"Detected game: {spec.title if spec else 'unknown'}")
        if spec:
            for p in spec.paths:
                full = posixpath.join(spec.root, p.rel_path)
                try:
                    entries = ftp_ops.walk(ftp, full)
                    n = sum(1 for e in entries if not e.is_dir)
                    b = sum(e.size for e in entries if not e.is_dir)
                    log(job_id, f"  {full}: {n} files, {b/1e6:.1f} MB")
                except ftplib.error_perm:
                    log(job_id, f"  {full}: NOT FOUND")
        JOBS[job_id].update(status="done", result={"game": spec.game_id if spec else None})
    finally:
        ftp_ops.close(ftp)


def start_job(kind: str, **kwargs) -> str:
    job_id = new_job(kind)
    if kind == "pull":
        t = threading.Thread(target=run_pull, args=(job_id,), daemon=True)
    elif kind == "push":
        t = threading.Thread(target=run_push, args=(job_id, kwargs.get("snapshot"), kwargs.get("target", "server")), daemon=True)
    elif kind == "verify":
        t = threading.Thread(target=run_verify, args=(job_id,), daemon=True)
    elif kind == "status":
        t = threading.Thread(target=run_status, args=(job_id,), daemon=True)
    else:
        raise ValueError(kind)
    t.start()
    return job_id


class Handler(BaseHTTPRequestHandler):
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj).encode(), "application/json")

    def do_GET(self):
        if self.path == "/":
            self._send(200, (Path(__file__).parent / "static" / "index.html").read_bytes(), "text/html; charset=utf-8")
        elif self.path == "/app.js":
            self._send(200, (Path(__file__).parent / "static" / "app.js").read_bytes(), "application/javascript")
        elif self.path == "/style.css":
            self._send(200, (Path(__file__).parent / "static" / "style.css").read_bytes(), "text/css")
        elif self.path == "/api/state":
            with STATE_LOCK:
                snaps = {
                    gid: [
                        {"name": s.name, "mtime": s.stat().st_mtime}
                        for s in store.snapshots(gid)
                    ]
                    for gid in games_mod.GAMES
                }
                jobs = {k: {kk: vv for kk, vv in v.items()} for k, v in JOBS.items()}
            self._json({"games": list(games_mod.GAMES), "snapshots": snaps, "jobs": jobs, "store_root": str(store.STORE_ROOT), **_public_config()})
        elif self.path.startswith("/api/job/"):
            job_id = self.path.rsplit("/", 1)[1]
            with STATE_LOCK:
                job = JOBS.get(job_id)
                self._json(job or {"error": "unknown job"}, 200 if job else 404)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/pull":
            if not get_creds().get("host"):
                self._json({"error": "Not configured yet — finish the setup wizard first."}, 400)
                return
            self._json({"job": start_job("pull")})
        elif self.path == "/api/push":
            if not get_creds().get("host"):
                self._json({"error": "Not configured yet — finish the setup wizard first."}, 400)
                return
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
            self._json({"job": start_job("push", snapshot=body.get("snapshot"), target=body.get("target", "server"))})
        elif self.path == "/api/verify":
            self._json({"job": start_job("verify")})
        elif self.path == "/api/status":
            self._json({"job": start_job("status")})
        elif self.path == "/api/config/suggest-client-dir":
            candidates = [
                Path.home() / ".local/share/Steam/steamapps/compatdata/1149460/pfx/drive_c/users/steamuser/AppData/Local/Icarus/Saved",
                Path.home() / ".steam/steam/steamapps/compatdata/1149460/pfx/drive_c/users/steamuser/AppData/Local/Icarus/Saved",
                Path.home() / ".var/app/com.valvesoftware.Steam/data/Steam/steamapps/compatdata/1149460/pfx/drive_c/users/steamuser/AppData/Local/Icarus/Saved",
            ]
            for c in candidates:
                if c.is_dir():
                    self._json({"found": True, "path": str(c), "why": "Detected Icarus (Proton/Steam) client save folder."})
                    return
            self._json({"found": False, "candidates": [str(c) for c in candidates]})
        elif self.path == "/api/config/parse-url":
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
            self._json(parse_ftp_url(body.get("url", "")))
        elif self.path == "/api/config/test":
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
            try:
                result = validate_creds(
                    body.get("host", "").strip(),
                    int(body.get("port") or 21),
                    body.get("user", "").strip(),
                    body.get("password", ""),
                )
                self._json(result)
            except Exception as e:
                self._json({"connected": False, "error": f"{type(e).__name__}: {e}"})
        elif self.path == "/api/config":
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
            current = get_creds()
            # blank password = keep the stored one (don't force re-entry)
            if not body.get("password") and current.get("password"):
                body["password"] = current["password"]
            body.setdefault("client_dir", current.get("client_dir", ""))
            save_creds(body)
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):  # quiet
        return


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8777)
    ap.add_argument("--open", action="store_true", help="open browser automatically")
    args = ap.parse_args()
    addr = ("127.0.0.1", args.port)
    httpd = ThreadingHTTPServer(addr, Handler)
    url = f"http://{addr[0]}:{addr[1]}"
    print(f"gportal-saves web UI: {url}")
    if args.open:
        webbrowser.open(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")


if __name__ == "__main__":
    main()
