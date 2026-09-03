"""Tests for the /tools/convert endpoint.

No network: the upstream is faked with an httpx MockTransport, injected in place
of the real FxClient. Every ECB error case is exercised against canned responses.
"""

from __future__ import annotations

import asyncio
from datetime import date

import httpx
import pytest
from fastapi.testclient import TestClient

import main
from fx_client import FxClient


def _json(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(status, json=payload)


class Upstream:
    """Records calls and returns whatever the test tells it to."""

    def __init__(self, handler):
        self.calls: list[httpx.Request] = []
        self._handler = handler

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self._handler(request)


def make_client(handler) -> tuple[TestClient, Upstream]:
    upstream = Upstream(handler)
    fx = FxClient(base_url="http://mock.local", transport=httpx.MockTransport(upstream))
    main.app.dependency_overrides[main.get_fx_client] = lambda: fx
    client = TestClient(main.app)
    return client, upstream


@pytest.fixture(autouse=True)
def _clear_overrides():
    yield
    main.app.dependency_overrides.clear()


# --- success paths --------------------------------------------------------

def test_success_weekday_rate_date_matches_asked():
    client, up = make_client(
        lambda r: _json({"amount": 1.0, "base": "EUR", "date": "2026-08-28",
                         "rates": {"TRY": 47.1234}})
    )
    resp = client.get("/tools/convert",
                      params={"amount": 250, "from": "EUR", "to": "TRY",
                              "date": "2026-08-28"})
    assert resp.status_code == 200
    body = resp.json()
    assert body == {
        "amount": 250,
        "from": "EUR",
        "to": "TRY",
        "rate": 47.1234,
        "result": 11780.85,
        "rate_date": "2026-08-28",
        "asked_date": "2026-08-28",
        "source": "ECB via frankfurter.dev",
    }


def test_weekend_returns_prior_trading_day_visibly():
    # Asked for a Saturday; ECB's rate belongs to the Friday. Must be visible.
    client, up = make_client(
        lambda r: _json({"amount": 1.0, "base": "EUR", "date": "2026-08-28",
                         "rates": {"TRY": 47.1234}})
    )
    resp = client.get("/tools/convert",
                      params={"amount": 100, "from": "EUR", "to": "TRY",
                              "date": "2026-08-29"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["rate_date"] == "2026-08-28"
    assert body["asked_date"] == "2026-08-29"
    assert body["rate_date"] != body["asked_date"]


def test_latest_when_no_date_asked_date_is_today():
    client, up = make_client(
        lambda r: _json({"amount": 1.0, "base": "EUR", "date": "2026-09-02",
                         "rates": {"TRY": 55.9145}})
    )
    resp = client.get("/tools/convert", params={"amount": 1, "from": "EUR", "to": "TRY"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["asked_date"] == date.today().isoformat()
    assert body["rate_date"] == "2026-09-02"
    assert up.calls[0].url.path == "/v1/latest"


def test_currency_codes_are_normalised():
    client, up = make_client(
        lambda r: _json({"amount": 1.0, "base": "EUR", "date": "2026-08-28",
                         "rates": {"USD": 1.1}})
    )
    resp = client.get("/tools/convert",
                      params={"amount": 10, "from": " eur ", "to": "usd",
                              "date": "2026-08-28"})
    assert resp.status_code == 200
    assert resp.json()["from"] == "EUR"
    assert resp.json()["to"] == "USD"


# --- caching --------------------------------------------------------------

def test_repeat_question_does_not_reask_upstream():
    client, up = make_client(
        lambda r: _json({"amount": 1.0, "base": "EUR", "date": "2026-08-28",
                         "rates": {"TRY": 47.1234}})
    )
    params = {"amount": 250, "from": "EUR", "to": "TRY", "date": "2026-08-28"}
    client.get("/tools/convert", params=params)
    client.get("/tools/convert", params={**params, "amount": 999})  # same rate key
    assert len(up.calls) == 1


# --- client-side rejections (no upstream call) ----------------------------

def test_future_date_rejected_without_upstream():
    client, up = make_client(lambda r: _json({}))
    resp = client.get("/tools/convert",
                      params={"amount": 1, "from": "EUR", "to": "TRY",
                              "date": "2999-01-01"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "future_date"
    assert up.calls == []


def test_date_before_series_rejected_without_upstream():
    client, up = make_client(lambda r: _json({}))
    resp = client.get("/tools/convert",
                      params={"amount": 1, "from": "EUR", "to": "TRY",
                              "date": "1998-01-01"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "date_out_of_range"
    assert up.calls == []


def test_same_currency_rejected_without_upstream():
    client, up = make_client(lambda r: _json({}))
    resp = client.get("/tools/convert",
                      params={"amount": 1, "from": "EUR", "to": "EUR"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "same_currency"
    assert up.calls == []


# --- parameter validation -------------------------------------------------

@pytest.mark.parametrize("amount", ["0", "-10", "1.0000000001"])
def test_bad_amount_is_invalid_amount(amount):
    client, up = make_client(lambda r: _json({}))
    resp = client.get("/tools/convert",
                      params={"amount": amount, "from": "EUR", "to": "TRY"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_amount"
    assert up.calls == []


def test_missing_amount_is_invalid_amount():
    client, up = make_client(lambda r: _json({}))
    resp = client.get("/tools/convert", params={"from": "EUR", "to": "TRY"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_amount"


@pytest.mark.parametrize("code", ["EU", "EURO", "12"])
def test_bad_currency_format_is_invalid_request(code):
    client, up = make_client(lambda r: _json({}))
    resp = client.get("/tools/convert",
                      params={"amount": 1, "from": code, "to": "TRY"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_request"


def test_bad_date_format_is_invalid_request():
    client, up = make_client(lambda r: _json({}))
    resp = client.get("/tools/convert",
                      params={"amount": 1, "from": "EUR", "to": "TRY",
                              "date": "not-a-date"})
    assert resp.status_code == 422
    assert resp.json()["error"] == "invalid_request"


# --- upstream failures ----------------------------------------------------

def test_unknown_currency_maps_upstream_404():
    client, up = make_client(lambda r: _json({"message": "not found"}, status=404))
    resp = client.get("/tools/convert",
                      params={"amount": 1, "from": "EUR", "to": "ZZZ"})
    assert resp.status_code == 404
    assert resp.json()["error"] == "unknown_currency"


def test_missing_symbol_in_200_body_is_unknown_currency():
    client, up = make_client(
        lambda r: _json({"amount": 1.0, "base": "EUR", "date": "2026-08-28", "rates": {}})
    )
    resp = client.get("/tools/convert",
                      params={"amount": 1, "from": "EUR", "to": "TRY",
                              "date": "2026-08-28"})
    assert resp.status_code == 404
    assert resp.json()["error"] == "unknown_currency"


def test_upstream_500_is_upstream_error():
    client, up = make_client(lambda r: httpx.Response(500, text="boom"))
    resp = client.get("/tools/convert",
                      params={"amount": 1, "from": "EUR", "to": "TRY"})
    assert resp.status_code == 502
    assert resp.json()["error"] == "upstream_error"


def test_non_json_body_is_upstream_invalid():
    client, up = make_client(lambda r: httpx.Response(200, text="<html>nope</html>"))
    resp = client.get("/tools/convert",
                      params={"amount": 1, "from": "EUR", "to": "TRY"})
    assert resp.status_code == 502
    assert resp.json()["error"] == "upstream_invalid"


def test_unexpected_shape_is_upstream_invalid():
    client, up = make_client(lambda r: _json({"totally": "wrong"}))
    resp = client.get("/tools/convert",
                      params={"amount": 1, "from": "EUR", "to": "TRY"})
    assert resp.status_code == 502
    assert resp.json()["error"] == "upstream_invalid"


def test_timeout_is_upstream_timeout():
    def handler(r):
        raise httpx.TimeoutException("slow", request=r)
    client, up = make_client(handler)
    resp = client.get("/tools/convert",
                      params={"amount": 1, "from": "EUR", "to": "TRY"})
    assert resp.status_code == 504
    assert resp.json()["error"] == "upstream_timeout"


def test_connection_error_is_upstream_unreachable():
    def handler(r):
        raise httpx.ConnectError("refused", request=r)
    client, up = make_client(handler)
    resp = client.get("/tools/convert",
                      params={"amount": 1, "from": "EUR", "to": "TRY"})
    assert resp.status_code == 503
    assert resp.json()["error"] == "upstream_unreachable"


def test_real_closed_port_is_unreachable():
    # End-to-end against an actually closed port (no mock), still no real network.
    async def run():
        fx = FxClient(base_url="http://127.0.0.1:9", timeout=2.0)
        try:
            with pytest.raises(Exception) as exc_info:
                await fx.get_rate("EUR", "TRY", None)
            assert exc_info.value.code == "upstream_unreachable"
        finally:
            await fx.aclose()
    asyncio.run(run())
