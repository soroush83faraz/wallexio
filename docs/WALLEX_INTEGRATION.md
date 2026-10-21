# Wallex integration

Status: **public fixtures exist; private API facts are unverified.** Saved public GET responses live in `tests/fixtures/` (capture time in `tests/fixtures/FETCHED_AT.txt`). They were captured from unauthenticated public GETs. The official rules are login-gated and have not been read. This page separates what is **observed** (in the saved fixtures) from what is **unverified**. Treat every unverified item as an assumption until a fixture or an official source confirms it.

## Verified vs unverified

| Item | Status | Evidence / note |
|---|---|---|
| Host is `api.wallex.ir` | Observed (public fixtures) for public GETs | `ALLOWED_HOSTS` in `config.py`; `exchange/wallex_public.py` pins the same host. |
| Public GET endpoints: markets (`/hector/web/v1/markets`), `udf/history` (candles), `/v1/depth`, `/v1/trades` | Observed (public fixtures) | Saved as `tests/fixtures/markets.json`, `udf_*.json`, `depth_BTCUSDT.json`, `trades_BTCUSDT.json`; parsed by `exchange/wallex_public.py`. Markets, depth and trades wrap payloads as `{"result": ..., "success": true}`. |
| Candle volume semantics | Observed (public fixtures) | Volume is in base units. The strategy still uses relative volume only (volume vs its median). |
| Candle resolutions | Observed (public fixtures) | Only `1`, `60` and `1D` are honoured server-side. Documented `15`, `240`, `480`, `720`, `2D`, `3D` are not: `15` returns 1m bars and `240`+ return 1h bars. There is no native 15m or 4h; we aggregate 15m and 4h from 1m (4h is also derivable from 1h), and cross-check the aggregator against native 1h (resolution `60`). |
| Candle series behaviour | Observed (public fixtures) | The 1m series forward-fills no-trade minutes as zero-volume flat bars. 1m range cap is about 1500 bars per request. Native-60 truncates to about 696 bars. |
| Public trades shape | Observed (public fixtures) | Trades have no id: dedupe by (ts, price, qty, side). Returned newest first. |
| Order-status enum | **Unverified** | Pending check against the Swagger definition. |
| Order endpoints: create (POST), lookup by client id (`GET …/orders/{client_id}`), `openOrders` | **Unverified** | Names come from the plan. Not called in this build. |
| Client id format (letters, digits, underscore; unique) | **Unverified** | Assumed in `types.py`. Intent client ids look like `wb_` + 32 hex characters. |
| Auth header names `X-API-Key` and `sub-account-client-id` | **Unverified** | Assumed in `exchange/auth.py`. No private request is sent in this build. |
| Minimum notional | **Unverified** | Placeholder `min_notional: 5` USDT in config. |
| Taker and maker fee | **Unverified** | Placeholder `0.0035` (0.35%) for both in config. |
| Stop-order balance locking | **Unverified** | Affects how much balance a stop order reserves. Not known. |
| Global rate limits | **Unverified** | Not known. |
| Order-create rate limit | Documented by Wallex [from docs], not tested | Wallex documents 20 order-creates per 10 s. Our client-side `TokenBucket` is deliberately 10 per 10 s (half) as a safety margin. |
| Max keys per account (3) | **Unverified** | From the brief. The plan uses two keys. |
| Official rules and fee schedule | **Unverified** | Login-gated. Not accessed. |

Retry policy (implemented for GET only, in `exchange/wallex_public.py`): a GET is retried up to 3 times on 5xx, 429, network errors and timeouts; other 4xx responses are not retried. POST requests are never retried by the transport; a POST timeout is reconciled through the OMS (see ARCHITECTURE.md). No POST client exists yet.

## Access model

Only public, unauthenticated GET requests are made in this build, by `exchange/wallex_public.py`. Private calls are not made.

| Key | Environment variable | Permission | Use |
|---|---|---|---|
| Key 1 | `WALLEX_API_KEY_READ` | Read only | Account and order reads, `doctor --read-only` (planned) |
| Key 2 | `WALLEX_API_KEY_TRADE` | Read + Trade | Order placement, only after arming (see CONFIG_ENV.md) |

Rules:

- **Never grant Withdraw** on any key. This is a policy rule for this project, not a Wallex-verified permission model. `exchange/auth.py` also refuses any URL whose path mentions `withdraw`.
- Keys live only in a local `.env` (git-ignored) or the process environment. They are never written to the repo, the audit store, or logs. The store passes every payload and text column through `redact()` (`redact.py`) before writing.
- `WALLEX_SUB_ACCOUNT_CLIENT_ID` is listed in `.env.example` and maps to the `sub-account-client-id` header in `exchange/auth.py`. Its exact use is not specified in the plan.

## Contract tests

- Saved public responses live under `tests/fixtures/` and are parsed by `tests/test_wallex_public.py`.
- Mutated fixtures (missing fields, wrong types, bad volumes, failed envelopes) must fail closed. The tests check this.
- A fixture passing its contract test moves the matching row above to **observed**, with the fixture path noted in the table.
- The fixtures are one public capture (see `FETCHED_AT.txt`). They do not confirm any private endpoint or any row marked unverified.

## Network calls

- Only unauthenticated GET requests to `api.wallex.ir`, made by `exchange/wallex_public.py`. The host is pinned there. No POST, no private call, no withdraw path.
- `exchange/auth.py` builds auth headers and an HTTPS opener for future private calls, but nothing calls them yet.
- `exchange/mock_wallex.py` makes no network calls.
