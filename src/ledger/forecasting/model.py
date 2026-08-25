"""
The forward cash forecaster.

Knowing today's balance is easy. Knowing whether payroll clears in eighteen days
is not, and that is the question a treasurer actually has.

BOOK WHAT YOU KNOW; MODEL ONLY THE REST
---------------------------------------
Payroll on the 1st is not a prediction. The date is known, the amount is known,
and it is contractually certain. Feeding it to a statistical model would replace
arithmetic with an estimate — strictly worse, and it would smear a sharp, certain
outflow across a probability band.

So the projection is built in two parts:

    forecast = booked outflows (exact)  +  modelled residual (uncertain)

and the residual is fitted on history with those same booked flows removed. That
removal is load-bearing: leaving payroll in both halves would count it twice and
project a second payroll run that never happens.

A BAND, NEVER A POINT
---------------------
Output is P10 / P50 / P90. A single line implies a precision nobody has, and a
treasurer who acts on it is acting on a number that was never true. The band is
the honest part.

The distribution is produced by **bootstrap** rather than a normal approximation.
Daily cash flow is not symmetric — a few large vendor payments make the left tail
heavier than the right — and a normal band would be too narrow exactly where it
matters, on the downside. Resampling observed days makes no shape assumption at
all.

CALIBRATION IS THE METRIC THAT MATTERS
--------------------------------------
Accuracy tells you how close the middle was. Calibration tells you whether the
band can be trusted: does the P10-P90 interval actually contain the outcome about
80% of the time? A narrow band that is wrong 40% of the time is far more dangerous
than a wide, honest one, because someone will make a payroll decision on it.
`backtest.py` measures this.

DETERMINISM
-----------
The bootstrap is seeded. The same history and seed produce the same band every
time — a forecast that moves between runs cannot be reported, defended, or acted
on.
"""

from __future__ import annotations

import random
import statistics
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta

from ledger.forecasting.history import DailyFlow

# Percentiles reported. P10/P90 gives an 80% central interval, which is the band
# `backtest.py` checks coverage against.
LOW_PERCENTILE = 10
MID_PERCENTILE = 50
HIGH_PERCENTILE = 90

# Bootstrap draws. Enough that the percentile estimates are stable to the rupee
# at these amounts; more would buy precision the underlying data does not support.
DEFAULT_SIMULATIONS = 2_000

# A history shorter than this cannot support a distribution — resampling a
# handful of days produces a band that is an artefact of those days rather than
# an estimate. The forecaster refuses instead of returning a confident-looking
# number built on nothing.
MIN_HISTORY_DAYS = 14


class InsufficientHistoryError(ValueError):
    """Raised when there is not enough history to model honestly."""


@dataclass(frozen=True)
class BookedOutflow:
    """A known future payment. Not forecast — booked."""

    label: str
    due_date: date
    amount_minor: int
    certainty: str = "booked"


@dataclass(frozen=True)
class ForecastPoint:
    """The projected position on one future day."""

    day: date
    p10_minor: int
    p50_minor: int
    p90_minor: int
    booked_outflow_minor: int

    def __post_init__(self) -> None:
        # A band whose bounds are out of order is a bug that would otherwise
        # surface as a nonsensical chart rather than an error.
        if not (self.p10_minor <= self.p50_minor <= self.p90_minor):
            raise ValueError(
                f"percentiles out of order on {self.day}: "
                f"p10={self.p10_minor} p50={self.p50_minor} p90={self.p90_minor}"
            )


@dataclass
class Forecast:
    """A projected cash curve with its uncertainty and its known commitments."""

    opening_balance_minor: int
    points: list[ForecastPoint]
    safety_floor_minor: int = 0
    booked: list[BookedOutflow] = field(default_factory=list)
    history_days: int = 0

    @property
    def horizon_days(self) -> int:
        return len(self.points)

    def shortfalls(self) -> list[ForecastPoint]:
        """Days where the pessimistic case breaches the floor.

        Alerting on P10 rather than P50 is deliberate. A shortfall warning exists
        to be acted on in advance, so it should fire on a plausible bad outcome,
        not only when the median case is already underwater — by which point
        there is no time left to do anything.
        """
        return [p for p in self.points if p.p10_minor < self.safety_floor_minor]

    def shortfall_lead_days(self) -> int | None:
        """Days of warning before the first projected breach."""
        breaches = self.shortfalls()
        if not breaches:
            return None
        return (breaches[0].day - self.points[0].day).days

    def total_booked_minor(self) -> int:
        return sum(b.amount_minor for b in self.booked)


class CashForecaster:
    """Projects the cash position forward from reconciled history."""

    def __init__(
        self,
        seed: int = 7,
        simulations: int = DEFAULT_SIMULATIONS,
        min_history_days: int = MIN_HISTORY_DAYS,
    ) -> None:
        self.seed = seed
        self.simulations = simulations
        self.min_history_days = min_history_days
        self._residuals: list[int] = []
        self._fitted = False

    # -- fitting -------------------------------------------------------------

    def fit(self, history: Sequence[DailyFlow]) -> CashForecaster:
        """Learn the daily residual distribution.

        The history passed here must already have booked flows removed (see
        `build_cash_history(exclude_counterparties=...)`), or the booked
        component is modelled as well as booked and lands in the forecast twice.
        """
        if len(history) < self.min_history_days:
            raise InsufficientHistoryError(
                f"{len(history)} day(s) of history; at least "
                f"{self.min_history_days} are needed to estimate a distribution. "
                "Refusing to produce a band that would be an artefact of a "
                "handful of days."
            )
        self._residuals = [day.net_minor for day in history]
        self._fitted = True
        return self

    @property
    def daily_mean_minor(self) -> float:
        self._require_fit()
        return statistics.fmean(self._residuals)

    @property
    def daily_stdev_minor(self) -> float:
        self._require_fit()
        if len(self._residuals) < 2:  # pragma: no cover - fit() enforces >= 14
            return 0.0
        return statistics.stdev(self._residuals)

    # -- projection ----------------------------------------------------------

    def project(
        self,
        *,
        start_day: date,
        horizon_days: int,
        opening_balance_minor: int,
        booked: Sequence[BookedOutflow] = (),
        safety_floor_minor: int = 0,
    ) -> Forecast:
        """Project the balance forward day by day.

        Args:
            start_day: the first *projected* day, i.e. the day after the history
                ends.
            horizon_days: how far ahead to project.
            opening_balance_minor: the balance as at the end of history. The
                forecaster projects change; the level comes from the bank, since
                only the bank knows what is actually in the account.
            booked: known future outflows, applied exactly on their due dates.
            safety_floor_minor: the balance below which a shortfall is raised.
        """
        self._require_fit()
        if horizon_days < 1:
            raise ValueError("horizon_days must be at least 1")

        rng = random.Random(self.seed)
        by_due: dict[date, int] = {}
        for outflow in booked:
            if outflow.amount_minor < 0:
                raise ValueError(
                    f"booked outflow {outflow.label!r} has a negative amount; "
                    "outflows are positive magnitudes"
                )
            by_due[outflow.due_date] = (
                by_due.get(outflow.due_date, 0) + outflow.amount_minor
            )

        # One simulated path per draw: cumulative residual, day by day. Paths are
        # built across the whole horizon rather than sampling each day
        # independently, because a cash position is a running total — the
        # uncertainty at T+30 is the accumulated uncertainty of thirty days, not
        # the spread of a single day.
        paths: list[list[int]] = []
        for _ in range(self.simulations):
            running = 0
            path: list[int] = []
            for _ in range(horizon_days):
                running += rng.choice(self._residuals)
                path.append(running)
            paths.append(path)

        points: list[ForecastPoint] = []
        cumulative_booked = 0
        for offset in range(horizon_days):
            day = start_day + timedelta(days=offset)
            cumulative_booked += by_due.get(day, 0)

            draws = sorted(path[offset] for path in paths)
            base = opening_balance_minor - cumulative_booked

            points.append(
                ForecastPoint(
                    day=day,
                    p10_minor=base + _percentile(draws, LOW_PERCENTILE),
                    p50_minor=base + _percentile(draws, MID_PERCENTILE),
                    p90_minor=base + _percentile(draws, HIGH_PERCENTILE),
                    booked_outflow_minor=by_due.get(day, 0),
                )
            )

        return Forecast(
            opening_balance_minor=opening_balance_minor,
            points=points,
            safety_floor_minor=safety_floor_minor,
            booked=list(booked),
            history_days=len(self._residuals),
        )

    def _require_fit(self) -> None:
        if not self._fitted:
            raise RuntimeError("fit() must be called before projecting")


def _percentile(sorted_values: Sequence[int], percentile: int) -> int:
    """Nearest-rank percentile of an already-sorted sequence, as minor units.

    Nearest-rank rather than interpolated: an interpolated percentile invents a
    value that was never observed, and returning a rupee amount nobody's data
    contained is the wrong kind of precision for money. The result is always a
    real draw from the simulation.
    """
    if not sorted_values:  # pragma: no cover - callers always pass draws
        raise ValueError("cannot take a percentile of an empty sequence")
    rank = max(1, round(percentile / 100 * len(sorted_values)))
    return sorted_values[min(rank, len(sorted_values)) - 1]
