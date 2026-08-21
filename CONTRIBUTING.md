# Contributing to Ledger

> **Read this before your first change.** It is the source of truth for how Ledger
> is built, the decisions already made, and what to build next. The conventions
> here are deliberate and tested, and other components depend on them. When in
> doubt, prefer the pattern already in the codebase over introducing a new one.

---

## What we're building

**Ledger** — an autonomous finance controller. It ingests transactions from four
sources (payment gateway, settlement report, bank statement, internal ledger),
reconciles them across a large batch, and — the core idea — **reports exactly what
it could not resolve instead of guessing**. Unresolved records become typed,
explainable exceptions carrying the money at risk and a suggested fix.

Three numbers, reported honestly on a **held-out** synthetic set:

- **Match rate** — fraction auto-reconciled correctly.
- **Throughput** — records/sec under load.
- **Honest exceptions** — everything it refused to guess on, typed and counted.

---

## Non-negotiable rules

Violating these breaks the system, usually silently — which is the worst way for a
finance system to fail.

1. **Money is `int` minor units (paise). Never float, never `Decimal` in storage.**
   Use `to_minor` / `to_major` in `domain/models.py` only at the display edge.
   `to_minor` refuses sub-paisa precision rather than rounding. A test
   (`test_classic_float_trap_does_not_occur`) fails if float money returns.

2. **`direction` carries the sign; `amount_minor` is always ≥ 0.**

3. **Validate at the boundary.** Bad records are rejected at ingestion, never
   passed half-valid into the pipeline.

4. **Canonical records are immutable** (`frozen=True`). Corrections create new
   records with audit linkage; they never mutate.

5. **Money is never moved on a guess.** The ML tier auto-resolves only above the
   confidence threshold (default 0.85). Below it → an exception with candidates.

6. **Every input record lands in exactly one bucket** (matched | excepted).
   Nothing is silently dropped. This is a tested invariant.

7. **Don't lower a quality gate to pass.** Write the test. Coverage gate is 85%;
   core logic files stay ≥ 95%.

8. **No PII in logs or error messages.** Locators are indices and ids, never
   record contents — a bank narration contains a person's name.

9. **Keep `CHALLENGES.md` updated** as real obstacles come up.

---

## Architecture

```
  ingestion            reconciliation           exceptions + cash        serving
  ─────────            ──────────────           ─────────────────        ───────
  4 connectors  ──►    cascade: exact ─►rule    unresolved ─► typed      API +
  → canonical          ─►fuzzy ─►ML (tail)      exception + cash         audit log
  transaction          each match scored        forecast                 dashboard
```

The **matching cascade** is the heart. Four tiers, cheapest and most certain
first; a record exits at the first tier that resolves it confidently:

| Tier | Name  | Uses ML? | Resolves |
|------|-------|----------|----------|
| 0 | Exact | No  | reference-linked joins only — see below |
| 1 | Rule  | No  | fee-adjusted amount, T+1/T+2 date window, recorded FX rate |
| 2 | Fuzzy | No  | blocking-key candidates + `rapidfuzz` similarity |
| 3 | Model | Yes | gradient-boosted classifier on engineered pair features (tail only) |

### Tier 0 — the keys that actually exist

Cross-source `external_ref` equality is **always false**: each source has its own
reference namespace (`order_…`, `pay_…`, `pout_…`, a UTR). The two real joins are:

| Join | Key |
|------|-----|
| gateway/settlement → ledger | `child.parent_ref == ledger.external_ref` (+ `currency`) |
| settlement → bank | `settlement.group_ref == bank.external_ref` |

Amount is a **confirmation, not part of the key** — settlement is net of fees while
ledger is gross, so requiring equality here rejects every fee-bearing match.

### The collision policy — binding

When a key matches more than one candidate, **do not tie-break and do not pick the
best**. Pass the record down the cascade with candidates attached; if the last tier
still cannot separate them, emit `AMBIGUOUS_MATCH` / `ONE_TO_MANY_UNRESOLVED` with
ranked candidates for a human.

`(amount_minor, value_date, counterparty)` is **not a key**. It is not unique in a
50k batch, and matching on it produces confident wrong pairings that silently move
money.

### The complexity budget

No tier may compare the full cross-product (O(n·m) — billions of comparisons at
50k, will not finish). Use **blocking keys** (date-window × amount-band ×
currency) to bound candidate pairs to a small constant per record → effectively
O(n log n). State this budget in code comments.

---

## Code layout

```
src/ledger/
  domain/models.py        THE CONTRACT. CanonicalTransaction, MatchResult,
                          MatchTier, money helpers. Everything speaks this.
  exceptions/taxonomy.py  ReconciliationException, ExceptionType, Severity,
                          CandidateMatch. The honest-refusal output.
  ingestion/
    connectors/base.py    Connector Protocol + BaseConnector.
    connectors/*.py       bank, gateway, settlement, ledger — all four built.
    pipeline.py           TO BUILD: idempotent ingestion + central dedupe
  reconciliation/         TO BUILD: the cascade (tiers 0–3)
  forecasting/            TO BUILD: forward cash position
  api/                    TO BUILD: REST serving
  audit/                  TO BUILD: append-only decision log
  security/               TO BUILD: auth, RBAC, secrets, PII policy
  synthetic/generator.py  DONE. Ground-truth data generator (measuring tool).
  synthetic/raw_shapes.py DONE. The raw source payload behind every record.

contracts/CONTRACTS.md    FROZEN interfaces + data dictionary. Read before
                          touching any boundary.
tests/unit/               mirror src structure; write tests WITH the code
tests/integration/        end-to-end (mark @pytest.mark.integration)
report.md                 data coverage per direction + deferred scope
```

---

## Conventions

- **Imports:** absolute, from the `ledger.` root.
- **Types:** full hints everywhere; `from __future__ import annotations` at the top
  of every module. mypy runs in strict mode and **gates CI**.
- **Enums:** `class X(str, enum.Enum)` — deliberate (not `StrEnum`) for stable
  Pydantic serialisation. `UP042` is disabled in `pyproject.toml` for this reason.
- **Models:** Pydantic v2, `ConfigDict(frozen=True, extra="forbid")` for domain
  objects. `extra="forbid"` makes a typo'd field a loud error, not a silent drop.
- **Docstrings:** module-level docstring explaining WHY the module exists and the
  rules that govern it (see `domain/models.py` as the template). Explain design
  decisions, not just behaviour.
- **Errors:** typed, with context but no PII (see `ConnectorError`).
- **Tests:** every new module gets a test file in the same change. Test invariants
  and edge cases, not just the happy path. Use the generator's ground truth — never
  hand-craft expected matches when the generator already knows the answer.

---

## Commands

```bash
pip install -e ".[dev,ml,api]"                    # install
pytest --cov=ledger --cov-report=term-missing     # tests + coverage
ruff check src tests                              # lint
mypy src                                          # types (strict)
python -m ledger.synthetic.demo --events 15000 --seed 42   # see the data
```

---

## Data & measurement

**Seed convention (binding):** `42` = development, tune freely. **`1337` = held
out** — reported numbers come from this seed and nothing may be tuned against it.
You cannot un-see a test set.

**Windows:** `history_days=30` = dense month-end close (the reconciliation
benchmark and the demo narrative); `history_days=365` = long history the cash
forecaster needs to backtest T+7/T+14/T+30.

**Scoring has three outcomes, not two.** `GroundTruthEntry.expected_handling` is
`MATCHED`, `DEDUPED_AT_INGESTION`, or `EXCEPTION`. A duplicate caught by ingestion
dedupe is handled *correctly* and must not be scored as a miss or an exception.

---

## Status

**Done — 94 tests, 94% coverage, ruff clean, mypy strict clean (gating).**

- Canonical model, match/tier models, exception taxonomy
- Synthetic generator with ground truth, plus raw source payloads
- All four source connectors, round-trip verified across a 55,321-record batch
  with zero mismatches
- Frozen contracts, CI with gates, repo hygiene

**Known limitation:** the round trip proves the connectors are self-consistent
with our *assumed* source shapes — not that those shapes match real exports.
Validating against real test-mode and bank samples is tracked in `report.md`.

## What to build next, in order

Each depends on the one before. Write tests alongside. Measure against ground truth.

1. **`ingestion/pipeline.py`** ← start here. Route raw records to the right
   connector; **idempotent dedupe** on `dedupe_key = (source, external_ref)`; emit
   canonical transactions plus an ingestion report (counts in, deduped, rejected
   with locators).
2. **`reconciliation/deterministic.py`** — Tiers 0 and 1 using the corrected keys
   above, with blocking keys and the collision policy. Vectorise with `polars`;
   never row-by-row loops over 50k. This produces the first real match rate.
3. **`reconciliation/engine.py`** — the orchestrator. Enforce and test the
   invariant that every record ends up matched or excepted, exactly once.
4. **Then** the eval harness, fuzzy/model tiers, exception engine wiring,
   forecaster, API. Build the harness *before* the model tiers — a tier you cannot
   measure cannot be justified.

## Local-only files — important

Some files exist on a contributor's machine to support their own editor and
tooling. **They are never part of this repository**, and nothing that names or
describes a contributor's personal toolchain belongs in a published file.

The mechanism matters, because it is easy to get wrong:

| File | Scope | Use it for |
|------|-------|------------|
| `.gitignore` | **published** — everyone sees it | build artefacts, caches, secrets, generated data |
| `.git/info/exclude` | **local only** — never pushed | your own editor/tooling files and scratch work |

Putting a personal path in `.gitignore` defeats the purpose: the ignore rule is
committed, so the filename is published even though the file is not. Personal
ignores go in `.git/info/exclude`.

**Enforcement.** Two local git hooks back this up rather than relying on memory:
`pre-commit` rejects any staged path or added line matching the excluded tooling
patterns, and `commit-msg` rejects tooling attribution in commit messages. They
live in `.git/hooks/` (local, not committed). If you clone this repo fresh on
another machine, re-create them — they are the reason a slip becomes a blocked
commit instead of a published one.

**Before any push**, confirm nothing local-only is staged:

```bash
git diff --cached --name-only     # exactly what is about to be committed
git ls-files                      # everything currently tracked
```

Note also that **git history is published, not just the current files**. Removing
a file in a later commit does not remove it from the repository — it stays in
history and stays reachable. If something local-only was committed, the fix is to
rewrite history before pushing, not to delete the file in a follow-up commit.

## Working rhythm

- **Before editing a boundary** (`domain/models.py`, `contracts/CONTRACTS.md`, the
  `Connector` protocol), stop and confirm — every other slice depends on it.
- Write the test in the same change. Run `pytest` before calling it done.
- Prefer extending an existing pattern over inventing a new one.
- When a real obstacle costs you time, add it to `CHALLENGES.md` immediately.
