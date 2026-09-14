"""The one place a request is checked against a privilege.

Before this module every gated route called its own private ``_require_admin``, and the
answer was always the same question -- "is the role exactly the string ``admin``?" That is
a question with no room for a second answer. Adding a moderator meant either pretending a
moderator was an admin, which would hand it user management, or writing a second gate and a
second parallel list of which routes it protected, which is how the two copies of
``_require_admin`` came to exist in the first place.

There are two gates here, and they answer two different questions.

**What may this caller do?** A route names the one capability it needs::

    @router.delete("/confessions/{confession_id}")
    async def delete_confession(
        confession_id: str,
        db: Database = Depends(get_db),
        _: AccountDetails = Depends(require_capability(Capability.CONFESSIONS_DELETE)),
    ):

**May this caller be here at all?** A participant route names the table its caller has to
live in::

    @router.post("/matchmaking/find")
    async def find_match(
        _: UserDetails = Depends(require_participant),
    ):

The second gate is what replaced the role checks. A staff account has no row in ``users``,
so a feature that reads ``users`` cannot see it, cannot match it, and cannot accept a write
naming it. The dependency makes that explicit at the edge rather than leaving it to be
rediscovered in each service.

Both resolve the caller through ``get_current_user``, which reads the account table from
PostgreSQL on every request. A change to an account therefore takes effect on the next call
rather than when some token happens to expire.
"""

from typing import Optional

from fastapi import Depends, HTTPException, status

from ..models import AccountDetails, UserDetails
from ..accounts import AccountType, Capability, has_capability, normalize_account_type
from .auth_service import get_current_user, get_current_user_optional

# One wording for every refusal, so a client cannot tell which gate stopped it and so the
# message cannot drift between routes. It names no account table and no capability: a
# caller learns that they may not do this, not what they would have to become.
FORBIDDEN_DETAIL = "You do not have permission to access this resource"


def require_capability(capability: Capability):
    """Build a dependency that admits callers holding ``capability``.

    The returned callable is a real FastAPI dependency, so it is declared in the route's
    signature rather than called inside the handler body. That matters: as a declared
    dependency the check runs before the handler, so a handler cannot be reached without
    passing it, and the requirement is visible in the OpenAPI schema next to the operation
    it protects.
    """

    def dependency(current_user: AccountDetails = Depends(get_current_user)) -> AccountDetails:
        # An unrecognised account table resolves to no capabilities at all, so a value that
        # somehow holds something unrecognised is refused here exactly as a participant
        # would be.
        if not has_capability(current_user.account_type, capability):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=FORBIDDEN_DETAIL,
            )
        return current_user

    # Carried on the callable purely so the route's signature reads as a sentence and so
    # FastAPI's generated dependency name distinguishes two capabilities on one operation.
    dependency.__name__ = f"require_{capability.value.replace(':', '_')}"
    return dependency


def require_account_type(account_type: AccountType):
    """Build a dependency that admits only callers whose row is in ``account_type``.

    Used to keep staff out of the participant surface. The check is on the table rather than
    on a privilege, so it cannot be widened by a capability change: no capability anywhere in
    the system grants a staff account a row in ``users``, and this dependency is the one
    place that says so out loud.
    """

    def dependency(current_user: AccountDetails = Depends(get_current_user)) -> UserDetails:
        if normalize_account_type(current_user.account_type) != account_type.value:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=FORBIDDEN_DETAIL,
            )
        # Returned unchanged, and that is correct rather than lazy: ``get_current_user``
        # already resolves a ``users`` row to the participant model and a staff row to the
        # shared model, so by the time this branch is reached the caller *is* a
        # ``UserDetails``. The annotation is what tells FastAPI -- and a reader -- that a
        # participant handler can rely on the participant fields being present.
        return current_user

    dependency.__name__ = f"require_account_{account_type.value}"
    return dependency


# Matchmaking, confessions, love notes, conversations, messaging, notifications and profile
# are all participant features. Naming the dependency rather than the argument means a route
# that forgets the gate fails type-checking-and-review rather than quietly admitting staff.
require_participant = require_account_type(AccountType.USERS)


def get_current_participant_optional(
    current_user: Optional[AccountDetails] = Depends(get_current_user_optional),
) -> Optional[UserDetails]:
    """Resolve the caller if, and only if, they are a participant.

    For the one participant surface that is readable while signed out -- the confession
    feed -- and which therefore cannot use ``require_participant``, which would turn a public
    read into a 403 for anonymous visitors.

    A signed-in staff account is deliberately resolved to ``None`` rather than passed
    through. It is not an error: staff have no row in ``users``, so the only thing that
    identity could do on this route is mark the feed as read-by-someone, and a staff
    account's id matches no confession author. Collapsing it to anonymous makes that explicit
    instead of leaving a participant-typed value with no participant behind it.
    """
    if current_user is None:
        return None
    if normalize_account_type(current_user.account_type) != AccountType.USERS.value:
        return None
    return current_user
