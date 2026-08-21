"""
Connector tests.

The centrepiece is `TestRoundTrip`: every record in a generated batch is fed back
through its connector as raw source data, and the result must equal the canonical
record the generator started from. That is the acceptance criterion in
`CONTRACTS.md` §2, and it is what keeps ingestion **on the measured path** — if a
connector misreads a date format or drops a field, the match rate would quietly
degrade and we'd blame the matcher. This catches it at the boundary instead.

Beyond the round trip, each connector gets tests for the specific trap its source
sets: day-first dates and blank Debit/Credit columns for the bank, IST offsets for
settlement, debit/credit collapse for the GL, status-derived direction for the
gateway.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ledger.domain.models import (
    Currency,
    Direction,
    Source,
    TxnStatus,
)
from ledger.ingestion.connectors.bank import BankStatementConnector
from ledger.ingestion.connectors.base import ConnectorError
from ledger.ingestion.connectors.gateway import GatewayConnector
from ledger.ingestion.connectors.ledger import LedgerConnector
from ledger.ingestion.connectors.settlement import SettlementConnector
from ledger.synthetic.generator import SyntheticGenerator

CONNECTORS = {
    Source.BANK: BankStatementConnector(),
    Source.GATEWAY: GatewayConnector(),
    Source.SETTLEMENT: SettlementConnector(),
    Source.LEDGER: LedgerConnector(),
}

# Fields the connector cannot know: they are assigned by Ledger at ingestion.
_GENERATED = {"txn_id", "ingested_at"}


def _comparable(txn) -> dict:
    return {
        k: v for k, v in txn.model_dump().items() if k not in _GENERATED
    }


class TestRoundTrip:
    """connector.parse([txn.raw]) == txn — over a whole batch, not a sample."""

    def setup_method(self):
        self.batch = SyntheticGenerator(seed=1337).generate(400)

    def test_every_record_round_trips(self):
        mismatches = []
        for txn in self.batch.transactions:
            connector = CONNECTORS[txn.source]
            (parsed,) = list(connector.parse([txn.raw]))
            if _comparable(parsed) != _comparable(txn):
                mismatches.append((txn.source.value, txn.external_ref))
        assert not mismatches, (
            f"{len(mismatches)} records failed to round-trip; "
            f"first few: {mismatches[:5]}"
        )

    def test_all_four_sources_were_actually_exercised(self):
        """Guards against a vacuous pass if a source stops being generated."""
        seen = {t.source for t in self.batch.transactions}
        assert seen == set(CONNECTORS)

    def test_round_trip_preserves_the_deterministic_links(self):
        """parent_ref and group_ref survive ingestion, or Tier 0 has no keys."""
        checked_parent = checked_group = 0
        for txn in self.batch.transactions:
            (parsed,) = list(CONNECTORS[txn.source].parse([txn.raw]))
            assert parsed.parent_ref == txn.parent_ref
            assert parsed.group_ref == txn.group_ref
            checked_parent += txn.parent_ref is not None
            checked_group += txn.group_ref is not None
        assert checked_parent > 0 and checked_group > 0


class TestBankConnector:
    def setup_method(self):
        self.c = BankStatementConnector()

    def _row(self, **over):
        row = {
            "Txn Date": "02/08/2026",
            "Value Date": "02/08/2026",
            "Description": "NEFT CR-HDFC0000123-ACME RETAIL-UTR9911",
            "Ref No./Cheque No.": "UTR9911",
            "Debit": "",
            "Credit": "1,23,456.78",
            "Balance": "5,00,000.00",
        }
        row.update(over)
        return row

    def test_indian_grouping_is_parsed(self):
        (txn,) = list(self.c.parse([self._row()]))
        assert txn.amount_minor == 12_345_678
        assert txn.direction == Direction.INBOUND

    def test_dates_are_day_first_not_month_first(self):
        """02/08 is 2 August. Reading it as 8 February is silent and wrong."""
        (txn,) = list(self.c.parse([self._row()]))
        assert txn.value_date.day == 2
        assert txn.value_date.month == 8

    def test_debit_row_is_outbound(self):
        (txn,) = list(self.c.parse([self._row(Debit="500.00", Credit="")]))
        assert txn.direction == Direction.OUTBOUND
        assert txn.amount_minor == 50_000

    def test_blank_column_is_not_zero(self):
        """The trap: treating '' as 0 makes both columns 'populated'."""
        with pytest.raises(ConnectorError, match="exactly one of Debit/Credit"):
            list(self.c.parse([self._row(Debit="100.00", Credit="200.00")]))

    def test_row_with_neither_column_is_rejected(self):
        with pytest.raises(ConnectorError, match="exactly one of Debit/Credit"):
            list(self.c.parse([self._row(Debit="", Credit="")]))

    def test_missing_reference_is_rejected(self):
        with pytest.raises(ConnectorError, match="no reference number"):
            list(self.c.parse([self._row(**{"Ref No./Cheque No.": ""})]))

    def test_summary_rows_are_skipped_not_failed(self):
        rows = [
            self._row(Description="Opening Balance", **{"Ref No./Cheque No.": ""}),
            self._row(),
        ]
        assert len(list(self.c.parse(rows))) == 1

    def test_bad_date_format_is_rejected_with_context(self):
        with pytest.raises(ConnectorError, match="DD/MM/YYYY"):
            list(self.c.parse([self._row(**{"Value Date": "2026-08-02"})]))

    def test_counterparty_case_is_not_prettified(self):
        (txn,) = list(self.c.parse([self._row()]))
        assert txn.counterparty == "ACME RETAIL"

    def test_unfamiliar_narration_yields_no_counterparty(self):
        (txn,) = list(self.c.parse([self._row(Description="MISC CREDIT")]))
        assert txn.counterparty is None

    def test_statement_has_no_time_of_day(self):
        (txn,) = list(self.c.parse([self._row()]))
        assert (txn.posted_at.hour, txn.posted_at.minute) == (0, 0)


class TestSettlementConnector:
    def setup_method(self):
        self.c = SettlementConnector()

    def _row(self, **over):
        row = {
            "settlement_id": "setl_a1",
            "payout_id": "pout_a1",
            "order_id": "order_a1",
            "amount": "39200.00",
            "fee": "800.00",
            "tax": "0.00",
            "currency": "INR",
            "utr": "UTR7788",
            "settled_at": "2026-08-02T11:30:00+05:30",
            "counterparty": "Acme Retail",
            "status": "processed",
        }
        row.update(over)
        return row

    def test_ist_is_normalised_to_utc(self):
        """11:30 IST is 06:00 UTC. Reading it as UTC shifts the payout 5.5h."""
        (txn,) = list(self.c.parse([self._row()]))
        assert txn.posted_at == datetime(2026, 8, 2, 6, 0, tzinfo=UTC)

    def test_late_evening_ist_rolls_the_utc_date_back(self):
        """The case that breaks the T+1 window if the offset is ignored."""
        (txn,) = list(self.c.parse([self._row(settled_at="2026-08-03T02:00:00+05:30")]))
        assert txn.value_date.day == 2  # 02:00 IST on the 3rd is 20:30 UTC on the 2nd

    def test_amount_is_net_with_fee_recorded_separately(self):
        (txn,) = list(self.c.parse([self._row()]))
        assert txn.amount_minor == 3_920_000
        assert txn.fee_minor == 80_000

    def test_utr_becomes_the_group_ref(self):
        (txn,) = list(self.c.parse([self._row()]))
        assert txn.group_ref == "UTR7788"

    def test_missing_utr_is_recorded_as_none_not_invented(self):
        (txn,) = list(self.c.parse([self._row(utr=None)]))
        assert txn.group_ref is None

    def test_missing_payout_id_is_rejected(self):
        with pytest.raises(ConnectorError, match="no payout_id"):
            list(self.c.parse([self._row(payout_id="")]))

    def test_unparseable_amount_is_rejected(self):
        with pytest.raises(ConnectorError, match="unparseable amount"):
            list(self.c.parse([self._row(amount="thirty nine thousand")]))


class TestGatewayConnector:
    def setup_method(self):
        self.c = GatewayConnector()

    def _row(self, **over):
        row = {
            "id": "pay_a1",
            "entity": "payment",
            "amount": 4_000_000,
            "currency": "INR",
            "status": "captured",
            "order_id": "order_a1",
            "method": "upi",
            "captured": True,
            "fee": 80_000,
            "tax": 14_400,
            "created_at": 1_785_000_000,
            "contact": "Acme Retail",
        }
        row.update(over)
        return row

    def test_minor_units_pass_through_untouched(self):
        (txn,) = list(self.c.parse([self._row()]))
        assert txn.amount_minor == 4_000_000

    def test_refund_is_outbound(self):
        (txn,) = list(self.c.parse([self._row(status="refunded")]))
        assert txn.direction == Direction.OUTBOUND
        assert txn.status == TxnStatus.REFUNDED

    def test_capture_is_inbound(self):
        (txn,) = list(self.c.parse([self._row()]))
        assert txn.direction == Direction.INBOUND

    def test_order_id_becomes_parent_ref(self):
        (txn,) = list(self.c.parse([self._row()]))
        assert txn.parent_ref == "order_a1"

    def test_unknown_status_is_rejected_loudly(self):
        """Silently mapping an unknown status would corrupt the ledger."""
        with pytest.raises(ConnectorError, match="unknown gateway status"):
            list(self.c.parse([self._row(status="disputed")]))

    def test_unknown_currency_is_rejected(self):
        with pytest.raises(ConnectorError):
            list(self.c.parse([self._row(currency="XYZ")]))

    def test_missing_id_is_rejected(self):
        with pytest.raises(ConnectorError, match="no id"):
            list(self.c.parse([self._row(id="")]))


class TestLedgerConnector:
    def setup_method(self):
        self.c = LedgerConnector()

    def _row(self, **over):
        row = {
            "voucher_no": "order_a1",
            "gl_date": "2026-08-01",
            "account": "1200 - Accounts Receivable",
            "party": "Acme Retail",
            "debit": "40000.00",
            "credit": "0.00",
            "narration": "Sales invoice",
            "currency": "INR",
            "status": "captured",
        }
        row.update(over)
        return row

    def test_debit_is_inbound(self):
        (txn,) = list(self.c.parse([self._row()]))
        assert txn.direction == Direction.INBOUND
        assert txn.amount_minor == 4_000_000

    def test_credit_is_outbound(self):
        (txn,) = list(self.c.parse([self._row(debit="0.00", credit="1500.00")]))
        assert txn.direction == Direction.OUTBOUND
        assert txn.amount_minor == 150_000

    def test_compound_entry_is_rejected(self):
        with pytest.raises(ConnectorError, match="compound"):
            list(self.c.parse([self._row(debit="100.00", credit="50.00")]))

    def test_zero_amount_row_is_rejected(self):
        with pytest.raises(ConnectorError, match="carries no amount"):
            list(self.c.parse([self._row(debit="0.00", credit="0.00")]))

    def test_missing_voucher_is_rejected(self):
        with pytest.raises(ConnectorError, match="no voucher_no"):
            list(self.c.parse([self._row(voucher_no="")]))

    def test_unknown_status_is_rejected(self):
        with pytest.raises(ConnectorError, match="unknown GL status"):
            list(self.c.parse([self._row(status="posted-ish")]))

    def test_foreign_currency_is_carried_through(self):
        (txn,) = list(self.c.parse([self._row(currency="USD", debit="100.00")]))
        assert txn.currency == Currency.USD


class TestErrorsCarryNoPII:
    """Lead 09's logging policy: locators identify, they never leak content."""

    def test_bank_error_locator_has_no_narration(self):
        c = BankStatementConnector()
        row = {
            "Txn Date": "02/08/2026",
            "Value Date": "02/08/2026",
            "Description": "NEFT CR-HDFC0000123-SENSITIVE PERSON NAME-UTR1",
            "Ref No./Cheque No.": "UTR1",
            "Debit": "5.00",
            "Credit": "5.00",
            "Balance": "0.00",
        }
        with pytest.raises(ConnectorError) as exc:
            list(c.parse([row]))
        assert "SENSITIVE PERSON NAME" not in str(exc.value)
