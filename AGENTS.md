# AGENTS.md — homelab-monitor

Telegram homelab monitor: central bot (Docker on the server) + node agents
(systemd user services). Pure Python 3 standard library, no pip packages.

## Testing and shipping (dev-playbook)

This repo follows the dev-playbook (`~/.claude/skills/dev-playbook/PLAYBOOK.md`).

- One gate: `scripts/check` (compile + unit tests + shellcheck + clean-code audit).
  The pre-push hook runs it (`git config core.hooksPath .githooks`). Never push with `--no-verify`.
- Tests are stdlib `unittest` in `tests/` (`python3 -m unittest discover -s tests`).
- Bugs: write a failing regression test first, then fix.
- Never weaken, skip or delete a test to make it pass.
- Risk class for this repo: system (runs on every machine) + public repo (keep CLI
  flags and env keys backwards compatible in a patch release).
- External tools and APIs (Telegram, Docker socket, yt-dlp) are always faked in
  tests with named fakes (`FakeYtDlp`), never called for real.
- No real paths, IPs, passwords or tokens in code, tests, fixtures or docs:
  they live in the gitignored `monitor.env` / `.env`.

## Clean code (dev-playbook CLEAN-CODE.md)

Language: Python 3.8+, standard library only.
Formatter: none enforced yet (ruff isn't installed on the machines); keep PEP 8.

- Functions 4–20 lines. Files ≤300 lines, hard max 500. `scripts/clean-code-audit`
  lists violations (report only until the audit is clean; existing long functions
  are split when a change touches them, never in a mass refactor).
- One responsibility per module. Layout: `homelab_monitor/<concern>.py`, `tests/`.
- Names specific and searchable (<5 grep hits). Banned: `data`, `handler`, `utils`, `Manager`, `Service`.
- Type hints on every new `def`; dataclasses instead of bare dicts for new structures.
- Early returns, max 2 nested levels. Extract duplicated logic the second time it appears.
- Errors include the offending value and the expected shape
  (`LIKED_MUSIC_DIR '/x' is not a directory`).
- Comments: keep the ones you find. Write WHY, cite issue numbers / SHAs.
  Docstring + one usage example on public functions.
- Dependencies injected via parameters (e.g. `runner=subprocess.run`); tests pass fakes.
- Logging: plain text via `logging` (journald / docker logs read it).
- Defensive code this repo needs (implement these; flag anything else instead of adding it silently):
  timeouts on every HTTP call and subprocess (Telegram, heartbeat, yt-dlp);
  short retries of a failed or unfinished liked-music run (liked_music_loop:
  RETRY_MINUTES, at most MAX_QUICK_RETRIES in a row, then the normal interval);
  waits on lrclib.net "server is busy" answers (lyrics_lrclib: BUSY_WAITS_SECONDS,
  then the run stops and the song is tried next run; the backfill command cools
  down BUSY_COOLDOWN_SECONDS between failing batches, at most MAX_FAILED_BATCHES;
  lyrics errors never count as a failed sync, so they don't trigger quick retries);
  lock file against concurrent liked-music syncs; never overwrite files in the
  music folder (yt-dlp keeps its default no-overwrite behaviour); delete music
  only through liked_music_reconcile.apply_removal_decisions after the owner's
  Telegram answer (opt-in switch, LIKED_MUSIC_MAX_REMOVALS cap, only songs the
  sync indexed and saw unliked, playlists first, plain filenames); never delete
  on detection alone, because YouTube dropping a removed video looks like an unlike;
  graceful "unavailable" instead of a crash when a sensor, socket or log is missing.
- README has a "How it works" architecture diagram; update it when the shape changes.
