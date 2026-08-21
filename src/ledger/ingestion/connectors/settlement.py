"""
Settlement report connector.

A settlement report is the payout side of the gateway: which captured payments
were batched, what was deducted, and which bank credit the money landed in. Two
things make it interesting.

**Timestamps carry an IST offset.** ``2026-08-02T11:30:00+05:30`` is local time.
Treating it as UTC shifts every settlement 5.5 hours — enough to move a late
evening payout onto the following day, which then blows the T+1 window in the
rule tier and turns a clean match into an exception. This connector normalises to
UTC at the boundary so nothing downstream ever has to think about it.

**The UTR is the sideways link.** `utr` becomes `group_ref` — the join from a
settlement line to the bank credit that carried it (`settlement.group_ref ==
bank.external_ref`). It is the only deterministic path to a batch settlement, and
it is legitimately absent when a payout hasn't been credited yet. We record that
absence as `None` rather than inventing a value: a missing UTR is what makes a
batch settlement genuinely ambiguous, and the engine is required to raise an
exception there rather than guess.

Note the amount stored is the **net** figure the report gives, with `fee` and
`tax` recorded alongside. The gross is `net + fee + tax`, reconstructed by the
rule tier. Storing a computed gross here would be inventing a number the source
never printed.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ledger.domain.models import (
    CanonicalTransaction,
    Currency,
    Direction,
    Source,
    TxnStatus,
    to_minor,
)
from ledger.ingestion.connectors.base import BaseConnector, ConnectorError


class SettlementConnector(BaseConnector):
    """Parses settlement report rows into canonical transactions."""

    source = Source.SETTLEMENT

    @staticmethod
    def _money(value: Any, field: str) -> Decimal:
        text = str(value if value is not None else "0").strip().replace(",", "")
        if not text:
            return Decimal("0")
        try:
            return Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"unparseable {field}: {value!r}") from exc

    def _to_canonical(
        self, record: dict[str, Any]
    ) -> CanonicalTransaction | None:
        payout_id = str(record.get("payout_id", "") or "").strip()
        if not payout_id:
            raise ConnectorError(
                source=self.source,
                locator=f"settlement_id={record.get('settlement_id')!r}",
                detail="settlement row has no payout_id",
            )

        try:
            currency = Currency(str(record["currency"]).upper())
            net = self._money(record.get("amount"), "amount")
            fee = self._money(record.get("fee"), "fee")
            tax = self._money(record.get("tax"), "tax")
            # fromisoformat keeps the +05:30 offset; astimezone normalises it.
            settled_at = datetime.fromisoformat(
                str(record["settled_at"])
            ).astimezone(UTC)
        except (KeyError, ValueError, TypeError) as exc:
            raise ConnectorError(
                source=self.source, locator=f"payout_id={payout_id}", detail=str(exc)
            ) from exc

        if settled_at.tzinfo is None:  # pragma: no cover - defensive
            raise ConnectorError(
                source=self.source,
                locator=f"payout_id={payout_id}",
                detail="settled_at has no timezone; refusing to assume one",
            )

        utr = record.get("utr")
        return CanonicalTransaction(
            source=self.source,
            external_ref=payout_id,
            amount_minor=to_minor(net, currency),
            currency=currency,
            fee_minor=to_minor(fee, currency),
            tax_minor=to_minor(tax, currency),
            direction=Direction.INBOUND,
            value_date=settled_at.date(),
            posted_at=settled_at,
            status=TxnStatus.SETTLED,
            counterparty=record.get("counterparty"),
            # Upward link: the order this payout settles.
            parent_ref=record.get("order_id"),
            # Sideways link: the bank credit that carried it. None is meaningful.
            group_ref=str(utr) if utr else None,
            raw=dict(record),
        )
