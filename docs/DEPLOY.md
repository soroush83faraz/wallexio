# Deploying wallexbot (shadow mode, single host)

Status: **shadow only.** The bot reads public order books from `api.wallex.ir` (GET only) and simulates fills (`ShadowBroker`); no order is ever sent and no key is needed. Live trading does not exist in this build. This page was checked by building the image (with a local base image) and running the demo, guardian and healthcheck inside it, offline, read-only and as a non-root user; it was **not** run for a day against the real API and the systemd units were not started on a live systemd.

## Prerequisites

- Linux host or VM, 1 vCPU, 1 GB RAM, ~1 GB disk. Outbound HTTPS to `api.wallex.ir` only (compose and systemd cannot filter by host; use the host firewall if you need that). No inbound ports.
- Docker route: Docker Engine with Compose v2.24+. Systemd route: Python 3.12, `sqlite3`, systemd.
- Local named volume or local disk for `/data` (SQLite WAL needs shared memory: no NFS/SMB).

## Docker

```bash
git clone <repo> && cd Wallex
docker compose up -d --build          # the shadow bot; add --profile recorder for market-data recording
docker compose ps                     # STATUS shows (healthy) after ~2 min
docker compose logs -f bot
```

`.env` is optional (shadow needs no secrets). If you create one, copy `.env.example` or `deploy/env.example` and fill only what you need. The image runs as uid 10001 on a read-only root filesystem with `/tmp` as tmpfs, all capabilities dropped, `no-new-privileges`, memory/CPU/pids limits, no published ports. State lives in the named volume `wallex-data` mounted at `/data` (`bot.db`, `shadow_journal.jsonl`, `recorder/`).

## systemd

```bash
sudo useradd --system --home-dir /var/lib/wallexbot --shell /usr/sbin/nologin wallexbot
sudo git clone <repo> /opt/wallexbot && cd /opt/wallexbot
sudo python3.12 -m venv .venv && sudo .venv/bin/pip install -e .   # editable on purpose: config/ is found relative to the source tree
sudo install -d -m 0750 -o root -g wallexbot /etc/wallexbot
sudo install -m 0640 -o root -g wallexbot deploy/env.example /etc/wallexbot/env
sudo cp deploy/systemd/wallexbot-bot.service /etc/systemd/system/    # (+ wallexbot-recorder.service if wanted)
sudo systemctl daemon-reload && sudo systemctl enable --now wallexbot-bot
```

The checkout is root-owned and read-only to the service; the service user can write only `/var/lib/wallexbot` (`StateDirectory`, `ProtectSystem=strict`). Units set `NoNewPrivileges`, `PrivateTmp`, an empty capability set, a syscall filter and `Restart=on-failure`.

## Guardian: read this first

`python -m wallexbot guardian` is a **demo loop** in this build: it watches its own in-memory mock broker (10000 USDT), not the bot's shadow broker, and writes equity high-water marks into the shared database. Against a bot started with `--balance 1000` that looks like a 90% drawdown, so the bot's risk engine blocks its entries (checked with a scratch DB). A bot position would also trigger a balance mismatch and an automatic kill switch. For that reason the guardian is **opt-in** (`docker compose --profile guardian up -d`; `wallexbot-guardian.service` is shipped but not to be enabled) until it can read the shadow broker. If you run it anyway: exactly one per database, start the bot first (no strategy heartbeat for 60 s engages the kill switch), and expect to inspect `errors` and `killswitch_events`.

## What to monitor

- Health: `docker compose exec bot python /app/deploy/healthcheck.py --db /data/bot.db` (or `make healthcheck DB=...`). One JSON line; exit 0 only if the strategy heartbeat is younger than `--max-heartbeat-age-s` (120) and no `guardian.*` error, `needs_review` audit event or `needs_review` intent is newer than `--alert-window-min` (15). An engaged kill switch is reported as a warning; add `--fail-on-killswitch` to make it fail. It opens the DB read-only. Docker runs it as the container HEALTHCHECK.
- Shadow journal: `/data/shadow_journal.jsonl` (simulated fills; appears once an order exists). No file after hours means no signals, which is a valid outcome.
- Store tables (read-only): `sqlite3 -readonly bot.db`. Look at `order_intents`, `fills`, `positions_snapshots`, `killswitch_events`, `errors`, `audit_events`.

## Stop safely

- `docker compose stop` / `systemctl stop wallexbot-bot` sends SIGTERM; the bot finishes its current tick and exits (grace 60 s). Stop the guardian (if any) with it.
- Kill switch: engaging it blocks new entries and never blocks exits. The CLI has no command for it; it is the latest row of `killswitch_events` (`KillSwitch.engage` / `.disengage` in `risk/killswitch.py`), and the guardian engages it automatically. Check `killswitch_engaged` in the healthcheck before assuming the bot is trading.

## Back up the database

```bash
docker compose exec bot python - <<'PY'      # or run sqlite3 on the host path for systemd
import sqlite3; s = sqlite3.connect("file:/data/bot.db?mode=ro", uri=True); d = sqlite3.connect("/data/backup.db"); s.backup(d); d.close()
PY
sqlite3 /var/lib/wallexbot/bot.db ".backup '/var/backups/wallexbot-$(date +%F).db'"      # systemd host
```

Use the online backup, never `cp` of `bot.db` alone (WAL files hold recent rows). The slim image has no `sqlite3` binary, hence the Python form. Copy `shadow_journal.jsonl` and `recorder/` as plain files.

## Upgrade

1. Stop the services; back up the DB (above).
2. `git pull` (check `git log` and `docs/` for schema changes), then `docker compose up -d --build`, or `pip install -e .` in `/opt/wallexbot` and `systemctl restart`.
3. Check `healthcheck` and the logs. State is restored from the DB after a restart.

## Do not

- Do not mount or set Wallex keys. Shadow needs none; never put `WALLEX_API_KEY_TRADE` or `WALLEX_ARM_LIVE` in `.env` or `/etc/wallexbot/env`.
- Do not run two guardians on one database, and do not run two bots on one database.
- Do not start a bot against a database written by another version without reading the changelog and backing it up first: the schema is created, not migrated.
- Do not publish ports, put the volume on network storage or edit `bot.db` by hand (triggers make the audit tables append-only).
- Do not read shadow results as evidence: the fill model is an unvalidated assumption set.

## Going live is a separate, explicit, user-approved step

Nothing in this repo enables it, and this page does not. The gates already in the code and process, all required together (docs/CONFIG_ENV.md):

1. `mode: live` in config, which `load_config` rejects unless the rest holds.
2. `WALLEX_ARM_LIVE=YES_I_UNDERSTAND` in the environment.
3. A `--arm-live` CLI flag. Today it is refused on every command and `bot --mode live` exits 2.
4. `WALLEX_API_KEY_TRADE` present and header-safe (no withdrawal rights).
5. `doctor` green (`WALLEX_API_KEY_READ`, GET only), never yet run against Wallex.
6. Promotion evidence in docs/BACKTEST.md ("Evidence required before live"): backtest, the single test-set evaluation and a 24-48 h shadow run, none of which is complete.
7. An explicit go-ahead from the user in conversation.

The private client is unverified against the real API (docs/WALLEX_INTEGRATION.md). Treat any live attempt as a new project with its own review.
