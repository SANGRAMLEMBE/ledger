"""
Anomaly detection — the money problems matching cannot see.

SCOPE: DEFENCE ONLY
-------------------
This module detects losses against the merchant. It does not probe, evade, or
exploit anything, and nothing here should ever become offence-capable. That is an
explicit disqualification criterion on this track and it is also simply the right
line.

WHY THIS IS SEPARATE FROM MATCHING
----------------------------------
The cascade asks "which record on the other side is this one?". That question has
no answer for the most expensive failure in a real close: **the same payment made
twice**. Both payments are real, both have valid references, and each one matches
its own paperwork perfectly. Nothing in the matching cascade is wrong, and yet the
merchant has paid twice.

Ingestion dedupe cannot see it either, by construction. Dedupe keys on
`(source, external_ref)` and the whole point of a genuine double payment is that
the references *differ* — a re-issued payout gets a new UTR. Widening the dedupe
key to catch it would start discarding legitimate distinct records, which is a
worse failure: silently dropping real money movements.

So it needs a third question, asked of the economics rather than the identifiers:
*did this exact amount go to this exact counterparty twice in the same window?*

PRECISION MATTERS MORE THAN RECALL HERE
---------------------------------------
Every false positive costs a human's attention, and a detector that cries wolf
gets muted — at which point it catches nothing and is worse than not existing.
So the detector is deliberately conservative, and `AnomalyReport` carries the
false-positive count so the number is reported rather than assumed. Missing a
marginal case is an acceptable trade for a queue people still read.

WHAT IS NOT BUILT, AND WHY
--------------------------
Split-transaction and amount-outlier detection are described in the plan and are
**not** implemented, because the synthetic generator does not plant either case.
Writing a detector with no labelled examples would produce code that cannot be
shown to work — the tests would assert against cases we invented to match the
implementation, which proves nothing. Both become worthwhile the moment the
generator grows those cases; until then they would be decoration.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import Enum

from ledger.domain.models import CanonicalTransaction, Source
from ledger.reconciliation.engine import ReconciliationResult

# How far apart two payments may sit and still plausibly be the same one issued
# twice. A re-issue lands within a day or so; beyond that it is more likely a
# genuine recurring payment of the same amount, which must not be flagged.
DUPLICATE_WINDOW_DAYS = 1


class AnomalyKind(str, Enum):
    DUPLICATE_PAYMENT = "duplicate_payment"
    # The same economic payment settled twice under different references.


@dataclass(frozen=True)
class Anomaly:
    """One detected loss, with the evidence that justifies raising it."""

    kind: AnomalyKind
    subjects: list[str]
    amount_at_risk_minor: int
    currency: str
    confidence: float
    reason: str
    counterpart_ref: str = ""

    @property
    def primary(self) -> str:
        return self.subjects[0]


@dataclass
class AnomalyReport:
    anomalies: list[Anomaly] = field(default_factory=list)

    @property
    def count(self) -> int:
        return len(self.anomalies)

    @property
    def total_at_risk_minor(self) -> int:
        return sum(a.amount_at_risk_minor for a in self.anomalies)

    def by_kind(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for anomaly in self.anomalies:
            counts[anomaly.kind.value] = counts.get(anomaly.kind.value, 0) + 1
        return counts


class AnomalyDetector:
    """Finds economic duplicates that identifiers cannot reveal."""

    def __init__(self, window_days: int = DUPLICATE_WINDOW_DAYS) -> None:
        self.window_days = window_days

    def detect(
        self,
        transactions: Sequence[CanonicalTransaction],
        result: ReconciliationResult,
    ) -> AnomalyReport:
        """Flag unreconciled records that duplicate an already-reconciled one.

        The asymmetry is deliberate and load-bearing. We compare *unresolved*
        records against *reconciled* ones rather than comparing everything with
        everything, for two reasons:

        1. A reconciled record has independent corroboration — a settlement line
           and a ledger entry agree it happened. That makes it the credible half
           of the pair, and the unmatched twin the suspicious one.
        2. Comparing all records against all records would flag both halves of
           every legitimate recurring payment. Payroll is the same amount to the
           same counterparty every month; two consecutive rent payments look
           identical to any economics-only test. Anchoring on "one side is
           reconciled and the other is orphaned" is what separates a duplicate
           from a routine.
        """
        unresolved_ids = {t.txn_id for t in result.unresolved}

        # Index the reconciled side by its economics. This is the blocking key
        # again: an exact-match dict lookup rather than a scan, so the pass stays
        # linear rather than quadratic.
        reconciled: dict[
            tuple[str, int, str], list[CanonicalTransaction]
        ] = defaultdict(list)
        for txn in transactions:
            if txn.source is not Source.BANK or txn.txn_id in unresolved_ids:
                continue
            reconciled[self._economic_key(txn)].append(txn)

        report = AnomalyReport()
        for txn in result.unresolved:
            if txn.source is not Source.BANK:
                continue

            twins = [
                other
                for other in reconciled.get(self._economic_key(txn), [])
                if abs((other.value_date - txn.value_date).days) <= self.window_days
                and other.external_ref != txn.external_ref
            ]
            if not twins:
                continue

            # Nearest in time is the most plausible re-issue.
            twin = min(
                twins, key=lambda o: abs((o.value_date - txn.value_date).days)
            )
            same_day = twin.value_date == txn.value_date

            report.anomalies.append(
                Anomaly(
                    kind=AnomalyKind.DUPLICATE_PAYMENT,
                    subjects=[txn.txn_id, twin.txn_id],
                    amount_at_risk_minor=txn.amount_minor,
                    currency=txn.currency.value,
                    # Same-day is stronger evidence than next-day: a genuine
                    # re-issue usually happens immediately, while a day's gap
                    # leaves more room for a legitimate second payment.
                    confidence=0.8 if same_day else 0.65,
                    reason=(
                        "A reconciled payment with the same counterparty, amount "
                        f"and value date exists under reference {twin.external_ref}. "
                        "The references differ, so ingestion dedupe could not see "
                        "this — it looks like the same money paid twice."
                    ),
                    counterpart_ref=twin.external_ref,
                )
            )
        return report

    @staticmethod
    def _economic_key(txn: CanonicalTransaction) -> tuple[str, int, str]:
        """What makes two records the same *payment* rather than the same record.

        Counterparty is casefolded because the bank prints ACME RETAIL while the
        ledger holds Acme Retail, and a case difference must not hide a duplicate.
        """
        return (
            (txn.counterparty or "").casefold(),
            txn.amount_minor,
            txn.currency.value,
        )
