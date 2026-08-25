"""
Building the daily cash history the forecaster learns from.

ONLY BANK RECORDS ARE CASH
--------------------------
This is the correctness decision the whole forecaster rests on. A ledger entry is
a claim, a gateway capture is a promise, and a settlement line is an instruction —
none of them is money in the account. Only the bank statement records cash that
actually moved.

Building the series from all four sources would count the same rupee up to four
times and produce a forecast that is confidently, enormously wrong. It would also
*look* fine: the curve would be smooth, the trend plausible, and nothing would
flag it.

EVERY DAY MUST APPEAR, INCLUDING THE EMPTY ONES
-----------------------------------------------
A day with no transactions is a day with zero net flow, not a missing day. If gaps
are left out, a "14-day" window silently covers more than 14 calendar days, every
horizon is stretched, and the volatility estimate is inflated because only active
days contribute. Gap-filling is not tidiness here; it is what makes h-day
arithmetic mean what it says.

MONEY STAYS INTEGER
-------------------
Daily flows are exact sums of integer minor units. No float touches the history.
Approximation is confined to the projection, where it belongs, and is rounded back
to integer minor units at that boundary.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from ledger.domain.models import CanonicalTransaction, Direction, Source


@dataclass(frozen=True)
class DailyFlow:
    """One day of actual cash movement."""

    day: date
    inflow_minor: int
    outflow_minor: int

    @property
    def net_minor(self) -> int:
        """Positive means the balance rose that day."""
        return self.inflow_minor - self.outflow_minor

    @property
    def is_quiet(self) -> bool:
        return self.inflow_minor == 0 and self.outflow_minor == 0


def build_cash_history(
    transactions: Iterable[CanonicalTransaction],
    *,
    start: date | None = None,
    end: date | None = None,
    exclude_counterparties: Iterable[str] = (),
) -> list[DailyFlow]:
    """Daily cash movement from bank records, gap-filled and sorted.

    Args:
        transactions: the full batch. Non-bank records are ignored — see the
            module docstring.
        start / end: window bounds. Defaults to the span of the bank records
            present. Passing them explicitly matters when the caller knows the
            true period, because an empty leading day is real information.
        exclude_counterparties: counterparties whose flows are handled
            deterministically elsewhere. Their history is removed here so the
            statistical layer models only the stochastic remainder — leaving
            payroll in both the booked scaffold and the learned residual would
            count it twice, and the forecast would show a payroll run that never
            happens.
    """
    excluded = {name.casefold() for name in exclude_counterparties}

    inflow: dict[date, int] = {}
    outflow: dict[date, int] = {}
    observed: list[date] = []

    for txn in transactions:
        if txn.source is not Source.BANK:
            continue
        if (txn.counterparty or "").casefold() in excluded:
            continue

        observed.append(txn.value_date)
        if txn.direction is Direction.INBOUND:
            inflow[txn.value_date] = inflow.get(txn.value_date, 0) + txn.amount_minor
        else:
            outflow[txn.value_date] = (
                outflow.get(txn.value_date, 0) + txn.amount_minor
            )

    if not observed and start is None:
        return []

    first = start if start is not None else min(observed)
    last = end if end is not None else max(observed)
    if last < first:
        raise ValueError(f"end {last} precedes start {first}")

    history: list[DailyFlow] = []
    cursor = first
    while cursor <= last:
        history.append(
            DailyFlow(
                day=cursor,
                inflow_minor=inflow.get(cursor, 0),
                outflow_minor=outflow.get(cursor, 0),
            )
        )
        cursor += timedelta(days=1)
    return history


def closing_position(
    history: Sequence[DailyFlow], opening_balance_minor: int = 0
) -> int:
    """Balance after applying every day in the history to an opening balance."""
    return opening_balance_minor + sum(day.net_minor for day in history)


def summarise(history: Sequence[DailyFlow]) -> dict[str, int]:
    """Shape of the series, for sanity-checking before a model is fitted."""
    if not history:
        return {"days": 0}
    nets = [day.net_minor for day in history]
    return {
        "days": len(history),
        "quiet_days": sum(1 for day in history if day.is_quiet),
        "total_inflow_minor": sum(day.inflow_minor for day in history),
        "total_outflow_minor": sum(day.outflow_minor for day in history),
        "net_minor": sum(nets),
        "best_day_minor": max(nets),
        "worst_day_minor": min(nets),
    }
