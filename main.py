"""FX conversion tool for an AI agent.

One endpoint — ``GET /tools/convert`` — that converts an amount between two
currencies using ECB rates via the Frankfurter API. The caller is a language
model talking to a paying customer, so the endpoint never invents a rate and
never presents a rate as belonging to a date it does not belong to.
"""

from __future__ import annotations

import os
import re
from contextlib import asynccontextmanager
from datetime import date
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Union

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from fx_client import FxClient, FxUpstreamError

# ECB's daily reference rate series begins on this date (Frankfurter has nothing
# earlier). Kept as a constant so we can reject out-of-range dates without a
# wasted upstream round-trip.
SERIES_START = date(1999, 1, 4)
MAX_DECIMAL_PLACES = 4
UPSTREAM_TIMEOUT = 5.0
SOURCE = "ECB via frankfurter.dev"


class ConvertError(Exception):
    """A request we refuse to answer, with a status, machine code and message."""

    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


class ConvertRequest(BaseModel):
    """Validated query parameters.

    ``from`` is a Python keyword, so it is aliased to ``from_currency``.
    Currencies are normalised to upper-case 3-letter ISO codes; ``amount`` must
    be a positive number with at most four decimal places.
    """

    model_config = ConfigDict(populate_by_name=True)

    amount: Decimal
    from_currency: str = Field(alias="from")
    to_currency: str = Field(alias="to")
    on_date: Optional[date] = Field(default=None, alias="date")

    @field_validator("amount")
    @classmethod
    def _validate_amount(cls, value: Decimal) -> Decimal:
        if value <= 0:
            raise ValueError("amount must be greater than zero")
        if -value.as_tuple().exponent > MAX_DECIMAL_PLACES:
            raise ValueError(
                f"amount may have at most {MAX_DECIMAL_PLACES} decimal places"
            )
        return value

    @field_validator("from_currency", "to_currency")
    @classmethod
    def _validate_currency(cls, value: str) -> str:
        value = value.strip().upper()
        if not re.fullmatch(r"[A-Z]{3}", value):
            raise ValueError("currency must be a 3-letter ISO code")
        return value


@asynccontextmanager
async def lifespan(app: FastAPI):
    base = os.getenv("FX_UPSTREAM_BASE", "https://api.frankfurter.dev")
    app.state.fx_client = FxClient(base_url=base, timeout=UPSTREAM_TIMEOUT)
    try:
        yield
    finally:
        await app.state.fx_client.aclose()


app = FastAPI(
    title="FX Convert Tool",
    version="1.0.0",
    summary="Convert an amount between two currencies using ECB reference rates.",
    lifespan=lifespan,
)


def get_fx_client(request: Request) -> FxClient:
    return request.app.state.fx_client


@app.exception_handler(ConvertError)
async def _convert_error_handler(_: Request, exc: ConvertError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status, content={"error": exc.code, "message": exc.message}
    )


@app.exception_handler(FxUpstreamError)
async def _upstream_error_handler(_: Request, exc: FxUpstreamError) -> JSONResponse:
    return JSONResponse(
        status_code=exc.status, content={"error": exc.code, "message": exc.message}
    )


def _parse_request(
    amount: Optional[str], from_: Optional[str], to: Optional[str], on_date: Optional[str]
) -> ConvertRequest:
    """Build a validated ``ConvertRequest`` or raise a ``ConvertError``.

    Missing/malformed values are turned into our own ``{error, message}`` shape
    rather than FastAPI's default 422 body, so every failure looks the same.
    """
    try:
        return ConvertRequest.model_validate(
            {"amount": amount, "from": from_, "to": to, "date": on_date}
        )
    except ValidationError as exc:
        first = exc.errors()[0]
        field = first["loc"][0] if first["loc"] else ""
        message = first["msg"].replace("Value error, ", "")
        # amount problems get their own code; everything else is a bad request.
        code = "invalid_amount" if field == "amount" else "invalid_request"
        if field == "amount" and amount is None:
            message = "amount is required"
        raise ConvertError(422, code, message)


def _to_number(value: Decimal) -> Union[int, float]:
    """Render a Decimal as an int when whole, else a float (matches the spec)."""
    return int(value) if value == value.to_integral_value() else float(value)


@app.get(
    "/tools/convert",
    summary="Convert an amount between two currencies using ECB reference rates.",
    description=(
        "Converts `amount` from one currency to another using European Central "
        "Bank reference rates (via Frankfurter).\n\n"
        "**Dates:** the ECB does not publish rates on weekends or holidays. When "
        "you ask for such a date, the rate from the most recent prior trading day "
        "is used and `rate_date` reports that earlier day, while `asked_date` "
        "echoes what you requested. Compare the two before quoting a number to a "
        "customer. Future dates and dates before 1999-01-04 are rejected.\n\n"
        "**On failure** the response is a non-2xx status with "
        "`{\"error\": <code>, \"message\": <sentence>}`."
    ),
    operation_id="convert_currency",
)
async def convert(
    amount: Optional[str] = Query(
        None, description="Amount to convert. Positive, up to 4 decimal places.", examples=["250"]
    ),
    from_: Optional[str] = Query(
        None, alias="from", description="Source currency, 3-letter ISO code.", examples=["EUR"]
    ),
    to: Optional[str] = Query(
        None, description="Target currency, 3-letter ISO code.", examples=["TRY"]
    ),
    on_date: Optional[str] = Query(
        None,
        alias="date",
        description="Rate date (YYYY-MM-DD). Omit for the latest published rate.",
        examples=["2026-08-28"],
    ),
    client: FxClient = Depends(get_fx_client),
) -> dict:
    """Convert `amount` from one currency to another at the ECB rate.

    Returns the rate used, the converted result, and both the date the rate
    belongs to (`rate_date`) and the date requested (`asked_date`).
    """
    req = _parse_request(amount, from_, to, on_date)

    if req.from_currency == req.to_currency:
        raise ConvertError(
            400,
            "same_currency",
            "'from' and 'to' are the same currency; nothing to convert.",
        )

    today = date.today()
    if req.on_date is not None:
        if req.on_date > today:
            raise ConvertError(
                400,
                "future_date",
                "The requested date is in the future; no rate exists yet.",
            )
        if req.on_date < SERIES_START:
            raise ConvertError(
                400,
                "date_out_of_range",
                f"The requested date is before the ECB series starts "
                f"({SERIES_START.isoformat()}).",
            )

    rate, rate_date = await client.get_rate(
        req.from_currency, req.to_currency, req.on_date
    )

    asked_date = (req.on_date or today).isoformat()
    result = (req.amount * Decimal(str(rate))).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )

    return {
        "amount": _to_number(req.amount),
        "from": req.from_currency,
        "to": req.to_currency,
        "rate": rate,
        "result": float(result),
        "rate_date": rate_date,
        "asked_date": asked_date,
        "source": SOURCE,
    }
