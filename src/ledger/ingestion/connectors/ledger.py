"""
Internal ledger (GL) connector.

The general ledger is the merchant's own book, and it speaks accounting rather
than payments. The one structural difference from the other three sources: money
direction is expressed as a **debit/credit pair of columns** rather than a sign or
a status. Every row carries both; one is the amount and the other is ``0.00``.

Convention (stated here because it is a genuine choice, and the wrong reading
inverts every cash flow in the system): these exports are taken from the
**cash/receivable perspective**, so a *debit* is money coming to us (inbound) and
a *credit* is money going out (outbound). If a future export arrives from the
revenue perspective the signs flip, so this assumption belongs in one place — a
connector — and nowhere else.

Both columns populated with non-zero values would be a compound journal entry,
which cannot be expressed as one canonical transaction. We reject it loudly
rather than silently picking one side.
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

_STATUS_MAP: dict[str, TxnStatus] = {
    "captured": TxnStatus.CAPTURED,
    "settled": TxnStatus.SETTLED,
    "refunded": TxnStatus.REFUNDED,
    "partially_refunded": TxnStatus.PARTIALLY_REFUNDED,
    "failed": TxnStatus.FAILED,
    "reversed": TxnStatus.REVERSED,
    "pending": TxnStatus.PENDING,
}


class LedgerConnector(BaseConnector):
    """Parses GL export rows into canonical transactions."""

    source = Source.LEDGER

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
        voucher = str(record.get("voucher_no", "") or "").strip()
        if not voucher:
            raise ConnectorError(
                source=self.source,
                locator=f"gl_date={record.get('gl_date')!r}",
                detail="GL row has no voucher_no",
            )

        try:
            currency = Currency(str(record.get("currency", "INR")).upper())
            debit = self._money(record.get("debit"), "debit")
            credit = self._money(record.get("credit"), "credit")
            gl_date = datetime.strptime(
                str(record["gl_date"]).strip(), "%Y-%m-%d"
            ).date()
        except (KeyError, ValueError, TypeError) as exc:
            raise ConnectorError(
                source=self.source, locator=f"voucher={voucher}", detail=str(exc)
            ) from exc

        if debit > 0 and credit > 0:
            raise ConnectorError(
                source=self.source,
                locator=f"voucher={voucher}",
                detail=(
                    "both debit and credit are non-zero — this is a compound "
                    "journal entry and cannot be one canonical transaction"
                ),
            )
        if debit == 0 and credit == 0:
            raise ConnectorError(
                source=self.source,
                locator=f"voucher={voucher}",
                detail="both debit and credit are zero; row carries no amount",
            )

        if debit > 0:
            amount, direction = debit, Direction.INBOUND
        else:
            amount, direction = credit, Direction.OUTBOUND

        raw_status = str(record.get("status", "") or "").strip().lower()
        status = _STATUS_MAP.get(raw_status)
        if status is None:
            raise ConnectorError(
                source=self.source,
                locator=f"voucher={voucher}",
                detail=f"unknown GL status {raw_status!r}",
            )

        return CanonicalTransaction(
            source=self.source,
            external_ref=voucher,
            amount_minor=to_minor(amount, currency),
            currency=currency,
            direction=direction,
            value_date=gl_date,
            # A GL export carries a date, not a time.
            posted_at=datetime(
                gl_date.year, gl_date.month, gl_date.day, tzinfo=UTC
            ),
            status=status,
            counterparty=record.get("party"),
            raw=dict(record),
        )
