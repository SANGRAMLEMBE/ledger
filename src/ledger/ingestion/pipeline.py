"""
The ingestion pipeline — the single door into Ledger.

Raw records from four sources go in; validated, deduplicated
`CanonicalTransaction`s come out, together with a report of exactly what happened
to every input record. Nothing else in the system reads raw source data.

WHY THE POLICY LIVES HERE AND NOT IN THE CONNECTORS
---------------------------------------------------
A connector's job is faithful transcription of one source. Deduplication is a
*cross-record* decision, and if each connector made it independently we would
have four subtly different dedupe policies and no single place to reason about
correctness. So connectors never dedupe; this module does, once, for everyone.

THE ACCOUNTING INVARIANT
------------------------
Every input record ends in exactly one bucket::

    total_in == accepted + deduplicated + rejected + skipped

This is enforced by `IngestionReport.check()` and tested. It matters more than it
looks: a finance system that silently loses a record produces a smaller, cleaner,
completely wrong reconciliation. A dropped record must be impossible, not merely
unlikely.

IDEMPOTENCY
-----------
The pipeline remembers the keys it has already accepted, so re-ingesting the same
file is a no-op rather than a doubling. That is what makes a re-run safe after a
partial failure — and re-running after a partial failure is the normal case in
finance ops, not the exotic one.

The dedupe key is ``(source, external_ref)``. This deliberately catches only the
re-import: the same record arriving twice under the same identifier. It is blind,
by design, to the same *economic* payment settled twice under different
references — that needs different evidence and belongs to anomaly detection.
Conflating the two here would mean either missing real double-payments or
rejecting legitimate distinct records.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from ledger.domain.models import CanonicalTransaction, Source
from ledger.ingestion.connectors.bank import BankStatementConnector
from ledger.ingestion.connectors.base import Connector, ConnectorError
from ledger.ingestion.connectors.gateway import GatewayConnector
from ledger.ingestion.connectors.ledger import LedgerConnector
from ledger.ingestion.connectors.settlement import SettlementConnector

logger = logging.getLogger(__name__)


def default_connectors() -> dict[Source, Connector]:
    """The standard routing table: one connector per source."""
    return {
        Source.BANK: BankStatementConnector(),
        Source.GATEWAY: GatewayConnector(),
        Source.SETTLEMENT: SettlementConnector(),
        Source.LEDGER: LedgerConnector(),
    }


@dataclass(frozen=True)
class Rejection:
    """One record that could not be made canonical.

    `locator` identifies the record without reproducing it — an index or a source
    id, never its contents. A bank narration contains a person's name, so a
    rejection log that echoed the record would leak PII into an operational log.
    """

    source: Source
    locator: str
    detail: str


@dataclass
class IngestionReport:
    """What happened to every input record. The honest accounting."""

    total_in: int = 0
    accepted: int = 0
    deduplicated: int = 0
    rejected: int = 0
    skipped: int = 0
    rejections: list[Rejection] = field(default_factory=list)
    per_source: dict[str, int] = field(default_factory=dict)

    def check(self) -> None:
        """Enforce the accounting invariant. Raises rather than warns.

        A mismatch means a record vanished somewhere in the pipeline. There is no
        safe way to continue from that, so we refuse to.
        """
        counted = self.accepted + self.deduplicated + self.rejected + self.skipped
        if counted != self.total_in:
            raise RuntimeError(
                "ingestion accounting does not balance: "
                f"{self.total_in} in, but accepted={self.accepted} "
                f"deduplicated={self.deduplicated} rejected={self.rejected} "
                f"skipped={self.skipped} sums to {counted}. "
                "A record was lost — refusing to continue."
            )

    def summary(self) -> str:
        return (
            f"ingested {self.accepted:,} of {self.total_in:,} "
            f"({self.deduplicated:,} duplicate, {self.rejected:,} rejected, "
            f"{self.skipped:,} skipped)"
        )


class IngestionPipeline:
    """Routes raw records to their connector, deduplicates, and reports.

    Args:
        connectors: source -> connector routing table. Defaults to all four.
        remember_across_batches: when True (the default) the pipeline keeps the
            keys it has accepted, so ingesting the same file twice is a no-op.
            Set False for a stateless one-shot run.
    """

    def __init__(
        self,
        connectors: Mapping[Source, Connector] | None = None,
        *,
        remember_across_batches: bool = True,
    ) -> None:
        self._connectors: Mapping[Source, Connector] = (
            connectors if connectors is not None else default_connectors()
        )
        self._remember = remember_across_batches
        self._seen: set[tuple[str, str]] = set()

    def reset(self) -> None:
        """Forget accepted keys. Only for tests and deliberate re-processing."""
        self._seen.clear()

    def ingest(
        self, records_by_source: Mapping[Source, Iterable[dict[str, Any]]]
    ) -> tuple[list[CanonicalTransaction], IngestionReport]:
        """Parse, deduplicate and account for every record.

        A record that cannot be parsed is recorded as a rejection and the batch
        continues. One malformed row in a 50k statement must not discard the
        other 49,999 — but it must also never be silently swallowed, which is why
        every rejection lands in the report with a locator.
        """
        report = IngestionReport()
        accepted: list[CanonicalTransaction] = []
        seen = self._seen if self._remember else set()

        for source, records in records_by_source.items():
            connector = self._connectors.get(source)
            if connector is None:
                raise KeyError(
                    f"no connector registered for source {source.value!r}; "
                    f"known sources: {sorted(s.value for s in self._connectors)}"
                )

            for record in records:
                report.total_in += 1
                # Feed one record at a time so a single bad row raises in
                # isolation. Handing the connector the whole iterable would kill
                # the generator on the first error and lose everything after it.
                try:
                    parsed = list(connector.parse([record]))
                except ConnectorError as exc:
                    report.rejected += 1
                    report.rejections.append(
                        Rejection(
                            source=source, locator=exc.locator, detail=exc.detail
                        )
                    )
                    continue

                if not parsed:
                    # The connector deliberately skipped it — a header, footer or
                    # summary row. Not an error, but still accounted for.
                    report.skipped += 1
                    continue

                for txn in parsed:
                    key = txn.dedupe_key
                    if key in seen:
                        report.deduplicated += 1
                        # Debug level, and only the key — never the record.
                        logger.debug(
                            "duplicate dropped: source=%s ref=%s", key[0], key[1]
                        )
                        continue
                    seen.add(key)
                    accepted.append(txn)
                    report.accepted += 1
                    report.per_source[source.value] = (
                        report.per_source.get(source.value, 0) + 1
                    )

        report.check()
        return accepted, report
