"""
Cascade tests.

The most valuable test here is `test_no_false_matches`. Match rate is easy to
raise by guessing harder; a false match is the failure that actually costs money,
because a wrongly reconciled record tells the books everything is fine and nobody
ever looks at it again. So the suite checks correctness before coverage.

A note on identity, because it caused a genuinely misleading result while writing
these: `txn_id` is assigned at construction, so records that come back out of the
ingestion pipeline have **different** ids from the generator's originals. Any
comparison against ground truth must bridge on `(source, external_ref)`, which
survives the round trip. Keying on `txn_id` silently matches nothing and every
assertion passes vacuously.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from ledger.domain.models import (
    CanonicalTransaction,
    Currency,
    Direction,
    MatchTier,
    Source,
    TxnStatus,
)
from ledger.ingestion.pipeline import IngestionPipeline
from ledger.reconciliation.blocking import BlockingIndex, amount_band
from ledger.reconciliation.deterministic import (
    tier0_group_ref,
    tier0_parent_ref,
    tier1_economic_pair,
)
from ledger.reconciliation.engine import ReconciliationEngine, tier_breakdown
from ledger.synthetic.generator import (
    CaseType,
    ExpectedHandling,
    SyntheticGenerator,
)


def txn(**over) -> CanonicalTransaction:
    """A canonical record with sensible defaults, overridable per test."""
    base = dict(
        source=Source.LEDGER,
        external_ref="order_1",
        amount_minor=100_000,
        currency=Currency.INR,
        direction=Direction.INBOUND,
        value_date=date(2026, 8, 10),
        posted_at=datetime(2026, 8, 10, tzinfo=UTC),
        status=TxnStatus.CAPTURED,
    )
    base.update(over)
    return CanonicalTransaction(**base)


def reconcile_batch(seed: int, events: int = 4000):
    """Generate -> ingest -> reconcile, with a ground-truth bridge."""
    batch = SyntheticGenerator(seed=seed).generate(events)
    grouped: dict[Source, list[dict]] = {s: [] for s in Source}
    for record in batch.transactions:
        grouped[record.source].append(record.raw)
    ingested, _ = IngestionPipeline().ingest(grouped)
    result = ReconciliationEngine().reconcile(ingested)

    gen_key = {t.txn_id: (t.source.value, t.external_ref) for t in batch.transactions}
    pipe_key = {t.txn_id: (t.source.value, t.external_ref) for t in ingested}
    return batch, ingested, result, gen_key, pipe_key


class TestTier0ParentRef:
    def test_child_links_to_its_ledger_entry(self):
        parent = txn(external_ref="order_9")
        child = txn(
            source=Source.SETTLEMENT, external_ref="pout_9", parent_ref="order_9"
        )
        (match,) = tier0_parent_ref([parent, child])
        assert match.tier is MatchTier.EXACT
        assert match.confidence == 1.0
        assert match.right_ids == [parent.txn_id]

    def test_amount_may_differ_because_settlement_is_net_of_fees(self):
        """The key is the reference. Requiring equal amounts breaks fee cases."""
        parent = txn(external_ref="order_9", amount_minor=100_000)
        child = txn(
            source=Source.SETTLEMENT,
            external_ref="pout_9",
            parent_ref="order_9",
            amount_minor=98_000,
            fee_minor=2_000,
        )
        assert len(tier0_parent_ref([parent, child])) == 1

    def test_ambiguous_parent_is_refused_not_guessed(self):
        """Collision policy: two candidates means no match, not a coin-flip."""
        a = txn(external_ref="order_dup")
        b = txn(external_ref="order_dup")
        child = txn(
            source=Source.GATEWAY, external_ref="pay_1", parent_ref="order_dup"
        )
        assert tier0_parent_ref([a, b, child]) == []

    def test_missing_parent_produces_no_match(self):
        child = txn(source=Source.GATEWAY, external_ref="pay_1", parent_ref="nope")
        assert tier0_parent_ref([child]) == []

    def test_currency_must_agree(self):
        parent = txn(external_ref="order_9", currency=Currency.USD)
        child = txn(
            source=Source.GATEWAY,
            external_ref="pay_9",
            parent_ref="order_9",
            currency=Currency.INR,
        )
        assert tier0_parent_ref([parent, child]) == []


class TestTier0GroupRef:
    def test_batch_settlement_resolves_one_to_many(self):
        bank = txn(source=Source.BANK, external_ref="UTR_1", amount_minor=300_000)
        lines = [
            txn(
                source=Source.SETTLEMENT,
                external_ref=f"pout_{i}",
                group_ref="UTR_1",
                amount_minor=100_000,
            )
            for i in range(3)
        ]
        (match,) = tier0_group_ref([bank, *lines])
        assert match.is_one_to_many
        assert len(match.left_ids) == 3
        assert match.right_ids == [bank.txn_id]

    def test_refuses_when_the_parts_do_not_sum_to_the_whole(self):
        """Reference says together, arithmetic says otherwise. Trust neither."""
        bank = txn(source=Source.BANK, external_ref="UTR_1", amount_minor=999_999)
        line = txn(
            source=Source.SETTLEMENT,
            external_ref="pout_1",
            group_ref="UTR_1",
            amount_minor=100_000,
        )
        assert tier0_group_ref([bank, line]) == []

    def test_missing_group_ref_yields_nothing(self):
        """The 2 AM case in miniature: no UTR, so no deterministic grouping."""
        bank = txn(source=Source.BANK, external_ref="UTR_1", amount_minor=100_000)
        line = txn(
            source=Source.SETTLEMENT, external_ref="pout_1", amount_minor=100_000
        )
        assert tier0_group_ref([bank, line]) == []


class TestTier1:
    def test_pairs_on_economics_when_no_reference_exists(self):
        gl = txn(
            external_ref="bill_1",
            direction=Direction.OUTBOUND,
            counterparty="Kraft Supplies",
            status=TxnStatus.SETTLED,
        )
        bank = txn(
            source=Source.BANK,
            external_ref="UTR_1",
            direction=Direction.OUTBOUND,
            counterparty="KRAFT SUPPLIES",  # the bank shouts; case must not matter
            status=TxnStatus.SETTLED,
        )
        (match,) = tier1_economic_pair([gl, bank], resolved=[])
        assert match.tier is MatchTier.RULE

    def test_two_identical_payments_are_refused(self):
        """Indistinguishable on these fields, so there is no honest pairing."""
        gl = txn(external_ref="bill_1", direction=Direction.OUTBOUND, counterparty="V")
        banks = [
            txn(
                source=Source.BANK,
                external_ref=f"UTR_{i}",
                direction=Direction.OUTBOUND,
                counterparty="V",
            )
            for i in range(2)
        ]
        assert tier1_economic_pair([gl, *banks], resolved=[]) == []

    def test_already_resolved_records_are_not_repaired(self):
        gl = txn(external_ref="bill_1", direction=Direction.OUTBOUND, counterparty="V")
        bank = txn(
            source=Source.BANK,
            external_ref="UTR_1",
            direction=Direction.OUTBOUND,
            counterparty="V",
        )
        assert tier1_economic_pair([gl, bank], resolved=[bank.txn_id]) == []


class TestBlocking:
    def test_amount_band_groups_nearby_amounts(self):
        assert amount_band(10_500) == amount_band(19_999)
        assert amount_band(10_500) != amount_band(20_001)

    def test_index_only_returns_plausible_candidates(self):
        near = txn(source=Source.BANK, external_ref="a", amount_minor=100_000)
        far = txn(source=Source.BANK, external_ref="b", amount_minor=9_000_000)
        index = BlockingIndex([near, far])
        found = list(
            index.candidates(
                currency=Currency.INR,
                direction=Direction.INBOUND,
                value_date=date(2026, 8, 10),
                amount_minor=100_000,
            )
        )
        assert found == [near]

    def test_buckets_stay_small_on_a_real_batch(self):
        """If this grows into the thousands the pass is drifting to quadratic."""
        batch = SyntheticGenerator(seed=3).generate(3000)
        index = BlockingIndex(
            [t for t in batch.transactions if t.source == Source.BANK]
        )
        assert index.largest_bucket < 200


class TestEngineInvariants:
    def test_every_record_is_matched_or_unresolved_never_both(self):
        _, ingested, result, _, _ = reconcile_batch(seed=5, events=2000)
        result.check()  # runs inside reconcile too; assert explicitly
        assert result.total_in == len(ingested)
        assert result.matched_count + len(result.unresolved) == result.total_in
        assert not (result.matched_ids & {t.txn_id for t in result.unresolved})

    def test_cascade_clears_most_of_the_batch_on_the_cheap_tiers(self):
        _, _, result, _, _ = reconcile_batch(seed=7, events=2000)
        by_tier = tier_breakdown(result)
        assert by_tier["exact"] > by_tier["rule"], (
            "the deterministic tiers must carry the batch; if they do not, the "
            "reference links are broken"
        )


class TestAgainstGroundTruth:
    """The checks that decide whether the match rate means anything."""

    def setup_method(self):
        (
            self.batch,
            self.ingested,
            self.result,
            self.gen_key,
            self.pipe_key,
        ) = reconcile_batch(seed=42, events=4000)
        self.owner = {}
        for index, gt in enumerate(self.batch.ground_truth):
            for tid in gt.all_ids:
                self.owner[self.gen_key[tid]] = index
        self.matched_keys = {self.pipe_key[i] for i in self.result.matched_ids}

    def test_ground_truth_bridge_is_not_vacuous(self):
        """Guards the whole class: a broken bridge makes every check pass."""
        assert len(self.owner) > 1000
        assert len(self.matched_keys & set(self.owner)) > 1000

    def test_no_false_matches(self):
        """No match may link records from two different economic events."""
        offenders = []
        for match in self.result.matches:
            events = {
                self.owner.get(self.pipe_key[i])
                for i in (match.left_ids + match.right_ids)
            }
            events.discard(None)
            if len(events) > 1:
                offenders.append(match.reason)
        assert offenders == [], (
            f"{len(offenders)} false match(es) — a wrong auto-match is worse "
            f"than an exception. First: {offenders[:2]}"
        )

    def test_ambiguous_batch_settlements_stay_unresolved(self):
        """The 2 AM case. If this resolves, a key is leaking and the metric lies."""
        planted = leaked = 0
        for gt in self.batch.ground_truth:
            if gt.case_type is not CaseType.ONE_TO_MANY_NO_UTR:
                continue
            planted += 1
            if any(self.gen_key[i] in self.matched_keys for i in gt.bank_ids):
                leaked += 1
        assert planted > 0, "no ambiguous cases generated to check"
        assert leaked == 0

    def _matched_sources(self) -> dict[tuple[str, str], set[str]]:
        """For each record key, which sources it ended up matched against.

        "Was the record matched at all?" is the wrong question for a break. In
        BREAK_NO_BANK the generator plants a ledger entry and a settlement with no
        bank credit — and the settlement *correctly* matches its ledger entry via
        parent_ref. That is a true fact about the data, not a swallowed break. The
        break is the absent bank counterpart, so what must be checked is which
        SIDE is missing, not whether the record participates in some match.
        """
        pairs: dict[tuple[str, str], set[str]] = {}
        for match in self.result.matches:
            left = [self.pipe_key[i] for i in match.left_ids]
            right = [self.pipe_key[i] for i in match.right_ids]
            for key in left:
                pairs.setdefault(key, set()).update(source for source, _ in right)
            for key in right:
                pairs.setdefault(key, set()).update(source for source, _ in left)
        return pairs

    def test_settlement_without_a_bank_credit_never_acquires_one(self):
        """BREAK_NO_BANK: money settled that never arrived. Must stay visible."""
        against = self._matched_sources()
        planted = wrongly_banked = 0
        for gt in self.batch.ground_truth:
            if gt.case_type is not CaseType.BREAK_NO_BANK:
                continue
            planted += 1
            for sid in gt.settlement_ids:
                if Source.BANK.value in against.get(self.gen_key[sid], set()):
                    wrongly_banked += 1
        assert planted > 0, "no BREAK_NO_BANK cases generated to check"
        assert wrongly_banked == 0, (
            f"{wrongly_banked} settlement(s) with no real bank credit were "
            "matched to one — that is money reported as arrived when it did not"
        )

    def test_bank_credit_with_no_ledger_entry_stays_unexplained(self):
        """BREAK_NO_LEDGER: unexplained money in. Must not be given an owner."""
        against = self._matched_sources()
        planted = wrongly_explained = 0
        for gt in self.batch.ground_truth:
            if gt.case_type is not CaseType.BREAK_NO_LEDGER:
                continue
            planted += 1
            for bid in gt.bank_ids:
                if against.get(self.gen_key[bid]):
                    wrongly_explained += 1
        assert planted > 0
        assert wrongly_explained == 0

    def test_double_payment_leaves_a_record_unresolved(self):
        """DUPLICATE_DOUBLE_PAY: dedupe is blind to it, so it must surface here."""
        planted = fully_absorbed = 0
        for gt in self.batch.ground_truth:
            if gt.case_type is not CaseType.DUPLICATE_DOUBLE_PAY:
                continue
            planted += 1
            bank_keys = [self.gen_key[b] for b in gt.bank_ids]
            if all(k in self.matched_keys for k in bank_keys):
                fully_absorbed += 1
        assert planted > 0
        assert fully_absorbed == 0, (
            "a second payment under a different UTR was absorbed as if legitimate"
        )

    def test_should_match_events_mostly_reconcile(self):
        present = set(self.pipe_key.values())
        ok = miss = 0
        for gt in self.batch.ground_truth:
            if gt.expected_handling is not ExpectedHandling.MATCHED:
                continue
            keys = [
                self.gen_key[i] for i in gt.all_ids if self.gen_key[i] in present
            ]
            if not keys:
                continue
            if all(k in self.matched_keys for k in keys):
                ok += 1
            else:
                miss += 1
        assert ok + miss > 0
        # A floor, not a target. Raising this by loosening the matcher would
        # trade correctness for a number, which is the wrong trade.
        assert ok / (ok + miss) > 0.98
