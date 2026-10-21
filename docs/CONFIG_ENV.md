# Configuration and environment

Status: `config.py`, `config/default.yaml` and `exchange/auth.py` exist. The CLI (`cli.py`, including the `--arm-live` flag) and the `doctor` command are planned.

## Environment variable names

Names only. Values never go in the repo. Local values belong in a git-ignored `.env` or the process environment. The template is `.env.example`.

| Name | Used by | Status in this build |
|---|---|---|
| `WALLEX_API_KEY_READ` | Read-only Wallex key | Named in `exchange/auth.py` (`READ_KEY_ENV`) but not read by any runtime path yet. No credentials are used or requested. |
| `WALLEX_API_KEY_TRADE` | Read + Trade Wallex key | **Read** by `assert_live_armed()` in `exchange/auth.py`, which `load_config()` calls when `mode: live`. Only presence and header-safe format are checked. No order can be sent yet (no private client). |
| `WALLEX_SUB_ACCOUNT_CLIENT_ID` | Sub-account identifier | Named in `exchange/auth.py` (`SUB_ACCOUNT_ENV`) but not read by any runtime path yet. Use not specified in the plan. |
| `WALLEX_ARM_LIVE` | Live arming | **Enforced** by `config.py` and `assert_live_armed()`: must equal `YES_I_UNDERSTAND` (exact match) when `mode: live`. |
| `ANTHROPIC_API_KEY` | Optional AI advisor | Not read by any code yet. The advisor is off in the critical path by default. |

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

Risk limits are explained in RISK.md. Strategy parameters are not in the config yet (see STRATEGY.md).

## Live arming conditions

Live arming is enforced in code by `load_config(..., cli_armed=...)` (config loading), which calls `exchange.auth.assert_live_armed(env, cli_armed)` when `mode: live`. That check requires the environment variable, `cli_armed is True` and `WALLEX_API_KEY_TRADE`.

Live trading needs **all** of the following:

1. `mode: live` in the config file. The default is `mock`.
2. `WALLEX_ARM_LIVE=YES_I_UNDERSTAND` in the environment. **Enforced now** by `config.py`; without it, loading fails with `ConfigError`.
3. A `--arm-live` command-line flag. The code check is **implemented**: `load_config(..., cli_armed=True)` is required. The flag itself is **planned** (`cli.py`, not yet present), so today only tests and callers can pass `cli_armed=True`.
4. `WALLEX_API_KEY_TRADE` present. **Enforced now** by `assert_live_armed()` (presence and format only).
5. `doctor` reporting green. **Planned.**
6. Explicit go-ahead from the user, given in the conversation. This is a process rule, not code.

Before arming, the evidence in BACKTEST.md must also be complete: backtest, single test-set evaluation, and a 24–48 hour shadow run.

Notes:

- `mode: shadow` is accepted by the config loader and does not require `WALLEX_ARM_LIVE`. Shadow mode sends no orders (planned `ShadowBroker`).
- `mode: mock` is the default and uses no network writes.
- Changing `mode` in the YAML file is not sufficient for live trading. The environment check still applies.
