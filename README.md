# wallexbot

A long-only, spot momentum-breakout trading agent for the Wallex exchange, quoted in USDT.

**Status: Day-1 mock vertical slice; live trading disabled by default.**

This is a hackathon submission. It does not promise profit. "No trades" is a valid outcome of the strategy, and no backtest results are reported here, because none have been produced yet.

## What it is

- Spot only, long only, USDT quote currency, top 12 markets by 24h volume (config-driven).
- A single order path: every order goes through the OMS, which calls the broker. Nothing else submits orders.
- Fail-closed risk engine: any failed check blocks the order. A kill switch blocks new entries but not reduce-only exits.
- An optional AI advisor that can only veto or size down. It is off in the critical path by default.
- A guardian process that watches the audit store and flattens positions on a hard breach (planned, Day 2).
- An append-only SQLite audit store (planned).

## What works at this commit

Commit `0e7ee8e` contains the skeleton only: shared types (`src/wallexbot/types.py`), the config loader with fail-closed checks (`src/wallexbot/config.py`), default config (`config/default.yaml`), and a baseline test file. The exchange mock, store, risk engine, OMS, strategy, backtest, guardian and CLI are planned and not yet present.

## Quickstart

Requires Python 3.12. No credentials are needed or used.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest -q
```

Verified at this commit: 4 tests pass.

## Documentation

- [docs/STRATEGY.md](docs/STRATEGY.md): momentum-breakout spec, cost gate, exits E1–E4, abstention.
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): modules, single-order-path rule, guardian, AI boundary.
- [docs/RISK.md](docs/RISK.md): limits, soft and hard breaches, flatten procedure, failure behaviour.
- [docs/WALLEX_INTEGRATION.md](docs/WALLEX_INTEGRATION.md): Wallex API facts, verified vs unverified, API key rules.
- [docs/BACKTEST.md](docs/BACKTEST.md): methodology, splits, test-set rule, promotion criteria, limitations.
- [docs/CONFIG_ENV.md](docs/CONFIG_ENV.md): environment variable names, config keys, live arming conditions.
- [docs/DEMO.md](docs/DEMO.md): five-minute demo outline (planned).

## Safety defaults

- `mode: mock` in `config/default.yaml`. Live mode is rejected unless `WALLEX_ARM_LIVE=YES_I_UNDERSTAND` is set.
- Host allow-list: only `api.wallex.ir`.
- Secrets live in a local `.env` (git-ignored). Only variable names are committed, in `.env.example`.
- Wallex keys, when used, are read-only where possible. No key may hold withdrawal rights.
