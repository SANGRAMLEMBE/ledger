"""
Load and complexity tests — proving the pass scales.

The claim the whole design rests on is that matching is **not** quadratic. The
naive approach compares every source-A record with every source-B record: at
50k x 50k that is 2.5 billion comparisons and it does not finish. Blocking keys
bound candidates to a small constant per record.

A claim like that cannot be verified by reading the code, because the failure mode
is silent — a quadratic pass on 5,000 records looks perfectly fast. It only shows
up as you scale, which is exactly when you can no longer afford to discover it.
So `test_scaling_is_not_quadratic` measures the growth curve directly: double the
input and the work must roughly double, not quadruple.

Marked `slow` and `integration`. Run them with:

    pytest -m integration
    pytest -m "not slow"        # skip during normal development
"""

from __future__ import annotations

import time

import pytest

from ledger.domain.models import Source
from ledger.eval.harness import HELD_OUT_SEED, EvalHarness
from ledger.exceptions.engine import ExceptionEngine, unexplained
from ledger.ingestion.pipeline import IngestionPipeline
from ledger.reconciliation.blocking import BlockingIndex
from ledger.reconciliation.engine import ReconciliationEngine
from ledger.synthetic.generator import SyntheticGenerator

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# The batch the track asks for is 50k+ records. 15,000 events produces ~55,000.
FULL_BATCH_EVENTS = 15_000

# Budgets. Generous on purpose: these are regression tripwires, not targets. A
# tight budget fails on a loaded CI runner and teaches everyone to ignore it,
# which is worse than having no budget at all.
MAX_RECONCILE_SECONDS = 30.0
MIN_THROUGHPUT_REC_PER_SEC = 2_000


def _ingest(seed: int, events: int):
    batch = SyntheticGenerator(seed=seed).generate(events)
    grouped: dict[Source, list[dict]] = {s: [] for s in Source}
    for record in batch.transactions:
        grouped[record.source].append(record.raw)
    ingested, report = IngestionPipeline().ingest(grouped)
    return batch, ingested, report


class TestFullBatch:
    """The batch the submission is measured on, start to finish."""

    def test_full_batch_completes_within_budget(self):
        _, ingested, ingestion = _ingest(HELD_OUT_SEED, FULL_BATCH_EVENTS)
        assert ingestion.accepted > 50_000, (
            f"the track asks for 50k+ records; got {ingestion.accepted:,}"
        )

        started = time.perf_counter()
        result = ReconciliationEngine().reconcile(ingested)
        elapsed = time.perf_counter() - started

        assert elapsed < MAX_RECONCILE_SECONDS, (
            f"reconciling {len(ingested):,} records took {elapsed:.1f}s"
        )
        assert result.throughput > MIN_THROUGHPUT_REC_PER_SEC

    def test_nothing_is_lost_at_full_scale(self):
        """The accounting invariant must hold at 55k, not just in unit tests."""
        _, ingested, _ = _ingest(HELD_OUT_SEED, FULL_BATCH_EVENTS)
        result = ReconciliationEngine().reconcile(ingested)
        exceptions = ExceptionEngine().detect(ingested, result)

        result.check()
        assert unexplained(ingested, result, exceptions) == []
        assert result.matched_count + len(result.unresolved) == len(ingested)

    def test_end_to_end_run_is_publishable_at_full_scale(self):
        outcome = EvalHarness().run(seed=HELD_OUT_SEED, events=FULL_BATCH_EVENTS)
        ok, problems = outcome.is_publishable()
        assert ok, f"full-scale run is not reportable: {problems}"
        assert outcome.false_matches == 0
        assert outcome.break_recall == 1.0


class TestComplexityBudget:
    """The blocking keys have to actually work, not merely exist."""

    def test_scaling_is_not_quadratic(self):
        """Double the input; the work must roughly double, not quadruple.

        This is the test that would catch a regression to a full cross-product.
        The threshold is 2.5x rather than 2.0x because fixed setup costs and
        garbage collection add noise at these sizes — but quadratic growth would
        show as ~4x and is nowhere near the limit.
        """
        _, small, _ = _ingest(7, 4_000)
        _, large, _ = _ingest(7, 8_000)

        ratio_in = len(large) / len(small)
        assert 1.8 < ratio_in < 2.2, f"inputs not comparable: {ratio_in:.2f}x"

        # Warm the interpreter so the first run does not carry import costs.
        ReconciliationEngine().reconcile(small)

        started = time.perf_counter()
        ReconciliationEngine().reconcile(small)
        small_seconds = time.perf_counter() - started

        started = time.perf_counter()
        ReconciliationEngine().reconcile(large)
        large_seconds = time.perf_counter() - started

        growth = large_seconds / max(small_seconds, 1e-6)
        assert growth < 2.5 * ratio_in / 2.0, (
            f"doubling the batch multiplied the work by {growth:.2f}x. "
            "Near-linear is expected; quadratic growth means a tier is "
            "comparing the full cross-product."
        )

    def test_blocking_buckets_stay_small_at_full_scale(self):
        """Bucket size is the per-record comparison count. Watch it directly."""
        _, ingested, _ = _ingest(HELD_OUT_SEED, FULL_BATCH_EVENTS)
        bank = [t for t in ingested if t.source is Source.BANK]
        index = BlockingIndex(bank)

        assert index.bucket_count > 100, "the key is not discriminating at all"
        assert index.largest_bucket < 500, (
            f"largest bucket holds {index.largest_bucket} records; the blocking "
            "key has stopped discriminating and the pass is drifting quadratic"
        )

    def test_ingestion_throughput_holds_at_full_scale(self):
        batch = SyntheticGenerator(seed=HELD_OUT_SEED).generate(FULL_BATCH_EVENTS)
        grouped: dict[Source, list[dict]] = {s: [] for s in Source}
        for record in batch.transactions:
            grouped[record.source].append(record.raw)

        started = time.perf_counter()
        ingested, _ = IngestionPipeline().ingest(grouped)
        elapsed = time.perf_counter() - started

        assert len(ingested) / elapsed > 5_000, (
            "ingestion parses every record through its connector; if this drops "
            "the bottleneck is the boundary, not the matcher"
        )


class TestRepeatability:
    def test_running_the_same_batch_twice_gives_the_same_result(self):
        """A benchmark that moves between runs cannot be reported."""
        _, ingested, _ = _ingest(HELD_OUT_SEED, 4_000)
        first = ReconciliationEngine().reconcile(ingested)
        second = ReconciliationEngine().reconcile(ingested)

        assert first.matched_count == second.matched_count
        assert len(first.unresolved) == len(second.unresolved)
        assert first.per_tier == second.per_tier

    def test_reingesting_does_not_double_the_batch(self):
        """Idempotency under a retry, at scale."""
        batch = SyntheticGenerator(seed=HELD_OUT_SEED).generate(4_000)
        grouped: dict[Source, list[dict]] = {s: [] for s in Source}
        for record in batch.transactions:
            grouped[record.source].append(record.raw)

        pipeline = IngestionPipeline()
        first, _ = pipeline.ingest(grouped)
        second, report = pipeline.ingest(grouped)

        assert first, "nothing ingested on the first pass"
        assert second == []
        assert report.accepted == 0
