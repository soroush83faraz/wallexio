# wallexbot

A long-only, spot momentum-breakout trading agent for the Wallex exchange, quoted in USDT.

**Status: mock vertical slice, strategy, backtest engine, guardian and history downloader are implemented and unit-tested. Not runnable end to end: there is no CLI or bot loop. Live trading is disabled by default.**

This is a hackathon submission. It does not promise profit. "No trades" is a valid outcome of the strategy, and no backtest results are reported here: the backtest engine exists, but **no backtest has been run on real data yet**.

## What it is

- Spot only, long only, USDT quote currency, top 12 markets by 24h volume (config-driven).
- A single order path: every order goes through the OMS, which calls the broker. Nothing else submits orders.
- Fail-closed risk engine: any failed check blocks the order. A kill switch blocks new entries but not reduce-only exits.
- An optional AI advisor that can only veto or size down. It is off in the critical path by default (**not implemented**: `ai/` is empty).
- A guardian process that reads the exchange state and flattens positions on a hard breach (implemented in `guardian/`, tested against the mock only; it is the only component that engages the kill switch automatically).
- An append-only SQLite audit store (implemented in `store.py`).

## What exists now

Implemented and unit-tested (against the mock or synthetic data; nothing has run against the real exchange):

- Public Wallex data client, unauthenticated GET only, host pinned (`exchange/wallex_public.py`); candle aggregation, cache, quality checks and a market-data recorder (`data/`).
- Resumable historical 1m candle downloader (`data/history.py`), writing `data/history/<SYMBOL>/<SYMBOL>_1m_<YYYY-MM>.csv` (git-ignored). No CLI yet.
- Strategy: S1 momentum-breakout entry engine, S0 trend baseline, cooldown, risk-based sizing and exit policies E1-E4 (`strategy/`). Parameter defaults are unvalidated placeholders ([docs/STRATEGY.md](docs/STRATEGY.md)).
- Backtest engine with cost model, metrics, chronological splits with a single-use test-set guard, sensitivity analysis and a report writer (`backtest/`).
- Append-only SQLite store (`store.py`), fail-closed risk engine and kill switch (`risk/`), the OMS, the only caller of `broker.submit_order` (`oms.py`).
- Guardian: independent monitor and hard-breach flatten through the OMS (`guardian/`); demo entry point `python -m wallexbot.guardian.main --mode mock|shadow`.
- `MockWallex`, an in-memory exchange simulator with no network (`exchange/mock_wallex.py`).
- `WallexBroker` private client and a read-only `doctor` module (`exchange/wallex_private.py`, `doctor.py`). **UNVERIFIED against the real API**: its endpoint, field and status assumptions are listed in [docs/WALLEX_INTEGRATION.md](docs/WALLEX_INTEGRATION.md). It cannot send or cancel orders unless live arming succeeded.
- Redaction, auth and host guards, live-arming checks (`redact.py`, `exchange/auth.py`, `config.py`).
- A scripted mock round trip, `wallexbot.pipeline.run_scripted_trade` (see [docs/DEMO.md](docs/DEMO.md)).

**Not implemented**: the CLI (and `--arm-live` flag, `doctor` command), the bot loop that wires strategy to the OMS, `ShadowBroker`, the AI advisor. **Not done**: any backtest run, any shadow run, any check of the private client against Wallex.

## Quickstart

Requires Python 3.12. No credentials are needed or used.

```bash
python3.12 -m venv .venv && .venv/bin/pip install -e '.[dev]' && .venv/bin/pytest -q
```

Verified on the code at this commit: `pytest -q` reports 1244 passed.

## Documentation

- [docs/STRATEGY.md](docs/STRATEGY.md): S1 entry rules, S0 baseline, sizing, exits E1–E4 as implemented (placeholder parameters).
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): modules, single-order-path rule, guardian, AI boundary.
- [docs/RISK.md](docs/RISK.md): limits, soft and hard breaches, guardian and flatten procedure, failure behaviour.
- [docs/WALLEX_INTEGRATION.md](docs/WALLEX_INTEGRATION.md): Wallex API facts, observed vs unverified, private-client assumptions, API key rules.
- [docs/BACKTEST.md](docs/BACKTEST.md): engine assumptions, history downloader, splits, test-set rule, promotion criteria, limitations.
- [docs/CONFIG_ENV.md](docs/CONFIG_ENV.md): environment variable names, config keys, live arming conditions.
- [docs/DEMO.md](docs/DEMO.md): five-minute demo outline (partly runnable; no CLI).

## Safety defaults

- `mode: mock` in `config/default.yaml`. Live mode is rejected unless `WALLEX_ARM_LIVE=YES_I_UNDERSTAND`, a `WALLEX_API_KEY_TRADE` and the `--arm-live` flag (`cli_armed=True` in code; there is no CLI yet) are all present.
- Host allow-list: only `api.wallex.ir`.
- Secrets live in a local `.env` (git-ignored). Only variable names are committed, in `.env.example`.
- Wallex keys, when used, are read-only where possible. No key may hold withdrawal rights.
