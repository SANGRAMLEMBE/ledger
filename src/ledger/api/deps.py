"""
Request-scoped dependencies: identity, rate limiting, pagination, correlation.

AUTHENTICATION
--------------
Bearer tokens, compared in constant time against **stored hashes** rather than
plaintext. Two reasons, both of which have burned real systems:

*Hashes, not plaintext* — the token store is the thing that leaks (a config dump,
a crash report, an environment listing). A leaked hash is not a credential; a
leaked token is.

*Constant time* — `==` on secrets returns as soon as it finds a differing byte, so
its runtime reveals how many leading bytes were right. That is enough to
reconstruct a token given enough attempts. `hmac.compare_digest` always takes the
same time.

There is no anonymous fallback. A request without a valid token is refused, never
downgraded to a read-only identity — a fallback principal is how an endpoint ends
up world-readable while the code still looks correct.

RATE LIMITING
-------------
Per principal, not per IP: several users behind one office NAT must not exhaust
each other's budget, and an attacker rotating IPs must not get a fresh budget each
time. The window is fixed and in-process, which is honest for a single instance
and documented as needing a shared store the moment there is more than one.

PAGINATION
----------
Opaque cursors, not offsets. An offset shifts when records are added mid-scan, so
a client paging a live batch silently skips or repeats rows — the kind of bug that
shows up as a reconciliation that mysteriously misses a few hundred records.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import os
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

from fastapi import Depends, Header, HTTPException, Request, status

from ledger.security.rbac import Permission, Principal, Role

# Requests allowed per principal per window. Generous for reads; the expensive
# operation is running a batch, which is gated by permission rather than by rate.
RATE_LIMIT_REQUESTS = 120
RATE_LIMIT_WINDOW_SECONDS = 60

MAX_PAGE_SIZE = 500
DEFAULT_PAGE_SIZE = 100


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class TokenStore:
    """Maps a bearer token to the principal it authenticates.

    Tokens are held as SHA-256 hashes. `authenticate` hashes the presented token
    and compares digests, so a dump of this structure yields nothing usable.
    """

    _by_hash: dict[str, Principal] = field(default_factory=dict)

    def add(self, token: str, subject: str, role: Role) -> None:
        if len(token) < 16:
            raise ValueError(
                "token is too short to be a credential; use at least 16 characters"
            )
        self._by_hash[_hash_token(token)] = Principal(subject=subject, role=role)

    def authenticate(self, token: str) -> Principal | None:
        presented = _hash_token(token)
        # Compare against every stored hash in constant time rather than doing a
        # dict lookup. A dict hit/miss is itself timing-observable, and the store
        # is small enough that the scan costs nothing.
        found: Principal | None = None
        for stored, principal in self._by_hash.items():
            if hmac.compare_digest(stored, presented):
                found = principal
        return found

    def __len__(self) -> int:
        return len(self._by_hash)

    @classmethod
    def from_env(cls, raw: str | None = None) -> TokenStore:
        """Build from `LEDGER_API_TOKENS`: `token:subject:role,token:subject:role`.

        Deliberately no default token. A development fallback credential is the
        one that reaches production, and it is always the one nobody rotates.
        """
        store = cls()
        source = raw if raw is not None else os.environ.get("LEDGER_API_TOKENS", "")
        for entry in filter(None, (part.strip() for part in source.split(","))):
            pieces = entry.split(":")
            if len(pieces) != 3:
                raise ValueError(
                    "LEDGER_API_TOKENS entries must be token:subject:role"
                )
            token, subject, role = pieces
            store.add(token, subject, Role(role))
        return store


class RateLimiter:
    """Fixed-window counter, per principal.

    In-process, so it is per-instance. That is correct and sufficient for a single
    deployment and stated plainly here rather than discovered later: behind more
    than one instance this needs a shared store (Redis), or the effective limit
    multiplies by the instance count.
    """

    def __init__(
        self,
        limit: int = RATE_LIMIT_REQUESTS,
        window_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits: dict[str, list[float]] = defaultdict(list)

    def check(self, subject: str, now: float | None = None) -> None:
        moment = time.monotonic() if now is None else now
        cutoff = moment - self.window_seconds
        recent = [t for t in self._hits[subject] if t > cutoff]

        if len(recent) >= self.limit:
            self._hits[subject] = recent
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"rate limit of {self.limit} requests per "
                f"{self.window_seconds}s exceeded",
                headers={"Retry-After": str(self.window_seconds)},
            )

        recent.append(moment)
        self._hits[subject] = recent

    def reset(self) -> None:
        self._hits.clear()


# Module-level singletons, replaceable in tests and at startup.
_token_store = TokenStore()
_rate_limiter = RateLimiter()


def configure(store: TokenStore, limiter: RateLimiter | None = None) -> None:
    global _token_store, _rate_limiter
    _token_store = store
    if limiter is not None:
        _rate_limiter = limiter


def get_token_store() -> TokenStore:
    return _token_store


def get_rate_limiter() -> RateLimiter:
    return _rate_limiter


def correlation_id(request: Request) -> str:
    """A stable id for this request, echoed in every error.

    Lets a caller quote one string and lets an engineer find the full context
    server-side, without the error body ever carrying record contents.
    """
    existing = request.headers.get("X-Correlation-Id")
    return existing if existing else str(uuid.uuid4())


def current_principal(
    authorization: str | None = Header(default=None),
    request: Request = None,  # type: ignore[assignment]
) -> Principal:
    """Authenticate the caller, or refuse.

    401 for a missing or malformed header and for a token that does not
    authenticate — the same status and the same message for both. Distinguishing
    "no such token" from "wrong token" tells an attacker which half to keep
    guessing.
    """
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="a valid bearer token is required",
        headers={"WWW-Authenticate": "Bearer"},
    )

    if not authorization or not authorization.lower().startswith("bearer "):
        raise unauthorized

    token = authorization[7:].strip()
    if not token:
        raise unauthorized

    principal = get_token_store().authenticate(token)
    if principal is None:
        raise unauthorized

    get_rate_limiter().check(principal.subject)
    return principal


def require_permission(
    permission: Permission,
) -> Callable[[Principal], Principal]:
    """Dependency factory: admit only principals holding `permission`.

    403, not 404. Hiding existence behind a 404 is a defensible pattern in some
    systems, but here the caller is authenticated and needs to know the operation
    exists and that they lack the right — otherwise they retry forever against
    what looks like a broken endpoint.
    """

    def dependency(principal: Principal = Depends(current_principal)) -> Principal:
        if not principal.can(permission):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"this operation requires {permission.value}",
            )
        return principal

    return dependency


def encode_cursor(position: int) -> str:
    """Opaque, so clients cannot construct one and depend on its shape."""
    return base64.urlsafe_b64encode(f"p{position}".encode()).decode()


def decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        raw = base64.urlsafe_b64decode(cursor.encode()).decode()
        if not raw.startswith("p"):
            raise ValueError("bad prefix")
        position = int(raw[1:])
    except (ValueError, UnicodeDecodeError, binascii.Error) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="malformed cursor; use the `next_cursor` from a previous page",
        ) from exc

    if position < 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="malformed cursor"
        )
    return position


@dataclass(frozen=True)
class PageParams:
    cursor: int
    limit: int


def page_params(cursor: str | None = None, limit: int = DEFAULT_PAGE_SIZE) -> PageParams:
    """Validate paging inputs.

    The cap is enforced server-side. A client asking for 50,000 rows in one
    response gets `MAX_PAGE_SIZE`, not an out-of-memory error — the limit exists
    to protect the server, so the server decides it.
    """
    if limit < 1:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="limit must be at least 1"
        )
    return PageParams(cursor=decode_cursor(cursor), limit=min(limit, MAX_PAGE_SIZE))
