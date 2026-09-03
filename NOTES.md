# Notes

## Decisions

The endpoint's job is to be trustworthy to a language model quoting a number to a
paying customer, so the guiding rule was: **never return a number we can't stand
behind, and never mislabel the one we do return.**

- **No rate for the date asked (weekend/holiday).** This is the core case. The
  ECB doesn't publish on weekends/holidays, and Frankfurter answers such a date
  with the most recent prior trading day's rate — and, crucially, reports *that*
  day in its `date` field. I read that field into `rate_date` and echo the
  request in `asked_date`. So a Saturday request succeeds (200) but visibly shows
  `rate_date` = the Friday. The agent can see the mismatch and tell the customer
  which day the number is from. I chose to answer rather than error, because the
  data is real and honestly labelled.
- **Future date / before 1999-01-04** are rejected client-side without touching
  the upstream — we can't have a rate we don't have, and rejecting is clearer
  than returning a stale "latest" rate under a future label.
- **`from == to`** is an error (`same_currency`) rather than a hardcoded 1.0. A
  rate of 1.0 would need a `rate_date`, and inventing one would violate the rule
  above. Cleaner to ask the caller for two different currencies.
- **Amount** must be positive with ≤4 decimal places; `result` is rounded to 2.
  Ten decimal places is treated as a bad request, not silently rounded.
- **Upstream failures** are each given a distinct code (timeout / unreachable /
  error / invalid) so the agent can decide whether to retry or apologise, and we
  never fall back to a guessed number.
- **Caching**: keyed on `(from, to, date)` in the client instance, because a
  published rate never changes. Repeats don't re-ask upstream.
- Upstream base URL and port come from env; the client transport is injectable so
  tests fake the upstream with no network.

## With another day

- A short TTL on the "latest" (no-date) cache entry — a published rate is
  immutable, but "latest" can go stale within the process lifetime.
- Support multi-currency (`symbols=A,B,C`) and an amount-free "just the rate" mode.
- Structured logging + a `/healthz`, and a small contract test that runs against
  the real Frankfurter API in CI (kept out of the offline suite).
- Retry-with-backoff on `upstream_timeout`/5xx before giving up.

## AI tools

Claude Code (Opus). I used it to probe the real Frankfurter contract with `curl`
before writing anything (confirming the `/v1/` path, the backfill behaviour, and
the exact 404/422 bodies), then to draft `fx_client.py`, `main.py`, and the test
suite. I reviewed and adjusted every file, and ran the tests and a live server to
verify behaviour rather than trusting the generated code.

## One thing the AI got wrong

Its first instinct was to reach for `api.frankfurter.app` (the older host, no
path prefix) and to assume a future/weekend date would just error. I checked the
real API by hand: the configured default is `api.frankfurter.dev`, which uses a
`/v1/` prefix, and a weekend date returns **200 with an earlier `date`**, not an
error. That backfill detail is the whole point of the task, so getting the host
and path right — and reading the upstream's own `date` instead of assuming it
equals the request — was the key correction.
