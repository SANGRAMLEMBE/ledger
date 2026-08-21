# Ledger

**An autonomous finance controller.** Ledger ingests transactions from multiple
sources, reconciles them across a large batch, and — crucially — tells you
*exactly what it could not resolve* instead of guessing. It closes one finance-ops
loop end to end: reconciliation, honest exceptions, and a forward cash position.

Built for the Razorpay /buildathon, Track 04 (AI Finance Controller).

---

## The one idea

Reconciliation is somebody's 2 AM. Month-end close means matching thousands of
transactions across a payment gateway, a bank statement, and an internal ledger —
by hand, with fees, timing lag, split settlements and partial refunds in the way.
Ledger does it in seconds, and **does not move money on a guess**: anything it
can't confidently match becomes a typed, explainable exception with the amount at
risk and a suggested resolution.

Three numbers, reported honestly on a held-out set:

- **Match rate** — how much of the batch reconciled correctly.
- **Throughput** — records/sec, under load.
- **Honest exceptions** — everything it refused to guess on, typed and counted.

## Architecture

```
  ingestion            reconciliation           exceptions + cash        serving
  ─────────            ──────────────           ─────────────────        ───────
  4 connectors  ──►    cascade: exact ─►rule    unresolved ─► typed      API +
  → canonical          ─►fuzzy ─►ML (tail)      exception + cash         audit log
  transaction          each match scored        forecast                 dashboard
```

The matching cascade is **deterministic first, on purpose** — a rule is correct,
cheap and auditable — and reaches for a model only on the ambiguous tail that
rules genuinely cannot separate. See [`contracts/CONTRACTS.md`](contracts/CONTRACTS.md)
for the frozen interfaces.

## Quickstart

```bash
# 1. install (editable, with dev + ml extras)
pip install -e ".[dev,ml,api]"

# 2. run the test suite
pytest

# 3. generate a synthetic batch with ground truth
python -m ledger.synthetic.demo --events 15000 --seed 42
```

Requires Python ≥ 3.11.

## Layout

```
src/ledger/
  domain/          canonical transaction + match/exception models (the contract)
  ingestion/       connectors (one per source) + the pipeline
  reconciliation/  the matching cascade (tiers 0–3)
  exceptions/      the typed exception taxonomy
  forecasting/     forward cash position
  api/             REST serving layer
  audit/           append-only decision log
  security/        auth, RBAC, secrets, PII policy
  synthetic/       ground-truth data generator (the measuring instrument)
contracts/         FROZEN interface + data dictionary
tests/             unit + integration, with fixtures
infra/             Terraform (AWS)
```

## Design principles

- **Money is never a float.** Integer minor units everywhere; conversion only at
  the display edge.
- **Validation at the boundary.** A record that can't be made canonical is
  rejected at ingestion, loudly — not three layers deep.
- **Every decision is reproducible.** The audit log records what matched, what
  didn't, and why.
- **Honest metrics.** Measured on a held-out synthetic set with known ground
  truth. One cherry-picked match proves nothing.

## Status

**94 tests, 94% coverage, lint clean, strict type-checking gating CI.**

Complete: canonical schema, exception taxonomy, the synthetic generator with
ground truth, and all four source connectors — round-trip verified across a
55,321-record batch with zero mismatches.

In progress: the ingestion pipeline, then the deterministic matching tiers, which
produce the first measured match rate.

**Honest limitation:** metrics are computed on synthetic data with known ground
truth, which is what makes a match rate possible at all — real statements carry no
answer key. The connector round trip proves internal consistency with our assumed
source shapes, not that those shapes match real exports. See
[`report.md`](report.md) for coverage and open items.
