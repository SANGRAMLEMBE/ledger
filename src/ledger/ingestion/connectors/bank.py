"""
Bank statement connector.

The hardest of the four, and the one that will meet the ugliest real input. A
bank statement export is not an API response — it is a spreadsheet someone
downloaded, and it shows:

  - column names with spaces and punctuation (``Ref No./Cheque No.``)
  - ``DD/MM/YYYY`` dates (day-first — reading them month-first silently produces
    wrong dates for every day <= 12, which is the worst kind of bug: it looks
    fine in half the rows)
  - amounts as comma-grouped strings in **Indian lakh notation** (``1,23,456.78``)
  - **separate Debit and Credit columns**, exactly one populated, the other an
    empty string — not ``0``, not ``None``
  - no currency column at all (a statement is single-currency; the account's
    currency is connector configuration)
  - no time of day — only a date

That last point is why `posted_at` is midnight UTC: inventing a time would be
recording something the source never said.

WHAT THIS CONNECTOR DELIBERATELY DOES NOT DO
--------------------------------------------
It does not normalise the counterparty's case. The bank says ``ACME RETAIL`` and
that is what we store. Prettifying it here would hide the case/format mismatch
against the ledger's ``Acme Retail`` — and resolving that mismatch is exactly the
fuzzy tier's job. A connector's job is faithful transcription, not cleverness.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
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

# Rows a statement export carries that are not transactions.
_NON_TXN_MARKERS = ("opening balance", "closing balance", "statement summary")


class BankStatementConnector(BaseConnector):
    """Parses bank statement lines into canonical transactions.

    Args:
        currency: the account's currency. A statement has no currency column, so
            this is configuration, not data. Defaulting it silently would be a
            correctness risk on a foreign-currency account, but INR is the right
            default for this deployment.
    """

    source = Source.BANK

    def __init__(self, currency: Currency = Currency.INR) -> None:
        self.currency = currency

    # -- field parsers -------------------------------------------------------

    @staticmethod
    def _parse_amount(value: str) -> Decimal | None:
        """``'1,23,456.78'`` -> ``Decimal('123456.78')``; blank -> None.

        Blank means "this column doesn't apply to this row", which is different
        from zero. Returning 0 here is the classic bug that turns every credit
        into a zero-amount row.
        """
        if value is None:
            return None
        text = str(value).strip().replace(",", "")
        if not text:
            return None
        try:
            return Decimal(text)
        except (InvalidOperation, ValueError) as exc:
            raise ValueError(f"unparseable amount {value!r}") from exc

    @staticmethod
    def _parse_date(value: str) -> date:
        """``'02/08/2026'`` -> ``date(2026, 8, 2)``. Day-first, always."""
        text = str(value).strip()
        try:
            return datetime.strptime(text, "%d/%m/%Y").date()
        except ValueError as exc:
            raise ValueError(
                f"expected DD/MM/YYYY date, got {value!r}"
            ) from exc

    def _to_canonical(
        self, record: dict[str, Any]
    ) -> CanonicalTransaction | None:
        description = str(record.get("Description", "") or "")

        # Skip summary/footer rows rather than failing on them.
        if any(m in description.lower() for m in _NON_TXN_MARKERS):
            return None

        utr = str(record.get("Ref No./Cheque No.", "") or "").strip()
        if not utr:
            raise ConnectorError(
                source=self.source,
                locator=f"value_date={record.get('Value Date')!r}",
                detail="statement line has no reference number; cannot identify it",
            )

        try:
            debit = self._parse_amount(record.get("Debit", ""))
            credit = self._parse_amount(record.get("Credit", ""))
            value_date = self._parse_date(record["Value Date"])
            txn_date = self._parse_date(record.get("Txn Date", record["Value Date"]))
        except (ValueError, KeyError) as exc:
            raise ConnectorError(
                source=self.source, locator=f"ref={utr}", detail=str(exc)
            ) from exc

        # Exactly one of the two columns must carry a value.
        if (debit is None) == (credit is None):
            raise ConnectorError(
                source=self.source,
                locator=f"ref={utr}",
                detail=(
                    "expected exactly one of Debit/Credit to be populated, "
                    f"got debit={debit!r} credit={credit!r}"
                ),
            )

        if credit is not None:
            amount, direction = credit, Direction.INBOUND
        else:
            assert debit is not None  # narrowed by the check above
            amount, direction = debit, Direction.OUTBOUND

        return CanonicalTransaction(
            source=self.source,
            external_ref=utr,
            amount_minor=to_minor(amount, self.currency),
            currency=self.currency,
            direction=direction,
            value_date=value_date,
            # A statement carries no time of day — midnight is the honest reading.
            posted_at=datetime(
                txn_date.year, txn_date.month, txn_date.day, tzinfo=UTC
            ),
            # Money is in the account; from the bank's side it is settled.
            status=TxnStatus.SETTLED,
            counterparty=self._counterparty_from(description),
            raw=dict(record),
        )

    @staticmethod
    def _counterparty_from(description: str) -> str | None:
        """Pull the party out of ``NEFT CR-HDFC0000123-ACME RETAIL-UTR123``.

        Kept deliberately simple and returns None when the shape is unfamiliar.
        A connector that guesses at a narration it doesn't recognise produces
        confident garbage; None is an honest "the bank didn't tell us".
        """
        parts = [p.strip() for p in description.split("-")]
        # [prefix, ifsc, party, (utr)] — party is the third field when present.
        if len(parts) >= 3 and parts[2]:
            return parts[2]
        return None
