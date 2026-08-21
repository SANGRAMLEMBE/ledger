"""
Exception taxonomy for Ledger.

When reconciliation cannot confidently resolve a record, it does NOT guess. It
emits a typed `ReconciliationException`. This module defines the closed set of
exception types and the exception record itself.

Why this matters (and why it's a first-class part of the domain, not an
afterthought):

  - "Honest exceptions" is one of the three numbers we report to judges.
  - Every exception carries `amount_at_risk_minor` so we can state, truthfully,
    how much money is *not* being silently mis-handled.
  - Every exception carries a `suggested_resolution` and, where relevant, the
    candidate matches that were considered but not confident enough — this is
    what a human reviews in ten seconds instead of ten hours.

A record becomes an exception for exactly one reason (the `type`), even if
several conditions apply — we report the most specific one so the reviewer knows
what to do.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, datetime

from pydantic import BaseModel, ConfigDict, Field


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ExceptionType(str, enum.Enum):
    """The closed set of reasons a record cannot be auto-reconciled.

    Ordered roughly from 'we found candidates but weren't sure' to 'there is
    nothing to match against at all'.
    """

    AMBIGUOUS_MATCH = "ambiguous_match"
    # Multiple candidate matches, none confident enough to auto-resolve.
    # This is the 2 AM one-to-many case. Carries `candidates`.

    ONE_TO_MANY_UNRESOLVED = "one_to_many_unresolved"
    # A record plausibly matches a *group* on the other side, but the grouping
    # itself is uncertain (which subset of ledger lines sums to this deposit?).

    AMOUNT_MISMATCH = "amount_mismatch"
    # A single strong candidate exists but the amounts differ beyond tolerance
    # after fee/tax adjustment — needs a human to say why.

    NO_COUNTERPART = "no_counterpart"
    # No candidate on the other side at all. A payout with no bank credit, a
    # bank line with no ledger entry. The clearest 'money at risk' case.

    DUPLICATE_SUSPECTED = "duplicate_suspected"
    # Two records on the same side look like the same economic event; matching
    # one would double-count. Defense against double-settlement.

    STALE_UNMATCHED = "stale_unmatched"
    # Unmatched and older than the settlement window (e.g. > T+3). Timing lag no
    # longer explains it; it's a genuine break.

    FX_UNRESOLVED = "fx_unresolved"
    # Cross-currency pair where no rate within tolerance reconciles the amounts.


# Severity drives triage order in the human queue and dashboards.
class Severity(str, enum.Enum):
    LOW = "low"        # cosmetic / will likely self-resolve next cycle
    MEDIUM = "medium"  # needs review this cycle
    HIGH = "high"      # money at risk, review now


# Default severity per type. Overridable per-instance when context warrants.
DEFAULT_SEVERITY: dict[ExceptionType, Severity] = {
    ExceptionType.AMBIGUOUS_MATCH: Severity.MEDIUM,
    ExceptionType.ONE_TO_MANY_UNRESOLVED: Severity.MEDIUM,
    ExceptionType.AMOUNT_MISMATCH: Severity.HIGH,
    ExceptionType.NO_COUNTERPART: Severity.HIGH,
    ExceptionType.DUPLICATE_SUSPECTED: Severity.HIGH,
    ExceptionType.STALE_UNMATCHED: Severity.MEDIUM,
    ExceptionType.FX_UNRESOLVED: Severity.MEDIUM,
}


class CandidateMatch(BaseModel):
    """A considered-but-rejected match, surfaced for human review.

    This is what the reviewer sees in the exception panel: the groupings the
    system thought about, ranked by confidence, so the decision is a click.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    candidate_ids: list[str] = Field(..., min_length=1)
    confidence: float = Field(..., ge=0.0, le=1.0)
    amount_minor: int
    reason: str = Field(..., min_length=1)


class ReconciliationException(BaseModel):
    """A record the engine refused to auto-resolve, made honest and actionable."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    exception_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    type: ExceptionType
    severity: Severity
    # The record(s) that triggered the exception.
    subject_ids: list[str] = Field(..., min_length=1)
    amount_at_risk_minor: int = Field(
        ...,
        ge=0,
        description="How much money is unresolved. The number we report honestly.",
    )
    currency: str = Field(..., min_length=3, max_length=3)
    # Ranked alternatives the reviewer can pick from (empty for NO_COUNTERPART).
    candidates: list[CandidateMatch] = Field(default_factory=list)
    reason: str = Field(..., min_length=1, description="Why this could not be resolved.")
    suggested_resolution: str = Field(
        ...,
        min_length=1,
        description="What a reviewer should probably do. Never auto-applied for "
        "money-moving types.",
    )
    raised_at: datetime = Field(default_factory=_utcnow)
    raised_by: str = Field(
        default="reconciler",
        description="Which engine component raised it (for the audit trail).",
    )

    @classmethod
    def for_type(
        cls,
        exc_type: ExceptionType,
        subject_ids: list[str],
        amount_at_risk_minor: int,
        currency: str,
        reason: str,
        suggested_resolution: str,
        candidates: list[CandidateMatch] | None = None,
        raised_by: str = "reconciler",
    ) -> ReconciliationException:
        """Construct an exception with the sensible default severity for its type."""
        return cls(
            type=exc_type,
            severity=DEFAULT_SEVERITY[exc_type],
            subject_ids=subject_ids,
            amount_at_risk_minor=amount_at_risk_minor,
            currency=currency,
            candidates=candidates or [],
            reason=reason,
            suggested_resolution=suggested_resolution,
            raised_by=raised_by,
        )
