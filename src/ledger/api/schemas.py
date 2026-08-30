"""
Wire schemas.

MONEY ON THE WIRE IS AN INTEGER AND A CURRENCY
----------------------------------------------
Never a float, never a pre-formatted string. `{"amount_minor": 4000000,
"currency": "INR"}` — not `40000.00`, and not `"Rs 40,000.00"`.

A JSON number is a double on the other side, and the client *will* do arithmetic
on it. Sending a formatted string is worse: it forces every consumer to write a
parser, and they will each write a slightly different one. Formatting is a
display concern and belongs at the display edge, which is the client.

This is the first thing an integrator gets wrong, so it is stated in the field
descriptions and appears in the generated OpenAPI document.

PAGINATION IS NOT OPTIONAL
--------------------------
Batches are 50k+ records. Every collection response carries a cursor. An endpoint
that returns an unbounded list works beautifully on a developer's 200-record
sample and falls over on the batch it exists to serve.

ERRORS CARRY A CORRELATION ID, NOT A RECORD
-------------------------------------------
An error response gives a machine-readable code, a human-readable message with no
record contents, and an id that ties to the server-side log where the detail
lives. Echoing the offending record back is how a customer name reaches a
screenshot in a chat thread.
"""

from __future__ import annotations

from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

MONEY_DESCRIPTION = (
    "Amount in integer minor units (paise for INR). NEVER a float and never "
    "pre-formatted: pair it with `currency` and format at the display edge."
)


class Money(BaseModel):
    """An amount and its currency, travelling together.

    Bundled rather than sent as loose fields so an amount cannot be read without
    its currency. A bare number is the bug that turns 40,000 USD into 40,000 INR.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    amount_minor: int = Field(..., description=MONEY_DESCRIPTION)
    currency: str = Field(..., min_length=3, max_length=3)


class TransactionOut(BaseModel):
    """A canonical transaction as the API exposes it.

    Deliberately excludes `raw`. The original payload carries counterparty names
    and is the single largest PII surface in the system; it stays server-side for
    audit and is never served.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    txn_id: str
    source: str
    external_ref: str
    money: Money
    fee: Money
    direction: str
    value_date: date
    posted_at: datetime
    status: str
    counterparty: str | None
    parent_ref: str | None = Field(
        default=None, description="The order this record belongs to."
    )
    group_ref: str | None = Field(
        default=None,
        description="The payout this record moved with. Null when the source "
        "omitted it — that absence is meaningful, not missing data.",
    )


class CandidateOut(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_ids: list[str]
    confidence: float = Field(..., ge=0.0, le=1.0)
    money: Money
    reason: str


class ExceptionOut(BaseModel):
    """An unresolved record, priced and actionable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exception_id: str
    type: str
    severity: str
    subject_ids: list[str]
    at_risk: Money = Field(..., description="Money unresolved because of this.")
    candidates: list[CandidateOut]
    reason: str
    suggested_resolution: str
    raised_at: datetime
    raised_by: str


class MatchOut(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    match_id: str
    left_ids: list[str]
    right_ids: list[str]
    tier: str
    confidence: float
    reason: str
    is_one_to_many: bool


class Page(BaseModel):
    """Cursor pagination envelope.

    Cursor rather than offset: offsets shift when records are added mid-scan, so
    a client paging a live batch silently skips or repeats rows. A cursor pins
    the position.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    total: int = Field(..., description="Total matching records.")
    count: int = Field(..., description="Records in this page.")
    next_cursor: str | None = Field(
        default=None, description="Pass as `cursor` for the next page. Null at end."
    )


class TransactionPage(Page):
    items: list[TransactionOut]


class ExceptionPage(Page):
    items: list[ExceptionOut]


class MatchPage(Page):
    items: list[MatchOut]


class ForecastPointOut(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    day: date
    p10: Money
    p50: Money
    p90: Money
    booked_outflow: Money


class ForecastOut(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    opening_balance: Money
    safety_floor: Money
    horizon_days: int
    history_days: int
    points: list[ForecastPointOut]
    shortfall_days: int
    shortfall_lead_days: int | None


class BatchSummaryOut(BaseModel):
    """The three numbers, plus the correctness figures that qualify them."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    batch_id: str
    records_in: int
    records_accepted: int
    records_deduplicated: int
    records_rejected: int
    matched: int
    match_rate: float
    throughput_per_second: float
    exceptions: int
    at_risk: Money
    false_matches: int = Field(
        ..., description="Wrong auto-matches. Reported separately from match "
        "rate on purpose — blending them hides the failure that costs money."
    )
    unexplained: int = Field(
        ..., description="Records neither matched nor excepted. Must be 0."
    )
    exceptions_by_type: dict[str, int]


class ResolveRequest(BaseModel):
    """Confirm how an exception should be settled."""

    model_config = ConfigDict(extra="forbid")

    resolution: str = Field(..., min_length=3, max_length=500)
    accept_candidate_index: int | None = Field(
        default=None,
        ge=0,
        description="Index of the candidate grouping being confirmed. Supplying "
        "this asserts money moved that way and requires the "
        "approve_money_movement permission — resolving without it does not.",
    )
    idempotency_key: str = Field(
        ...,
        min_length=8,
        max_length=128,
        description="Client-generated. A retried request with the same key is "
        "acknowledged without applying the resolution twice.",
    )


class ResolveResponse(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    exception_id: str
    resolved_by: str
    resolution: str
    accepted_candidate_index: int | None
    replayed: bool = Field(
        ..., description="True when this was a retry of an already-applied key."
    )


class ErrorOut(BaseModel):
    """A failure the client can act on, with no record contents in it."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(..., description="Machine-readable, stable across versions.")
    message: str = Field(..., description="Human-readable. Never echoes a record.")
    correlation_id: str = Field(
        ..., description="Quote this to find the server-side detail."
    )
