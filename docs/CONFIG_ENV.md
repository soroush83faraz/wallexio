# Configuration and environment

Status: `config.py`, `src/wallexbot/default_config.yaml` (package data) and `exchange/auth.py` exist. `wallexbot.doctor` now exists as a module (`run_doctor`, GET-only; see below), and `python -m wallexbot doctor` runs it. `cli.py` exists, but the `--arm-live` flag is deliberately refused everywhere (no live mode in this build).

## Environment variable names

Names only. Values never go in the repo. Local values belong in a git-ignored `.env` or the process environment. The template is `.env.example`.

| Name | Used by | Status in this build |
|---|---|---|
| `WALLEX_API_KEY_READ` | Read-only Wallex key | Named in `exchange/auth.py` (`READ_KEY_ENV`). Used by `wallex_private.py` and `doctor.py` when a caller passes `load_secret(READ_KEY_ENV)`; `python -m wallexbot doctor` does so; the bot loop never does. No credentials are used by tests or the mock. |
| `WALLEX_API_KEY_TRADE` | Read + Trade Wallex key | **Read** by `assert_live_armed()` in `exchange/auth.py`, which `load_config()` calls when `mode: live`. Presence and header-safe format are checked. `WallexBroker` (unverified) refuses to send or cancel unless built with this key and the token returned by `assert_live_armed()`; nothing builds one yet. |
| `WALLEX_SUB_ACCOUNT_CLIENT_ID` | Sub-account identifier | Named in `exchange/auth.py` (`SUB_ACCOUNT_ENV`); optionally passed to the private client as a header. Not read by any loop yet. Use not specified in the plan. |
| `WALLEX_ARM_LIVE` | Live arming | **Enforced** by `config.py` and `assert_live_armed()`: must equal `YES_I_UNDERSTAND` (exact match) when `mode: live`. |
| `WALLEXBOT_CONFIG` | Config file override | Path to a YAML file used instead of the packaged default whenever no explicit path is passed (`load_config()`, `bot` without `--config`, `doctor`). Read from the process environment; empty = unset. Same fail-closed loader: secret-like keys, unknown mode, non-allow-listed host and the live-arming rules apply unchanged. A set but missing/unreadable/invalid file is a `ConfigError` (exit 2 for `bot`), never a silent fallback. Not a secret; a path only. |
| `ANTHROPIC_API_KEY` | Optional AI advisor | Read only by `bot --advisor shadow` (`advisor_bridge.py`, via `redact.load_secret`); without it the bot warns and runs with `NullAdvisor`. Shadow verdicts are logged, never applied. |

## Config keys

From `src/wallexbot/default_config.yaml`, shipped inside the wheel (a plain `pip install .` includes it; there is no second copy in the repo). Loaded by `load_config()`, which uses `os.environ` for the live-arming variables unless an `env` dict is passed. Resolution order of the file: explicit path argument (`bot --config`), then `WALLEXBOT_CONFIG`, then the packaged default. To customise, copy the file, edit it and point `WALLEXBOT_CONFIG` at the copy.

| Key | Default | Meaning |
|---|---|---|
| `mode` | `mock` | `mock`, `shadow` or `live`. Any other value raises `ConfigError`. |
| `quote_currency` | `USDT` | Quote asset. |
| `host` | `api.wallex.ir` | Must be in the allow-list (`api.wallex.ir` only), otherwise `ConfigError`. |
| `universe.top_n` | `12` | Number of markets by 24h volume. |
| `universe.max_spread_bps` | `15` | Entry check. |
| `universe.max_slippage_bps` | `20` | |
| `universe.min_depth_multiple` | `5` | Basis not yet fixed in code. |
| `universe.max_data_age_s` | `30` | Staleness limit for entries; live `bot --interval-s` may be at most half of it. |
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
| `fees.taker_rate` | `0.002` | Flat 0.2 % for USDT pairs per a public, undated Wallex help page (and the API docs' fee example). Tier/volume dependent; our account's rate is unverified: run `doctor` before trusting it: its `fee_vs_config` check FAILS when the account's highest maker/taker rate on the configured quote currency is above `fees.*`. Was `0.0035` (an unverified planning assumption). |
| `fees.maker_rate` | `0.002` | Same source and caveat as `fees.taker_rate`. |

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
- Guardian stale action is a CLI flag, not YAML: `bot --stale-action hold|flatten` (in-process guardian) and `guardian --stale-action ...` (demo); default `hold`, anything else exits 2. It sets `GuardianConfig.stale_action` for a held symbol without a fresh quote for > 120 s.
- `ANTHROPIC_API_KEY` is read only by `bot --advisor shadow` (via `redact.load_secret`); without it the bot warns and runs with `NullAdvisor`.
- `mode: mock` is the default and uses no network writes.
- Changing `mode` in the YAML file is not sufficient for live trading. The environment check still applies.
