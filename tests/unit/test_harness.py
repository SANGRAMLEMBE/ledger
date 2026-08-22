"""
Harness tests.

The harness is what everyone trusts, so it has to be the thing hardest to fool.
Two tests carry that weight.

`test_publishable_verdict_blocks_a_vacuous_run` proves the vacuity guard actually
fires. The original ground-truth comparison keyed on `txn_id`, matched nothing,
and reported a flawless result — a bug that looks exactly like success. A guard
nobody has seen fail is not a guard, so this one is forced to fail on purpose.

`test_held_out_run_is_publishable` is the gate on the numbers themselves: a false
match, a lost record, or a guessed-at ambiguity makes the run unreportable rather
than merely lower-scoring.
"""

from __future__ import annotations

import pytest

from ledger.eval.harness import DEV_SEED, HELD_OUT_SEED, EvalHarness, tier_counts

# The full benchmark is slow; the properties hold at a smaller size.
EVENTS = 3000


@pytest.fixture(scope="module")
def held_out():
    return EvalHarness().run(seed=HELD_OUT_SEED, events=EVENTS)


@pytest.fixture(scope="module")
def dev():
    return EvalHarness().run(seed=DEV_SEED, events=EVENTS)


class TestTheVerdict:
    def test_held_out_run_is_publishable(self, held_out):
        ok, problems = held_out.is_publishable()
        assert ok, f"held-out run is not reportable: {problems}"

    def test_publishable_verdict_blocks_a_vacuous_run(self, held_out):
        """A broken ground-truth bridge must be refused, not scored."""
        import copy

        broken = copy.copy(held_out)
        broken.bridge_is_sound = False
        ok, problems = broken.is_publishable()
        assert not ok
        assert any("vacuous" in p for p in problems)

    def test_verdict_blocks_a_false_match(self, held_out):
        import copy

        broken = copy.copy(held_out)
        broken.false_matches = 1
        ok, problems = broken.is_publishable()
        assert not ok
        assert any("false match" in p for p in problems)

    def test_verdict_blocks_a_lost_record(self, held_out):
        import copy

        broken = copy.copy(held_out)
        broken.unexplained_records = 1
        ok, problems = broken.is_publishable()
        assert not ok
        assert any("silently lost" in p for p in problems)

    def test_verdict_blocks_a_guessed_ambiguity(self, held_out):
        import copy

        broken = copy.copy(held_out)
        broken.ambiguous_held = broken.ambiguous_planted - 1
        ok, problems = broken.is_publishable()
        assert not ok
        assert any("guessing" in p for p in problems)


class TestCorrectness:
    def test_bridge_connects(self, held_out):
        """Without this every other assertion in the file passes vacuously."""
        assert held_out.bridge_is_sound

    def test_no_false_matches(self, held_out):
        assert held_out.false_matches == 0

    def test_nothing_is_unexplained(self, held_out):
        assert held_out.unexplained_records == 0

    def test_every_planted_break_surfaces(self, held_out):
        assert held_out.breaks_planted > 0
        assert held_out.break_recall == 1.0

    def test_ambiguity_is_never_guessed(self, held_out):
        assert held_out.ambiguous_planted > 0
        assert held_out.ambiguous_held == held_out.ambiguous_planted


class TestReportedNumbers:
    def test_match_rate_is_measured_against_ground_truth(self, held_out):
        assert 0.0 < held_out.match_rate <= 1.0
        assert held_out.events_expected_match > 0
        assert held_out.events_reconciled <= held_out.events_expected_match

    def test_throughput_is_positive(self, held_out):
        assert held_out.throughput > 0

    def test_exceptions_carry_money_at_risk(self, held_out):
        assert held_out.exceptions.count > 0
        assert held_out.exceptions.total_at_risk_minor > 0

    def test_deterministic_tiers_carry_the_batch(self, held_out):
        counts = tier_counts(held_out)
        assert counts["exact"] > counts["rule"]
        # No model is wired in yet; if these ever become non-zero without a model
        # tier existing, something is mislabelling its matches.
        assert counts["fuzzy"] == 0
        assert counts["ml"] == 0


class TestReproducibility:
    def test_same_seed_gives_identical_numbers(self):
        """A metric that moves between runs is not a metric."""
        first = EvalHarness().run(seed=HELD_OUT_SEED, events=1000)
        second = EvalHarness().run(seed=HELD_OUT_SEED, events=1000)
        assert first.match_rate == second.match_rate
        assert first.false_matches == second.false_matches
        assert first.exceptions.count == second.exceptions.count
        assert (
            first.exceptions.total_at_risk_minor
            == second.exceptions.total_at_risk_minor
        )

    def test_dev_and_held_out_seeds_are_different_data(self, dev, held_out):
        assert dev.ingestion.total_in != held_out.ingestion.total_in

    def test_dev_seed_also_passes(self, dev):
        """Held-out performance should not depend on which seed was tuned on."""
        ok, problems = dev.is_publishable()
        assert ok, problems


class TestReportCli:
    """The CLI is what runs on stage, so it gets exercised like anything else."""

    def test_report_runs_and_exits_zero_on_a_clean_run(self, capsys, monkeypatch):
        from ledger.eval import report

        monkeypatch.setattr(
            "sys.argv", ["report", "--seed", str(HELD_OUT_SEED), "--events", "800"]
        )
        assert report.main() == 0

        out = capsys.readouterr().out
        assert "THE THREE NUMBERS" in out
        assert "match rate" in out
        assert "false matches" in out
        assert "VERDICT: publishable." in out
        assert "HELD OUT" in out

    def test_dev_seed_is_labelled_as_not_for_reporting(self, capsys, monkeypatch):
        from ledger.eval import report

        monkeypatch.setattr(
            "sys.argv", ["report", "--seed", str(DEV_SEED), "--events", "800"]
        )
        report.main()
        out = capsys.readouterr().out
        assert "not for reporting" in out
