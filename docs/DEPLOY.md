# Deploying wallexbot (shadow mode, single host)

Status: **shadow only.** The bot reads public order books from `api.wallex.ir` (GET only) and simulates fills (`ShadowBroker`); no order is ever sent and no key is needed. Live trading does not exist in this build. **Verified on this revision** (one Linux host, Docker 29 from snap): the shipped Dockerfile builds (base pulled through a registry mirror, see below). In the image, offline (`--network none`), read-only root, `--cap-drop ALL`, uid 10001: `demo`, `bot --mode mock --replay-from`, and `bot --mode live` (refused, exit 2). With network, the default CMD (shadow) ran 160 s, `HEALTHCHECK` turned `healthy`, `docker stop` ended it with exit 0 in about 2 s and `PRAGMA integrity_check` on the DB was `ok`. `docker compose up -d` for the bot service behaved the same (see the `no-new-privileges` caveat), and `record` and `history` ran in the image against the real public API. A non-editable wheel was installed into a fresh venv outside the repo and ran `demo`, a config load and the live refusal. The systemd units were exercised only as `systemd-run --user` transient units carrying the sandbox properties a user manager supports (it cannot apply `CapabilityBoundingSet=`), not as the system units themselves. **Not verified:** a run longer than a few minutes against the real API, any real fill, the system-level units, the compose `recorder` profile.

## Prerequisites

- Linux host or VM, 1 vCPU, 1 GB RAM, ~1 GB disk. Outbound HTTPS to `api.wallex.ir` only (compose and systemd cannot filter by host; use the host firewall if you need that). No inbound ports.
- Docker route: Docker Engine with Compose v2.24+. Systemd route: Python 3.12, `sqlite3`, systemd.
- Local named volume or local disk for `/data` (SQLite WAL needs shared memory: no NFS/SMB).

## Docker

```bash
git clone <repo> && cd Wallex
docker compose up -d --build          # the shadow bot (guardian in-process); add --profile recorder for recording
# Docker Hub blocked/denied here? Build the same pinned tag through a registry mirror:
BASE_IMAGE=docker.arvancloud.ir/library/python:3.12.11-slim-bookworm docker compose up -d --build
# plain docker: docker build --build-arg BASE_IMAGE=<mirror>/library/python:3.12.11-slim-bookworm -t wallexbot:shadow .
docker compose ps                     # STATUS shows (healthy) after ~2 min
docker compose logs -f bot
```

`.env` is optional (shadow needs no secrets). If you create one, copy `.env.example` or `deploy/env.example` and fill only what you need. The image runs as uid 10001 on a read-only root filesystem with `/tmp` as tmpfs, all capabilities dropped, `no-new-privileges`, `init: true` (tini as PID 1, so a SIGTERM during interpreter start-up is not ignored), memory/CPU/pids limits, no published ports. State lives in the named volume `wallex-data` mounted at `/data` (`bot.db`, `shadow_journal.jsonl`, `recorder/`). The package is installed non-editable and carries its own default config; `WALLEXBOT_CONFIG=/path.yaml` overrides it (docs/CONFIG_ENV.md; mount the file read-only and set the variable in `.env`).

**`no-new-privileges` caveat.** On the tested host (snap Docker) every container with `no-new-privileges:true` failed at start (`exec /sbin/docker-init: operation not permitted`; `docker run --security-opt no-new-privileges:true python:3.12-slim true` fails too), so it is a host problem, not the image. If you see it, use the shipped override `docker-compose.snap.yml` (`make docker-up-snap`, or `docker compose -f docker-compose.yml -f docker-compose.snap.yml up -d --build`). It resets `security_opt` for both services and changes nothing else (needs Compose 2.24+ for `!reset`). Move to a non-snap Docker when you can; do not weaken the shipped base file.

## Pre-deploy smoke test

`make smoke` (or `.venv/bin/python deploy/smoke_shadow_fill.py SCRATCH_DIR`) closes the gap that a strict-profile shadow run usually shows no signal, so no fill path has run in a live loop. It runs the real stack in shadow mode on the live public order book (public GETs only): `PublicFeed` and the bot's own universe filter, `Bot`, sizing, `RiskEngine`, `OMS`, `ShadowBroker`, the in-process guardian, the E4 exit policy and the audit store, on a throwaway database in the scratch directory. Only two things are test-only: a `ForcedSignalEngine` injected where `MomentumSignalEngine` normally goes (it emits ONE signal for BTCUSDT, or the next most liquid symbol that passed the filter, on the first closed 15m bar, stop = entry - 1.5 ATR), and a `ForcedExitPolicy` that wraps E4 and adds one take-profit level after `--hold-s` (45 s) so the exit does not wait hours. The entry and exit still pass sizing, risk, the OMS and the shadow book's real spread and depth. It prints the banner `SMOKE TEST - forced test signal - SHADOW ONLY - NOT A STRATEGY RESULT`, labels every audit row `smoke_forced`, and refuses any mode but shadow, any broker but `ShadowBroker`, and any credential in the environment.

It exits 0 only if: one entry and one exit intent, both FILLED; realized P&L equals the exact quote cash flows incl. fees (plus the unreleased cost basis of fee dust); ledger equals shadow balances; kill switch never engaged; guardian ticked with 0 failures; no bot errors; `healthcheck` ok; no secrets in the DB, journal or output. It also prints fill VWAP vs the mid at decision and the slippage in bps. Takes about 1.5-4 minutes (first live run: 85 s; gives up after 25 min, exit 1). Run it on the deploy host before the first real shadow run and after every upgrade; the result says the plumbing works, nothing about any strategy. Offline coverage: `tests/test_smoke_shadow_fill.py`.

## systemd

```bash
sudo useradd --system --home-dir /var/lib/wallexbot --shell /usr/sbin/nologin wallexbot
sudo git clone <repo> /opt/wallexbot && cd /opt/wallexbot
sudo python3.12 -m venv .venv && sudo .venv/bin/pip install .   # regular install: the default config is package data
sudo install -d -m 0750 -o root -g wallexbot /etc/wallexbot
sudo install -m 0640 -o root -g wallexbot deploy/env.example /etc/wallexbot/env
sudo cp deploy/systemd/wallexbot-bot.service /etc/systemd/system/    # (+ wallexbot-recorder.service if wanted)
sudo systemctl daemon-reload && sudo systemctl enable --now wallexbot-bot
```

The checkout is root-owned and read-only to the service; the service user can write only `/var/lib/wallexbot` (`StateDirectory`, `ProtectSystem=strict`). Units set `NoNewPrivileges`, `PrivateTmp`, an empty capability set, a syscall filter (`~@privileged` only: also denying `@resources` made numpy/pandas die with SIGSYS at import under `systemd-run --user`) and `Restart=on-failure`. The recorder unit runs `python -m wallexbot record --out /var/lib/wallexbot/recorder`; `history` is the same kind of one-shot command (docs/DEMO.md).

## Guardian: read this first

The guardian runs **inside the bot process** (`bot --mode shadow`, on by default): its own SQLite connection on `bot.db`, the bot's own `ShadowBroker` object, a tick after every bot tick and at least every `--guardian-interval-s` (15 s). It writes a `guardian` heartbeat, engages the kill switch on soft breaches, a balance mismatch or a lost strategy heartbeat, and flattens through the OMS on a hard breach. Stale data on a held symbol is reported (`stale_feed:<symbols>`) and after 120 s follows `--stale-action hold|flatten` (default `hold`), but does not engage the kill switch; while it lasts the guardian writes a `guardian.alert` row every 5 min and flags it in its tick audit event. Only the heartbeat engagement (reason `auto:heartbeat_lost`) is released by the guardian itself, after 3 clear ticks and 60 s (wall-clock and monotonic, compare-and-set on that row); every other engagement waits for an operator (docs/RISK.md). `--interval-s` must be at most 15 s (`max_data_age_s / 2`), else the bot exits 2. In shadow mode this **exercises the guardian logic against simulated fills**; it protects no real money. `--no-guardian` disables it (the healthcheck then stops requiring its heartbeat).

There is no separate guardian service any more (no compose service, no systemd unit). A separate guardian process only makes sense against a live exchange account, which this build does not have: a separate process cannot see the bot's in-process paper or shadow broker. `python -m wallexbot guardian` is a standalone DEMO on its own in-memory mock broker (10000 USDT). It refuses to run without `--demo-db`, and refuses any database that holds bot events or equity rows it did not write, because its equity rows would corrupt the bot's drawdown and balance checks.

## What to monitor

- Health: `docker compose exec bot python /app/deploy/healthcheck.py --db /data/bot.db` (or `make healthcheck DB=...`). One JSON line; exit 0 only if the strategy heartbeat (and, when the run enabled the guardian, the guardian heartbeat) is younger than `--max-heartbeat-age-s` (120) and no `guardian.*` error, `needs_review` audit event or `needs_review` intent is newer than `--alert-window-min` (15), and the guardian's latest tick of the run reports no held/ordered symbol without a fresh quote (`held_stale`). It also reports the run's `mode` and strategy `profile`; `profile: demo` (`exercise_profile: true`) adds a warning. An engaged kill switch **fails** the probe (no new entries is not healthy); `--allow-killswitch` (`make healthcheck HEALTH_ARGS=--allow-killswitch`) reports it as a warning instead, and `killswitch_auto_release: true` marks the guardian's transient engagement. While Wallex's public API is down the bot is alive but degraded (no new entries, no kill switch): the probe reports `feed_down_since` / `feed_outage_s` and a **warning** (exit 0) up to `--max-feed-outage-s` (900 s), so an orchestrator does not restart a healthy bot during a short outage, and **fails** beyond it. A held position in that outage still fails the probe after 120 s (`held_stale`). Restarting does not help an exchange outage; look for `bot.feed_down` rows in `errors`. It opens the DB read-only. Docker runs it as the container HEALTHCHECK; with systemd, run it from your monitoring (command in the unit file).
- Shadow journal: `/data/shadow_journal.jsonl` (simulated fills; appears once an order exists). No file after hours means no signals, which is a valid outcome.
- Store tables (read-only): `sqlite3 -readonly bot.db`. Look at `order_intents`, `fills`, `positions_snapshots`, `killswitch_events`, `errors`, `audit_events`.

## Stop safely

- `docker compose stop` / `systemctl stop wallexbot-bot` sends SIGTERM; the bot finishes its current tick and exits (grace 60 s). The in-process guardian stops with it.
- Kill switch: engaging it blocks new entries and never blocks exits. The CLI has no command for it; it is the latest row of `killswitch_events` (`KillSwitch.engage` / `.disengage` in `risk/killswitch.py`), and the guardian (and the bot, after 5 ticks in a row with an internal failure, reason `bot: N consecutive internal failures`; an exchange outage never counts) engages it automatically. The healthcheck fails while it is engaged; check `killswitch_reason` before disengaging.

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
2. `git pull` (check `git log` and `docs/` for schema changes), then `docker compose up -d --build`, or `.venv/bin/pip install --upgrade .` in `/opt/wallexbot` and `systemctl restart`.
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
