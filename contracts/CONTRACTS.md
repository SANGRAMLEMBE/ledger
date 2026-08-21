# Ledger — Frozen Contracts (v1)

> **Status: FROZEN as of Day 2.** Changing anything in this document requires a
> schema review with Lead 02 (Ingestion) and Lead 09 (Audit/Security), because
> every team codes against it. This is the interface that lets ten people build
> in parallel without stepping on each other.

## Why this document exists

A production system with ten contributors survives only if the *boundaries*
between their work are written down and stable. This is that writedown. If it's
in here, you can depend on it. If it's not, don't assume it.

---

## 1. The canonical transaction

Defined in `ledger.domain.models.CanonicalTransaction`. Every connector produces
these; every downstream component consumes only these.

| Field           | Type              | Required | Meaning |
|-----------------|-------------------|----------|---------|
| `txn_id`        | `str` (UUID)      | auto     | Ledger-internal id, assigned at ingestion. |
| `source`        | `Source` enum     | yes      | `gateway` \| `settlement` \| `bank` \| `ledger`. |
| `external_ref`  | `str`             | yes      | Source's own id. Unique *within* a source, not across. |
| `amount_minor`  | `int`             | yes      | Magnitude in minor units (paise). **Never negative, never float.** |
| `currency`      | `Currency` enum   | yes      | ISO 4217. Unknown currency is rejected. |
| `fee_minor`     | `int` ≥ 0         | no (0)   | Fee/charge in minor units. |
| `tax_minor`     | `int` ≥ 0         | no (0)   | Tax on fee, in minor units. |
| `direction`     | `Direction` enum  | yes      | `inbound` \| `outbound` — carries the sign. |
| `value_date`    | `date`            | yes      | When money actually moved. |
| `posted_at`     | `datetime` (UTC)  | yes      | Source record creation time. **Must be tz-aware.** |
| `status`        | `TxnStatus` enum  | yes      | Normalised lifecycle state. |
| `counterparty`  | `str \| None`     | no       | Normalised name/handle. |
| `parent_ref`    | `str \| None`     | no       | Economic parent (refund→capture, line→order). Points *up*. |
| `group_ref`     | `str \| None`     | no       | Batch/settlement grouping — records that moved as one payout. Points *sideways*. `None` when the source omits it. |
| `raw`           | `dict`            | no ({})  | Original payload, verbatim. Audit only — **the engine never matches on it.** |
| `ingested_at`   | `datetime` (UTC)  | auto     | When Ledger ingested it. |

### The three rules that are not negotiable

1. **Money is `int` minor units.** Use `to_minor` / `to_major` (in
   `domain.models`) at the display edge only. No floats touch money, ever.
2. **`direction` carries the sign.** `amount_minor` is always ≥ 0.
3. **Records are immutable** (`frozen=True`). Corrections create new records with
   audit linkage; they never mutate.

---

## 2. The connector interface

Defined in `ledger.ingestion.connectors.base.Connector` (a `Protocol`).

```python
class Connector(Protocol):
    source: Source
    def parse(self, raw_records: Iterable[dict]) -> Iterator[CanonicalTransaction]: ...
```

**A connector's contract:**
- Yields only fully-valid canonical transactions.
- Does **not** dedupe (the pipeline does, centrally, on `dedupe_key`).
- Does **not** do any cross-source or matching logic — it sees only its source.
- Raises `ConnectorError(source, locator, detail)` on a record that should be
  valid but isn't. `locator` must **not** contain PII.

Four connectors to build (one owner each): `settlement`, `bank`, `gateway`,
`ledger`. Each ships with its own fixture file in `tests/fixtures/` and its own
unit test.

### The round-trip requirement

The synthetic generator emits, for every record, the raw source payload that
record came from (`ledger.synthetic.raw_shapes`). Every connector **must** satisfy:

```python
connector.parse([txn.raw]) == txn      # modulo generated txn_id / ingested_at
```

This is the strongest correctness check available on a connector, and it exists
so ingestion sits **on the measured path**. Without it, connectors would be
validated against hand-written fixtures while the 50k benchmark quietly bypassed
them — and "50k records end-to-end" would not be a true statement.

The four shapes are deliberately of differing difficulty, mirroring reality:

| Source | Shape | The hard part |
|--------|-------|---------------|
| `gateway` | JSON, paise `int`, epoch ts | none — the easy one |
| `settlement` | report row, major-unit strings | timestamps carry a **+05:30 IST offset** that must be normalised to UTC |
| `bank` | statement line | `DD/MM/YYYY` dates, **Indian lakh grouping** (`1,23,456.78`), separate Debit/Credit columns where the unused one is `""` — not `0`, not `None` |
| `ledger` | GL export | debit/credit pair must collapse into `direction` |

---

## 3. Reconciliation output

Defined in `ledger.domain.models.MatchResult` and
`ledger.exceptions.taxonomy.ReconciliationException`.

The engine partitions the batch into exactly three buckets:

- **Matched** → a `MatchResult` with `tier`, `confidence`, and an explainable
  `reason`. Supports one-to-many via `left_ids` / `right_ids` lists.
- **Exception** → a `ReconciliationException` with `type`, `severity`,
  `amount_at_risk_minor`, ranked `candidates`, and a `suggested_resolution`.
- **(nothing else)** — every input record lands in exactly one bucket. No record
  is silently dropped. This invariant is tested.

### The confidence contract (money safety)

- `tier == EXACT` ⇒ `confidence ≈ 1.0` (enforced in the model).
- The ML tier auto-resolves **only above the auto-match threshold** (default
  **0.85**, config-owned by Lead 04). Below it → an `AMBIGUOUS_MATCH` exception
  with the candidates attached. **Money is never moved on a coin-flip.**

---

## 4. The matching cascade (tiers)

Order is fixed; a record exits at the first tier that resolves it confidently.

| Tier | Name    | Uses AI? | Resolves |
|------|---------|----------|----------|
| 0    | Exact   | No       | **Reference-linked** joins only — see the key table below. |
| 1    | Rule    | No       | Fee-adjusted amount, T+1/T+2 date window, recorded FX rate. |
| 2    | Fuzzy   | No       | Blocking-key candidates + `rapidfuzz` similarity. |
| 3    | ML      | Yes      | LightGBM classifier on engineered pair features (the ambiguous tail only). |

### Tier 0 — the keys that actually exist

> **Corrected Day 2.** The original spec said Tier 0 matches on
> `(external_ref, amount_minor, currency)`. That can never fire across sources:
> every source has its own reference namespace (ledger `order_…`, gateway
> `pay_…`, settlement `pout_…`, bank a UTR), so cross-source `external_ref`
> equality is false by construction. Writing the engine against that key would
> have produced a 0% Tier-0 hit rate and pushed the entire batch into the
> expensive tiers.

The real deterministic links are directional and there are exactly two:

| Join | Key | Covers |
|------|-----|--------|
| gateway/settlement → ledger | `child.parent_ref == ledger.external_ref` (+ `currency`) | order-level linkage |
| settlement → bank | `settlement.group_ref == bank.external_ref` | payout/batch linkage, incl. one-to-many |

Amount is a **confirmation**, not part of the key — the settlement is net of fees
while the ledger is gross, so requiring amount equality here would reject every
fee-bearing match. Tier 1 is where the fee-adjusted arithmetic happens.

### When there is no reference link

`group_ref` is `None` whenever the source genuinely omitted it — a truncated bank
narration, an in-flight payout with no UTR yet. Those records **must not** fall
back to `(amount_minor, value_date, counterparty)` as if it were a key. In a 50k
batch that tuple is **not unique**: many same-day payouts to the same counterparty
share an amount, and matching on it produces confident, wrong pairings. A wrong
auto-match is worse than an exception — it silently moves money.

**Collision policy (binding on Tiers 0–2):**

1. Build the candidate set for a record.
2. If **exactly one** candidate satisfies the key → match.
3. If **more than one** → do **not** pick one, and do not pick the "best" by
   arbitrary tie-break. Pass the record down the cascade with its candidates
   attached.
4. If the last tier still cannot separate them → emit `AMBIGUOUS_MATCH` or
   `ONE_TO_MANY_UNRESOLVED` with the ranked `candidates`, and let a human decide.

This policy is the reason the batch-settlement case in the demo raises an honest
exception instead of a confident guess. It is a correctness requirement, not a
presentational one.

**Complexity budget (owned by Leads 03 & 04):** no tier may compare the full
cross-product. Blocking keys (date-window × amount-band × currency) bound
candidate pairs to a small constant per record. Target: full 50k batch in
**seconds**, not minutes. State the budget in code comments and the demo.

---

## 5. Ground truth & metrics

`ledger.synthetic.SyntheticGenerator` produces the labelled dataset. The eval
harness (Lead 10) computes, against a **held-out** seed:

- **Match rate** — fraction of true-match events correctly auto-reconciled.
- **Precision / recall** on matches, plus **false-match count** (a wrong
  auto-match is worse than an exception).
- **Throughput** — records/sec on the full batch, under load.
- **Exception honesty** — every planted break must surface as an exception; a
  missed break is a silent loss and fails the harness.

### Scoring against three outcomes, not two

`GroundTruthEntry.expected_handling` says what *correct* looks like, because for
one case "did it match?" is the wrong question:

| `expected_handling` | Correct behaviour | Scored as a failure if… |
|---------------------|-------------------|--------------------------|
| `MATCHED` | reconciles into a `MatchResult` | unmatched, or matched to the wrong counterpart |
| `DEDUPED_AT_INGESTION` | dropped by the pipeline on `dedupe_key`, counted in the ingestion report | it reaches matching, **or** it is raised as an exception |
| `EXCEPTION` | surfaces as the typed exception named in `expected_exception` | silently matched, or dropped |

A same-`external_ref` re-import that dedupe catches is handled **correctly** and
must not be scored as a missed match or as a break. Scoring it either way would
punish the system for being right — the Day-1 answer key did exactly that, and it
is fixed.

### Seed convention (binding)

| Seed | Use |
|------|-----|
| `42` | development. Tune, debug and iterate against this freely. |
| `1337` | **held-out**. The reported numbers come from this seed and nothing may be tuned against it. |
| any other | ad-hoc exploration. |

Two windows, deliberately:

- **`history_days=30`** — the dense month-end close. This is the reconciliation
  benchmark and the demo narrative ("August close, overnight").
- **`history_days=365`** — long history for the cash forecaster, which cannot
  backtest T+7/T+14/T+30 on 30 days. Same generator, different window.

---

## 6. Ownership map

| Area | Owner | Depends on this contract for |
|------|-------|------------------------------|
| Canonical schema | Lead 02 | everyone |
| Connectors ×4 | Leads 02 (+2 helpers) | §1, §2 |
| Deterministic tiers 0–1 | Lead 03 | §1, §4 |
| Fuzzy + ML tiers 2–3 | Lead 04 | §1, §4, §5 |
| Exception engine | Lead 06 | §3 |
| Forecaster | Lead 07 | §1 (reconciled ledger) |
| API & serving | Lead 08 | §1, §3 |
| Audit/security | Lead 09 | all |
| Eval/dashboard/demo | Lead 10 | §5 |
| Platform/infra | Lead 01 | repo, CI, deploy |
