"""
Exception engine tests.

The property that matters most is `test_nothing_is_unexplained`: every record is
either reconciled or on the exception list. A record that is neither has been
silently lost, and the books then balance over a smaller set than they were given
with nothing saying so. That is the one failure mode a finance system must not
have.

Second in importance is `test_settled_but_never_received_is_caught`. Those
settlements *look* reconciled — they appear in a MatchResult and never reach the
unresolved list — but the money never arrived. An engine that only inspected
unmatched records would report a clean close and miss the largest at-risk
category in the batch.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from ledger.domain.models import (
    CanonicalTransaction,
    Currency,
    Direction,
    Source,
    TxnStatus,
)
from ledger.exceptions.engine import ExceptionEngine, unexplained
from ledger.exceptions.taxonomy import ExceptionType, Severity
from ledger.ingestion.pipeline import IngestionPipeline
from ledger.reconciliation.engine import ReconciliationEngine
from ledger.synthetic.generator import CaseType, SyntheticGenerator


def txn(**over) -> CanonicalTransaction:
    base = dict(
        source=Source.BANK,
        external_ref="UTR_1",
        amount_minor=100_000,
        currency=Currency.INR,
        direction=Direction.INBOUND,
        value_date=date(2026, 8, 10),
        posted_at=datetime(2026, 8, 10, tzinfo=UTC),
        status=TxnStatus.SETTLED,
    )
    base.update(over)
    return CanonicalTransaction(**base)


def run(seed: int, events: int = 4000):
    batch = SyntheticGenerator(seed=seed).generate(events)
    grouped: dict[Source, list[dict]] = {s: [] for s in Source}
    for record in batch.transactions:
        grouped[record.source].append(record.raw)
    ingested, _ = IngestionPipeline().ingest(grouped)
    result = ReconciliationEngine().reconcile(ingested)
    report = ExceptionEngine().detect(ingested, result)

    gen_key = {t.txn_id: (t.source.value, t.external_ref) for t in batch.transactions}
    pipe_key = {t.txn_id: (t.source.value, t.external_ref) for t in ingested}
    return batch, ingested, result, report, gen_key, pipe_key


class TestAccounting:
    def test_nothing_is_unexplained(self):
        """Every record is reconciled or on the list. Never neither."""
        _, ingested, result, report, _, _ = run(seed=42)
        assert unexplained(ingested, result, report) == []

    def test_a_record_is_explained_only_once(self):
        _, _, _, report, _, _ = run(seed=42)
        subjects = [
            sid
            for exc in report.exceptions
            if exc.type is not ExceptionType.DUPLICATE_SUSPECTED
            for sid in exc.subject_ids
        ]
        assert len(subjects) == len(set(subjects))

    def test_deduped_reimports_are_not_also_flagged_as_duplicates(self):
        """Ingestion handled those correctly; flagging them again double-reports.

        Note the two records in a DUPLICATE_SAME_REF event share one
        (source, external_ref), so the key survives dedupe even though a record
        is dropped. Absence of the key is therefore not the thing to assert —
        absence of a duplicate exception against it is.
        """
        batch, ingested, _result, report, gen_key, pipe_key = run(seed=42)
        assert len(ingested) < len(batch.transactions), "nothing was deduped"

        flagged_as_duplicate = {
            pipe_key[sid]
            for exc in report.exceptions
            if exc.type is ExceptionType.DUPLICATE_SUSPECTED
            for sid in exc.subject_ids
            if sid in pipe_key
        }
        reimport_keys = {
            gen_key[i]
            for gt in batch.ground_truth
            if gt.case_type is CaseType.DUPLICATE_SAME_REF
            for i in gt.all_ids
        }
        assert reimport_keys, "no re-import cases generated"
        assert not (reimport_keys & flagged_as_duplicate), (
            "a re-import caught by dedupe was also raised as a suspected "
            "duplicate — the same event reported twice"
        )


class TestIncompleteMatches:
    def test_settled_but_never_received_is_caught(self):
        """The category that looks reconciled but is money that never arrived."""
        batch, _, _, report, gen_key, pipe_key = run(seed=42)

        flagged = {
            pipe_key[sid]
            for exc in report.exceptions
            if exc.type is ExceptionType.NO_COUNTERPART
            for sid in exc.subject_ids
            if sid in pipe_key
        }
        planted = missed = 0
        for gt in batch.ground_truth:
            if gt.case_type is not CaseType.BREAK_NO_BANK:
                continue
            planted += 1
            if not any(gen_key[s] in flagged for s in gt.settlement_ids):
                missed += 1

        assert planted > 0, "no BREAK_NO_BANK cases generated"
        assert missed == 0, (
            f"{missed} settled-but-never-received payouts were not flagged; "
            "they appear reconciled while the money is gone"
        )


class TestTheTwoAmCase:
    def test_ambiguous_batch_settlements_are_typed_and_carry_candidates(self):
        batch, _, _, report, _, _ = run(seed=42)
        planted = sum(
            1
            for gt in batch.ground_truth
            if gt.case_type is CaseType.ONE_TO_MANY_NO_UTR
        )
        typed = [
            e
            for e in report.exceptions
            if e.type is ExceptionType.ONE_TO_MANY_UNRESOLVED
        ]
        assert planted > 0
        # Not all are typed this precisely; the rest fall through to
        # NO_COUNTERPART, which is less specific but still honest. What must not
        # happen is silence.
        assert len(typed) > planted * 0.8

        for exc in typed:
            assert exc.candidates, "an ambiguous grouping with no candidates is useless"
            assert exc.amount_at_risk_minor > 0
            for candidate in exc.candidates:
                assert candidate.candidate_ids
                assert 0.0 <= candidate.confidence <= 1.0

    def test_candidate_groupings_actually_sum_to_the_deposit(self):
        _, ingested, _, report, _, _ = run(seed=42)
        by_id = {t.txn_id: t for t in ingested}
        checked = 0
        for exc in report.exceptions:
            if exc.type is not ExceptionType.ONE_TO_MANY_UNRESOLVED:
                continue
            deposit = by_id[exc.subject_ids[0]].amount_minor
            for candidate in exc.candidates:
                total = sum(by_id[i].amount_minor for i in candidate.candidate_ids)
                assert total == deposit, "a candidate that does not add up is noise"
                assert candidate.amount_minor == deposit
                checked += 1
        assert checked > 0

    def test_nothing_is_auto_resolved(self):
        """Surfacing candidates is the job. Choosing one is not."""
        _, _, result, report, _, _ = run(seed=42)
        matched = result.matched_ids
        for exc in report.exceptions:
            if exc.type is not ExceptionType.ONE_TO_MANY_UNRESOLVED:
                continue
            assert exc.subject_ids[0] not in matched


class TestDuplicatePayments:
    def test_double_payment_under_a_different_reference_is_flagged(self):
        """Dedupe is blind to this by design; only economics give it away."""
        batch, _, _, report, gen_key, pipe_key = run(seed=42)
        flagged = {
            pipe_key[sid]
            for exc in report.exceptions
            if exc.type is ExceptionType.DUPLICATE_SUSPECTED
            for sid in exc.subject_ids
            if sid in pipe_key
        }
        planted = missed = 0
        for gt in batch.ground_truth:
            if gt.case_type is not CaseType.DUPLICATE_DOUBLE_PAY:
                continue
            planted += 1
            if not any(gen_key[b] in flagged for b in gt.bank_ids):
                missed += 1
        assert planted > 0
        assert missed == 0

    def test_duplicate_exception_names_both_records(self):
        _, _, _, report, _, _ = run(seed=42)
        dupes = [
            e
            for e in report.exceptions
            if e.type is ExceptionType.DUPLICATE_SUSPECTED
        ]
        assert dupes
        for exc in dupes:
            assert len(exc.subject_ids) == 2, "a duplicate involves a pair"
            assert exc.severity is Severity.HIGH


class TestEveryExceptionIsActionable:
    def test_each_carries_risk_reason_and_a_next_step(self):
        _, _, _, report, _, _ = run(seed=42)
        assert report.count > 0
        for exc in report.exceptions:
            assert exc.amount_at_risk_minor >= 0
            assert len(exc.reason) > 20, "a reason a reviewer cannot use is not a reason"
            assert len(exc.suggested_resolution) > 20
            assert exc.raised_by.startswith("exception.")

    def test_totals_are_reported_for_the_headline(self):
        _, _, _, report, _, _ = run(seed=42)
        assert report.total_at_risk_minor > 0
        assert sum(report.by_type().values()) == report.count
        assert sum(report.at_risk_by_type().values()) == report.total_at_risk_minor


class TestUnitDetectors:
    def test_unmatched_record_with_nothing_nearby_is_no_counterpart(self):
        lone = txn(external_ref="UTR_lonely")
        result = ReconciliationEngine().reconcile([lone])
        report = ExceptionEngine().detect([lone], result)
        (exc,) = report.exceptions
        assert exc.type is ExceptionType.NO_COUNTERPART
        assert exc.severity is Severity.HIGH
        assert exc.amount_at_risk_minor == lone.amount_minor

    def test_outbound_and_inbound_reasons_differ(self):
        outbound = txn(external_ref="UTR_out", direction=Direction.OUTBOUND)
        result = ReconciliationEngine().reconcile([outbound])
        (exc,) = ExceptionEngine().detect([outbound], result).exceptions
        assert "payment nothing accounts for" in exc.reason
