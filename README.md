# gportal-saves

One-command backup / restore / verify for GPORTAL game-server saves, with
GPORTAL's own docs as the source of truth for *what* to copy (FTP + save
paths) and a local web UI as the *how*.

**Why**: GPORTAL has no pause. When your rental ends, anything you haven't
downloaded is gone. Their docs explicitly say: download via FTP before the
term ends. This tool turns that into: click **Backup now**, done.

## First run

```bash
python3 server.py --open        # serves http://127.0.0.1:8777
```

The web UI walks you through a one-time setup wizard:

1. Paste your FTP login line (`ftp://user:pass@host:port` — from GPORTAL →
   *My servers* → your server → **Status** → FTP) or fill the labeled fields,
   then **Test connection** — it detects ICARUS and probes every save path live.
2. Optionally point it at your Icarus client save folder (auto-detected for
   Steam/Proton installs) to enable the offline end-to-end check.
3. **Save** — credentials go into your system keyring (KDE Wallet /
   gnome-keyring via the Secret Service API); if no keyring exists they fall
   back to `~/.local/state/gportal-saves/config.json` with owner-only
   permissions. Nothing is written inside the project.

Everything is stdlib Python — no pip installs.

## What gets backed up (ICARUS)

| Server path (relative to FTP root) | What it is |
|---|---|
| `Icarus/Config/Saved/PlayerData/DedicatedServer/Prospects/` | **Worlds** — prospect JSON + world state (critical) |
| `Icarus/Config/Saved/PlayerData/DedicatedServer/` | **Player progression** — per-SteamID folders: talents, bestiary, currency |
| `Icarus/Config/Saved/Config/` | Server settings (GameSettings.json etc.) |

Snapshots land in `~/.local/share/gportal-saves/icarus/snapshots/<UTC time>/`
with a cumulative `manifest.json`. The last 10 are kept; older are pruned.

## "Are we sure we have everything?" — the three checks

1. **Verify (offline)** — checks every critical component is present in the
   latest snapshot (world files, player data, required patterns). No server
   needed.
2. **End-to-end test (the real proof, needs no re-rent)** — the ICARUS client
   reads the *same folder layout* as the server. Use
   **Copy → client Saved folder** with `client_dir` pointed at
   `%LOCALAPPDATA%\Icarus\Saved` on your Windows box, then launch Icarus
   single-player: if the prospect shows in your load list, the backup is
   complete and playable. Delete the copies afterwards if unwanted.
3. **Restore to a fresh server later** — click **Restore → server** (server
   stopped), restart, connect. First reconnect may fail with Error 65; retry.

## Restore semantics

- Push to **server** uploads the snapshot over the server's save paths
  (parents auto-created). Stop the server in the GPORTAL UI first.
- Push to **client** copies to a local folder for the end-to-end test or
  offline play.

## Extending to other games

`games.py` holds one `GameSpec` per game: signature folders (used for
auto-detection from an FTP listing), the paths to capture, and required file
patterns. Adding a game = one dataclass entry; the UI and verification
checklist come free.

## Security notes

- The web UI binds to `127.0.0.1` only.
- Credentials live in your system keyring (Secret Service API — KDE Wallet,
  gnome-keyring), falling back to `~/.local/state/gportal-saves/config.json`
  (chmod 600) when no keyring is available. Nothing secret is stored in the
  project directory.
- The app never talks to the internet except the FTP connection you
  configured.

## Sources

- GPORTAL wiki: [Icarus](https://www.g-portal.com/wiki/en/icarus/) (save path,
  FTP), [Backups](https://www.g-portal.com/wiki/en/how-do-i-create-a-backup-of-my-gportal-server/)
  (built-in backups live *on* the server), [Cancellation](https://www.g-portal.com/wiki/en/how-do-i-cancel-a-server/)
  (download via FTP before term ends; no pause).
- Steam guides: [migrate local → dedicated](https://steamcommunity.com/sharedfiles/filedetails/?id=2908350744),
  [share saves](https://steamcommunity.com/sharedfiles/filedetails/?id=3661461211)
  (client Prospects folder layout, Error 65 quirk).
