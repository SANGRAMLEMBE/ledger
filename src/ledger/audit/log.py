"""
The append-only audit log.

Every automated decision this system makes is written here, with enough context to
reconstruct *why* it was made. That is the difference between a reconciliation you
trust and one you merely accept.

WHY THIS EXISTS ON DAY 3 AND NOT DAY 10
----------------------------------------
Retrofitting an audit log is the classic failure on this track, and the reason is
structural rather than lazy: by the time you add it, the decisions have already
been made and the context that explained them has been discarded. You end up
logging *outcomes* — "these two records matched" — when what an auditor needs is
the *reasoning*: which tier fired, on what evidence, at what confidence, and what
the alternatives were. That information only exists at the moment of the decision.

WHY A FILE AND NOT A DATABASE
-----------------------------
Append-only is a property, not an intention. On a file opened in append mode each
write goes to the end and existing bytes are never addressed, so the guarantee is
enforced by the storage layer rather than by everyone remembering not to run an
UPDATE. A Postgres table can be made append-only with triggers and revoked
permissions, but that is machinery defending a property this gets for free.

It is also demoable — you can read the log on stage — and it needs no
infrastructure, so a laptop with nothing running still produces a full audit
trail. `AuditSink` is the interface, so a database-backed sink can be added later
without touching a single call site.

JSON Lines, one decision per line: appendable without rewriting, readable without
a parser, and streamable without loading the file.

WHAT NEVER GOES IN
------------------
No PII. A bank narration contains a person's name, so records are referenced by
`(source, external_ref)` and by internal id — never by their contents. An audit
log is exactly the file that gets copied around, attached to tickets and shared
with auditors, which makes it the worst possible place to leak a customer name.
"""

from __future__ import annotations

import json
import os
import threading
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any, Protocol


class Decision(str, Enum):
    """The closed set of decisions worth reproducing.

    Deliberately includes the *negative* decisions. "I refused to match these"
    is a decision with consequences — money sat unreconciled because of it — and
    an audit trail that only records successes cannot explain a close.
    """

    BATCH_STARTED = "batch_started"
    RECORD_INGESTED = "record_ingested"
    RECORD_DEDUPLICATED = "record_deduplicated"
    RECORD_REJECTED = "record_rejected"
    MATCH_MADE = "match_made"
    MATCH_REFUSED = "match_refused"
    EXCEPTION_RAISED = "exception_raised"
    BATCH_COMPLETED = "batch_completed"


@dataclass(frozen=True)
class AuditEntry:
    """One decision, reproducible from this line alone.

    `subjects` holds `(source, external_ref)` pairs rather than internal ids
    because those survive re-ingestion — an entry that references only a
    process-local uuid is unreproducible the moment the batch is re-run.
    """

    decision: Decision
    actor: str
    reason: str
    batch_id: str
    subjects: list[str] = field(default_factory=list)
    confidence: float | None = None
    tier: str | None = None
    amount_minor: int | None = None
    currency: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    entry_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())

    def to_json(self) -> str:
        payload: dict[str, Any] = {
            "entry_id": self.entry_id,
            "at": self.at,
            "batch_id": self.batch_id,
            "decision": self.decision.value,
            "actor": self.actor,
            "reason": self.reason,
            "subjects": self.subjects,
        }
        for key, value in (
            ("confidence", self.confidence),
            ("tier", self.tier),
            ("amount_minor", self.amount_minor),
            ("currency", self.currency),
        ):
            if value is not None:
                payload[key] = value
        if self.detail:
            payload["detail"] = self.detail
        return json.dumps(payload, separators=(",", ":"), sort_keys=False)

    @staticmethod
    def from_json(line: str) -> dict[str, Any]:
        parsed: dict[str, Any] = json.loads(line)
        return parsed


class AuditSink(Protocol):
    """Where entries go. One method, so a database sink drops in unchanged."""

    def write(self, entry: AuditEntry) -> None: ...


class AppendOnlyFileLog:
    """Writes entries to a JSON Lines file, append-only and never rewritten.

    The file is opened in append mode for every write. That is slower than
    holding a handle, and it is the right trade: a long-lived handle can be
    truncated by a stray `seek`/`write`, and the audit log is the one file where
    the guarantee matters more than the throughput. At a few thousand entries per
    batch the cost is irrelevant.

    Writes are serialised with a lock so concurrent components cannot interleave
    partial lines — a torn line is an unparseable audit record, which is worse
    than a slow one.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, entry: AuditEntry) -> None:
        line = entry.to_json()
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            # An audit entry that is still in a buffer when the process dies is
            # an audit entry that never existed. Flush and fsync so the record
            # survives a crash between the decision and the next write.
            handle.flush()
            os.fsync(handle.fileno())

    def read(self) -> Iterator[dict[str, Any]]:
        if not self.path.exists():
            return
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                if line.strip():
                    yield AuditEntry.from_json(line)

    def __len__(self) -> int:
        return sum(1 for _ in self.read())


class InMemoryLog:
    """A sink that keeps entries in a list. For tests and dry runs."""

    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def write(self, entry: AuditEntry) -> None:
        self.entries.append(entry)

    def read(self) -> Iterator[dict[str, Any]]:
        for entry in self.entries:
            yield AuditEntry.from_json(entry.to_json())

    def __len__(self) -> int:
        return len(self.entries)


class AuditTrail:
    """The recording interface the rest of the system calls.

    One `AuditTrail` per batch run. It stamps every entry with the same
    `batch_id`, which is what lets an auditor pull one close out of a log holding
    many.
    """

    def __init__(self, sink: AuditSink, batch_id: str | None = None) -> None:
        self.sink = sink
        self.batch_id = batch_id or str(uuid.uuid4())

    def record(
        self,
        decision: Decision,
        *,
        actor: str,
        reason: str,
        subjects: list[str] | None = None,
        confidence: float | None = None,
        tier: str | None = None,
        amount_minor: int | None = None,
        currency: str | None = None,
        **detail: Any,
    ) -> AuditEntry:
        entry = AuditEntry(
            decision=decision,
            actor=actor,
            reason=reason,
            batch_id=self.batch_id,
            subjects=subjects or [],
            confidence=confidence,
            tier=tier,
            amount_minor=amount_minor,
            currency=currency,
            detail=detail,
        )
        self.sink.write(entry)
        return entry
