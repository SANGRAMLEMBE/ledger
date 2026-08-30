"""
Security tests.

Two properties are worth more than the rest combined.

`test_resolve_does_not_imply_approve` is the separation of duties. If working the
exception queue also let you confirm a candidate grouping, every reviewer could
assert where money landed — and the control would look present while being absent.

`test_bank_narration_name_is_redacted` is the leak that actually happens. Not the
database: the log line. Our narrations carry real names in a field the parser
touches on every row, and log lines end up in aggregators, tickets and
screenshots.
"""

from __future__ import annotations

import pytest

from ledger.security.rbac import (
    ROLE_PERMISSIONS,
    Permission,
    PermissionDeniedError,
    Principal,
    Role,
)
from ledger.security.redaction import (
    redact_mapping,
    redact_text,
    safe_ref,
    safe_summary,
)
from ledger.synthetic.generator import SyntheticGenerator


class TestSeparationOfDuties:
    def test_resolve_does_not_imply_approve(self):
        """The boundary that matters: working the queue is not moving money."""
        reviewer = Principal("carol", Role.REVIEWER)
        assert reviewer.can(Permission.RESOLVE_EXCEPTION)
        assert not reviewer.can(Permission.APPROVE_MONEY_MOVEMENT), (
            "a reviewer who can confirm a grouping can assert where money went"
        )

    def test_controller_holds_both(self):
        controller = Principal("alice", Role.CONTROLLER)
        assert controller.can(Permission.RESOLVE_EXCEPTION)
        assert controller.can(Permission.APPROVE_MONEY_MOVEMENT)

    def test_viewer_is_read_only(self):
        viewer = Principal("bob", Role.VIEWER)
        assert viewer.can(Permission.READ_TRANSACTIONS)
        for write in (
            Permission.RESOLVE_EXCEPTION,
            Permission.APPROVE_MONEY_MOVEMENT,
            Permission.RUN_BATCH,
        ):
            assert not viewer.can(write)

    def test_audit_is_not_a_viewer_permission(self):
        """The trail names counterparty references and amounts."""
        assert not Principal("bob", Role.VIEWER).can(Permission.READ_AUDIT)
        assert Principal("carol", Role.REVIEWER).can(Permission.READ_AUDIT)


class TestDenyByDefault:
    def test_unknown_role_gets_nothing(self):
        """A permissive default fails silently and totally."""
        rogue = Principal("mallory", role="superuser")  # type: ignore[arg-type]
        assert rogue.permissions == frozenset()
        assert not rogue.can(Permission.READ_TRANSACTIONS)

    def test_require_raises_with_context(self):
        viewer = Principal("bob", Role.VIEWER)
        with pytest.raises(PermissionDeniedError) as exc:
            viewer.require(Permission.RUN_BATCH)
        assert exc.value.subject == "bob"
        assert exc.value.permission is Permission.RUN_BATCH

    def test_admin_is_enumerated_not_wildcarded(self):
        """A new permission must not be granted to admin by inheritance."""
        admin = ROLE_PERMISSIONS[Role.ADMIN]
        assert admin == ROLE_PERMISSIONS[Role.CONTROLLER]
        assert isinstance(admin, frozenset)

    def test_no_anonymous_principal_exists(self):
        """A fallback identity is how an endpoint becomes world-readable."""
        import ledger.security.rbac as rbac

        assert not hasattr(rbac, "ANONYMOUS")


class TestRedaction:
    def test_bank_narration_name_is_redacted(self):
        text = "NEFT CR-HDFC0000123-ANITA SHARMA-UTR9911"
        cleaned = redact_text(text)
        assert "ANITA SHARMA" not in cleaned
        assert "[NAME]" in cleaned

    def test_structural_tokens_survive(self):
        """Redacting NEFT CR would make the line useless without protecting anyone."""
        assert "NEFT CR" in redact_text("NEFT CR-HDFC0000123-ANITA SHARMA-UTR1")

    def test_transaction_references_are_not_redacted(self):
        """A UTR is not personal data, and an auditor needs it."""
        text = "settlement pout_ab12cd34 for order_99 via UTR9911"
        assert redact_text(text) == text

    def test_email_and_phone_are_redacted(self):
        cleaned = redact_text("anita@example.com / +91 9876543210")
        assert "anita@example.com" not in cleaned
        assert "9876543210" not in cleaned
        assert "[EMAIL]" in cleaned and "[PHONE]" in cleaned

    def test_account_number_keeps_only_the_last_four(self):
        cleaned = redact_text("A/C 123456789012 credited")
        assert "123456789012" not in cleaned
        assert "[ACCT:9012]" in cleaned

    def test_empty_text_is_safe(self):
        assert redact_text("") == ""

    def test_sensitive_fields_are_replaced_wholesale(self):
        payload = {
            "Description": "NEFT CR-X-ANITA SHARMA-UTR1",
            "raw": {"anything": "at all"},
            "counterparty": "Acme Retail",
            "utr": "UTR991",
            "amount_minor": 4000,
        }
        cleaned = redact_mapping(payload)
        assert cleaned["Description"] == "[REDACTED]"
        assert cleaned["raw"] == "[REDACTED]"
        assert cleaned["counterparty"] == "[REDACTED]"
        # Non-sensitive fields survive untouched.
        assert cleaned["utr"] == "UTR991"
        assert cleaned["amount_minor"] == 4000

    def test_nested_structures_are_scrubbed(self):
        """What leaks is usually nested — raw inside context inside a log record."""
        payload = {
            "context": {"narration": "NEFT CR-X-ANITA SHARMA-UTR1"},
            "items": [{"description": "ANITA SHARMA paid"}, "ANITA SHARMA"],
        }
        cleaned = redact_mapping(payload)
        assert cleaned["context"]["narration"] == "[REDACTED]"
        assert cleaned["items"][0]["description"] == "[REDACTED]"
        assert "ANITA SHARMA" not in cleaned["items"][1]


class TestSafeReferences:
    def setup_method(self):
        batch = SyntheticGenerator(seed=5).generate(20)
        self.txn = next(t for t in batch.transactions if t.raw)

    def test_safe_ref_identifies_without_reproducing(self):
        ref = safe_ref(self.txn)
        assert ref == f"{self.txn.source.value}:{self.txn.external_ref}"
        assert str(self.txn.raw) not in ref

    def test_safe_summary_is_an_allowlist(self):
        """A denylist leaks every field added after it was written."""
        summary = safe_summary(self.txn)
        assert set(summary) == {
            "ref",
            "amount_minor",
            "currency",
            "direction",
            "value_date",
            "status",
        }
        assert "raw" not in summary
        assert "counterparty" not in summary

    def test_safe_summary_carries_no_narration(self):
        blob = str(safe_summary(self.txn))
        assert "NEFT" not in blob
