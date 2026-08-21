"""
The connector contract.

Every data source (Razorpay settlement report, bank statement, gateway log,
internal ledger) is brought into Ledger through a `Connector`. This module
defines the ONE interface all of them implement, so that:

  - the ingestion pipeline depends only on this Protocol, never on a concrete
    source — adding a fifth source touches zero engine code;
  - four connectors can be built in parallel by four people against a frozen
    contract;
  - each connector is independently testable with its own fixtures.

A connector's single job: take raw source records (rows, JSON, whatever the
source speaks) and yield validated `CanonicalTransaction`s. It owns the messy,
source-specific parsing. Everything downstream sees only canonical records.

Idempotency and dedupe are handled centrally by the ingestion pipeline using
`CanonicalTransaction.dedupe_key` — a connector does NOT dedupe itself, so that
the policy lives in one place.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, Protocol, runtime_checkable

from ledger.domain.models import CanonicalTransaction, Source


class ConnectorError(Exception):
    """Raised when a source record cannot be parsed into canonical form.

    Carries enough context to find the offending record in the source without
    dumping PII into logs (see Lead 09's logging policy).
    """

    def __init__(self, source: Source, locator: str, detail: str):
        self.source = source
        self.locator = locator  # e.g. "row 4213" or "settlement_id=pout_x" — NOT PII
        self.detail = detail
        super().__init__(f"[{source.value}] {locator}: {detail}")


@runtime_checkable
class Connector(Protocol):
    """The contract every source connector implements.

    Implementations are expected to be pure with respect to their input: given
    the same raw records they yield the same canonical transactions, so that
    ingestion is reproducible and testable.
    """

    #: Which source this connector produces. Used for routing and dedupe scoping.
    source: Source

    def parse(self, raw_records: Iterable[dict[str, Any]]) -> Iterator[CanonicalTransaction]:
        """Yield validated canonical transactions from raw source records.

        Contract:
          - MUST yield only fully-valid CanonicalTransaction instances (validation
            happens on construction; a record that can't be made valid raises
            ConnectorError rather than yielding a half-object).
          - MUST NOT dedupe — that's the pipeline's job.
          - MUST NOT perform any matching or cross-source logic — a connector sees
            only its own source.
          - SHOULD preserve the original record in `raw` for provenance.
        """
        ...


class BaseConnector:
    """Optional convenience base with the common shape.

    Concrete connectors may subclass this and implement `_to_canonical` for a
    single record, getting the iterator plumbing and error-wrapping for free.
    Using it is not required — the Protocol is the real contract — but it keeps
    the four connectors consistent.
    """

    source: Source

    def parse(self, raw_records: Iterable[dict[str, Any]]) -> Iterator[CanonicalTransaction]:
        for i, record in enumerate(raw_records):
            try:
                txn = self._to_canonical(record)
            except ConnectorError:
                raise
            except Exception as exc:
                raise ConnectorError(
                    source=self.source,
                    locator=f"record #{i}",
                    detail=f"{type(exc).__name__}: {exc}",
                ) from exc
            if txn is not None:
                yield txn

    def _to_canonical(self, record: dict[str, Any]) -> CanonicalTransaction | None:
        """Convert one raw record to canonical form.

        Return None to intentionally skip a record (e.g. a header/footer line in a
        bank statement). Raise ConnectorError for a record that *should* be valid
        but isn't.
        """
        raise NotImplementedError
