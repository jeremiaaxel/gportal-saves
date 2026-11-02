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
        "ftp_root": c.get("ftp_root", ""),
        "store_dir": c.get("store_dir", ""),
        "store_default": str(default_store_root()),
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


def resolve_game_spec(ftp, ftp_root: str = "") -> tuple[games_mod.GameSpec | None, str]:
    """Return (spec, base) where base is the FTP dir the game lives under.

    ftp_root override (from config) takes precedence: paths are probed
    relative to it first; on miss we still try plain auto-detection so a
    stale override degrades gracefully instead of breaking backups.
    """
    if ftp_root:
        base = "/" + ftp_root.strip("/") if ftp_root.strip("/") else ""
        for gid in games_mod.GAMES:
            spec = games_mod.GAMES[gid]
            probe = posixpath.join(base, spec.root)
            try:
                if ftp_ops.mlsd(ftp, probe) is not None:
                    return spec, base
            except Exception:
                continue
    # auto-detect from FTP root
    spec = games_mod.detect_game_from_dirs(ftp)
    if spec is not None:
        return spec, ""
    return None, ""


def run_pull(job_id: str) -> None:
    creds = get_creds()
    if not creds.get("host"):
        JOBS[job_id].update(status="error", error="No FTP credentials configured — finish the setup wizard first.")
        return
    log(job_id, f"Connecting to FTP {creds['host']}:{creds.get('port', 21)}…")
    ftp = ftp_ops.connect(creds["host"], creds["port"], creds["user"], creds["password"])
    try:
        spec, base = resolve_game_spec(ftp, creds.get("ftp_root", ""))
        if spec is None:
            JOBS[job_id].update(
                status="error",
                error="Could not auto-detect a known game from the FTP root. "
                "Set the server folder manually in ⚙ Settings (FTP folder override).",
            )
            return
        log(job_id, f"Detected game: {spec.title} (root: {posixpath.join(base, spec.root) if base else spec.root})")
        snap_dir = store.new_snapshot_dir(spec.game_id)
        log(job_id, f"Snapshot dir: {snap_dir}")
        files: list[ftp_ops.FtpFile] = []
        for p in spec.paths:
            full = posixpath.join(base, spec.root, p.rel_path) if base else posixpath.join(spec.root, p.rel_path)
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
            # strip the (possibly overridden) base so snapshots stay spec-relative
            strip_from = posixpath.join(base, spec.root) if base else spec.root
            rel = posixpath.relpath(f.path, strip_from)
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
        # honor the FTP folder override (same base logic as run_pull)
        base = ""
        if creds.get("ftp_root"):
            base = "/" + creds["ftp_root"].strip("/")
        log(job_id, f"Restoring snapshot {snap_dir.name} to server…")
        ftp = ftp_ops.connect(creds["host"], creds["port"], creds["user"], creds["password"])
        try:
            strip_from = posixpath.join(base, spec.root) if base else spec.root
            for p in spec.paths:
                full = posixpath.join(strip_from, p.rel_path)
                local_base = snap_dir / p.rel_path
                if not local_base.exists():
                    log(job_id, f"(optional) {p.rel_path} not in snapshot — skipping")
                    continue
                local_files = [f for f in snap_dir.rglob("*") if f.is_file() and str(f).startswith(str(local_base))]
                total = len(local_files)
                log(job_id, f"Uploading {total} files to {full}…")
                for i, lf in enumerate(local_files, 1):
                    rel = lf.relative_to(snap_dir).as_posix()
                    remote = posixpath.join(strip_from, rel)
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
        # Client layout differs from the server's! Server snapshot:
        #   PlayerData/DedicatedServer/Prospects/*.json
        # Client expects per-player:
        #   PlayerData/<steamid>/Prospects/*.json
        # Remap DedicatedServer/Prospects into the local player folder so
        # prospects actually appear in the client's load list.
        steamid_dir = next(
            (d for d in (dest / "PlayerData").glob("7*") if d.is_dir() and d.name.isdigit()),
            None,
        ) if (dest / "PlayerData").is_dir() else None
        if steamid_dir is None:
            JOBS[job_id].update(
                status="error",
                error=f"No PlayerData/<steamid> folder found under {dest} — launch Icarus once (client) so it creates your player folder, then retry.",
            )
            return
        prospects_src = snap_dir / "PlayerData" / "DedicatedServer" / "Prospects"
        if not prospects_src.is_dir():
            JOBS[job_id].update(status="error", error="Snapshot has no Prospects folder (unexpected).")
            return
        prospects_dst = steamid_dir / "Prospects"
        log(job_id, f"Copying server Prospects → {prospects_dst} (client layout)…")
        copied = 0
        import shutil

        for lf in prospects_src.rglob("*"):
            if lf.is_file():
                rel = lf.relative_to(prospects_src).as_posix()
                target_path = prospects_dst / rel
                target_path.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(lf, target_path)
                copied += 1
                log(job_id, f"  {rel}")
        log(job_id, f"✔ Copied {copied} files into your client's Prospects folder ({steamid_dir.name}).")
        log(job_id, "Launch Icarus single-player — the prospect should appear in the load list. "
                    "Remove the copies afterwards if you don't want them locally (Steam Cloud may sync them).")
    JOBS[job_id].update(status="done", result={"game": spec.game_id, "snapshot": snap_dir.name, "target": target})


def run_verify(job_id: str, snap_name: str | None = None) -> None:
    """Offline integrity check: does a snapshot contain all critical parts?"""
    spec = games_mod.GAMES["icarus"]
    snaps = store.snapshots(spec.game_id)
    if not snaps:
        JOBS[job_id].update(status="error", error="No snapshots yet.")
        return
    snap_dir = (store.game_dir(spec.game_id) / "snapshots" / snap_name) if snap_name else snaps[0]
    if not snap_dir.exists():
        JOBS[job_id].update(status="error", error=f"Snapshot {snap_name} not found.")
        return
    rec = store.snapshot_record(spec.game_id, snap_dir, spec)
    log(job_id, f"Verifying snapshot {snap_dir.name}…")
    ok = all(i["ok"] for i in rec["checklist"] if i["critical"])
    for item in rec["checklist"]:
        state = "OK " if item["ok"] else ("MISSING" if item["critical"] else "warn")
        log(job_id, f"  [{state}] {item['role']}: {item['files']} files ({item['bytes']/1e6:.1f} MB)")
    log(job_id, "✔ All critical components present." if ok else "✘ Some critical components are MISSING — re-run Backup.")
    verdict = {
        "snapshot": snap_dir.name,
        "ok": ok,
        "checked": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "checklist": rec["checklist"],
    }
    # persist per-snapshot + latest verdict in ONE write (read-modify-write
    # via set_meta here would clobber the per-snapshot changes)
    m = store.read_manifest(spec.game_id)
    snap_rec = m.setdefault("snapshots", {}).setdefault(snap_dir.name, {})
    snap_rec["files"] = rec["files"]
    snap_rec["bytes"] = rec["bytes"]
    snap_rec["verify"] = verdict
    m["last_verify"] = verdict
    store.write_manifest(spec.game_id, m)
    JOBS[job_id].update(status="done", result={"game": spec.game_id, **verdict})


def run_status(job_id: str) -> None:
    creds = get_creds()
    if not creds.get("host"):
        JOBS[job_id].update(status="error", error="No FTP credentials configured.")
        return
    ftp = ftp_ops.connect(creds["host"], creds["port"], creds["user"], creds["password"])
    try:
        root = ftp_ops.mlsd(ftp, "/")
        spec, base = resolve_game_spec(ftp, creds.get("ftp_root", ""))
        log(job_id, f"FTP root: {[e.path for e in root]}")
        if creds.get("ftp_root"):
            log(job_id, f"Folder override active: {creds['ftp_root']}")
        log(job_id, f"Detected game: {spec.title if spec else 'unknown'}")
        if spec:
            strip_from = posixpath.join(base, spec.root) if base else spec.root
            for p in spec.paths:
                full = posixpath.join(strip_from, p.rel_path)
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
        t = threading.Thread(target=run_verify, args=(job_id, kwargs.get("snapshot")), daemon=True)
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
                snaps = {}
                manifests = {}
                for gid in games_mod.GAMES:
                    m = store.read_manifest(gid)
                    manifests[gid] = m
                    snaps[gid] = [
                        {
                            "name": s.name,
                            "mtime": s.stat().st_mtime,
                            "files": m.get("snapshots", {}).get(s.name, {}).get("files"),
                            "bytes": m.get("snapshots", {}).get(s.name, {}).get("bytes"),
                            "verify": m.get("snapshots", {}).get(s.name, {}).get("verify"),
                        }
                        for s in store.snapshots(gid)
                    ]
                jobs = {k: {kk: vv for kk, vv in v.items()} for k, v in JOBS.items()}
            self._json({
                "games": list(games_mod.GAMES),
                "snapshots": snaps,
                "last_verify": {gid: manifests[gid].get("last_verify") for gid in games_mod.GAMES},
                "jobs": jobs,
                "store_root": str(store.get_root()),
                **_public_config(),
            })
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
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
            self._json({"job": start_job("verify", snapshot=body.get("snapshot"))})
        elif self.path == "/api/status":
            self._json({"job": start_job("status")})
def icarus_client_dir_candidates() -> list[Path]:
    """Well-known locations of the Icarus client save folder (Steam/Proton)."""
    return [
        Path.home() / ".local/share/Steam/steamapps/compatdata/1149460/pfx/drive_c/users/steamuser/AppData/Local/Icarus/Saved",
        Path.home() / ".steam/steam/steamapps/compatdata/1149460/pfx/drive_c/users/steamuser/AppData/Local/Icarus/Saved",
        Path.home() / ".var/app/com.valvesoftware.Steam/data/Steam/steamapps/compatdata/1149460/pfx/drive_c/users/steamuser/AppData/Local/Icarus/Saved",
    ]


def default_store_root() -> Path:
    """Easy-to-find default for the snapshot archive: inside the Steam
    library when present (next to steamapps), else ~/gportal-saves."""
    steam = Path.home() / ".local/share/Steam"
    if steam.is_dir():
        return steam / "gportal-saves"
    return Path.home() / "gportal-saves"


def _suggested_client_dir() -> str | None:
    for c in icarus_client_dir_candidates():
        try:
            if c.is_dir():
                return str(c)
        except OSError:
            continue
    return None


def openable_path(raw: str) -> Path | None:
    """Allowlist for /api/fs/open: only paths that are part of this tool's
    world — the snapshot store, the configured client dir, the detected
    client dir, or the server config dir. Anything else is refused so a
    stray request can't pop arbitrary local folders."""
    if not raw:
        return None
    try:
        rp = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    store_root = store.get_root().resolve()
    parts = [store_root]
    c = get_creds().get("client_dir", "")
    if c:
        try:
            parts.append(Path(c).expanduser().resolve(strict=True))
        except (OSError, RuntimeError):
            pass
    sug = _suggested_client_dir()
    if sug:
        try:
            parts.append(Path(sug).resolve(strict=True))
        except OSError:
            pass
    for base in parts:
        if rp == base or base in rp.parents:
            return rp
    return None


def open_in_file_manager(path: Path) -> bool:
    """Reveal a folder in the desktop file manager (dolphin/explorer/finder)."""
    import subprocess
    import sys

    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(path)])
        else:
            subprocess.Popen(["xdg-open", str(path)])
        return True
    except OSError:
        return False


def native_pick_folder(start: str = "") -> dict:
    """Open the OS's native folder picker and return the chosen path.

    KDE: kdialog — GNOME/other: zenity — Windows: tkinter — macOS: osascript.
    Blocks until the user closes the dialog; local tool, so that's fine.
    """
    import shutil
    import subprocess
    import sys

    start_dir = start if start and Path(start).is_dir() else str(Path.home())
    try:
        if sys.platform == "darwin":
            r = subprocess.run(
                ["osascript", "-e",
                 f'POSIX file "{start_dir}" as text'],
                capture_output=True, text=True, timeout=600)
            script = (f'tell application "System Events" to activate\r'
                      f'set _p to choose folder default location POSIX file "{start_dir}"\r'
                      f'return POSIX path of _p')
            r = subprocess.run(["osascript", "-e", script],
                               capture_output=True, text=True, timeout=600)
            if r.returncode != 0:
                return {"ok": False, "cancelled": True}
            return {"ok": True, "path": r.stdout.strip()}
        if sys.platform == "win32":
            import tkinter as tk
            from tkinter import filedialog

            root = tk.Tk()
            root.withdraw()
            root.attributes("-topmost", True)
            path = filedialog.askdirectory(initialdir=start_dir)
            root.destroy()
            if not path:
                return {"ok": False, "cancelled": True}
            return {"ok": True, "path": path}
        # Linux / BSD: kdialog first (native KDE), then zenity
        for cmd in (["kdialog", "--getexistingdirectory", start_dir],
                    ["zenity", "--file-selection", "--directory",
                     "--filename", start_dir + "/"]):
            if shutil.which(cmd[0]) is None:
                continue
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
            if r.returncode != 0 or not r.stdout.strip():
                return {"ok": False, "cancelled": True}
            return {"ok": True, "path": r.stdout.strip().rstrip("/")}
        return {"ok": False, "error": "No folder picker available (install kdialog or zenity)."}
    except subprocess.TimeoutExpired:
        return {"ok": False, "cancelled": True}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}


def delete_snapshot(game_id: str, name: str) -> dict:
    """Delete ONE snapshot (dir + manifest record). Name must be a plain
    snapshot folder name — no path traversal."""
    import shutil

    if not name or "/" in name or "\\" in name or name.startswith("."):
        return {"ok": False, "error": "Invalid snapshot name."}
    snap_dir = store.game_dir(game_id) / "snapshots" / name
    if not snap_dir.is_dir():
        return {"ok": False, "error": f"Snapshot {name} not found."}
    others = [s for s in store.snapshots(game_id) if s.name != name]
    if not others:
        # deleting the only snapshot = full cleanup semantics
        return delete_all_snapshots(game_id)
    shutil.rmtree(snap_dir)
    m = store.read_manifest(game_id)
    m.get("snapshots", {}).pop(name, None)
    lv = m.get("last_verify") or {}
    if lv.get("snapshot") == name:
        m.pop("last_verify", None)
    store.write_manifest(game_id, m)
    return {"ok": True, "deleted": [name]}


def delete_all_snapshots(game_id: str) -> dict:
    """Delete EVERY snapshot + the manifest. The dangerous 'cleanup' path."""
    import shutil

    gdir = store.game_dir(game_id)
    count = len(store.snapshots(game_id))
    if gdir.exists():
        shutil.rmtree(gdir)
    return {"ok": True, "deleted_count": count}


def relocate_store(old_setting: str, new_setting: str) -> int:
    """Move the snapshot store to a new location; returns migrated count.

    old_setting: previously configured store dir ("" = default location)
    new_setting: desired store dir ("" = back to default)
    Only the game's snapshot tree moves; the old dir is left as an empty
    husk (or removed if empty) so a wrong path can be corrected.
    """
    import shutil

    old_root = Path(old_setting).expanduser() if old_setting else store.default_root()
    new_root = Path(new_setting).expanduser() if new_setting else default_store_root()
    old_gdir = old_root / "icarus"
    new_gdir = new_root / "icarus"
    if old_gdir == new_gdir:
        return 0
    moved = 0
    if old_gdir.exists():
        moved = len(store.snapshots("icarus"))
        new_root.mkdir(parents=True, exist_ok=True)
        if new_gdir.exists():
            raise FileExistsError(f"Target already contains snapshots: {new_gdir}")
        shutil.move(str(old_gdir), str(new_gdir))
        # clean up the emptied old tree (snapshots/<game> gone)
        try:
            (old_root / "snapshots").rmdir()
        except OSError:
            pass
        if old_root not in (store.default_root(), default_store_root()):
            try:
                old_root.rmdir()
            except OSError:
                pass  # not empty or in use — leave it
    store.set_root(new_root)
    if not new_setting:
        store.set_default_root(default_store_root())
    return moved


def apply_store_setting() -> None:
    """On boot: point the store at the configured location (no move).
    Without a user setting, upgrade the default to the easy-to-find
    location — unless GPORTAL_SAVES_STORE was set explicitly (env wins)."""
    c = get_creds()
    sd = (c.get("store_dir") or "").strip()
    env_set = bool(os.environ.get("GPORTAL_SAVES_STORE"))
    if sd:
        try:
            store.set_root(Path(sd).expanduser())
        except Exception:
            pass
    elif not env_set:
        store.set_default_root(default_store_root())


def list_dir(raw: str) -> dict:
    """List a local directory for the client-folder browser.

    Bounded: real directories only, symlinked dirs skipped (no traversal
    out of the tree), entry cap. Hidden entries included — AppData is
    hidden under Proton prefixes.
    """
    if not raw or raw == "/__roots__":
        roots = {"/", str(Path.home())}
        for base in ("/run/media", "/media"):
            try:
                if Path(base).is_dir():
                    roots.add(base)
            except OSError:
                pass
        return {"ok": True, "path": "", "is_root_listing": True,
                "entries": sorted(roots), "parent": None,
                "suggestion": _suggested_client_dir()}

    p = Path(raw).expanduser()
    try:
        rp = p.resolve(strict=True)
    except (OSError, RuntimeError):
        return {"ok": False, "error": f"Path not found: {raw}"}
    if not rp.is_dir():
        return {"ok": False, "error": f"Not a directory: {raw}"}
    try:
        children = list(rp.iterdir())[:2000]
    except PermissionError:
        return {"ok": False, "error": f"Permission denied: {raw}"}
    dirs: list[str] = []
    files = 0
    for c in children:
        try:
            # symlinked dirs skipped: browser navigates real dirs only
            if c.is_dir() and not c.is_symlink():
                dirs.append(c.name)
            elif c.is_file():
                files += 1
        except OSError:
            continue
    suggestion = _suggested_client_dir()
    hit = False
    if suggestion:
        try:
            hit = rp == Path(suggestion).resolve()
        except OSError:
            hit = False
    return {"ok": True, "path": str(rp), "entries": sorted(dirs), "files": files,
            "parent": str(rp.parent) if rp.parent != rp else None,
            "suggestion": suggestion, "suggestion_here": hit}


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
                snaps = {}
                manifests = {}
                for gid in games_mod.GAMES:
                    m = store.read_manifest(gid)
                    manifests[gid] = m
                    snaps[gid] = [
                        {
                            "name": s.name,
                            "mtime": s.stat().st_mtime,
                            "files": m.get("snapshots", {}).get(s.name, {}).get("files"),
                            "bytes": m.get("snapshots", {}).get(s.name, {}).get("bytes"),
                            "verify": m.get("snapshots", {}).get(s.name, {}).get("verify"),
                        }
                        for s in store.snapshots(gid)
                    ]
                jobs = {k: {kk: vv for kk, vv in v.items()} for k, v in JOBS.items()}
            self._json({
                "games": list(games_mod.GAMES),
                "snapshots": snaps,
                "last_verify": {gid: manifests[gid].get("last_verify") for gid in games_mod.GAMES},
                "jobs": jobs,
                "store_root": str(store.get_root()),
                **_public_config(),
            })
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
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
            self._json({"job": start_job("verify", snapshot=body.get("snapshot"))})
        elif self.path == "/api/status":
            self._json({"job": start_job("status")})
        elif self.path == "/api/config/suggest-client-dir":
            sug = _suggested_client_dir()
            if sug:
                self._json({"found": True, "path": sug, "why": "Detected Icarus (Proton/Steam) client save folder."})
            else:
                self._json({"found": False, "candidates": [str(c) for c in icarus_client_dir_candidates()]})
        elif self.path == "/api/fs/list":
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
            self._json(list_dir(body.get("path", "")))
        elif self.path == "/api/ftp/list":
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
            # wizard mode: use the form's values, falling back to stored creds
            # (lets users browse BEFORE saving anything)
            stored = get_creds()
            creds = {
                "host": (body.get("host") or "").strip() or stored.get("host", ""),
                "port": int(body.get("port") or stored.get("port") or 21),
                "user": (body.get("user") or "").strip() or stored.get("user", ""),
                "password": body.get("password") or stored.get("password", ""),
            }
            if not creds.get("host"):
                self._json({"ok": False, "error": "Enter FTP host, user and password first (Step 1), or save the config."})
                return
            raw = (body.get("path") or "/").strip() or "/"
            try:
                ftp = ftp_ops.connect(creds["host"], creds["port"], creds["user"], creds["password"])
                try:
                    entries = ftp_ops.mlsd(ftp, raw)
                    dirs = sorted(
                        posixpath.basename(e.path) for e in entries if e.is_dir
                    )
                    files = sum(1 for e in entries if not e.is_dir)
                    parent = posixpath.dirname(raw) or "/" if raw != "/" else None
                    # offer game-spec hints for the current dir
                    hint = None
                    for gid, spec in games_mod.GAMES.items():
                        probe = posixpath.join(raw, spec.root)
                        try:
                            ftp_ops.mlsd(ftp, probe)
                            hint = posixpath.join(raw, spec.root)
                            break
                        except Exception:
                            continue
                    self._json({"ok": True, "path": raw, "entries": dirs,
                                "files": files, "parent": parent, "game_root_here": hint})
                finally:
                    ftp_ops.close(ftp)
            except Exception as e:
                self._json({"ok": False, "error": f"{type(e).__name__}: {e}"})
        elif self.path == "/api/fs/open":
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
            rp = openable_path(body.get("path", ""))
            if rp is None:
                self._json({"ok": False, "error": "Path is not part of this tool's world (store, client dir, or detected dir)."}, 403)
            elif open_in_file_manager(rp):
                self._json({"ok": True, "path": str(rp)})
            else:
                self._json({"ok": False, "error": "No file manager available (xdg-open failed)."})
        elif self.path == "/api/fs/pick":
            # native OS folder dialog; blocks until user picks/cancels
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
            self._json(native_pick_folder(body.get("start", "")))
        elif self.path == "/api/snapshots/delete":
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
            self._json(delete_snapshot(games_mod.GAMES["icarus"].game_id, body.get("name", "")))
        elif self.path == "/api/snapshots/delete-all":
            self._json(delete_all_snapshots(games_mod.GAMES["icarus"].game_id))
        elif self.path == "/api/config/parse-url":
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
            self._json(parse_ftp_url(body.get("url", "")))
        elif self.path == "/api/config/test":
            ln = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(ln) or b"{}")
            # settings mode: blanks mean "use the stored value" for every field
            stored = get_creds()
            host = body.get("host", "").strip() or stored.get("host", "")
            user = body.get("user", "").strip() or stored.get("user", "")
            pw = body.get("password", "") or stored.get("password", "")
            try:
                result = validate_creds(
                    host,
                    int(body.get("port") or stored.get("port") or 21),
                    user,
                    pw,
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
            body.setdefault("ftp_root", current.get("ftp_root", ""))
            # snapshot store location: empty = default; changing it moves
            # existing snapshots so no backup is orphaned
            new_store = (body.get("store_dir") or "").strip()
            old_store = current.get("store_dir", "")
            if new_store != old_store:
                try:
                    moved = relocate_store(old_store, new_store)
                    log_to_all(f"Snapshot store {'moved to ' + new_store if new_store else 'reset to default'}"
                               + (f" ({moved} snapshot(s) migrated)" if moved else ""))
                except Exception as e:
                    self._json({"ok": False, "error": f"Could not move snapshot store: {e}"}, 400)
                    return
            body.setdefault("store_dir", old_store if not new_store else new_store)
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
    apply_store_setting()
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
