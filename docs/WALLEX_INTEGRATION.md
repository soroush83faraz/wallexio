# Wallex integration

Status: **no Wallex fact has been verified against the live API yet.** No saved fixtures exist at commit `0e7ee8e`. The official rules are login-gated and have not been read. This page separates what is **verified** from what is **unverified**. Treat every unverified item as an assumption until a saved fixture or an official source confirms it.

## Verified vs unverified

| Item | Status | Evidence / note |
|---|---|---|
| Host is `api.wallex.ir` | Config allow-list only | `ALLOWED_HOSTS` in `config.py`. Not checked against the live service. |
| Public GET endpoints: markets, `udf/history` (candles), `depth`, `trades` | **Unverified** | Names come from the plan. Shapes to be confirmed by saved public fixtures. |
| Candle volume semantics | Observed (public fixtures) | Volume is in base units. The strategy still uses relative volume only (volume vs its median). |
| Candle resolutions | Observed (public fixtures) | Only `1`, `60` and `1D` are honoured server-side. Documented `15`, `240`, `480`, `720`, `2D`, `3D` are not: `15` returns 1m bars and `240`+ return 1h bars. There is no native 15m or 4h; we aggregate 15m and 4h from 1m (4h is also derivable from 1h), and cross-check the aggregator against native 1h (resolution `60`). |
| Candle series behaviour | Observed (public fixtures) | The 1m series forward-fills no-trade minutes as zero-volume flat bars. 1m range cap is about 1500 bars per request. Native-60 truncates to about 696 bars. |
| Public trades shape | Observed (public fixtures) | Trades have no id: dedupe by (ts, price, qty, side). Returned newest first. |
| Order-status enum | **Unverified** | Pending check against the Swagger definition. |
| Order endpoints: create (POST), lookup by client id (`GET …/orders/{client_id}`), `openOrders` | **Unverified** | Names come from the plan. Not called in this build. |
| Client id format (letters, digits, underscore; unique) | **Unverified** | Assumed in `types.py`. Intent client ids look like `wb_` + 32 hex characters. |
| Minimum notional | **Unverified** | Placeholder `min_notional: 5` USDT in config. |
| Taker and maker fee | **Unverified** | Placeholder `0.0035` (0.35%) for both in config. |
| Stop-order balance locking | **Unverified** | Affects how much balance a stop order reserves. Not known. |
| Global rate limits | **Unverified** | Not known. |
| Order-create rate limit | Documented by Wallex [from docs], not tested | Wallex documents 20 order-creates per 10 s. Our client-side `TokenBucket` is deliberately 10 per 10 s (half) as a safety margin. |
| Max keys per account (3) | **Unverified** | From the brief. The plan uses two keys. |
| Official rules and fee schedule | **Unverified** | Login-gated. Not accessed. |

Retry policy (design, implemented in code when present): GET requests may be retried. POST requests are never retried by the transport; a POST timeout is reconciled through the OMS (see ARCHITECTURE.md).

## Access model

Only public, unauthenticated GET requests are planned for Day 1, for saved fixtures and the recorder. Private calls are not made in this build.

| Key | Environment variable | Permission | Use |
|---|---|---|---|
| Key 1 | `WALLEX_API_KEY_READ` | Read only | Account and order reads, `doctor --read-only` (planned) |
| Key 2 | `WALLEX_API_KEY_TRADE` | Read + Trade | Order placement, only after arming (see CONFIG_ENV.md) |

Rules:

- **Never grant Withdraw** on any key. This is a policy rule for this project, not a Wallex-verified permission model.
- Keys live only in a local `.env` (git-ignored) or the process environment. They are never written to the repo, the audit store, or logs. Redaction runs before writes (planned in `redact.py`).
- `WALLEX_SUB_ACCOUNT_CLIENT_ID` is listed in `.env.example`. Its exact use is not specified in the plan.

## Contract tests (planned)

- Saved public responses will live under `tests/` (planned fixture directory) and be parsed by contract tests.
- Mutated fixtures (missing fields, wrong types, unknown statuses) must fail closed.
- A fixture passing its contract test moves the matching row above to **verified**, with the fixture path noted in the table.
- No fixture currently exists, so no row is verified.

## Network calls

- Planned for Day 1: unauthenticated GET requests to `api.wallex.ir` only, for fixtures and the recorder.
- No code at commit `0e7ee8e` makes network calls.
