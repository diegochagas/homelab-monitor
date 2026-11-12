# Homelab Monitor

![Python](https://img.shields.io/badge/Python-3.8%2B-blue)
![License](https://img.shields.io/github/license/diegochagas/homelab-monitor)
![Version](https://img.shields.io/badge/version-1.5.0-blue)

A Telegram bot that watches a small homelab: one always-on **central** server
runs the bot, other Linux machines run a lightweight **agent**, and you get
status on demand (`/status`, `/health`, `/apps`, …) plus automatic alerts
when something breaks — including when a backup silently stops running.

Pure Python 3 standard library. No pip packages, no database server, no
external service besides Telegram.

Companion project: [homelab-backup](https://github.com/diegochagas/homelab-backup)
— the monitor reads its logs to report backup freshness and alert on stale or
failed backups.

---

## How it works

```
┌───────────────────────────────────┐        ┌────────────────────────────────┐
│  Central server (Docker)          │        │  Other machines (agent)        │
│                                   │        │                                │
│  homelab-monitor container        │        │  homelab-monitor-agent.service │
│   ├─ Telegram bot (long polling)  │◀───────┤   heartbeat + metrics every    │
│   ├─ HTTP endpoint :8787          │  LAN / │   60 s (POST /api/v1/heartbeat)│
│   ├─ alert loop every 60 s        │Tailscale                                │
│   └─ SQLite state                 │        │   optional: every 6 h download │
│                                   │        │   new YouTube Music likes      │
│                                   │        │   (yt-dlp → music folder)      │
│                                   │        │   + .m3u8 playlists, unlikes,  │
│                                   │        │   deletes made on a device     │
└───────────────┬───────────────────┘        └────────────────────────────────┘
                │ alerts, replies
                ▼
           Telegram (you)
```

| Piece | Runs on | What it does |
| ----- | ------- | ------------ |
| `homelab_monitor.bot` | Central server, as a Docker container | Telegram commands, alerts, endpoint for agents |
| `homelab_monitor.agent` | Any Linux machine, systemd user service | Pushes metrics (and the last backup result) to the central |
| `homelab_monitor.liked_music` | Inside the agent, optional | Downloads songs newly liked on YouTube Music (and other playlists) into a folder, writes `.m3u8` playlist files next to them, keeps a Jellyfin playlist in step, (opt-in) deletes unliked songs and (opt-in) removes everywhere and dislikes a song you deleted on one device |

The central runs as a container on purpose: appliance-style distros (ZimaOS,
CasaOS, …) keep `/etc` and root's crontab on a system partition that OTA
updates replace, while a Docker container with a restart policy and a data
volume survives updates and reboots.

---

## Features

- ✅ Status of every machine from Telegram: CPU, memory, disks, temperatures,
  uptime, IPs, listening ports, Docker containers
- ✅ Automatic alerts with confirmation, rate limiting and recovery messages
- ✅ Detects stopped, unhealthy and *disappeared* containers
- ✅ Detects offline machines (missed heartbeats)
- ✅ Detects stale or failed backups (reads homelab-backup's logs)
- ✅ Only authorised Telegram users can talk to the bot; agents authenticate
  with per-machine tokens; no shell access of any kind
- ✅ Degrades gracefully: a missing sensor, Docker socket or log is reported
  as unavailable, never a crash
- ✅ One-command installers for the central and for agents
- ✅ Optional: agents download songs you newly like on YouTube Music into a
  local folder

---

## Commands

| Command | Description |
| ------- | ----------- |
| `/servers` | Registered machines and whether they are online |
| `/status [server]` | Quick overview: CPU, memory, worst disk, temperature, Docker, uptime, IP |
| `/health [server]` | Every metric against its limit, backup freshness, problems, active alerts |
| `/backups` | Last result of the server-side and offsite backups |
| `/apps [server]` | Docker containers with status, ports and image |
| `/docker [server]` | Docker summary: stopped, unhealthy and missing containers |
| `/cpu [server]` | Usage, load, cores, top processes |
| `/memory [server]` | Total/used/available, top processes |
| `/disk [server]` | Every mounted filesystem |
| `/temperature [server]` | Every hardware sensor |
| `/network [server]` | Interfaces and IPv4 addresses |
| `/ports` | Listening TCP ports on the central, with the owning process |
| `/unliked` | Songs that left your YouTube Music likes and wait for a decision |
| `/unliked_delete [server]` | Delete those songs from the playlists and the music server |
| `/unliked_keep [server]` | Keep their files and clear the queue |
| `/rename old new` | Rename a machine (its id and token stay the same) |
| `/remove server` | Unregister an agent |
| `/forget container [server]` | Stop tracking a container you removed on purpose |
| `/integrate` | How to add a machine |
| `/restart` | Restart the bot (the central container) |
| `/clear` | Delete the bot's recent messages in the chat |
| `/version`, `/help` | |

Without a server name, commands refer to the central.

Indicators: 🟢 normal · 🟡 warning (≥ 85 % of the limit, or a stopped
container) · 🔴 critical · ⚪ unavailable.

Example `/status`:

```
🖥 server
🟢 All good

⚙️ CPU      3.2% · load 0.38
🧠 Memory   57.3% · 4.4 GB/7.6 GB
💾 Disk     47.0% · /media/DATA
🌡️ Temp     41.0 °C
🐳 Docker   12/12 healthy
⏱️ Uptime   6d 6h 33m
🌐 IP       http://192.168.1.10
```

---

## Alerts

Checks run every `CHECK_INTERVAL` seconds for every machine:

| Alert | Condition |
| ----- | --------- |
| HIGH CPU / MEMORY / TEMPERATURE | above the `ALERT_*` limit |
| DISK FULL | any mount above `ALERT_DISK_PERCENT` (except `DISK_IGNORE`) |
| CONTAINER FAILED | a known container is stopped, unhealthy or gone |
| SERVER OFFLINE | an agent missed `OFFLINE_AFTER_MISSED` heartbeats |
| SERVER BACKUP / OFFSITE BACKUP | a backup stage failed, or its last success is older than `BACKUP_MAX_AGE_HOURS` |

A problem must be seen `ALERT_CONFIRMATIONS` times in a row (default 2)
before an alert is sent. While it persists it is repeated at most every
`ALERT_REPEAT_SECONDS` (default 1 h). When it clears, a 🟢 RECOVERED message
follows.

Containers are remembered once seen. If you remove one on purpose, answer
the alert with `/forget name [server]`.

A node listed in `BACKUP_ONLY_NODES` only gets the backup alerts above —
useful for a laptop or other machine that isn't expected to stay online.

---

## Requirements

**Central:** Linux with Docker (Compose optional), `curl`, a bot token from
[@BotFather](https://t.me/BotFather).

**Agent:** Linux with `systemd`, Python 3.8+, `curl`. Nothing is installed
besides a user service pointing at this checkout.

---

## Install — central

1. Create a bot with [@BotFather](https://t.me/BotFather) (`/newbot`) and copy
   the token.
2. Send any message to the bot, then open
   `https://api.telegram.org/bot<TOKEN>/getUpdates` — `message.from.id` is your
   user id and `message.chat.id` the chat id (the same number in a private
   chat).
3. On the server:

   ```bash
   git clone https://github.com/diegochagas/homelab-monitor.git
   cd homelab-monitor
   ./install-central.sh
   ```

   The installer asks for the token, chat id, allowed user ids, server name
   and data directory; generates a `REGISTRATION_KEY`; writes `monitor.env`
   and `.env`; builds the image; starts the container and checks the agent
   endpoint. Re-run it after `git pull` to upgrade.

4. Send `/help` to the bot.

Review `monitor.env` afterwards — in particular `SERVER_BACKUP_LOG` (the log
of the server-side backup job, if any) and `DISK_IGNORE`.

Logs: `sudo docker logs -f homelab-monitor`.

---

## Install — agent

On each machine you want to see in `/servers`:

```bash
git clone https://github.com/diegochagas/homelab-monitor.git
cd homelab-monitor
./install-agent.sh
```

It asks for the machine name, the central URL (`http://<central-ip>:8787`),
the registration key (printed by the central installer, also in its
`monitor.env`) and, optionally, the directory with homelab-backup's logs.
It then generates a unique `AGENT_ID`/`AGENT_TOKEN`, writes `monitor.env`,
installs `homelab-monitor-agent.service` as a *user* service, enables
lingering so it runs without a login session, and sends a first heartbeat.
The machine appears in `/servers` within a minute and the bot announces it.

Useful:

```bash
systemctl --user status homelab-monitor-agent
journalctl --user -u homelab-monitor-agent -f
python3 -m homelab_monitor.agent monitor.env --once   # one heartbeat, then exit
```

---

## Configuration

Everything lives in two gitignored files next to the code:

- `monitor.env` — the application settings (copy of `monitor.env.example`)
- `.env` — Docker Compose variables for the central (copy of `.env.example`)

`MONITOR_DATA_DIR`, `BACKUP_LOG_DIR`, and `SERVER_BACKUP_LOG` are ordinary
data/log paths: point them at visible persistent storage appropriate for that
machine. The same files also contain Telegram and registration secrets, so they
remain gitignored and must not be treated as non-secret path configuration.

| Key | Used by | Meaning |
| --- | ------- | ------- |
| `NODE_NAME` | both | Name shown in Telegram |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` | central | Bot token and where alerts go |
| `TELEGRAM_ALLOWED_USER_IDS`, `TELEGRAM_ALLOWED_CHAT_IDS` | central | Who may run commands; everyone else is ignored |
| `REGISTRATION_KEY` | both | Shared secret an agent must present on its first heartbeat |
| `LISTEN_HOST`, `LISTEN_PORT` | central | Agent endpoint (default `0.0.0.0:8787`) |
| `SERVER_BACKUP_LOG` | central | Server-side backup log for `/backups` and stale alerts |
| `SERVER_FAILOVER_FLAG` | central | homelab-backup's `failover.active` file; while it exists the server backup shows as paused by the failover |
| `DISK_IGNORE` | both | Mounts never alerted on (e.g. a read-only root that is always full) |
| `BACKUP_ONLY_NODES` | central | Node names that only get backup alerts (no offline/CPU/memory/disk/temp/container checks) — for intermittently-online machines like a laptop |
| `CENTRAL_URL`, `AGENT_ID`, `AGENT_TOKEN`, `HEARTBEAT_INTERVAL` | agent | Where and how to report |
| `BACKUP_LOG_DIR` | agent | homelab-backup `backup.sh` logs for `/backups` and stale alerts |
| `CHECK_INTERVAL`, `ALERT_CONFIRMATIONS`, `ALERT_REPEAT_SECONDS`, `OFFLINE_AFTER_MISSED` | central | Alert cadence |
| `ALERT_CPU_PERCENT`, `ALERT_MEMORY_PERCENT`, `ALERT_DISK_PERCENT`, `ALERT_TEMP_C`, `BACKUP_MAX_AGE_HOURS` | central | Thresholds |
| `MONITOR_DATA_DIR` (`.env`) | central | Host directory for the SQLite database |

Apply changes with `./install-central.sh` (central) or
`systemctl --user restart homelab-monitor-agent` (agent).

### Running without Docker

The bot is a plain Python module, so it also runs directly:

```bash
python3 -m homelab_monitor.bot monitor.env     # central
python3 -m homelab_monitor.agent monitor.env   # agent
```

Set `DATA_DIR` for the database location (default `./data`). Leave
`HOST_ROOT` unset when not in a container.

---

## Backups integration

The monitor reads the logs written by
[homelab-backup](https://github.com/diegochagas/homelab-backup):

- `SERVER_BACKUP_LOG` (central) — the log of `zimaos/backup.sh`, the
  server-side job
- `BACKUP_LOG_DIR` (agent) — the `logs/` directory of `backup.sh`, the
  offsite pull

From them `/backups` and `/health` show the last run, its result and the
per-folder summary, and the alert loop raises SERVER BACKUP / OFFSITE BACKUP
when a run failed or the last success is older than `BACKUP_MAX_AGE_HOURS`.
Dry runs are ignored.

With `SERVER_FAILOVER_FLAG` set, a homelab-backup failover is reported for what
it is: SERVER BACKUP says "PAUSED — failover active since …" (still an alert,
since no nightly backup runs until the failback) instead of a misleading
"never finished, check the scheduler" read from the backup drive's copy of the log.

The "backup finished" messages themselves are sent by homelab-backup's own
`notify.sh`, independently of this bot, so they arrive even when the monitor
is down.

---

## Security

- Commands are only accepted from `TELEGRAM_ALLOWED_USER_IDS` /
  `TELEGRAM_ALLOWED_CHAT_IDS`; other senders are logged and ignored.
- No command runs arbitrary shell. Every command is a fixed, read-only query
  (plus renames/unregistering in the bot's own database, and `/restart`,
  which just exits the process for the container's `restart: unless-stopped`
  policy to relaunch it — no Docker socket write, no shell).
- Agents register with `REGISTRATION_KEY` and afterwards authenticate with
  their own per-machine token; comparisons are constant-time.
- The central container mounts the host filesystem and the Docker socket
  read-only and only uses `GET` endpoints of the Docker API. It runs with
  `pid: host`, `CAP_SYS_PTRACE` and `apparmor=unconfined` purely so `/ports`
  can map sockets to host processes — it never writes to the host.
- The agent endpoint is plain HTTP meant for a LAN or a Tailscale network.
  Don't expose port `8787` to the Internet.
- `monitor.env` holds the bot token and keys: keep it `chmod 600` and never
  commit it (it is gitignored).

---

## Liked music sync

An agent can keep a local copy of your YouTube Music **Liked Music**
playlist. Set `LIKED_MUSIC_DIR` in the agent's `monitor.env` and restart it:
every `LIKED_MUSIC_INTERVAL_HOURS` (default 6) it runs yt-dlp, which reads the
cookies of the browser signed in to YouTube Music (`LIKED_MUSIC_BROWSER`,
default `firefox`), walks the playlist newest first and stops at the first song
already in the folder's `.archive.txt`. Songs are saved in their original
format (Opus or m4a) with cover art and tags, named `Artist - Title`.

- Nothing is overwritten. Unliked songs are deleted only when you opt in
  (see below); otherwise unliking a song keeps the file.
- Videos YouTube no longer serves are skipped and retried on the next run.
- A run that fails (often the network isn't up yet right after boot) or
  leaves a song a server hasn't indexed is retried after 15 minutes instead
  of the full interval, up to 4 times in a row.
- A lock file stops two syncs from running at once in the same folder.
- The last result (time, new songs, uploads, error) travels in the
  heartbeat as `liked_music`.
- Optional upload to a music server: with `LIKED_MUSIC_PUSH_TARGET`
  (`user@server:/path/`) the songs are moved there with rsync over SSH after
  each sync. A local file is removed only once rsync has copied it, a failed
  upload is retried on the next run, and the archive stays in
  `LIKED_MUSIC_DIR`, which then works as a staging folder.
- Needs `yt-dlp`, `ffmpeg` and a JavaScript runtime for yt-dlp (deno); put
  the runtime's directory in `LIKED_MUSIC_EXTRA_PATH` because the user
  service doesn't see your shell's PATH.

### Playlists and unlikes

Each download is recorded in `.library-index.json` (video id → file) in
`LIKED_MUSIC_DIR`. Two optional steps run after every sync:

- **New likes join the Jellyfin playlist.** With `LIKED_MUSIC_JELLYFIN_*` set
  they're appended to that playlist. It isn't reordered (Jellyfin's move
  endpoint refuses API keys); the `.m3u8` file below has the right order. A
  song Jellyfin hasn't indexed yet stays queued in `.playlist-pending.json`
  and the 15-minute retry adds it once it's indexed.
- **Unlikes wait for your answer** (`LIKED_MUSIC_REMOVE_UNLIKED=true`). The
  agent lists the whole Liked Music playlist and compares it with the list it
  saved last time (`.liked-ids.json`). A song that dropped out goes into
  `.removal-pending.json`; nothing is deleted. The heartbeat reports the
  queue and the bot sends one Telegram message per new batch:
  - `/unliked` shows what is waiting.
  - `/unliked_delete [server]` removes those songs from every Jellyfin
    playlist and the `.m3u8` files, deletes their files on the push target
    (or in `LIKED_MUSIC_DIR`) and drops their archive lines, so liking one
    again downloads it again. Copies synced from the folder (Syncthing) and
    players that sync from the server drop them on their next sync.
  - A song still in a mirrored playlist (below) is never queued: unliking
    it keeps the file.
  - `/unliked_keep [server]` keeps the files and clears the queue.
  - Liking a song again before you answer takes it out of the queue.

  Why ask: YouTube can also drop a removed or private video from the list,
  and that looks exactly like an unlike. Such a file can't be downloaded
  again, so a person decides.

### Playlist files and mirrored playlists

Playlists live as plain `.m3u8` files next to the songs, so every player that
reads M3U (Poweramp or Musicolet on a phone, Strawberry on a desktop,
Navidrome, …) and every synced copy of the folder (Syncthing) has them, in
order, with no server in between.

- `LIKED_MUSIC_M3U_NAME=YouTube Liked` writes `YouTube Liked.m3u8`: the
  library's Liked Music songs, newest like first. The agent lists Liked
  Music on every run for it (it does that anyway with the unlike sync).
- `LIKED_MUSIC_PLAYLISTS=Christians=https://music.youtube.com/playlist?list=…`
  mirrors another YouTube Music playlist (several: `Name=url;Other=url`).
  Each run lists it, downloads the songs the library doesn't have yet and
  writes `Christians.m3u8` in the playlist's own order. Manage the playlist
  on YouTube Music; the file follows. Its songs count as wanted: unliking
  one never queues it for deletion. Removing a song from the mirrored
  playlist only drops it from the file; the library keeps it.
- `Christians.extra.m3u8` in `LIKED_MUSIC_DIR` (one library filename per
  line, `#` comments allowed) appends songs by hand, for videos YouTube
  dropped that are still in the library and therefore can't be put back in
  the YouTube Music playlist. They're protected from the unlike removal too.
- Files are written over SSH into the push target (or into `LIKED_MUSIC_DIR`
  without one), only when their content changed (`.m3u-written.json`), so a
  run with nothing new doesn't make every synced device re-download them.
  Mirror or file trouble is reported as `playlist_error` and never counts as
  a failed sync.

### Lyrics

With `LIKED_MUSIC_LYRICS=true` (plus the Jellyfin settings and a push target)
each run looks up lyrics for up to `LIKED_MUSIC_LYRICS_PER_RUN` (default 150)
songs on [lrclib.net](https://lrclib.net), a free open lyrics database, and
writes `<song name>.lrc` next to the audio file on the server. Jellyfin (so
Finamp too), Poweramp and most desktop players read that sidecar: synced
lyrics scroll with the song, plain ones show as text.

- The lookup uses the title, artist, album and length Jellyfin read from the
  file, so a song is tried once Jellyfin has scanned it. A search result only
  counts when its length is within 3 seconds of the file's, so a cover or a
  live version doesn't get mistimed lyrics from another recording.
- lrclib often answers "server is busy" for hours. Lookups then pause until
  the next run; this never counts as a failed sync. The backfill command
  below cools down 15 minutes between busy batches for up to a day.
- Songs without a match (instrumentals, covers, parodies) are retried after
  30 days. Lookups are recorded in `.lyrics-checked.json` in `LIKED_MUSIC_DIR`.
- Confirmed deletes of unliked songs remove their `.lrc` too.
- To fill in the whole library at once instead of 150 songs per run:

  ```bash
  python3 -m homelab_monitor.liked_music_lyrics monitor.env --limit 5000
  ```

### Deleting a song on a phone

If Syncthing copies the library to your phones and laptops, deleting a song
on one of them can remove it everywhere and dislike it on YouTube Music
(which also takes it out of Liked Music):

```
phone A: delete song ──Syncthing──▶ music server (Send Only: keeps the file,
                                     lists it as "deleted on another device")
agent: reads that list ─▶ Jellyfin playlists ─▶ rm on the server ─▶ dislike
music server ──Syncthing──▶ phone B, laptop: file deleted
```

Set it up once:

1. On the music server the Syncthing folder stays **Send Only**, so nothing a
   device does reaches the library without the agent.
2. On every device whose deletes should count, set the folder type to
   **Send & Receive**. A Receive Only device never tells anyone about its
   deletes, so nothing happens for it.
3. In the agent's `monitor.env` set `LIKED_MUSIC_SYNCTHING_URL` (the server's
   Syncthing address), `LIKED_MUSIC_SYNCTHING_API_KEY` (Actions > Settings >
   API Key) and `LIKED_MUSIC_SYNCTHING_FOLDER` (the Folder ID, not the label).

The agent checks on every sync and every `LIKED_MUSIC_SYNCTHING_CHECK_MINUTES`
(default 10) in between. There is no Telegram question: deleting the file was
your decision already.

- Only songs the sync put in the library count. Anything else a device
  deleted, added or changed (a `.lrc`, a playlist file, a photo) is ignored
  and stays as it is on the server; Syncthing shows the folder as
  "Out of Sync" there until you press **Override Changes**.
- More than `LIKED_MUSIC_MAX_REMOVALS` songs deleted at once removes nothing
  and is reported as the sync's error: that is a wiped phone folder far more
  often than a decision.
- The dislike is sent with the cookies of the browser the sync already reads
  (`LIKED_MUSIC_BROWSER`), exported by yt-dlp into a private temporary folder
  that is removed right after. If it fails (signed out, offline) the file is
  gone anyway and the dislike is retried on every run
  (`.dislike-pending.json`).
- `LIKED_MUSIC_DISLIKE_DELETED=false` removes the files and leaves the rating
  alone. The song then stays liked and is not downloaded again.
- A deleted song that is still in a mirrored playlist is not downloaded again
  (`.device-deleted.json`); liking it again later brings it back.

Safety rails: only songs the sync downloaded *and* saw in Liked Music count,
so songs that are in other playlists only are never touched. The first
listing only saves the list. Nothing is queued when the listing fails or when
more than `LIKED_MUSIC_MAX_REMOVALS` (default 25) songs vanish at once, which
usually means a truncated listing rather than real unlikes. A failed playlist
removal deletes nothing and keeps the song queued; the bot repeats your answer
on every heartbeat until the agent has carried it out.

Run it once by hand:

```bash
python3 -m homelab_monitor.liked_music monitor.env --once
```

---

## Project structure

```
homelab-monitor/
├── homelab_monitor/
│   ├── bot.py          # central: Telegram polling, alert loop, wiring
│   ├── agent.py        # agent: heartbeat loop
│   ├── liked_music.py  # agent: download new YouTube Music likes, one run's steps
│   ├── liked_music_settings.py  # agent: sync settings and run result
│   ├── liked_music_sources.py  # agent: mirror other YouTube Music playlists
│   ├── liked_music_m3u.py  # agent: .m3u8 playlist files next to the songs
│   ├── liked_music_push.py  # agent: upload them to the music server
│   ├── liked_music_loop.py  # agent: sync timer + Telegram decision inbox
│   ├── liked_music_reconcile.py  # agent: playlist adds, unlike queue, confirmed deletes
│   ├── unlike_confirmations.py  # central: asks on Telegram, relays the answer
│   ├── liked_music_servers.py  # agent: builds the clients from the config
│   ├── liked_music_ytdlp_output.py  # agent: parse a yt-dlp run's output
│   ├── liked_music_state.py  # agent: index, liked list, playlist lists, queue files
│   ├── liked_music_remote.py  # agent: list Liked Music, delete over SSH
│   ├── liked_music_lyrics.py  # agent: lyrics sidecars for the library
│   ├── lyrics_lrclib.py  # agent: lrclib.net lookups with length matching
│   ├── jellyfin_playlists.py  # agent: Jellyfin playlist API
│   ├── http_json.py    # JSON-over-HTTP helper with timeouts
│   ├── commands.py     # /command handlers
│   ├── formatting.py   # Telegram message rendering
│   ├── alerts.py       # confirmation / rate limit / recovery logic
│   ├── metrics.py      # /proc, /sys, Docker and backup-log collectors
│   ├── dockerapi.py    # Docker Engine API over the Unix socket
│   ├── server.py       # HTTP endpoint for agents
│   ├── state.py        # SQLite persistence
│   └── config.py       # environment-driven configuration
├── tests/              # unittest suite (python3 -m unittest discover -s tests)
├── scripts/check       # the single validation command (pre-push hook)
├── install-central.sh
├── install-agent.sh
├── homelab-monitor-agent.service
├── Dockerfile
├── docker-compose.yml
├── monitor.env.example
├── .env.example
├── AGENTS.md
├── LICENSE
└── README.md
```

---

## Troubleshooting

| Symptom | Check |
| ------- | ----- |
| Bot doesn't answer | `sudo docker logs homelab-monitor` — "unauthorized command" means your user id is not in `TELEGRAM_ALLOWED_USER_IDS` |
| Agent never shows up | `journalctl --user -u homelab-monitor-agent` — HTTP 403 means a wrong `REGISTRATION_KEY`; "unreachable" means the central URL/port |
| Machine shown offline although it's up | The agent service is stopped or the machine was asleep; it recovers on the next heartbeat |
| `/temperature` says no sensor | Nothing under `/sys/class/hwmon` — normal on some VMs; not an error |
| `/ports` shows no process names | The container needs `CAP_SYS_PTRACE` and `apparmor=unconfined` (both set by the compose file) |
| Stale SERVER BACKUP alert | The server-side backup job stopped running — check its scheduler (`sudo crontab -l`) |
| Liked music: "playlist/unlike sync skipped" | A `LIKED_MUSIC_JELLYFIN_*` value is empty while its URL is set; the message names it |
| Liked music: "songs left Liked Music at once" | More than `LIKED_MUSIC_MAX_REMOVALS` vanished in one listing; nothing was deleted. If you really unliked that many, raise the limit for one run |
| Liked music not downloading | `python3 -m homelab_monitor.liked_music monitor.env --once` — "Sign in to confirm" means the browser's YouTube session expired (open YouTube Music in it once); "not found" means set `LIKED_MUSIC_YTDLP` |
| Container name already in use | The installer replaces a stray `homelab-monitor` container automatically; otherwise `sudo docker rm -f homelab-monitor` |

## Upgrading

```bash
git pull
./install-central.sh                              # central (rebuilds the image)
systemctl --user restart homelab-monitor-agent    # each agent
```

## License

MIT — see [LICENSE](LICENSE).
