"""
The exception engine — turning what the cascade could not resolve into something
a human can act on in ten seconds.

This is the half of the system that makes the other half trustworthy. A match
rate is a claim; the exception list is the evidence that the claim is honest. So
the framing here is always "this is what I would not guess at", never "this is
what I failed to do".

TWO KINDS OF UNRESOLVED, AND THE SECOND IS EASY TO MISS
-------------------------------------------------------
The obvious input is the residue: records the cascade never matched at all.

The dangerous input is the **incomplete match**. A settlement that links to its
ledger entry but has no bank credit is, by every naive measure, "matched" — it
appears in a `MatchResult`, it is not in the unresolved list, and a reconciliation
report would show it as done. But the money never arrived. Money settled and never
received is the single largest at-risk category in a real close, and looking only
at unmatched records misses all of it.

So the engine asks two questions of every record: *were you matched to anything?*
and *were you matched to everything you should have been?*

ORDER MATTERS
-------------
A record becomes an exception for exactly one reason, and the detectors run
most-specific first. Several conditions may apply to the same record; the reviewer
needs the one that tells them what to do, not the one that happens to be checked
first.

WHAT THIS MODULE WILL NOT DO
----------------------------
It will not resolve anything. For an ambiguous batch settlement it surfaces the
candidate groupings, ranked, with their sums — and stops. Picking the top one
would be a coin-flip with real money, and the whole point of the collision policy
upstream is that we do not take that flip.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from itertools import combinations

from ledger.domain.models import CanonicalTransaction, Direction, Source
from ledger.exceptions.taxonomy import (
    CandidateMatch,
    ExceptionType,
    ReconciliationException,
)
from ledger.reconciliation.engine import ReconciliationResult

# How far apart a settlement and its bank credit may sit before the pairing stops
# being plausible. Wider than the T+2 matching window on purpose: here we are
# explaining a failure, not making a match, and a slightly stale candidate is
# still useful context for a reviewer.
CANDIDATE_WINDOW_DAYS = 4

# Bounds on the grouping search. Subset-sum is exponential in general, so it is
# fenced rather than trusted: at most this many candidate lines are considered,
# and only groupings up to this size. Real batch settlements group a handful of
# payouts, not fifty. If a deposit genuinely needs a twenty-line grouping to
# explain it, that is itself worth a human looking at.
MAX_GROUPING_CANDIDATES = 12
MAX_GROUP_SIZE = 4


@dataclass
class ExceptionReport:
    """The honest list, plus the totals that make it a headline number."""

    exceptions: list[ReconciliationException]

    @property
    def count(self) -> int:
        return len(self.exceptions)

    @property
    def total_at_risk_minor(self) -> int:
        return sum(e.amount_at_risk_minor for e in self.exceptions)

    def by_type(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for exc in self.exceptions:
            counts[exc.type.value] = counts.get(exc.type.value, 0) + 1
        return counts

    def at_risk_by_type(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for exc in self.exceptions:
            totals[exc.type.value] = (
                totals.get(exc.type.value, 0) + exc.amount_at_risk_minor
            )
        return totals


class ExceptionEngine:
    """Types the cascade's leftovers, and finds the matches that only look complete."""

    def detect(
        self,
        transactions: Sequence[CanonicalTransaction],
        result: ReconciliationResult,
    ) -> ExceptionReport:
        by_id = {t.txn_id: t for t in transactions}
        matched_with = self._matched_sources(result, by_id)
        unresolved = list(result.unresolved)

        # Settlement lines that reached a ledger entry but never a bank credit.
        unbanked = [
            t
            for t in transactions
            if t.source is Source.SETTLEMENT
            and Source.BANK.value not in matched_with.get(t.txn_id, set())
        ]

        exceptions: list[ReconciliationException] = []
        claimed: set[str] = set()

        # Most specific first. Each detector skips records already explained.
        for detector in (
            self._one_to_many,
            self._duplicate_payment,
            self._fx_near_miss,
        ):
            found = detector(unresolved, unbanked, transactions, claimed)
            exceptions.extend(found)

        exceptions.extend(self._settled_never_received(unbanked, claimed))
        exceptions.extend(self._no_counterpart(unresolved, claimed))

        return ExceptionReport(exceptions=exceptions)

    # -- helpers -------------------------------------------------------------

    @staticmethod
    def _matched_sources(
        result: ReconciliationResult, by_id: dict[str, CanonicalTransaction]
    ) -> dict[str, set[str]]:
        """For each record, which sources it actually ended up linked to.

        This is what makes an incomplete match visible: a settlement whose set
        contains `ledger` but not `bank` is money that was settled and never
        received.
        """
        linked: dict[str, set[str]] = defaultdict(set)
        for match in result.matches:
            left = [i for i in match.left_ids if i in by_id]
            right = [i for i in match.right_ids if i in by_id]
            for i in left:
                linked[i].update(by_id[j].source.value for j in right)
            for i in right:
                linked[i].update(by_id[j].source.value for j in left)
        return linked

    # -- detectors -----------------------------------------------------------

    def _one_to_many(
        self,
        unresolved: Sequence[CanonicalTransaction],
        unbanked: Sequence[CanonicalTransaction],
        _all: Sequence[CanonicalTransaction],
        claimed: set[str],
    ) -> list[ReconciliationException]:
        """One deposit, several plausible groupings, no reference to choose by.

        THE 2 AM CASE. The settlement report omitted the UTR, so there is no
        deterministic link from the bank credit back to the lines that composed
        it. We look for subsets of unbanked settlement lines that sum exactly to
        the deposit, rank them, and hand them over.

        We do not pick one. When several subsets sum to the same total they are
        genuinely indistinguishable on the evidence available, and choosing would
        mean silently mis-attributing real money. A human decides in ten seconds
        with the candidates in front of them.
        """
        pool_by_currency: dict[str, list[CanonicalTransaction]] = defaultdict(list)
        for line in unbanked:
            if line.txn_id not in claimed:
                pool_by_currency[line.currency.value].append(line)

        raised: list[ReconciliationException] = []
        for bank in unresolved:
            if bank.source is not Source.BANK or bank.txn_id in claimed:
                continue
            if bank.direction is not Direction.INBOUND:
                continue

            # Counterparty is the filter that makes this tractable. A batch
            # settlement pays one merchant, so its lines share the deposit's
            # counterparty. Without it the pool is every unbanked line in the
            # window — hundreds — and the cap below would discard the very lines
            # that compose this deposit before the search ever sees them.
            # Compared case-insensitively: the bank prints ACME RETAIL, the
            # settlement report says Acme Retail.
            wanted = (bank.counterparty or "").casefold()
            window = [
                line
                for line in pool_by_currency.get(bank.currency.value, [])
                if line.txn_id not in claimed
                and (line.counterparty or "").casefold() == wanted
                and abs((line.value_date - bank.value_date).days)
                <= CANDIDATE_WINDOW_DAYS
                and line.amount_minor <= bank.amount_minor
            ]
            if not window:
                continue

            # Prefer the closest in time, then largest — the lines most likely to
            # belong to this deposit — and cap the search.
            window.sort(
                key=lambda t: (
                    abs((t.value_date - bank.value_date).days),
                    -t.amount_minor,
                )
            )
            pool = window[:MAX_GROUPING_CANDIDATES]

            groupings: list[tuple[CanonicalTransaction, ...]] = []
            for size in range(2, min(MAX_GROUP_SIZE, len(pool)) + 1):
                for combo in combinations(pool, size):
                    if sum(c.amount_minor for c in combo) == bank.amount_minor:
                        groupings.append(combo)
                        if len(groupings) >= 5:
                            break
                if len(groupings) >= 5:
                    break

            if not groupings:
                continue

            # More alternatives means less confidence in any single one. This is
            # a ranking aid for a human, never a threshold for auto-resolving.
            base = 0.75 if len(groupings) == 1 else 0.6
            candidates = [
                CandidateMatch(
                    candidate_ids=[c.txn_id for c in combo],
                    confidence=round(max(0.4, base - 0.05 * i), 2),
                    amount_minor=sum(c.amount_minor for c in combo),
                    reason=(
                        f"{len(combo)} settlement line(s) sum exactly to the "
                        f"deposit; value dates within {CANDIDATE_WINDOW_DAYS}d"
                    ),
                )
                for i, combo in enumerate(groupings)
            ]

            claimed.add(bank.txn_id)
            raised.append(
                ReconciliationException.for_type(
                    ExceptionType.ONE_TO_MANY_UNRESOLVED,
                    subject_ids=[bank.txn_id],
                    amount_at_risk_minor=bank.amount_minor,
                    currency=bank.currency.value,
                    candidates=candidates,
                    reason=(
                        f"{len(groupings)} grouping(s) of settlement lines sum to "
                        f"this deposit and the settlement report carries no UTR, "
                        "so there is no reference to choose between them."
                    ),
                    suggested_resolution=(
                        "Confirm the correct grouping against the payout advice. "
                        "Not auto-resolved: the candidates are indistinguishable "
                        "on the available evidence."
                    ),
                    raised_by="exception.one_to_many",
                )
            )
        return raised

    def _duplicate_payment(
        self,
        unresolved: Sequence[CanonicalTransaction],
        _unbanked: Sequence[CanonicalTransaction],
        transactions: Sequence[CanonicalTransaction],
        claimed: set[str],
    ) -> list[ReconciliationException]:
        """The same money moving twice under different references.

        Ingestion dedupe keys on `(source, external_ref)`, so it is blind to this
        by design: the references genuinely differ. Only the economics give it
        away — same counterparty, same amount, same day, and one of the two
        already reconciled.
        """
        settled: dict[tuple[str, int, str], list[CanonicalTransaction]] = defaultdict(
            list
        )
        unresolved_ids = {t.txn_id for t in unresolved}
        for txn in transactions:
            if txn.source is not Source.BANK or txn.txn_id in unresolved_ids:
                continue
            key = (
                (txn.counterparty or "").casefold(),
                txn.amount_minor,
                txn.currency.value,
            )
            settled[key].append(txn)

        raised: list[ReconciliationException] = []
        for txn in unresolved:
            if txn.source is not Source.BANK or txn.txn_id in claimed:
                continue
            key = (
                (txn.counterparty or "").casefold(),
                txn.amount_minor,
                txn.currency.value,
            )
            twins = [
                other
                for other in settled.get(key, [])
                if abs((other.value_date - txn.value_date).days) <= 1
            ]
            if not twins:
                continue

            twin = twins[0]
            claimed.add(txn.txn_id)
            raised.append(
                ReconciliationException.for_type(
                    ExceptionType.DUPLICATE_SUSPECTED,
                    subject_ids=[txn.txn_id, twin.txn_id],
                    amount_at_risk_minor=txn.amount_minor,
                    currency=txn.currency.value,
                    candidates=[
                        CandidateMatch(
                            candidate_ids=[twin.txn_id],
                            confidence=0.8,
                            amount_minor=twin.amount_minor,
                            reason=(
                                "already reconciled record with identical "
                                "counterparty, amount and value date"
                            ),
                        )
                    ],
                    reason=(
                        "A reconciled payment with the same counterparty, amount "
                        f"and value date exists under reference {twin.external_ref}. "
                        "Different references, so ingestion dedupe could not see "
                        "this — it looks like the same money paid twice."
                    ),
                    suggested_resolution=(
                        "Verify against the payout advice whether this is a "
                        "genuine second payment. If duplicated, raise a recovery."
                    ),
                    raised_by="exception.duplicate",
                )
            )
        return raised

    def _fx_near_miss(
        self,
        unresolved: Sequence[CanonicalTransaction],
        _unbanked: Sequence[CanonicalTransaction],
        _all: Sequence[CanonicalTransaction],
        claimed: set[str],
    ) -> list[ReconciliationException]:
        """A cross-currency record no recorded rate reconciles within tolerance."""
        raised: list[ReconciliationException] = []
        for txn in unresolved:
            if txn.txn_id in claimed or txn.source is not Source.LEDGER:
                continue
            if txn.currency.value == "INR":
                continue
            claimed.add(txn.txn_id)
            raised.append(
                ReconciliationException.for_type(
                    ExceptionType.FX_UNRESOLVED,
                    subject_ids=[txn.txn_id],
                    amount_at_risk_minor=txn.amount_minor,
                    currency=txn.currency.value,
                    reason=(
                        f"{txn.currency.value} entry with no INR credit matching "
                        "at the recorded rate within tolerance."
                    ),
                    suggested_resolution=(
                        "Check the rate actually applied on the settlement date; "
                        "the booked rate may differ from the recorded one."
                    ),
                    raised_by="exception.fx",
                )
            )
        return raised

    def _settled_never_received(
        self, unbanked: Sequence[CanonicalTransaction], claimed: set[str]
    ) -> list[ReconciliationException]:
        """Settled, matched to its order, and the money never arrived.

        These records look reconciled — they appear in a `MatchResult` and never
        reach the unresolved list. Reporting only on unmatched records would hide
        every one of them, which is why the engine checks completeness rather than
        mere participation.
        """
        raised: list[ReconciliationException] = []
        for txn in unbanked:
            if txn.txn_id in claimed:
                continue
            claimed.add(txn.txn_id)
            raised.append(
                ReconciliationException.for_type(
                    ExceptionType.NO_COUNTERPART,
                    subject_ids=[txn.txn_id],
                    amount_at_risk_minor=txn.amount_minor,
                    currency=txn.currency.value,
                    reason=(
                        "Settlement reports this payout as processed, but no bank "
                        "credit corresponds to it. Money left the gateway and has "
                        "not arrived."
                    ),
                    suggested_resolution=(
                        "Chase the payout with the gateway; confirm the UTR was "
                        "issued and whether the credit is in transit or failed."
                    ),
                    raised_by="exception.no_counterpart",
                )
            )
        return raised

    def _no_counterpart(
        self, unresolved: Sequence[CanonicalTransaction], claimed: set[str]
    ) -> list[ReconciliationException]:
        """Nothing on the other side at all — the clearest money-at-risk case."""
        raised: list[ReconciliationException] = []
        for txn in unresolved:
            if txn.txn_id in claimed:
                continue
            claimed.add(txn.txn_id)
            inbound = txn.direction is Direction.INBOUND
            raised.append(
                ReconciliationException.for_type(
                    ExceptionType.NO_COUNTERPART,
                    subject_ids=[txn.txn_id],
                    amount_at_risk_minor=txn.amount_minor,
                    currency=txn.currency.value,
                    reason=(
                        f"{txn.source.value} record with no candidate on any other "
                        "source"
                        + (
                            " — money credited that nothing accounts for."
                            if inbound
                            else " — a payment nothing accounts for."
                        )
                    ),
                    suggested_resolution=(
                        "Identify the counterparty from the source narration and "
                        "confirm whether the corresponding entry was never created."
                    ),
                    raised_by="exception.no_counterpart",
                )
            )
        return raised


def unexplained(
    transactions: Sequence[CanonicalTransaction],
    result: ReconciliationResult,
    report: ExceptionReport,
) -> list[str]:
    """Records that are neither matched nor explained by an exception.

    This must always be empty. It is the last line of the accounting: a record
    that is not reconciled and not on the exception list has been silently lost,
    and a silently lost record is the one failure mode a finance system must never
    have — the books balance over a smaller set than they were given, and nothing
    says so.
    """
    explained = {sid for exc in report.exceptions for sid in exc.subject_ids}
    matched = result.matched_ids
    return [
        t.txn_id
        for t in transactions
        if t.txn_id not in matched and t.txn_id not in explained
    ]


__all__ = [
    "CANDIDATE_WINDOW_DAYS",
    "MAX_GROUPING_CANDIDATES",
    "MAX_GROUP_SIZE",
    "ExceptionEngine",
    "ExceptionReport",
    "unexplained",
]
