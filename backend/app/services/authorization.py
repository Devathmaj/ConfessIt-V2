"""The one place a request is checked against a privilege.

Before this module every gated route called its own private ``_require_admin``, and the
answer was always the same question -- "is the role exactly the string ``admin``?" That is a
question with no room for a second answer. Adding a moderator meant either pretending a
moderator was an admin, which would hand it user management, or writing a second gate and a
second parallel list of which routes it protected, which is how the two copies of
``_require_admin`` came to exist in the first place.

A route now names the one capability it needs::

    @router.delete("/confessions/{confession_id}")
    async def delete_confession(
        confession_id: str,
        db: Database = Depends(get_db),
        _: UserDetails = Depends(require_capability(Capability.CONFESSIONS_DELETE)),
    ):

The capability decides, and the mapping from role to capabilities lives only in
:mod:`app.roles`. Reading a route tells you what it does, and reading ``roles.py`` tells you
who can do it; neither requires tracing a role string to a comparison.

The dependency resolves the caller through ``get_current_user``, which reads the role from
PostgreSQL on every request. A role change therefore takes effect on the next call rather
than when some token happens to expire.
"""

from fastapi import Depends, HTTPException, status

from ..models import UserDetails
from ..roles import Capability, has_capability
from .auth_service import get_current_user

# One wording for every refusal, so a client cannot tell which gate stopped it and so the
# message cannot drift between routes. It names no role and no capability: a caller learns
# that they may not do this, not what they would have to become.
FORBIDDEN_DETAIL = "You do not have permission to access this resource"


def require_capability(capability: Capability):
    """Build a dependency that admits callers holding ``capability``.

    The returned callable is a real FastAPI dependency, so it is declared in the route's
    signature rather than called inside the handler body. That matters: as a declared
    dependency the check runs before the handler, so a handler cannot be reached without
    passing it, and the requirement is visible in the OpenAPI schema next to the operation
    it protects.
    """

    def dependency(current_user: UserDetails = Depends(get_current_user)) -> UserDetails:
        # An unrecognised role resolves to no capabilities at all, so a row that somehow
        # holds a bad value is refused here exactly as a plain user would be.
        if not has_capability(current_user.role, capability):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=FORBIDDEN_DETAIL,
            )
        return current_user

    # Carried on the callable purely so the route's signature reads as a sentence and so
    # FastAPI's generated dependency name distinguishes two capabilities on one operation.
    dependency.__name__ = f"require_{capability.value.replace(':', '_')}"
    return dependency
