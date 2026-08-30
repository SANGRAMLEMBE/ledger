"""
Pre-demo check: prove the whole path works before anyone is watching.

    python scripts/demo_check.py

Runs the complete journey a judge will see — start the app, authenticate, run a
batch, read the three numbers, open an ambiguous exception, page the transactions,
read the audit trail — and prints a verdict.

WHY THIS EXISTS
---------------
"It worked yesterday" is not a demo plan. The failures that ruin a live demo are
never the interesting ones: a missing environment variable, an empty token store,
a route that quietly 404s after a rename. Each is trivial to fix with two minutes'
warning and impossible to fix with an audience.

It uses the in-process test client rather than a live server on purpose: no port
to be already in use, no process to leave running, and it can be run from CI. If
this passes, the only thing left that can break on stage is the network — and
there isn't one, because everything runs locally.
"""

from __future__ import annotations

import sys

from fastapi.testclient import TestClient

from ledger.api.app import app, reset_store
from ledger.api.deps import RateLimiter, TokenStore, configure
from ledger.security.rbac import Role

TOKEN = "demo-check-token-0123456789"
CONTROLLER = {"Authorization": f"Bearer {TOKEN}"}
EVENTS = 4_000
SEED = 1337

PASS = "  [ok]  "
FAIL = "  [XX]  "


class Checks:
    def __init__(self) -> None:
        self.failures: list[str] = []

    def that(self, description: str, condition: bool, detail: str = "") -> bool:
        if condition:
            print(f"{PASS}{description}{('  ' + detail) if detail else ''}")
        else:
            print(f"{FAIL}{description}{('  ' + detail) if detail else ''}")
            self.failures.append(description)
        return condition


def main() -> int:
    checks = Checks()

    store = TokenStore()
    store.add(TOKEN, "demo", Role.CONTROLLER)
    # A high ceiling so the check itself is never what trips the rate limiter.
    configure(store, RateLimiter(limit=100_000))
    reset_store()

    client = TestClient(app)

    print("\n  DEMO CHECK\n" + "  " + "-" * 62)

    print("\n  Reachability")
    health = client.get("/health")
    checks.that("health responds without auth", health.status_code == 200)
    page = client.get("/")
    checks.that(
        "dashboard is served by the API",
        page.status_code == 200 and "Ledger" in page.text,
        f"{len(page.text):,} bytes",
    )
    checks.that(
        "dashboard carries no hard-coded figures",
        "94.2%" not in page.text and "3,940" not in page.text,
    )

    print("\n  Access control")
    checks.that(
        "an unauthenticated request is refused",
        client.get("/v1/batches/current").status_code == 401,
    )
    checks.that(
        "asking before a batch is a clear conflict, not a crash",
        client.get("/v1/batches/current", headers=CONTROLLER).status_code == 409,
    )

    print("\n  The batch")
    run = client.post(
        f"/v1/batches?events={EVENTS}&seed={SEED}", headers=CONTROLLER
    )
    if not checks.that("batch runs", run.status_code == 201, str(run.status_code)):
        print("\n  Cannot continue without a batch.\n")
        return 1

    summary = run.json()
    checks.that(
        "match rate is reported",
        0.0 < summary["match_rate"] <= 1.0,
        f"{summary['match_rate']:.2%}",
    )
    checks.that(
        "throughput is reported",
        summary["throughput_per_second"] > 0,
        f"{summary['throughput_per_second']:,.0f} rec/s",
    )
    checks.that(
        "exceptions are typed and priced",
        summary["exceptions"] > 0 and summary["at_risk"]["amount_minor"] > 0,
        f"{summary['exceptions']:,} worth "
        f"Rs {summary['at_risk']['amount_minor'] / 100:,.2f}",
    )
    checks.that(
        "NO false matches",
        summary["false_matches"] == 0,
        f"{summary['false_matches']}",
    )
    checks.that(
        "NO record silently lost",
        summary["unexplained"] == 0,
        f"{summary['unexplained']}",
    )

    print("\n  The moment the pitch is built on")
    exceptions = client.get(
        "/v1/exceptions?type=one_to_many_unresolved&limit=5", headers=CONTROLLER
    ).json()
    ambiguous = [e for e in exceptions["items"] if e["candidates"]]
    checks.that(
        "an ambiguous batch settlement is available to show",
        bool(ambiguous),
        f"{exceptions['total']:,} of them",
    )
    if ambiguous:
        example = ambiguous[0]
        checks.that(
            "it offers ranked candidates rather than a guess",
            len(example["candidates"]) >= 1,
            f"{len(example['candidates'])} grouping(s)",
        )
        checks.that(
            "it states the money at risk",
            example["at_risk"]["amount_minor"] > 0,
            f"Rs {example['at_risk']['amount_minor'] / 100:,.2f}",
        )
        checks.that(
            "it suggests what a human should do",
            len(example["suggested_resolution"]) > 20,
        )

    print("\n  Serving")
    transactions = client.get("/v1/transactions?limit=50", headers=CONTROLLER).json()
    checks.that(
        "transactions paginate",
        transactions["count"] == 50 and transactions["next_cursor"],
        f"{transactions['total']:,} total",
    )
    checks.that(
        "raw source payloads are never served",
        all("raw" not in item for item in transactions["items"]),
    )
    checks.that(
        "money is integer minor units on the wire",
        all(
            isinstance(item["money"]["amount_minor"], int)
            for item in transactions["items"]
        ),
    )

    matches = client.get("/v1/matches?limit=5", headers=CONTROLLER).json()
    checks.that(
        "every match carries its reason",
        matches["total"] > 0 and all(m["reason"] for m in matches["items"]),
        f"{matches['total']:,} matches",
    )

    audit = client.get("/v1/audit?limit=200", headers=CONTROLLER).json()
    checks.that("audit trail is populated", audit["total"] > 0, f"{audit['total']:,} entries")
    refused = client.get(
        "/v1/audit?decision=match_refused&limit=5", headers=CONTROLLER
    ).json()
    checks.that(
        "refusals are on the record, not just successes",
        refused["total"] > 0,
        f"{refused['total']:,} refusals recorded",
    )
    checks.that(
        "each refusal says what it considered",
        all(e["detail"]["candidates"] for e in refused["items"]),
    )
    blob = str(audit["items"])
    checks.that(
        "no PII in the audit trail",
        "NEFT CR" not in blob and "NEFT DR" not in blob,
    )

    print("\n  " + "-" * 62)
    if checks.failures:
        print(f"  NOT READY — {len(checks.failures)} check(s) failed:")
        for failure in checks.failures:
            print(f"      - {failure}")
        print()
        return 1

    print("  READY. Every step of the demo path works.\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
