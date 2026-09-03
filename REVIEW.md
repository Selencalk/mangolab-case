# Review of tool.py

One page. Findings **ranked** — most harmful to a customer first. Each was
reproduced offline by swapping `tool.client` for an `httpx.MockTransport`, so the
"verify" steps are exactly what I ran.

## 1. `rate_date` is fabricated, and the cache ignores the date

`fetch_rate` never reads the upstream's own `payload["date"]`; it returns
`str(on or date.today())` — the date the caller *asked for*, not the date the
rate belongs to ([tool.py:30,44](tool.py:30)). On top of that the cache key is
`f"{base}-{target}"` with **no date** ([tool.py:28](tool.py:28)), so the first
EUR→TRY rate is reused for every later EUR→TRY call whatever date is asked.

**What it does to a customer:** they get a real-looking, wrong number stamped
with an authoritative but false date. Ask for a Saturday and you get Friday's
rate labelled Saturday; ask "the rate on 2020-01-01" and you get *today's* rate
labelled 2020-01-01. Unlike a `0.00`, this looks correct, so the agent quotes it
with confidence and the customer acts on it. This is the exact thing the service
must never do: present a rate as belonging to a date it does not.

**How I verified:** mock returns `date:2026-08-28`; ask `on=2026-08-30` → response
`rate_date:2026-08-30`. Then mock returns latest; call latest, then call
`on=2020-01-01` → both return the same rate, second one with `rate_date:2020-01-01`.

## 2. Every failure returns `rate:0.0, result:0.0` with HTTP 200

The `except Exception` swallows all errors — upstream down, timeout, unknown
currency, bad JSON — logs a `print`, and returns a 200 body with zeros
([tool.py:71-81](tool.py:71)).

**What it does to a customer:** on any upstream hiccup the agent tells them
"250 EUR = 0.00 TRY" as a successful answer. There is no error signal for the
agent to catch, so a wrong number ships as fact — worse than no number.

**How I verified:** point the client at a connection that raises `ConnectError`;
`GET /tools/convert` returns HTTP 200 with `rate:0.0, result:0.0`.

## 3. The rate is rounded to 2 dp *before* multiplying

`rate = round(rate, 2)` runs before `amount * rate` ([tool.py:60-61](tool.py:60)),
truncating the ECB rate and then scaling the error by the amount.

**What it does to a customer:** every conversion is systematically off. With
EUR/USD 1.1615, `250 × rate` returns **290.0** instead of **290.38** — the rate
was flattened to 1.16. On large amounts this is real money, lost on every call.

**How I verified:** mock returns `USD:1.1615`, ask `amount=250` → `result:290.0`,
`rate:1.16`.

## 4. The `from` parameter is silently ignored

The Python arg is `from_` with no alias, so the query key is `from_`, not `from`
([tool.py:48](tool.py:48)). A caller using the documented `?from=USD` doesn't bind
it; FastAPI falls back to the default `EUR`.

**What it does to a customer:** they ask to convert *USD* and the service
silently converts *EUR* — wrong source currency, no error. Harm depends on the
caller using `from` (the brief's spelling) rather than `from_`.

**How I verified:** the OpenAPI schema for `/tools/convert` lists the query key as
`from_` (default `EUR`); a request with `?from=USD` returns `"from":"EUR"`.

## The one I would fix before shipping tonight

**Finding 1.** It is the most dangerous because it is invisible: `0.00` and a
wrong currency at least look suspect, but a plausible rate under a wrong date
passes every sniff test and the customer trades on it. The fix is contained to
`fetch_rate`: return `payload["date"]` as the rate date, and key the cache on
`(base, target, resolved-date)` (and don't cache the mutable `latest`).

## Things that look suspicious but are fine

- **No explicit HTTP timeout.** `httpx.AsyncClient()` looks like it could hang
  forever, but its default timeout is `Timeout(5.0)` (verified), so a slow
  upstream fails after 5s rather than hanging. I'd still set it explicitly, but
  it is not the outage risk it appears to be.
- **The weekend "fall back to latest" branch** ([tool.py:36-40](tool.py:36)) looks
  load-bearing, but Frankfurter already backfills a weekend/holiday date to the
  prior trading day, so for real currencies the branch rarely fires. Its only
  practical effect is to feed the mislabelling in Finding 1.

*Minor, not customer-facing:* `UPSTREAM` is hardcoded (no `FX_UPSTREAM_BASE`, so
it can't be pointed at a fake/failover); the cache is unbounded and never expires;
the module-level `client` is never closed.
