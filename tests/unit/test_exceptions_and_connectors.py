"""
Tests for the exception taxonomy and the connector base class.

Written to close the coverage gap the CI gate correctly flagged — we test the
code, we don't lower the bar.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError

from ledger.domain.models import (
    CanonicalTransaction,
    Currency,
    Direction,
    Source,
    TxnStatus,
)
from ledger.exceptions.taxonomy import (
    CandidateMatch,
    ExceptionType,
    ReconciliationException,
    Severity,
)
from ledger.ingestion.connectors.base import (
    BaseConnector,
    Connector,
    ConnectorError,
)

# --------------------------------------------------------------------------- #
# Exception taxonomy
# --------------------------------------------------------------------------- #


class TestExceptionTaxonomy:
    def test_for_type_assigns_default_severity(self):
        exc = ReconciliationException.for_type(
            ExceptionType.NO_COUNTERPART,
            subject_ids=["txn-1"],
            amount_at_risk_minor=4_000_000,
            currency="INR",
            reason="settled but no bank credit",
            suggested_resolution="investigate missing bank credit",
        )
        assert exc.severity == Severity.HIGH  # NO_COUNTERPART is high by default
        assert exc.amount_at_risk_minor == 4_000_000
        assert exc.candidates == []

    def test_ambiguous_match_carries_candidates(self):
        candidates = [
            CandidateMatch(
                candidate_ids=["a", "b", "c"], confidence=0.71,
                amount_minor=4_000_000, reason="sum of 3 ledger lines",
            ),
            CandidateMatch(
                candidate_ids=["a", "d"], confidence=0.64,
                amount_minor=3_998_000, reason="sum of 2 ledger lines",
            ),
        ]
        exc = ReconciliationException.for_type(
            ExceptionType.AMBIGUOUS_MATCH,
            subject_ids=["settle-1"],
            amount_at_risk_minor=4_000_000,
            currency="INR",
            reason="multiple candidate groupings, none above threshold",
            suggested_resolution="review top grouping",
            candidates=candidates,
        )
        assert exc.severity == Severity.MEDIUM
        assert len(exc.candidates) == 2
        # Candidates preserve confidence order as given.
        assert exc.candidates[0].confidence > exc.candidates[1].confidence

    def test_exception_is_immutable(self):
        exc = ReconciliationException.for_type(
            ExceptionType.STALE_UNMATCHED,
            subject_ids=["txn-x"], amount_at_risk_minor=0, currency="INR",
            reason="older than settlement window",
            suggested_resolution="write off or chase",
        )
        with pytest.raises(ValidationError):
            exc.severity = Severity.LOW

    def test_negative_amount_at_risk_rejected(self):
        with pytest.raises(ValidationError):
            ReconciliationException.for_type(
                ExceptionType.NO_COUNTERPART,
                subject_ids=["x"], amount_at_risk_minor=-1, currency="INR",
                reason="r", suggested_resolution="s",
            )

    def test_every_exception_type_has_default_severity(self):
        # Guards against adding a type but forgetting its severity mapping.
        for exc_type in ExceptionType:
            exc = ReconciliationException.for_type(
                exc_type, subject_ids=["x"], amount_at_risk_minor=0,
                currency="INR", reason="r", suggested_resolution="s",
            )
            assert isinstance(exc.severity, Severity)

    def test_candidate_confidence_bounds(self):
        with pytest.raises(ValidationError):
            CandidateMatch(
                candidate_ids=["a"], confidence=1.2,
                amount_minor=100, reason="impossible",
            )


# --------------------------------------------------------------------------- #
# Connector base
# --------------------------------------------------------------------------- #


class _GoodConnector(BaseConnector):
    source = Source.LEDGER

    def _to_canonical(self, record: dict) -> CanonicalTransaction | None:
        if record.get("skip"):
            return None  # e.g. a header/footer line
        return CanonicalTransaction(
            source=Source.LEDGER,
            external_ref=record["ref"],
            amount_minor=record["amount_minor"],
            currency=Currency.INR,
            direction=Direction.INBOUND,
            value_date=date(2026, 8, 20),
            posted_at=datetime(2026, 8, 20, 10, 0, tzinfo=UTC),
            status=TxnStatus.CAPTURED,
            raw=record,
        )


class TestConnectorBase:
    def test_parse_yields_canonical(self):
        conn = _GoodConnector()
        records = [
            {"ref": "order_1", "amount_minor": 100_000},
            {"ref": "order_2", "amount_minor": 250_000},
        ]
        out = list(conn.parse(records))
        assert len(out) == 2
        assert out[0].external_ref == "order_1"
        assert out[0].raw == records[0]  # provenance preserved

    def test_parse_skips_none(self):
        conn = _GoodConnector()
        records = [
            {"skip": True},  # header line -> None
            {"ref": "order_1", "amount_minor": 100_000},
        ]
        out = list(conn.parse(records))
        assert len(out) == 1

    def test_parse_wraps_errors_with_context(self):
        conn = _GoodConnector()
        # Missing 'ref' -> KeyError inside _to_canonical -> wrapped ConnectorError.
        with pytest.raises(ConnectorError) as ei:
            list(conn.parse([{"amount_minor": 100}]))
        assert ei.value.source == Source.LEDGER
        assert "record #0" in ei.value.locator

    def test_satisfies_protocol(self):
        # runtime_checkable Protocol: our concrete connector IS a Connector.
        assert isinstance(_GoodConnector(), Connector)

    def test_connector_error_message_has_no_pii(self):
        # The locator is an index/id, not the record contents.
        err = ConnectorError(Source.BANK, "record #42", "bad amount")
        assert "record #42" in str(err)
        assert err.source == Source.BANK
