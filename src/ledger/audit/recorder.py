"""
Recording a batch run into the audit trail.

Kept separate from the engines on purpose. The cascade decides; this observes and
writes down what it decided. Threading an audit sink through every tier would
couple the matching logic to the logging concern and make both harder to change —
and it is exactly how audit calls end up half-applied, with some tiers recording
and others silently not.

Observing the finished result instead means the trail is complete by construction:
if a decision is in the result it is in the log, and there is no path through the
engine that quietly skips recording.

The one thing this costs is intermediate reasoning that never reaches the result —
a candidate the cascade considered and discarded without emitting anything. Where
that reasoning matters (the ranked candidates behind an ambiguous settlement) it
is already carried on the exception itself, so the trail keeps it.
"""

from __future__ import annotations

from collections.abc import Sequence

from ledger.audit.log import AuditTrail, Decision
from ledger.domain.models import CanonicalTransaction
from ledger.exceptions.engine import ExceptionReport
from ledger.ingestion.pipeline import IngestionReport
from ledger.reconciliation.engine import ReconciliationResult


def _ref(txn: CanonicalTransaction) -> str:
    """Reference a record without reproducing it. Never PII."""
    return f"{txn.source.value}:{txn.external_ref}"


def record_batch(
    trail: AuditTrail,
    transactions: Sequence[CanonicalTransaction],
    ingestion: IngestionReport,
    reconciliation: ReconciliationResult,
    exceptions: ExceptionReport,
) -> int:
    """Write the full decision trail for one batch. Returns entries written."""
    by_id = {t.txn_id: t for t in transactions}
    written = 0

    trail.record(
        Decision.BATCH_STARTED,
        actor="ingest.pipeline",
        reason=f"batch opened with {ingestion.total_in} source records",
        records_in=ingestion.total_in,
    )
    written += 1

    # Ingestion decisions. Individual accepted records are summarised rather than
    # logged one by one: 55,000 "record_ingested" lines would bury the decisions
    # that actually needed judgement, and the per-record fact is recoverable from
    # the batch anyway. Deduplications and rejections ARE judgements, so each one
    # is recorded.
    trail.record(
        Decision.RECORD_INGESTED,
        actor="ingest.pipeline",
        reason=(
            f"{ingestion.accepted} records accepted across "
            f"{len(ingestion.per_source)} sources"
        ),
        accepted=ingestion.accepted,
        per_source=ingestion.per_source,
    )
    written += 1

    if ingestion.deduplicated:
        trail.record(
            Decision.RECORD_DEDUPLICATED,
            actor="ingest.pipeline",
            reason=(
                f"{ingestion.deduplicated} record(s) already seen under the same "
                "(source, external_ref); dropped as re-imports, not exceptions"
            ),
            count=ingestion.deduplicated,
        )
        written += 1

    for rejection in ingestion.rejections:
        trail.record(
            Decision.RECORD_REJECTED,
            actor=f"connector.{rejection.source.value}",
            reason=rejection.detail,
            subjects=[f"{rejection.source.value}:{rejection.locator}"],
        )
        written += 1

    # Every match, with the evidence that produced it.
    for match in reconciliation.matches:
        subjects = [
            _ref(by_id[i])
            for i in (match.left_ids + match.right_ids)
            if i in by_id
        ]
        trail.record(
            Decision.MATCH_MADE,
            actor=f"reconciler.{match.tier.value}",
            reason=match.reason,
            subjects=subjects,
            confidence=match.confidence,
            tier=match.tier.value,
            one_to_many=match.is_one_to_many,
        )
        written += 1

    # Every refusal, with what was considered. A refusal is a decision with
    # consequences — money sat unreconciled because of it — so a trail that
    # recorded only successes could not explain the close.
    for exc in exceptions.exceptions:
        subjects = [_ref(by_id[s]) for s in exc.subject_ids if s in by_id]
        if exc.candidates:
            trail.record(
                Decision.MATCH_REFUSED,
                actor=exc.raised_by,
                reason=(
                    f"{len(exc.candidates)} candidate(s) considered, none "
                    "resolvable on the available evidence"
                ),
                subjects=subjects,
                candidates=[
                    {
                        "ids": [
                            _ref(by_id[c]) for c in candidate.candidate_ids
                            if c in by_id
                        ],
                        "confidence": candidate.confidence,
                        "amount_minor": candidate.amount_minor,
                        "reason": candidate.reason,
                    }
                    for candidate in exc.candidates
                ],
            )
            written += 1

        trail.record(
            Decision.EXCEPTION_RAISED,
            actor=exc.raised_by,
            reason=exc.reason,
            subjects=subjects,
            amount_minor=exc.amount_at_risk_minor,
            currency=exc.currency,
            exception_type=exc.type.value,
            severity=exc.severity.value,
            suggested_resolution=exc.suggested_resolution,
        )
        written += 1

    trail.record(
        Decision.BATCH_COMPLETED,
        actor="reconciler.engine",
        reason=(
            f"{reconciliation.matched_count} records matched, "
            f"{exceptions.count} exceptions raised, "
            f"{exceptions.total_at_risk_minor} minor units at risk"
        ),
        matched=reconciliation.matched_count,
        exceptions=exceptions.count,
        at_risk_minor=exceptions.total_at_risk_minor,
        elapsed_seconds=round(reconciliation.elapsed_seconds, 3),
    )
    written += 1

    return written
