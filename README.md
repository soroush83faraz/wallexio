# wallexbot

A long-only, spot momentum-breakout trading agent for the Wallex exchange, quoted in USDT.

**Status: Day-1 mock vertical slice implemented; live trading disabled by default.**

This is a hackathon submission. It does not promise profit. "No trades" is a valid outcome of the strategy, and no backtest results are reported here, because none have been produced yet.

## What it is

- Spot only, long only, USDT quote currency, top 12 markets by 24h volume (config-driven).
- A single order path: every order goes through the OMS, which calls the broker. Nothing else submits orders.
- Fail-closed risk engine: any failed check blocks the order. A kill switch blocks new entries but not reduce-only exits.
- An optional AI advisor that can only veto or size down. It is off in the critical path by default (not yet written: `ai/` is empty).
- A guardian process that watches the audit store and flattens positions on a hard breach (planned, Day 2).
- An append-only SQLite audit store (implemented in `store.py`).

## What exists now

Implemented in the Day-1 mock slice:

- Public Wallex data client, unauthenticated GET only, host pinned: markets, candles (`udf/history`), depth, trades (`exchange/wallex_public.py`).
- Candle aggregation from closed buckets, candle cache, data-quality checks and a market-data recorder (`data/`).
- Append-only SQLite store with compare-and-set order-intent states (`store.py`).
- Fail-closed pre-trade risk engine and kill switch (`risk/`).
- OMS, the only caller of `broker.submit_order` (`oms.py`).
- `MockWallex`, an in-memory exchange simulator with no network (`exchange/mock_wallex.py`).
- Redaction, auth and host guards, and live-arming checks (`redact.py`, `exchange/auth.py`, `config.py`).
- A scripted mock round trip, `wallexbot.pipeline.run_scripted_trade` (see [docs/DEMO.md](docs/DEMO.md)).

Not yet implemented: strategy, backtest engine, guardian, bot loop, CLI, `WallexBroker` private client, shadow broker.

## Quickstart

Requires Python 3.12. No credentials are needed or used.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest -q
```

Verified on the code at this commit: `pytest -q` reports 784 passed.

## Documentation

- [docs/STRATEGY.md](docs/STRATEGY.md): momentum-breakout spec, cost gate, exits E1–E4, abstention (specification only).
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): modules, single-order-path rule, guardian, AI boundary.
- [docs/RISK.md](docs/RISK.md): limits, soft and hard breaches, flatten procedure, failure behaviour.
- [docs/WALLEX_INTEGRATION.md](docs/WALLEX_INTEGRATION.md): Wallex API facts, observed vs unverified, API key rules.
- [docs/BACKTEST.md](docs/BACKTEST.md): methodology, splits, test-set rule, promotion criteria, limitations (planned).
- [docs/CONFIG_ENV.md](docs/CONFIG_ENV.md): environment variable names, config keys, live arming conditions.
- [docs/DEMO.md](docs/DEMO.md): five-minute demo outline (partly runnable).

## Safety defaults

- `mode: mock` in `config/default.yaml`. Live mode is rejected unless `WALLEX_ARM_LIVE=YES_I_UNDERSTAND`, a `WALLEX_API_KEY_TRADE` and the `--arm-live` flag (`cli_armed=True` in code; the CLI is not yet written) are all present.
- Host allow-list: only `api.wallex.ir`.
- Secrets live in a local `.env` (git-ignored). Only variable names are committed, in `.env.example`.
- Wallex keys, when used, are read-only where possible. No key may hold withdrawal rights.
