"""
The serving layer.

This is the surface an integrator actually touches, so it is where "a demo" and
"a system they can extend" visibly differ. Every route is authenticated, every
permission is checked server-side, every collection is paginated, and no error
echoes a record.

ROUTES ARE THIN ON PURPOSE
--------------------------
Authenticate, validate, delegate to `service`, serialise. Logic in a handler
cannot be tested without HTTP and cannot be reused by the CLI, so there is none
here.

ERRORS ARE MAPPED, NOT LEAKED
-----------------------------
Domain exceptions become typed responses with a correlation id. The unhandled
case returns a 500 that says nothing about what failed — the detail goes to the
server-side log, and the caller gets an id to quote. A stack trace in a response
body is a data leak wearing a helpful face.
"""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, status
from fastapi.responses import JSONResponse

from ledger.api import service
from ledger.api.deps import (
    PageParams,
    TokenStore,
    configure,
    correlation_id,
    current_principal,
    encode_cursor,
    page_params,
    require_permission,
)
from ledger.api.schemas import (
    BatchSummaryOut,
    ErrorOut,
    ExceptionOut,
    ExceptionPage,
    MatchPage,
    ResolveRequest,
    ResolveResponse,
    TransactionPage,
)
from ledger.api.service import (
    BatchStore,
    CandidateOutOfRangeError,
    ExceptionNotFoundError,
    NoBatchError,
)
from ledger.security.rbac import (
    Permission,
    PermissionDeniedError,
    Principal,
    Role,
)

logger = logging.getLogger(__name__)

# Ceiling on a single batch request. Without it a caller can ask for ten million
# events and take the process down — a limit the server enforces, because the
# limit exists to protect the server.
MAX_BATCH_EVENTS = 50_000

_store = BatchStore()


def get_store() -> BatchStore:
    return _store


def reset_store() -> None:
    """Drop batch state. For tests and for a clean demo run."""
    global _store
    _store = BatchStore()


@asynccontextmanager
async def lifespan(_: FastAPI) -> AsyncIterator[None]:
    """Load credentials before the first request, or say clearly why there are none.

    Without this the token store starts empty and every request answers 401 with
    no indication of the cause — a server that is up, reachable, and completely
    unusable. The misconfiguration must be obvious at startup rather than
    diagnosed from a stream of identical 401s.

    We log and continue rather than exiting. A container that crashloops on a
    missing environment variable is harder to debug than one that is running and
    telling you what it needs, and `/health` stays useful either way.
    """
    store = TokenStore.from_env()

    if len(store) == 0 and os.environ.get("LEDGER_DEV_MODE") == "1":
        # A generated token, printed once, different every start. Deliberately
        # not a fixed default: a static development credential is the one that
        # reaches production and the one nobody ever rotates.
        token = secrets.token_urlsafe(24)
        store.add(token, "dev", Role.CONTROLLER)
        logger.warning(
            "LEDGER_DEV_MODE is on. Generated a single-session controller "
            "token: %s  (use as: Authorization: Bearer <token>)",
            token,
        )
    elif len(store) == 0:
        logger.error(
            "No API tokens configured, so every request will be rejected. Set "
            "LEDGER_API_TOKENS='<token>:<subject>:<role>' (roles: viewer, "
            "reviewer, controller, admin), or set LEDGER_DEV_MODE=1 to have one "
            "generated for this session."
        )
    else:
        logger.info("loaded %d API token(s)", len(store))

    configure(store)
    yield


app = FastAPI(
    lifespan=lifespan,
    title="Ledger",
    version="1.0.0",
    description=(
        "An autonomous finance controller. Reconciles transactions across "
        "gateway, settlement, bank and internal ledger, and reports every record "
        "it refuses to guess at as a typed exception with the money at risk.\n\n"
        "**Money is always integer minor units** (paise for INR) paired with a "
        "currency — never a float, never pre-formatted. Format at the display "
        "edge.\n\n"
        "Every endpoint requires a bearer token. Collections are cursor-paginated."
    ),
)


# --------------------------------------------------------------------------- #
# Error handling
# --------------------------------------------------------------------------- #


def _error(request: Request, code: str, message: str, status_code: int) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content=ErrorOut(
            code=code, message=message, correlation_id=correlation_id(request)
        ).model_dump(),
    )


@app.exception_handler(NoBatchError)
async def _no_batch(request: Request, exc: NoBatchError) -> JSONResponse:
    return _error(request, "no_batch", str(exc), status.HTTP_409_CONFLICT)


@app.exception_handler(ExceptionNotFoundError)
async def _not_found(
    request: Request, exc: ExceptionNotFoundError
) -> JSONResponse:
    return _error(
        request, "exception_not_found", "no such exception", status.HTTP_404_NOT_FOUND
    )


@app.exception_handler(CandidateOutOfRangeError)
async def _bad_candidate(
    request: Request, exc: CandidateOutOfRangeError
) -> JSONResponse:
    return _error(
        request, "candidate_out_of_range", str(exc), status.HTTP_400_BAD_REQUEST
    )


@app.exception_handler(PermissionDeniedError)
async def _denied(request: Request, exc: PermissionDeniedError) -> JSONResponse:
    return _error(
        request,
        "permission_denied",
        f"this operation requires {exc.permission.value}",
        status.HTTP_403_FORBIDDEN,
    )


@app.exception_handler(HTTPException)
async def _http_error(request: Request, exc: HTTPException) -> JSONResponse:
    """Map FastAPI's own errors into the same envelope as everything else.

    Without this the API speaks two error dialects: `{"detail": ...}` for a 401 or
    403 raised by a dependency, and `{code, message, correlation_id}` for a domain
    error. An integrator then has to parse both, and will get one of them wrong.
    A single shape is worth the handler.
    """
    codes = {
        401: "unauthenticated",
        403: "permission_denied",
        404: "not_found",
        409: "conflict",
        422: "invalid_request",
        429: "rate_limited",
    }
    response = _error(
        request,
        codes.get(exc.status_code, "request_failed"),
        str(exc.detail),
        exc.status_code,
    )
    # Preserve WWW-Authenticate and Retry-After, which clients act on.
    for key, value in (exc.headers or {}).items():
        response.headers[key] = value
    return response


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
    """Say nothing useful to the caller; say everything to the log.

    The message is deliberately generic. An unhandled error often carries the
    input that caused it, and that input is a financial record.
    """
    reference = correlation_id(request)
    logger.exception("unhandled error [correlation_id=%s]", reference)
    return _error(
        request,
        "internal_error",
        "the request could not be completed",
        status.HTTP_500_INTERNAL_SERVER_ERROR,
    )


# --------------------------------------------------------------------------- #
# Health
# --------------------------------------------------------------------------- #


@app.get("/health", tags=["ops"], summary="Liveness probe")
def health() -> dict[str, Any]:
    """Unauthenticated on purpose: a load balancer has no bearer token.

    It reports liveness only. Whether a batch has run, how many exceptions exist,
    and anything else about the data stays behind auth.
    """
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# Batches
# --------------------------------------------------------------------------- #


@app.post(
    "/v1/batches",
    tags=["batches"],
    response_model=BatchSummaryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Run a reconciliation batch",
)
def run_batch(
    seed: int = Query(1337, description="Data seed. 1337 is the held-out set."),
    events: int = Query(2_000, ge=1, le=MAX_BATCH_EVENTS),
    principal: Principal = Depends(require_permission(Permission.RUN_BATCH)),
    store: BatchStore = Depends(get_store),
) -> BatchSummaryOut:
    """Ingest, reconcile, raise exceptions, and score against ground truth.

    Gated on `run_batch` rather than a write permission generally: this is the
    most expensive operation the service offers, and rate limiting alone would
    not stop an authenticated viewer from running it repeatedly.
    """
    outcome = store.run(seed=seed, events=events)
    logger.info(
        "batch %s run by %s: %d records, %d exceptions",
        store.batch_id,
        principal.subject,
        outcome.ingestion.accepted,
        outcome.exceptions.count,
    )
    return service.to_summary_out(store.batch_id, outcome)


@app.get(
    "/v1/batches/current",
    tags=["batches"],
    response_model=BatchSummaryOut,
    summary="Summary of the current batch",
)
def current_batch(
    principal: Principal = Depends(require_permission(Permission.READ_TRANSACTIONS)),
    store: BatchStore = Depends(get_store),
) -> BatchSummaryOut:
    outcome = store.require_batch()
    return service.to_summary_out(store.batch_id, outcome)


# --------------------------------------------------------------------------- #
# Transactions
# --------------------------------------------------------------------------- #


@app.get(
    "/v1/transactions",
    tags=["transactions"],
    response_model=TransactionPage,
    summary="List canonical transactions",
)
def list_transactions(
    source: str | None = Query(None, description="Filter by source."),
    params: PageParams = Depends(page_params),
    principal: Principal = Depends(require_permission(Permission.READ_TRANSACTIONS)),
    store: BatchStore = Depends(get_store),
) -> TransactionPage:
    store.require_batch()
    records = store.transactions
    if source:
        records = [t for t in records if t.source.value == source]

    window, next_cursor = service.paginate(records, params.cursor, params.limit)
    return TransactionPage(
        total=len(records),
        count=len(window),
        next_cursor=encode_cursor(next_cursor) if next_cursor is not None else None,
        items=[service.to_transaction_out(t) for t in window],
    )


# --------------------------------------------------------------------------- #
# Exceptions
# --------------------------------------------------------------------------- #


@app.get(
    "/v1/exceptions",
    tags=["exceptions"],
    response_model=ExceptionPage,
    summary="List unresolved exceptions",
)
def list_exceptions(
    type: str | None = Query(None, description="Filter by exception type."),
    severity: str | None = Query(None, description="Filter by severity."),
    params: PageParams = Depends(page_params),
    principal: Principal = Depends(require_permission(Permission.READ_EXCEPTIONS)),
    store: BatchStore = Depends(get_store),
) -> ExceptionPage:
    """The honest list. Sorted by money at risk, descending.

    Triage order is the point of this endpoint: a reviewer with an hour should
    spend it on the largest exposure, not on whatever the engine happened to
    raise first.
    """
    outcome = store.require_batch()
    items = list(outcome.exceptions.exceptions)
    if type:
        items = [e for e in items if e.type.value == type]
    if severity:
        items = [e for e in items if e.severity.value == severity]
    items.sort(key=lambda e: e.amount_at_risk_minor, reverse=True)

    window, next_cursor = service.paginate(items, params.cursor, params.limit)
    return ExceptionPage(
        total=len(items),
        count=len(window),
        next_cursor=encode_cursor(next_cursor) if next_cursor is not None else None,
        items=[service.to_exception_out(e) for e in window],
    )


@app.get(
    "/v1/exceptions/{exception_id}",
    tags=["exceptions"],
    response_model=ExceptionOut,
    summary="One exception, with its candidates",
)
def get_exception(
    exception_id: str,
    principal: Principal = Depends(require_permission(Permission.READ_EXCEPTIONS)),
    store: BatchStore = Depends(get_store),
) -> ExceptionOut:
    store.require_batch()
    return service.to_exception_out(store.exception(exception_id))


@app.post(
    "/v1/exceptions/{exception_id}/resolve",
    tags=["exceptions"],
    response_model=ResolveResponse,
    summary="Resolve an exception",
)
def resolve_exception(
    exception_id: str,
    body: ResolveRequest,
    principal: Principal = Depends(require_permission(Permission.RESOLVE_EXCEPTION)),
    store: BatchStore = Depends(get_store),
) -> ResolveResponse:
    """Record how an exception was settled.

    Two permissions, deliberately. `resolve_exception` admits you here; supplying
    `accept_candidate_index` additionally requires `approve_money_movement`,
    because confirming a grouping asserts where money went. A reviewer can work
    the queue; only a controller can say the money landed a particular way.

    Idempotent on `idempotency_key`: a retry is acknowledged, not re-applied.
    """
    store.require_batch()
    return store.resolve(
        exception_id=exception_id,
        principal=principal,
        resolution=body.resolution,
        accept_candidate_index=body.accept_candidate_index,
        idempotency_key=body.idempotency_key,
    )


# --------------------------------------------------------------------------- #
# Matches and audit
# --------------------------------------------------------------------------- #


@app.get(
    "/v1/matches",
    tags=["matches"],
    response_model=MatchPage,
    summary="List reconciled matches with their reasons",
)
def list_matches(
    tier: str | None = Query(None, description="Filter by cascade tier."),
    params: PageParams = Depends(page_params),
    principal: Principal = Depends(require_permission(Permission.READ_TRANSACTIONS)),
    store: BatchStore = Depends(get_store),
) -> MatchPage:
    outcome = store.require_batch()
    items = list(outcome.reconciliation.matches)
    if tier:
        items = [m for m in items if m.tier.value == tier]

    window, next_cursor = service.paginate(items, params.cursor, params.limit)
    return MatchPage(
        total=len(items),
        count=len(window),
        next_cursor=encode_cursor(next_cursor) if next_cursor is not None else None,
        items=[service.to_match_out(m) for m in window],
    )


@app.get(
    "/v1/audit",
    tags=["audit"],
    summary="The decision trail for the current batch",
)
def list_audit(
    params: PageParams = Depends(page_params),
    principal: Principal = Depends(require_permission(Permission.READ_AUDIT)),
    store: BatchStore = Depends(get_store),
) -> dict[str, Any]:
    """Every automated decision, reproducible.

    Restricted to reviewers and above: the trail names counterparty references
    and amounts, which is more than a read-only viewer needs.
    """
    store.require_batch()
    entries = list(store.audit.read())
    window, next_cursor = service.paginate(entries, params.cursor, params.limit)
    return {
        "total": len(entries),
        "count": len(window),
        "next_cursor": (
            encode_cursor(next_cursor) if next_cursor is not None else None
        ),
        "items": window,
    }


@app.get("/v1/me", tags=["ops"], summary="Who am I and what may I do")
def whoami(principal: Principal = Depends(current_principal)) -> dict[str, Any]:
    """Lets a client discover its own permissions rather than probing for 403s."""
    return {
        "subject": principal.subject,
        "role": principal.role.value,
        "permissions": sorted(p.value for p in principal.permissions),
    }


__all__ = ["MAX_BATCH_EVENTS", "app", "get_store", "reset_store"]
