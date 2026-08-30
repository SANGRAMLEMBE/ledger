# SETUP — Running Ledger in VS Code

This gets you from a fresh clone to a working project in VS Code, tests green,
ready to build.

---

## 1. Unpack and open

```bash
git clone https://github.com/SANGRAMLEMBE/ledger.git
cd ledger
code .          # opens the folder in VS Code
```

## 2. Create a virtual environment

Keep the project's dependencies isolated. From the project root:

```bash
python3 -m venv .venv
source .venv/bin/activate        # macOS/Linux
# .venv\Scripts\activate         # Windows PowerShell
```

VS Code will usually prompt "We noticed a new virtual environment — select it?".
Say yes, or pick it manually: `Cmd/Ctrl+Shift+P` → **Python: Select Interpreter**
→ choose `.venv`.

## 3. Install

```bash
pip install --upgrade pip
pip install -e ".[dev,ml,api]"
```

`-e` installs Ledger in *editable* mode, so your source edits take effect without
reinstalling. The extras pull in test tools (`dev`), the ML stack (`ml`), and the
API stack (`api`).

## 4. Prove it works

```bash
# run the whole test suite — should be 39 passed
pytest

# see the data shape (generates a ground-truth batch)
python -m ledger.synthetic.demo --events 15000 --seed 42

# lint (should say: All checks passed!)
ruff check src tests
```

If those three pass, the environment is correct and you're building on solid
ground.

---

## 5. Recommended VS Code extensions

- **Python** (Microsoft) — interpreter, debugging, test discovery
- **Ruff** (Astral) — inline lint + format on save
- **Mypy Type Checker** (Microsoft) — inline type errors

Open the Testing panel (the flask icon) — VS Code auto-discovers the pytest suite,
so you can run/debug individual tests by clicking them.

---

## 6. Read the contributor guide

`CONTRIBUTING.md` carries the full project context: the non-negotiable rules
(money as integer minor units, validation at the boundary, never resolving money
on a guess), the conventions, the corrected Tier-0 keys, and the exact build
order. Read it before your first change — the rules there are tested, and other
components depend on them.

`contracts/CONTRACTS.md` is the frozen interface spec. Read it before touching
any boundary.

## 7. Run the API

The virtual environment must be active, or `uvicorn` resolves to a global install
that cannot import `ledger`:

```bash
.venv\Scripts\activate         # Windows
# source .venv/bin/activate      # macOS/Linux

# Generate a session token and start the server
$env:LEDGER_DEV_MODE = "1"       # PowerShell
uvicorn ledger.api:app --reload
```

The token is printed at startup. Open <http://127.0.0.1:8000/docs>, click
**Authorize**, and paste it.

Without `LEDGER_DEV_MODE` or `LEDGER_API_TOKENS` the server starts, logs an
explicit error, and rejects every request — deliberately, so a misconfiguration is
visible at startup rather than diagnosed from a stream of identical 401s.

---

## 8. The working rhythm (keep this discipline)

1. Pick the next item from the "What to build next" list in `CONTRIBUTING.md`.
2. Write the code **and** its test in the same change.
3. `pytest` + `ruff check` before considering it done.
4. If something real broke and cost you time, add a line to `CHALLENGES.md`.
5. Commit with a clear message. Keep the trunk green.

---

## Troubleshooting

- **`ModuleNotFoundError: ledger`** → you didn't `pip install -e .`, or the wrong
  interpreter is selected. Re-check step 2–3.
- **`lightgbm` fails to install** → it needs a C++ runtime; on macOS `brew install
  libomp`. You can defer this: `pip install -e ".[dev]"` alone is enough for
  connectors and the deterministic tiers (Day 2). Add `ml` when you reach the ML
  tier.
- **Tests not discovered in VS Code** → `Cmd/Ctrl+Shift+P` → **Python: Configure
  Tests** → pytest → `tests`.
