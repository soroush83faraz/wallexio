# wallexbot

A long-only, spot momentum-breakout trading agent for the Wallex exchange, quoted in USDT.

**Status: the pieces are wired end to end in `mock` (paper, offline replay possible) and `shadow` (live public order book, simulated fills, no orders sent) modes: `python -m wallexbot demo|backtest|doctor|guardian|bot`. Everything is tested against the mock, synthetic or recorded data only. Live trading does not exist in this build.**

This is a hackathon submission. It does not promise profit. "No trades" is a valid outcome of the strategy, and no validated backtest result is reported here. One exploratory grid exists under `reports/` (see below); treat it as a smoke test of the tooling, not as evidence.

## What it is

- Spot only, long only, USDT quote currency, top 12 markets by 24h volume (config-driven).
- A single order path: every order goes through the OMS, which calls the broker. Nothing else submits orders.
- Fail-closed risk engine: any failed check blocks the order. A kill switch blocks new entries but not reduce-only exits.
- An optional AI advisor (`ai/`, `advisor_bridge.py`) that can only veto or size down. It is off by default; `bot --advisor shadow` records its verdicts without acting on them (`apply` is never enabled). Its calls to the Anthropic API are tested against a fake transport only.
- A guardian process that reads the exchange state and flattens positions on a hard breach (implemented in `guardian/`, tested against the mock only; it is the only component that engages the kill switch automatically).
- An append-only SQLite audit store (implemented in `store.py`).

## What exists now

Implemented and unit-tested (against the mock or synthetic data; nothing has run against the real exchange):

- Public Wallex data client, unauthenticated GET only, host pinned (`exchange/wallex_public.py`); candle aggregation, cache, quality checks and a market-data recorder (`data/`).
- Resumable historical 1m candle downloader (`data/history.py`), writing `data/history/<SYMBOL>/<SYMBOL>_1m_<YYYY-MM>.csv` (git-ignored). No CLI. `data/recorder.py::Recorder` appends depth and trades to `data/recorder/*.jsonl` (git-ignored); it has no CLI either, so it is started from a few lines of Python (docs/DEMO.md).
- Strategy: S1 momentum-breakout entry engine, S0 trend baseline, cooldown, risk-based sizing and exit policies E1-E4 (`strategy/`). Parameter defaults are unvalidated placeholders ([docs/STRATEGY.md](docs/STRATEGY.md)).
- Backtest engine with cost model, metrics, chronological splits with a single-use test-set guard, sensitivity analysis and a report writer, plus the runner behind `python -m wallexbot backtest` (`backtest/`). `reports/` holds one exploratory grid (train and validation only), made with placeholder costs on the engine **before** the verifier fixes (`bt-0.1`); it must be re-run and is not evidence of anything.
- Append-only SQLite store (`store.py`), fail-closed risk engine and kill switch (`risk/`), the OMS, the only caller of `broker.submit_order` (`oms.py`).
- Guardian: independent monitor and hard-breach flatten through the OMS (`guardian/`); `python -m wallexbot guardian --db PATH --mode mock|shadow` (demo loop).
- Bot loop (`bot/`): S1 entries, E4 exits, risk-based sizing, every order through `pipeline.route_to_oms` -> `OMS.submit`; state is persisted and restored after a restart. `python -m wallexbot bot ...` (mock or shadow only).
- `ShadowBroker` (`exchange/shadow.py`): simulates fills from the cached public depth with conservative assumptions; local JSONL journal. Its fill model is an assumption set, not validated against real fills.
- `MockWallex`, an in-memory exchange simulator with no network (`exchange/mock_wallex.py`).
- `WallexBroker` private client and a read-only `doctor` module (`exchange/wallex_private.py`, `doctor.py`). **UNVERIFIED against the real API**: its endpoint, field and status assumptions are listed in [docs/WALLEX_INTEGRATION.md](docs/WALLEX_INTEGRATION.md). It cannot send or cancel orders unless live arming succeeded.
- Redaction, auth and host guards, live-arming checks (`redact.py`, `exchange/auth.py`, `config.py`).
- A scripted mock round trip: `python -m wallexbot demo` (see [docs/DEMO.md](docs/DEMO.md)).

**Not done**: any validated backtest, the single test-set evaluation, a 24-48 h shadow run, any check of the private client against Wallex, any live run. Live is refused: `--arm-live` and `bot --mode live` both exit with an error.

## Quickstart

Requires Python 3.12. No credentials are needed or used.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest -q
```

Verified on the code at this commit: `pytest -q` reports 1744 passed.

Run it (all offline except `bot --mode shadow`, which makes public GET requests only):

```bash
.venv/bin/python -m wallexbot demo                                   # scripted mock round trip
.venv/bin/python -m wallexbot backtest                               # exploratory grid -> reports/ (train+validation only)
.venv/bin/python -m wallexbot bot --db /tmp/bot.db --mode mock --replay-from data/history --max-ticks 400
.venv/bin/python -m wallexbot bot --db /tmp/shadow.db --mode shadow [--balance 1000] [--advisor shadow]
.venv/bin/python -m wallexbot guardian --db /tmp/g.db --mode mock --ticks 3 --interval 1
```

## Documentation

- [docs/STRATEGY.md](docs/STRATEGY.md): S1 entry rules, S0 baseline, sizing, exits E1–E4 as implemented (placeholder parameters).
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): modules, single-order-path rule, guardian, AI boundary.
- [docs/RISK.md](docs/RISK.md): limits, soft and hard breaches, guardian and flatten procedure, failure behaviour.
- [docs/WALLEX_INTEGRATION.md](docs/WALLEX_INTEGRATION.md): Wallex API facts, observed vs unverified, private-client assumptions, API key rules.
- [docs/BACKTEST.md](docs/BACKTEST.md): engine assumptions, history downloader, splits, test-set rule, promotion criteria, limitations.
- [docs/CONFIG_ENV.md](docs/CONFIG_ENV.md): environment variable names, config keys, live arming conditions.
- [docs/DEMO.md](docs/DEMO.md): five-minute demo outline and the exact commands.

## Safety defaults

- `mode: mock` in `config/default.yaml`. Live mode is rejected unless `WALLEX_ARM_LIVE=YES_I_UNDERSTAND`, a `WALLEX_API_KEY_TRADE` and `cli_armed=True` in code are all present. The CLI refuses `--arm-live` and never passes `cli_armed`, so no command can reach live.
- Host allow-list: only `api.wallex.ir`.
- Secrets live in a local `.env` (git-ignored). Only variable names are committed, in `.env.example`.
- Wallex keys, when used, are read-only where possible. No key may hold withdrawal rights.
