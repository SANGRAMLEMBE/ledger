"""
The metrics report — one command, the same numbers every time.

    python -m ledger.eval.report                    # held-out seed, the real numbers
    python -m ledger.eval.report --seed 42          # development seed
    python -m ledger.eval.report --events 15000

This is what gets run on stage and what backs every claim in the submission. It
prints the three headline figures, the exception breakdown that makes them
honest, and a verdict on whether the run is publishable at all.

The verdict is not decoration. A run with a false match, a lost record, or a
guessed-at ambiguous settlement is refused rather than reported, because each of
those makes the headline numbers look *better* while making them untrue. It exits
non-zero so CI can enforce it.
"""

from __future__ import annotations

import argparse
import sys

from ledger.domain.models import Currency, to_major
from ledger.eval.harness import DEV_SEED, HELD_OUT_SEED, EvalHarness, tier_counts

BAR = "-" * 66


def _rupees(minor: int) -> str:
    return f"Rs {to_major(minor, Currency.INR):,.2f}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the reconciliation benchmark and report its metrics."
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=HELD_OUT_SEED,
        help=f"RNG seed. Default {HELD_OUT_SEED} (held out). "
        f"Use {DEV_SEED} while developing.",
    )
    parser.add_argument("--events", type=int, default=15_000)
    args = parser.parse_args()

    result = EvalHarness().run(seed=args.seed, events=args.events)
    held_out = args.seed == HELD_OUT_SEED

    print()
    print(BAR)
    print(f"  LEDGER BENCHMARK   seed {args.seed}" + ("  (HELD OUT)" if held_out else ""))
    if not held_out:
        print("  development seed — not for reporting")
    print(BAR)

    ing = result.ingestion
    print("\n  INGESTION")
    print(f"    records in       {ing.total_in:>12,}")
    print(f"    accepted         {ing.accepted:>12,}")
    print(f"    deduplicated     {ing.deduplicated:>12,}   (re-imports, handled)")
    print(f"    rejected         {ing.rejected:>12,}")

    print("\n  THE THREE NUMBERS")
    print(
        f"    match rate       {result.match_rate:>11.2%}   "
        f"({result.events_reconciled:,}/{result.events_expected_match:,} events)"
    )
    print(f"    throughput       {result.throughput:>11,.0f}   records/sec")
    print(
        f"    exceptions       {result.exceptions.count:>12,}   "
        f"{_rupees(result.exceptions.total_at_risk_minor)} at risk"
    )

    print("\n  CORRECTNESS  (reported separately, never blended)")
    print(f"    false matches    {result.false_matches:>12,}   <- must be 0")
    print(f"    unexplained      {result.unexplained_records:>12,}   <- must be 0")
    print(
        f"    breaks surfaced  {result.break_recall:>11.2%}   "
        f"({result.breaks_surfaced:,}/{result.breaks_planted:,} planted)"
    )
    print(
        f"    ambiguity held   {result.ambiguous_held:>12,}"
        f"/{result.ambiguous_planted:,}   refused rather than guessed"
    )

    print("\n  EXCEPTIONS BY TYPE")
    at_risk = result.exceptions.at_risk_by_type()
    for name, count in sorted(
        result.exceptions.by_type().items(), key=lambda kv: -kv[1]
    ):
        print(f"    {name:<26} {count:>7,}   {_rupees(at_risk[name]):>20}")

    print("\n  RESOLVED BY TIER")
    for tier, count in tier_counts(result).items():
        note = "   (no model involved)" if tier in ("exact", "rule") else ""
        print(f"    {tier:<26} {count:>7,}{note}")

    if result.missed_by_case:
        print("\n  WHERE THE MISSES ARE")
        for case, count in sorted(
            result.missed_by_case.items(), key=lambda kv: -kv[1]
        ):
            print(f"    {case:<26} {count:>7,}")

    ok, problems = result.is_publishable()
    print()
    print(BAR)
    if ok:
        print("  VERDICT: publishable.")
        if not held_out:
            print(f"  Development seed — rerun with --seed {HELD_OUT_SEED} to report.")
    else:
        print("  VERDICT: NOT publishable. These numbers must not be quoted.")
        for problem in problems:
            print(f"    - {problem}")
    print(BAR)
    print()

    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
