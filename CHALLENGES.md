# Build Challenges Log

> Every lead adds to this as real obstacles come up — one entry per genuine
> problem, with the fix. On submission day this is the raw material for the form's
> "Build Challenges & Technical Obstacles" answer. Razorpay scores *"what broke,
> and what you did about it"* — a real log beats an invented story every time.
> Keep it honest. A boring fix honestly recorded is worth more than a dramatic one
> invented.

Format:
```
### [DATE] — [short title]  (owner)
**Problem:** what actually went wrong.
**Root cause:** why.
**Fix:** what we did.
**Lesson / guardrail:** what stops it recurring.
```

---

### Day 2 — The round-trip test proved the generator was inventing data  (Lead 02)
**Problem:** With the four connectors written, the round-trip
(`connector.parse([txn.raw]) == txn`) failed for three fields across two sources,
and none of the failures were connector bugs.
**Root cause:** The generator was recording things the source never says.
(1) Bank and GL records had a random *time of day* on `posted_at`, but a statement
line and a GL export carry a date only — no connector could ever recover
`14:37`. (2) Bank records stored the nicely-cased `Acme Retail` while the
statement narration only ever contains `ACME RETAIL`. (3) The settlement report
shape had no counterparty field at all, yet the canonical record had one.
**Fix:** `posted_at` is midnight UTC for date-only sources (`_date_only_ts`);
bank `counterparty` stores the uppercase form the bank actually printed; added
`counterparty` to the settlement report shape.
**Lesson / guardrail:** A round-trip test doesn't just check the parser — it
audits whether your *fixtures* are honest. Every field that failed was a field we
had quietly made up. Keeping the bank's `ACME RETAIL` uncorrected is also more
useful: normalising it at ingestion would have hidden the case mismatch that the
fuzzy tier legitimately has to solve.

---

### Day 2 — A "mostly unique" sort key is not a sort key  (Lead 02)
**Problem:** The bank statement's running-balance column started failing its
coherence test intermittently after bank timestamps moved to midnight.
**Root cause:** Balances are computed pre-shuffle by sorting on
`(value_date, posted_at, external_ref)`. That key is *almost* total — except the
duplicate-re-import case deliberately emits two bank lines sharing all three. On
ties, `sorted` is stable and falls back to list order, so the pre-shuffle and
post-shuffle orders disagreed and the balances no longer followed from the rows.
**Fix:** Append `txn_id` to make the key genuinely total.
**Lesson / guardrail:** Stable-sort tie-breaking silently couples your output to
input order. If a key can tie, finish it with something unique — especially when
the data deliberately contains duplicates.

---

### Day 2 — Tier 0 was specified against a key that can never match  (Lead 02 / Lead 03)
**Problem:** The frozen contract defined the exact-match tier as
`(external_ref, amount_minor, currency)`. Reviewing the generator before writing
the engine, we found this can never fire across sources.
**Root cause:** Every source owns its own reference namespace — ledger `order_…`,
gateway `pay_…`, settlement `pout_…`, bank a UTR. Cross-source `external_ref`
equality is false by construction. Had we coded to the spec, Tier 0 would have
returned a 0% hit rate and pushed all 50k records into the expensive tiers, which
we'd probably have misdiagnosed as "the ML tier needs work".
**Fix:** Added `group_ref` to the canonical model for the settlement↔bank payout
link (`parent_ref` couldn't carry it — it already points up at the order, and one
field can't hold two different links). Rewrote `CONTRACTS.md` §4 around the two
joins that actually exist.
**Lesson / guardrail:** A frozen contract is not a correct contract. `TestDeterministicLink`
now asserts the link exists exactly where the answer key claims, and is absent
where it claims ambiguity.

---

### Day 2 — The answer key punished the system for being right  (Lead 02 / Lead 10)
**Problem:** The duplicate case was marked as a true break expecting a
`duplicate_suspected` exception, but the pipeline dedupes on
`(source, external_ref)` and never raises one. The eval harness would have scored
a failure for correct behaviour.
**Root cause:** The ground truth modelled two outcomes (matched / not matched)
when there are genuinely three — a record can also be correctly *dropped* at
ingestion.
**Fix:** Added `ExpectedHandling` (`MATCHED` / `DEDUPED_AT_INGESTION` /
`EXCEPTION`) and split the case in two: `DUPLICATE_SAME_REF` (dedupe catches it,
not an exception) and `DUPLICATE_DOUBLE_PAY` (same economics under a different
UTR — dedupe is blind, so anomaly detection must catch it and it *is* real money
at risk).
**Lesson / guardrail:** Before trusting a metric, check that the answer key can
express every correct outcome. The second duplicate kind is also a better demo —
it's the one that costs actual money.

---

### Day 2 — Connectors would have been tested against fiction  (Lead 02)
**Problem:** The generator built canonical records directly and left `raw` empty,
so the four connectors had nothing real to parse.
**Root cause:** Day 1 built the canonical model first (correctly) but never went
back to emit the source shapes behind it. The connectors would have been unit
tested against hand-written fixtures while the 50k benchmark bypassed ingestion —
making "50k records end-to-end" untrue in a way that's hard to notice.
**Fix:** Added `synthetic/raw_shapes.py` emitting the real payload behind every
record, deliberately messy where the sources are messy (IST offsets on settlement
timestamps, Indian lakh grouping `1,23,456.78` on bank statements, `DD/MM/YYYY`,
blank-string Debit/Credit columns). Contract now requires
`connector.parse([txn.raw]) == txn`.
**Lesson / guardrail:** If a layer isn't exercised by the benchmark, the benchmark
doesn't cover it. Cost: generation went from ~1s to ~8s — acceptable, since it's
the measuring instrument and not the hot path.

---

### Day 2 — Float money slipped back in, in our own fix  (Lead 02)
**Problem:** While writing the double-payment duplicate case we wrote
`Decimal(f"{amount_minor / 100:.2f}")` — float division on money, breaking rule 1
about ten minutes after quoting it.
**Root cause:** Needing a major-unit value in a context where `to_major` wasn't
already imported, and reaching for arithmetic instead of the helper.
**Fix:** Use `to_major(amount_minor, currency)`. Caught on review before the
commit.
**Lesson / guardrail:** The rule needs enforcement, not discipline. Candidate CI
check: fail any diff introducing `/ 100` or `* 100` near a `_minor` identifier.

---

### Day 1 — Money precision decision  (Lead 02)
**Problem:** First instinct was to store amounts as `Decimal` or `float` for
readability.
**Root cause:** Reconciliation depends on *exact* equality; float breaks it
(`0.1 + 0.2 != 0.3`) and Decimal invites accidental float coercion at boundaries.
**Fix:** Store all money as `int` minor units (paise). Conversion helpers
(`to_minor`/`to_major`) are the only sanctioned crossing, and `to_minor` *refuses*
sub-paisa precision rather than rounding silently.
**Lesson / guardrail:** A unit test (`test_classic_float_trap_does_not_occur`)
locks it in. Any PR reintroducing float money fails CI.

---

<!-- Add new entries above this line, newest first. -->
