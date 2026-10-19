# app/routers/staff_notifications.py

"""Staff notifications: the role-based inbox for admins and moderators.

One feed per role rather than per person. A moderator sees ``report`` notices
(every report filed) and ``love_note`` notices (every note awaiting approval);
an admin sees those plus live ``game`` alerts (a missing featured pick, a
manual-mode session waiting for its word, an empty Trivia Drop pipeline).

Three endpoints besides the feed: the category switches (three for an admin,
two for a moderator), the master on/off switch (the account's own
``notifications_enabled``), and marking one notice read. Preferences are
stored but deliberately not wired to delivery -- the feed returns every
notice regardless of the switches.
"""

from typing import Annotated, Any, Dict, Optional

from fastapi import APIRouter, Depends, Query, status
from pydantic import BaseModel, ConfigDict

from ..accounts import AccountType, normalize_account_type
from ..dependencies import get_db
from ..models import AccountDetails
from ..postgres import Database
from ..limits import PAGE_OFFSET_MAX
from ..services.auth_service import get_current_user
from ..services.staff_notification_service import (
    STAFF_NOTIFICATIONS_MAX_LIMIT,
    get_staff_notifications_enabled,
    get_staff_preferences,
    list_staff_notifications,
    mark_staff_notification_read,
    set_staff_notifications_enabled,
    unread_staff_count,
    update_staff_preferences,
)

router = APIRouter(prefix="/staff/notifications", tags=["Staff Notifications"])


def _require_staff(
    current_user: AccountDetails = Depends(get_current_user),
) -> AccountDetails:
    """Admit admin and moderator accounts; refuse participants."""
    from fastapi import HTTPException

    from ..services.authorization import FORBIDDEN_DETAIL

    if normalize_account_type(current_user.account_type) not in (
        AccountType.ADMIN.value,
        AccountType.MODERATOR.value,
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN, detail=FORBIDDEN_DETAIL
        )
    return current_user


class StaffPreferenceUpdate(BaseModel):
    """Patch body for the category switches. All fields optional; omitted stays.

    ``extra="forbid"`` so a misspelled switch is a 422 rather than a silently
    dropped edit.
    """

    model_config = ConfigDict(extra="forbid")

    reports_enabled: Optional[bool] = None
    love_notes_enabled: Optional[bool] = None
    games_enabled: Optional[bool] = None


class StaffEnabledUpdate(BaseModel):
    """Body for the master on/off switch ("Enable notifications")."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool


@router.get("")
def get_staff_notifications(
    current_user: Annotated[AccountDetails, Depends(_require_staff)],
    db: Database = Depends(get_db),
    skip: int = Query(0, ge=0, le=PAGE_OFFSET_MAX, description="How many matching notices to skip."),
    limit: int = Query(
        50,
        ge=1,
        le=STAFF_NOTIFICATIONS_MAX_LIMIT,
        description="How many matching notices to return.",
    ),
    q: Optional[str] = Query(None, max_length=100, description="Substring match on heading and body."),
    category: Optional[str] = Query(None, description="One of report, love_note, game."),
    unread_only: bool = Query(False, description="Only notices this account has not read."),
) -> Dict[str, Any]:
    """One page of this staff account's notices, newest first, with the total.

    Stored ``report``/``love_note`` rows page in SQL; live ``game`` alerts
    (admin only) are computed on the read and ride along on the first page.
    ``preferences``, ``notifications_enabled`` and ``categories`` travel beside
    the page so the screen can render its switches without a second round trip.
    """
    return list_staff_notifications(
        db, current_user, limit=limit, skip=skip, query=q,
        category=category, unread_only=unread_only,
    )


@router.get("/unread-count")
def get_unread_count(
    current_user: Annotated[AccountDetails, Depends(_require_staff)],
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """How many stored notices this account has not read, split by category."""
    return unread_staff_count(db, current_user)


@router.get("/preferences")
def get_preferences(
    current_user: Annotated[AccountDetails, Depends(_require_staff)],
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """This account's category switches plus its master on/off switch."""
    return {
        "preferences": get_staff_preferences(db, current_user),
        "notifications_enabled": get_staff_notifications_enabled(db, current_user),
    }


@router.put("/preferences")
def put_preferences(
    payload: StaffPreferenceUpdate,
    current_user: Annotated[AccountDetails, Depends(_require_staff)],
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """Flip which categories this account wants. Stored, not enforced on the feed."""
    updated = update_staff_preferences(
        db, current_user, payload.model_dump(exclude_unset=True)
    )
    return {"preferences": updated}


@router.put("/enabled")
def put_enabled(
    payload: StaffEnabledUpdate,
    current_user: Annotated[AccountDetails, Depends(_require_staff)],
    db: Database = Depends(get_db),
) -> Dict[str, Any]:
    """Flip this account's own ``notifications_enabled`` (the Enable button)."""
    stored = set_staff_notifications_enabled(db, current_user, enabled=payload.enabled)
    return {"notifications_enabled": stored}


@router.put("/{notification_id}/read")
def put_notification_read(
    notification_id: str,
    current_user: Annotated[AccountDetails, Depends(_require_staff)],
    db: Database = Depends(get_db),
):
    """Mark one stored notice as read for this account. Live game alerts are a no-op."""
    mark_staff_notification_read(db, current_user, notification_id=notification_id)
    return {"message": "Notification marked as read"}
