"""
The evaluation harness — the only place metrics come from.

Every number the team says out loud must come from here, reproducibly, with one
command. Numbers typed into a throwaway script are not evidence: nobody can rerun
them, nobody can check them, and they quietly stop being true the moment the code
changes.

THE IDENTITY BRIDGE, AND WHY IT IS THE FIRST THING IN THIS FILE
---------------------------------------------------------------
`txn_id` is assigned when a record is constructed, so a record that has been
through ingestion has a *different* id from the generator's original. Comparing
engine output to ground truth on `txn_id` therefore matches nothing — and, worse,
every check passes: no false matches found, no breaks missed, a flawless report
that means nothing at all.

That happened while building this, and it looked like success. The bridge is
`(source, external_ref)`, which survives the round trip, and
`EvalResult.bridge_is_sound` exists so a future regression announces itself
instead of quietly producing perfect scores.

SCORING HAS THREE OUTCOMES, NOT TWO
-----------------------------------
"Did it match?" is the wrong question for a duplicate that ingestion correctly
dropped. `GroundTruthEntry.expected_handling` says which of matched / deduped /
exception is right for each planted case, and scoring against two buckets would
mark correct behaviour as a miss.

WHAT THIS MODULE REFUSES TO DO
------------------------------
It does not blend precision and recall into one "accuracy" figure. A false match
and an unmatched record are not equally bad: an unmatched record raises its hand
and costs a reviewer thirty seconds, while a false match reports the money as
reconciled and nobody ever looks again. One number would hide exactly the failure
that matters most.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from ledger.audit.log import AuditSink, AuditTrail, InMemoryLog
from ledger.audit.recorder import record_batch
from ledger.domain.models import CanonicalTransaction, Source
from ledger.exceptions.engine import ExceptionEngine, ExceptionReport, unexplained
from ledger.ingestion.pipeline import IngestionPipeline, IngestionReport
from ledger.reconciliation.engine import (
    ReconciliationEngine,
    ReconciliationResult,
    tier_breakdown,
)
from ledger.synthetic.generator import (
    CaseType,
    ExpectedHandling,
    SyntheticBatch,
    SyntheticGenerator,
)

# The seed the reported numbers come from. Nothing may be tuned against it.
HELD_OUT_SEED = 1337
DEV_SEED = 42

RecordKey = tuple[str, str]


def _key(txn: CanonicalTransaction) -> RecordKey:
    """The identity that survives ingestion. See the module docstring."""
    return (txn.source.value, txn.external_ref)


@dataclass
class EvalResult:
    """Every reported figure, and enough context to defend each one."""

    seed: int
    events: int

    ingestion: IngestionReport
    reconciliation: ReconciliationResult
    exceptions: ExceptionReport

    # Correctness against the answer key
    events_expected_match: int = 0
    events_reconciled: int = 0
    false_matches: int = 0
    missed_by_case: dict[str, int] = field(default_factory=dict)

    # Honesty
    breaks_planted: int = 0
    breaks_surfaced: int = 0
    ambiguous_planted: int = 0
    ambiguous_held: int = 0
    unexplained_records: int = 0

    # Guard against a silently vacuous comparison
    bridge_is_sound: bool = False

    # Audit
    audit_entries: int = 0
    batch_id: str = ""

    # The canonical records the batch produced. Held so a caller (the API)
    # can serve them without re-running ingestion, which would assign new
    # txn_ids and break every reference the result already handed out.
    transactions: list[CanonicalTransaction] = field(default_factory=list)

    @property
    def match_rate(self) -> float:
        """Fraction of should-match events fully reconciled, against ground truth."""
        total = self.events_expected_match
        return self.events_reconciled / total if total else 0.0

    @property
    def record_match_rate(self) -> float:
        return self.reconciliation.match_rate

    @property
    def throughput(self) -> float:
        return self.reconciliation.throughput

    @property
    def break_recall(self) -> float:
        """Fraction of planted breaks that surfaced as exceptions."""
        return (
            self.breaks_surfaced / self.breaks_planted if self.breaks_planted else 0.0
        )

    def is_publishable(self) -> tuple[bool, list[str]]:
        """Whether these numbers may be reported. Failures are disqualifying.

        Each condition below is a way the metrics could look good while being
        untrue, so a failure blocks publication rather than lowering a score.
        """
        problems: list[str] = []
        if not self.bridge_is_sound:
            problems.append(
                "ground-truth bridge matched almost nothing — the comparison is "
                "vacuous and every score below is meaningless"
            )
        if self.false_matches:
            problems.append(
                f"{self.false_matches} false match(es): money reported as "
                "reconciled against the wrong counterpart"
            )
        if self.unexplained_records:
            problems.append(
                f"{self.unexplained_records} record(s) neither reconciled nor on "
                "the exception list — silently lost"
            )
        if self.ambiguous_planted and self.ambiguous_held < self.ambiguous_planted:
            problems.append(
                f"{self.ambiguous_planted - self.ambiguous_held} ambiguous batch "
                "settlement(s) were resolved by guessing"
            )
        return (not problems, problems)


class EvalHarness:
    """Generate, ingest, reconcile, raise exceptions, then score it all."""

    def run(
        self,
        seed: int = HELD_OUT_SEED,
        events: int = 15_000,
        audit_sink: AuditSink | None = None,
    ) -> EvalResult:
        started = time.perf_counter()
        batch = SyntheticGenerator(seed=seed).generate(events)

        grouped: dict[Source, list[dict[str, Any]]] = {s: [] for s in Source}
        for record in batch.transactions:
            grouped[record.source].append(record.raw)

        ingested, ingestion = IngestionPipeline().ingest(grouped)
        reconciliation = ReconciliationEngine().reconcile(ingested)
        exceptions = ExceptionEngine().detect(ingested, reconciliation)

        result = EvalResult(
            seed=seed,
            events=events,
            ingestion=ingestion,
            reconciliation=reconciliation,
            exceptions=exceptions,
        )
        result.transactions = list(ingested)
        self._score(batch, ingested, result)
        result.unexplained_records = len(
            unexplained(ingested, reconciliation, exceptions)
        )

        # Every decision is written down. The trail is recorded from the finished
        # result rather than threaded through each tier, so it is complete by
        # construction — there is no path through the engine that skips it.
        # `audit_sink or InMemoryLog()` would be a bug: both sinks define
        # __len__, so an EMPTY sink is falsy and the caller's sink would be
        # silently discarded for a throwaway one. Test against None.
        trail = AuditTrail(
            audit_sink if audit_sink is not None else InMemoryLog()
        )
        result.batch_id = trail.batch_id
        result.audit_entries = record_batch(
            trail, ingested, ingestion, reconciliation, exceptions
        )
        result.reconciliation.elapsed_seconds = reconciliation.elapsed_seconds
        _ = time.perf_counter() - started
        return result

    # -- scoring -------------------------------------------------------------

    def _score(
        self,
        batch: SyntheticBatch,
        ingested: Sequence[CanonicalTransaction],
        result: EvalResult,
    ) -> None:
        gen_key = {t.txn_id: _key(t) for t in batch.transactions}
        pipe_key = {t.txn_id: _key(t) for t in ingested}
        present = set(pipe_key.values())

        owner: dict[RecordKey, int] = {}
        for index, entry in enumerate(batch.ground_truth):
            for txn_id in entry.all_ids:
                owner[gen_key[txn_id]] = index

        matched = {pipe_key[i] for i in result.reconciliation.matched_ids}
        flagged = {
            pipe_key[sid]
            for exc in result.exceptions.exceptions
            for sid in exc.subject_ids
            if sid in pipe_key
        }

        # If the bridge is broken every check below passes vacuously, so prove it
        # connects before trusting a single number derived from it.
        result.bridge_is_sound = (
            len(owner) > 100 and len(matched & set(owner)) > 100
        )

        # --- false matches: a match spanning two economic events ---
        for match in result.reconciliation.matches:
            events = {
                owner.get(pipe_key[i])
                for i in (match.left_ids + match.right_ids)
                if i in pipe_key
            }
            events.discard(None)
            if len(events) > 1:
                result.false_matches += 1

        # --- coverage of what should have matched ---
        missed: dict[str, int] = {}
        for entry in batch.ground_truth:
            if entry.expected_handling is not ExpectedHandling.MATCHED:
                continue
            keys = [
                gen_key[i] for i in entry.all_ids if gen_key[i] in present
            ]
            if not keys:
                continue
            result.events_expected_match += 1
            if all(k in matched for k in keys):
                result.events_reconciled += 1
            else:
                missed[entry.case_type.value] = (
                    missed.get(entry.case_type.value, 0) + 1
                )
        result.missed_by_case = missed

        # --- honesty: planted breaks must surface, ambiguity must be held ---
        for entry in batch.ground_truth:
            if entry.expected_handling is ExpectedHandling.EXCEPTION:
                result.breaks_planted += 1
                keys = [gen_key[i] for i in entry.all_ids if gen_key[i] in present]
                if any(k in flagged for k in keys):
                    result.breaks_surfaced += 1

            if entry.case_type is CaseType.ONE_TO_MANY_NO_UTR:
                result.ambiguous_planted += 1
                if not any(gen_key[b] in matched for b in entry.bank_ids):
                    result.ambiguous_held += 1


def tier_counts(result: EvalResult) -> dict[str, int]:
    return tier_breakdown(result.reconciliation)
