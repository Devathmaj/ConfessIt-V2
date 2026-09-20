# app/routers/auth.py

from fastapi import APIRouter, Depends, Request
from ..postgres import Database
from typing import Annotated

# Corrected: Use direct imports from the project root
from ..dependencies import get_db
from ..accounts import AccountType
from ..models import AccountDetails, UserDetails
from ..services.auth_service import (
    generate_magic_link_service,
    verify_magic_link_service,
    get_current_user
)
from pydantic import BaseModel

router = APIRouter(
    prefix="/auth",
    tags=["Authentication"]
)

class Token(BaseModel):
    access_token: str
    token_type: str
    redirect_url: str

@router.post("/login/{account_type}/magic")
async def generate_magic_link(
    account_type: AccountType,
    request: Request,
    db: Database = Depends(get_db),
):
    """
    Mint a magic link for an account in the named account table.

    One endpoint per account kind -- ``/auth/login/users/magic``,
    ``/auth/login/admin/magic``, ``/auth/login/moderator/magic`` -- and the path is what
    selects the table. A regno is unique only within a table, so the sign-in page a person
    used is the only thing that can say which account they mean when one regno exists as a
    participant and as staff at the same time. An unrecognised account type is a 404 from
    the path parser, not a default to some other table.
    """
    return await generate_magic_link_service(request, db, account_type.value)

@router.get("/login/magic/verify", response_model=Token)
async def verify_magic_link(token: str, request: Request, db: Database = Depends(get_db)):
    """
    Used to verify a magic link token and return a JWT access token.
    """
    return verify_magic_link_service(token, db, request)

# Deliberately not a response_model. The response is a ``UserDetails`` for a participant and
# an ``AccountDetails`` for a staff account, and a single model would have to declare every
# participant field optional to be valid for both -- which would let a staff response
# quietly carry a year and department the row does not have, and would stop the client from
# being able to tell a missing field from a null one. Declaring no model keeps the response
# equal to what the account actually is.
@router.get("/me")
async def read_users_me(current_user: Annotated[AccountDetails, Depends(get_current_user)]):
    """
    Fetch the details of the currently authenticated account.

    The shape follows the table: a participant gets the full profile, staff get the shared
    account fields plus the capabilities their table grants. The client uses the same
    ``account_type`` to decide which landing path and which screens are reachable, and the
    server re-checks all of it per request regardless.
    """
    payload = current_user.model_dump(by_alias=True)
    # ``profile_picture_url`` is already correct here: ``auth_service._account_payload`` derives it
    # from the stored Cloudinary identity when the account is resolved, so there is no URL in the
    # database to rewrite and nothing that could still hold a stale address from a previous build.
    if current_user.account_type == AccountType.USERS.value:
        return UserDetails(**payload)
    return AccountDetails(**payload)
