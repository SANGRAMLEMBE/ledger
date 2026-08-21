"""
Payment gateway connector.

The easiest of the four, and deliberately so — the four connectors span a
realistic difficulty range so the ingestion layer is proven against more than one
kind of input. A gateway speaks JSON over an API: amounts already arrive as
integer minor units, timestamps as epoch seconds, and the field names are stable.

The one piece of real work is that `direction` is not a field. The gateway
expresses it through `status`: a refund moves money *out*, everything else moves
money *in*. Deriving that here — once, at the boundary — is what stops every
downstream component from having to know gateway-specific semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from ledger.domain.models import (
    CanonicalTransaction,
    Currency,
    Direction,
    Source,
    TxnStatus,
)
from ledger.ingestion.connectors.base import BaseConnector, ConnectorError

# Gateway status -> our normalised lifecycle state.
_STATUS_MAP: dict[str, TxnStatus] = {
    "created": TxnStatus.PENDING,
    "authorized": TxnStatus.PENDING,
    "captured": TxnStatus.CAPTURED,
    "settled": TxnStatus.SETTLED,
    "refunded": TxnStatus.REFUNDED,
    "partially_refunded": TxnStatus.PARTIALLY_REFUNDED,
    "failed": TxnStatus.FAILED,
    "reversed": TxnStatus.REVERSED,
}

# Statuses that represent money leaving the merchant.
_OUTBOUND_STATUSES = {"refunded", "reversed"}


class GatewayConnector(BaseConnector):
    """Parses gateway payment objects into canonical transactions."""

    source = Source.GATEWAY

    def _to_canonical(
        self, record: dict[str, Any]
    ) -> CanonicalTransaction | None:
        payment_id = str(record.get("id", "") or "").strip()
        if not payment_id:
            raise ConnectorError(
                source=self.source,
                locator=f"order_id={record.get('order_id')!r}",
                detail="payment object has no id",
            )

        raw_status = str(record.get("status", "") or "").strip().lower()
        status = _STATUS_MAP.get(raw_status)
        if status is None:
            raise ConnectorError(
                source=self.source,
                locator=f"id={payment_id}",
                detail=(
                    f"unknown gateway status {raw_status!r}. Add it to _STATUS_MAP "
                    "deliberately — silently mapping it would corrupt the ledger."
                ),
            )

        try:
            currency = Currency(str(record["currency"]).upper())
            amount_minor = int(record["amount"])
            posted_at = datetime.fromtimestamp(int(record["created_at"]), tz=UTC)
        except (KeyError, ValueError, TypeError, OSError) as exc:
            raise ConnectorError(
                source=self.source, locator=f"id={payment_id}", detail=str(exc)
            ) from exc

        direction = (
            Direction.OUTBOUND
            if raw_status in _OUTBOUND_STATUSES
            else Direction.INBOUND
        )

        return CanonicalTransaction(
            source=self.source,
            external_ref=payment_id,
            amount_minor=amount_minor,
            currency=currency,
            fee_minor=int(record.get("fee") or 0),
            tax_minor=int(record.get("tax") or 0),
            direction=direction,
            # The gateway records a single instant; the value date is the day it
            # fell on, in UTC.
            value_date=posted_at.date(),
            posted_at=posted_at,
            status=status,
            counterparty=record.get("contact"),
            # The order this payment belongs to — the *upward* link Tier 0 uses
            # to join gateway records to the ledger.
            parent_ref=record.get("order_id"),
            raw=dict(record),
        )
