"""Game registry: per-game save-layout knowledge + FTP auto-detection.

Adding a new game later = adding one GameSpec entry here. Detection scans an
FTP directory listing for the game's signature paths, so plugging in a new
GPORTAL server "just works" once its spec exists.
"""

from __future__ import annotations

import fnmatch
import posixpath
from dataclasses import dataclass, field

import ftp_ops


@dataclass
class PathSpec:
    """A directory on the FTP server that must be captured, and what it means."""

    rel_path: str  # relative to the game's root folder on the server
    role: str  # human label used by the verification checklist
    critical: bool = True  # False = nice to have, missing only warns


@dataclass
class GameSpec:
    game_id: str
    title: str
    # Folder-name signatures used to auto-detect this game from an FTP listing.
    # Keys are relative subpaths whose *existence* identifies the game.
    signatures: tuple[str, ...]
    root: str  # game root folder on the server (relative to FTP root)
    paths: tuple[PathSpec, ...]
    # Filename patterns (within paths[0]) that must be present for a "complete"
    # verdict, e.g. prospect metadata jsons.
    required_patterns: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default_factory=tuple)

    def detect(self, dir_paths: set[str]) -> bool:
        """dir_paths: absolute FTP dir paths ('/Icarus/Config', …). A signature
        matches when its full path exists as a directory."""
        for sig in self.signatures:
            if "/" + sig.strip("/") in dir_paths:
                return True
        return False

    def checklist(self, files: list[str]) -> list[dict]:
        """files: full FTP paths captured. Returns per-component verify items."""
        items = []
        for spec in self.paths:
            prefix = posixpath.join(self.root, spec.rel_path).rstrip("/") + "/"
            matched = [f for f in files if f.startswith(prefix)]
            items.append(
                {
                    "role": spec.role,
                    "path": prefix,
                    "files": len(matched),
                    "bytes": 0,  # filled by caller from manifest
                    "critical": spec.critical,
                    "ok": bool(matched) if spec.critical else True,
                }
            )
        base = posixpath.join(self.root, self.paths[0].rel_path) if self.paths else self.root
        for pat in self.required_patterns:
            hit = any(
                fnmatch.fnmatch(posixpath.basename(f), pat)
                for f in files
                if posixpath.dirname(f) == base
            )
            items.append(
                {
                    "role": f"Required file pattern: {pat}",
                    "path": posixpath.join(base, pat),
                    "files": sum(
                        1
                        for f in files
                        if posixpath.dirname(f) == base
                        and fnmatch.fnmatch(posixpath.basename(f), pat)
                    ),
                    "bytes": 0,
                    "critical": True,
                    "ok": hit,
                }
            )
        return items


ICARUS = GameSpec(
    game_id="icarus",
    title="ICARUS",
    signatures=(
        "Icarus/Config/Saved/PlayerData/DedicatedServer",
        "Icarus/Config/Saved/PlayerData",
    ),
    root="Icarus/Config/Saved",
    paths=(
        PathSpec(
            rel_path="PlayerData/DedicatedServer/Prospects",
            role="World saves (prospect JSON + world state)",
        ),
        PathSpec(
            rel_path="PlayerData/DedicatedServer",
            role="Player progression (per-SteamID folders: talents, bestiary, currency)",
            critical=False,
        ),
        PathSpec(rel_path="Config", role="Server settings (GameSettings.json etc.)", critical=False),
    ),
    required_patterns=("*",),
    notes=(
        "Prospects/ holds one .json per prospect plus its world save data.",
        "Player progression lives in per-SteamID subfolders of PlayerData/DedicatedServer.",
        "To play offline later: copy Prospects -> %LOCALAPPDATA%\\Icarus\\Saved\\Prospects "
        "and the SteamID folder -> %LOCALAPPDATA%\\Icarus\\Saved\\PlayerData\\<your steamid>. "
        "The client is free, so this is the end-to-end test that needs no rented server.",
        "Known quirk: first reconnect after a save move may fail with Error 65 — just retry.",
    ),
)

GAMES: dict[str, GameSpec] = {g.game_id: g for g in (ICARUS,)}


def collect_dirs(ftp, root: str = "/", out: set[str] | None = None, depth: int = 0) -> set[str]:
    """Recursively collect directory paths (bounded depth to stay quick)."""
    if out is None:
        out = set()
    if depth > 6:
        return out
    try:
        entries = ftp_ops.mlsd(ftp, root)
    except Exception:
        return out
    for e in entries:
        if e.is_dir:
            out.add(e.path)
            collect_dirs(ftp, e.path, out, depth + 1)
    return out


def detect_game_from_dirs(ftp) -> GameSpec | None:
    dirs = collect_dirs(ftp)
    for spec in GAMES.values():
        if spec.detect(dirs):
            return spec
    return None


def detect_game(listing) -> GameSpec | None:
    """Back-compat helper: listing = iterable of dir paths."""
    dirs = set(listing) if not isinstance(listing, set) else listing
    for spec in GAMES.values():
        if spec.detect(dirs):
            return spec
    return None
