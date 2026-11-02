# gportal-saves

Back up, verify, and restore **GPORTAL game-server saves** — built for **ICARUS** (more games welcome).

> **Disclaimer:** this project is AI-generated ("vibe-coded") — built with Claude Code from a problem description, tested against a real GPORTAL server, maintained by a human. Bugs are expected; PRs welcome.

**The problem:** GPORTAL has no pause. When a rental ends, anything not downloaded is deleted — worlds, prospect saves, player progression. GPORTAL's docs say to download via FTP before the term ends; their built-in backups live on the rented server and are lost with it.

**What this tool does:**

1. **Backup** — downloads all save files over FTP into dated local snapshots
2. **Verify** — checks offline that every expected save component is present in a snapshot
3. **Restore** — uploads a snapshot to a (new) server, or copies it into a local Icarus install for offline play

Local web UI, one-time setup wizard, no config files.

## Quick start

```bash
git clone https://github.com/jeremiaaxel/gportal-saves.git
cd gportal-saves
python3 server.py --open        # serves http://127.0.0.1:8777 and opens it
```

Requires Python 3.10+ (stdlib only, no pip installs). Linux, Windows, macOS.

Setup: the wizard asks for the GPORTAL FTP login (web UI → *My servers* → your server → **Status** → FTP), or paste the whole `ftp://user:pass@host:port` line and it parses. Test connection detects the game and probes the save paths before the first backup.

## What gets backed up (ICARUS)

Layout taken from a live GPORTAL server (November 2026):

| Server path (relative to FTP root) | Contents | Critical |
|---|---|---|
| `Icarus/Config/Saved/PlayerData/DedicatedServer/Prospects/` | Worlds: one `.json` per prospect + rolling `.backup_*` copies | ✅ |
| `Icarus/Config/Saved/PlayerData/DedicatedServer/` | Global progression: `Accolades.json` (orbital workshop currency) | – |
| `Icarus/Config/Saved/Config/WindowsServer/` | Server settings (`ServerSettings.ini`, `Engine.ini`) | – |

Snapshots go to a configurable archive folder (default: `~/.local/share/Steam/gportal-saves/`, else `~/gportal-saves/`); the last 10 are kept, older ones pruned.

## Checking a backup without re-renting

Verify confirms the files are present, but the only real test is loading the world. The Icarus client reads the same prospect format as the server, so:

1. **Verify** — checklist per component (worlds, progression, settings)
2. **Restore → This PC** — copies the snapshot's prospects into the client layout (`PlayerData/<steamid>/Prospects/`, not the server's `DedicatedServer/` layout — they differ)
3. Launch Icarus single-player — if the prospect appears in the load list, the backup works

Later: rent a fresh server, **Restore → Server** (server stopped), restart, connect. The first reconnect may fail with Error 65; retry.

## Notes

- **Auto-detection** — finds the game and save paths from the FTP listing; manual override with an FTP folder browser for other hosting layouts
- **FTP URL paste** — `ftp://user:pass@host:port` fills the fields
- **Credentials** — OS keyring (KDE Wallet / gnome-keyring via Secret Service), fallback `~/.local/state/gportal-saves/config.json` (chmod 600); nothing secret in the project directory
- **Folder pickers** — native OS dialogs (kdialog/zenity/tkinter/osascript) for local paths
- **Snapshot management** — rotation, per-snapshot verify status, delete one/all (double-confirmed), open-in-file-manager
- **Timeouts** — FTP operations are time-bounded with one retry; UI busy states clear on a timer
- **Game registry** — one `GameSpec` dataclass per game in `games.py`; detection, paths, and checklist derive from it

## Why not GPORTAL's built-in backups?

They're stored on the rented server (50 GB quota). Cancel the rental and they're gone. This tool keeps the backup on your machine.

## Adding another game

Add a `GameSpec` to `games.py`: signature folders (for detection), paths to capture, required file patterns. The UI, checklist, and restore logic are game-agnostic.

## Roadmap

- Leaner codebase
- Hosted web version: paste FTP credentials, request a download, receive a `.zip`; server wipes files within 1–2 hours. Caveat: you'd give your FTP login to a third party, which the local version never asks.
- Headless CLI / TUI for cron and SSH-only machines

## Security

- Web UI binds to `127.0.0.1` only
- Only network traffic is the FTP connection you configure
- Credentials in OS keyring → `~/.local/state/gportal-saves/config.json` (chmod 600) fallback

## Sources

- GPORTAL wiki: [Icarus server setup](https://www.g-portal.com/wiki/en/icarus/) (FTP, save paths), [backups](https://www.g-portal.com/wiki/en/how-do-i-create-a-backup-of-my-gportal-server/), [cancellation](https://www.g-portal.com/wiki/en/how-do-i-cancel-a-server/) (download before term ends)
- Steam guides: [migrate saves to a dedicated server](https://steamcommunity.com/sharedfiles/filedetails/?id=2908350744), [share save files](https://steamcommunity.com/sharedfiles/filedetails/?id=3661461211)

## Keywords

GPORTAL backup, GPORTAL savegame download, GPORTAL server cancel keep world, ICARUS dedicated server save files, ICARUS backup FTP, ICARUS prospect transfer, ICARUS move server save to single player, ICARUS Error 65, download Icarus world before rental ends, game server save migration
