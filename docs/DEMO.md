# Demo runbook

Five minutes, four beats. Every number said aloud comes from a command in this
file, run live.

---

## Before you start

```bash
.venv\Scripts\activate                 # Windows
python scripts/demo_check.py           # must print READY
```

`demo_check.py` walks the entire demo path — auth, batch, the ambiguous
exception, pagination, the audit trail — and fails loudly if any of it is broken.
Run it the morning of, not the night before. It takes about twenty seconds and it
is the difference between finding a problem with two minutes' warning and finding
it with an audience.

Then, in a terminal you will not touch again:

```bash
$env:LEDGER_DEV_MODE = "1"
uvicorn ledger.api:app --port 8000
```

Copy the token it prints. Open <http://127.0.0.1:8000>, paste it, press
**Connect**, then **Run batch**. Leave that tab open.

---

## Beat 1 — the problem (45s)

*No screen. Just say it.*

> Month-end close means matching thousands of transactions across a payment
> gateway, a bank statement and an internal ledger. Fees, timing lag, split
> settlements, partial refunds. Finance teams do it by hand, at night, and it is
> the thing that delays every close.

Then put the dashboard up.

> This is a 50,000-record close. It took under three seconds.

---

## Beat 2 — the three numbers (75s)

*Point at the top row of the dashboard.*

Say each number and what it is **measured against**:

- **Match rate** — against a known answer key, on a seed the engine has never
  been tuned on.
- **Throughput** — records per second across the full batch.
- **Honest exceptions** — everything it refused to guess at, typed, each with the
  money at risk.

Then the fourth tile, which is the one that matters:

> **Zero false matches. Zero records unexplained.** Every record is either
> reconciled or on that list. Nothing was silently dropped.

*Point at the cascade bar.*

> Notice the two largest bands are labelled **no model**. Most of this is
> resolved by references and rules — cheap, exact, auditable. That is deliberate.

---

## Beat 3 — the 2 AM moment (110s) · **the climax**

*Scroll to the exception panel. Pick a `one_to_many_unresolved`.*

> Here is one bank deposit. Several settlement lines could compose it. The
> settlement report did not record the UTR, so there is no reference that says
> which grouping is right.
>
> A confident system picks the best-scoring one. **This one refuses.**

*Point at the ranked candidates.*

> It shows the groupings it considered, what each sums to, and how confident it
> is in each. Then it stops, states the money at risk, and tells a human what to
> check.
>
> Forcing a match here would report the money as reconciled. Nobody would ever
> look at it again. An unmatched record raises its hand and costs someone thirty
> seconds — a wrong match costs you the money and the audit trail.

**Then show it is on the record.** In a second terminal:

```bash
curl -s "http://127.0.0.1:8000/v1/audit?decision=match_refused&limit=1" ^
  -H "Authorization: Bearer <token>"
```

> Every refusal is in the audit log with the candidates it weighed. Not just the
> successes — a trail of only successes cannot explain a close.

---

## Beat 4 — what broke, and the close (60s)

*This is the graceful-failure beat. Use a real one from `CHALLENGES.md`.*

The strongest is the audit-sink bug, because the failure mode is the interesting
part:

> Our audit trail silently did not exist. The code reported 832 entries written
> and the sink was empty. Both our sinks define `__len__`, so an **empty** sink
> is falsy in Python — `audit_sink or InMemoryLog()` threw away the caller's sink
> and wrote every decision into a throwaway object. No error, no warning.
>
> We found it because the test asserted entries landed in the sink we passed, not
> merely that a count came back. A returned count proves work happened somewhere,
> not that it happened where you asked.

Alternatives if asked for another: the ground-truth comparison that keyed on
`txn_id` and produced a flawless report from a comparison that matched nothing;
or Tier 0 specified against a key that could never fire.

**Close on this:**

> We built the measurement before the engine. That is why we can tell you the
> match rate is real, and why we can tell you the ML matching tier has six records
> of legitimate work in fifty-five thousand — so we did not build it. The
> measurement decides, not the roadmap.

---

## If something goes wrong

| Symptom | Cause | Fix |
|---|---|---|
| Every request 401s | no token loaded | check the startup log; set `LEDGER_DEV_MODE=1` |
| `ModuleNotFoundError: ledger` | venv not active | `.venv\Scripts\activate` |
| Dashboard blank | no batch run | press **Run batch** |
| `409 no_batch` | same | same |
| Port busy | something else on 8000 | `--port 8001` |

**If the live demo fails entirely**, fall back to the terminal — it needs nothing
but Python:

```bash
python -m ledger.eval.report          # the three numbers
python -m ledger.forecasting.report   # the cash position
```

---

## Numbers to have memorised

Run `python -m ledger.eval.report` and use **its** output, not these — they move
with the code. But know the shape:

| | |
|---|---|
| Batch | ~55,000 records, four sources |
| Match rate | measured against ground truth on held-out seed `1337` |
| False matches | **0** |
| Unexplained records | **0** |
| Planted breaks surfaced | **100%** |
| Ambiguous settlements | **refused, never guessed** |
| Resolved without a model | **all of it** |

## What not to claim

Say these plainly if asked — they are the answers that survive a follow-up
question, and each is already written down in `report.md`.

- Metrics are on **synthetic data with known ground truth**. Real statements carry
  no answer key, which is why synthetic is required here rather than a shortcut.
- The connectors are **self-consistent with our assumed source shapes**, not yet
  validated against real bank exports.
- The forecaster's MAPE is **flattered by the data** — the generator assigns events
  to uniformly random days, which is close to what the bootstrap assumes. Real cash
  flow is autocorrelated and would do worse.
- **Throughput varies with batch size.** Quote it from a full 50k+ run, not a small
  one.
