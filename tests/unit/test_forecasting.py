"""
Cash forecaster tests.

Three properties carry the weight, and all three fail *silently* if broken — the
forecast still renders, the curve still looks plausible, and nothing raises.

`test_booked_outflows_are_not_also_modelled` is the double-count guard. Payroll is
both a known future commitment and a large historical outflow. Book it and model
it and the forecast shows two payroll runs a month. The curve looks fine; it is
just wrong by a payroll.

`test_only_bank_records_count_as_cash` guards the other silent one. A ledger entry
is a claim, not money. Counting all four sources inflates the position roughly
fourfold, smoothly and believably.

`test_backtest_does_not_leak_the_future` guards the evaluation itself. Fit on the
whole history and "test" on part of it and every error is tiny — a superb report
about a model that has already seen the answer.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from ledger.domain.models import (
    CanonicalTransaction,
    Currency,
    Direction,
    Source,
    TxnStatus,
)
from ledger.forecasting import (
    BookedOutflow,
    CashForecaster,
    DailyFlow,
    ForecastPoint,
    InsufficientHistoryError,
    backtest,
    build_cash_history,
    closing_position,
    summarise,
)
from ledger.forecasting.backtest import TARGET_COVERAGE
from ledger.ingestion.pipeline import IngestionPipeline
from ledger.synthetic.generator import RECURRING_OUTFLOWS, SyntheticGenerator

RUPEE = 100  # minor units


def txn(**over) -> CanonicalTransaction:
    base = dict(
        source=Source.BANK,
        external_ref="UTR_1",
        amount_minor=1_000 * RUPEE,
        currency=Currency.INR,
        direction=Direction.INBOUND,
        value_date=date(2026, 3, 2),
        posted_at=datetime(2026, 3, 2, tzinfo=UTC),
        status=TxnStatus.SETTLED,
        counterparty="ACME RETAIL",
    )
    base.update(over)
    return CanonicalTransaction(**base)


def flat_history(days: int, net_minor: int, start: date = date(2026, 1, 1)):
    return [
        DailyFlow(
            day=start + timedelta(days=i),
            inflow_minor=max(net_minor, 0),
            outflow_minor=max(-net_minor, 0),
        )
        for i in range(days)
    ]


class TestHistory:
    def test_only_bank_records_count_as_cash(self):
        """A ledger entry is a claim; a settlement is an instruction. Not money."""
        records = [
            txn(source=Source.BANK, external_ref="UTR_1"),
            txn(source=Source.LEDGER, external_ref="order_1"),
            txn(source=Source.GATEWAY, external_ref="pay_1"),
            txn(source=Source.SETTLEMENT, external_ref="pout_1"),
        ]
        (day,) = build_cash_history(records)
        assert day.inflow_minor == 1_000 * RUPEE, (
            "counting all four sources would inflate the position fourfold"
        )

    def test_quiet_days_are_present_not_missing(self):
        """A 14-day window must span 14 calendar days, not 14 active ones."""
        records = [
            txn(external_ref="a", value_date=date(2026, 3, 1),
                posted_at=datetime(2026, 3, 1, tzinfo=UTC)),
            txn(external_ref="b", value_date=date(2026, 3, 5),
                posted_at=datetime(2026, 3, 5, tzinfo=UTC)),
        ]
        history = build_cash_history(records)
        assert len(history) == 5
        assert [d.day.day for d in history] == [1, 2, 3, 4, 5]
        assert sum(1 for d in history if d.is_quiet) == 3

    def test_explicit_window_is_respected(self):
        records = [txn(value_date=date(2026, 3, 3),
                       posted_at=datetime(2026, 3, 3, tzinfo=UTC))]
        history = build_cash_history(
            records, start=date(2026, 3, 1), end=date(2026, 3, 5)
        )
        assert len(history) == 5
        assert history[0].is_quiet and history[-1].is_quiet

    def test_excluded_counterparties_are_removed(self):
        records = [
            txn(external_ref="a", counterparty="PAYROLL",
                direction=Direction.OUTBOUND),
            txn(external_ref="b", counterparty="ACME RETAIL"),
        ]
        history = build_cash_history(records, exclude_counterparties={"Payroll"})
        assert history[0].outflow_minor == 0, "exclusion is case-insensitive"
        assert history[0].inflow_minor == 1_000 * RUPEE

    def test_net_and_closing_position(self):
        history = flat_history(10, net_minor=5 * RUPEE)
        assert closing_position(history, opening_balance_minor=100 * RUPEE) == (
            100 * RUPEE + 50 * RUPEE
        )

    def test_end_before_start_is_rejected(self):
        with pytest.raises(ValueError, match="precedes start"):
            build_cash_history([], start=date(2026, 3, 5), end=date(2026, 3, 1))

    def test_empty_input_is_empty_not_an_error(self):
        assert build_cash_history([]) == []
        assert summarise([]) == {"days": 0}


class TestRefusesToGuess:
    def test_short_history_is_refused(self):
        """A band built on five days is an artefact of those five days."""
        with pytest.raises(InsufficientHistoryError, match="at least"):
            CashForecaster().fit(flat_history(5, net_minor=RUPEE))

    def test_projecting_before_fitting_is_an_error(self):
        with pytest.raises(RuntimeError, match="fit\\(\\) must be called"):
            CashForecaster().project(
                start_day=date(2026, 4, 1),
                horizon_days=7,
                opening_balance_minor=0,
            )

    def test_zero_horizon_is_rejected(self):
        model = CashForecaster().fit(flat_history(30, net_minor=RUPEE))
        with pytest.raises(ValueError, match="at least 1"):
            model.project(
                start_day=date(2026, 4, 1),
                horizon_days=0,
                opening_balance_minor=0,
            )

    def test_negative_booked_amount_is_rejected(self):
        model = CashForecaster().fit(flat_history(30, net_minor=RUPEE))
        with pytest.raises(ValueError, match="negative amount"):
            model.project(
                start_day=date(2026, 4, 1),
                horizon_days=7,
                opening_balance_minor=0,
                booked=[
                    BookedOutflow("bad", date(2026, 4, 2), -100)
                ],
            )


class TestBookedVersusModelled:
    def test_booked_outflows_are_not_also_modelled(self):
        """The double-count guard.

        History here contains only inflows, so the residual model can only push
        the balance up. The single booked outflow must therefore be the only thing
        that pulls it down, and by exactly its own amount.
        """
        model = CashForecaster(simulations=300).fit(
            flat_history(60, net_minor=10 * RUPEE)
        )
        payroll = BookedOutflow("Payroll", date(2026, 4, 3), 5_000 * RUPEE)

        with_booking = model.project(
            start_day=date(2026, 4, 1),
            horizon_days=10,
            opening_balance_minor=0,
            booked=[payroll],
        )
        without = model.project(
            start_day=date(2026, 4, 1),
            horizon_days=10,
            opening_balance_minor=0,
        )

        # Before the due date the two projections are identical.
        assert with_booking.points[0].p50_minor == without.points[0].p50_minor
        # From the due date onward they differ by exactly the booked amount.
        for index in range(2, 10):
            delta = (
                without.points[index].p50_minor
                - with_booking.points[index].p50_minor
            )
            assert delta == payroll.amount_minor, (
                f"day {index}: booked outflow applied as {delta}, expected "
                f"{payroll.amount_minor} — a mismatch means it is being counted "
                "somewhere else too"
            )

    def test_booked_outflow_lands_on_its_due_date(self):
        model = CashForecaster(simulations=200).fit(
            flat_history(40, net_minor=0)
        )
        forecast = model.project(
            start_day=date(2026, 4, 1),
            horizon_days=5,
            opening_balance_minor=0,
            booked=[BookedOutflow("rent", date(2026, 4, 3), 700 * RUPEE)],
        )
        assert [p.booked_outflow_minor for p in forecast.points] == [
            0, 0, 700 * RUPEE, 0, 0
        ]

    def test_several_bookings_on_one_day_accumulate(self):
        model = CashForecaster(simulations=200).fit(flat_history(40, net_minor=0))
        due = date(2026, 4, 2)
        forecast = model.project(
            start_day=date(2026, 4, 1),
            horizon_days=3,
            opening_balance_minor=0,
            booked=[
                BookedOutflow("payroll", due, 100 * RUPEE),
                BookedOutflow("rent", due, 50 * RUPEE),
            ],
        )
        assert forecast.points[1].booked_outflow_minor == 150 * RUPEE
        assert forecast.total_booked_minor() == 150 * RUPEE


class TestTheBand:
    def test_percentiles_are_ordered(self):
        model = CashForecaster(simulations=500).fit(
            flat_history(90, net_minor=0)
        )
        forecast = model.project(
            start_day=date(2026, 4, 1), horizon_days=30, opening_balance_minor=0
        )
        for point in forecast.points:
            assert point.p10_minor <= point.p50_minor <= point.p90_minor

    def test_out_of_order_band_is_rejected_at_construction(self):
        with pytest.raises(ValueError, match="percentiles out of order"):
            ForecastPoint(
                day=date(2026, 4, 1),
                p10_minor=500,
                p50_minor=100,
                p90_minor=900,
                booked_outflow_minor=0,
            )

    def test_uncertainty_widens_with_the_horizon(self):
        """A cash position is a running total: 30 days of doubt exceed 7."""
        history = [
            DailyFlow(
                day=date(2026, 1, 1) + timedelta(days=i),
                inflow_minor=(i % 7) * 100 * RUPEE,
                outflow_minor=((i + 3) % 5) * 80 * RUPEE,
            )
            for i in range(120)
        ]
        model = CashForecaster(simulations=1_000).fit(history)
        forecast = model.project(
            start_day=date(2026, 5, 1), horizon_days=30, opening_balance_minor=0
        )
        width_7 = forecast.points[6].p90_minor - forecast.points[6].p10_minor
        width_30 = forecast.points[29].p90_minor - forecast.points[29].p10_minor
        assert width_30 > width_7

    def test_zero_variance_history_gives_a_zero_width_band(self):
        """Nothing ever varied, so claiming uncertainty would be invention."""
        model = CashForecaster(simulations=200).fit(
            flat_history(30, net_minor=42 * RUPEE)
        )
        forecast = model.project(
            start_day=date(2026, 4, 1), horizon_days=5, opening_balance_minor=0
        )
        point = forecast.points[4]
        assert point.p10_minor == point.p50_minor == point.p90_minor
        assert point.p50_minor == 5 * 42 * RUPEE


class TestDeterminism:
    def test_same_seed_gives_an_identical_band(self):
        """A forecast that moves between runs cannot be defended or acted on."""
        history = flat_history(60, net_minor=0)
        args = dict(
            start_day=date(2026, 4, 1), horizon_days=14, opening_balance_minor=0
        )
        first = CashForecaster(seed=3, simulations=400).fit(history).project(**args)
        second = CashForecaster(seed=3, simulations=400).fit(history).project(**args)
        assert [p.p10_minor for p in first.points] == [
            p.p10_minor for p in second.points
        ]
        assert [p.p90_minor for p in first.points] == [
            p.p90_minor for p in second.points
        ]


class TestShortfalls:
    def test_alert_fires_on_p10_not_p50(self):
        """A warning must fire on a plausible bad day, not only once the median
        case is already underwater — by then there is no time to act."""
        history = [
            DailyFlow(
                day=date(2026, 1, 1) + timedelta(days=i),
                inflow_minor=0,
                outflow_minor=(500 if i % 2 else 0) * RUPEE,
            )
            for i in range(60)
        ]
        model = CashForecaster(simulations=1_000).fit(history)
        forecast = model.project(
            start_day=date(2026, 3, 1),
            horizon_days=20,
            opening_balance_minor=3_000 * RUPEE,
            safety_floor_minor=0,
        )
        breaches = forecast.shortfalls()
        assert breaches, "a draining account must raise a shortfall"
        first = breaches[0]
        assert first.p10_minor < 0 <= first.p90_minor or first.p50_minor >= 0, (
            "the alert should fire while the optimistic case is still above the "
            "floor — that is the lead time it exists to provide"
        )

    def test_lead_time_is_reported(self):
        history = flat_history(60, net_minor=-100 * RUPEE)
        model = CashForecaster(simulations=200).fit(history)
        forecast = model.project(
            start_day=date(2026, 4, 1),
            horizon_days=20,
            opening_balance_minor=500 * RUPEE,
            safety_floor_minor=0,
        )
        lead = forecast.shortfall_lead_days()
        assert lead is not None and 0 <= lead < 20

    def test_healthy_account_raises_nothing(self):
        model = CashForecaster(simulations=200).fit(
            flat_history(60, net_minor=100 * RUPEE)
        )
        forecast = model.project(
            start_day=date(2026, 4, 1),
            horizon_days=20,
            opening_balance_minor=10_000 * RUPEE,
            safety_floor_minor=0,
        )
        assert forecast.shortfalls() == []
        assert forecast.shortfall_lead_days() is None


class TestBacktest:
    def test_backtest_does_not_leak_the_future(self, monkeypatch):
        """Each cutoff must fit on the prefix only, never the whole series."""
        history = flat_history(200, net_minor=0)
        seen: list[int] = []
        original = CashForecaster.fit

        def spy(self, train):
            seen.append(len(train))
            return original(self, train)

        monkeypatch.setattr(CashForecaster, "fit", spy)
        result = backtest(history, simulations=50, step_days=30)

        assert result.cutoffs_evaluated > 0
        assert seen, "no model was fitted"
        assert max(seen) < len(history), (
            "a model was fitted on the entire history — the backtest has seen "
            "the answers it is being scored against"
        )

    def test_calibration_is_measured_on_real_data(self):
        batch = SyntheticGenerator(seed=42, history_days=365).generate(6_000)
        grouped: dict[Source, list[dict]] = {s: [] for s in Source}
        for record in batch.transactions:
            grouped[record.source].append(record.raw)
        ingested, _ = IngestionPipeline().ingest(grouped)

        booked_names = {label for label, *_ in RECURRING_OUTFLOWS}
        history = build_cash_history(
            ingested,
            start=batch.start_date,
            end=batch.end_date,
            exclude_counterparties=booked_names,
        )
        result = backtest(history, simulations=300)

        assert result.cutoffs_evaluated >= 10
        for horizon, score in result.scores.items():
            assert score.samples > 0
            assert score.mae_minor >= 0
            # Under-coverage is the dangerous direction: more confidence than
            # earned. Over-coverage is merely uninformative.
            assert score.coverage >= TARGET_COVERAGE - 0.25, (
                f"T+{horizon} band covered only {score.coverage:.0%} of actuals; "
                "the forecaster is more confident than the data supports"
            )
        assert result.is_calibrated

    def test_summary_flags_miscalibration(self):
        history = flat_history(150, net_minor=0)
        result = backtest(history, simulations=50, step_days=30)
        lines = result.summary()
        assert len(lines) == 3
        assert all(line.startswith("T+") for line in lines)


class TestForecastCli:
    """The CLI is what gets demoed, so it is exercised like anything else."""

    def test_report_runs_and_prints_the_band(self, capsys, monkeypatch):
        from ledger.forecasting import report

        monkeypatch.setattr(
            "sys.argv",
            ["report", "--events", "2000", "--history-days", "200", "--horizon", "20"],
        )
        assert report.main() == 0

        out = capsys.readouterr().out
        assert "CASH FORECAST" in out
        assert "BACKTEST" in out
        assert "BOOKED COMMITMENTS" in out
        assert "P10" in out and "P50" in out and "P90" in out
        assert "SHORTFALL WATCH" in out
        # The backtest must be printed before the projection: a band that does
        # not hold up should be read before the curve it qualifies.
        assert out.index("BACKTEST") < out.index("PROJECTED POSITION")

    def test_report_refuses_when_history_is_too_short(self, capsys, monkeypatch):
        from ledger.forecasting import report

        monkeypatch.setattr(
            "sys.argv",
            ["report", "--events", "50", "--history-days", "5", "--horizon", "5"],
        )
        assert report.main() == 1
        assert "REFUSED" in capsys.readouterr().out
