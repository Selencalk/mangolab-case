# FX Convert Tool

A tiny FastAPI service with one endpoint an AI agent can call as a tool. It
converts an amount between two currencies using ECB reference rates via the
[Frankfurter API](https://frankfurter.dev). It never invents a rate and never
presents a rate as belonging to a date it does not belong to.

```
GET /tools/convert?amount=250&from=EUR&to=TRY&date=2026-08-28
```

## Run it

```bash
./run.sh          # listens on $PORT (default 8080)
```

Configuration (both read by the app, nothing hardcoded):

| Env var | Default | Meaning |
|---|---|---|
| `FX_UPSTREAM_BASE` | `https://api.frankfurter.dev` | Upstream base URL (point at a fake for review) |
| `PORT` | `8080` | Port to listen on |

Interactive API docs (for humans and agents): `http://localhost:8080/docs`.

## Test it

```bash
./test.sh
```

Tests use an in-memory `httpx.MockTransport`, so they touch **no network at
all** — `FX_UPSTREAM_BASE` can point at a closed port (the script defaults it to
one to prove the point).

## Success response

`200`:

```json
{
  "amount": 250,
  "from": "EUR",
  "to": "TRY",
  "rate": 47.1234,
  "result": 11780.85,
  "rate_date": "2026-08-28",
  "asked_date": "2026-08-28",
  "source": "ECB via frankfurter.dev"
}
```

- `rate_date` — the date the rate actually belongs to (what the upstream reports).
- `asked_date` — the date the caller asked for (today, if `date` is omitted).
- `result` is `amount × rate` rounded to 2 decimals; `rate` is passed through as
  the ECB published it.

## Error response

Every failure is a non-2xx status with the same shape:

```json
{ "error": "<code>", "message": "<a sentence a person could read>" }
```

| Code | Status | When |
|---|---|---|
| `invalid_amount` | 422 | `amount` missing, zero, negative, or more than 4 decimal places |
| `invalid_request` | 422 | currency not a 3-letter code, or `date` not `YYYY-MM-DD` |
| `same_currency` | 400 | `from` and `to` are the same |
| `future_date` | 400 | `date` is in the future |
| `date_out_of_range` | 400 | `date` is before the ECB series starts (`1999-01-04`) |
| `unknown_currency` | 404 | currency code is well-formed but the ECB does not publish it |
| `upstream_timeout` | 504 | upstream took too long to answer |
| `upstream_unreachable` | 503 | could not connect to upstream (e.g. closed port) |
| `upstream_error` | 502 | upstream returned a non-2xx status (e.g. 500) |
| `upstream_invalid` | 502 | upstream returned non-JSON or an unexpected shape |

## What it does in each required case

- **No rate for the date asked (weekend/holiday):** the ECB does not publish on
  those days. Frankfurter returns the most recent prior trading day's rate and
  reports *that* date; we surface it in `rate_date` while `asked_date` echoes the
  request. The two differ, and that difference is visible so the agent can tell
  the customer which day the number is from. **Success (200).**
- **Future date:** rejected with `future_date`. We never guess a future rate and
  never call upstream for it.
- **Before the series starts:** rejected with `date_out_of_range` (before
  `1999-01-04`), without calling upstream.
- **Currency does not exist:** bad format → `invalid_request`; well-formed but not
  published by the ECB → `unknown_currency`.
- **`from` == `to`:** rejected with `same_currency`. We do not fabricate a rate of
  1.0 tied to an arbitrary date.
- **Upstream slow / 500 / not JSON:** mapped to `upstream_timeout`,
  `upstream_error`, and `upstream_invalid` respectively; a closed port is
  `upstream_unreachable`. We never return a number we did not get.
- **`amount` missing / zero / negative / 10 decimal places:** all `invalid_amount`.

## Caching

Rates for a `(from, to, date)` are immutable once published, so the first answer
is cached in-process and repeating the same dated question does not re-ask the
upstream. A dateless "latest" query is **not** cached — it can change the moment
the ECB publishes, and serving a stale rate would be worse than a fresh call.

## Layout

- `main.py` — FastAPI app, request validation (Pydantic), business rules, error mapping.
- `fx_client.py` — upstream client: HTTP call, error classification, cache.
- `test_convert.py` — offline tests covering every case above.
