"""
Canonical domain model for Ledger.

This module is the single source of truth for what a "transaction" means inside
Ledger, regardless of which external system it came from. Every connector
normalises its raw payload into a `CanonicalTransaction`; every downstream
component (matching, exceptions, forecasting, API) speaks *only* this language.

DESIGN RULES (do not violate without a schema-review from Lead 02 + Lead 09):

1.  MONEY IS NEVER A FLOAT.
    All monetary amounts are stored as integer minor units (paise for INR) in an
    `int` field named `*_minor`. Floating-point arithmetic on money is a
    correctness bug: 0.1 + 0.2 != 0.3 in IEEE-754, and reconciliation lives or
    dies on exact equality. We convert to/from decimal *only* at the display edge.

2.  EVERY CONTROLLED VOCABULARY IS AN ENUM.
    `source`, `status`, `direction`, `exception_type` etc. are enums, not free
    strings. This makes invalid states unrepresentable and gives us exhaustive
    matching in the engine.

3.  THE RAW PAYLOAD IS PRESERVED, UNTOUCHED.
    `raw` holds the original source record verbatim. Auditors and debuggers must
    always be able to see exactly what came in, byte-for-byte in meaning. We never
    discard provenance.

4.  IDENTITY IS ASSIGNED BY US, AT INGESTION.
    `txn_id` is a Ledger-internal UUID. `external_ref` is the source's own id and
    is NOT assumed unique across sources. The pair (source, external_ref) is the
    natural key we dedupe on.

5.  VALIDATION HAPPENS AT THE BOUNDARY.
    Pydantic validates on construction. A record that cannot be made canonical is
    rejected at ingestion with a typed error — it never enters the pipeline as a
    half-valid object that blows up three layers deep.
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

# --------------------------------------------------------------------------- #
# Controlled vocabularies
# --------------------------------------------------------------------------- #


class Source(str, enum.Enum):
    """Which external system a record originated from.

    Adding a new source is a one-line change here plus a new connector — the
    engine never needs to know the concrete list, only that it's one of these.
    """

    GATEWAY = "gateway"          # payment gateway capture/txn log
    SETTLEMENT = "settlement"    # gateway settlement report (payouts)
    BANK = "bank"                # bank statement (UTR / NEFT / IMPS lines)
    LEDGER = "ledger"            # internal GL / order ledger


class Direction(str, enum.Enum):
    """Money flow relative to the merchant."""

    INBOUND = "inbound"    # merchant receives (capture, settlement credit)
    OUTBOUND = "outbound"  # merchant pays (refund, payout, fee debit)


class TxnStatus(str, enum.Enum):
    """Lifecycle state of the underlying transaction, normalised across sources."""

    CAPTURED = "captured"
    SETTLED = "settled"
    REFUNDED = "refunded"
    PARTIALLY_REFUNDED = "partially_refunded"
    FAILED = "failed"
    REVERSED = "reversed"
    PENDING = "pending"


class Currency(str, enum.Enum):
    """ISO 4217. Kept as an enum so an unknown currency is rejected at the edge.

    Extend deliberately — every currency here must have a known minor-unit
    exponent in `MINOR_UNIT_EXPONENT` below.
    """

    INR = "INR"
    USD = "USD"
    EUR = "EUR"
    GBP = "GBP"


# Number of decimal places in the currency's minor unit.
# INR -> paise (2), USD -> cents (2). JPY would be 0. This table is the ONLY
# place that knows how to convert between major and minor units.
MINOR_UNIT_EXPONENT: dict[Currency, int] = {
    Currency.INR: 2,
    Currency.USD: 2,
    Currency.EUR: 2,
    Currency.GBP: 2,
}


# --------------------------------------------------------------------------- #
# Money helpers — the ONLY sanctioned way to move between major/minor units
# --------------------------------------------------------------------------- #


def to_minor(amount_major: Decimal | str, currency: Currency) -> int:
    """Convert a human/major amount (e.g. Decimal('40000.00')) to minor units.

    Uses Decimal throughout — never float — then returns a plain int. Raises if
    the amount has more precision than the currency allows, because silently
    rounding money is how you lose a paisa 50,000 times.
    """
    exp = MINOR_UNIT_EXPONENT[currency]
    d = Decimal(amount_major)
    scaled = d.scaleb(exp)
    if scaled % 1 != 0:
        raise ValueError(
            f"Amount {amount_major} has finer precision than {currency.value} "
            f"allows ({exp} dp); refusing to round silently."
        )
    return int(scaled)


def to_major(amount_minor: int, currency: Currency) -> Decimal:
    """Convert minor units back to a Decimal major amount for display only."""
    exp = MINOR_UNIT_EXPONENT[currency]
    return (Decimal(amount_minor) / Decimal(10) ** exp).quantize(
        Decimal(1).scaleb(-exp)
    )


def _utcnow() -> datetime:
    return datetime.now(UTC)


def _new_txn_id() -> str:
    return str(uuid.uuid4())


# --------------------------------------------------------------------------- #
# The canonical transaction
# --------------------------------------------------------------------------- #


class CanonicalTransaction(BaseModel):
    """One transaction, normalised. The atom of the entire system.

    Every field below is either (a) required and meaningful for every source, or
    (b) optional because some sources genuinely don't carry it. We do NOT add
    source-specific fields here — those live in `raw`. Keeping this model lean is
    what keeps matching source-agnostic.
    """

    model_config = ConfigDict(
        frozen=True,             # canonical records are immutable once created
        use_enum_values=False,   # keep enum members, not their raw values
        extra="forbid",          # a typo'd field name is a bug, not a silent drop
    )

    # --- identity -----------------------------------------------------------
    txn_id: str = Field(default_factory=_new_txn_id, description="Ledger-internal UUID.")
    source: Source = Field(..., description="Originating system.")
    external_ref: str = Field(
        ...,
        min_length=1,
        description="The source's own id (order_id, UTR, voucher no.). "
        "Unique within a source, NOT across sources.",
    )

    # --- money --------------------------------------------------------------
    amount_minor: int = Field(
        ...,
        description="Signed is disallowed; direction carries the sign. Minor units.",
    )
    currency: Currency = Field(...)
    fee_minor: int = Field(
        default=0,
        ge=0,
        description="Gateway fee / bank charge in minor units, if the source reports it.",
    )
    tax_minor: int = Field(
        default=0,
        ge=0,
        description="Tax component (e.g. GST on fee) in minor units, if reported.",
    )
    direction: Direction = Field(...)

    # --- time ---------------------------------------------------------------
    value_date: date = Field(..., description="The date money actually moved.")
    posted_at: datetime = Field(
        ..., description="When the source created this record (tz-aware UTC)."
    )

    # --- context ------------------------------------------------------------
    status: TxnStatus = Field(...)
    counterparty: str | None = Field(
        default=None, description="Normalised counterparty name/handle, if known."
    )
    # Links a record to its economic parent (e.g. a refund -> original capture,
    # a settled line -> its order). Populated by connectors where the source
    # exposes it; used by the matcher to resolve one-to-many and reversals.
    parent_ref: str | None = Field(default=None)
    # Links records that MOVED AS ONE PAYOUT (a batch/settlement grouping).
    #
    # Why this exists, and why it is separate from `parent_ref`: a settlement line
    # has two distinct links — *up* to the order it settles (`parent_ref`) and
    # *sideways* to the other lines that landed in the same bank credit
    # (`group_ref`). One field cannot carry both. Without this, there is NO
    # deterministic path from a bank deposit to the settlement lines that compose
    # it, and every batch settlement would fall through to the fuzzy/ML tiers —
    # which is exactly the O(n^2) subset-sum problem we must not need to solve.
    #
    # Convention: settlement lines carry the payout's UTR/settlement id here; the
    # bank line carries that same value as its `external_ref`. When the source
    # genuinely omits it (truncated bank narration — common in the real world),
    # this is None and the grouping becomes a real ambiguity for the exception
    # engine to surface rather than guess.
    group_ref: str | None = Field(default=None)

    # --- provenance ---------------------------------------------------------
    raw: dict[str, Any] = Field(
        default_factory=dict,
        description="Original source payload, verbatim. Never read by the engine "
        "for matching — audit and debugging only.",
    )
    ingested_at: datetime = Field(default_factory=_utcnow)

    # --- validators ---------------------------------------------------------

    @field_validator("amount_minor")
    @classmethod
    def _amount_non_negative(cls, v: int) -> int:
        # Sign is carried by `direction`, so the magnitude is always >= 0.
        # A negative magnitude means a connector bug — fail loud, at the edge.
        if v < 0:
            raise ValueError(
                "amount_minor must be non-negative; use `direction` for sign."
            )
        return v

    @field_validator("posted_at")
    @classmethod
    def _posted_at_is_tz_aware(cls, v: datetime) -> datetime:
        if v.tzinfo is None:
            raise ValueError(
                "posted_at must be timezone-aware. Naive datetimes cause "
                "off-by-hours matching bugs across sources."
            )
        return v.astimezone(UTC)

    @model_validator(mode="after")
    def _fee_not_exceeding_amount(self) -> CanonicalTransaction:
        # A fee larger than the transaction is almost always a parsing error
        # (e.g. columns swapped). Catch it here, not in reconciliation.
        if self.fee_minor > self.amount_minor and self.amount_minor > 0:
            raise ValueError(
                f"fee_minor ({self.fee_minor}) exceeds amount_minor "
                f"({self.amount_minor}) for {self.source.value}:{self.external_ref}; "
                "likely a connector parsing error."
            )
        return self

    # --- derived / convenience (display edge only) --------------------------

    @property
    def net_minor(self) -> int:
        """Amount net of fee and tax, in minor units. Inbound money you keep."""
        return self.amount_minor - self.fee_minor - self.tax_minor

    @property
    def dedupe_key(self) -> tuple[str, str]:
        """Natural key for idempotent ingestion: (source, external_ref)."""
        return (self.source.value, self.external_ref)

    def amount_major(self) -> Decimal:
        """Human-readable amount. For display/logging ONLY, never for matching."""
        return to_major(self.amount_minor, self.currency)


# --------------------------------------------------------------------------- #
# Match records — the output of reconciliation
# --------------------------------------------------------------------------- #


class MatchTier(str, enum.Enum):
    """Which tier of the cascade produced a match. Ordered by certainty."""

    EXACT = "exact"            # Tier 0: deterministic, indexed
    RULE = "rule"              # Tier 1: rule-based near-match (fee/date tolerance)
    FUZZY = "fuzzy"            # Tier 2: probabilistic / blocking-key similarity
    ML = "ml"                  # Tier 3: classifier on the ambiguous tail


class MatchResult(BaseModel):
    """A confirmed correspondence between records from two (or more) sources.

    Supports one-to-many (a settlement against several ledger lines) via lists.
    Carries an explainable `reason` and a `confidence` so no money decision is a
    black box.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    match_id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    left_ids: list[str] = Field(..., min_length=1, description="txn_ids on one side.")
    right_ids: list[str] = Field(..., min_length=1, description="txn_ids on the other.")
    tier: MatchTier
    confidence: float = Field(..., ge=0.0, le=1.0)
    # Human-readable justification, e.g. "exact on (external_ref, amount, currency)"
    # or "ml: amount_delta=0, date_delta=1d, counterparty_sim=0.92".
    reason: str = Field(..., min_length=1)
    matched_at: datetime = Field(default_factory=_utcnow)

    @model_validator(mode="after")
    def _exact_must_be_confident(self) -> MatchResult:
        # An "exact" match with low confidence is a contradiction in terms and
        # signals a bug in the tier that emitted it.
        if self.tier == MatchTier.EXACT and self.confidence < 0.999:
            raise ValueError("EXACT tier must carry confidence ~1.0.")
        return self

    @property
    def is_one_to_many(self) -> bool:
        return len(self.left_ids) > 1 or len(self.right_ids) > 1
