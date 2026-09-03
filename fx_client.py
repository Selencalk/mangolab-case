"""Thin async client for the Frankfurter upstream (ECB rates).

Everything that can go wrong upstream is turned into an ``FxUpstreamError`` with a
machine code + a human sentence, so the endpoint never has to guess a number.
A small in-process cache means the same question is not asked twice.
"""

from __future__ import annotations

from datetime import date
from typing import Optional

import httpx


class FxUpstreamError(Exception):
    """A failure that originates from (or while talking to) the upstream.

    Carries the HTTP status we want to answer the caller with, a short machine
    code, and a human-readable message.
    """

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class FxClient:
    """Talks to a Frankfurter-compatible API.

    The base URL is injected (from ``FX_UPSTREAM_BASE``), never hardcoded, so a
    fake upstream can be pointed at during review. Rates for a given
    (base, symbol, date) are immutable once published, so we cache them for the
    lifetime of the process.
    """

    def __init__(
        self,
        base_url: str,
        timeout: float = 5.0,
        transport: Optional[httpx.BaseTransport] = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url, timeout=timeout, transport=transport
        )
        # key: (date_or_"latest", base, symbol) -> (rate, rate_date)
        self._cache: dict[tuple[str, str, str], tuple[float, str]] = {}

    async def aclose(self) -> None:
        await self._client.aclose()

    async def get_rate(
        self, base: str, symbol: str, on_date: Optional[date]
    ) -> tuple[float, str]:
        """Return ``(rate, rate_date)`` for ``symbol`` priced in ``base``.

        ``rate_date`` is the date the upstream says the rate actually belongs to
        — which, for a weekend/holiday, is an earlier trading day than asked.
        Raises ``FxUpstreamError`` on any upstream trouble.
        """
        cache_key = (on_date.isoformat() if on_date else "latest", base, symbol)
        if cache_key in self._cache:
            return self._cache[cache_key]

        path = f"/v1/{on_date.isoformat()}" if on_date else "/v1/latest"
        try:
            resp = await self._client.get(path, params={"base": base, "symbols": symbol})
        except httpx.TimeoutException:
            raise FxUpstreamError(
                504, "upstream_timeout", "The rates provider took too long to answer."
            )
        except httpx.HTTPError:
            # closed port, DNS failure, connection reset, ...
            raise FxUpstreamError(
                503, "upstream_unreachable", "Could not reach the rates provider."
            )

        if resp.status_code in (404, 422):
            # We validate dates client-side before calling, so a 4xx here means
            # the currency pair is not one the ECB publishes.
            raise FxUpstreamError(
                404,
                "unknown_currency",
                f"The rates provider has no data for {base}->{symbol} "
                f"on the requested date.",
            )
        if resp.status_code != 200:
            raise FxUpstreamError(
                502,
                "upstream_error",
                f"The rates provider returned an unexpected status "
                f"({resp.status_code}).",
            )

        try:
            data = resp.json()
        except ValueError:
            raise FxUpstreamError(
                502, "upstream_invalid", "The rates provider returned a non-JSON body."
            )

        rates = data.get("rates") if isinstance(data, dict) else None
        rate_date = data.get("date") if isinstance(data, dict) else None
        if not isinstance(rates, dict) or not isinstance(rate_date, str):
            raise FxUpstreamError(
                502, "upstream_invalid", "The rates provider returned an unexpected shape."
            )

        rate = rates.get(symbol)
        if not isinstance(rate, (int, float)):
            raise FxUpstreamError(
                404,
                "unknown_currency",
                f"The rates provider did not return a rate for {symbol}.",
            )

        result = (float(rate), rate_date)
        self._cache[cache_key] = result
        return result
