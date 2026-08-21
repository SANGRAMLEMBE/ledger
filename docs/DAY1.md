# Day 1 — Build Log

**Date:** Aug 21 · **Goal:** lay the foundations everything depends on and that
are expensive to change later. No feature code until the contract is proven.

## What shipped (all tested, all green)

| Component | File(s) | Status |
|-----------|---------|--------|
| Canonical transaction model | `domain/models.py` | ✅ 99% covered, 18 tests |
| Match & tier models | `domain/models.py` | ✅ invariants enforced |
| Exception taxonomy | `exceptions/taxonomy.py` | ✅ 100% covered |
| Synthetic generator + ground truth | `synthetic/generator.py` | ✅ 99% covered, 10 tests |
| Connector contract | `ingestion/connectors/base.py` | ✅ 94% covered |
| Frozen interface spec | `contracts/CONTRACTS.md` | ✅ ready to freeze Day 2 |
| Repo skeleton + CI + README | root | ✅ public-ready |

**Test suite: 39 passed, 92% coverage, lint clean.**

## Decisions made (and why) — the ones that matter later

1. **Python, not Go.** Team strength is ML/data; the core is dataframe set-logic
   and gradient-boosted trees. Throughput is solved architecturally (vectorised
   ops + blocking keys), not by language choice. If one hot path profiles too
   slow later, we drop a Rust/Cython kernel for *that function only* — we do not
   pay that tax on a guess.

2. **Money is `int` minor units.** The single most important correctness
   decision. Float breaks exact equality; reconciliation dies on that. Conversion
   is centralised and *refuses* sub-paisa precision. Locked by a test.

3. **All four sources from Day 1, one lean canonical schema.** Source-specific
   fields live in `raw`, never in the model. A fifth source drops in by writing a
   connector — zero engine changes.

4. **Ground-truth generator built before any matcher.** You cannot report a match
   rate without a known answer key. The generator plants the hard cases
   (one-to-many, fees, lag, FX, duplicates, genuine breaks) each tagged, and its
   answer key is itself tested for internal consistency (sums add up, breaks have
   no counterpart).

5. **Coverage gate at 85%, met honestly (92%).** When the gate flagged untested
   modules, we wrote the tests rather than lower the bar. The one uncovered file
   is a thin CLI wrapper.

6. **UP042 lint rule disabled with a documented reason.** We use `str, Enum`
   (not `StrEnum`) deliberately for stable Pydantic serialisation. A senior team
   configures a linter when the idiom is right and the rule is new — with a
   comment saying why.

## Proven at scale

`python -m ledger.synthetic.demo --events 15000` produces **55,895 transactions**
with a full ground-truth answer key including **1,306 genuine breaks** and
**1,039 one-to-many cases**, in ~1 second, deterministically.

## Day 2 picks up here

1. **Freeze `contracts/CONTRACTS.md`** with Lead 02 + Lead 09 sign-off.
2. **Four connectors in parallel** (settlement, bank, gateway, ledger) against the
   frozen `Connector` protocol — each with fixtures + tests.
3. **Ingestion pipeline** — central idempotent dedupe on `dedupe_key`.
4. **Deterministic tiers 0–1** (Lead 03) against the canonical model, measured on
   the synthetic batch.
5. **Terraform skeleton** (Lead 01) — VPC, RDS, S3, the deploy target.

The critical-path rule holds: nothing downstream starts until the contract it
depends on is frozen and tested. That's what Day 1 bought us.
