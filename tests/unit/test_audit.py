"""
Audit log tests.

The claim being defended is "every automated decision is reproducible". Two tests
carry it.

`test_a_match_can_be_reconstructed_from_its_log_line_alone` is the real one: pick
any match from a completed run and rebuild why the system did it — which tier, on
what evidence, at what confidence — from the log and nothing else. If that fails,
the log records outcomes rather than reasoning, which is the failure mode of every
retrofitted audit trail.

`test_refusals_are_recorded_too` guards the other half. A trail that logs only
successes cannot explain a close: money sat unreconciled because of a decision,
and that decision has to be on the record.
"""

from __future__ import annotations

import json

import pytest

from ledger.audit.log import (
    AppendOnlyFileLog,
    AuditEntry,
    AuditTrail,
    Decision,
    InMemoryLog,
)
from ledger.eval.harness import HELD_OUT_SEED, EvalHarness

EVENTS = 1500


@pytest.fixture(scope="module")
def run_with_log(tmp_path_factory):
    path = tmp_path_factory.mktemp("audit") / "decisions.jsonl"
    sink = AppendOnlyFileLog(path)
    result = EvalHarness().run(seed=HELD_OUT_SEED, events=EVENTS, audit_sink=sink)
    return result, sink, path


class TestAppendOnly:
    def test_writes_are_appended_never_rewritten(self, tmp_path):
        log = AppendOnlyFileLog(tmp_path / "a.jsonl")
        trail = AuditTrail(log)
        trail.record(Decision.BATCH_STARTED, actor="t", reason="first")
        first_size = (tmp_path / "a.jsonl").stat().st_size

        trail.record(Decision.BATCH_COMPLETED, actor="t", reason="second")
        second_size = (tmp_path / "a.jsonl").stat().st_size

        assert second_size > first_size, "the file must only grow"
        lines = list(log.read())
        assert [entry["reason"] for entry in lines] == ["first", "second"]

    def test_earlier_entries_are_never_altered(self, tmp_path):
        log = AppendOnlyFileLog(tmp_path / "b.jsonl")
        trail = AuditTrail(log)
        first = trail.record(Decision.MATCH_MADE, actor="t", reason="original")
        for i in range(20):
            trail.record(Decision.MATCH_MADE, actor="t", reason=f"later {i}")

        replayed = next(iter(log.read()))
        assert replayed["entry_id"] == first.entry_id
        assert replayed["reason"] == "original"

    def test_every_line_is_valid_json(self, tmp_path):
        log = AppendOnlyFileLog(tmp_path / "c.jsonl")
        trail = AuditTrail(log)
        trail.record(
            Decision.EXCEPTION_RAISED,
            actor="t",
            reason="quotes \" and \n newlines and unicode Rs",
            subjects=["bank:UTR_1"],
            amount_minor=100,
            currency="INR",
        )
        raw = (tmp_path / "c.jsonl").read_text(encoding="utf-8").strip()
        assert len(raw.splitlines()) == 1, "one decision per line, always"
        json.loads(raw)

    def test_batch_id_groups_one_close(self, tmp_path):
        log = AppendOnlyFileLog(tmp_path / "d.jsonl")
        first = AuditTrail(log)
        second = AuditTrail(log)
        first.record(Decision.BATCH_STARTED, actor="t", reason="run one")
        second.record(Decision.BATCH_STARTED, actor="t", reason="run two")

        batches = {entry["batch_id"] for entry in log.read()}
        assert len(batches) == 2
        assert first.batch_id != second.batch_id


class TestReproducibility:
    def test_a_match_can_be_reconstructed_from_its_log_line_alone(self, run_with_log):
        """The claim: any decision explainable from the log by itself."""
        _, sink, _ = run_with_log
        matches = [
            entry
            for entry in sink.read()
            if entry["decision"] == Decision.MATCH_MADE.value
        ]
        assert matches, "no matches were recorded"

        for entry in matches[:50]:
            # Which records
            assert entry["subjects"], "a match with no subjects explains nothing"
            # On what evidence
            assert len(entry["reason"]) > 15
            # By which component, at what certainty
            assert entry["actor"].startswith("reconciler.")
            assert entry["tier"] in {"exact", "rule", "fuzzy", "ml"}
            assert 0.0 <= entry["confidence"] <= 1.0
            # When, and as part of which close
            assert entry["at"]
            assert entry["batch_id"]

    def test_refusals_are_recorded_too(self, run_with_log):
        """A trail of only successes cannot explain why money sat unreconciled."""
        _, sink, _ = run_with_log
        refusals = [
            entry
            for entry in sink.read()
            if entry["decision"] == Decision.MATCH_REFUSED.value
        ]
        assert refusals, "the cascade refused matches but recorded none"

        for entry in refusals[:20]:
            candidates = entry["detail"]["candidates"]
            assert candidates, "a refusal must say what it considered"
            for candidate in candidates:
                assert candidate["ids"]
                assert "confidence" in candidate
                assert candidate["amount_minor"] > 0

    def test_every_exception_reaches_the_log(self, run_with_log):
        result, sink, _ = run_with_log
        logged = [
            entry
            for entry in sink.read()
            if entry["decision"] == Decision.EXCEPTION_RAISED.value
        ]
        assert len(logged) == result.exceptions.count

        for entry in logged[:50]:
            assert entry["amount_minor"] is not None
            assert entry["detail"]["exception_type"]
            assert entry["detail"]["severity"] in {"low", "medium", "high"}
            assert entry["detail"]["suggested_resolution"]

    def test_batch_opens_and_closes(self, run_with_log):
        _, sink, _ = run_with_log
        decisions = [entry["decision"] for entry in sink.read()]
        assert decisions[0] == Decision.BATCH_STARTED.value
        assert decisions[-1] == Decision.BATCH_COMPLETED.value

    def test_the_whole_run_is_one_batch(self, run_with_log):
        result, sink, _ = run_with_log
        batches = {entry["batch_id"] for entry in sink.read()}
        assert batches == {result.batch_id}


class TestNoPii:
    def test_no_record_contents_reach_the_log(self, run_with_log):
        """The audit log gets copied around and shared. It must stay clean."""
        _, _, path = run_with_log
        text = path.read_text(encoding="utf-8")
        # Bank narrations carry counterparty names; none of that shape may appear.
        assert "NEFT CR" not in text
        assert "NEFT DR" not in text
        assert "Ref No./Cheque No." not in text
        assert "Balance" not in text

    def test_subjects_are_references_not_contents(self, run_with_log):
        _, sink, _ = run_with_log
        checked = 0
        for entry in sink.read():
            for subject in entry["subjects"]:
                assert ":" in subject, "subjects are source:external_ref"
                source = subject.split(":", 1)[0]
                assert source in {"bank", "gateway", "settlement", "ledger"}
                checked += 1
            if checked > 200:
                break
        assert checked > 0


class TestSinks:
    def test_in_memory_sink_matches_file_semantics(self):
        memory = InMemoryLog()
        trail = AuditTrail(memory)
        trail.record(Decision.BATCH_STARTED, actor="t", reason="hello")
        (entry,) = list(memory.read())
        assert entry["reason"] == "hello"
        assert len(memory) == 1

    def test_reading_a_missing_file_is_empty_not_an_error(self, tmp_path):
        log = AppendOnlyFileLog(tmp_path / "nope" / "x.jsonl")
        assert list(log.read()) == []
        assert len(log) == 0

    def test_optional_fields_are_omitted_rather_than_null(self):
        entry = AuditEntry(
            decision=Decision.BATCH_STARTED,
            actor="t",
            reason="r",
            batch_id="b",
        )
        payload = json.loads(entry.to_json())
        assert "confidence" not in payload
        assert "tier" not in payload
        assert "detail" not in payload
