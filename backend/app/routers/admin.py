from typing import Annotated, Any, Dict, Optional
from sqlalchemy import func, or_, select, text, update

from ..postgres import new_id, is_valid_id
from ..limits import PAGE_OFFSET_MAX, USER_YEAR_MAX, USER_YEAR_MIN
from fastapi import APIRouter, Depends, File, HTTPException, Query, UploadFile, status, Body
from ..postgres import Database

from ..dependencies import get_db
from ..models import AccountDetails, AdminUserCreate, AdminUserUpdate, Year
from ..db_models import (ACCOUNT_MODELS, CaseClosedMessageRecord,
    CaseClosedMessageReportRecord, CaseClosedSessionRecord, ConfessionRecord,
    ConversationRecord, MessageRecord, MessageReportRecord,
    UserRecord, find_account_in_table, resolve_account_by_id)
from ..services.auth_service import retire_account_sessions
from ..accounts import (
    AccountType, Capability, account_cap, account_cap_lock_key,
    normalize_account_type,
)
from ..services.authorization import require_capability
from ..utils import ilike_contains
from .. import limits as app_limits
from ..logger import get_logger
from ..services.love_note_service import rotate_sender_love_note_images
from ..services.storage_service import storage_service
from ..services.admin_service import (
    ADMIN_USERS_MAX_LIMIT,
    CONFESSIONS_MAX_LIMIT,
    serialize_datetime,
    serialize_user_doc,
    _account_row_payload,
    get_admin_users_data,
    get_admin_confessions_data,
    get_all_confessions_data,
    get_confession_reports_data,
    get_admin_statistics_data,
)
from ..services.report_resolution_service import (
    resolve_case_closed_message_report,
    resolve_confession_report,
    resolve_message_report,
    resolve_status_filter,
)
from ..services.report_resolution_service import hide_confession as hide_confession_service
from ..services.report_resolution_service import hide_comment as hide_comment_service
from ..services.report_resolution_service import unhide_case_closed_message as unhide_case_closed_message_service
from ..services.report_resolution_service import unhide_comment as unhide_comment_service
from ..services.report_resolution_service import unhide_confession as unhide_confession_service
from ..services.report_resolution_service import unhide_message as unhide_message_service
from ..services.chat_evidence_service import (
    audit_evidence_access,
    decrypt_evidence,
    evidence_for_reports,
)
from ..services.content_safety_service import (
    AUTOMATED_REPORTER_DISPLAY_NAME,
    AUTOMATED_SOURCE,
)

router = APIRouter(prefix="/admin", tags=["Admin"])

logger = get_logger(__name__)

# Every account the list can hold, newest-looking first, ordered explicitly so the
# response is stable across calls: three unordered SELECTs concatenated would
# reshuffle the page wherever PostgreSQL picked a different plan.
_ACCOUNT_TABLE_ORDER = (AccountType.ADMIN, AccountType.MODERATOR, AccountType.USERS)


def _distinct_department_count(session) -> int:
    """How many distinct departments/divisions exist, case-insensitive.

    ``func.lower`` keeps "CSE" and "cse" as one department.
    """
    return session.scalar(
        select(func.count(func.distinct(func.lower(UserRecord.department))))
    ) or 0


def _department_value_exists(session, department: str, exclude_id: Any = None) -> bool:
    """Whether any participant row already carries ``department`` (case-insensitive)."""
    wanted = department.strip().lower()
    query = select(UserRecord.id).where(func.lower(UserRecord.department) == wanted)
    if exclude_id is not None:
        query = query.where(UserRecord.id != exclude_id)
    return session.scalar(query.limit(1)) is not None


def _ensure_department_capacity(
    session,
    new_department: str,
    exclude_id: Any = None,
    current_department: Any = None,
) -> None:
    """Refuse a brand-new department once ``MAX_DEPARTMENTS`` distinct values exist.

    Joining a department somebody already uses never counts against the cap --
    only a value no row carries opens a new slot. Renaming the last holder onto
    a new value is a swap, not growth, so it is allowed even at the ceiling.
    """
    if not isinstance(new_department, str) or not new_department.strip():
        return
    new_norm = new_department.strip().lower()
    if isinstance(current_department, str) and current_department.strip().lower() == new_norm:
        return
    if _department_value_exists(session, new_norm, exclude_id=exclude_id):
        return
    # An update that moves the *only* holder of its old department onto a new name keeps
    # the distinct count flat (one leaves as one arrives), so it is not new growth.
    if exclude_id is not None and isinstance(current_department, str) and current_department.strip():
        old_norm = current_department.strip().lower()
        if not _department_value_exists(session, old_norm, exclude_id=exclude_id):
            return
    # Read off the module on every call -- not captured at import -- so editing the one
    # number in ``backend/app/limits.py`` (or monkeypatching it in a test) takes effect
    # without a reload. There is no second copy of this cap anywhere.
    cap = app_limits.MAX_DEPARTMENTS
    distinct = _distinct_department_count(session)
    if distinct >= cap:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"The department/division limit has been reached "
                f"({distinct} of {cap}). No new departments or divisions "
                f"can be added. Use one of the existing departments, or raise "
                f"MAX_DEPARTMENTS in backend/app/limits.py."
            ),
        )

def _user_payload(user):
    if user is None: return None
    return {
        "id": str(user.id), "regno": user.regno, "name": user.name, "email": user.email,
        "bio": user.bio, "year": user.year, "department": user.department,
        "gender": user.gender,
        "profile_picture_url": storage_service.profile_delivery_url(
            user.profile_picture_public_id, user.profile_picture_format
        ),
        "interests": user.interests or [],
        "account_type": AccountType.USERS.value,
        "matchmaking_enabled": user.matchmaking_enabled,
        "notifications_enabled": user.notifications_enabled, "love_notes_receive_enabled": user.love_notes_receive_enabled,
        "love_notes_send_enabled": user.love_notes_send_enabled,
        "last_login_at": user.last_login_at, "last_login_ip": str(user.last_login_ip) if user.last_login_ip else None,
        "last_login_user_agent": user.last_login_user_agent, "last_matchmaking_at": user.last_matchmaking_at,
        "last_confession_at": user.last_confession_at,
        "last_profile_picture_at": user.last_profile_picture_at,
        "created_at": user.created_at, "updated_at": user.updated_at,
        "is_blocked": user.is_blocked,
    }


@router.get("/confessions", response_model=Dict[str, Any])
def get_all_confessions(
    skip: int = Query(0, ge=0, le=PAGE_OFFSET_MAX, description="How many matching confessions to skip."),
    limit: int = Query(50, ge=1, le=CONFESSIONS_MAX_LIMIT, description="How many matching confessions to return. Clamped so one request can never materialise the whole table with every comment, report and reactor row attached."),
    q: Optional[str] = Query(None, max_length=100, description="Case-insensitive substring match on the body, the recipient name and the sender's name, regno and email."),
    filter: str = Query("all", description="Narrow to a subset: all, reported, anonymous or with-comments."),
    sort: str = Query("recent", description="Order the page: recent, reports, comments or reactions."),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.CONFESSIONS_REVIEW))
) -> Dict[str, Any]:
    """Return one page of confessions for admin review.

    Everything a reviewer acts on is here, with one exception: the sender of an
    anonymous confession, withheld in the service so the identity is absent
    from the response itself, not merely hidden on screen. Search, filter, sort
    and paging are SQL WHERE/ORDER BY/LIMIT-OFFSET, and ``total`` is a COUNT
    under the same filter, so the backend reads at most one page.
    """
    return get_admin_confessions_data(db, skip=skip, limit=limit, query=q, filter_option=filter, sort=sort)


# Declared before ``/confessions/{confession_id}`` siblings so the literal
# ``search`` segment can never be read as a confession id on a future GET route.
@router.get("/confessions/search", response_model=Dict[str, Any])
def search_confessions_hybrid(
    q: str = Query(..., min_length=1, max_length=1000, description="Keyword + meaning query: lexical matches first, similar-in-meaning below."),
    skip: int = Query(0, ge=0, le=PAGE_OFFSET_MAX, description="Keyword page offset; the semantic section is top-K only and ignores it."),
    limit: int = Query(10, ge=1, le=CONFESSIONS_MAX_LIMIT, description="Keyword page size; semantic is capped at the configured top-K."),
    filter: str = Query("all", description="Narrow to a subset: all, reported, anonymous or with-comments. Applies to both sections."),
    sort: str = Query("recent", description="Order the keyword page: recent, reports, comments or reactions. Semantic stays relevance-ordered."),
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.CONFESSIONS_REVIEW))
) -> Dict[str, Any]:
    """Hybrid keyword + semantic search for staff review.

    Keyword is the existing lexical page unchanged (same sender-aware search,
    filter/sort/paging/total). Semantic runs the shared embed + vector-query
    pipeline with staff visibility -- removed rows included and flagged,
    anonymous senders withheld -- deduped against the keyword page and capped
    at the configured top-K, on the admin rate-limit bucket. Semantic failures
    return keyword results with ``semantic.status`` naming the reason.
    """
    from ..services.admin_service import get_admin_confessions_hybrid_data

    return get_admin_confessions_hybrid_data(
        db, query=q, filter_option=filter, sort=sort,
        skip=skip, limit=limit, rate_key=reviewer.regno,
    )


@router.delete("/confessions/{confession_id}")
def delete_confession(
    confession_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.CONFESSIONS_DELETE))
) -> Dict[str, Any]:
    """Remove a confession permanently, row and attached reports included.

    A hard delete, deliberately the odd one out: for content that must not
    remain in the database at all. The review screen offers the hide route
    instead, which keeps the row so the report trail stays intact.
    """
    if not is_valid_id(confession_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid confession id")

    with db.sessions.begin() as session:
        confession = session.get(ConfessionRecord, str(confession_id))
        if confession: session.delete(confession)
    if not confession:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Confession not found")
    return {"deleted": confession_id}


@router.patch("/confessions/{confession_id}/hide")
def hide_confession(
    confession_id: str,
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.CONFESSIONS_DELETE)),
) -> Dict[str, Any]:
    """Hide a confession from public view, keeping the row and everything attached to it.

    The write is the same one a ``removed`` report ruling performs -- ``removed_at``
    plus who stamped it -- so a screen hide and a report takedown land in identical
    state, and every participant-facing read filters that one column. The delete
    route stays for cases that need the row and its reports gone, but this screen
    offers no path to it. Hiding an already-hidden confession answers
    ``already_hidden``, not 409, so a double click is not an error toast. Manual
    hides are one-way; only Azure-hidden rows restore via the unhide route.
    """
    return hide_confession_service(db, confession_id, reviewer.regno)


@router.patch("/confessions/{confession_id}/unhide")
def unhide_confession(
    confession_id: str,
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.CONFESSIONS_DELETE)),
) -> Dict[str, Any]:
    """Restore an Azure-hidden confession to public view.

    Only content with ``removed_by_regno`` marking Azure Content Safety
    restores. Anything hidden by an admin/moderator answers 409 and stays
    hidden, so a reviewer cannot undo a colleague's decision here. Already
    visible rows answer ``already_visible`` instead of erroring.
    """
    return unhide_confession_service(db, confession_id, reviewer.regno)


@router.patch("/comments/{comment_id}/hide")
def hide_comment(
    comment_id: str,
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.CONFESSIONS_DELETE)),
) -> Dict[str, Any]:
    """Hide one comment from its confession, keeping the row and everything attached to it.

    Comment-level counterpart to the confession hide: the same soft write, the
    same ``removed_at`` every comment read filters on, so one bad comment in a
    hundred need not hide the confession wholesale. Declared under
    ``/admin/comments`` because a literal segment beside a path parameter can
    be read as that parameter's value -- a comment id read as a confession id
    is a 404 that looks like a missing row. Gated on ``confessions:delete``
    like ``hide_confession`` since it is the same job; a separate capability
    would mean keeping two names for one moderator privilege in step. Reports
    filed against the comment survive (hiding is a visibility change), and
    manual hides are one-way.
    """
    return hide_comment_service(db, comment_id, reviewer.regno)


@router.patch("/comments/{comment_id}/unhide")
def unhide_comment(
    comment_id: str,
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.CONFESSIONS_DELETE)),
) -> Dict[str, Any]:
    """Restore an Azure-hidden comment to public view.

    Same gate as confession unhide: only system-hidden rows restore, manual
    hides answer 409. The parent ``comment_count`` is incremented back.
    """
    return unhide_comment_service(db, comment_id, reviewer.regno)


# Keep the literal reports route before any future catch-all confession route; report
# review permissions remain separate from ordinary post review.


@router.get("/confessions/reports")
def get_confession_reports(
    status_filter: Optional[str] = Query(
        None, alias="status", description="Filter by status: pending, dismissed (or the older reviewed), removed"
    ),
    skip: int = Query(0, ge=0, le=PAGE_OFFSET_MAX),
    limit: int = Query(50, ge=1, le=200),
    q: Optional[str] = Query(None, max_length=100, description="Case-insensitive substring match on the reason, the reporter name and the reported content."),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.REPORTS_REVIEW))
) -> Dict[str, Any]:
    """Every report filed against a confession or one of its comments, newest first.

    Carries the reported content itself rather than an id, so "what did this
    actually say" is answerable here. Anonymous confession authors are
    withheld, as they are on the confession review screen.
    """
    return get_confession_reports_data(db, status_filter=status_filter, skip=skip, limit=limit, query=q)


@router.patch("/confessions/reports/{report_id}")
def update_confession_report(
    report_id: str,
    new_status: str = Query(..., alias="status", pattern="^(reviewed|dismissed|removed)$"),
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.REPORTS_MODERATE)),
) -> Dict[str, Any]:
    """Dismiss or remove a report; retain the audit row and accept legacy ``reviewed`` status."""
    return resolve_confession_report(db, report_id, new_status, reviewer.regno)


@router.get("/users", response_model=Dict[str, Any])
def get_all_users(
    skip: int = Query(0, ge=0, le=PAGE_OFFSET_MAX, description="How many matching participants to skip. Applies to the users table only when no account_type is given."),
    limit: int = Query(50, ge=1, le=ADMIN_USERS_MAX_LIMIT, description="How many matching participants to return. Clamped so one request can never materialise the whole table."),
    q: Optional[str] = Query(None, max_length=100, description="Case-insensitive substring match on name, regno, email (and department for participants)."),
    account_type: Optional[str] = Query(None, description="Restrict to one account table: users, admin or moderator."),
    year: Optional[int] = Query(None, ge=USER_YEAR_MIN, le=USER_YEAR_MAX, description="Participants only: exact year of study."),
    department: Optional[str] = Query(None, description="Participants only: exact department, case-insensitive."),
    sort: str = Query("name", description="name, name_desc, year, year_desc or removals_desc. Year/removals fall back to name on staff tables."),
    flagged: str = Query("all", description="Participants only: all, flagged (more than 5 removals) or ordinary."),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.USERS_REVIEW))
) -> Dict[str, Any]:
    """Return one page of accounts, from all three tables, for review.

    One endpoint because staff and participants are one population of accounts;
    the ``account_type`` on each document is which table it came from, and the
    search box does not have to know which table to search. Every filter is a
    SQL WHERE and every page a SQL LIMIT/OFFSET, with ``total`` a COUNT under
    the same WHERE, so the backend never reads more than one page.
    """
    return get_admin_users_data(
        db, skip=skip, limit=limit, query=q, account_type=account_type,
        year=year, department=department, sort=sort, flagged=flagged,
    )


@router.post("/users", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
def create_user(
    payload: AdminUserCreate,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.USERS_CREATE))
) -> Dict[str, Any]:
    """Create an account in the table named by ``account_type``.

    The same payload shape creates a participant or a staff account, so there is no
    separate admin-creation path to fall out of step with. ``year`` and ``department``
    are required only for a participant. The payload is identity plus cohort only --
    the picture, bio, interests and the four preference flags are absent, and
    ``AdminUserCreate`` forbids them by name so a curl sending one gets a 422 rather
    than a 201 for an insert that dropped it. Those columns still fill from the
    defaults declared in ``db_models``, and a staff account also gets its
    ``staff_notification_preferences`` row in the same transaction via the mapper hook.
    """

    account_type = payload.account_type
    model = ACCOUNT_MODELS[account_type.value]
    normalized_regno = payload.regno.strip()

    # Uniqueness is enforced per table, by PostgreSQL, and deliberately not across the
    # three tables: sign-in is category scoped and a session is pinned to that row for
    # its whole life, so the same regno can legitimately exist as a participant and
    # as staff -- each sign-in page resolves its own account.
    with db.sessions() as session:
        if find_account_in_table(session, account_type.value, normalized_regno) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this registration number already exists for this account type.",
            )
        # Email is unique within one account table and never across the three, like
        # regno: it identifies a person for contact, not a route into an account.
        normalized_email = payload.email.strip().lower()
        email_conflict = session.scalar(select(model.id).where(
            model.email == normalized_email
        ).limit(1))
        if email_conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists for this account type.",
            )

    # Shared flags use model defaults. Build only fields supported by the target
    # table so participant-only data cannot reach staff account rows.
    account_fields: Dict[str, Any] = {
        "regno": normalized_regno,
        "name": payload.name.strip(),
        "email": normalized_email,
    }
    if account_type is AccountType.USERS:
        account_fields.update({
            # No picture: both halves of the picture's identity are named rather than
            # left to a default, because "no picture" must be one state in two columns
            # -- a public_id without a format would render a broken image.
            "profile_picture_public_id": None,
            "profile_picture_format": None,
            "gender": payload.gender.strip(),
            # Written as separate columns rather than one joined string: matchmaking
            # groups by each half, and a stored "CSE 2" is groupable by neither.
            "year": payload.year,
            "department": payload.department.strip(),
        })


    with db.sessions.begin() as session:
        # Count and insert under a per-table advisory lock so concurrent requests
        # cannot exceed the account cap.
        session.execute(
            text("SELECT pg_advisory_xact_lock(:lock_key)"),
            {"lock_key": account_cap_lock_key(account_type.value)},
        )
        existing_count = session.scalar(select(func.count()).select_from(model)) or 0
        cap = account_cap(account_type.value)
        if existing_count >= cap:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=(
                    f"The {account_type.value} account limit has been reached "
                    f"({existing_count} of {cap}). No further {account_type.value} accounts "
                    f"can be created. Blocked accounts count towards this limit, so unblocking "
                    f"or blocking an existing account will not free a slot."
                ),
            )

        # Departments are free text, so the ``MAX_DEPARTMENTS`` ceiling counts distinct
        # ``users.department`` values; the users-table advisory lock above serialises
        # the count against the insert, so concurrent creates cannot both open a slot.
        if account_type is AccountType.USERS:
            _ensure_department_capacity(session, account_fields["department"])

        user = model(**account_fields)
        session.add(user)
        session.flush()
        created_doc = _account_row_payload(account_type.value, user)
    return serialize_user_doc(created_doc)


@router.post(
    "/users/bulk-upload",
    response_model=Dict[str, Any],
    status_code=status.HTTP_201_CREATED,
)
async def bulk_upload_users(
    file: UploadFile = File(
        ...,
        description=(
            "A .csv or .xlsx file whose first row names registration number, email, full "
            "name, gender, year and department. At most 1000 participant rows. The upload "
            "is all-or-nothing."
        ),
    ),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.USERS_CREATE)),
) -> Dict[str, Any]:
    """Create many participant accounts from one CSV or XLSX file.

    Participants only -- there is no ``account_type`` parameter, so this route
    cannot mint an admin or a moderator. The file is parsed and validated in
    full before the transaction opens; inside it the users-table advisory lock
    is taken and the upload is refused whole if it would exceed
    ``MAX_USER_COUNT``, so a file that crosses the ceiling never leaves a
    partial intake behind. The same all-or-nothing rule covers duplicates,
    invalid rows and ``MAX_DEPARTMENTS``. The 1000-row ceiling is an
    application limit independent of ``MAX_USER_COUNT``; over-long files are
    refused, never truncated.
    """
    # Imported here so the spreadsheet parser is loaded only by the endpoint that
    # needs it, not on every admin process start.
    from ..services.bulk_user_service import create_participants_bulk, read_bulk_upload

    rows = await read_bulk_upload(file)
    return create_participants_bulk(db, rows)


@router.put("/users/{user_id}/block")
def set_user_block_state(
    user_id: str,
    blocked: bool = Query(..., alias="blocked"),
    db: Database = Depends(get_db),
    current_admin: AccountDetails = Depends(require_capability(Capability.USERS_BLOCK))
) -> Dict[str, Any]:
    """Block or unblock an account; admins cannot be blocked (also enforced by a DB constraint).

    Blocking retires sessions in the same transaction and rotates participant love-note
    images after commit to invalidate old signed URLs. Image rotation is best-effort.
    """
    if not is_valid_id(user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user id")

    if str(current_admin.id) == user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot change your own block status")

    became_blocked = False
    blocked_type: Optional[str] = None
    with db.sessions.begin() as session:
        found = resolve_account_by_id(session, user_id)
        if found:
            target_type, row = found
            if blocked and normalize_account_type(target_type) == AccountType.ADMIN.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="An admin account cannot be blocked.",
                )
            became_blocked = blocked and not row.is_blocked
            blocked_type = normalize_account_type(target_type)
            row.is_blocked = blocked
            if became_blocked:
                # Same transaction as the flag flip -- a block that revokes nothing
                # means the session table disagrees with the block. Shared with
                # the automatic block at the removal threshold.
                retire_account_sessions(session, str(user_id), blocked_type)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    if became_blocked and blocked_type == AccountType.USERS.value:
        # Only participants can own note images. Run after the commit so a
        # rotation failure can never roll back (or block) the flag flip.
        try:
            rotate_sender_love_note_images(db, user_id)
        except Exception as exc:
            logger.error(
                "Love note image rotation failed for newly blocked account %s: %s",
                user_id, exc, exc_info=True,
            )
    return {"id": user_id, "is_blocked": blocked}


@router.put("/users/{user_id}", response_model=Dict[str, Any])
def update_user_details(
    user_id: str,
    update: AdminUserUpdate,
    db: Database = Depends(get_db),
    current_admin: AccountDetails = Depends(require_capability(Capability.USERS_UPDATE))
) -> Dict[str, Any]:
    """Edit account identity fields without changing account type or block state.

    Admins may edit only their own admin account. Participant-only fields are validated
    separately; blank values for required columns are treated as unchanged.
    """
    if not is_valid_id(user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user id")

    # NOT NULL columns on the account tables, so a ``None`` here is never a value
    # to write. Year and department join name and regno: they are the two halves
    # of the old ``class_name`` a matchmaking cohort needs.
    not_null_fields = frozenset({
        "regno", "name", "email", "gender", "year", "department",
    })
    # exclude_unset distinguishes "field absent" (leave alone) from "field sent
    # as null/blank" (None after the validator). Both spellings are dropped for
    # NOT NULL columns, so a cleared input can never become a NULL write; it is
    # read as "leave it alone".
    update_payload = {
        key: value
        for key, value in update.model_dump(exclude_unset=True).items()
        if not (value is None and key in not_null_fields)
    }

    if not update_payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No changes provided")

    with db.sessions.begin() as session:
        found = resolve_account_by_id(session, user_id)
        if found:
            account_type, account = found
            # Another admin's row is not this route's to edit. Compared by id, not
            # regno -- the id is what the session was minted with, a regno can be
            # renamed. Fail-closed on a missing caller id.
            if (
                normalize_account_type(account_type) == AccountType.ADMIN.value
                and str(account.id) != str(current_admin.id)
            ):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="An admin can edit only their own profile.",
                )
            # Drop participant-only fields for staff; email is shared across account tables.
            participant_only = ("gender", "year", "department")
            if account_type != AccountType.USERS.value:
                update_payload = {
                    key: value for key, value in update_payload.items()
                    if key not in participant_only
                }
            # A department edit introducing a value nobody uses opens a new
            # department/division slot, so it is held to MAX_DEPARTMENTS,
            # serialised behind the users-table advisory lock as at creation.
            if account_type == AccountType.USERS.value and "department" in update_payload:
                session.execute(
                    text("SELECT pg_advisory_xact_lock(:lock_key)"),
                    {"lock_key": account_cap_lock_key(AccountType.USERS.value)},
                )
                _ensure_department_capacity(
                    session,
                    update_payload["department"],
                    exclude_id=account.id,
                    current_department=account.department,
                )
            for key, value in update_payload.items():
                setattr(account, key, value)
            updated_doc = _account_row_payload(account_type, account)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return serialize_user_doc(updated_doc)




@router.get("/stats", response_model=Dict[str, Any])
def get_admin_statistics(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.STATS_READ))
) -> Dict[str, Any]:
    """Provide high-level metrics for the admin dashboard."""
    return get_admin_statistics_data(db)


@router.get("/config", response_model=Dict[str, Any])
def read_app_configuration(
    _: AccountDetails = Depends(require_capability(Capability.APP_CONFIG_READ)),
    db: Database = Depends(get_db)
) -> Dict[str, Any]:
    """Return the application's operator-settable configuration.

    ``settings`` is deliberately empty for now: platform settings are still
    environment variables read by ``app/config.py`` at import time, so there is
    nothing stored to report yet. ``cohorts`` is every ``(year, department)``
    pair a participant occupies and ``slots`` the pairings between them; the
    key was ``classes`` while participants carried a single ``class_name``
    column, a name that would otherwise outlive its meaning.
    """
    from ..services.matchmaking_slot_service import get_all_slots_service, list_participant_cohorts

    cohorts = list_participant_cohorts(db)
    slots = get_all_slots_service(db)

    return {
        "settings": {},
        "cohorts": cohorts,
        "slots": slots,
    }


@router.post("/config/matchmaking-slot", response_model=Dict[str, Any])
def create_matchmaking_slot(
    year_a: Annotated[Year, Body(..., description="Year of the first cohort. Must be between USER_YEAR_MIN and USER_YEAR_MAX.")],
    year_b: Annotated[Year, Body(..., description="Year of the second cohort. Must be between USER_YEAR_MIN and USER_YEAR_MAX.")],
    department_a: str = Body(..., description="Department or division of the first cohort. Required."),
    department_b: str = Body(..., description="Department or division of the second cohort. Required."),
    group_key: Optional[str] = Body(
        None,
        description=(
            "UUID naming the editor slot box this pair belongs to. Optional: a pair is valid on its "
            "own, and rows written before this existed have none. It does not affect what may match."
        ),
    ),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.APP_CONFIG_READ))
) -> Dict[str, Any]:
    """Record that one cohort is allowed to match with another.

    Both sides are ``(year, department)`` pairs rather than one joined string:
    a joined name could not be compared against either half of a participant's
    own row, and two identical sides mean the cohort may match itself.
    ``year_a``/``year_b`` use ``Annotated[Year, Body(...)]`` deliberately: the
    ``Year = Body(...)`` spelling **drops** the annotation's bound (a parameter
    whose default is an explicit ``Body`` takes that as its field info), which
    let year 13 through to be refused by a CHECK constraint as a 500; the
    ``Annotated`` spelling keeps the bound *and* names the field in the 422.
    ``group_key`` is a plain ``str`` so a malformed one answers a 400 naming the
    field, not a 422 whose ``loc`` points at the body.
    """
    from ..services.matchmaking_slot_service import create_matchmaking_slot_service

    slot = create_matchmaking_slot_service(
        db, year_a, department_a, year_b, department_b, group_key
    )
    return slot


@router.delete("/config/matchmaking-slot", response_model=Dict[str, Any])
def delete_matchmaking_slot(
    slot_id: str = Body(..., embed=True),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.APP_CONFIG_READ))
) -> Dict[str, Any]:
    """Delete a matchmaking slot by ID.

    ``embed=True`` so the id arrives as ``{"slot_id": "..."}`` instead of the
    whole body being read as one bare scalar; without it the obvious JSON
    object -- what the POST above takes -- is refused with a 422 naming
    ``body``, which reads as a server fault rather than a shape mismatch.
    """
    from ..services.matchmaking_slot_service import delete_matchmaking_slot_service

    result = delete_matchmaking_slot_service(db, slot_id)
    return result


# ----------------------
# Message Management
# ----------------------

@router.get("/messages/reports")
def get_message_reports(
    status_filter: Optional[str] = Query(
        None, alias="status", description="Filter by status: pending, dismissed (or the older reviewed), removed"
    ),
    skip: int = Query(0, ge=0, le=PAGE_OFFSET_MAX),
    limit: int = Query(50, ge=1, le=200),
    q: Optional[str] = Query(None, max_length=100, description="Case-insensitive substring match on the report reason and both parties' regnos. Message bodies are encrypted and are deliberately not searched."),
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.REPORTS_REVIEW))
) -> Dict[str, Any]:
    """Return a merged, newest-first page of chat reports and encrypted evidence.

    Search covers report reasons and party IDs, never message bodies. Matchmaking and Case
    Closed rows share a response shape; each source is paged before the merged ordering.
    """
    wanted = resolve_status_filter(status_filter)
    cleaned = (q or "").strip()
    filters_mm = []
    filters_cc = []
    if wanted:
        filters_mm.append(MessageReportRecord.status.in_(wanted))
        filters_cc.append(CaseClosedMessageReportRecord.status.in_(wanted))
    if cleaned:
        # The message body is not searchable: it is encrypted, and decrypting
        # every row to run a LIKE would be a broad read rather than a report
        # review. Reason and the two party identifiers remain searchable.
        filters_mm.append(or_(
            ilike_contains(MessageReportRecord.reason, cleaned),
            ilike_contains(MessageReportRecord.reporter_regno, cleaned),
            ilike_contains(MessageReportRecord.reported_user_regno, cleaned),
        ))
        filters_cc.append(or_(
            ilike_contains(CaseClosedMessageReportRecord.reason, cleaned),
            ilike_contains(CaseClosedMessageReportRecord.reporter_regno, cleaned),
            ilike_contains(CaseClosedMessageReportRecord.reported_user_regno, cleaned),
        ))
    from ..services.admin_service import ADMIN_MAX_SKIP as _ADMIN_MAX_SKIP

    if skip > _ADMIN_MAX_SKIP:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"skip too deep (max {_ADMIN_MAX_SKIP}); use cursor pagination",
        )

    fetch_depth = skip + limit
    with db.sessions() as session:
        mm_base = select(MessageReportRecord).outerjoin(
            MessageRecord, MessageRecord.id == MessageReportRecord.message_id
        ).where(*filters_mm)
        cc_base = select(CaseClosedMessageReportRecord).outerjoin(
            CaseClosedMessageRecord, CaseClosedMessageRecord.id == CaseClosedMessageReportRecord.message_id
        ).where(*filters_cc)

        mm_rows = session.scalars(mm_base.order_by(
            MessageReportRecord.reported_at.desc(),
            MessageReportRecord.id.asc()).limit(fetch_depth)).all()
        cc_rows = session.scalars(cc_base.order_by(
            CaseClosedMessageReportRecord.reported_at.desc(),
            CaseClosedMessageReportRecord.id.asc()).limit(fetch_depth)).all()

        total = (session.scalar(select(func.count()).select_from(MessageReportRecord).outerjoin(
            MessageRecord, MessageRecord.id == MessageReportRecord.message_id
        ).where(*filters_mm)) or 0) + (
            session.scalar(select(func.count()).select_from(CaseClosedMessageReportRecord).outerjoin(
                CaseClosedMessageRecord, CaseClosedMessageRecord.id == CaseClosedMessageReportRecord.message_id
            ).where(*filters_cc)) or 0
        )

        # Merge newest-first: each table's window of ``skip + limit`` necessarily
        # contains the global window, so slicing the union is exact. Sort by id,
        # then by ``reported_at`` with a *stable* sort so equal timestamps still
        # tie-break by ascending id.
        merged = [("matchmaking", r) for r in mm_rows] + [("case_closed", r) for r in cc_rows]
        merged.sort(key=lambda pair: str(pair[1].id))
        merged.sort(key=lambda pair: pair[1].reported_at, reverse=True)
        page = merged[skip:skip + limit]
        mm_page = [r for kind, r in page if kind == "matchmaking"]
        cc_page = [r for kind, r in page if kind == "case_closed"]

        # Evidence is the authoritative body source: it survives ordinary chat
        # retention and is the only content this endpoint may decrypt, and only
        # for the reports on this page.
        evidence_by_report = evidence_for_reports(
            session, [str(report.id) for _kind, report in page]
        )
        evidence_bodies: Dict[str, str] = {}
        for _kind, report in page:
            evidence = evidence_by_report.get(str(report.id))
            if evidence is None:
                continue
            plaintext = decrypt_evidence(evidence, report_id=str(report.id))
            if plaintext is not None:
                evidence_bodies[str(report.id)] = plaintext

        # Two bulk reads rather than two per row: a queue grows with every report filed, so
        # the per-row lookup would be a cost paid on every page a moderator opens.
        regnos = {report.reporter_regno for report in mm_page} | {report.reported_user_regno for report in mm_page}
        regnos |= {report.reporter_regno for report in cc_page} | {report.reported_user_regno for report in cc_page}
        regnos.discard(None)
        users = {user.regno: user for user in session.scalars(select(UserRecord).where(UserRecord.regno.in_(regnos))).all()} if regnos else {}
        def _party(regno: str) -> Dict[str, Any]:
            person = users.get(regno) or parties.get(regno)
            return {"regno": regno, "name": person.name if person else "Unknown"}

        # --- Matchmaking chat: message + conversation context, in bulk. -------------------
        message_ids = {report.message_id for report in mm_page if report.message_id}
        messages = {str(message.id): message for message in session.scalars(select(MessageRecord).where(MessageRecord.id.in_(message_ids))).all()} if message_ids else {}

        # Conversation context in bulk reads: a reported message without its thread
        # is a sentence with nothing around it, and whether anything else already
        # came down decides lapse-vs-pattern. Evidence carries the conversation id
        # even after the message row has been purged.
        conversation_ids = {
            str(message.conversation_id) for message in messages.values() if message.conversation_id
        }
        conversation_ids |= {
            str(evidence_by_report[str(report.id)].chat_id)
            for report in mm_page
            if str(report.id) in evidence_by_report
        }
        conversations = {
            str(row.id): row for row in session.scalars(
                select(ConversationRecord).where(ConversationRecord.id.in_(conversation_ids))
            ).all()
        } if conversation_ids else {}

        party_regnos: set = set()
        for conversation in conversations.values():
            party_regnos.add(conversation.initiator_regno)
            party_regnos.add(conversation.receiver_regno)
        # The sender and receiver of a reported message are already in ``users`` above; the other
        # side of the thread may be nobody involved in this report at all, so they are read here.
        party_regnos -= set(users)
        parties = {user.regno: user for user in session.scalars(select(UserRecord).where(UserRecord.regno.in_(party_regnos))).all()} if party_regnos else {}

        # Both figures in one grouped pass, with removed rows included on
        # purpose: a moderator auditing a thread needs to see how much of it
        # already came down, which is invisible if removals are excluded.
        thread_totals: Dict[str, int] = {}
        thread_removed: Dict[str, int] = {}
        if conversation_ids:
            counted = session.execute(
                select(
                    MessageRecord.conversation_id,
                    func.count(MessageRecord.id),
                    func.count(MessageRecord.id).filter(MessageRecord.removed_at.is_not(None)),
                )
                .where(MessageRecord.conversation_id.in_(conversation_ids))
                .group_by(MessageRecord.conversation_id)
            ).all()
            thread_totals = {str(row[0]): row[1] for row in counted}
            thread_removed = {str(row[0]): row[2] for row in counted}

        # --- Case Closed detective threads: turn + couple context, in bulk. ----------------
        cc_message_ids = {report.message_id for report in cc_page if report.message_id}
        cc_messages = {
            str(message.id): message for message in session.scalars(
                select(CaseClosedMessageRecord).where(CaseClosedMessageRecord.id.in_(cc_message_ids))
            ).all()
        } if cc_message_ids else {}
        cc_session_ids = {str(message.session_id) for message in cc_messages.values()}
        cc_session_ids |= {
            str(evidence_by_report[str(report.id)].chat_id)
            for report in cc_page
            if str(report.id) in evidence_by_report
        }
        cc_sessions = {
            str(row.id): row for row in session.scalars(
                select(CaseClosedSessionRecord).where(CaseClosedSessionRecord.id.in_(cc_session_ids))
            ).all()
        } if cc_session_ids else {}
        cc_user_ids: set = set()
        for couple in cc_sessions.values():
            cc_user_ids.add(str(couple.user_a_id))
            cc_user_ids.add(str(couple.user_b_id))
        cc_people = {
            str(person.id): person for person in session.scalars(
                select(UserRecord).where(UserRecord.id.in_(cc_user_ids))
            ).all()
        } if cc_user_ids else {}
        cc_totals: Dict[str, int] = {}
        cc_removed: Dict[str, int] = {}
        if cc_session_ids:
            counted = session.execute(
                select(
                    CaseClosedMessageRecord.session_id,
                    func.count(CaseClosedMessageRecord.id),
                    func.count(CaseClosedMessageRecord.id).filter(CaseClosedMessageRecord.removed_at.is_not(None)),
                )
                .where(CaseClosedMessageRecord.session_id.in_(cc_session_ids))
                .group_by(CaseClosedMessageRecord.session_id)
            ).all()
            cc_totals = {str(row[0]): row[1] for row in counted}
            cc_removed = {str(row[0]): row[2] for row in counted}

    def _mm_row(report) -> Dict[str, Any]:
        reporter = users.get(report.reporter_regno)
        reported_user = users.get(report.reported_user_regno)
        message = messages.get(str(report.message_id)) if report.message_id else None
        evidence = evidence_by_report.get(str(report.id))
        chat_id = (
            str(evidence.chat_id)
            if evidence is not None
            else (str(message.conversation_id) if message is not None else None)
        )
        # Evidence is the only content a moderator may decrypt, with no fallback
        # to the live chat row: that path would hand out content whose evidence
        # retention window had closed and bypass the access audit below.
        body = evidence_bodies.get(str(report.id))
        if body is None:
            body = "[Message not found]"
        created_at = (
            evidence.message_created_at
            if evidence is not None
            else (message.created_at if message is not None else None)
        )

        conversation = conversations.get(chat_id) if chat_id else None
        conversation_context = None
        if conversation is not None:
            conversation_context = {
                "id": str(conversation.id),
                # Whether the thread is still open matters: a single lapse and a
                # pattern read differently against an already-quiet conversation.
                "status": conversation.status,
                # The two parties, deduplicated by regno so a self-conversation would not render
                # the same person twice.
                "participants": [
                    _party(regno)
                    for regno in dict.fromkeys(
                        [conversation.initiator_regno, conversation.receiver_regno]
                    )
                ],
                "message_count": thread_totals.get(str(conversation.id), 0),
                "removed_message_count": thread_removed.get(str(conversation.id), 0),
            }

        return {
            "id": str(report.id),
            "kind": "matchmaking",
            "message_id": str(report.message_id) if report.message_id else None,
            "body": body,
            # When the message was sent, not merely reported: a queue row could
            # otherwise look freshly sent however long ago it actually was.
            # Evidence keeps this timestamp after the live row is purged.
            "message_created_at": serialize_datetime(created_at) if created_at else None,
            "conversation_id": chat_id,
            "conversation": conversation_context,
            # Automated reports carry no participant reporter -- the system is the
            # author. For a manual report the live users row wins, so a renamed
            # participant shows the name they have now.
            "reporter": (
                {"regno": None, "name": AUTOMATED_REPORTER_DISPLAY_NAME}
                if report.source == AUTOMATED_SOURCE
                else {
                    "regno": report.reporter_regno,
                    "name": reporter.name if reporter else "Unknown"
                }
            ),
            "reported_user": {
                "regno": report.reported_user_regno,
                "name": reported_user.name if reported_user else "Unknown"
            },
            "reason": report.reason,
            "reported_at": serialize_datetime(report.reported_at),
            "status": report.status,
            # Manual ("user") vs automated ("azure_content_safety"): the screen labels the
            # latter and never attributes it to a participant.
            "source": report.source,
            # Carried so the unified screen can show who closed a report and when without
            # a second request, matching the other two queues.
            "reviewed_at": serialize_datetime(report.reviewed_at),
            "reviewed_by_regno": report.reviewed_by_regno,
            # Whether the message this report is about has been taken down.
            # Cannot be inferred from ``status``: a content admin can remove a
            # message no report was ever filed against.
            "content_removed": bool(message.removed_at) if message else False,
            # Who took it down: the restore button is offered only for rows the system hid.
            "removed_by_regno": message.removed_by_regno if message else None,
        }

    def _cc_row(report) -> Dict[str, Any]:
        reporter = users.get(report.reporter_regno)
        reported_user = users.get(report.reported_user_regno)
        message = cc_messages.get(str(report.message_id)) if report.message_id else None
        evidence = evidence_by_report.get(str(report.id))
        chat_id = (
            str(evidence.chat_id)
            if evidence is not None
            else (str(message.session_id) if message is not None else None)
        )
        body = evidence_bodies.get(str(report.id))
        if body is None:
            body = "[Message not found]"
        created_at = (
            evidence.message_created_at
            if evidence is not None
            else (message.created_at if message is not None else None)
        )
        couple = cc_sessions.get(chat_id) if chat_id else None

        conversation_context = None
        if couple is not None:
            participants = []
            for person_id in dict.fromkeys([str(couple.user_a_id), str(couple.user_b_id)]):
                person = cc_people.get(person_id)
                participants.append({
                    "regno": person.regno if person else None,
                    "name": person.name if person else "Unknown",
                })
            conversation_context = {
                "id": str(couple.id),
                # A detective thread has no open/blocked state the way a matchmaking
                # conversation does; the screen shows this as a Case Closed thread.
                "status": None,
                "participants": participants,
                "message_count": cc_totals.get(str(couple.id), 0),
                "removed_message_count": cc_removed.get(str(couple.id), 0),
            }

        return {
            "id": str(report.id),
            "kind": "case_closed",
            "message_id": str(report.message_id) if report.message_id else None,
            "body": body,
            "message_created_at": serialize_datetime(created_at) if created_at else None,
            "conversation_id": chat_id,
            "conversation": conversation_context,
            "reporter": (
                {"regno": None, "name": AUTOMATED_REPORTER_DISPLAY_NAME}
                if report.source == AUTOMATED_SOURCE
                else {
                    "regno": report.reporter_regno,
                    "name": reporter.name if reporter else "Unknown"
                }
            ),
            "reported_user": {
                "regno": report.reported_user_regno,
                "name": reported_user.name if reported_user else "Unknown"
            },
            "reason": report.reason,
            "reported_at": serialize_datetime(report.reported_at),
            "status": report.status,
            "source": report.source,
            "reviewed_at": serialize_datetime(report.reviewed_at),
            "reviewed_by_regno": report.reviewed_by_regno,
            "content_removed": bool(message.removed_at) if message else False,
            "removed_by_regno": message.removed_by_regno if message else None,
        }

    by_id: Dict[str, Dict[str, Any]] = {}
    for report in mm_page:
        by_id[str(report.id)] = _mm_row(report)
    for report in cc_page:
        by_id[str(report.id)] = _cc_row(report)
    enriched_reports = [by_id[str(report.id)] for kind, report in page]

    # Auditing is part of the read: if it cannot be recorded, the decrypted
    # reported content is not returned. Only ids and the reviewer are stored.
    returned_evidence = {
        report_id: evidence
        for report_id, evidence in evidence_by_report.items()
        if report_id in evidence_bodies
    }
    audit_evidence_access(
        db,
        reviewer_regno=reviewer.regno,
        report_kind_by_id={
            report_id: evidence.report_kind
            for report_id, evidence in returned_evidence.items()
        },
        evidence_by_report=returned_evidence,
    )

    return {
        "reports": enriched_reports,
        "total": total,
        "skip": skip,
        "limit": limit
    }


@router.patch("/messages/reports/{report_id}")
def update_message_report_status(
    report_id: str,
    new_status: str = Query(..., alias="status", pattern="^(reviewed|dismissed|removed)$"),
    kind: str = Query("matchmaking", pattern="^(matchmaking|case_closed)$"),
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.REPORTS_MODERATE)),
) -> Dict[str, Any]:
    """Rule on a chat-message report: dismiss it, or take the message down.

    ``reviewed`` is the older spelling of ``dismissed`` and still accepted; the
    screen only offers the two new words. See ``update_confession_report`` for
    why. ``dismissed`` leaves the thread as it was and tells the reporter;
    ``removed`` also moves the conversation's preview forward, since it is a
    copy of the newest message's text.

    ``kind`` selects the table the report lives in: the default ``matchmaking``
    keeps every existing caller working, while ``case_closed`` rules on a
    detective-thread turn, which has no preview to move. Returns
    ``{id, status, content_removed}`` to match the other two close-out endpoints.
    """
    if kind == "case_closed":
        return resolve_case_closed_message_report(db, report_id, new_status, reviewer.regno)
    return resolve_message_report(db, report_id, new_status, reviewer.regno)


@router.patch("/messages/{message_id}/unhide")
def unhide_message(
    message_id: str,
    kind: str = Query("matchmaking", pattern="^(matchmaking|case_closed)$"),
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.REPORTS_MODERATE)),
) -> Dict[str, Any]:
    """Restore an Azure-hidden chat message to both participants.

    Only content with ``removed_by_regno`` marking Azure Content Safety
    restores; anything hidden by an admin/moderator answers 409 so a reviewer
    cannot undo a colleague's decision, and already-visible rows answer
    ``already_visible``. ``kind`` selects the table.

    Gated on ``reports:moderate`` rather than ``confessions:delete``: a message
    has no hide route of its own, and the automated removal this restores is
    the counterpart of the message-report ruling that same capability governs.
    The conversation preview is moved back onto the restored message when it is
    the newest visible one; nothing is notified, since a restoration is a
    reviewer override rather than a ruling.
    """
    if kind == "case_closed":
        return unhide_case_closed_message_service(db, message_id, reviewer.regno)
    return unhide_message_service(db, message_id, reviewer.regno)
