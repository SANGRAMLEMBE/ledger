"""
Pull real Razorpay test-mode records and report their actual field names.

    python scripts/fetch_razorpay.py --month 2026-08

WHY THIS SCRIPT EXISTS
----------------------
Our connectors are currently proven against `ledger.synthetic.raw_shapes` — our
own idea of what each source emits. That is self-consistent, not validated. The
only way to know whether `GatewayConnector` parses a genuine payment object is to
hold one and look at it.

So this script does exactly two things, and deliberately nothing more:

  1. Downloads raw responses to `data/` (gitignored) with **no transformation**.
     Reshaping on the way in would destroy the evidence we came for.
  2. Prints the field names each endpoint actually returned, and flags every key
     our connectors read that is *not* present verbatim — i.e. every place a
     mapping decision is owed.

It does NOT write fixtures, and it does NOT feed the connectors. Read the report
first, decide the mappings deliberately, then hand-build a small redacted
fixture. An auto-generated fixture that silently papered over a field mismatch
would defeat the entire point of pulling real data.

Stdlib only — no new dependency for a script that runs a handful of times.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

API_BASE = "https://api.razorpay.com/v1"

# Razorpay caps a page at 100 items.
PAGE_SIZE = 100

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = REPO_ROOT / ".env"
DEFAULT_OUT = REPO_ROOT / "data"

# The keys each connector reads from a raw record. Kept in sync by hand with
# ledger/ingestion/connectors/*.py — if you change what a connector reads, change
# it here too, or this report will quietly stop being true.
CONNECTOR_KEYS: dict[str, tuple[str, ...]] = {
    "gateway": (
        "id", "amount", "currency", "status", "created_at",
        "fee", "tax", "contact", "order_id",
    ),
    "settlement": (
        "payout_id", "settlement_id", "order_id", "amount", "fee", "tax",
        "currency", "utr", "settled_at", "counterparty", "status",
    ),
}


# --------------------------------------------------------------------------- #
# Credentials
# --------------------------------------------------------------------------- #


def load_env(path: Path) -> dict[str, str]:
    """Minimal .env reader — KEY=VALUE, '#' comments, blank lines ignored.

    Deliberately not python-dotenv: one function beats a dependency for a file
    format this small.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("'\"")
    return values


def credentials() -> tuple[str, str]:
    env = load_env(ENV_PATH)
    key_id = env.get("RAZORPAY_KEY_ID", "")
    key_secret = env.get("RAZORPAY_KEY_SECRET", "")

    if not key_id or not key_secret:
        sys.exit(
            f"No credentials found in {ENV_PATH}.\n"
            "Copy .env.example to .env and fill in RAZORPAY_KEY_ID / "
            "RAZORPAY_KEY_SECRET from the Razorpay dashboard "
            "(Test Mode -> Settings -> API Keys)."
        )

    if not key_id.startswith("rzp_test_"):
        # A live key would pull real customer data into a hackathon repo. Refuse
        # rather than warn: this is the one mistake here with consequences
        # outside the project.
        sys.exit(
            f"RAZORPAY_KEY_ID starts with {key_id[:9]!r}, which is not a test key.\n"
            "This script only runs against test mode. Switch the dashboard to "
            "Test Mode and generate a test key."
        )

    return key_id, key_secret


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #


def get(path: str, params: dict[str, Any], auth: tuple[str, str]) -> dict[str, Any]:
    """One authenticated GET. Razorpay uses HTTP Basic (key_id:key_secret)."""
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{API_BASE}{path}?{query}" if query else f"{API_BASE}{path}"
    token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()

    request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload: dict[str, Any] = json.loads(response.read().decode("utf-8"))
            return payload
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")[:500]
        if exc.code == 401:
            raise SystemExit(
                "401 Unauthorized — the key id/secret pair was rejected. Check "
                "that both halves came from the same generated test key."
            ) from exc
        raise SystemExit(f"HTTP {exc.code} on {path}: {body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"Could not reach {API_BASE}: {exc.reason}") from exc


def fetch_all(
    path: str,
    auth: tuple[str, str],
    limit: int,
    params: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Page through a list endpoint until `limit` or the source runs dry."""
    items: list[dict[str, Any]] = []
    skip = 0
    while len(items) < limit:
        page_params = dict(params or {})
        page_params.update(count=min(PAGE_SIZE, limit - len(items)), skip=skip)
        body = get(path, page_params, auth)
        page = body.get("items", [])
        if not page:
            break
        items.extend(page)
        skip += len(page)
    return items


# --------------------------------------------------------------------------- #
# Reporting
# --------------------------------------------------------------------------- #


def observed_keys(records: list[dict[str, Any]]) -> list[str]:
    """Every key seen across the sample.

    The union, not the first record's keys: endpoints omit fields on some records,
    and a field that appears on 1 row in 200 is still a field the connector may
    have to handle.
    """
    keys: set[str] = set()
    for record in records:
        keys.update(record.keys())
    return sorted(keys)


def report(label: str, records: list[dict[str, Any]], expected: tuple[str, ...]) -> None:
    print(f"\n{label}  ({len(records)} records)")
    if not records:
        print("  nothing returned — test mode starts empty. Create test payments")
        print("  first (payment links, or checkout with card 4111 1111 1111 1111).")
        return

    seen = observed_keys(records)
    print(f"  fields returned : {', '.join(seen)}")

    if expected:
        missing = [key for key in expected if key not in seen]
        if missing:
            print(f"  NOT PRESENT     : {', '.join(missing)}")
            print("  ^ each of these is a mapping decision the connector owes:")
            print("    either the value lives here under a different name, or it")
            print("    genuinely does not exist and the connector must stop")
            print("    requiring it.")
        else:
            print("  every field the connector reads is present verbatim.")

    print("  first record:")
    for line in json.dumps(records[0], indent=2, default=str).splitlines():
        print(f"    {line}")


def write(out_dir: Path, name: str, records: list[dict[str, Any]]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / name
    path.write_text(json.dumps(records, indent=2, default=str), encoding="utf-8")
    print(f"  -> wrote {len(records)} records to {path.relative_to(REPO_ROOT)}")


# --------------------------------------------------------------------------- #


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Pull Razorpay test-mode records and report their real shapes."
    )
    parser.add_argument(
        "--month",
        default=None,
        help="Settlement recon month as YYYY-MM. Omitted, the recon report is skipped.",
    )
    parser.add_argument(
        "--limit", type=int, default=200,
        help="Max records per list endpoint (default 200).",
    )
    parser.add_argument(
        "--out", type=Path, default=DEFAULT_OUT,
        help="Output directory (default: data/, which is gitignored).",
    )
    args = parser.parse_args()

    auth = credentials()
    print(f"Razorpay test mode — key {auth[0]}")

    payments = fetch_all("/payments", auth, args.limit)
    write(args.out, "razorpay_payments.json", payments)
    report("GET /payments  -> GatewayConnector", payments, CONNECTOR_KEYS["gateway"])

    settlements = fetch_all("/settlements", auth, args.limit)
    write(args.out, "razorpay_settlements.json", settlements)
    report("GET /settlements  (payout level, no per-txn detail)", settlements, ())

    if args.month:
        try:
            year_text, month_text = args.month.split("-")
            year, month = int(year_text), int(month_text)
        except ValueError:
            sys.exit(f"--month must look like 2026-08, got {args.month!r}")
        recon = fetch_all(
            "/settlements/recon/combined",
            auth,
            args.limit,
            params={"year": year, "month": month},
        )
        write(args.out, f"razorpay_recon_{args.month}.json", recon)
        report(
            "GET /settlements/recon/combined  -> SettlementConnector",
            recon,
            CONNECTOR_KEYS["settlement"],
        )
    else:
        print("\nSkipped the settlement recon report (pass --month YYYY-MM).")
        print("That report carries the per-transaction UTR that Tier 0 joins on,")
        print("so pull it before trusting the settlement connector.")

    print("\nRaw pulls live in data/ and are gitignored. Commit only small,")
    print("redacted samples, under tests/fixtures/real/.")


if __name__ == "__main__":
    main()
