"""
Backtesting the forecaster on held-out history.

A forecast is a claim about the future, so the only honest way to evaluate it is
to stand at a past date, project forward using *only* what was known then, and
compare against what actually happened.

THE LEAKAGE TRAP
----------------
The mistake this module exists to avoid: fitting on the full history and then
"testing" on a slice of it. The model has already seen the answer, every error is
tiny, and the report looks superb. `_split` is the only place history is divided,
and the model is re-fitted from scratch at each cutoff on the prefix alone.

TWO NUMBERS, NOT ONE
--------------------
**Accuracy** (MAE / MAPE) says how close the middle line was.

**Calibration** says whether the band means anything: does the P10-P90 interval
contain the actual outcome about 80% of the time? These measure different things
and a model can be good at one and dangerous at the other. A narrow band that is
wrong 40% of the time scores well on MAE and will get someone into trouble,
because the whole point of the interval is that a treasurer can plan against its
lower edge.

Under-coverage is the dangerous direction: it means the forecaster is more
confident than it has earned. Over-coverage merely means the band is wider than
necessary, which is uninformative rather than unsafe.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from ledger.forecasting.history import DailyFlow
from ledger.forecasting.model import CashForecaster, InsufficientHistoryError

# Horizons the plan calls for.
DEFAULT_HORIZONS = (7, 14, 30)

# Target coverage of the P10-P90 band.
TARGET_COVERAGE = 0.80

# How far coverage may drift before the band is misleading rather than merely
# imperfect. Backtests have limited sample sizes, so an exact 0.80 is not a
# realistic bar; being inside 0.60-0.95 means the interval is broadly telling the
# truth about its own uncertainty.
COVERAGE_TOLERANCE = 0.20


@dataclass
class HorizonScore:
    """Accuracy and calibration at one horizon."""

    horizon_days: int
    samples: int = 0
    absolute_errors_minor: list[int] = field(default_factory=list)
    percentage_errors: list[float] = field(default_factory=list)
    inside_band: int = 0

    @property
    def mae_minor(self) -> int:
        if not self.absolute_errors_minor:
            return 0
        return round(sum(self.absolute_errors_minor) / len(self.absolute_errors_minor))

    @property
    def mape(self) -> float:
        if not self.percentage_errors:
            return 0.0
        return sum(self.percentage_errors) / len(self.percentage_errors)

    @property
    def coverage(self) -> float:
        """Fraction of actuals that fell inside the P10-P90 band."""
        return self.inside_band / self.samples if self.samples else 0.0

    @property
    def is_calibrated(self) -> bool:
        return abs(self.coverage - TARGET_COVERAGE) <= COVERAGE_TOLERANCE


@dataclass
class BacktestResult:
    scores: dict[int, HorizonScore] = field(default_factory=dict)
    cutoffs_evaluated: int = 0
    cutoffs_skipped: int = 0

    @property
    def is_calibrated(self) -> bool:
        return bool(self.scores) and all(
            score.is_calibrated for score in self.scores.values()
        )

    def summary(self) -> list[str]:
        lines = []
        for horizon in sorted(self.scores):
            score = self.scores[horizon]
            flag = "ok" if score.is_calibrated else "MISCALIBRATED"
            lines.append(
                f"T+{horizon:<3} MAE {score.mae_minor / 100:>14,.2f}  "
                f"MAPE {score.mape:>6.1%}  "
                f"coverage {score.coverage:>6.1%} [{flag}]  "
                f"n={score.samples}"
            )
        return lines


def _split(
    history: Sequence[DailyFlow], cutoff: int
) -> tuple[Sequence[DailyFlow], Sequence[DailyFlow]]:
    """History known at the cutoff, and what happened after. The only split."""
    return history[:cutoff], history[cutoff:]


def backtest(
    history: Sequence[DailyFlow],
    *,
    horizons: Sequence[int] = DEFAULT_HORIZONS,
    seed: int = 7,
    simulations: int = 500,
    step_days: int = 7,
    min_train_days: int = 60,
) -> BacktestResult:
    """Walk forward through history, forecasting from each cutoff.

    Args:
        history: the full daily series, booked flows already excluded.
        horizons: how far ahead to score.
        step_days: gap between cutoffs. Overlapping windows share data and their
            errors are correlated, so a step of a week keeps the samples closer
            to independent than testing every single day would.
        min_train_days: the shortest prefix worth fitting on.
        simulations: fewer draws than a production projection — a backtest fits
            many models and the percentile estimates only need to be stable to
            within a rupee or so.
    """
    result = BacktestResult(
        scores={h: HorizonScore(horizon_days=h) for h in horizons}
    )
    longest = max(horizons)

    cutoff = min_train_days
    while cutoff + longest <= len(history):
        train, future = _split(history, cutoff)

        try:
            forecaster = CashForecaster(seed=seed, simulations=simulations).fit(train)
        except InsufficientHistoryError:  # pragma: no cover - guarded by min_train_days
            result.cutoffs_skipped += 1
            cutoff += step_days
            continue

        projection = forecaster.project(
            start_day=future[0].day,
            horizon_days=longest,
            # Backtesting the *change*, so the level is irrelevant and starting
            # from zero keeps MAPE meaningful — a percentage error against an
            # arbitrary opening balance would say nothing about the model.
            opening_balance_minor=0,
        )

        for horizon in horizons:
            actual = sum(day.net_minor for day in future[:horizon])
            point = projection.points[horizon - 1]
            score = result.scores[horizon]

            score.samples += 1
            score.absolute_errors_minor.append(abs(point.p50_minor - actual))
            if actual != 0:
                score.percentage_errors.append(
                    abs(point.p50_minor - actual) / abs(actual)
                )
            if point.p10_minor <= actual <= point.p90_minor:
                score.inside_band += 1

        result.cutoffs_evaluated += 1
        cutoff += step_days

    return result
