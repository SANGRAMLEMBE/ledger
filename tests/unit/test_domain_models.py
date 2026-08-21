"""
Unit tests for the canonical domain model.

These tests are the executable specification of the schema. If a proposed schema
change breaks one of these, that's the schema review conversation happening
automatically — which is the point.

Coverage focus:
  - money never loses precision (the single most important invariant)
  - validation rejects bad data AT THE BOUNDARY, loudly
  - invariants that protect downstream matching hold
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timezone
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ledger.domain.models import (
    CanonicalTransaction,
    Currency,
    Direction,
    MatchResult,
    MatchTier,
    Source,
    TxnStatus,
    to_major,
    to_minor,
)


def _valid_txn(**overrides) -> CanonicalTransaction:
    """A minimal valid transaction, with overridable fields, for tests."""
    base = dict(
        source=Source.SETTLEMENT,
        external_ref="pout_ABC123",
        amount_minor=4_000_000,  # ₹40,000.00
        currency=Currency.INR,
        direction=Direction.INBOUND,
        value_date=date(2026, 8, 20),
        posted_at=datetime(2026, 8, 20, 18, 30, tzinfo=UTC),
        status=TxnStatus.SETTLED,
    )
    base.update(overrides)
    return CanonicalTransaction(**base)


# --------------------------------------------------------------------------- #
# Money safety — the invariant we care about most
# --------------------------------------------------------------------------- #


class TestMoney:
    def test_to_minor_exact(self):
        assert to_minor(Decimal("40000.00"), Currency.INR) == 4_000_000
        assert to_minor("40000.00", Currency.INR) == 4_000_000
        assert to_minor(Decimal("0.01"), Currency.INR) == 1

    def test_to_minor_rejects_excess_precision(self):
        # ₹40000.001 has sub-paisa precision — refuse rather than round.
        with pytest.raises(ValueError, match="finer precision"):
            to_minor(Decimal("40000.001"), Currency.INR)

    def test_roundtrip_is_lossless(self):
        for amt in ["0.00", "0.01", "1.99", "40000.00", "999999.99"]:
            minor = to_minor(Decimal(amt), Currency.INR)
            assert to_major(minor, Currency.INR) == Decimal(amt)

    def test_classic_float_trap_does_not_occur(self):
        # 0.1 + 0.2 in float != 0.3. In minor units it's plain integer addition.
        a = to_minor("0.10", Currency.INR)
        b = to_minor("0.20", Currency.INR)
        assert a + b == to_minor("0.30", Currency.INR)


# --------------------------------------------------------------------------- #
# Validation at the boundary
# --------------------------------------------------------------------------- #


class TestValidation:
    def test_valid_transaction_constructs(self):
        txn = _valid_txn()
        assert txn.amount_minor == 4_000_000
        assert txn.txn_id  # auto-assigned UUID

    def test_negative_amount_rejected(self):
        with pytest.raises(ValidationError, match="non-negative"):
            _valid_txn(amount_minor=-100)

    def test_naive_datetime_rejected(self):
        with pytest.raises(ValidationError, match="timezone-aware"):
            _valid_txn(posted_at=datetime(2026, 8, 20, 18, 30))  # no tzinfo

    def test_fee_exceeding_amount_rejected(self):
        # Guards against swapped columns in a connector.
        with pytest.raises(ValidationError, match="exceeds amount"):
            _valid_txn(amount_minor=1000, fee_minor=5000)

    def test_empty_external_ref_rejected(self):
        with pytest.raises(ValidationError):
            _valid_txn(external_ref="")

    def test_unknown_field_rejected(self):
        # extra="forbid": a typo'd field is a bug, not a silent drop.
        with pytest.raises(ValidationError):
            _valid_txn(amont_minor=100)  # typo

    def test_unknown_currency_rejected(self):
        with pytest.raises(ValidationError):
            _valid_txn(currency="XYZ")


# --------------------------------------------------------------------------- #
# Invariants used downstream
# --------------------------------------------------------------------------- #


class TestInvariants:
    def test_immutability(self):
        txn = _valid_txn()
        with pytest.raises(ValidationError):
            txn.amount_minor = 999  # frozen

    def test_dedupe_key(self):
        txn = _valid_txn(source=Source.BANK, external_ref="UTR12345")
        assert txn.dedupe_key == ("bank", "UTR12345")

    def test_net_of_fee_and_tax(self):
        txn = _valid_txn(amount_minor=4_000_000, fee_minor=59_000, tax_minor=10_620)
        assert txn.net_minor == 4_000_000 - 59_000 - 10_620

    def test_posted_at_normalised_to_utc(self):
        from datetime import timedelta

        ist = timezone(timedelta(hours=5, minutes=30))
        txn = _valid_txn(posted_at=datetime(2026, 8, 20, 18, 30, tzinfo=ist))
        # 18:30 IST == 13:00 UTC
        assert txn.posted_at.hour == 13
        assert txn.posted_at.tzinfo == UTC


# --------------------------------------------------------------------------- #
# Match results
# --------------------------------------------------------------------------- #


class TestMatchResult:
    def test_exact_match_requires_high_confidence(self):
        with pytest.raises(ValidationError, match="EXACT tier"):
            MatchResult(
                left_ids=["a"],
                right_ids=["b"],
                tier=MatchTier.EXACT,
                confidence=0.7,
                reason="should have been 1.0",
            )

    def test_one_to_many_detected(self):
        m = MatchResult(
            left_ids=["settlement-1"],
            right_ids=["ledger-1", "ledger-2", "ledger-3"],
            tier=MatchTier.ML,
            confidence=0.88,
            reason="grouped 3 ledger lines summing to settlement",
        )
        assert m.is_one_to_many is True

    def test_confidence_bounds_enforced(self):
        with pytest.raises(ValidationError):
            MatchResult(
                left_ids=["a"], right_ids=["b"], tier=MatchTier.ML,
                confidence=1.5, reason="impossible",
            )
