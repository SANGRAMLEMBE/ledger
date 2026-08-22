"""
Tiers 0 and 1 — the deterministic half of the cascade.

These two tiers use no model and make no probabilistic judgement. They resolve
what can be resolved by a reference or a stated rule, and they hand everything
else onward untouched. Most of the match rate comes from here, and every match
they emit should be defensible by pointing at a field.

TIER 0 — REFERENCE-LINKED
-------------------------
Cross-source `external_ref` equality is *always false*: each source owns its
reference namespace (`order_…`, `pay_…`, `pout_…`, a bank UTR). Matching on it
would produce a 0% hit rate and push the whole batch into the expensive tiers.
The two links that genuinely exist:

    gateway/settlement -> ledger    child.parent_ref == ledger.external_ref
    settlement -> bank              settlement.group_ref == bank.external_ref

Amount is a **confirmation, not part of the key.** A settlement is net of fees
while the ledger entry is gross, so requiring amount equality here would reject
every fee-bearing match — the exact cases Tier 1 exists to handle.

TIER 1 — RULES
--------------
For pairs with no reference between them at all: a vendor payment recorded in the
GL and debited at the bank shares no identifier, only economics. Tier 1 matches
on (currency, direction, amount, date-within-window, counterparty), using blocking
so it never compares the full cross-product. It also converts foreign-currency
ledger entries at the recorded rate to find their INR bank credit.

THE COLLISION POLICY — BINDING ON BOTH TIERS
--------------------------------------------
When a key identifies more than one candidate, we do **not** tie-break and we do
**not** take the best. The record is left unresolved and passes down the cascade
with its ambiguity intact.

This costs match rate on purpose. `(amount, value_date, counterparty)` is not
unique in a 50k batch — many same-day payouts to the same counterparty share an
amount — so tie-breaking would manufacture confident, wrong pairings. A wrong
auto-match tells the books everything reconciled and nobody looks again; an
unmatched record raises its hand and a human spends thirty seconds. The second
failure is recoverable and the first is not.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from decimal import Decimal

from ledger.domain.models import (
    CanonicalTransaction,
    Currency,
    MatchResult,
    MatchTier,
    Source,
)
from ledger.reconciliation.blocking import BlockingIndex

# Settlement lag we treat as normal rather than exceptional. A payout landing
# T+2 after its ledger entry is ordinary; T+5 is a question worth asking.
SETTLEMENT_WINDOW_DAYS = (0, 1, 2)

# Recorded FX rates. The engine is given the same table the source used, so a
# correct conversion is possible; anything outside tolerance stays unmatched
# rather than being forced.
FX_RATES: dict[tuple[Currency, Currency], Decimal] = {
    (Currency.USD, Currency.INR): Decimal("83.20"),
    (Currency.EUR, Currency.INR): Decimal("90.10"),
    (Currency.GBP, Currency.INR): Decimal("105.40"),
}

# Rounding slack on a converted amount, in minor units. ₹1.00 absorbs the paise
# rounding a real conversion produces without being wide enough to let a
# genuinely different amount through.
FX_TOLERANCE_MINOR = 100


def _normalise_counterparty(name: str | None) -> str:
    """Case- and space-insensitive form for comparison only.

    The bank prints `ACME RETAIL` and the GL holds `Acme Retail`. Both are stored
    verbatim — a connector transcribes, it does not clean — so the normalisation
    happens here, at the point of comparison, where it is visible.
    """
    return " ".join((name or "").split()).casefold()


# --------------------------------------------------------------------------- #
# Tier 0 — reference-linked
# --------------------------------------------------------------------------- #


def tier0_parent_ref(
    transactions: Sequence[CanonicalTransaction],
) -> list[MatchResult]:
    """Link gateway and settlement records up to the ledger entry they belong to.

    The child names its parent explicitly, so this is the strongest evidence in
    the system: no inference, no tolerance, nothing to get wrong.
    """
    ledger_by_ref: dict[tuple[str, str], list[CanonicalTransaction]] = defaultdict(list)
    for txn in transactions:
        if txn.source == Source.LEDGER:
            ledger_by_ref[(txn.external_ref, txn.currency.value)].append(txn)

    matches: list[MatchResult] = []
    for txn in transactions:
        if txn.source not in (Source.GATEWAY, Source.SETTLEMENT):
            continue
        if not txn.parent_ref:
            continue

        parents = ledger_by_ref.get((txn.parent_ref, txn.currency.value), [])
        if len(parents) != 1:
            # Zero: the parent is genuinely absent — a real break, not our
            # problem to invent a fix for. More than one: the reference is
            # ambiguous, and the collision policy forbids choosing.
            continue

        parent = parents[0]
        matches.append(
            MatchResult(
                left_ids=[txn.txn_id],
                right_ids=[parent.txn_id],
                tier=MatchTier.EXACT,
                confidence=1.0,
                reason=(
                    f"exact: {txn.source.value}.parent_ref="
                    f"{txn.parent_ref} == ledger.external_ref"
                ),
            )
        )
    return matches


def tier0_group_ref(
    transactions: Sequence[CanonicalTransaction],
) -> list[MatchResult]:
    """Link settlement lines sideways to the bank credit that carried them.

    This is the only deterministic path to a batch settlement. Several settlement
    lines sharing one UTR against a single bank credit is a one-to-many match, and
    it is resolved here rather than by inferring a grouping — which is exactly the
    subset-sum problem we must not need to solve.

    The summed amount is checked as a *confirmation*. If the parts do not add up
    to the whole, the reference is telling us something the amounts contradict, and
    we refuse rather than trust one over the other.
    """
    bank_by_ref: dict[str, list[CanonicalTransaction]] = defaultdict(list)
    for txn in transactions:
        if txn.source == Source.BANK:
            bank_by_ref[txn.external_ref].append(txn)

    settlements_by_group: dict[str, list[CanonicalTransaction]] = defaultdict(list)
    for txn in transactions:
        if txn.source == Source.SETTLEMENT and txn.group_ref:
            settlements_by_group[txn.group_ref].append(txn)

    matches: list[MatchResult] = []
    for group_ref, lines in settlements_by_group.items():
        banks = bank_by_ref.get(group_ref, [])
        if len(banks) != 1:
            continue

        bank = banks[0]
        if {line.currency for line in lines} != {bank.currency}:
            continue

        total = sum(line.amount_minor for line in lines)
        if total != bank.amount_minor:
            # The reference says these belong together; the arithmetic says they
            # do not. Both cannot be right, so this becomes an exception rather
            # than a match we would have to defend.
            continue

        matches.append(
            MatchResult(
                left_ids=[line.txn_id for line in lines],
                right_ids=[bank.txn_id],
                tier=MatchTier.EXACT,
                confidence=1.0,
                reason=(
                    f"exact: {len(lines)} settlement line(s) share group_ref="
                    f"{group_ref} == bank.external_ref; amounts sum to "
                    f"{total} and agree"
                ),
            )
        )
    return matches


# --------------------------------------------------------------------------- #
# Tier 1 — rules
# --------------------------------------------------------------------------- #


def tier1_economic_pair(
    transactions: Sequence[CanonicalTransaction],
    resolved: Iterable[str],
) -> list[MatchResult]:
    """Pair ledger entries with bank lines that share no reference, only economics.

    A vendor payment or payroll run is recorded in the GL and debited at the bank
    with no identifier in common. What they do share is currency, direction,
    amount, counterparty and a date within the settlement window — and that
    combination, when it identifies exactly one candidate, is strong evidence.

    When it identifies more than one, it is not. Two identical payments to the
    same vendor on the same day are indistinguishable on these fields, and there
    is no honest basis for pairing a particular one. Both are left for a human.
    """
    done = set(resolved)
    ledger_side = [
        t for t in transactions if t.source == Source.LEDGER and t.txn_id not in done
    ]
    bank_side = [
        t for t in transactions if t.source == Source.BANK and t.txn_id not in done
    ]
    index = BlockingIndex(bank_side)

    matches: list[MatchResult] = []
    claimed: set[str] = set()

    for entry in ledger_side:
        wanted = _normalise_counterparty(entry.counterparty)
        candidates = [
            candidate
            for candidate in index.candidates(
                currency=entry.currency,
                direction=entry.direction,
                value_date=entry.value_date,
                amount_minor=entry.amount_minor,
                date_offsets=SETTLEMENT_WINDOW_DAYS,
            )
            if candidate.txn_id not in claimed
            and candidate.amount_minor == entry.amount_minor
            and _normalise_counterparty(candidate.counterparty) == wanted
        ]

        if len(candidates) != 1:
            continue  # zero: no counterpart. more than one: ambiguous. refuse.

        bank = candidates[0]
        lag = (bank.value_date - entry.value_date).days
        claimed.add(bank.txn_id)
        matches.append(
            MatchResult(
                left_ids=[entry.txn_id],
                right_ids=[bank.txn_id],
                tier=MatchTier.RULE,
                confidence=0.95,
                reason=(
                    f"rule: amount={entry.amount_minor} {entry.currency.value}, "
                    f"direction={entry.direction.value}, counterparty match, "
                    f"settlement lag T+{lag}"
                ),
            )
        )
    return matches


def tier1_fx(
    transactions: Sequence[CanonicalTransaction],
    resolved: Iterable[str],
) -> list[MatchResult]:
    """Match a foreign-currency ledger entry to its INR bank credit.

    The conversion uses the recorded rate, and the result must land within a
    ₹1 tolerance — enough to absorb paise rounding, not enough to let a different
    amount through. An amount outside tolerance stays unmatched: a near miss on
    money is a question, not a match.
    """
    done = set(resolved)
    foreign = [
        t
        for t in transactions
        if t.source == Source.LEDGER
        and t.currency != Currency.INR
        and t.txn_id not in done
    ]
    if not foreign:
        return []

    bank_side = [
        t
        for t in transactions
        if t.source == Source.BANK
        and t.currency == Currency.INR
        and t.txn_id not in done
    ]
    index = BlockingIndex(bank_side)

    matches: list[MatchResult] = []
    claimed: set[str] = set()

    for entry in foreign:
        rate = FX_RATES.get((entry.currency, Currency.INR))
        if rate is None:
            continue  # no recorded rate: we will not invent one.

        expected = int(
            (Decimal(entry.amount_minor) * rate).to_integral_value()
        )
        candidates = [
            candidate
            for candidate in index.candidates(
                currency=Currency.INR,
                direction=entry.direction,
                value_date=entry.value_date,
                amount_minor=expected,
                # A converted amount can land either side of a band boundary.
                date_offsets=SETTLEMENT_WINDOW_DAYS,
                band_offsets=(-1, 0, 1),
            )
            if candidate.txn_id not in claimed
            and abs(candidate.amount_minor - expected) <= FX_TOLERANCE_MINOR
        ]

        if len(candidates) != 1:
            continue

        bank = candidates[0]
        claimed.add(bank.txn_id)
        matches.append(
            MatchResult(
                left_ids=[entry.txn_id],
                right_ids=[bank.txn_id],
                tier=MatchTier.RULE,
                confidence=0.92,
                reason=(
                    f"rule: {entry.currency.value}->INR at {rate}, expected "
                    f"{expected} vs actual {bank.amount_minor} "
                    f"(within {FX_TOLERANCE_MINOR} tolerance)"
                ),
            )
        )
    return matches
