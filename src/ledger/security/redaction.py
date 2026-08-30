"""
PII redaction for anything that leaves the process.

WHERE THE LEAK ACTUALLY HAPPENS
-------------------------------
Not in the database. In the **error message**, the **log line**, and the **stack
trace** — the three places nobody threat-models because they feel like debugging
output rather than data egress. A 500 that echoes the offending record ships a
customer's name into a log aggregator, a ticket, and a screenshot in a chat
thread, all before anyone notices.

Our bank narrations look like `NEFT CR-HDFC0000123-ANITA SHARMA-UTR9911`. That is
a real person's name in a field the parser touches on every row.

REDACT, DO NOT DROP
-------------------
A redacted value keeps its shape: `ANITA SHARMA` becomes `[NAME]`, an account
number becomes `[ACCT:…3421]` with only the last four digits kept. Dropping the
field entirely makes the log useless for debugging, which means people route
around the redaction — and a control people route around is worse than none.

WHAT IS NOT PII HERE
--------------------
A UTR, an order id and a payout id are transaction references, not personal data.
They are exactly what an engineer needs to find a record and exactly what an
auditor needs to follow a decision. Redacting them would gut the audit trail for
no privacy gain, so they are deliberately left intact.

THE SAFE PATH IS THE EASY PATH
------------------------------
`safe_ref()` gives a one-line, provably clean way to refer to any record. If
referring to a record safely is easier than dumping it, people do the safe thing
without being asked.
"""

from __future__ import annotations

import re
from typing import Any

from ledger.domain.models import CanonicalTransaction

# Fields whose values are never safe to emit. Bank narrations carry names; `raw`
# is the whole source payload by definition.
SENSITIVE_FIELDS = frozenset(
    {
        "counterparty",
        "contact",
        "party",
        "description",
        "narration",
        "raw",
        "email",
        "phone",
        "address",
    }
)

# Long digit runs are account or card numbers. Bounded at 9+ so a UTR reference
# or an amount in minor units is not swept up — over-redaction that hides the
# very identifiers an auditor needs is its own failure.
_ACCOUNT = re.compile(r"\b\d{9,18}\b")
_EMAIL = re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b")
# No leading \b: a word boundary cannot match before "+", so the country-code
# plus would be left dangling outside the redaction.
_PHONE = re.compile(r"(?:\+?91[-\s]?)?\b[6-9]\d{9}\b")

# Two or more consecutive all-caps words: how a bank statement prints a
# counterparty name. Deliberately anchored to the narration format rather than
# applied to arbitrary text, because plenty of legitimate tokens are uppercase.
_CAPS_NAME = re.compile(r"\b[A-Z][A-Z]+(?:\s+[A-Z][A-Z]+)+\b")

# Tokens that are uppercase but structural rather than personal.
_NOT_NAMES = frozenset({"NEFT CR", "NEFT DR", "IMPS", "RTGS", "UPI", "ACH"})


def redact_text(text: str) -> str:
    """Scrub identifiers from free text while keeping it readable.

    Order matters: account numbers and phone numbers overlap in shape, so the
    more specific pattern runs first.
    """
    if not text:
        return text

    scrubbed = _EMAIL.sub("[EMAIL]", text)
    scrubbed = _PHONE.sub("[PHONE]", scrubbed)

    def _account(match: re.Match[str]) -> str:
        digits = match.group(0)
        return f"[ACCT:{digits[-4:]}]"

    scrubbed = _ACCOUNT.sub(_account, scrubbed)

    def _name(match: re.Match[str]) -> str:
        value = match.group(0)
        return value if value in _NOT_NAMES else "[NAME]"

    return _CAPS_NAME.sub(_name, scrubbed)


def redact_mapping(payload: dict[str, Any]) -> dict[str, Any]:
    """Copy of a mapping with sensitive values removed and text scrubbed.

    Recurses, because the thing that leaks is usually nested — a `raw` payload
    inside an error context inside a log record.
    """
    clean: dict[str, Any] = {}
    for key, value in payload.items():
        if key.casefold() in SENSITIVE_FIELDS:
            clean[key] = "[REDACTED]"
        elif isinstance(value, dict):
            clean[key] = redact_mapping(value)
        elif isinstance(value, list):
            clean[key] = [
                redact_mapping(item)
                if isinstance(item, dict)
                else (redact_text(item) if isinstance(item, str) else item)
                for item in value
            ]
        elif isinstance(value, str):
            clean[key] = redact_text(value)
        else:
            clean[key] = value
    return clean


def safe_ref(txn: CanonicalTransaction) -> str:
    """Refer to a record without reproducing any of it.

    `source:external_ref` identifies the row for an engineer or an auditor and
    contains nothing personal. This is the sanctioned way to name a record in a
    log line, an error, or an audit entry.
    """
    return f"{txn.source.value}:{txn.external_ref}"


def safe_summary(txn: CanonicalTransaction) -> dict[str, Any]:
    """The fields of a record that are safe to emit, and only those.

    An allowlist rather than a denylist. A denylist silently leaks every field
    added after it was written; an allowlist silently omits them, which is the
    failure you want.
    """
    return {
        "ref": safe_ref(txn),
        "amount_minor": txn.amount_minor,
        "currency": txn.currency.value,
        "direction": txn.direction.value,
        "value_date": txn.value_date.isoformat(),
        "status": txn.status.value,
    }
