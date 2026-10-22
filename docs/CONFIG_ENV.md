# Configuration and environment

Status: `config.py`, `config/default.yaml` and `exchange/auth.py` exist. `wallexbot.doctor` now exists as a module (`run_doctor`, GET-only; see below), and `python -m wallexbot doctor` runs it. `cli.py` exists, but the `--arm-live` flag is deliberately refused everywhere (no live mode in this build).

## Environment variable names

Names only. Values never go in the repo. Local values belong in a git-ignored `.env` or the process environment. The template is `.env.example`.

| Name | Used by | Status in this build |
|---|---|---|
| `WALLEX_API_KEY_READ` | Read-only Wallex key | Named in `exchange/auth.py` (`READ_KEY_ENV`). Used by `wallex_private.py` and `doctor.py` when a caller passes `load_secret(READ_KEY_ENV)`; `python -m wallexbot doctor` does so; the bot loop never does. No credentials are used by tests or the mock. |
| `WALLEX_API_KEY_TRADE` | Read + Trade Wallex key | **Read** by `assert_live_armed()` in `exchange/auth.py`, which `load_config()` calls when `mode: live`. Presence and header-safe format are checked. `WallexBroker` (unverified) refuses to send or cancel unless built with this key and the token returned by `assert_live_armed()`; nothing builds one yet. |
| `WALLEX_SUB_ACCOUNT_CLIENT_ID` | Sub-account identifier | Named in `exchange/auth.py` (`SUB_ACCOUNT_ENV`); optionally passed to the private client as a header. Not read by any loop yet. Use not specified in the plan. |
| `WALLEX_ARM_LIVE` | Live arming | **Enforced** by `config.py` and `assert_live_armed()`: must equal `YES_I_UNDERSTAND` (exact match) when `mode: live`. |
| `ANTHROPIC_API_KEY` | Optional AI advisor | Read only by `bot --advisor shadow` (`advisor_bridge.py`, via `redact.load_secret`); without it the bot warns and runs with `NullAdvisor`. Shadow verdicts are logged, never applied. |

## Config keys

From `config/default.yaml`. Loaded by `load_config()`, which uses `os.environ` unless an `env` dict is passed.

| Key | Default | Meaning |
|---|---|---|
| `mode` | `mock` | `mock`, `shadow` or `live`. Any other value raises `ConfigError`. |
| `quote_currency` | `USDT` | Quote asset. |
| `host` | `api.wallex.ir` | Must be in the allow-list (`api.wallex.ir` only), otherwise `ConfigError`. |
| `universe.top_n` | `12` | Number of markets by 24h volume. |
| `universe.max_spread_bps` | `15` | Entry check. |
| `universe.max_slippage_bps` | `20` | |
| `universe.min_depth_multiple` | `5` | Basis not yet fixed in code. |
| `universe.max_data_age_s` | `30` | Staleness limit for entries. |
| `risk.risk_per_trade_pct` | `1.0` | |
| `risk.max_position_pct` | `25` | |
| `risk.max_exposure_pct` | `60` | |
| `risk.max_open_positions` | `3` | |
| `risk.max_order_value` | `1000` | Quote currency. |
| `risk.daily_loss_soft_pct` | `4` | |
| `risk.daily_loss_hard_pct` | `6` | |
| `risk.drawdown_soft_pct` | `8` | |
| `risk.drawdown_hard_pct` | `12` | |
| `risk.consecutive_loss_cooldown` | `3` | |
| `risk.min_notional` | `5` | Placeholder, unverified. |
| `fees.taker_rate` | `0.0035` | Placeholder, unverified. |
| `fees.maker_rate` | `0.0035` | Placeholder, unverified. |

Risk limits are explained in RISK.md. Strategy and exit parameters are **not** in the YAML: they are dataclass defaults in `strategy/config.py` (`MomentumParams`), `strategy/exits.py` (`ExitParams`) and `backtest/costs.py` (`CostModel`), all unvalidated placeholders (see STRATEGY.md). Guardian settings (`GuardianConfig`, `FlattenConfig`) are code defaults too (see RISK.md).

## Live arming conditions

Live arming is enforced in code by `load_config(..., cli_armed=...)` (config loading), which calls `exchange.auth.assert_live_armed(env, cli_armed)` when `mode: live`. That check requires the environment variable, `cli_armed is True` and `WALLEX_API_KEY_TRADE`.

Live trading needs **all** of the following:

1. `mode: live` in the config file. The default is `mock`.
2. `WALLEX_ARM_LIVE=YES_I_UNDERSTAND` in the environment. **Enforced now** by `config.py`; without it, loading fails with `ConfigError`.
3. A `--arm-live` command-line flag. The code check is **implemented**: `load_config(..., cli_armed=True)` is required. `cli.py` exists but refuses `--arm-live` on every command (and `bot --mode live`) and never passes `cli_armed`, so only tests and library callers can.
4. `WALLEX_API_KEY_TRADE` present. **Enforced now** by `assert_live_armed()` (presence and format only).
5. `doctor` reporting green. The check code exists (`wallexbot.doctor.run_doctor`, read key only, never arms anything); **`python -m wallexbot doctor` runs it** (exit 2 without `WALLEX_API_KEY_READ`), and it has not been run against Wallex.
6. Explicit go-ahead from the user, given in the conversation. This is a process rule, not code.

Before arming, the evidence in BACKTEST.md must also be complete: backtest, single test-set evaluation, and a 24–48 hour shadow run.

Notes:

- `mode: shadow` is accepted by the config loader and does not require `WALLEX_ARM_LIVE`. Shadow mode sends no orders: `python -m wallexbot bot --mode shadow` uses `ShadowBroker` over public GETs, and the guardian demo's `--mode shadow` only uses a dry-run flag on a mock. The bot takes the mode from `--mode`, not from the YAML, and reads no secrets.
- `ANTHROPIC_API_KEY` is read only by `bot --advisor shadow` (via `redact.load_secret`); without it the bot warns and runs with `NullAdvisor`.
- `mode: mock` is the default and uses no network writes.
- Changing `mode` in the YAML file is not sufficient for live trading. The environment check still applies.
