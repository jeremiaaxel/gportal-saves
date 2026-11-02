# gportal-saves

Back up, verify, and restore your **GPORTAL game server saves** with one click — built for **ICARUS** (more games welcome).

> **Disclaimer:** this project is AI-generated ("vibe-coded") — built end-to-end with Claude Code from a problem description, tested against a real GPORTAL server, and maintained by a human. If you spot a bug, that's why — and PRs are welcome.

**The problem it solves:** GPORTAL has no pause button. When you stop renting your server, everything you haven't downloaded is **permanently deleted** — worlds, prospect saves, player progression. GPORTAL's own docs say to download your savegame via FTP before the rental ends; their built-in backups live *on* the rented server and die with it.

**This tool turns that into three clicks:**

1. **Backup now** — pulls all save files over FTP into dated local snapshots
2. **Verify** — proves, offline, that every critical save component is present
3. **Restore** — years later, push a snapshot back to a fresh server or into your local game

No config files, no command line beyond starting the server. A local web UI walks you through everything.

## Quick start

```bash
git clone https://github.com/jeremiaaxel/gportal-saves.git
cd gportal-saves
python3 server.py --open        # opens http://127.0.0.1:8777 in your browser
```

Requirements: **Python 3.10+ only — zero pip installs.** Linux, Windows, macOS.

The setup wizard asks for your GPORTAL FTP login (find it in the GPORTAL web UI → *My servers* → your server → **Status** → FTP), or just paste the whole `ftp://user:pass@host:port` line and it parses itself. A live connection test detects the game and probes every save path before your first real backup.

## What gets backed up (ICARUS)

Discovered from a live GPORTAL server — this is the layout that actually ships, including the parts other guides miss:

| Server path (relative to FTP root) | What it is | Critical |
|---|---|---|
| `Icarus/Config/Saved/PlayerData/DedicatedServer/Prospects/` | **Your worlds** — one `.json` per prospect (+ rolling `.backup_*` copies) | ✅ |
| `Icarus/Config/Saved/PlayerData/DedicatedServer/` | **Global progression** — `Accolades.json` (orbital workshop currency) etc. | – |
| `Icarus/Config/Saved/Config/WindowsServer/` | Server settings (`ServerSettings.ini`, `Engine.ini`) | – |

Snapshots land in your chosen archive folder (default: `~/.local/share/Steam/gportal-saves/`, else `~/gportal-saves/`), keep the last 10, and are verified offline via a per-component checklist.

## "How do I know my backup is complete?" — the offline proof

The killer feature, no re-renting needed: the Icarus **client** reads the same save format as the server.

1. **Verify** — one click, checks all critical components exist in the snapshot
2. **Restore → This PC** — remaps server saves into your client's layout (`PlayerData/<your-steamid>/Prospects/` — *not* the server's `DedicatedServer` layout; getting this wrong is why "my world doesn't show up" happens)
3. Launch Icarus **single-player** — if the prospect appears in the load list, your backup is provably complete and playable

Then when you're ready: rent a fresh GPORTAL server, **Restore → Server** (server stopped), restart, connect. First reconnect may fail with **Error 65** — just retry. Your world continues where you left off.

## Features

- **Auto-detection** — identifies the game and its save paths from the FTP listing; manual override with a server-side folder browser for unusual hosting layouts
- **FTP URL paste** — `ftp://user:pass@host:port` parses into fields automatically
- **Credential safety** — FTP login goes into your OS keyring (KDE Wallet / gnome-keyring via Secret Service); falls back to a chmod-600 file in `~/.local/state/` otherwise. Never written into the project, never committed
- **Native folder pickers** — Dolphin / Explorer / Finder dialogs for local paths, real KDE/zenity dialogs, not fake web ones
- **Snapshot management** — dated snapshots, 10-snapshot rotation, per-snapshot verify badges, delete-one or delete-all (double-confirmed danger zone), open-in-file-manager buttons
- **Hard timeouts everywhere** — bounded FTP operations and retry, pulsing busy button states that can't stick
- **Extensible game registry** — one `GameSpec` dataclass per game in `games.py`; detection, backup paths, and verify checklist all derive from it

## Why not just use GPORTAL's backup feature?

GPORTAL's built-in backups (50 GB) are stored **on the rented server itself**. Cancel the rental, and they're gone with it. This tool is the missing piece: a backup that lives on *your* machine and outlives the rental.

## Adding another game

`games.py` is a registry: signature folders (for FTP auto-detection), paths to capture, required file patterns. A new game ≈ 20 lines of dataclass — the UI, verification checklist, and restore logic are game-agnostic. PRs welcome.

## Roadmap

- **Leaner codebase** — keep shrinking the core; the backup/restore path is already stdlib-only and should stay that way
- **Hosted web version** — paste your FTP credentials, request a download, get your saves as a `.zip`; files are wiped from the server within 1–2 hours. Caveat: you'd be handing your FTP login to a third party, which the local version never asks of you.
- **Headless CLI / TUI** — same backup → verify → restore flow for servers, cron jobs, and SSH-only boxes

## Security notes

- Web UI binds to `127.0.0.1` only — nothing is exposed to your network
- The only internet traffic is the FTP connection you configure
- Credentials: OS keyring → `~/.local/state/gportal-saves/config.json` (chmod 600) fallback; nothing secret in the project directory

## Sources & further reading

- GPORTAL wiki: [Icarus server setup](https://www.g-portal.com/wiki/en/icarus/) (FTP, save paths), [backups](https://www.g-portal.com/wiki/en/how-do-i-create-a-backup-of-my-gportal-server/) (live on the server), [cancellation](https://www.g-portal.com/wiki/en/how-do-i-cancel-a-server/) (download before term ends)
- Steam guides: [migrate saves to a dedicated server](https://steamcommunity.com/sharedfiles/filedetails/?id=2908350744), [share save files](https://steamcommunity.com/sharedfiles/filedetails/?id=3661461211)

## Keywords

GPORTAL backup, GPORTAL savegame download, GPORTAL server cancel keep world, ICARUS dedicated server save files, ICARUS backup FTP, ICARUS prospect transfer, ICARUS move server save to single player, ICARUS Error 65, download Icarus world before rental ends, game server save migration
