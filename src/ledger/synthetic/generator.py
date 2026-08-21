"""
Synthetic transaction generator with ground truth.

This is the foundation of every metric we report. It generates a coherent set of
transactions across all four sources AND the ground-truth answer key: which
records truly match which, and which records are genuine, deliberate breaks.

Without a ground-truth generator you cannot compute match rate, precision, or
recall — you can only eyeball a demo. That is the difference between "we think
it's ~90%" and "94.2% on a held-out set of 50,000". This file is why we can say
the second sentence.

The generator plants a realistic MIX of cases, each tagged so the eval harness
knows what the right answer was:

  CLEAN            - flows cleanly gateway -> settlement -> bank, recorded in the
                     ledger. The settlement carries the payout UTR, so there is a
                     deterministic path all the way to the bank credit.
  FEE_LAG          - same, but settlement applies a gateway fee and the bank
                     credit lands T+1/T+2. Tests the rule tier.
  ONE_TO_MANY      - several orders settle in ONE bank deposit, and the settlement
                     report DOES record the UTR. Deterministically groupable.
  ONE_TO_MANY_NO_UTR - the same batch settlement, but the settlement report omits
                     the UTR and the bank narration is truncated. There is no
                     deterministic link: the engine must infer the grouping, and
                     when several subsets plausibly sum to the deposit it must
                     REFUSE and raise an exception. **This is the 2 AM case the
                     entire demo is built around.**
  PARTIAL_REFUND   - an order partially refunded; nets against the original.
  FX               - a cross-currency capture settled at a recorded rate.
  DUPLICATE_SAME_REF   - the same record imported twice with the SAME external_ref.
                     Ingestion dedupe catches this; it must never reach matching
                     and must NOT be counted as an exception.
  DUPLICATE_DOUBLE_PAY - a genuine double settlement: same economics, DIFFERENT
                     external_ref. Dedupe CANNOT catch it. Only anomaly detection
                     can, and it must surface as a real exception with money at
                     risk.
  BREAK_NO_BANK    - a settlement with NO corresponding bank credit (money at risk).
  BREAK_NO_LEDGER  - a bank credit with NO ledger entry.
  OUTFLOW_VENDOR   - a variable vendor payment going out. Reconciles ledger<->bank,
                     and is the stochastic spend the forecaster has to model.

On top of the random mix, the generator lays down RECURRING outflows (payroll,
rent) on a fixed monthly schedule, and a set of FUTURE `ScheduledOutflow`s. Those
two exist for the cash forecaster: recurring history is what the statistical layer
learns from, and the future bookings are the "deterministic scaffold" that is
booked rather than forecast.

WHY THE RAW PAYLOADS MATTER
---------------------------
Every canonical record carries the raw source payload that produced it (see
`raw_shapes.py`). This keeps the four connectors on the measured path: they parse
the same batch the metrics are computed on, instead of being tested against
hand-invented fixtures and silently bypassed end-to-end.

Determinism: seeded RNG so a given seed always yields the identical dataset and
answer key. Reproducible datasets are non-negotiable for reproducible metrics.
Metrics are reported on a HELD-OUT seed the engine was never tuned against.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum
from typing import ClassVar

from ledger.domain.models import (
    CanonicalTransaction,
    Currency,
    Direction,
    Source,
    TxnStatus,
    to_major,
    to_minor,
)
from ledger.synthetic.raw_shapes import (
    bank_raw,
    gateway_raw,
    indian_grouped,
    ledger_raw,
    settlement_raw,
)


class CaseType(str, Enum):
    CLEAN = "clean"
    FEE_LAG = "fee_lag"
    ONE_TO_MANY = "one_to_many"
    ONE_TO_MANY_NO_UTR = "one_to_many_no_utr"
    PARTIAL_REFUND = "partial_refund"
    FX = "fx"
    DUPLICATE_SAME_REF = "duplicate_same_ref"
    DUPLICATE_DOUBLE_PAY = "duplicate_double_pay"
    BREAK_NO_BANK = "break_no_bank"
    BREAK_NO_LEDGER = "break_no_ledger"
    OUTFLOW_VENDOR = "outflow_vendor"
    OUTFLOW_RECURRING = "outflow_recurring"


class ExpectedHandling(str, Enum):
    """What CORRECT behaviour looks like for a planted case.

    This exists because "did it match?" is the wrong question for two of our
    cases. A duplicate caught by ingestion dedupe is handled *correctly* and must
    never be scored as a missed match or as an exception — scoring it either way
    would punish the system for being right. The eval harness reads this field to
    know which of the three outcomes it should be checking for.
    """

    MATCHED = "matched"
    # Should reconcile into a MatchResult.

    DEDUPED_AT_INGESTION = "deduped_at_ingestion"
    # Should be dropped by the pipeline on (source, external_ref) and counted in
    # the ingestion report. NOT an exception, NOT a match.

    EXCEPTION = "exception"
    # Should surface as a typed ReconciliationException. `expected_exception`
    # names which type.


# Distribution of case types in a generated batch. Tuned to look like a real
# merchant's month: mostly clean, a meaningful tail of hard cases and breaks.
# OUTFLOW_RECURRING is absent here deliberately — recurring outflows are laid down
# on a fixed schedule, not sampled randomly, because a forecaster learning a
# monthly payroll pattern needs it to actually fall on the same day each month.
DEFAULT_MIX: dict[CaseType, float] = {
    CaseType.CLEAN: 0.53,
    CaseType.FEE_LAG: 0.17,
    CaseType.ONE_TO_MANY: 0.05,
    CaseType.ONE_TO_MANY_NO_UTR: 0.03,
    CaseType.PARTIAL_REFUND: 0.05,
    CaseType.FX: 0.03,
    CaseType.DUPLICATE_SAME_REF: 0.015,
    CaseType.DUPLICATE_DOUBLE_PAY: 0.015,
    CaseType.BREAK_NO_BANK: 0.035,
    CaseType.BREAK_NO_LEDGER: 0.025,
    CaseType.OUTFLOW_VENDOR: 0.05,
}


@dataclass
class GroundTruthEntry:
    """The answer key for one economic event: which txn_ids truly correspond."""

    case_type: CaseType
    ledger_ids: list[str] = field(default_factory=list)
    gateway_ids: list[str] = field(default_factory=list)
    settlement_ids: list[str] = field(default_factory=list)
    bank_ids: list[str] = field(default_factory=list)
    expected_handling: ExpectedHandling = ExpectedHandling.MATCHED
    # For EXCEPTION handling: which typed exception should be raised.
    expected_exception: str | None = None
    note: str = ""

    @property
    def all_ids(self) -> list[str]:
        return (
            self.ledger_ids
            + self.gateway_ids
            + self.settlement_ids
            + self.bank_ids
        )

    @property
    def should_match(self) -> bool:
        """True if this event should reconcile into a match."""
        return self.expected_handling == ExpectedHandling.MATCHED

    @property
    def is_true_break(self) -> bool:
        """True if this event should surface as an exception (money at risk)."""
        return self.expected_handling == ExpectedHandling.EXCEPTION


@dataclass
class ScheduledOutflow:
    """A known FUTURE outflow — the deterministic scaffold for the forecaster.

    These are not forecast, they are *booked*: payroll on the 1st, rent on the
    5th, an approved vendor invoice with a due date. The forecaster's statistical
    layer only models the stochastic remainder on top of these.
    """

    label: str
    due_date: date
    amount_minor: int
    currency: Currency
    counterparty: str
    # "booked"   — contractually certain (payroll, rent, signed invoice)
    # "expected" — probable but not committed
    certainty: str = "booked"


@dataclass
class SyntheticBatch:
    """A generated dataset plus its ground-truth answer key."""

    transactions: list[CanonicalTransaction]
    ground_truth: list[GroundTruthEntry]
    seed: int
    # Window the history covers. The forecaster treats `end_date` as "today".
    start_date: date
    end_date: date
    # Known future outflows beyond `end_date` (forecaster scaffold).
    scheduled_outflows: list[ScheduledOutflow] = field(default_factory=list)

    def by_source(self, source: Source) -> list[CanonicalTransaction]:
        return [t for t in self.transactions if t.source == source]

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for gt in self.ground_truth:
            counts[gt.case_type.value] = counts.get(gt.case_type.value, 0) + 1
        counts["_transactions_total"] = len(self.transactions)
        counts["_events_total"] = len(self.ground_truth)
        counts["_true_breaks"] = sum(
            1 for gt in self.ground_truth if gt.is_true_break
        )
        counts["_deduped_expected"] = sum(
            1
            for gt in self.ground_truth
            if gt.expected_handling == ExpectedHandling.DEDUPED_AT_INGESTION
        )
        counts["_scheduled_outflows"] = len(self.scheduled_outflows)
        counts["_history_days"] = (self.end_date - self.start_date).days + 1
        return counts


# FX rates the generator "records" — the engine is given the same table so a
# correct FX match is possible within tolerance.
FX_RATES: dict[tuple[Currency, Currency], Decimal] = {
    (Currency.USD, Currency.INR): Decimal("83.20"),
    (Currency.EUR, Currency.INR): Decimal("90.10"),
    (Currency.GBP, Currency.INR): Decimal("105.40"),
}

# Recurring outflows laid down monthly. (label, day-of-month, amount, certainty)
RECURRING_OUTFLOWS: list[tuple[str, int, str, str]] = [
    ("Payroll", 1, "1850000.00", "booked"),
    ("Office rent", 5, "225000.00", "booked"),
    ("Cloud infrastructure", 12, "48000.00", "expected"),
]


class SyntheticGenerator:
    """Generates a coherent multi-source batch with a ground-truth answer key."""

    def __init__(
        self,
        seed: int = 42,
        mix: dict[CaseType, float] | None = None,
        history_days: int = 30,
        end_date: date | None = None,
    ):
        """
        Args:
            seed: RNG seed. Same seed => byte-identical batch and answer key.
            mix: case-type distribution. Defaults to `DEFAULT_MIX`.
            history_days: how many days of history to span. **Density matters:**
                `n_events` spread over 30 days is a dense month-end close (the
                reconciliation benchmark, and the demo narrative); the same count
                over 365 days is sparse. Reconciliation metrics use the dense
                window; the cash forecaster needs a long one (365) so it has
                enough history to backtest T+7/T+14/T+30 against.
            end_date: the last day of history — "today" from the forecaster's
                point of view. Defaults to 2026-08-30, matching the demo's
                August month-end close.
        """
        self.seed = seed
        self.rng = random.Random(seed)
        self.mix = mix or DEFAULT_MIX
        self.history_days = history_days
        self._end_date = end_date or date(2026, 8, 30)
        self._base_date = self._end_date - timedelta(days=history_days - 1)

    # -- small helpers -------------------------------------------------------

    def _amount(self, lo: int = 500, hi: int = 200_000) -> Decimal:
        """A plausible order amount in major units, 2dp."""
        rupees = self.rng.randint(lo, hi)
        paise = self.rng.choice([0, 0, 0, 50, 99, 25])  # mostly round
        return Decimal(f"{rupees}.{paise:02d}")

    def _fee(self, amount: Decimal) -> Decimal:
        """A ~2% gateway fee, rounded to paise."""
        return (amount * Decimal("0.02")).quantize(Decimal("0.01"))

    def _day(self, offset: int) -> date:
        return self._base_date + timedelta(days=offset)

    def _ts(self, d: date) -> datetime:
        """A timestamp with a real time-of-day, for sources that record one."""
        return datetime(
            d.year, d.month, d.day,
            self.rng.randint(0, 23), self.rng.randint(0, 59),
            tzinfo=UTC,
        )

    def _date_only_ts(self, d: date) -> datetime:
        """Midnight UTC, for sources that record a DATE and no time.

        Bank statement lines and GL exports carry `02/08/2026` — there is no
        time-of-day in the source at all. If the generator invented one, no
        connector could ever recover it and the round-trip property would be
        unsatisfiable for two of the four sources. Midnight is the honest
        representation of "this source only knows the day".
        """
        return datetime(d.year, d.month, d.day, tzinfo=UTC)

    def _ref(self, prefix: str) -> str:
        return f"{prefix}_{uuid.UUID(int=self.rng.getrandbits(128)).hex[:12]}"

    def _counterparty(self) -> str:
        names = ["Acme Retail", "Blue Mart", "Ganesh Traders", "Nova Foods",
                 "Star Textiles", "Metro Cabs", "Sunrise Pharma", "Zen Books"]
        return self.rng.choice(names)

    def _vendor(self) -> str:
        names = ["Kraft Supplies", "Indus Logistics", "Vertex Print",
                 "Orbit Facilities", "Trinity Legal"]
        return self.rng.choice(names)

    # -- record builders (canonical + its raw payload) ------------------------
    # Each builder attaches the raw source payload that would have produced this
    # canonical record, so connectors can be tested by round-trip against it.

    def _make_ledger(
        self, *, ref: str, amount: Decimal, d: date, cp: str | None,
        status: TxnStatus, direction: Direction = Direction.INBOUND,
        currency: Currency = Currency.INR, narration: str = "Sales invoice",
        account: str = "1200 - Accounts Receivable",
    ) -> CanonicalTransaction:
        minor = to_minor(amount, currency)
        return CanonicalTransaction(
            source=Source.LEDGER, external_ref=ref,
            amount_minor=minor, currency=currency,
            direction=direction, value_date=d, posted_at=self._date_only_ts(d),
            status=status, counterparty=cp,
            raw=ledger_raw(
                voucher_no=ref, gl_date=d, party=cp, amount_minor=minor,
                currency=currency, is_debit=direction == Direction.INBOUND,
                narration=narration, status=status.value, account=account,
            ),
        )

    def _make_gateway(
        self, *, ref: str, order_ref: str | None, amount: Decimal, d: date,
        cp: str | None, status: TxnStatus, direction: Direction = Direction.INBOUND,
        fee: Decimal = Decimal("0.00"), currency: Currency = Currency.INR,
    ) -> CanonicalTransaction:
        minor = to_minor(amount, currency)
        fee_minor = to_minor(fee, currency)
        ts = self._ts(d)
        return CanonicalTransaction(
            source=Source.GATEWAY, external_ref=ref,
            amount_minor=minor, fee_minor=fee_minor, currency=currency,
            direction=direction, value_date=d, posted_at=ts,
            status=status, counterparty=cp, parent_ref=order_ref,
            raw=gateway_raw(
                payment_id=ref, order_id=order_ref, amount_minor=minor,
                currency=currency, fee_minor=fee_minor, tax_minor=0,
                status=status.value, created_at=ts, contact=cp,
            ),
        )

    def _make_settlement(
        self, *, ref: str, order_ref: str | None, net: Decimal, fee: Decimal,
        d: date, cp: str | None, utr: str | None,
        currency: Currency = Currency.INR,
    ) -> CanonicalTransaction:
        net_minor = to_minor(net, currency)
        fee_minor = to_minor(fee, currency)
        ts = self._ts(d)
        return CanonicalTransaction(
            source=Source.SETTLEMENT, external_ref=ref,
            amount_minor=net_minor, fee_minor=fee_minor, currency=currency,
            direction=Direction.INBOUND, value_date=d, posted_at=ts,
            status=TxnStatus.SETTLED, counterparty=cp,
            parent_ref=order_ref,
            # The sideways link to the bank credit. None when the report omits
            # it — that omission is what creates the 2 AM ambiguity.
            group_ref=utr,
            raw=settlement_raw(
                payout_id=ref, settlement_id=self._ref("setl"),
                order_id=order_ref, net_minor=net_minor, fee_minor=fee_minor,
                tax_minor=0, currency=currency, utr=utr, settled_at=ts,
                counterparty=cp,
            ),
        )

    def _make_bank(
        self, *, utr: str, amount: Decimal, d: date, cp: str | None,
        direction: Direction = Direction.INBOUND,
        currency: Currency = Currency.INR, truncated_narration: bool = False,
    ) -> CanonicalTransaction:
        minor = to_minor(amount, currency)
        is_credit = direction == Direction.INBOUND
        party = (cp or "UNKNOWN").upper()
        narration = (
            f"{'NEFT CR' if is_credit else 'NEFT DR'}-HDFC0000123-{party}"
            if truncated_narration
            else f"{'NEFT CR' if is_credit else 'NEFT DR'}-HDFC0000123-{party}-{utr}"
        )
        return CanonicalTransaction(
            source=Source.BANK, external_ref=utr,
            amount_minor=minor, currency=currency,
            direction=direction, value_date=d, posted_at=self._date_only_ts(d),
            status=TxnStatus.SETTLED,
            # The bank only ever says "ACME RETAIL" — uppercase, in a narration
            # blob. Storing the nicely-cased name here would be recording
            # something the source never told us, and would hide the case/format
            # mismatch that the fuzzy tier legitimately has to solve.
            counterparty=party,
            raw=bank_raw(
                utr=utr, amount_minor=minor, currency=currency,
                is_credit=is_credit, txn_date=d, value_date=d,
                narration=narration,
                running_balance_minor=0,  # rewritten by _apply_running_balance
            ),
        )

    # -- case builders -------------------------------------------------------
    # Each returns (transactions, ground_truth_entry). They share a lot of
    # structure; kept explicit rather than over-abstracted so each case is
    # readable and independently tweakable.

    def _clean(self, day: int) -> tuple[list[CanonicalTransaction], GroundTruthEntry]:
        amt = self._amount()
        cp = self._counterparty()
        d = self._day(day)
        order_ref = self._ref("order")
        utr = self._ref("UTR")

        led = self._make_ledger(
            ref=order_ref, amount=amt, d=d, cp=cp, status=TxnStatus.CAPTURED
        )
        gw = self._make_gateway(
            ref=self._ref("pay"), order_ref=order_ref, amount=amt, d=d, cp=cp,
            status=TxnStatus.CAPTURED,
        )
        st = self._make_settlement(
            ref=self._ref("pout"), order_ref=order_ref, net=amt,
            fee=Decimal("0.00"), d=d, cp=cp, utr=utr,
        )
        bk = self._make_bank(utr=utr, amount=amt, d=d, cp=cp)

        gt = GroundTruthEntry(
            case_type=CaseType.CLEAN,
            ledger_ids=[led.txn_id], gateway_ids=[gw.txn_id],
            settlement_ids=[st.txn_id], bank_ids=[bk.txn_id],
            note="clean flow; UTR present so bank link is deterministic",
        )
        return [led, gw, st, bk], gt

    def _fee_lag(self, day: int) -> tuple[list[CanonicalTransaction], GroundTruthEntry]:
        amt = self._amount()
        fee = self._fee(amt)
        net = amt - fee
        cp = self._counterparty()
        d = self._day(day)
        settle_d = self._day(day + self.rng.choice([1, 2]))  # T+1/T+2
        order_ref = self._ref("order")
        utr = self._ref("UTR")

        led = self._make_ledger(
            ref=order_ref, amount=amt, d=d, cp=cp, status=TxnStatus.CAPTURED
        )
        st = self._make_settlement(
            ref=self._ref("pout"), order_ref=order_ref, net=net, fee=fee,
            d=settle_d, cp=cp, utr=utr,
        )
        bk = self._make_bank(utr=utr, amount=net, d=settle_d, cp=cp)

        gt = GroundTruthEntry(
            case_type=CaseType.FEE_LAG,
            ledger_ids=[led.txn_id], settlement_ids=[st.txn_id],
            bank_ids=[bk.txn_id],
            note="fee-adjusted amount, T+1/T+2 settlement — rule tier",
        )
        return [led, st, bk], gt

    def _one_to_many_impl(
        self, day: int, *, with_utr: bool
    ) -> tuple[list[CanonicalTransaction], GroundTruthEntry]:
        """Several orders settling in ONE bank deposit.

        `with_utr=True`  -> the settlement report records the payout UTR, so the
                            grouping is deterministic (Tier 0 resolves it).
        `with_utr=False` -> the UTR is missing and the bank narration truncated.
                            The engine must infer which subset of settlements sums
                            to the deposit; where several subsets plausibly fit it
                            must refuse and raise an exception. THE 2 AM CASE.
        """
        n = self.rng.randint(2, 4)
        cp = self._counterparty()
        d = self._day(day)
        settle_d = self._day(day + 1)
        utr = self._ref("UTR")

        ledgers: list[CanonicalTransaction] = []
        settlements: list[CanonicalTransaction] = []
        total = Decimal("0.00")
        for _ in range(n):
            amt = self._amount(500, 20_000)
            order_ref = self._ref("order")
            ledgers.append(self._make_ledger(
                ref=order_ref, amount=amt, d=d, cp=cp, status=TxnStatus.CAPTURED
            ))
            settlements.append(self._make_settlement(
                ref=self._ref("pout"), order_ref=order_ref, net=amt,
                fee=Decimal("0.00"), d=settle_d, cp=cp,
                utr=utr if with_utr else None,
            ))
            total += amt

        bk = self._make_bank(
            utr=utr, amount=total, d=settle_d, cp=cp,
            truncated_narration=not with_utr,
        )

        if with_utr:
            gt = GroundTruthEntry(
                case_type=CaseType.ONE_TO_MANY,
                ledger_ids=[t.txn_id for t in ledgers],
                settlement_ids=[t.txn_id for t in settlements],
                bank_ids=[bk.txn_id],
                expected_handling=ExpectedHandling.MATCHED,
                note=f"{n} orders in one deposit; UTR recorded — deterministic",
            )
        else:
            gt = GroundTruthEntry(
                case_type=CaseType.ONE_TO_MANY_NO_UTR,
                ledger_ids=[t.txn_id for t in ledgers],
                settlement_ids=[t.txn_id for t in settlements],
                bank_ids=[bk.txn_id],
                expected_handling=ExpectedHandling.EXCEPTION,
                expected_exception="one_to_many_unresolved",
                note=f"{n} orders in one deposit; UTR missing — grouping ambiguous",
            )
        return [*ledgers, *settlements, bk], gt

    def _one_to_many(
        self, day: int
    ) -> tuple[list[CanonicalTransaction], GroundTruthEntry]:
        return self._one_to_many_impl(day, with_utr=True)

    def _one_to_many_no_utr(
        self, day: int
    ) -> tuple[list[CanonicalTransaction], GroundTruthEntry]:
        return self._one_to_many_impl(day, with_utr=False)

    def _partial_refund(self, day: int) -> tuple[list[CanonicalTransaction], GroundTruthEntry]:
        amt = self._amount(2000, 50_000)
        refund = (amt / 2).quantize(Decimal("0.01"))
        cp = self._counterparty()
        d = self._day(day)
        order_ref = self._ref("order")

        led = self._make_ledger(
            ref=order_ref, amount=amt, d=d, cp=cp,
            status=TxnStatus.PARTIALLY_REFUNDED,
        )
        rf = self._make_gateway(
            ref=self._ref("rfnd"), order_ref=order_ref, amount=refund,
            d=self._day(day + 1), cp=cp, status=TxnStatus.REFUNDED,
            direction=Direction.OUTBOUND,
        )
        gt = GroundTruthEntry(
            case_type=CaseType.PARTIAL_REFUND,
            ledger_ids=[led.txn_id], gateway_ids=[rf.txn_id],
            note="refund nets against original capture",
        )
        return [led, rf], gt

    def _fx(self, day: int) -> tuple[list[CanonicalTransaction], GroundTruthEntry]:
        foreign = self.rng.choice([Currency.USD, Currency.EUR, Currency.GBP])
        rate = FX_RATES[(foreign, Currency.INR)]
        amt_foreign = Decimal(self.rng.randint(50, 2000))
        amt_inr = (amt_foreign * rate).quantize(Decimal("0.01"))
        cp = self._counterparty()
        d = self._day(day)
        order_ref = self._ref("order")

        led = self._make_ledger(
            ref=order_ref, amount=amt_foreign, d=d, cp=cp,
            status=TxnStatus.CAPTURED, currency=foreign,
            narration=f"Export sale {foreign.value}",
        )
        bk = self._make_bank(
            utr=self._ref("UTR"), amount=amt_inr, d=self._day(day + 2), cp=cp
        )
        gt = GroundTruthEntry(
            case_type=CaseType.FX,
            ledger_ids=[led.txn_id], bank_ids=[bk.txn_id],
            note=f"{foreign.value}->INR at {rate}",
        )
        return [led, bk], gt

    def _duplicate_same_ref(self, day: int) -> tuple[list[CanonicalTransaction], GroundTruthEntry]:
        """A re-import artifact: identical record, identical external_ref.

        Ingestion dedupe on (source, external_ref) catches this. It must NOT be
        counted as an exception — the system handled it correctly.
        """
        txns, gt = self._clean(day)
        bank_line = next(t for t in txns if t.source == Source.BANK)
        dup = CanonicalTransaction(
            source=Source.BANK, external_ref=bank_line.external_ref,  # same ref
            amount_minor=bank_line.amount_minor, currency=bank_line.currency,
            direction=bank_line.direction, value_date=bank_line.value_date,
            posted_at=bank_line.posted_at, status=bank_line.status,
            counterparty=bank_line.counterparty, raw=dict(bank_line.raw),
        )
        gt.case_type = CaseType.DUPLICATE_SAME_REF
        gt.expected_handling = ExpectedHandling.DEDUPED_AT_INGESTION
        gt.expected_exception = None
        gt.bank_ids.append(dup.txn_id)
        gt.note = "same external_ref re-imported — dedupe catches it, not an exception"
        return [*txns, dup], gt

    def _duplicate_double_pay(self, day: int) -> tuple[list[CanonicalTransaction], GroundTruthEntry]:
        """A genuine double settlement: same economics, DIFFERENT external_ref.

        Dedupe cannot see this — the natural key differs. Only anomaly detection
        catches it, and it is real money at risk, so it must become an exception.
        """
        txns, gt = self._clean(day)
        bank_line = next(t for t in txns if t.source == Source.BANK)
        second_utr = self._ref("UTR")
        dup = self._make_bank(
            utr=second_utr,
            # to_major, never float division — see rule 1 in domain/models.py.
            amount=to_major(bank_line.amount_minor, bank_line.currency),
            d=bank_line.value_date, cp=bank_line.counterparty,
        )
        gt.case_type = CaseType.DUPLICATE_DOUBLE_PAY
        gt.expected_handling = ExpectedHandling.EXCEPTION
        gt.expected_exception = "duplicate_suspected"
        gt.bank_ids.append(dup.txn_id)
        gt.note = "paid twice under different UTRs — dedupe blind, anomaly must catch"
        return [*txns, dup], gt

    def _break_no_bank(self, day: int) -> tuple[list[CanonicalTransaction], GroundTruthEntry]:
        amt = self._amount()
        cp = self._counterparty()
        d = self._day(day)
        order_ref = self._ref("order")

        led = self._make_ledger(
            ref=order_ref, amount=amt, d=d, cp=cp, status=TxnStatus.CAPTURED
        )
        # Settled, but the payout never landed — so no UTR was ever issued.
        st = self._make_settlement(
            ref=self._ref("pout"), order_ref=order_ref, net=amt,
            fee=Decimal("0.00"), d=self._day(day + 1), cp=cp, utr=None,
        )
        gt = GroundTruthEntry(
            case_type=CaseType.BREAK_NO_BANK,
            ledger_ids=[led.txn_id], settlement_ids=[st.txn_id],
            expected_handling=ExpectedHandling.EXCEPTION,
            expected_exception="no_counterpart",
            note="settled but no bank credit — money at risk",
        )
        return [led, st], gt

    def _break_no_ledger(self, day: int) -> tuple[list[CanonicalTransaction], GroundTruthEntry]:
        amt = self._amount()
        cp = self._counterparty()
        d = self._day(day)
        bk = self._make_bank(utr=self._ref("UTR"), amount=amt, d=d, cp=cp)
        gt = GroundTruthEntry(
            case_type=CaseType.BREAK_NO_LEDGER,
            bank_ids=[bk.txn_id],
            expected_handling=ExpectedHandling.EXCEPTION,
            expected_exception="no_counterpart",
            note="bank credit with no ledger entry — unexplained money in",
        )
        return [bk], gt

    def _outflow_vendor(self, day: int) -> tuple[list[CanonicalTransaction], GroundTruthEntry]:
        """A variable vendor payment. Reconciles ledger<->bank on the debit side.

        Also the stochastic spend the cash forecaster has to model — without
        outflows, a forecast is only half a cash position.
        """
        amt = self._amount(5_000, 400_000)
        vendor = self._vendor()
        d = self._day(day)
        voucher = self._ref("bill")
        utr = self._ref("UTR")

        led = self._make_ledger(
            ref=voucher, amount=amt, d=d, cp=vendor, status=TxnStatus.SETTLED,
            direction=Direction.OUTBOUND, narration="Vendor payment",
            account="2100 - Accounts Payable",
        )
        bk = self._make_bank(
            utr=utr, amount=amt, d=d, cp=vendor, direction=Direction.OUTBOUND
        )
        gt = GroundTruthEntry(
            case_type=CaseType.OUTFLOW_VENDOR,
            ledger_ids=[led.txn_id], bank_ids=[bk.txn_id],
            note="vendor payment out — ledger/bank debit pair",
        )
        return [led, bk], gt

    _BUILDERS: ClassVar[dict[CaseType, str]] = {
        CaseType.CLEAN: "_clean",
        CaseType.FEE_LAG: "_fee_lag",
        CaseType.ONE_TO_MANY: "_one_to_many",
        CaseType.ONE_TO_MANY_NO_UTR: "_one_to_many_no_utr",
        CaseType.PARTIAL_REFUND: "_partial_refund",
        CaseType.FX: "_fx",
        CaseType.DUPLICATE_SAME_REF: "_duplicate_same_ref",
        CaseType.DUPLICATE_DOUBLE_PAY: "_duplicate_double_pay",
        CaseType.BREAK_NO_BANK: "_break_no_bank",
        CaseType.BREAK_NO_LEDGER: "_break_no_ledger",
        CaseType.OUTFLOW_VENDOR: "_outflow_vendor",
    }

    # -- recurring outflows & forward bookings -------------------------------

    def _recurring_outflows(
        self,
    ) -> tuple[list[CanonicalTransaction], list[GroundTruthEntry]]:
        """Lay payroll/rent/infra down on a FIXED monthly schedule.

        Deliberately not sampled from the random mix: a forecaster is supposed to
        learn that payroll lands on the 1st of every month. If the generator
        scattered it randomly there would be no pattern to learn, and the
        forecaster's recurring-inflow/outflow modelling would be untestable.
        """
        txns: list[CanonicalTransaction] = []
        gts: list[GroundTruthEntry] = []

        cursor = date(self._base_date.year, self._base_date.month, 1)
        while cursor <= self._end_date:
            for label, dom, amount_str, _certainty in RECURRING_OUTFLOWS:
                try:
                    due = cursor.replace(day=dom)
                except ValueError:  # pragma: no cover - dom is always 1/5/12
                    continue
                if not (self._base_date <= due <= self._end_date):
                    continue
                amt = Decimal(amount_str)
                voucher = self._ref("rec")
                utr = self._ref("UTR")
                led = self._make_ledger(
                    ref=voucher, amount=amt, d=due, cp=label,
                    status=TxnStatus.SETTLED, direction=Direction.OUTBOUND,
                    narration=label, account="2100 - Accounts Payable",
                )
                bk = self._make_bank(
                    utr=utr, amount=amt, d=due, cp=label,
                    direction=Direction.OUTBOUND,
                )
                txns.extend([led, bk])
                gts.append(GroundTruthEntry(
                    case_type=CaseType.OUTFLOW_RECURRING,
                    ledger_ids=[led.txn_id], bank_ids=[bk.txn_id],
                    note=f"recurring outflow: {label} on day {dom}",
                ))
            # advance one month
            cursor = (cursor.replace(day=28) + timedelta(days=7)).replace(day=1)
        return txns, gts

    def _future_bookings(self, horizon_days: int = 45) -> list[ScheduledOutflow]:
        """Known outflows AFTER end_date — the forecaster's deterministic scaffold.

        These are booked, not forecast. Modelling them statistically would be a
        mistake: we already know payroll is due on the 1st and how much it is.
        """
        out: list[ScheduledOutflow] = []
        horizon_end = self._end_date + timedelta(days=horizon_days)
        cursor = date(self._end_date.year, self._end_date.month, 1)
        while cursor <= horizon_end:
            for label, dom, amount_str, certainty in RECURRING_OUTFLOWS:
                due = cursor.replace(day=dom)
                if self._end_date < due <= horizon_end:
                    out.append(ScheduledOutflow(
                        label=label, due_date=due,
                        amount_minor=to_minor(Decimal(amount_str), Currency.INR),
                        currency=Currency.INR, counterparty=label,
                        certainty=certainty,
                    ))
            cursor = (cursor.replace(day=28) + timedelta(days=7)).replace(day=1)
        return sorted(out, key=lambda o: o.due_date)

    # -- post-processing -----------------------------------------------------

    def _apply_running_balance(
        self, transactions: list[CanonicalTransaction], opening_minor: int
    ) -> None:
        """Rewrite each bank line's statement Balance so the column is coherent.

        Bank lines are built in random event order, so a naive accumulator would
        produce a statement whose balance column contradicts its own rows. We sort
        chronologically and recompute. Mutating `raw` here is legitimate because
        at generation time we ARE the source — this is not post-ingestion
        tampering with provenance.
        """
        # The sort key must be TOTAL, not merely mostly-unique. Bank timestamps
        # are midnight (statements carry no time), so same-day ties are the norm;
        # and the duplicate-re-import case deliberately produces two lines sharing
        # value_date, posted_at AND external_ref. With anything less than a total
        # key, `sorted` falls back to list order and the balance column changes
        # under shuffling. `txn_id` is the final, always-unique tiebreak.
        bank = sorted(
            (t for t in transactions if t.source == Source.BANK),
            key=lambda t: (t.value_date, t.posted_at, t.external_ref, t.txn_id),
        )
        balance = opening_minor
        for txn in bank:
            if txn.direction == Direction.INBOUND:
                balance += txn.amount_minor
            else:
                balance -= txn.amount_minor
            if txn.raw:
                txn.raw["Balance"] = indian_grouped(balance, txn.currency)

    # -- public API ----------------------------------------------------------

    def generate(self, n_events: int) -> SyntheticBatch:
        """Generate `n_events` economic events across the history window.

        Note: n_events is the number of *events*, not transactions. A clean event
        is 4 transactions, a break might be 1. Total transactions will exceed
        n_events accordingly — the summary reports both. Recurring outflows are
        added on top of `n_events` on their fixed schedule.
        """
        case_types = list(self.mix.keys())
        weights = list(self.mix.values())

        transactions: list[CanonicalTransaction] = []
        ground_truth: list[GroundTruthEntry] = []

        for _ in range(n_events):
            case = self.rng.choices(case_types, weights=weights, k=1)[0]
            day = self.rng.randint(0, self.history_days - 1)
            builder = getattr(self, self._BUILDERS[case])
            txns, gt = builder(day)
            transactions.extend(txns)
            ground_truth.append(gt)

        rec_txns, rec_gts = self._recurring_outflows()
        transactions.extend(rec_txns)
        ground_truth.extend(rec_gts)

        # Coherent statement balances, then shuffle so the engine can't cheat off
        # insertion order. (Balance is computed pre-shuffle by value_date, so the
        # shuffle does not disturb it.)
        self._apply_running_balance(transactions, opening_minor=to_minor(
            Decimal("5000000.00"), Currency.INR
        ))
        self.rng.shuffle(transactions)

        return SyntheticBatch(
            transactions=transactions,
            ground_truth=ground_truth,
            seed=self.seed,
            start_date=self._base_date,
            end_date=self._end_date,
            scheduled_outflows=self._future_bookings(),
        )
