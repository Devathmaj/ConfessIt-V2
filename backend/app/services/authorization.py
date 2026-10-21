"""Centralize capability checks for staff and participant routes.

Both dependencies resolve the current account from PostgreSQL on each request.
"""

from typing import Optional

from fastapi import Depends, HTTPException, status

from ..models import AccountDetails, UserDetails
from ..accounts import AccountType, Capability, has_capability, normalize_account_type
from .auth_service import get_current_user, get_current_user_optional

# One wording for every refusal, so a client cannot tell which gate stopped it and the
# message cannot drift between routes. It names no table and no capability.
FORBIDDEN_DETAIL = "You do not have permission to access this resource"


def require_capability(capability: Capability):
    """Build a dependency that admits callers holding ``capability``.

    It is a real FastAPI dependency declared in the route signature, so the check runs
    before the handler and appears in the OpenAPI schema.
    """

    def dependency(current_user: AccountDetails = Depends(get_current_user)) -> AccountDetails:
        # An unrecognised table resolves to no capabilities, so it is refused here.
        if not has_capability(current_user.account_type, capability):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=FORBIDDEN_DETAIL,
            )
        return current_user

    # Named so the route signature reads as a sentence and OpenAPI distinguishes two
    # capabilities on one operation.
    dependency.__name__ = f"require_{capability.value.replace(':', '_')}"
    return dependency


def require_account_type(account_type: AccountType):
    """Build a dependency that admits only callers whose row is in ``account_type``.

    Keeps staff out of the participant surface. The check is on the table, not a
    privilege, so no capability change can widen it.
    """

    def dependency(current_user: AccountDetails = Depends(get_current_user)) -> UserDetails:
        if normalize_account_type(current_user.account_type) != account_type.value:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=FORBIDDEN_DETAIL,
            )
        # Already a UserDetails: get_current_user maps a users row to the participant
        # model, and the annotation tells FastAPI the participant fields are present.
        return current_user

    dependency.__name__ = f"require_account_{account_type.value}"
    return dependency


# Naming the dependency rather than the argument makes a route that forgets the gate fail
# type-checking and review rather than quietly admitting staff.
require_participant = require_account_type(AccountType.USERS)


def get_current_participant_optional(
    current_user: Optional[AccountDetails] = Depends(get_current_user_optional),
) -> Optional[UserDetails]:
    """Resolve the caller if, and only if, they are a participant.

    For the public confession feed, which cannot use ``require_participant`` without
    turning anonymous reads into 403s. A signed-in staff account resolves to ``None``:
    it has no ``users`` row, and its id matches no confession author.
    """
    if current_user is None:
        return None
    if normalize_account_type(current_user.account_type) != AccountType.USERS.value:
        return None
    return current_user
