"""
Ingestion pipeline tests.

Two properties carry the weight here.

**The accounting invariant** — every input record lands in exactly one bucket, so
`total_in == accepted + deduplicated + rejected + skipped`. A finance system that
silently loses a record produces a smaller, cleaner, completely wrong
reconciliation, and the loss is invisible precisely because the output looks fine.

**Idempotency** — re-ingesting the same file must be a no-op. Re-running after a
partial failure is the normal case in finance ops, and a pipeline that doubles
its input on a retry is worse than one that crashes.
"""

from __future__ import annotations

import logging

import pytest

from ledger.domain.models import Source
from ledger.ingestion.pipeline import (
    IngestionPipeline,
    IngestionReport,
    default_connectors,
)
from ledger.synthetic.generator import SyntheticGenerator


def raw_by_source(batch) -> dict[Source, list[dict]]:
    """Group a generated batch's raw payloads the way a real feed would arrive."""
    grouped: dict[Source, list[dict]] = {s: [] for s in Source}
    for txn in batch.transactions:
        grouped[txn.source].append(txn.raw)
    return grouped


class TestAccountingInvariant:
    def test_report_check_rejects_a_lost_record(self):
        report = IngestionReport(total_in=10, accepted=4, deduplicated=1)
        with pytest.raises(RuntimeError, match="does not balance"):
            report.check()

    def test_report_check_passes_when_balanced(self):
        report = IngestionReport(
            total_in=10, accepted=6, deduplicated=2, rejected=1, skipped=1
        )
        report.check()  # must not raise

    def test_nothing_is_lost_on_a_real_batch(self):
        batch = SyntheticGenerator(seed=5).generate(300)
        pipeline = IngestionPipeline()
        txns, report = pipeline.ingest(raw_by_source(batch))

        # check() runs inside ingest(); assert the numbers explicitly too.
        assert report.total_in == len(batch.transactions)
        assert (
            report.accepted
            + report.deduplicated
            + report.rejected
            + report.skipped
            == report.total_in
        )
        assert len(txns) == report.accepted


class TestDedupe:
    def test_same_ref_reimport_is_dropped(self):
        batch = SyntheticGenerator(seed=7).generate(200)
        grouped = raw_by_source(batch)
        pipeline = IngestionPipeline()
        _, first = pipeline.ingest(grouped)

        # The generator plants DUPLICATE_SAME_REF cases, so a single pass already
        # contains re-imports that dedupe must catch.
        assert first.deduplicated > 0

    def test_reingesting_the_same_batch_accepts_nothing_new(self):
        """Idempotency: the second run is a no-op, not a doubling."""
        batch = SyntheticGenerator(seed=11).generate(200)
        grouped = raw_by_source(batch)
        pipeline = IngestionPipeline()

        first_txns, first = pipeline.ingest(grouped)
        second_txns, second = pipeline.ingest(grouped)

        assert first.accepted > 0
        assert second.accepted == 0, "a retry must not double the ledger"
        assert second_txns == []
        assert second.deduplicated == second.total_in
        assert len(first_txns) == first.accepted

    def test_stateless_mode_does_not_remember_between_batches(self):
        batch = SyntheticGenerator(seed=13).generate(100)
        grouped = raw_by_source(batch)
        pipeline = IngestionPipeline(remember_across_batches=False)

        _, first = pipeline.ingest(grouped)
        _, second = pipeline.ingest(grouped)
        assert second.accepted == first.accepted

    def test_reset_clears_memory(self):
        batch = SyntheticGenerator(seed=17).generate(100)
        grouped = raw_by_source(batch)
        pipeline = IngestionPipeline()

        _, first = pipeline.ingest(grouped)
        pipeline.reset()
        _, third = pipeline.ingest(grouped)
        assert third.accepted == first.accepted

    def test_accepted_records_are_unique_by_dedupe_key(self):
        batch = SyntheticGenerator(seed=19).generate(400)
        txns, _ = IngestionPipeline().ingest(raw_by_source(batch))
        keys = [t.dedupe_key for t in txns]
        assert len(keys) == len(set(keys))


class TestRejections:
    def test_one_bad_row_does_not_discard_the_batch(self):
        """The property that matters at 50k: isolate the failure, keep the rest."""
        batch = SyntheticGenerator(seed=23).generate(50)
        grouped = raw_by_source(batch)
        # Corrupt one bank row: blank reference is unidentifiable.
        assert grouped[Source.BANK], "need bank rows for this test"
        broken = dict(grouped[Source.BANK][0])
        broken["Ref No./Cheque No."] = ""
        grouped[Source.BANK] = [broken, *grouped[Source.BANK][1:]]

        txns, report = IngestionPipeline().ingest(grouped)

        assert report.rejected == 1
        # The batch also carries planted re-imports, so the right assertion is
        # the invariant, not a fixed arithmetic: everything except the one bad
        # row and the duplicates still came through.
        assert report.accepted == (
            report.total_in - report.rejected - report.deduplicated
        )
        assert report.accepted > 100, "one bad row must not discard the batch"
        assert len(txns) == report.accepted

    def test_rejection_carries_a_locator_and_no_pii(self):
        grouped = {
            Source.BANK: [
                {
                    "Txn Date": "02/08/2026",
                    "Value Date": "02/08/2026",
                    "Description": "NEFT CR-HDFC0000123-SENSITIVE PERSON-UTR1",
                    "Ref No./Cheque No.": "UTR1",
                    "Debit": "5.00",
                    "Credit": "5.00",  # both populated -> rejected
                    "Balance": "0.00",
                }
            ]
        }
        _, report = IngestionPipeline().ingest(grouped)

        assert report.rejected == 1
        rejection = report.rejections[0]
        assert rejection.locator
        blob = f"{rejection.locator} {rejection.detail}"
        assert "SENSITIVE PERSON" not in blob

    def test_skipped_rows_are_counted_not_lost(self):
        grouped = {
            Source.BANK: [
                {
                    "Txn Date": "01/08/2026",
                    "Value Date": "01/08/2026",
                    "Description": "Opening Balance",
                    "Ref No./Cheque No.": "",
                    "Debit": "",
                    "Credit": "",
                    "Balance": "1,00,000.00",
                }
            ]
        }
        _, report = IngestionPipeline().ingest(grouped)
        assert report.skipped == 1
        assert report.rejected == 0
        report.check()


class TestRouting:
    def test_every_source_routes_to_its_connector(self):
        batch = SyntheticGenerator(seed=29).generate(200)
        _, report = IngestionPipeline().ingest(raw_by_source(batch))
        assert set(report.per_source) == {s.value for s in Source}

    def test_unregistered_source_fails_loudly(self):
        connectors = dict(default_connectors())
        del connectors[Source.BANK]
        pipeline = IngestionPipeline(connectors)
        with pytest.raises(KeyError, match="no connector registered"):
            pipeline.ingest({Source.BANK: [{}]})


class TestLogging:
    def test_duplicate_log_line_contains_no_record_content(self, caplog):
        batch = SyntheticGenerator(seed=31).generate(100)
        grouped = raw_by_source(batch)
        pipeline = IngestionPipeline()
        pipeline.ingest(grouped)

        with caplog.at_level(logging.DEBUG, logger="ledger.ingestion.pipeline"):
            pipeline.ingest(grouped)  # every record is now a duplicate

        assert caplog.records, "expected debug lines for dropped duplicates"
        for record in caplog.records:
            message = record.getMessage()
            assert "NEFT" not in message
            assert "Balance" not in message
