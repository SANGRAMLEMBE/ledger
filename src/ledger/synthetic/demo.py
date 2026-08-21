"""
CLI: generate a synthetic batch and print its ground-truth summary.

    python -m ledger.synthetic.demo --events 15000 --seed 42

This is the smoke test that proves the measuring instrument works, and the first
thing a new contributor runs to see the shape of the data.
"""

from __future__ import annotations

import argparse

from ledger.synthetic.generator import SyntheticGenerator


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a synthetic Ledger batch.")
    parser.add_argument("--events", type=int, default=15000,
                        help="Number of economic events (transactions will be more).")
    parser.add_argument("--seed", type=int, default=42, help="RNG seed (reproducible).")
    parser.add_argument("--history-days", type=int, default=30,
                        help="Days of history to span. 30 = dense month-end close "
                             "(reconciliation benchmark); 365 = long history for "
                             "the cash forecaster's backtest.")
    args = parser.parse_args()

    batch = SyntheticGenerator(
        seed=args.seed, history_days=args.history_days
    ).generate(args.events)
    s = batch.summary()

    print(f"Ledger synthetic batch — seed {args.seed}")
    print(f"  window          : {batch.start_date} -> {batch.end_date} "
          f"({s['_history_days']} days)")
    print(f"  transactions    : {s['_transactions_total']:,}")
    print(f"  events          : {s['_events_total']:,}")
    print(f"  true breaks     : {s['_true_breaks']:,}   (must surface as exceptions)")
    print(f"  expected dedupes: {s['_deduped_expected']:,}   (must NOT be exceptions)")
    print(f"  future bookings : {s['_scheduled_outflows']:,}   (forecaster scaffold)")
    print("  case mix:")
    for k in sorted(s):
        if not k.startswith("_"):
            print(f"    {k:24s} {s[k]:>7,}")


if __name__ == "__main__":
    main()
