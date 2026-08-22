"""
The reconciliation orchestrator.

Runs the cascade in order — cheapest and most certain first — carrying the
unresolved residue forward. Each tier sees only what the tiers above it could not
resolve, which is what keeps the expensive stages small.

THE INVARIANT THIS MODULE EXISTS TO ENFORCE
-------------------------------------------
Every input record ends up either **matched** (appearing in at least one
`MatchResult`) or **unresolved** (handed to the exception engine). Never both,
never neither. `ReconciliationResult.check()` raises if that is violated.

Note the deliberate wording: a record may appear in *several* matches, and that
is correct rather than a bug. A ledger entry is legitimately linked to its
gateway capture and to its settlement line; those are two separate true facts
about the same record. What must never happen is a record quietly belonging to
nothing — because the batch then reconciles beautifully over a smaller set than
it was given, and the shortfall is invisible.

WHAT THIS MODULE DOES NOT DO
----------------------------
It does not decide anything. It sequences tiers and accounts for records. The
matching logic lives in the tiers, and turning the residue into typed exceptions
belongs to the exception engine. Keeping the orchestrator free of judgement is
what makes the cascade order easy to change and easy to reason about.
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass, field

from ledger.domain.models import CanonicalTransaction, MatchResult, MatchTier
from ledger.reconciliation.deterministic import (
    tier0_group_ref,
    tier0_parent_ref,
    tier1_economic_pair,
    tier1_fx,
)


@dataclass
class ReconciliationResult:
    """Everything the cascade produced, plus the accounting to prove it."""

    matches: list[MatchResult] = field(default_factory=list)
    unresolved: list[CanonicalTransaction] = field(default_factory=list)
    total_in: int = 0
    elapsed_seconds: float = 0.0
    per_tier: dict[str, int] = field(default_factory=dict)

    @property
    def matched_count(self) -> int:
        """Distinct records appearing in at least one match."""
        return len(self.matched_ids)

    @property
    def matched_ids(self) -> set[str]:
        ids: set[str] = set()
        for match in self.matches:
            ids.update(match.left_ids)
            ids.update(match.right_ids)
        return ids

    @property
    def match_rate(self) -> float:
        """Fraction of input records that ended up in a match.

        Deliberately NOT called accuracy. This says how much was reconciled, not
        how much was reconciled *correctly* — only the ground truth can say that,
        and the two numbers must never be conflated.
        """
        return self.matched_count / self.total_in if self.total_in else 0.0

    @property
    def throughput(self) -> float:
        return self.total_in / self.elapsed_seconds if self.elapsed_seconds else 0.0

    def check(self) -> None:
        """Every record matched or unresolved, exactly one of the two."""
        matched = self.matched_ids
        unresolved = {t.txn_id for t in self.unresolved}

        overlap = matched & unresolved
        if overlap:
            raise RuntimeError(
                f"{len(overlap)} record(s) are both matched and unresolved; "
                "the cascade is double-counting."
            )

        counted = len(matched) + len(unresolved)
        if counted != self.total_in:
            raise RuntimeError(
                f"reconciliation lost records: {self.total_in} in, but "
                f"{len(matched)} matched + {len(unresolved)} unresolved = "
                f"{counted}. Refusing to continue."
            )

    def summary(self) -> str:
        return (
            f"{self.matched_count:,} of {self.total_in:,} records matched "
            f"({self.match_rate:.1%}), {len(self.unresolved):,} unresolved, "
            f"in {self.elapsed_seconds:.2f}s ({self.throughput:,.0f} rec/s)"
        )


class ReconciliationEngine:
    """Runs the deterministic cascade over a batch of canonical transactions."""

    def reconcile(
        self, transactions: Sequence[CanonicalTransaction]
    ) -> ReconciliationResult:
        started = time.perf_counter()
        result = ReconciliationResult(total_in=len(transactions))

        def resolved_ids() -> set[str]:
            ids: set[str] = set()
            for match in result.matches:
                ids.update(match.left_ids)
                ids.update(match.right_ids)
            return ids

        # Tier 0 — reference-linked. Both passes run against the full batch: they
        # key on explicit references, so there is nothing for a prior pass to
        # take away from them, and a record legitimately participates in both
        # (a settlement links up to its order and sideways to its bank credit).
        for name, tier_fn in (
            ("tier0_parent_ref", tier0_parent_ref),
            ("tier0_group_ref", tier0_group_ref),
        ):
            found = tier_fn(transactions)
            result.matches.extend(found)
            result.per_tier[name] = len(found)

        # Tier 1 — rules. These search for counterparts, so they must see only
        # what is still unclaimed, or they would re-pair records already matched
        # on stronger evidence.
        for name, rule_fn in (
            ("tier1_economic_pair", tier1_economic_pair),
            ("tier1_fx", tier1_fx),
        ):
            found = rule_fn(transactions, resolved_ids())
            result.matches.extend(found)
            result.per_tier[name] = len(found)

        matched = resolved_ids()
        result.unresolved = [t for t in transactions if t.txn_id not in matched]
        result.elapsed_seconds = time.perf_counter() - started

        result.check()
        return result


def tier_breakdown(result: ReconciliationResult) -> dict[str, int]:
    """Distinct records resolved by each tier, in cascade order.

    Reported per tier rather than as one total because the shape matters: a
    healthy cascade clears most of the batch on the cheap, certain tiers and
    passes only a small residue down. If the deterministic tiers are resolving
    little, the reference links are broken and no amount of work further down
    will fix it.
    """
    seen: set[str] = set()
    counts: dict[str, int] = {}
    for tier in (MatchTier.EXACT, MatchTier.RULE, MatchTier.FUZZY, MatchTier.ML):
        ids: set[str] = set()
        for match in result.matches:
            if match.tier is tier:
                ids.update(match.left_ids)
                ids.update(match.right_ids)
        counts[tier.value] = len(ids - seen)
        seen |= ids
    return counts
