# Deploying wallexbot (shadow mode, single host)

Status: **shadow only.** The bot reads public order books from `api.wallex.ir` (GET only) and simulates fills (`ShadowBroker`); no order is ever sent and no key is needed. Live trading does not exist in this build. An earlier revision of this page was checked by building the image (with a local base image) and running the demo, guardian and healthcheck inside it, offline, read-only and as a non-root user. The in-process guardian of this revision was checked with the test suite only (no image rebuild). Nothing was run for a day against the real API and the systemd units were not started on a live systemd.

## Prerequisites

- Linux host or VM, 1 vCPU, 1 GB RAM, ~1 GB disk. Outbound HTTPS to `api.wallex.ir` only (compose and systemd cannot filter by host; use the host firewall if you need that). No inbound ports.
- Docker route: Docker Engine with Compose v2.24+. Systemd route: Python 3.12, `sqlite3`, systemd.
- Local named volume or local disk for `/data` (SQLite WAL needs shared memory: no NFS/SMB).

## Docker

```bash
git clone <repo> && cd Wallex
docker compose up -d --build          # the shadow bot (guardian in-process); add --profile recorder for recording
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

The guardian runs **inside the bot process** (`bot --mode shadow`, on by default): its own SQLite connection on `bot.db`, the bot's own `ShadowBroker` object, a tick after every bot tick and at least every `--guardian-interval-s` (15 s). It writes a `guardian` heartbeat, engages the kill switch on soft breaches, a balance mismatch or a lost strategy heartbeat, and flattens through the OMS on a hard breach. Stale data on a held symbol is reported (`stale_feed:<symbols>`) and after 120 s follows `stale_action`, but does not engage the kill switch. Only the heartbeat engagement (reason `auto:heartbeat_lost`) is released by the guardian itself, after 3 clear ticks and 60 s; every other engagement waits for an operator (docs/RISK.md). `--interval-s` must be at most 15 s (`max_data_age_s / 2`), else the bot exits 2. In shadow mode this **exercises the guardian logic against simulated fills**; it protects no real money. `--no-guardian` disables it (the healthcheck then stops requiring its heartbeat).

There is no separate guardian service any more (no compose service, no systemd unit). A separate guardian process only makes sense against a live exchange account, which this build does not have: a separate process cannot see the bot's in-process paper or shadow broker. `python -m wallexbot guardian` is a standalone DEMO on its own in-memory mock broker (10000 USDT). It refuses to run without `--demo-db`, and refuses any database that holds bot events or equity rows it did not write, because its equity rows would corrupt the bot's drawdown and balance checks.

## What to monitor

- Health: `docker compose exec bot python /app/deploy/healthcheck.py --db /data/bot.db` (or `make healthcheck DB=...`). One JSON line; exit 0 only if the strategy heartbeat (and, when the run enabled the guardian, the guardian heartbeat) is younger than `--max-heartbeat-age-s` (120) and no `guardian.*` error, `needs_review` audit event or `needs_review` intent is newer than `--alert-window-min` (15). It also reports the run's `mode` and strategy `profile`; `profile: demo` (`exercise_profile: true`) adds a warning. An engaged kill switch **fails** the probe (no new entries is not healthy); `--allow-killswitch` (`make healthcheck HEALTH_ARGS=--allow-killswitch`) reports it as a warning instead, and `killswitch_auto_release: true` marks the guardian's transient engagement. It opens the DB read-only. Docker runs it as the container HEALTHCHECK; with systemd, run it from your monitoring (command in the unit file).
- Shadow journal: `/data/shadow_journal.jsonl` (simulated fills; appears once an order exists). No file after hours means no signals, which is a valid outcome.
- Store tables (read-only): `sqlite3 -readonly bot.db`. Look at `order_intents`, `fills`, `positions_snapshots`, `killswitch_events`, `errors`, `audit_events`.

## Stop safely

- `docker compose stop` / `systemctl stop wallexbot-bot` sends SIGTERM; the bot finishes its current tick and exits (grace 60 s). The in-process guardian stops with it.
- Kill switch: engaging it blocks new entries and never blocks exits. The CLI has no command for it; it is the latest row of `killswitch_events` (`KillSwitch.engage` / `.disengage` in `risk/killswitch.py`), and the guardian (and the bot, after 5 failing ticks) engages it automatically. The healthcheck fails while it is engaged; check `killswitch_reason` before disengaging.

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
- Do not run two bots on one database, and do not point the standalone guardian demo at a bot's database (it refuses).
- Do not deploy `--profile demo`: it is an exercise profile for watching the pipeline, with no claimed edge.
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
