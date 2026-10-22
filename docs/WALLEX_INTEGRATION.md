# Wallex integration

Status: **public fixtures exist. A private client exists (`exchange/wallex_private.py`); its request and response shapes were checked against Wallex's PUBLIC developer docs and Swagger files on 2026-10-22, but no private request has ever been made, so nothing below is tested against the live API.** Saved public GET responses live in `tests/fixtures/` (capture time in `tests/fixtures/FETCHED_AT.txt`). The official competition rules are login-gated and have not been read.

Status words: **Observed** = seen in the saved public fixtures. **Documented** = stated in a public source below (retrieved 2026-10-22); an assumption until a real response confirms it. **Unverified** = the public docs are silent; the client's choice fails closed or reconciles.

## Sources (all retrieved 2026-10-22, read-only GETs, no key)

- [SW-spot] spot Swagger 2.0: https://developers.wallex.ir/assets/files/spot-b9d06896d1009adc9ebc69b9a5495e7e.json (sha256 `10d86676…`)
- [SW-basic] basic-services Swagger: https://developers.wallex.ir/assets/files/basic-services-89626420b5f06e4d7a2dc9206cdeb192.json (sha256 `3f3effa9…`)
- [SW-tc] trade-credit OpenAPI: https://developers.wallex.ir/assets/files/trade-credit-ff5c60b5b97b77a5b2c2e4800f07729c.yaml (sha256 `d8141055…`)
- Doc pages under https://developers.wallex.ir/docs/ : [intro] `/docs/`, [key] `create-api-key`, [spot] `spot-intro`, [types] `spot-types`, [create] `spot-create-order`, [cancel] `spot-cancel-order`, [detail] `spot-single-order-detail`, [open] `spot-active-orders`, [wallet] `basic-services-main-wallet`, [fee] `basic-services-account-fee`, [markets] `spot-active-markets`, [socket] `socket-private-channel`. The pages are in Persian; "(tr.)" marks our translation.

## Verified vs unverified

| Item | Status | Source / excerpt / note |
|---|---|---|
| Host `api.wallex.ir` | Observed + Documented | [intro]: all APIs only via `https://api.wallex.ir` (tr.). The Swagger files' `host` field says `api.wallex.com`; the client stays pinned to `api.wallex.ir`. |
| Public GETs: markets (`/hector/web/v1/markets`), `udf/history`, `/v1/depth`, `/v1/trades` | Observed (fixtures) | `tests/fixtures/markets.json`, `udf_*.json`, `depth_BTCUSDT.json`, `trades_BTCUSDT.json`, parsed by `exchange/wallex_public.py`; payloads wrapped as `{"result": ..., "success": true}`. |
| Candle volume / resolutions / series | Observed (fixtures) | Volume in base units (strategy uses relative volume only). Only `1`, `60`, `1D` honoured (`15` returns 1m bars, `240`+ return 1h); 15m and 4h are aggregated from 1m. 1m forward-fills no-trade minutes; ~1500 1m bars per request; native-60 ~696 bars. |
| Public trades | Observed (fixtures) | No trade id: dedupe by (ts, price, qty, side); newest first. |
| Private paths | Documented | [SW-spot]: `POST /v1/account/orders`, `GET`/`DELETE /v1/account/orders/{client_id}`, `GET /v1/account/openOrders`; [SW-basic]: `GET /v1/account/balances`, `GET /v1/account/fee`. Same constants in the client. |
| Key header | Documented | [SW-spot] `securityDefinitions`: `"in": "header", "name": "x-api-key"` (header names are case-insensitive; we send `X-API-Key`). |
| Sub-account header | Documented | [spot], [SW-tc]: `sub-account-client-id` in the request header, only for trade-credit accounts, which also need their own key. |
| `Content-Type: application/json` | Documented, **was missing on GET/DELETE: fixed** | [intro]: Content-Type application/json must be in the header of all requests (tr.). |
| Envelope and error body | Documented | [SW-spot] `OrderResponse` `{message, result, success}`; `ErrorResponse` `{code, message, result: {field: [...]}, success}`. |
| Create body | Documented | [create], [SW-spot] `CreateOrderRequest`: all strings; side `"BUY" \| "SELL"`; type `"LIMIT" \| "MARKET" \| "STOP_LIMIT" \| "STOP_MARKET"`; `stop_Price` "Conditional": required for stop types, must not be sent otherwise (tr.). Success is HTTP 201. |
| Stop price in RESPONSES | Documented, **was read as `stop_Price`: fixed** | [SW-spot] `OrderResource` and every page example use `stopPrice` (request: `stop_Price`). Stop orders stay disabled unless `allow_stop_orders=True`. |
| Order and fill objects | Documented | [SW-spot] `OrderResource`: `clientOrderId, symbol, side, type, origQty, price, status, executedQty, executedSum, executedPrice, fee, transactTime` (integer), `created_at`/`updated_at` (strings), `stopPrice`, `fills[]`; `TradeResource`: `price, quantity, fee, feeAsset, symbol, sum, timestamp, isBuyer, …FeeCoefficient`. The client sums `fills[]` and cross-checks `executedQty`. |
| Client id | Documented (length: Unverified) | [create]: optional; "letters (A–Z), underscore(_), digits (0–9)", example `test_clientId`; must be unique, otherwise the order is not registered (tr.). Ours: `wb_` + 32 lowercase hex. |
| Order statuses | Partly documented | Examples only: `NEW`, `FILLED`, `CANCELED` ([open], [socket]); [cancel] says cancel works until the order is `FINISHED`, i.e. not fully executed (tr.), now read as FILLED only when fills cover `origQty`. No enum: `PARTIALLY_FILLED`, `CANCELLED`, `EXPIRED`, `REJECTED` and the state of an untriggered stop are **unverified** (unknown values stay open, logged). |
| Unknown order on GET | Documented | [SW-spot] `GET /v1/account/orders/{client_id}`: `404` "Order not found", schema `ErrorResponse`; matches "404 + JSON `success: false`". Body values not shown. |
| HTTP statuses | Documented / **Unverified** | [SW-spot]: create 201/400/401/422; GET by id 200/400/401/404; DELETE by id 200/400/401/422; openOrders 200/400/401/422. Not documented: 403, 409, 429 and the status of a duplicate client id, insufficient balance or a missing Trade permission (client: 409 or a duplicate/client-id hint => `DuplicateClientId`, ambiguous; 401/403 => `AuthRejected`; 429 => `RateLimited`). |
| `openOrders` shape and paging | Documented / **Unverified** | [open], [SW-spot]: query `symbol`, `page`, `per_page` (>= 1); `result` is an object whose values are order arrays (Swagger `additionalProperties`, example keys `additionalProp1..3`), plus `result_info`. The key name (we read `orders`; anything else fails closed) and the per_page cap are unverified; `GET /v1/account/orders` documents "default: 25, max: 100". Because the cap is unknown, `open_orders()` never treats a short page as the last one: it pages until a page is empty or brings no unseen client id, ignores `result_info` counts, and raises after 50 pages rather than return a possibly partial list (one extra GET in the normal case). |
| Balances | Documented / **Unverified** | [wallet], [SW-basic]: `result.balances` keyed by asset; rows `{asset, faName, fiat, value, locked, is_dust, …}`, values like `"13.97200000"`; `locked` = frozen during a withdrawal or a trade (tr.). Whether `value` includes `locked` is **unverified**: the client reads it as the total (free = value - locked, the conservative side). |
| Fee endpoint | Documented | [fee], [SW-basic] `FeeItem`: `{MARKET: {makerFeeRate, takerFeeRate, recent_days_sum}}`, example `"makerFeeRate": "0.00200000"` (a fraction, 0.2 %). Real rates are per account; see [COSTS.md](COSTS.md). |
| Order-create rate limit | Documented, not tested | [create]: at most 20 order requests per 10 s (tr.). Client bucket: 10 per 10 s. |
| Other rate limits | **Unverified** | Not documented. [SW-tc] example response headers show `x-ratelimit-limit: "600"`, `x-ratelimit-remaining: "599"` (window not stated). |
| Minimum order size / notional | **Unverified** | Not documented; [markets] documents only `amount_precision` / `price_precision`. Config placeholder `min_notional: 5` USDT. |
| Timestamps | Documented | [intro]: datetime ISO-8601 strings, `timestamp` = unix seconds; [socket] events use milliseconds. The parser accepts both. |
| Keys and permissions | Documented | [key]: at most three keys; permissions Read (default), Trade, Withdraw; a key expires on a chosen date and is shown once; a withdrawal needs the key's IP field to match (tr.). IP rules for Read/Trade: not documented. |
| MARKET orders | Documented | [types]: a MARKET order larger than the book, or one that would move the price 2 % from the last trade, is cancelled automatically (tr.). A guardian MARKET flatten can end CANCELED, possibly partially filled. |
| Fees, zero-fee markets | Documented (partly) | [markets]: `is_zero_fee` per market; false for all six USDT markets in `tests/fixtures/markets.json`. Rates and sources: [COSTS.md](COSTS.md). |
| Stop-order balance locking | **Unverified** | Not documented. |

Retry policy: public GETs (`exchange/wallex_public.py`) are retried up to 3 times on 5xx, 429, network errors and timeouts. The private client retries GET/DELETE boundedly on network errors, 408 and 5xx; a POST is sent at most once per call and never retried; any ambiguous outcome raises `BrokerTimeout`, which the OMS reconciles by client id (ARCHITECTURE.md).

## Live path is blocked without arming

`submit_order` and `cancel_order` raise `AuthError` before any I/O unless the broker was built with the trade key **and** the exact token object returned by `assert_live_armed()` (an identity check via `is_live_armed_token`; an equal-looking `Secret` arms nothing). Reads use the read key. Two identical keys are refused. Every URL passes the host allow-list (`api.wallex.ir` only; fund-movement paths refused). The CLI and the bot loop never build a live broker (mock and shadow only). `wallexbot.doctor.run_doctor` performs GET-only checks (host pin, read key, balances, fee, open orders, not-found lookup shape, clock skew, config, `.env` ignored); `python -m wallexbot doctor` runs it; it has not been run against Wallex. Run with a read-only key, it would check the real shapes of balances, fee, openOrders and the not-found lookup; statuses, error codes and fills need real orders.

## Access model

| Key | Environment variable | Permission | Use |
|---|---|---|---|
| Key 1 | `WALLEX_API_KEY_READ` | Read only | Account and order reads, the doctor |
| Key 2 | `WALLEX_API_KEY_TRADE` | Read + Trade | Order placement, only after arming (see CONFIG_ENV.md) |

- **Never grant Withdraw** on any key (project policy; Wallex documents Withdraw as an opt-in permission). `exchange/auth.py` also refuses any URL whose path mentions `withdraw`.
- Keys live only in a local `.env` (git-ignored) or the process environment, never in the repo, the audit store or logs. The store passes every payload and free-text column through `redact()` (`redact.py`); identifier columns are validated against strict patterns instead (ARCHITECTURE.md, Persistence).
- `WALLEX_SUB_ACCOUNT_CLIENT_ID` maps to the `sub-account-client-id` header. Per the docs it is only for a trade-credit account, used together with that account's own key; leave it unset for the main account.

## Contract tests

- Saved public responses under `tests/fixtures/` are parsed by `tests/test_wallex_public.py`; mutated fixtures (missing fields, wrong types, failed envelopes) must fail closed.
- Private responses in `tests/test_wallex_private.py` and `tests/test_doctor.py` are hand-written in the documented shapes (every documented field present); they test the client, not Wallex.
- A real private response that parses (the doctor) moves a row from Documented/Unverified towards Observed; record it here with the capture date.

## Network calls

- By default (mock mode, tests, the history downloader): only unauthenticated GET requests to `api.wallex.ir` from `exchange/wallex_public.py`, host pinned. The downloader (`data/history.py`) writes to `data/history/`.
- Private calls and the POST path exist only in `exchange/wallex_private.py` and need arming (above). Tests inject a fake transport; no test touches the network. No withdraw path.
- `exchange/auth.py` builds auth headers, the HTTPS opener and the arming checks; `exchange/mock_wallex.py` makes no network calls.
