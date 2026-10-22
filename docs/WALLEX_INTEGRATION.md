# Wallex integration

Status: **public fixtures exist. A private client exists (`exchange/wallex_private.py`) but is UNVERIFIED against the real API: no private request has ever been made.** Saved public GET responses live in `tests/fixtures/` (capture time in `tests/fixtures/FETCHED_AT.txt`). They were captured from unauthenticated public GETs. The official rules are login-gated and have not been read. This page separates what is **observed** (in the saved fixtures) from what is **unverified**. Treat every unverified item as an assumption until a fixture or an official source confirms it.

## Verified vs unverified

| Item | Status | Evidence / note |
|---|---|---|
| Host is `api.wallex.ir` | Observed (public fixtures) for public GETs | `ALLOWED_HOSTS` in `config.py`; `exchange/wallex_public.py` pins the same host. |
| Public GET endpoints: markets (`/hector/web/v1/markets`), `udf/history` (candles), `/v1/depth`, `/v1/trades` | Observed (public fixtures) | Saved as `tests/fixtures/markets.json`, `udf_*.json`, `depth_BTCUSDT.json`, `trades_BTCUSDT.json`; parsed by `exchange/wallex_public.py`. Markets, depth and trades wrap payloads as `{"result": ..., "success": true}`. |
| Candle volume semantics | Observed (public fixtures) | Volume is in base units. The strategy still uses relative volume only (volume vs its median). |
| Candle resolutions | Observed (public fixtures) | Only `1`, `60` and `1D` are honoured server-side. Documented `15`, `240`, `480`, `720`, `2D`, `3D` are not: `15` returns 1m bars and `240`+ return 1h bars. There is no native 15m or 4h; we aggregate 15m and 4h from 1m (4h is also derivable from 1h), and cross-check the aggregator against native 1h (resolution `60`). |
| Candle series behaviour | Observed (public fixtures) | The 1m series forward-fills no-trade minutes as zero-volume flat bars. 1m range cap is about 1500 bars per request. Native-60 truncates to about 696 bars. |
| Public trades shape | Observed (public fixtures) | Trades have no id: dedupe by (ts, price, qty, side). Returned newest first. |
| Order-status enum | **Unverified** | `STATUS_MAP` in `wallex_private.py` is a guess; unknown values map to a conservative open state and are logged. |
| Order endpoints: create (POST), lookup/cancel by client id (`…/orders/{client_id}`), `openOrders`, balances, fee | **Unverified** | Paths are constants in `wallex_private.py` (`/v1/account/orders`, `/openOrders`, `/balances`, `/fee`). Names come from the plan, never called against Wallex. |
| Client id format (letters, digits, underscore; unique) | **Unverified** | Assumed in `types.py`. Intent client ids look like `wb_` + 32 hex characters. |
| Auth header names `X-API-Key` and `sub-account-client-id` | **Unverified** | Assumed in `exchange/auth.py`. Used by `wallex_private.py`; no private request has been sent. |
| Minimum notional | **Unverified** | Placeholder `min_notional: 5` USDT in config. |
| Taker and maker fee | **Unverified** | Placeholder `0.0035` (0.35%) for both in config. |
| Stop-order balance locking | **Unverified** | Affects how much balance a stop order reserves. Not known. |
| Global rate limits | **Unverified** | Not known. |
| Order-create rate limit | Documented by Wallex [from docs], not tested | Wallex documents 20 order-creates per 10 s. Our client-side `TokenBucket` is deliberately 10 per 10 s (half) as a safety margin. |
| Max keys per account (3) | **Unverified** | From the brief. The plan uses two keys. |
| Official rules and fee schedule | **Unverified** | Login-gated. Not accessed. |

Retry policy (implemented for GET only, in `exchange/wallex_public.py`): a GET is retried up to 3 times on 5xx, 429, network errors and timeouts; other 4xx responses are not retried. POST requests are never retried by the transport; a POST timeout is reconciled through the OMS (see ARCHITECTURE.md). The POST client (`WallexBroker`) sends an order at most once per call and never retries it; any ambiguous outcome raises `BrokerTimeout`, which the OMS reconciles by client id.

## Private client assumptions (all UNVERIFIED)

`WallexBroker` and its GET-only half `WallexAccountReader` assume the following. Each must be checked against the real API (or the login-gated docs) before any live use; the read-only doctor is the intended way to check the response shapes:

- **Stop price field**: stop orders send the price under the key `stop_Price` (exact spelling unverified). Stop orders are refused unless `allow_stop_orders=True`.
- **Envelope**: success is `{"success": true, "result": ...}`, errors `{"success": false, "message": ...}`.
- **Order object**: `clientOrderId`, `symbol`, `side`, `type`, `origQty`, `price`, `status`, `fills[]` of `{price, quantity, fee, feeAsset}`; `executedQty` and `transactTime` optional. Fill totals are summed over `fills[]`.
- **Status mapping**: `NEW`, `PARTIALLY_FILLED`, `FILLED`, `CANCELED`/`CANCELLED`, `EXPIRED` (treated as canceled), `REJECTED`. Anything else is treated as open so the OMS keeps reconciling.
- **Balances**: `result.balances = {ASSET: {value, locked}}` with `value` read as the **total**; `free = value - locked`. A row with `locked > value` is rejected.
- **Fees**: `result = {MARKET: {makerFeeRate, takerFeeRate}}` as fractions.
- **Open orders**: `result.orders`, paged with `page` / `per_page` (names unverified).
- **Duplicate detection**: HTTP 409, or any other 4xx whose body mentions a duplicate or the client id, is read as "client_id already used" (`DuplicateClientId`, ambiguous, reconciled). A 404 counts as "order unknown" only when the body is a JSON `success: false` envelope.
- **Auth failures**: 401/403 are raised as `AuthRejected`, which the OMS also reconciles rather than treating as a rejection.

## Live path is blocked without arming

`submit_order` and `cancel_order` raise `AuthError` before any I/O unless the broker was built with the trade key **and** the exact token object returned by `assert_live_armed()` (an identity check via `is_live_armed_token`; an equal-looking `Secret` arms nothing). Reads use the read key. Two identical keys are refused. Every URL passes the host allow-list (`api.wallex.ir` only; fund-movement paths refused). The CLI and the bot loop never build a live broker (mock and shadow only). `wallexbot.doctor.run_doctor` performs GET-only checks (host pin, read key, balances, fee, open orders, not-found lookup shape, clock skew, config, `.env` ignored); `python -m wallexbot doctor` runs it; it has not been run against Wallex.

## Access model

Public GETs are made by `exchange/wallex_public.py` (host pinned, retried only for GET). Private calls are implemented in `exchange/wallex_private.py` but unverified and unarmed by default.

| Key | Environment variable | Permission | Use |
|---|---|---|---|
| Key 1 | `WALLEX_API_KEY_READ` | Read only | Account and order reads, the doctor |
| Key 2 | `WALLEX_API_KEY_TRADE` | Read + Trade | Order placement, only after arming (see CONFIG_ENV.md) |

Rules:

- **Never grant Withdraw** on any key. This is a policy rule for this project, not a Wallex-verified permission model. `exchange/auth.py` also refuses any URL whose path mentions `withdraw`.
- Keys live only in a local `.env` (git-ignored) or the process environment. They are never written to the repo, the audit store, or logs. The store passes every payload and free-text column through `redact()` (`redact.py`) before writing. It does not redact identifier columns (client id, signal id, symbol, event kind, error source); it validates them against strict patterns and refuses anything else (see ARCHITECTURE.md, Persistence).
- `WALLEX_SUB_ACCOUNT_CLIENT_ID` is listed in `.env.example` and maps to the `sub-account-client-id` header in `exchange/auth.py`. Its exact use is not specified in the plan.

## Contract tests

- Saved public responses live under `tests/fixtures/` and are parsed by `tests/test_wallex_public.py`.
- Mutated fixtures (missing fields, wrong types, bad volumes, failed envelopes) must fail closed. The tests check this.
- A fixture passing its contract test moves the matching row above to **observed**, with the fixture path noted in the table.
- The fixtures are one public capture (see `FETCHED_AT.txt`). They do not confirm any private endpoint or any row marked unverified.

## Network calls

- By default (mock mode, tests, the history downloader): only unauthenticated GET requests to `api.wallex.ir` from `exchange/wallex_public.py`, host pinned. The downloader (`data/history.py`) uses these GETs only and writes to `data/history/`.
- Private calls and the POST path exist only in `exchange/wallex_private.py` and need arming (above). Tests inject a fake transport; no test touches the network. No withdraw path.
- `exchange/auth.py` builds auth headers, the HTTPS opener and the arming checks.
- `exchange/mock_wallex.py` makes no network calls.
