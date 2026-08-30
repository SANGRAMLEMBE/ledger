"""
API tests.

The properties worth defending, in order.

`test_every_route_requires_a_token` walks the OpenAPI document rather than a
hand-written list. A test that enumerates routes by hand stops covering the route
someone adds next week — which is exactly the one that ships unprotected.

`test_confirming_a_grouping_requires_the_senior_permission` is separation of
duties enforced over HTTP, not just in the permission table.

`test_raw_payload_is_never_served` guards the largest PII surface in the system.

`test_paging_the_whole_batch_neither_skips_nor_repeats` is the pagination bug that
shows up as a reconciliation that quietly misses a few hundred records.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ledger.api.app import app, reset_store
from ledger.api.deps import (
    MAX_PAGE_SIZE,
    RateLimiter,
    TokenStore,
    configure,
    decode_cursor,
    encode_cursor,
)
from ledger.security.rbac import Role

CONTROLLER_TOKEN = "controller-token-0123456789"
REVIEWER_TOKEN = "reviewer-token-0123456789ab"
VIEWER_TOKEN = "viewer-token-0123456789abcd"

CONTROLLER = {"Authorization": f"Bearer {CONTROLLER_TOKEN}"}
REVIEWER = {"Authorization": f"Bearer {REVIEWER_TOKEN}"}
VIEWER = {"Authorization": f"Bearer {VIEWER_TOKEN}"}

EVENTS = 600


@pytest.fixture(scope="module")
def client():
    store = TokenStore()
    store.add(CONTROLLER_TOKEN, "alice", Role.CONTROLLER)
    store.add(REVIEWER_TOKEN, "carol", Role.REVIEWER)
    store.add(VIEWER_TOKEN, "bob", Role.VIEWER)
    # A high limit so rate limiting does not interfere; it has its own tests.
    configure(store, RateLimiter(limit=100_000))
    reset_store()

    test_client = TestClient(app)
    test_client.post(f"/v1/batches?events={EVENTS}&seed=1337", headers=CONTROLLER)
    return test_client


class TestAuthentication:
    def test_health_is_open(self, client):
        """A load balancer has no bearer token."""
        assert client.get("/health").status_code == 200

    def test_health_leaks_nothing_about_the_data(self, client):
        assert client.get("/health").json() == {"status": "ok"}

    def test_every_route_requires_a_token(self, client):
        """Walk the spec, not a hand-written list — the next route is covered too."""
        spec = client.get("/openapi.json").json()
        checked = 0
        for path, operations in spec["paths"].items():
            if path in ("/health", "/openapi.json"):
                continue
            for method in operations:
                response = client.request(method.upper(), path.replace("{exception_id}", "x"))
                assert response.status_code == 401, (
                    f"{method.upper()} {path} answered {response.status_code} "
                    "without a token"
                )
                checked += 1
        assert checked >= 6, "expected several protected routes"

    def test_missing_and_wrong_token_are_indistinguishable(self, client):
        """Different answers tell an attacker which half to keep guessing."""
        no_token = client.get("/v1/me")
        bad_token = client.get(
            "/v1/me", headers={"Authorization": "Bearer not-a-real-token-x"}
        )
        assert no_token.status_code == bad_token.status_code == 401
        assert no_token.json()["message"] == bad_token.json()["message"]
        assert no_token.json()["code"] == "unauthenticated"

    def test_malformed_header_is_refused(self, client):
        for header in ("Basic abc", "Bearer", "Bearer    ", "token abc"):
            assert (
                client.get("/v1/me", headers={"Authorization": header}).status_code
                == 401
            )

    def test_whoami_reports_permissions(self, client):
        body = client.get("/v1/me", headers=REVIEWER).json()
        assert body["subject"] == "carol"
        assert body["role"] == "reviewer"
        assert "write:resolve_exception" in body["permissions"]
        assert "write:approve_money_movement" not in body["permissions"]


class TestTokenStorage:
    def test_tokens_are_stored_hashed_not_plaintext(self):
        """A dump of the store must not be a set of credentials."""
        store = TokenStore()
        store.add("a-secret-token-value-1234", "alice", Role.VIEWER)
        assert "a-secret-token-value-1234" not in repr(store)

    def test_short_tokens_are_refused(self):
        with pytest.raises(ValueError, match="too short"):
            TokenStore().add("short", "alice", Role.VIEWER)

    def test_from_env_has_no_default_credential(self):
        """A development fallback is the credential that reaches production."""
        assert len(TokenStore.from_env("")) == 0

    def test_from_env_parses_entries(self):
        store = TokenStore.from_env("token-long-enough-here:alice:controller")
        principal = store.authenticate("token-long-enough-here")
        assert principal is not None
        assert principal.role is Role.CONTROLLER

    def test_from_env_rejects_malformed_entries(self):
        with pytest.raises(ValueError, match="token:subject:role"):
            TokenStore.from_env("just-a-token-value-here")


class TestAuthorisation:
    def test_viewer_cannot_run_a_batch(self, client):
        assert (
            client.post("/v1/batches?events=10", headers=VIEWER).status_code == 403
        )

    def test_viewer_cannot_read_the_audit_trail(self, client):
        assert client.get("/v1/audit", headers=VIEWER).status_code == 403

    def test_reviewer_can_read_the_audit_trail(self, client):
        assert client.get("/v1/audit", headers=REVIEWER).status_code == 200

    def test_forbidden_response_says_which_permission(self, client):
        body = client.get("/v1/audit", headers=VIEWER).json()
        assert "read:audit" in body["message"]
        assert body["code"] == "permission_denied"
        assert body["correlation_id"]


class TestSeparationOfDutiesOverHttp:
    def _first_exception_with_candidates(self, client):
        page = client.get("/v1/exceptions?limit=200", headers=VIEWER).json()
        for item in page["items"]:
            if item["candidates"]:
                return item
        pytest.skip("no exception with candidates in this batch")

    def test_reviewer_may_resolve_without_confirming_a_grouping(self, client):
        exception = self._first_exception_with_candidates(client)
        response = client.post(
            f"/v1/exceptions/{exception['exception_id']}/resolve",
            headers=REVIEWER,
            json={
                "resolution": "chased with the gateway, awaiting advice",
                "idempotency_key": "reviewer-key-000001",
            },
        )
        assert response.status_code == 200
        assert response.json()["replayed"] is False

    def test_confirming_a_grouping_requires_the_senior_permission(self, client):
        """A reviewer working the queue must not be able to say where money went."""
        exception = self._first_exception_with_candidates(client)
        response = client.post(
            f"/v1/exceptions/{exception['exception_id']}/resolve",
            headers=REVIEWER,
            json={
                "resolution": "confirming the top grouping",
                "accept_candidate_index": 0,
                "idempotency_key": "reviewer-key-000002",
            },
        )
        assert response.status_code == 403
        assert "approve_money_movement" in response.json()["message"]

    def test_controller_may_confirm_a_grouping(self, client):
        exception = self._first_exception_with_candidates(client)
        response = client.post(
            f"/v1/exceptions/{exception['exception_id']}/resolve",
            headers=CONTROLLER,
            json={
                "resolution": "verified against the payout advice",
                "accept_candidate_index": 0,
                "idempotency_key": "controller-key-000001",
            },
        )
        assert response.status_code == 200
        assert response.json()["accepted_candidate_index"] == 0

    def test_out_of_range_candidate_is_rejected(self, client):
        exception = self._first_exception_with_candidates(client)
        response = client.post(
            f"/v1/exceptions/{exception['exception_id']}/resolve",
            headers=CONTROLLER,
            json={
                "resolution": "confirming a candidate that does not exist",
                "accept_candidate_index": 99,
                "idempotency_key": "controller-key-000002",
            },
        )
        assert response.status_code == 400
        assert response.json()["code"] == "candidate_out_of_range"


class TestIdempotency:
    def test_a_retry_is_acknowledged_not_reapplied(self, client):
        """Networks retry and users double-click. A resolution is a money decision."""
        page = client.get("/v1/exceptions?limit=1", headers=VIEWER).json()
        exception_id = page["items"][0]["exception_id"]
        body = {
            "resolution": "written off after investigation",
            "idempotency_key": "retry-key-abcdef01",
        }
        url = f"/v1/exceptions/{exception_id}/resolve"

        first = client.post(url, headers=CONTROLLER, json=body)
        second = client.post(url, headers=CONTROLLER, json=body)

        assert first.status_code == second.status_code == 200
        assert first.json()["replayed"] is False
        assert second.json()["replayed"] is True
        assert first.json()["resolution"] == second.json()["resolution"]

    def test_idempotency_key_is_required(self, client):
        page = client.get("/v1/exceptions?limit=1", headers=VIEWER).json()
        response = client.post(
            f"/v1/exceptions/{page['items'][0]['exception_id']}/resolve",
            headers=CONTROLLER,
            json={"resolution": "no key supplied"},
        )
        assert response.status_code == 422


class TestMoneyOnTheWire:
    def test_amounts_are_integer_minor_units_with_a_currency(self, client):
        page = client.get("/v1/transactions?limit=5", headers=VIEWER).json()
        for item in page["items"]:
            assert isinstance(item["money"]["amount_minor"], int)
            assert not isinstance(item["money"]["amount_minor"], bool)
            assert item["money"]["currency"]

    def test_no_float_appears_in_any_money_field(self, client):
        """A JSON number is a double on the other side and the client will do maths."""
        page = client.get("/v1/exceptions?limit=20", headers=VIEWER).json()
        for item in page["items"]:
            assert isinstance(item["at_risk"]["amount_minor"], int)
            for candidate in item["candidates"]:
                assert isinstance(candidate["money"]["amount_minor"], int)

    def test_amounts_are_not_preformatted_strings(self, client):
        page = client.get("/v1/transactions?limit=3", headers=VIEWER).json()
        raw = str(page)
        assert "Rs " not in raw
        assert "₹" not in raw


class TestNoPiiOnTheWire:
    def test_raw_payload_is_never_served(self, client):
        """The largest PII surface in the system stays server-side."""
        page = client.get("/v1/transactions?limit=50", headers=VIEWER).json()
        for item in page["items"]:
            assert "raw" not in item
        blob = str(page)
        assert "NEFT CR" not in blob
        assert "Ref No./Cheque No." not in blob

    def test_errors_carry_a_correlation_id_not_a_record(self, client):
        response = client.get("/v1/exceptions/does-not-exist", headers=VIEWER)
        assert response.status_code == 404
        body = response.json()
        assert body["correlation_id"]
        assert body["message"] == "no such exception"
        assert "does-not-exist" not in body["message"]


class TestPagination:
    def test_paging_the_whole_batch_neither_skips_nor_repeats(self, client):
        """The bug that shows up as a reconciliation quietly missing records."""
        seen: list[str] = []
        cursor = None
        for _ in range(100):  # bounded so a paging bug cannot loop forever
            url = "/v1/transactions?limit=97"
            if cursor:
                url += f"&cursor={cursor}"
            page = client.get(url, headers=VIEWER).json()
            seen.extend(item["txn_id"] for item in page["items"])
            cursor = page["next_cursor"]
            if cursor is None:
                break

        assert cursor is None, "pagination did not terminate"
        total = client.get("/v1/transactions?limit=1", headers=VIEWER).json()["total"]
        assert len(seen) == total
        assert len(set(seen)) == total, "a record was returned twice"

    def test_limit_is_capped_server_side(self, client):
        """The limit protects the server, so the server decides it."""
        page = client.get("/v1/transactions?limit=99999", headers=VIEWER).json()
        assert page["count"] <= MAX_PAGE_SIZE

    def test_zero_limit_is_rejected(self, client):
        assert (
            client.get("/v1/transactions?limit=0", headers=VIEWER).status_code == 400
        )

    def test_malformed_cursor_is_rejected(self, client):
        response = client.get("/v1/transactions?cursor=not-a-cursor", headers=VIEWER)
        assert response.status_code == 400

    def test_cursor_round_trips(self):
        assert decode_cursor(encode_cursor(42)) == 42
        assert decode_cursor(None) == 0

    def test_last_page_has_no_next_cursor(self, client):
        """A client must be able to stop on the envelope, without a wasted call.

        Note the limit is capped server-side, so asking for `total + 10` does not
        return everything — the last page has to be reached by cursor.
        """
        total = client.get("/v1/transactions?limit=1", headers=VIEWER).json()["total"]
        near_end = encode_cursor(total - 2)
        page = client.get(
            f"/v1/transactions?limit=50&cursor={near_end}", headers=VIEWER
        ).json()
        assert page["count"] == 2
        assert page["next_cursor"] is None


class TestBatchLifecycle:
    def test_summary_reports_correctness_separately(self, client):
        """Match rate and false matches must never be blended into one figure."""
        body = client.get("/v1/batches/current", headers=VIEWER).json()
        assert body["false_matches"] == 0
        assert body["unexplained"] == 0
        assert 0.0 <= body["match_rate"] <= 1.0
        assert body["exceptions_by_type"]

    def test_asking_before_a_run_is_a_conflict_not_a_500(self):
        reset_store()
        fresh = TestClient(app)
        response = fresh.get("/v1/batches/current", headers=CONTROLLER)
        assert response.status_code == 409
        assert response.json()["code"] == "no_batch"
        # Restore state for any later test in the module.
        fresh.post(f"/v1/batches?events={EVENTS}&seed=1337", headers=CONTROLLER)

    def test_batch_size_is_capped(self, client):
        response = client.post("/v1/batches?events=999999999", headers=CONTROLLER)
        assert response.status_code == 422

    def test_matches_carry_their_reason(self, client):
        page = client.get("/v1/matches?limit=5", headers=VIEWER).json()
        assert page["total"] > 0
        for item in page["items"]:
            assert item["reason"]
            assert item["tier"] in {"exact", "rule", "fuzzy", "ml"}
            assert 0.0 <= item["confidence"] <= 1.0


class TestRateLimiting:
    def test_limit_is_enforced_per_principal(self):
        limiter = RateLimiter(limit=3, window_seconds=60)
        for _ in range(3):
            limiter.check("alice")

        from fastapi import HTTPException

        with pytest.raises(HTTPException) as exc:
            limiter.check("alice")
        assert exc.value.status_code == 429
        assert "Retry-After" in exc.value.headers

        # A different principal has its own budget.
        limiter.check("bob")

    def test_window_expiry_restores_the_budget(self):
        limiter = RateLimiter(limit=2, window_seconds=10)
        limiter.check("alice", now=1000.0)
        limiter.check("alice", now=1001.0)
        # Same window: refused.
        from fastapi import HTTPException

        with pytest.raises(HTTPException):
            limiter.check("alice", now=1002.0)
        # Past the window: allowed again.
        limiter.check("alice", now=1100.0)


class TestOpenApi:
    def test_spec_is_generated_and_documents_the_money_rule(self, client):
        spec = client.get("/openapi.json").json()
        assert spec["info"]["title"] == "Ledger"
        assert "minor units" in spec["info"]["description"]

    def test_money_schema_explains_the_convention(self, client):
        spec = client.get("/openapi.json").json()
        money = spec["components"]["schemas"]["Money"]
        assert "minor units" in money["properties"]["amount_minor"]["description"]
