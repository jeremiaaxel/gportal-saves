"""Local backup store: dated snapshots under ~/.local/share/gportal-saves/<game>/.

Layout:
  <store>/<game_id>/snapshots/<UTC timestamp>/...files...
  <store>/<game_id>/latest -> most recent snapshot (symlink)
  <store>/<game_id>/manifest.json   (cumulative index of snapshots)
"""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

STORE_ROOT = Path(os.environ.get("GPORTAL_SAVES_STORE", Path.home() / ".local/share/gportal-saves"))


def game_dir(game_id: str) -> Path:
    return STORE_ROOT / game_id


def new_snapshot_dir(game_id: str) -> Path:
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    d = game_dir(game_id) / "snapshots" / ts
    d.mkdir(parents=True, exist_ok=True)
    return d


def snapshots(game_id: str) -> list[Path]:
    root = game_dir(game_id) / "snapshots"
    if not root.exists():
        return []
    return sorted([p for p in root.iterdir() if p.is_dir()], reverse=True)


def latest_snapshot(game_id: str) -> Path | None:
    snaps = snapshots(game_id)
    return snaps[0] if snaps else None


def manifest_path(game_id: str) -> Path:
    return game_dir(game_id) / "manifest.json"


def read_manifest(game_id: str) -> dict:
    p = manifest_path(game_id)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return {"snapshots": {}}


def update_manifest(game_id: str, snap_name: str, record: dict) -> None:
    m = read_manifest(game_id)
    m["snapshots"][snap_name] = record
    m["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    manifest_path(game_id).parent.mkdir(parents=True, exist_ok=True)
    manifest_path(game_id).write_text(json.dumps(m, indent=2))


def snapshot_record(game_id: str, snap_dir: Path, game_spec) -> dict:
    """Walk a snapshot dir and produce file counts/sizes + checklist results."""
    files: list[str] = []
    total = 0
    for p in snap_dir.rglob("*"):
        if p.is_file():
            rel = p.relative_to(snap_dir).as_posix()
            files.append(rel)
            total += p.stat().st_size
    items = game_spec.checklist([f"{game_spec.root}/{rel}" for rel in files])
    # fill byte counts per checklist item using the snapshot files
    for it in items:
        prefix = it["path"].split("*")[0] if "*" in it["path"] else it["path"]
        it["bytes"] = sum(
            (snap_dir / rel).stat().st_size
            for rel in files
            if (game_spec.root + "/" + rel).startswith(prefix)
        ) if "*" not in it["path"] else 0
    return {"files": len(files), "bytes": total, "checklist": items, "file_list": files}


def prune_snapshots(game_id: str, keep: int = 10) -> list[str]:
    """Delete oldest snapshots beyond `keep`. Returns deleted names."""
    removed = []
    snaps = snapshots(game_id)
    m = read_manifest(game_id)
    for old in snaps[keep:]:
        shutil.rmtree(old, ignore_errors=True)
        m["snapshots"].pop(old.name, None)
        removed.append(old.name)
    if removed:
        manifest_path(game_id).parent.mkdir(parents=True, exist_ok=True)
        manifest_path(game_id).write_text(json.dumps(m, indent=2))
    return removed


def local_client_hint(game_id: str, platform: str = "linux") -> str | None:
    """Path on the *player's machine* where the game reads saves (for restore
    instructions). ICARUS client is Windows-only, so this is a Windows path."""
    if game_id == "icarus":
        return r"%LOCALAPPDATA%\Icarus\Saved"
    return None
