"""
Anomaly detector tests.

The detector exists for one failure the rest of the system structurally cannot
see: the same payment made twice under different references. Matching is not wrong
about it — both payments match their own paperwork perfectly. Dedupe cannot see it
either, because the references genuinely differ.

Two tests carry the weight.

`test_every_planted_double_payment_is_caught` is recall: this is real money
leaving twice, and missing it is the expensive failure.

`test_recurring_payments_are_not_flagged` is precision, and it is the one that
keeps the detector usable. Payroll is the same amount to the same counterparty
every month; rent likewise. Any economics-only test will call those duplicates
unless something stops it. A detector that flags every payroll run gets muted,
and a muted detector catches nothing — which is worse than not having one.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from ledger.domain.models import (
    CanonicalTransaction,
    Currency,
    Direction,
    Source,
    TxnStatus,
)
from ledger.ingestion.pipeline import IngestionPipeline
from ledger.reconciliation.anomaly import AnomalyDetector, AnomalyKind
from ledger.reconciliation.engine import ReconciliationEngine
from ledger.synthetic.generator import CaseType, SyntheticGenerator

EVENTS = 6000


def txn(**over) -> CanonicalTransaction:
    base = dict(
        source=Source.BANK,
        external_ref="UTR_1",
        amount_minor=500_000,
        currency=Currency.INR,
        direction=Direction.OUTBOUND,
        value_date=date(2026, 8, 10),
        posted_at=datetime(2026, 8, 10, tzinfo=UTC),
        status=TxnStatus.SETTLED,
        counterparty="ACME RETAIL",
    )
    base.update(over)
    return CanonicalTransaction(**base)


@pytest.fixture(scope="module")
def batch_run():
    batch = SyntheticGenerator(seed=42).generate(EVENTS)
    grouped: dict[Source, list[dict]] = {s: [] for s in Source}
    for record in batch.transactions:
        grouped[record.source].append(record.raw)
    ingested, _ = IngestionPipeline().ingest(grouped)
    result = ReconciliationEngine().reconcile(ingested)
    report = AnomalyDetector().detect(ingested, result)

    gen_key = {t.txn_id: (t.source.value, t.external_ref) for t in batch.transactions}
    pipe_key = {t.txn_id: (t.source.value, t.external_ref) for t in ingested}
    case_of = {
        gen_key[i]: gt.case_type
        for gt in batch.ground_truth
        for i in gt.all_ids
    }
    return batch, report, case_of, pipe_key, result


class TestRecall:
    def test_every_planted_double_payment_is_caught(self, batch_run):
        batch, report, case_of, pipe_key, _ = batch_run
        planted = sum(
            1
            for gt in batch.ground_truth
            if gt.case_type is CaseType.DUPLICATE_DOUBLE_PAY
        )
        caught = sum(
            1
            for a in report.anomalies
            if case_of.get(pipe_key[a.primary]) is CaseType.DUPLICATE_DOUBLE_PAY
        )
        assert planted > 0, "no double payments generated"
        assert caught == planted, (
            f"{planted - caught} double payment(s) missed — real money leaving "
            "twice with nothing flagging it"
        )

    def test_detected_anomalies_carry_money_at_risk(self, batch_run):
        _, report, _, _, _ = batch_run
        assert report.count > 0
        assert report.total_at_risk_minor > 0
        for anomaly in report.anomalies:
            assert anomaly.amount_at_risk_minor > 0
            assert len(anomaly.subjects) == 2, "a duplicate involves a pair"
            assert anomaly.counterpart_ref
            assert 0.0 < anomaly.confidence <= 1.0


class TestPrecision:
    def test_false_positive_rate_is_measured_and_low(self, batch_run):
        """Reported, not assumed. A noisy detector gets muted and catches nothing."""
        _, report, case_of, pipe_key, _ = batch_run
        false_positives = [
            a
            for a in report.anomalies
            if case_of.get(pipe_key[a.primary]) is not CaseType.DUPLICATE_DOUBLE_PAY
        ]
        rate = len(false_positives) / report.count
        assert rate < 0.05, (
            f"false-positive rate {rate:.1%} ({len(false_positives)}/{report.count})"
        )

    def test_recurring_payments_are_not_flagged(self):
        """Payroll is the same amount to the same counterparty every month.

        Without a time window, an economics-only test calls every payroll run a
        duplicate of the last one.
        """
        january = txn(external_ref="UTR_jan", value_date=date(2026, 1, 1),
                      posted_at=datetime(2026, 1, 1, tzinfo=UTC),
                      counterparty="PAYROLL")
        february = txn(external_ref="UTR_feb", value_date=date(2026, 2, 1),
                       posted_at=datetime(2026, 2, 1, tzinfo=UTC),
                       counterparty="PAYROLL")
        records = [january, february]
        result = ReconciliationEngine().reconcile(records)
        report = AnomalyDetector().detect(records, result)
        assert report.count == 0, "a monthly recurring payment is not a duplicate"

    def test_same_reference_is_not_an_anomaly(self):
        """That is a re-import; ingestion dedupe owns it and already handled it."""
        original = txn(external_ref="UTR_same")
        copy = txn(external_ref="UTR_same")
        records = [original, copy]
        result = ReconciliationEngine().reconcile(records)
        report = AnomalyDetector().detect(records, result)
        assert report.count == 0

    def test_different_counterparty_is_not_a_duplicate(self):
        a = txn(external_ref="UTR_a", counterparty="ACME RETAIL")
        b = txn(external_ref="UTR_b", counterparty="BLUE MART")
        result = ReconciliationEngine().reconcile([a, b])
        assert AnomalyDetector().detect([a, b], result).count == 0

    def test_different_amount_is_not_a_duplicate(self):
        a = txn(external_ref="UTR_a", amount_minor=500_000)
        b = txn(external_ref="UTR_b", amount_minor=500_001)
        result = ReconciliationEngine().reconcile([a, b])
        assert AnomalyDetector().detect([a, b], result).count == 0


class TestTheAsymmetry:
    def test_only_unresolved_records_are_flagged(self, batch_run):
        """The reconciled twin is corroborated; the orphan is the suspicious one.

        Comparing every record against every other would flag both halves of every
        legitimate recurring payment. Anchoring on "one side reconciled, one side
        orphaned" is what separates a duplicate from a routine.

        Note this uses the fixture's own result rather than re-ingesting: txn_id is
        assigned at construction, so a second ingestion produces different ids and
        every assertion below would pass vacuously.
        """
        _, report, _, _, result = batch_run
        unresolved = {t.txn_id for t in result.unresolved}
        assert report.anomalies, "nothing to check"

        for anomaly in report.anomalies:
            assert anomaly.subjects[0] in unresolved
            assert anomaly.subjects[1] not in unresolved

    def test_case_difference_does_not_hide_a_duplicate(self):
        """The bank prints ACME RETAIL; the ledger holds Acme Retail."""
        detector = AnomalyDetector()
        assert detector._economic_key(
            txn(counterparty="ACME RETAIL")
        ) == detector._economic_key(txn(counterparty="Acme Retail"))


class TestReportShape:
    def test_kinds_are_counted(self, batch_run):
        _, report, _, _, _ = batch_run
        counts = report.by_kind()
        assert counts[AnomalyKind.DUPLICATE_PAYMENT.value] == report.count

    def test_same_day_scores_higher_than_next_day(self):
        """A re-issue usually happens immediately; a day's gap is weaker evidence.

        The first payment is reconciled deterministically through a settlement's
        group_ref, mirroring a real double payment. Two bare bank rows would not
        work: they are indistinguishable, so Tier 1's collision policy correctly
        refuses both and neither becomes the corroborated half.
        """
        detector = AnomalyDetector(window_days=2)

        def run(gap_days: int) -> float:
            settlement = txn(
                source=Source.SETTLEMENT,
                external_ref="pout_1",
                group_ref="UTR_1",
                direction=Direction.INBOUND,
            )
            first = txn(
                external_ref="UTR_1", direction=Direction.INBOUND
            )
            second = txn(
                external_ref="UTR_2",
                direction=Direction.INBOUND,
                value_date=date(2026, 8, 10 + gap_days),
                posted_at=datetime(2026, 8, 10 + gap_days, tzinfo=UTC),
            )
            records = [settlement, first, second]
            result = ReconciliationEngine().reconcile(records)
            report = detector.detect(records, result)
            assert report.count == 1, f"expected one anomaly, got {report.count}"
            return report.anomalies[0].confidence

        assert run(0) > run(1)
