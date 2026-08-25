"""
The cash forecast report.

    python -m ledger.forecasting.report
    python -m ledger.forecasting.report --horizon 45 --floor 5000000

Runs the full path — generate, ingest, reconcile, build the cash history, fit,
backtest, project — and prints the forward position with its band, its booked
commitments, and the backtest that says whether the band can be trusted.

The backtest is printed **above** the projection on purpose. A forecast whose
interval does not hold up on history is not a forecast, and showing the pretty
curve first invites reading it before the caveat.
"""

from __future__ import annotations

import argparse
import sys
from typing import Any

from ledger.domain.models import Currency, Source, to_major
from ledger.forecasting.backtest import backtest
from ledger.forecasting.history import build_cash_history, closing_position, summarise
from ledger.forecasting.model import (
    BookedOutflow,
    CashForecaster,
    InsufficientHistoryError,
)
from ledger.ingestion.pipeline import IngestionPipeline
from ledger.reconciliation.engine import ReconciliationEngine
from ledger.synthetic.generator import RECURRING_OUTFLOWS, SyntheticGenerator

BAR = "-" * 70

# The opening balance the generator seeds the statement with. Passed in rather
# than inferred: only the bank knows what is actually in the account, and the
# forecaster projects change, not level.
OPENING_BALANCE_MINOR = 500_000_000


def _rs(minor: int) -> str:
    return f"Rs {to_major(minor, Currency.INR):>16,.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(description="Forward cash position with a band.")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--events", type=int, default=20_000)
    parser.add_argument(
        "--history-days",
        type=int,
        default=365,
        help="Days of history. A T+30 forecast cannot be backtested on 30 days.",
    )
    parser.add_argument("--horizon", type=int, default=45)
    parser.add_argument(
        "--floor",
        type=int,
        default=0,
        help="Safety floor in minor units. A shortfall fires when P10 dips below.",
    )
    args = parser.parse_args()

    batch = SyntheticGenerator(
        seed=args.seed, history_days=args.history_days
    ).generate(args.events)

    grouped: dict[Source, list[dict[str, Any]]] = {s: [] for s in Source}
    for record in batch.transactions:
        grouped[record.source].append(record.raw)
    ingested, _ = IngestionPipeline().ingest(grouped)
    reconciliation = ReconciliationEngine().reconcile(ingested)

    # Counterparties with booked future outflows are handled deterministically,
    # so their history is removed before fitting. Leaving them in would model the
    # same payroll it books and project two of them.
    booked_names = {label for label, *_ in RECURRING_OUTFLOWS}
    history = build_cash_history(
        ingested,
        start=batch.start_date,
        end=batch.end_date,
        exclude_counterparties=booked_names,
    )

    print()
    print(BAR)
    print(f"  CASH FORECAST   seed {args.seed}   as at {batch.end_date}")
    print(BAR)

    shape = summarise(history)
    print("\n  HISTORY  (bank movements only — a ledger entry is not cash)")
    print(f"    days                 {shape['days']:>10,}")
    print(f"    quiet days           {shape['quiet_days']:>10,}")
    print(f"    total in             {_rs(shape['total_inflow_minor'])}")
    print(f"    total out            {_rs(shape['total_outflow_minor'])}")
    print(f"    reconciled records   {reconciliation.matched_count:>10,}")

    try:
        forecaster = CashForecaster(seed=args.seed).fit(history)
    except InsufficientHistoryError as exc:
        print(f"\n  REFUSED: {exc}")
        return 1

    print("\n  BACKTEST  (does the band hold up on held-out history?)")
    scores = backtest(history)
    for line in scores.summary():
        print(f"    {line}")
    if not scores.is_calibrated:
        print("\n    The interval does not hold up. Treat the projection below as")
        print("    indicative only — a band that is wrong is worse than none.")

    booked = [
        BookedOutflow(
            label=outflow.label,
            due_date=outflow.due_date,
            amount_minor=outflow.amount_minor,
            certainty=outflow.certainty,
        )
        for outflow in batch.scheduled_outflows
    ]
    opening = closing_position(history, OPENING_BALANCE_MINOR)

    forecast = forecaster.project(
        start_day=batch.end_date,
        horizon_days=args.horizon,
        opening_balance_minor=opening,
        booked=booked,
        safety_floor_minor=args.floor,
    )

    print(f"\n  BOOKED COMMITMENTS  (not forecast — known)   {len(booked)} item(s)")
    for outflow in booked:
        print(
            f"    {outflow.due_date}  {outflow.label:<22} "
            f"{_rs(outflow.amount_minor)}  [{outflow.certainty}]"
        )

    print(f"\n  PROJECTED POSITION  opening {_rs(opening)}")
    print(f"    {'day':<12} {'P10':>18} {'P50':>18} {'P90':>18}")
    for point in forecast.points:
        marker = "  <- booked" if point.booked_outflow_minor else ""
        if point.day.day == 1 or point is forecast.points[-1] or marker:
            print(
                f"    {point.day!s:<12} {_rs(point.p10_minor)} "
                f"{_rs(point.p50_minor)} {_rs(point.p90_minor)}{marker}"
            )

    print("\n  SHORTFALL WATCH  (fires on P10 — a plausible bad day, not the median)")
    breaches = forecast.shortfalls()
    if breaches:
        lead = forecast.shortfall_lead_days()
        print(f"    {len(breaches)} day(s) below the floor, first in {lead} day(s):")
        print(f"    {breaches[0].day}  P10 {_rs(breaches[0].p10_minor)}")
    else:
        print(f"    no breach of {_rs(args.floor)} within {args.horizon} days")

    print()
    print(BAR)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
