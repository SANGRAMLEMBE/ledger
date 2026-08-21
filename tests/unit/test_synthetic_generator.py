"""
Tests for the synthetic generator.

The generator IS the measuring instrument. If it lies, every metric lies. So we
test not just that it runs, but that its ground truth is internally consistent:
one-to-many sums actually add up, breaks genuinely have no counterpart, the
deterministic link exists exactly where the answer key claims it does, and the
raw payloads carry enough to rebuild the canonical record.
"""

from __future__ import annotations

from datetime import date

from ledger.domain.models import Currency, Direction, Source, to_major
from ledger.synthetic.generator import (
    CaseType,
    ExpectedHandling,
    SyntheticGenerator,
)
from ledger.synthetic.raw_shapes import indian_grouped


class TestDeterminism:
    def test_same_seed_same_batch(self):
        a = SyntheticGenerator(seed=7).generate(200)
        b = SyntheticGenerator(seed=7).generate(200)
        # Same seed -> identical external_refs in the same order.
        refs_a = [t.external_ref for t in a.transactions]
        refs_b = [t.external_ref for t in b.transactions]
        assert refs_a == refs_b

    def test_different_seed_different_batch(self):
        a = SyntheticGenerator(seed=1).generate(200)
        b = SyntheticGenerator(seed=2).generate(200)
        assert [t.external_ref for t in a.transactions] != [
            t.external_ref for t in b.transactions
        ]


class TestGroundTruthConsistency:
    """The answer key must be correct, or metrics are meaningless."""

    def setup_method(self):
        self.batch = SyntheticGenerator(seed=99).generate(500)
        self.by_id = {t.txn_id: t for t in self.batch.transactions}

    def test_all_ground_truth_ids_exist(self):
        # Every id in the answer key must correspond to a real transaction.
        for gt in self.batch.ground_truth:
            for txn_id in gt.all_ids:
                assert txn_id in self.by_id, f"phantom id in ground truth: {txn_id}"

    def test_one_to_many_amounts_sum(self):
        # The single bank deposit must equal the sum of its settlement lines,
        # for BOTH the resolvable and the ambiguous variant.
        for gt in self.batch.ground_truth:
            if gt.case_type not in (
                CaseType.ONE_TO_MANY,
                CaseType.ONE_TO_MANY_NO_UTR,
            ):
                continue
            assert len(gt.bank_ids) == 1
            bank_total = self.by_id[gt.bank_ids[0]].amount_minor
            settle_total = sum(
                self.by_id[sid].amount_minor for sid in gt.settlement_ids
            )
            assert bank_total == settle_total, (
                "one-to-many ground truth is inconsistent: bank deposit "
                f"{bank_total} != sum of settlements {settle_total}"
            )

    def test_breaks_have_no_counterpart(self):
        for gt in self.batch.ground_truth:
            if gt.case_type == CaseType.BREAK_NO_BANK:
                assert gt.bank_ids == []
                assert gt.is_true_break
                assert gt.expected_exception == "no_counterpart"
            if gt.case_type == CaseType.BREAK_NO_LEDGER:
                assert gt.ledger_ids == []
                assert gt.is_true_break

    def test_fee_lag_settlement_is_net(self):
        for gt in self.batch.ground_truth:
            if gt.case_type != CaseType.FEE_LAG:
                continue
            settle = self.by_id[gt.settlement_ids[0]]
            assert settle.fee_minor > 0
            ledger = self.by_id[gt.ledger_ids[0]]
            # gross (ledger) = net (settlement) + fee
            assert ledger.amount_minor == settle.amount_minor + settle.fee_minor


class TestDeterministicLink:
    """The whole point of finding #1: Tier 0 must have a real key to join on.

    Cross-source `external_ref` equality is never true by construction (each
    source has its own ref namespace), so these tests pin down the links that DO
    exist — and, just as importantly, the case where the link is deliberately
    absent.
    """

    def setup_method(self):
        self.batch = SyntheticGenerator(seed=21).generate(600)
        self.by_id = {t.txn_id: t for t in self.batch.transactions}

    def test_clean_settlement_group_ref_equals_bank_external_ref(self):
        checked = 0
        for gt in self.batch.ground_truth:
            if gt.case_type != CaseType.CLEAN or not gt.bank_ids:
                continue
            settle = self.by_id[gt.settlement_ids[0]]
            bank = self.by_id[gt.bank_ids[0]]
            assert settle.group_ref is not None
            assert settle.group_ref == bank.external_ref, (
                "clean case must expose a deterministic settlement->bank link"
            )
            checked += 1
        assert checked > 0, "no clean cases generated to check"

    def test_gateway_and_settlement_parent_ref_point_at_the_order(self):
        checked = 0
        for gt in self.batch.ground_truth:
            if gt.case_type != CaseType.CLEAN:
                continue
            order = self.by_id[gt.ledger_ids[0]]
            for gid in gt.gateway_ids:
                assert self.by_id[gid].parent_ref == order.external_ref
            for sid in gt.settlement_ids:
                assert self.by_id[sid].parent_ref == order.external_ref
            checked += 1
        assert checked > 0

    def test_ambiguous_one_to_many_has_no_link_at_all(self):
        """The 2 AM case must genuinely lack the deterministic link.

        If the UTR leaked into these records the engine could resolve them
        trivially and the exception we build the whole demo around would never
        fire — the metric would look better and mean less.
        """
        checked = 0
        for gt in self.batch.ground_truth:
            if gt.case_type != CaseType.ONE_TO_MANY_NO_UTR:
                continue
            bank = self.by_id[gt.bank_ids[0]]
            for sid in gt.settlement_ids:
                settle = self.by_id[sid]
                assert settle.group_ref is None
                assert bank.external_ref not in str(settle.raw)
            # ...and the bank narration must not reveal it either.
            assert bank.external_ref not in bank.raw["Description"]
            assert gt.expected_handling == ExpectedHandling.EXCEPTION
            checked += 1
        assert checked > 0, "no ambiguous one-to-many cases generated"

    def test_resolvable_one_to_many_shares_one_utr(self):
        checked = 0
        for gt in self.batch.ground_truth:
            if gt.case_type != CaseType.ONE_TO_MANY:
                continue
            bank = self.by_id[gt.bank_ids[0]]
            refs = {self.by_id[sid].group_ref for sid in gt.settlement_ids}
            assert refs == {bank.external_ref}
            assert gt.expected_handling == ExpectedHandling.MATCHED
            checked += 1
        assert checked > 0


class TestDuplicateSplit:
    """Finding #3: the two duplicate kinds need opposite correct behaviour."""

    def setup_method(self):
        self.batch = SyntheticGenerator(seed=33).generate(800)
        self.by_id = {t.txn_id: t for t in self.batch.transactions}

    def test_same_ref_duplicate_is_deduped_not_excepted(self):
        checked = 0
        for gt in self.batch.ground_truth:
            if gt.case_type != CaseType.DUPLICATE_SAME_REF:
                continue
            bank_txns = [self.by_id[b] for b in gt.bank_ids]
            refs = [t.external_ref for t in bank_txns]
            # Two bank lines share a ref — dedupe on (source, external_ref) sees it.
            assert len(refs) != len(set(refs))
            assert gt.expected_handling == ExpectedHandling.DEDUPED_AT_INGESTION
            assert gt.expected_exception is None
            assert not gt.is_true_break, (
                "a deduped re-import is handled correctly and must not be "
                "scored as a break"
            )
            checked += 1
        assert checked > 0

    def test_double_pay_duplicate_is_invisible_to_dedupe(self):
        checked = 0
        for gt in self.batch.ground_truth:
            if gt.case_type != CaseType.DUPLICATE_DOUBLE_PAY:
                continue
            bank_txns = [self.by_id[b] for b in gt.bank_ids]
            refs = [t.external_ref for t in bank_txns]
            # All refs distinct => dedupe_key cannot catch it.
            assert len(refs) == len(set(refs))
            # But the economics are identical — that's what anomaly must spot.
            amounts = {t.amount_minor for t in bank_txns}
            assert len(amounts) == 1
            assert gt.expected_handling == ExpectedHandling.EXCEPTION
            assert gt.expected_exception == "duplicate_suspected"
            checked += 1
        assert checked > 0


class TestRawPayloads:
    """Finding #2: connectors need something real to parse."""

    def setup_method(self):
        self.batch = SyntheticGenerator(seed=44).generate(400)

    def test_every_transaction_carries_a_raw_payload(self):
        missing = [t for t in self.batch.transactions if not t.raw]
        assert missing == [], f"{len(missing)} transactions have empty raw"

    def test_raw_shape_matches_its_source(self):
        expected_keys = {
            Source.GATEWAY: {"id", "entity", "amount", "currency", "order_id"},
            Source.SETTLEMENT: {"payout_id", "settlement_id", "amount", "utr"},
            Source.BANK: {"Txn Date", "Value Date", "Ref No./Cheque No.", "Credit"},
            Source.LEDGER: {"voucher_no", "gl_date", "debit", "credit"},
        }
        seen = set()
        for txn in self.batch.transactions:
            assert expected_keys[txn.source] <= set(txn.raw), (
                f"{txn.source.value} raw is missing required keys"
            )
            seen.add(txn.source)
        assert seen == set(expected_keys), "not all four sources were generated"

    def test_raw_amount_is_recoverable_to_the_canonical_amount(self):
        """The round-trip property a connector will have to satisfy."""
        for txn in self.batch.transactions:
            if txn.source == Source.GATEWAY:
                assert txn.raw["amount"] == txn.amount_minor
            elif txn.source == Source.SETTLEMENT:
                assert txn.raw["amount"] == f"{txn.amount_major():.2f}"
            elif txn.source == Source.BANK:
                col = "Credit" if txn.direction == Direction.INBOUND else "Debit"
                assert txn.raw[col] == indian_grouped(txn.amount_minor, txn.currency)
                # The opposite column must be blank, not "0.00" — the trap that
                # makes naive parsers emit zero-amount rows.
                other = "Debit" if col == "Credit" else "Credit"
                assert txn.raw[other] == ""
            elif txn.source == Source.LEDGER:
                col = "debit" if txn.direction == Direction.INBOUND else "credit"
                assert txn.raw[col] == f"{txn.amount_major():.2f}"

    def test_bank_dates_are_day_first(self):
        bank = next(
            t for t in self.batch.transactions if t.source == Source.BANK
        )
        d = bank.value_date
        assert bank.raw["Value Date"] == d.strftime("%d/%m/%Y")

    def test_settlement_timestamp_carries_ist_offset(self):
        settle = next(
            t for t in self.batch.transactions if t.source == Source.SETTLEMENT
        )
        # +05:30 must survive into the raw payload so the connector is forced to
        # normalise it rather than silently treating local time as UTC.
        assert settle.raw["settled_at"].endswith("+05:30")


class TestIndianGrouping:
    def test_lakh_grouping(self):
        # 12345678 paise = 123456.78 rupees -> Indian grouping is 1,23,456.78
        assert indian_grouped(12_345_678, Currency.INR) == "1,23,456.78"

    def test_small_amounts_ungrouped(self):
        assert indian_grouped(50_000, Currency.INR) == "500.00"

    def test_crore_grouping(self):
        # 1234567890 paise = 12345678.90 -> 1,23,45,678.90
        assert indian_grouped(1_234_567_890, Currency.INR) == "1,23,45,678.90"


class TestHistoryWindowAndOutflows:
    """Data the cash forecaster needs, which Day 1 did not produce."""

    def test_history_window_is_configurable(self):
        batch = SyntheticGenerator(seed=5, history_days=365).generate(400)
        assert (batch.end_date - batch.start_date).days + 1 == 365
        for txn in batch.transactions:
            # Settlement lag can push a value_date slightly past the window end.
            assert txn.value_date >= batch.start_date

    def test_default_window_preserves_the_month_end_shape(self):
        batch = SyntheticGenerator(seed=5).generate(100)
        assert batch.start_date == date(2026, 8, 1)
        assert batch.end_date == date(2026, 8, 30)

    def test_outflows_exist(self):
        batch = SyntheticGenerator(seed=6).generate(500)
        out = [
            t for t in batch.transactions if t.direction == Direction.OUTBOUND
        ]
        assert len(out) > 0
        # Outflows must reach the BANK, or the cash position is fiction.
        assert any(t.source == Source.BANK for t in out)

    def test_recurring_outflows_land_on_a_fixed_day_each_month(self):
        batch = SyntheticGenerator(seed=8, history_days=180).generate(200)
        payroll = [
            t
            for t in batch.transactions
            if t.counterparty == "PAYROLL" and t.source == Source.BANK
        ]
        assert len(payroll) >= 3, "need several months of payroll to learn a pattern"
        assert {t.value_date.day for t in payroll} == {1}

    def test_future_bookings_are_after_the_history_window(self):
        batch = SyntheticGenerator(seed=9).generate(100)
        assert batch.scheduled_outflows, "forecaster needs booked future outflows"
        for so in batch.scheduled_outflows:
            assert so.due_date > batch.end_date
            assert so.amount_minor > 0

    def test_statement_balance_is_chronologically_coherent(self):
        """A statement whose Balance column contradicts its own rows is fiction."""
        batch = SyntheticGenerator(seed=10).generate(150)
        bank = sorted(
            (t for t in batch.transactions if t.source == Source.BANK),
            key=lambda t: (t.value_date, t.posted_at, t.external_ref, t.txn_id),
        )
        prev = None
        for txn in bank:
            bal = txn.raw["Balance"].replace(",", "")
            if prev is not None:
                delta = round(float(bal) - prev, 2)
                signed = to_major(txn.amount_minor, txn.currency)
                expect = float(
                    signed if txn.direction == Direction.INBOUND else -signed
                )
                assert abs(delta - expect) < 0.011, (
                    "balance column does not follow from the row amounts"
                )
            prev = float(bal)


class TestCoverage:
    def test_all_four_sources_present(self):
        batch = SyntheticGenerator(seed=3).generate(1000)
        sources = {t.source for t in batch.transactions}
        assert sources == {
            Source.LEDGER,
            Source.GATEWAY,
            Source.SETTLEMENT,
            Source.BANK,
        }

    def test_summary_counts_add_up(self):
        batch = SyntheticGenerator(seed=5).generate(1000)
        s = batch.summary()
        # Recurring outflows are added on top of the sampled events.
        assert s["_events_total"] >= 1000
        assert s["_transactions_total"] > 1000
        assert s["_true_breaks"] > 0
        assert s["_deduped_expected"] > 0
        assert s["_scheduled_outflows"] > 0

    def test_hard_cases_are_present(self):
        batch = SyntheticGenerator(seed=11).generate(1000)
        s = batch.summary()
        assert s.get("one_to_many", 0) > 0
        assert s.get("one_to_many_no_utr", 0) > 0
        assert s.get("break_no_bank", 0) > 0
        assert s.get("fx", 0) > 0
        assert s.get("duplicate_double_pay", 0) > 0
