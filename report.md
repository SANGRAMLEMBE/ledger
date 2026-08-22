# Data Coverage Report — what the generator produces, and what it doesn't

**Last updated:** Day 2 (Aug 21) · **Owner:** Lead 02 (Ingestion & Schema)

This document answers one question honestly: **for each of the four directions in
the build plan, do we have the data to build and measure it?** It also carries the
full specification for the work we deliberately deferred, so picking it up later
is an implementation task and not a design task.

---

## 1. Coverage today

| Direction | Data status | Blocking gap |
|-----------|-------------|--------------|
| **2 · Multi-source Reconciliation** | ✅ Complete — and now proven end-to-end through the four connectors | none |
| **3 · Settlement Q&A Agent** | ⚠️ Partial | no fixed question set with known answers |
| **4 · Forward Cash Forecaster** | ✅ Sufficient | none for a first model |
| **5 · Tax-line Matcher** | ❌ None | no invoice / tax-line / rate-table data — **spec in §4 below** |

### What was fixed on Day 2

Three defects in the Day-1 foundation, all of which would have surfaced as
expensive rework around Day 8:

1. **Tier 0 had no key that could ever fire.** The contract specified matching on
   `(external_ref, amount_minor, currency)`, but every source has its own
   reference namespace, so cross-source `external_ref` equality is false by
   construction. Added `group_ref` to the canonical model (the settlement↔bank
   payout link, which `parent_ref` could not carry because it already points at
   the order), rewrote `CONTRACTS.md` §4 with the two joins that genuinely exist,
   and added a binding **collision policy**: when a key matches more than one
   candidate the engine must refuse rather than tie-break, because a confident
   wrong match silently moves money.

2. **Connectors had nothing real to parse.** Every generated record shipped
   `raw={}`, so the four connectors would have been built against hand-invented
   fixtures and the 50k benchmark would have bypassed ingestion entirely. Added
   `synthetic/raw_shapes.py`, which emits the true source payload behind every
   record, and a round-trip requirement in the contract. The shapes are
   deliberately messy where the real sources are messy — IST offsets, Indian lakh
   grouping, `DD/MM/YYYY`, blank-string Debit/Credit columns.

3. **The duplicate case contradicted itself.** A same-`external_ref` re-import was
   marked as a true break, but the pipeline dedupes it on `dedupe_key` and no
   exception is ever raised — the harness would have scored a miss for behaviour
   that was correct. Split into `DUPLICATE_SAME_REF` (dedupe catches it; not an
   exception) and `DUPLICATE_DOUBLE_PAY` (same economics, different UTR — dedupe
   is blind, anomaly detection must catch it, real money at risk). Added
   `ExpectedHandling` so the harness scores against three outcomes, not two.

### What was added for the forecaster

- **Configurable history window.** Was hard-coded to 30 days, which cannot
  backtest a T+30 forecast. Now `history_days` (default 30 for the dense
  month-end reconciliation benchmark; pass 365 for forecasting).
- **A real outflow stream.** Day 1 had exactly one outbound record type
  (refunds), so a "cash position" would have been inflows only. Added variable
  vendor payments (the stochastic spend) and **recurring** payroll/rent/infra on a
  fixed monthly schedule — fixed rather than random, because a forecaster is meant
  to *learn* that payroll lands on the 1st.
- **Forward bookings.** `SyntheticBatch.scheduled_outflows` carries known future
  outflows past `end_date` — the deterministic scaffold the forecaster books
  rather than predicts.
- **Coherent statement balances**, recomputed chronologically so the Balance
  column doesn't contradict its own rows.

---

## 2. What the generator emits now

```
SyntheticBatch
  transactions          list[CanonicalTransaction]   — all four sources, each with raw
  ground_truth          list[GroundTruthEntry]       — answer key + expected_handling
  start_date / end_date date                         — the history window
  scheduled_outflows    list[ScheduledOutflow]       — known future outflows
  seed                  int
```

A 15,000-event run on the default window produces roughly:

| | |
|---|---|
| transactions | ~55,700 |
| true breaks (must become exceptions) | ~1,580 |
| expected dedupes (must **not** become exceptions) | ~220 |
| ambiguous batch settlements (the 2 AM case) | ~470 |
| generation time | ~1.5s |
| full round-trip back through all four connectors | 1.8s (~30,400 rec/s), **0 mismatches** |

> Adding raw payloads cost roughly 0.5s of generation time. That is a build-time
> cost on the measuring instrument, not on the matching hot path, and it is what
> puts ingestion on the measured path. If it ever becomes annoying in CI, generate
> once and cache to `data/`.

---

## 3. Remaining gap for Direction 3 (Settlement Q&A)

The *data* is mostly there — `group_ref` gives settlement composition, and the
exception store gives reconciliation state. What's missing is the **evaluation
set**: a fixed list of questions with known-correct answers, without which
"answer accuracy" and "grounding rate" cannot be reported.

Needed when Direction 3 starts (small, ~half a day):

- ~40 questions across the four classes in the build plan (composition, variance,
  timing, reconciliation state), generated *from* the ground truth so the expected
  figures are known exactly.
- ~10 deliberately unanswerable / out-of-scope questions, to measure refusal
  correctness rather than assume it.

---

## 4. DEFERRED — Direction 5 data model (tax-line matching)

> **Status: not built, by decision.** Direction 5 is explicitly the first thing to
> cut under time pressure (build plan §6.1), so building its data model now would
> be speculative work. This section is the spec, ready to implement if we reach
> the FULL checkpoint. Estimated cost: **1–1.5 days** including tests.

### 4.1 Why it isn't free

Tax matching is not a variation on transaction matching — it needs a **line-item**
entity that does not exist today. `CanonicalTransaction` is deliberately one
record per money movement; an invoice with three items at three GST rates is one
money movement and three tax lines. That is a genuinely new shape, not a field.

### 4.2 New domain objects

```python
class TaxLine(BaseModel):          # frozen, extra="forbid"
    line_id: str
    invoice_ref: str               # parent invoice
    hsn_sac: str                   # HSN (goods) / SAC (services) code
    taxable_value_minor: int       # base, minor units
    rate_bps: int                  # basis points — 1800 = 18%. int, never float.
    cgst_minor: int
    sgst_minor: int
    igst_minor: int
    place_of_supply: str           # state code — decides CGST+SGST vs IGST
    currency: Currency

class Invoice(BaseModel):          # frozen, extra="forbid"
    invoice_ref: str
    counterparty_gstin: str | None
    invoice_date: date
    lines: list[TaxLine]
    total_minor: int               # must equal sum(taxable) + sum(all tax)
```

Rates as **`rate_bps: int`**, for the same reason money is `int` minor units:
18% as a float reintroduces the rounding error the whole schema exists to avoid.

### 4.3 Rate tables — versioned, not constants

```python
@dataclass(frozen=True)
class RateRule:
    hsn_prefix: str
    rate_bps: int
    valid_from: date
    valid_to: date | None          # open-ended = currently in force
```

Keyed by `value_date`, so a rate change mid-period is a data change and not a
code change. The build plan is explicit that these must be "versioned, testable
rules — never hard-coded constants scattered in code."

### 4.4 Cases the generator must plant

Mirroring how the reconciliation cases work — each tagged with its expected
handling:

| Case | Plants | Correct outcome |
|------|--------|-----------------|
| `TAX_CLEAN` | invoice tax == transaction tax, single rate | matched |
| `TAX_MIXED_RATE` | one invoice, 3 lines at 5% / 12% / 18% | matched **at line level** — invoice-level matching must fail here, which is the point |
| `TAX_WRONG_RATE` | 12% charged where the HSN table says 18% | exception `wrong_rate`, exposure = the delta |
| `TAX_ROUNDING_DRIFT` | ₹0.01 off from per-line rounding | matched within tolerance, rule cited — **not** an alarm |
| `TAX_MISSING_LINE` | transaction has tax, invoice has no line | exception `missing_tax_line` |
| `TAX_RATE_CHANGE` | invoice straddling a rate-change date | matched using the rate in force on `value_date` |
| `TAX_INTERSTATE` | place-of-supply differs → IGST not CGST+SGST | matched; wrong split becomes `wrong_rate` |

The two that carry the demo are `TAX_MIXED_RATE` (proves line-level matching) and
`TAX_ROUNDING_DRIFT` (proves we don't cry wolf over a paisa).

### 4.5 Metrics it unlocks

- Match rate on tax lines + discrepancy precision.
- **Exposure quantified** — total value under unresolved discrepancies.
- Exception list typed by the rule that caught it, each with amount and reference.

### 4.6 Trigger to build it

Build this **only** when all of these are true:

1. Direction 2 passes on the held-out seed (`1337`).
2. Directions 3 and 4 are demoable and rehearsed.
3. There are ≥ 2 clear days left.

Otherwise it stays deferred. Per the build plan: *depth on one loop beats breadth
across four.*

---

## 4b. Deployment target — decided

**Development and the demo run on Docker. AWS is deliberately not deployed.**

The budget is roughly **$50 in credits**, fixed. A `db.t4g.micro` at ~$12–15/month
would consume a large share of it for a database the build does not need — the
full pipeline runs locally in under three seconds, and every reported metric is
produced on a laptop.

| | |
|---|---|
| Local (`docker-compose.yml`) | Postgres 16 on port 5433. Zero cost. The working target. |
| AWS (`infra/terraform/`) | Written, validated, planned against a real account — **never applied**. |

Terraform still earns its place in the repo: it is the evidence that this is
deployable and extensible rather than a laptop demo, and reviewers read it. What
it does not need to be is *running*.

Deploy only if a live endpoint is genuinely wanted for the pitch video, and
`terraform destroy` the same day.

**Watch:** an EC2 instance from an unrelated project was found running in
`ap-south-1` since April. Idle resources drain this budget silently — check the
Billing console before assuming the credits are intact.

## 5. Open items

| Item | Owner | When |
|------|-------|------|
| Question set for Direction 3 eval | Lead 10 | when Direction 3 starts |
| Direction 5 data model (§4) | Lead 02 | only at FULL checkpoint |
| Cache a generated batch to `data/` if CI gets slow | Lead 01 | if needed |
| Confirm `history_days=365` batch size is tractable for the forecaster | Lead 07 | Day 11 |
