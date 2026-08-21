"""
Source-shaped raw payloads for the synthetic generator.

WHY THIS MODULE EXISTS
----------------------
Day 1's generator built `CanonicalTransaction` objects directly, leaving `raw`
empty. That made the connector layer untestable against the real batch: the four
connectors would have had to be built against hand-invented fixtures, so the
"50k records end-to-end" claim would have quietly skipped ingestion entirely.

This module closes that hole. For every canonical record the generator plants, it
also emits the raw payload that source *would* have produced. That buys us three
things:

  1. Connectors are exercised by the same 50k batch the metrics are computed on —
     ingestion is genuinely on the measured path, not bypassed.
  2. A round-trip property test becomes possible and is the strongest correctness
     check we have on a connector:

         connector.parse([txn.raw]) == txn        (modulo generated ids)

     If a connector drops a field, misreads a date format, or mangles an amount,
     that test fails immediately rather than silently degrading the match rate.
  3. New contributors can see the actual shape of each source without access to
     production data.

DESIGN RULE: THE SHAPES ARE DELIBERATELY MESSY
----------------------------------------------
Each shape mirrors how that source really behaves, including the parts that are
annoying:

  - gateway     — JSON, amounts already in paise (int), epoch timestamps. Easy.
  - settlement  — report row, amounts as major-unit *strings*, ISO timestamps
                  carrying an **IST offset** the connector must normalise to UTC.
  - bank        — statement line: keys with spaces, ``DD/MM/YYYY`` dates,
                  comma-grouped amount strings in **Indian lakh notation**, and
                  separate Debit/Credit columns where one is blank.
  - ledger      — GL export: debit/credit pair the connector must collapse into a
                  `direction`, dates as ``YYYY-MM-DD``.

Making these clean would make the connectors look good and prove nothing. A
connector that cannot parse ``"1,23,456.78"`` is a connector that will fail on the
first real bank statement.

INVARIANT: every raw shape must carry enough information to reconstruct the
canonical record exactly. If you add a canonical field, add its source of truth
here too, or the round-trip test will catch you.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ledger.domain.models import Currency, to_major

# Indian Standard Time. Settlement reports are written in local time; the
# connector is responsible for normalising to UTC. This is a real and frequent
# source of off-by-5.5-hours matching bugs, so we make the generator produce it.
IST = timezone(timedelta(hours=5, minutes=30))


def _rupees(amount_minor: int, currency: Currency) -> str:
    """Plain major-unit string, e.g. 4000000 -> '40000.00'. Settlement style."""
    return f"{to_major(amount_minor, currency):.2f}"


def indian_grouped(amount_minor: int, currency: Currency) -> str:
    """Format in Indian lakh/crore grouping, e.g. 12345678 -> '1,23,456.78'.

    Indian statements do NOT group in thousands: after the last three digits the
    grouping switches to pairs. A connector using a naive
    ``value.replace(",", "")`` survives this; one using locale-aware thousands
    parsing does not. Either way it must be exercised.
    """
    major = to_major(amount_minor, currency)
    whole, _, frac = f"{major:.2f}".partition(".")
    if len(whole) <= 3:
        grouped = whole
    else:
        head, tail = whole[:-3], whole[-3:]
        parts: list[str] = []
        while len(head) > 2:
            parts.insert(0, head[-2:])
            head = head[:-2]
        if head:
            parts.insert(0, head)
        grouped = ",".join([*parts, tail])
    return f"{grouped}.{frac}"


# --------------------------------------------------------------------------- #
# Gateway — Razorpay-style payment object
# --------------------------------------------------------------------------- #


def gateway_raw(
    *,
    payment_id: str,
    order_id: str | None,
    amount_minor: int,
    currency: Currency,
    fee_minor: int,
    tax_minor: int,
    status: str,
    created_at: datetime,
    contact: str | None,
    method: str = "upi",
) -> dict[str, Any]:
    """A payment object as the gateway API returns it.

    Amounts are already integer minor units and timestamps are epoch seconds, so
    this is the *easy* connector — deliberately, so the four connectors span a
    realistic difficulty range.
    """
    return {
        "id": payment_id,
        "entity": "payment",
        "amount": amount_minor,
        "currency": currency.value,
        "status": status,
        "order_id": order_id,
        "method": method,
        "captured": status in {"captured", "settled"},
        "fee": fee_minor,
        "tax": tax_minor,
        "created_at": int(created_at.timestamp()),
        "contact": contact,
    }


# --------------------------------------------------------------------------- #
# Settlement — payout report row
# --------------------------------------------------------------------------- #


def settlement_raw(
    *,
    payout_id: str,
    settlement_id: str,
    order_id: str | None,
    net_minor: int,
    fee_minor: int,
    tax_minor: int,
    currency: Currency,
    utr: str | None,
    settled_at: datetime,
    counterparty: str | None,
    status: str = "processed",
) -> dict[str, Any]:
    """A row from the settlement report.

    Amounts are major-unit strings and `settled_at` carries an IST offset — both
    must be normalised by the connector. `utr` may be None when the payout has
    not yet been credited, which is the honest representation of an in-flight
    settlement.
    """
    return {
        "settlement_id": settlement_id,
        "payout_id": payout_id,
        "order_id": order_id,
        "amount": _rupees(net_minor, currency),
        "fee": _rupees(fee_minor, currency),
        "tax": _rupees(tax_minor, currency),
        "currency": currency.value,
        "utr": utr,
        "settled_at": settled_at.astimezone(IST).isoformat(),
        "counterparty": counterparty,
        "status": status,
    }


# --------------------------------------------------------------------------- #
# Bank — statement line
# --------------------------------------------------------------------------- #


def bank_raw(
    *,
    utr: str,
    amount_minor: int,
    currency: Currency,
    is_credit: bool,
    txn_date: date,
    value_date: date,
    narration: str,
    running_balance_minor: int,
) -> dict[str, Any]:
    """A line from a bank statement export.

    The nastiest shape of the four, and the most realistic: column names contain
    spaces and punctuation, dates are ``DD/MM/YYYY``, amounts are comma-grouped
    strings in Indian notation, and Debit/Credit are separate columns of which
    exactly one is populated. The blank one is an empty string, not None or 0 —
    which is precisely the trap that makes naive parsers produce 0-amount rows.
    """
    amount_str = indian_grouped(amount_minor, currency)
    return {
        "Txn Date": txn_date.strftime("%d/%m/%Y"),
        "Value Date": value_date.strftime("%d/%m/%Y"),
        "Description": narration,
        "Ref No./Cheque No.": utr,
        "Debit": "" if is_credit else amount_str,
        "Credit": amount_str if is_credit else "",
        "Balance": indian_grouped(running_balance_minor, currency),
    }


# --------------------------------------------------------------------------- #
# Ledger — GL / order book export
# --------------------------------------------------------------------------- #


def ledger_raw(
    *,
    voucher_no: str,
    gl_date: date,
    party: str | None,
    amount_minor: int,
    currency: Currency,
    is_debit: bool,
    narration: str,
    status: str,
    account: str = "1200 - Accounts Receivable",
) -> dict[str, Any]:
    """A GL export row.

    Direction is expressed as a debit/credit pair rather than a sign, so the
    connector must collapse the two columns into `Direction`. Convention here is
    the cash/AR perspective: a **debit** means money is owed to or received by us
    (inbound); a **credit** means money leaving (outbound).
    """
    amt = f"{to_major(amount_minor, currency):.2f}"
    zero = f"{Decimal('0.00'):.2f}"
    return {
        "voucher_no": voucher_no,
        "gl_date": gl_date.strftime("%Y-%m-%d"),
        "account": account,
        "party": party,
        "debit": amt if is_debit else zero,
        "credit": zero if is_debit else amt,
        "narration": narration,
        "currency": currency.value,
        "status": status,
    }
