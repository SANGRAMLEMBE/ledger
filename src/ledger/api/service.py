"""
The state the API serves, and the conversions into wire shapes.

WHY A SERVICE LAYER
-------------------
Routes should be thin: authenticate, validate, delegate, serialise. Business
logic in a route handler cannot be tested without HTTP, cannot be reused by the
CLI, and quietly becomes the place where rules diverge from the engine's.

So this module owns the batch state and the mapping from domain objects to wire
schemas, and the routes own nothing but the HTTP.

IN-MEMORY, AND HONEST ABOUT IT
------------------------------
A batch lives in the process. Restart and it is gone. That is a real limitation,
stated here rather than discovered: persistence is what the Postgres in
`docker-compose.yml` and the RDS in `infra/terraform/` exist for, and the
`BatchStore` interface is the seam a database-backed implementation slots into
without touching a route.

For a benchmark that runs in three seconds and a demo that runs live, in-memory is
the correct amount of machinery.

IDEMPOTENCY IS ENFORCED HERE
----------------------------
A resolution is a money decision. Networks retry, users double-click, and clients
replay on timeout — applying a resolution twice is not acceptable. Keys are
remembered and a repeat is acknowledged as a replay rather than re-applied.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ledger.api.schemas import (
    BatchSummaryOut,
    CandidateOut,
    ExceptionOut,
    ForecastOut,
    ForecastPointOut,
    MatchOut,
    Money,
    ResolveResponse,
    TransactionOut,
)
from ledger.audit.log import AuditTrail, Decision, InMemoryLog
from ledger.domain.models import CanonicalTransaction, MatchResult
from ledger.eval.harness import EvalHarness, EvalResult
from ledger.exceptions.taxonomy import ReconciliationException
from ledger.forecasting.model import Forecast
from ledger.security.rbac import Permission, Principal

INR = "INR"


def money(amount_minor: int, currency: str = INR) -> Money:
    return Money(amount_minor=amount_minor, currency=currency)


# --------------------------------------------------------------------------- #
# Domain -> wire
# --------------------------------------------------------------------------- #


def to_transaction_out(txn: CanonicalTransaction) -> TransactionOut:
    """Serialise a record. `raw` is deliberately not exposed.

    The original payload carries counterparty names and is the largest PII
    surface in the system. It stays server-side for audit and is never served,
    which is why this is an explicit construction rather than a model dump —
    a dump would start leaking any field added later.
    """
    return TransactionOut(
        txn_id=txn.txn_id,
        source=txn.source.value,
        external_ref=txn.external_ref,
        money=money(txn.amount_minor, txn.currency.value),
        fee=money(txn.fee_minor, txn.currency.value),
        direction=txn.direction.value,
        value_date=txn.value_date,
        posted_at=txn.posted_at,
        status=txn.status.value,
        counterparty=txn.counterparty,
        parent_ref=txn.parent_ref,
        group_ref=txn.group_ref,
    )


def to_exception_out(exc: ReconciliationException) -> ExceptionOut:
    return ExceptionOut(
        exception_id=exc.exception_id,
        type=exc.type.value,
        severity=exc.severity.value,
        subject_ids=list(exc.subject_ids),
        at_risk=money(exc.amount_at_risk_minor, exc.currency),
        candidates=[
            CandidateOut(
                candidate_ids=list(c.candidate_ids),
                confidence=c.confidence,
                money=money(c.amount_minor, exc.currency),
                reason=c.reason,
            )
            for c in exc.candidates
        ],
        reason=exc.reason,
        suggested_resolution=exc.suggested_resolution,
        raised_at=exc.raised_at,
        raised_by=exc.raised_by,
    )


def to_match_out(match: MatchResult) -> MatchOut:
    return MatchOut(
        match_id=match.match_id,
        left_ids=list(match.left_ids),
        right_ids=list(match.right_ids),
        tier=match.tier.value,
        confidence=match.confidence,
        reason=match.reason,
        is_one_to_many=match.is_one_to_many,
    )


def to_forecast_out(forecast: Forecast) -> ForecastOut:
    return ForecastOut(
        opening_balance=money(forecast.opening_balance_minor),
        safety_floor=money(forecast.safety_floor_minor),
        horizon_days=forecast.horizon_days,
        history_days=forecast.history_days,
        points=[
            ForecastPointOut(
                day=point.day,
                p10=money(point.p10_minor),
                p50=money(point.p50_minor),
                p90=money(point.p90_minor),
                booked_outflow=money(point.booked_outflow_minor),
            )
            for point in forecast.points
        ],
        shortfall_days=len(forecast.shortfalls()),
        shortfall_lead_days=forecast.shortfall_lead_days(),
    )


def to_summary_out(batch_id: str, outcome: EvalResult) -> BatchSummaryOut:
    return BatchSummaryOut(
        batch_id=batch_id,
        records_in=outcome.ingestion.total_in,
        records_accepted=outcome.ingestion.accepted,
        records_deduplicated=outcome.ingestion.deduplicated,
        records_rejected=outcome.ingestion.rejected,
        matched=outcome.reconciliation.matched_count,
        match_rate=outcome.match_rate,
        throughput_per_second=outcome.throughput,
        exceptions=outcome.exceptions.count,
        at_risk=money(outcome.exceptions.total_at_risk_minor),
        false_matches=outcome.false_matches,
        unexplained=outcome.unexplained_records,
        exceptions_by_type=outcome.exceptions.by_type(),
    )


# --------------------------------------------------------------------------- #
# State
# --------------------------------------------------------------------------- #


class NoBatchError(LookupError):
    """Raised when a caller asks about a batch that has not been run."""


class ExceptionNotFoundError(LookupError):
    pass


class CandidateOutOfRangeError(ValueError):
    pass


@dataclass
class ResolutionRecord:
    """A resolution that was applied, kept so a retry can be recognised."""

    exception_id: str
    resolved_by: str
    resolution: str
    accepted_candidate_index: int | None


@dataclass
class BatchStore:
    """The current batch, in memory.

    Replace with a database-backed implementation and the routes do not change —
    which is the point of putting it behind an interface rather than reaching for
    globals in the handlers.
    """

    batch_id: str = ""
    outcome: EvalResult | None = None
    transactions: list[CanonicalTransaction] = field(default_factory=list)
    forecast: Forecast | None = None
    audit: InMemoryLog = field(default_factory=InMemoryLog)
    _resolutions: dict[str, ResolutionRecord] = field(default_factory=dict)
    _by_exception: dict[str, ReconciliationException] = field(default_factory=dict)

    @property
    def has_batch(self) -> bool:
        return self.outcome is not None

    def require_batch(self) -> EvalResult:
        if self.outcome is None:
            raise NoBatchError(
                "no batch has been run; POST /v1/batches first"
            )
        return self.outcome

    # -- running -------------------------------------------------------------

    def run(self, *, seed: int, events: int) -> EvalResult:
        """Run a full reconciliation and keep the result.

        The audit sink is passed explicitly rather than defaulted, so the trail
        for a batch run through the API is the same trail the CLI produces.
        """
        sink = InMemoryLog()
        outcome = EvalHarness().run(seed=seed, events=events, audit_sink=sink)

        self.outcome = outcome
        self.transactions = list(outcome.transactions)
        self.batch_id = outcome.batch_id
        self.audit = sink
        self._by_exception = {
            exc.exception_id: exc for exc in outcome.exceptions.exceptions
        }
        self._resolutions.clear()
        return outcome

    # -- exceptions ----------------------------------------------------------

    def exception(self, exception_id: str) -> ReconciliationException:
        try:
            return self._by_exception[exception_id]
        except KeyError as exc:
            raise ExceptionNotFoundError(exception_id) from exc

    def resolve(
        self,
        *,
        exception_id: str,
        principal: Principal,
        resolution: str,
        accept_candidate_index: int | None,
        idempotency_key: str,
    ) -> ResolveResponse:
        """Apply a resolution, once.

        A repeated idempotency key is acknowledged as a replay and nothing is
        applied a second time. The permission check happens *before* the
        idempotency check would matter, so an unauthorised retry is still
        refused rather than replayed.
        """
        exception = self.exception(exception_id)

        # Confirming a candidate asserts where money went, and needs the senior
        # permission. Resolving without accepting a grouping does not.
        if accept_candidate_index is not None:
            principal.require(Permission.APPROVE_MONEY_MOVEMENT)
            if accept_candidate_index >= len(exception.candidates):
                raise CandidateOutOfRangeError(
                    f"candidate {accept_candidate_index} does not exist; this "
                    f"exception has {len(exception.candidates)}"
                )

        existing = self._resolutions.get(idempotency_key)
        if existing is not None:
            return ResolveResponse(
                exception_id=existing.exception_id,
                resolved_by=existing.resolved_by,
                resolution=existing.resolution,
                accepted_candidate_index=existing.accepted_candidate_index,
                replayed=True,
            )

        record = ResolutionRecord(
            exception_id=exception_id,
            resolved_by=principal.subject,
            resolution=resolution,
            accepted_candidate_index=accept_candidate_index,
        )
        self._resolutions[idempotency_key] = record

        # A human decision on money belongs in the same trail as the automated
        # ones, or the log explains only half the close.
        AuditTrail(self.audit, batch_id=self.batch_id).record(
            Decision.EXCEPTION_RAISED
            if accept_candidate_index is None
            else Decision.MATCH_MADE,
            actor=f"human:{principal.subject}",
            reason=f"resolved by {principal.role.value}: {resolution}",
            subjects=list(exception.subject_ids),
            amount_minor=exception.amount_at_risk_minor,
            currency=exception.currency,
            accepted_candidate_index=accept_candidate_index,
            exception_id=exception_id,
        )

        return ResolveResponse(
            exception_id=exception_id,
            resolved_by=principal.subject,
            resolution=resolution,
            accepted_candidate_index=accept_candidate_index,
            replayed=False,
        )

    def resolutions(self) -> dict[str, ResolutionRecord]:
        return dict(self._resolutions)


def paginate(items: list[Any], cursor: int, limit: int) -> tuple[list[Any], int | None]:
    """Slice a page and compute the next cursor.

    Returns `None` for the next cursor at the end rather than a cursor that would
    yield an empty page — a client should be able to stop on the envelope alone,
    without a wasted round trip.
    """
    window = items[cursor : cursor + limit]
    following = cursor + len(window)
    return window, (following if following < len(items) else None)
