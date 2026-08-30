"""
Role-based access control.

WHY THIS EXISTS BEFORE THE API DOES
-----------------------------------
Auth added after endpoints exist is auth that misses endpoints. The route that was
written first, or added in a hurry, is the one nobody goes back to protect. So the
permission model is built first and the API is written against it, which makes
"which permission does this route need?" a question you must answer to write the
route at all.

SEPARATION OF DUTIES IS THE POINT
---------------------------------
The interesting boundary is not read-versus-write. It is **resolving an exception**
versus **approving something that moves money**.

A reviewer works the queue all day: marking a bank credit as explained, dismissing
a suspected duplicate that turned out to be legitimate, annotating a break. None of
that changes what the books say happened.

Confirming a candidate grouping does. When a reviewer picks which settlement lines
composed a deposit, the ledger now asserts those payouts were received — and if the
grouping is wrong, real money has been mis-attributed with a human's name on it.
That needs a second, more senior pair of eyes, which is why
`APPROVE_MONEY_MOVEMENT` is held by controllers alone and is not implied by
`RESOLVE_EXCEPTION`.

ENFORCEMENT IS SERVER-SIDE, ALWAYS
----------------------------------
Hiding a button is not access control. Every permission check happens here, on the
server, on every request. A client that constructs the request by hand must be
refused exactly as firmly as one that clicks through the UI.

DENY BY DEFAULT
---------------
`ROLE_PERMISSIONS` grants explicitly. A role with no entry has no permissions
rather than all of them, and an unknown role is rejected rather than treated as
harmless. The failure mode of a permissive default is silent and total.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class Permission(str, enum.Enum):
    """What a principal may do. Granted explicitly, never inferred."""

    READ_TRANSACTIONS = "read:transactions"
    READ_EXCEPTIONS = "read:exceptions"
    READ_FORECAST = "read:forecast"
    READ_AUDIT = "read:audit"

    RESOLVE_EXCEPTION = "write:resolve_exception"
    # Work the queue: annotate, dismiss, mark explained. Does not change what the
    # books assert happened.

    APPROVE_MONEY_MOVEMENT = "write:approve_money_movement"
    # Confirm a candidate grouping, accept a suggested match, or otherwise make
    # the ledger assert that money moved a particular way. Deliberately NOT
    # implied by RESOLVE_EXCEPTION.

    RUN_BATCH = "write:run_batch"


class Role(str, enum.Enum):
    VIEWER = "viewer"
    REVIEWER = "reviewer"
    CONTROLLER = "controller"
    ADMIN = "admin"


_VIEWER = frozenset(
    {
        Permission.READ_TRANSACTIONS,
        Permission.READ_EXCEPTIONS,
        Permission.READ_FORECAST,
    }
)

# A reviewer reads everything a viewer does and can work the exception queue —
# but cannot confirm a grouping, because that asserts where money went.
_REVIEWER = _VIEWER | {Permission.RESOLVE_EXCEPTION, Permission.READ_AUDIT}

# A controller is the second pair of eyes: they can approve the money-moving
# decisions a reviewer prepared, and trigger a close.
_CONTROLLER = _REVIEWER | {
    Permission.APPROVE_MONEY_MOVEMENT,
    Permission.RUN_BATCH,
}

ROLE_PERMISSIONS: dict[Role, frozenset[Permission]] = {
    Role.VIEWER: _VIEWER,
    Role.REVIEWER: frozenset(_REVIEWER),
    Role.CONTROLLER: frozenset(_CONTROLLER),
    # Admin is deliberately not "every permission by wildcard". Listing them means
    # a new permission is NOT silently granted to admin the moment it is defined —
    # adding a capability should be a decision, not an inheritance.
    Role.ADMIN: frozenset(_CONTROLLER),
}


class PermissionDeniedError(PermissionError):
    """Raised when a principal lacks a required permission.

    Carries the subject and permission for the audit trail, and deliberately no
    detail about the resource — an error message is a side channel, and telling an
    unauthorised caller what exists is itself a small leak.
    """

    def __init__(self, subject: str, permission: Permission) -> None:
        self.subject = subject
        self.permission = permission
        super().__init__(f"{subject} lacks {permission.value}")


@dataclass(frozen=True)
class Principal:
    """Who is making the request."""

    subject: str
    role: Role

    @property
    def permissions(self) -> frozenset[Permission]:
        # An unknown role gets nothing rather than everything.
        return ROLE_PERMISSIONS.get(self.role, frozenset())

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

    def require(self, permission: Permission) -> None:
        """Raise unless the principal holds the permission."""
        if not self.can(permission):
            raise PermissionDeniedError(self.subject, permission)


# There is deliberately no ANONYMOUS principal. An unauthenticated request is
# rejected at the boundary rather than handed a default identity — a fallback
# principal is how an endpoint ends up readable by anyone who omits the header,
# and the mistake looks like working code.
