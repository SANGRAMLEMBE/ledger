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
python -m venv .venv
.venv\Scripts\activate                 # Windows
# source .venv/bin/activate            # macOS/Linux
pip install -e ".[dev,ml,api]"

pytest -m "not slow"                   # the suite
python -m ledger.eval.report           # the three numbers, held-out seed
python scripts/demo_check.py           # every demo step, end to end
```

Requires Python >= 3.11.

### The dashboard

```bash
$env:LEDGER_DEV_MODE = "1"             # PowerShell; prints a session token
uvicorn ledger.api:app --reload
```

Open <http://127.0.0.1:8000>, paste the printed token, press **Connect**, then
**Run batch**. Interactive API docs are at `/docs`.

There is deliberately no default credential: a static development token is the one
that reaches production and the one nobody rotates. Set `LEDGER_API_TOKENS` as
`token:subject:role` for anything real.

### What you can run

| Command | What it shows |
|---|---|
| `python -m ledger.eval.report` | match rate, throughput, exceptions, and a publishable/not verdict |
| `python -m ledger.forecasting.report` | forward cash position with a calibrated band |
| `python scripts/demo_check.py` | the whole demo path, pass/fail |
| `pytest -m slow` | load and complexity budget at 55k records |

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

**Complete and measured.** 300+ tests, ~95% coverage, lint clean, strict
type-checking and the benchmark both gating CI.

| | |
|---|---|
| Ingestion | four connectors, central idempotent dedupe, nothing silently dropped |
| Reconciliation | deterministic tiers with blocking keys, a binding collision policy |
| Exceptions | typed, priced, with ranked candidates and a suggested fix |
| Anomaly | double payments dedupe cannot see — 100% recall, 99.6% precision |
| Forecasting | booked outflows plus a calibrated bootstrap band, walk-forward backtested |
| Audit | append-only, PII-free, refusals recorded alongside successes |
| API | authenticated, RBAC with separation of duties, cursor-paginated |
| Infra | Terraform validated and planned; deliberately not applied |

### Deliberately not built

Both decisions are recorded with the measurement behind them in
[`report.md`](report.md), not left as gaps:

- **The ML matching tier.** Of 1,085 records reaching the ambiguous tail, exactly
  **6** are ones a matcher should resolve; the other 1,079 must be refused. The
  tier's ceiling was +0.04% match rate against a real risk of introducing false
  matches, so it was measured and skipped.
- **Tax-line matching** (Direction 5) — first to cut under time pressure, with a
  full spec ready if it becomes worth building.

### Honest limitations

- Metrics are on **synthetic data with known ground truth**, which is what makes a
  match rate possible at all — real statements carry no answer key.
- Connectors are **self-consistent with our assumed source shapes**, not yet
  validated against real bank exports.
- The forecaster's error is **flattered by the data**: the generator assigns events
  to uniformly random days, close to what the bootstrap assumes.
- **Throughput varies with batch size.** Quote it from a full 50k+ run.
