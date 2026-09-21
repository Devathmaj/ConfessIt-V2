from typing import Any, Dict, List, Optional
from sqlalchemy import func, select, text

from ..postgres import new_id, is_valid_id
from fastapi import APIRouter, Depends, HTTPException, Query, status, Body
from ..postgres import Database

from ..dependencies import get_db
from ..models import AccountDetails, AdminUserCreate, AdminUserUpdate, Year
from ..db_models import (ACCOUNT_MODELS, ConfessionRecord, ConversationRecord,
    MessageRecord, MessageReportRecord, UserRecord, find_account_in_table,
    resolve_account_by_id)
from ..accounts import (
    AccountType, Capability, account_cap, account_cap_lock_key,
    normalize_account_type,
)
from ..services.authorization import require_capability
from ..services.storage_service import storage_service
from ..services.admin_service import (
    serialize_datetime,
    serialize_user_doc,
    _account_row_payload,
    get_all_confessions_data,
    get_confession_reports_data,
    get_admin_statistics_data,
)
from ..services.report_resolution_service import (
    resolve_confession_report,
    resolve_message_report,
    resolve_status_filter,
)
from ..services.report_resolution_service import hide_confession as hide_confession_service
from ..services.report_resolution_service import hide_comment as hide_comment_service

router = APIRouter(prefix="/admin", tags=["Admin"])

# Every account the list can hold, newest-looking first. Ordered explicitly so the response
# is stable across calls: three unordered SELECTs concatenated would otherwise reshuffle the
# page whenever PostgreSQL chose a different plan, and the admin would have to re-find what
# they were looking at after an unrelated edit to an unrelated account.
_ACCOUNT_TABLE_ORDER = (AccountType.ADMIN, AccountType.MODERATOR, AccountType.USERS)

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


@router.get("/confessions", response_model=List[Dict[str, Any]])
async def get_all_confessions(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.CONFESSIONS_REVIEW))
) -> List[Dict[str, Any]]:
    """Return every confession for admin review.

    Everything a reviewer acts on is here -- body, timestamps, reports, comments, engagement --
    with one exception: the sender of an anonymous confession. That identity is withheld for
    staff exactly as it is for users, and the redaction happens in the service so it is not
    merely absent from the screen but absent from the response.
    """
    return get_all_confessions_data(db)


@router.delete("/confessions/{confession_id}")
async def delete_confession(
    confession_id: str,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.CONFESSIONS_DELETE))
) -> Dict[str, Any]:
    """Remove a confession permanently, row and attached reports included.

A hard delete, and deliberately the odd one out: it is the route that exists for content that
must not remain in the database at all. The Confession Review screen does not offer it --
``PATCH /confessions/{confession_id}/hide`` is what a reviewer reaches for, and that one keeps
the row so the report trail stays intact.
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
async def hide_confession(
    confession_id: str,
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.CONFESSIONS_DELETE)),
) -> Dict[str, Any]:
    """Hide a confession from public view, keeping the row and everything attached to it.

    This is what the Confession Review screen offers instead of deleting. The write is the same
    one a ``removed`` ruling on a report performs -- ``removed_at`` plus who stamped it -- so a
    confession hidden from the review screen and one taken down through a report queue end up in
    identical state, and every participant-facing read filters the one column.

    What replaces ``DELETE /confessions/{confession_id}`` on that screen is not nothing: the
    delete route stays, for the cases that genuinely need the row and its reports to be gone.
    What is dropped is the one-keystroke path to it from the screen where a reviewer works
    through many posts.

    Hiding an already-hidden confession is answered, not refused, and says so in
    ``already_hidden``. The alternative -- a 409 -- turns a double click into an error toast
    that reads like a failure when the requested state is already in place, and pushes the
    screen to special-case a condition the user did nothing wrong about.

    There is deliberately no matching unhide route. Hiding is one-way here, and the
    confirmation dialog says so rather than offering a promise the API does not keep.
    """
    return hide_confession_service(db, confession_id, reviewer.regno)


@router.patch("/comments/{comment_id}/hide")
async def hide_comment(
    comment_id: str,
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.CONFESSIONS_DELETE)),
) -> Dict[str, Any]:
    """Hide one comment from its confession, keeping the row and everything attached to it.

    The comment-level counterpart to ``PATCH /confessions/{confession_id}/hide``: the same soft
    write, the same ``removed_at`` every participant-facing comment read filters on, and the same
    one-way warning in the confirmation dialog. A confession with one bad comment in a hundred
    should not have to be hidden wholesale to deal with it.

    Declared under ``/admin/comments`` rather than nested under ``/admin/confessions/{id}/``
    for the same routing reason as the report queue below: a literal segment beside a path
    parameter can be read as that parameter's value, and a comment id being interpreted as a
    confession id is a 404 that looks like a missing row.

    Gated on ``confessions:delete``, the capability ``hide_confession`` already uses, because
    this is the same job -- taking content out of public view without destroying the record --
    and moderators already hold it. A separate capability would mean granting moderators a
    second name for a privilege they already have, and then keeping the two in step.

    Unlike ``DELETE /confessions/{confession_id}``, this does not cascade the reports filed
    against the comment: hiding is a visibility change, so the record of it has to survive.
    """
    return hide_comment_service(db, comment_id, reviewer.regno)


# The report queue is declared after ``/confessions/{confession_id}`` on purpose. FastAPI
# matches on method first, and the only sibling is a DELETE, so nothing here is shadowed
# today; keeping the literal path last means adding a ``GET /{confession_id}`` later cannot
# swallow it. ``/confessions/{confession_id}/hide`` is PATCH like ``/confessions/reports/...``,
# and the literal ``reports`` segment is matched first, so a report id is never read as a
# confession id. These two carry the reporting capabilities rather than the confession ones, so
# that ruling on reports stays separately grantable from reviewing the posts themselves.


@router.get("/confessions/reports")
async def get_confession_reports(
    status_filter: Optional[str] = Query(
        None, alias="status", description="Filter by status: pending, dismissed (or the older reviewed), removed"
    ),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.REPORTS_REVIEW)),
) -> Dict[str, Any]:
    """Every report filed against a confession or one of its comments, newest first.

    Carries the reported content itself rather than an id, so the question a reviewer opens
    this screen to answer -- what did this actually say -- is answerable here. The author of
    an anonymous confession is withheld, as it is from the confession review screen: an
    anonymous post stays anonymous to staff through every door.
    """
    return get_confession_reports_data(db, status_filter=status_filter, skip=skip, limit=limit)


@router.patch("/confessions/reports/{report_id}")
async def update_confession_report(
    report_id: str,
    new_status: str = Query(..., alias="status", pattern="^(reviewed|dismissed|removed)$"),
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.REPORTS_MODERATE)),
) -> Dict[str, Any]:
    """Rule on a confession or comment report: dismiss it, or take the content down.

    Two outcomes and no third. ``dismissed`` records that a human looked and the content
    stays; ``removed`` marks the reported confession or comment removed so it disappears from
    every participant-facing read, and notifies both the author and the reporter.

    ``reviewed`` is accepted as a synonym for ``dismissed``. It is the word this route used
    before the screen grew a remove button, it is what the queue's status filter still labels
    older rows with, and it is what a bookmarked request or a stale client will send. Turning it
    into a 422 would break a caller over a rename and would not stop a single moderation action,
    so it is answered rather than refused -- while the screen only ever offers the two new
    words, which is what stops ``reviewed`` from being the thing a moderator actually picks.

    Removal here is deliberately *not* the same as ``DELETE /admin/confessions/{id}``. That
    route is a hard delete, and the report's foreign key cascades from the content, so deleting
    a reported post would delete the record that it was reported and ruled on. Ruling on a
    report keeps both.

    ``reports:moderate`` is the whole gate rather than a second capability for the removing
    case. A moderator already holds ``confessions:delete``, so this grants them nothing over
    confessions; the one genuine widening is that ruling on a *message* report can now take a
    message down, which no route could do before. A route declaring one capability is the
    invariant ``test_roles_routes.py`` holds the admin surface to, so splitting the check
    across two capabilities would mean checking the second one in the handler body instead --
    the one place a gate can be forgotten.
    """
    return resolve_confession_report(db, report_id, new_status, reviewer.regno)


@router.get("/users", response_model=List[Dict[str, Any]])
async def get_all_users(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.USERS_REVIEW))
) -> List[Dict[str, Any]]:
    """Return every account, from all three tables, for review.

    Staff and participants arrive through one endpoint because they are one population of
    accounts; the ``account_type`` on each document is which table it came from. Splitting
    the admin list per table would mean the search box on it would have to know which table
    to search, which is not a distinction a person administering the app should have to make.
    """
    with db.sessions() as session:
        docs: List[Dict[str, Any]] = []
        for account_type in _ACCOUNT_TABLE_ORDER:
            model = ACCOUNT_MODELS[account_type.value]
            rows = session.scalars(select(model).order_by(model.name.asc())).all()
            docs.extend(serialize_user_doc(_account_row_payload(account_type.value, row)) for row in rows)
        return docs


@router.post("/users", response_model=Dict[str, Any], status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: AdminUserCreate,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.USERS_CREATE))
) -> Dict[str, Any]:
    """Create an account in the table named by ``account_type``.

    The table is chosen by the payload, and the same payload shape creates a participant or
    a staff account: there is no separate admin-creation form or endpoint to fall out of
    step with this one. ``year`` and ``department`` are required by the payload validator only
    for a participant, so a caller cannot write them onto a staff row.

    What an admin supplies is an identity and a cohort -- regno, name, email, and, for a
    participant, gender, year and department. Everything else belongs to the account rather than
    to whoever typed the form: the picture, bio, interests and the four preference
    flags are not on this payload at all, and ``AdminUserCreate`` forbids them by name so a
    curl that sends one is answered 422 rather than a 201 for an insert that dropped it.

    Each of those columns is still populated on the new row, from the default declared in
    ``db_models`` rather than from the request: notifications on, matchmaking on, love notes
    receivable, love notes not yet sendable, no interests, and a NULL picture. A participant
    edits them through ``PUT /profile/update`` from the moment the account exists.
    """

    account_type = payload.account_type
    model = ACCOUNT_MODELS[account_type.value]
    normalized_regno = payload.regno.strip()

    # Uniqueness is enforced per table, by PostgreSQL, on the table being written to. It is
    # deliberately *not* enforced across the three tables any more: sign-in is category
    # scoped, so a regno is only ever looked up in the one table the person chose on the way
    # in, and a session is pinned to that row by primary key for its whole life. That means
    # the same regno can exist as a participant and as staff, and each sign-in page resolves
    # its own account -- which is a supported state, not corruption. Refusing it here would
    # have been a workaround for the ambiguity that no longer exists.
    with db.sessions() as session:
        if find_account_in_table(session, account_type.value, normalized_regno) is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this registration number already exists for this account type.",
            )
        # An address is unique within one account table and never across the three, for the
        # same reason regno is: the value identifies a person for contact, not a route into an
        # account, so nothing has to resolve one. The check is against the target table only.
        normalized_email = payload.email.strip().lower()
        email_conflict = session.scalar(select(model.id).where(
            model.email == normalized_email
        ).limit(1))
        if email_conflict:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="An account with this email already exists for this account type.",
            )

    # ``notifications_enabled`` and ``is_blocked`` are intentionally not written here. Both have
    # server defaults in ``db_models.AccountColumns`` (true and false), so the new row takes the
    # declared values whether or not this dict mentions them, and there is no longer any way to
    # ask for a different one at creation time. Blocking an existing account is
    # ``PUT /admin/users/{id}/block``, which is a decision about a row that already exists.
    #
    # Only the fields the target table actually has are passed to the constructor. Building
    # the kwargs from the payload with the participant keys removed -- rather than branching
    # into two constructors -- means a field added to AccountColumns lands in all three
    # tables at once, and a participant-only field can never reach a staff row.
    account_fields: Dict[str, Any] = {
        "regno": normalized_regno,
        "name": payload.name.strip(),
        "email": normalized_email,
    }
    if account_type is AccountType.USERS:
        account_fields.update({
            # A new account has no picture. Both halves of the picture's identity are named
            # rather than left to a default, because "no picture" is a real state and it has to
            # be one value in two columns -- a row with a public_id and no format would render a
            # broken image.
            "profile_picture_public_id": None,
            "profile_picture_format": None,
            "gender": payload.gender.strip(),
            # Written from the two fields separately rather than by joining them back into one
            # string. The columns are what matchmaking groups by, and a stored "CSE 2" would
            # be groupable by neither half.
            "year": payload.year,
            "department": payload.department.strip(),
        })


    with db.sessions.begin() as session:
        # The cap is checked here, inside the same transaction as the insert, and behind an
        # advisory lock -- not in the `db.sessions()` block above with the duplicate checks.
        #
        # A SELECT count(*) followed by an INSERT is not atomic: two admins submitting the create
        # form at the same moment both read "499 of 500", both decide there is room, and both
        # insert. The cap would then be exceeded by exactly as many simultaneous requests as the
        # deployment can serve at once, which is the one situation a cap exists to prevent.
        # `pg_advisory_xact_lock` serialises account creation *per account table* for the rest of
        # this transaction, so the second request blocks until the first has committed and then
        # counts the row it just added. It is transaction-scoped, so it is released by the commit
        # or the rollback without any cleanup to forget, and the three tables use different keys
        # so creating a participant never waits on an admin.
        #
        # This is the only place a row is added through the API, which is what makes the lock
        # sufficient: there is no second writer that could slip past it.
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

        user = model(**account_fields)
        session.add(user)
        session.flush()
        created_doc = _account_row_payload(account_type.value, user)
    return serialize_user_doc(created_doc)


@router.put("/users/{user_id}/block")
async def set_user_block_state(
    user_id: str,
    blocked: bool = Query(..., alias="blocked"),
    db: Database = Depends(get_db),
    current_admin: AccountDetails = Depends(require_capability(Capability.USERS_BLOCK))
) -> Dict[str, Any]:
    """Block or unblock an account in any of the three tables.

    Resolved by id across all tables rather than against ``users``, because a moderator can
    be blocked by an admin and the moderator's row is not in ``users``.

    **An admin is never blockable, and the database is what enforces it.**
    ``ck_admin_not_blockable`` (``db_models.AdminRecord``) is a CHECK constraint on the
    ``admin`` table, so the rule holds for raw SQL, a script, an operator at a psql prompt and
    any endpoint added later -- not just for this one. The check below is a courtesy that turns
    a would-be ``IntegrityError`` into a 400 explaining itself; it is not the enforcement, and
    deleting it would not make an admin blockable, only make the refusal a 500.

    Unblocking is unaffected and still allowed: the constraint permits any value that is not
    true, so this only ever refuses ``blocked=true`` on an admin.
    """
    if not is_valid_id(user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user id")

    if str(current_admin.id) == user_id:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="You cannot change your own block status")

    with db.sessions.begin() as session:
        found = resolve_account_by_id(session, user_id)
        if found:
            target_type, row = found
            if blocked and normalize_account_type(target_type) == AccountType.ADMIN.value:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="An admin account cannot be blocked.",
                )
            row.is_blocked = blocked
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")
    return {"id": user_id, "is_blocked": blocked}


@router.put("/users/{user_id}", response_model=Dict[str, Any])
async def update_user_details(
    user_id: str,
    update: AdminUserUpdate,
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.USERS_UPDATE))
) -> Dict[str, Any]:
    """Edit an account that already exists, in whichever table it lives.

    The account is found by id across all three tables and edited in place. Its table is not
    editable: ``AdminUserUpdate`` has no ``account_type`` field, so a save cannot turn a
    moderator into a participant and strand the content that references it.

    The editable set is the account's identity: regno, name, email, and -- for a participant
    only -- gender, year and department. The picture, bio, interests and the four
    preference flags are not fields on this payload, and ``extra="forbid"`` refuses them by
    name -- so a caller still sending one gets a 422 rather than a 200 for an edit that silently
    did not happen. ``is_blocked`` is likewise absent, and blocking an account is the separate
    ``PUT /admin/users/{id}/block`` route. That is what removes this route's old job of
    refusing to block an admin: an admin row's block state cannot be written here at all, so
    there is no second door to guard.

    A field the operator cleared is dropped rather than written. ``AdminUserUpdate``'s validator
    turns a blank string into ``None`` so the *model* is valid, but ``exclude_unset`` keeps the key
    -- the field was sent, it just held nothing -- and writing that ``None`` to a NOT NULL column
    is a 500 from PostgreSQL rather than anything the caller could act on. Dropping the key is
    also the honest reading of what a cleared input means: the operator left it empty, which is not
    a request to erase a value the row is required to have. This is the second line of defence the
    validator's docstring refers to.
    """
    if not is_valid_id(user_id):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid user id")

    # NOT NULL columns on the account tables, so a ``None`` here is never a value to write. Year
    # and department are on the list for the same reason name and regno are: they are two halves
    # of what used to be one ``class_name``, and a participant without either cannot be placed in
    # a matchmaking cohort.
    not_null_fields = frozenset({
        "regno", "name", "email", "gender", "year", "department",
    })
    update_payload = {
        key: value
        for key, value in update.dict(exclude_unset=True).items()
        if not (value is None and key in not_null_fields)
    }

    if not update_payload:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="No changes provided")

    with db.sessions.begin() as session:
        found = resolve_account_by_id(session, user_id)
        if found:
            account_type, account = found
            # ``gender``, ``year`` and ``department`` are dropped for a staff account. The
            # participant-shaped form submits every field it knows about, and without this an
            # edit to a moderator's name would also try to write a cohort -- or a gender -- onto
            # tables that have no such columns, and, more insidiously, would write one if the
            # columns were ever added for an unrelated reason.
            #
            # ``email`` is *not* on this list. It is a shared column now, required on every
            # account, so a moderator's address is edited exactly as a participant's is; the
            # note that once stood here to explain its absence is what would have led to
            # staff rows being left without one.
            participant_only = ("gender", "year", "department")
            if account_type != AccountType.USERS.value:
                update_payload = {
                    key: value for key, value in update_payload.items()
                    if key not in participant_only
                }
            for key, value in update_payload.items():
                setattr(account, key, value)
            updated_doc = _account_row_payload(account_type, account)
    if not found:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    return serialize_user_doc(updated_doc)




@router.get("/stats", response_model=Dict[str, Any])
async def get_admin_statistics(
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.STATS_READ))
) -> Dict[str, Any]:
    """Provide high-level metrics for the admin dashboard."""
    return get_admin_statistics_data(db)


@router.get("/config", response_model=Dict[str, Any])
async def read_app_configuration(
    _: AccountDetails = Depends(require_capability(Capability.APP_CONFIG_READ)),
    db: Database = Depends(get_db)
) -> Dict[str, Any]:
    """Return the application's operator-settable configuration.

    ``settings`` is deliberately empty for now: platform settings are still environment variables
    read by ``app/config.py`` at import time, so there is nothing stored to report yet.

    ``cohorts`` is every ``(year, department)`` pair at least one participant occupies, and
    ``slots`` the pairings between them. The key was ``classes`` while the participants it was
    derived from had one ``class_name`` column; it is a list of two-field objects now, and a key
    called ``classes`` returning that would be the kind of name that outlives its meaning.
    """
    from ..services.matchmaking_slot_service import get_all_slots_service, list_participant_cohorts

    cohorts = await list_participant_cohorts(db)
    slots = await get_all_slots_service(db)

    return {
        "settings": {},
        "cohorts": cohorts,
        "slots": slots,
    }


@router.post("/config/matchmaking-slot", response_model=Dict[str, Any])
async def create_matchmaking_slot(
    year_a: Year = Body(..., description="Year of the first cohort. Must be between USER_YEAR_MIN and USER_YEAR_MAX."),
    department_a: str = Body(..., description="Department or division of the first cohort. Required."),
    year_b: Year = Body(..., description="Year of the second cohort. Must be between USER_YEAR_MIN and USER_YEAR_MAX."),
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

    Both sides are sent as a ``(year, department)`` pair rather than as one name, because a slot is
    the rule *these* participants may match with *those* participants and a joined string could not
    be compared against either half of a participant's own row. Two identical sides are allowed and
    mean the cohort may match within itself.

    ``year_a``/``year_b`` are typed as ``Year``, the same bounded int the participant payload uses,
    so a year outside the range is a 422 naming the field. Without it the value would reach
    the table and be refused by a CHECK constraint as a 500, or -- on a write that somehow skipped
    that -- become a cohort that matches nobody.

    ``group_key`` is a plain ``str`` rather than a ``UUID`` so that a malformed one is answered with
    a 400 naming the field, the way ``slot_id`` is on the delete route. Letting the type system
    reject it would produce a 422 whose ``loc`` points at the body rather than at the field, which
    is the mistake ``test_the_delete_endpoint_takes_the_id_in_an_object`` records.
    """
    from ..services.matchmaking_slot_service import create_matchmaking_slot_service

    slot = await create_matchmaking_slot_service(
        db, year_a, department_a, year_b, department_b, group_key
    )
    return slot


@router.delete("/config/matchmaking-slot", response_model=Dict[str, Any])
async def delete_matchmaking_slot(
    slot_id: str = Body(..., embed=True),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.APP_CONFIG_READ))
) -> Dict[str, Any]:
    """Delete a matchmaking slot by ID.

    ``embed=True`` so the id arrives as ``{"slot_id": "..."}`` rather than as a bare JSON string.
    Without it FastAPI reads the *whole body* as this one scalar, so a client sending the obvious
    object -- which is what the POST above takes, and what every other endpoint in this router
    takes -- is refused with a 422 that names ``body`` and not ``slot_id``, which reads as a server
    fault rather than a shape mismatch. Embedding it makes the two halves of this resource speak
    the same dialect.
    """
    from ..services.matchmaking_slot_service import delete_matchmaking_slot_service

    result = await delete_matchmaking_slot_service(db, slot_id)
    return result


# ----------------------
# Message Management
# ----------------------

@router.post("/messages/archive")
async def archive_old_messages(
    hours_old: int = Query(4, ge=1, le=168, description="Archive messages older than this many hours"),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.MESSAGES_ARCHIVE))
) -> Dict[str, Any]:
    """
    Archive old messages to compressed JSON files and remove from PostgreSQL
    
    Default: Archives messages older than 4 hours
    """
    from ..services.message_archival_service import message_archival_service
    
    result = message_archival_service.archive_old_messages(db, hours_old=hours_old)
    return result


@router.get("/messages/archives")
async def list_message_archives(
    _: AccountDetails = Depends(require_capability(Capability.MESSAGES_ARCHIVE))
) -> Dict[str, Any]:
    """List all archived message files"""
    from ..services.message_archival_service import message_archival_service
    
    archives = message_archival_service.list_archives()
    
    total_size_kb = sum(archive["size_kb"] for archive in archives)
    
    return {
        "archives": archives,
        "count": len(archives),
        "total_size_kb": round(total_size_kb, 2),
        "total_size_mb": round(total_size_kb / 1024, 2)
    }


@router.get("/messages/archives/{date}")
async def get_archived_messages(
    date: str,
    user_regno: Optional[str] = Query(None, description="Filter by user regno"),
    conversation_id: Optional[str] = Query(None, description="Filter by conversation ID"),
    _: AccountDetails = Depends(require_capability(Capability.MESSAGES_ARCHIVE))
) -> Dict[str, Any]:
    """
    Get archived messages for a specific date
    
    Date format: YYYY-MM-DD
    """
    from ..services.message_archival_service import message_archival_service
    
    messages = message_archival_service.get_archived_messages(
        date=date,
        user_regno=user_regno,
        conversation_id=conversation_id
    )
    
    return {
        "date": date,
        "count": len(messages),
        "messages": messages
    }


@router.get("/messages/reports")
async def get_message_reports(
    status_filter: Optional[str] = Query(
        None, alias="status", description="Filter by status: pending, dismissed (or the older reviewed), removed"
    ),
    skip: int = Query(0, ge=0),
    limit: int = Query(50, ge=1, le=200),
    db: Database = Depends(get_db),
    _: AccountDetails = Depends(require_capability(Capability.REPORTS_REVIEW))
) -> Dict[str, Any]:
    """Every reported chat message, newest first, with the message and both parties.

    Shares its ``{reports, total, skip, limit}`` envelope and its status/audit fields with the
    confession and love-note queues, because the three are rendered by one screen. The rows
    themselves stay their own shapes -- ``message_id`` and ``body`` here, ``confession_id`` and
    ``content`` there -- because a message is not a confession and flattening the difference
    would mean the payload lied about one of them.

    The wire name is ``status``, aliased onto ``status_filter`` so the parameter does not
    shadow the ``fastapi.status`` module used for HTTP codes in this router. It used to arrive
    as ``status_filter``, which was the odd one out among the queues; nothing called this
    endpoint before the unified Reports Review screen, so renaming it cost nothing and leaves
    all three queues speaking the same word for the same filter.

    ``dismissed`` is widened to include the older ``reviewed`` by ``resolve_status_filter``, so a
    legacy row closed out before the rename is still returned by the filter whose name matches the
    label the screen prints (section 17.1).
    """
    filters = []
    wanted = resolve_status_filter(status_filter)
    if wanted:
        filters.append(MessageReportRecord.status.in_(wanted))
    with db.sessions() as session:
        reports = session.scalars(select(MessageReportRecord).where(*filters)
            .order_by(MessageReportRecord.reported_at.desc()).offset(skip).limit(limit)).all()
        total = session.scalar(select(func.count()).select_from(MessageReportRecord).where(*filters)) or 0

        # Two bulk reads rather than two per row: a queue grows with every report filed, so
        # the per-row lookup would be a cost paid on every page a moderator opens.
        regnos = {report.reporter_regno for report in reports} | {report.reported_user_regno for report in reports}
        users = {user.regno: user for user in session.scalars(select(UserRecord).where(UserRecord.regno.in_(regnos))).all()} if regnos else {}
        message_ids = {report.message_id for report in reports}
        messages = {str(message.id): message for message in session.scalars(select(MessageRecord).where(MessageRecord.id.in_(message_ids))).all()} if message_ids else {}

        # Conversation context, in three bulk reads rather than three per row. A reported
        # message without its thread is a sentence with nothing around it, and "who was this
        # person talking to, and had anything else in there already come down" is the context
        # that decides whether one message is a single lapse or a pattern.
        conversation_ids = {
            str(message.conversation_id) for message in messages.values() if message.conversation_id
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

        # Both figures in one grouped pass: how big the thread is, and how much of it has
        # already been taken down. Counts include removed rows on purpose -- a moderator
        # auditing a thread needs to know that two of its messages are already gone, which is
        # invisible if removed rows are excluded from the total.
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

    def _party(regno: str) -> Dict[str, Any]:
        person = users.get(regno) or parties.get(regno)
        return {"regno": regno, "name": person.name if person else "Unknown"}

    enriched_reports = []
    for report in reports:
        reporter = users.get(report.reporter_regno)
        reported_user = users.get(report.reported_user_regno)
        message = messages.get(str(report.message_id))

        conversation = conversations.get(str(message.conversation_id)) if message else None
        conversation_context = None
        if conversation is not None:
            conversation_context = {
                "id": str(conversation.id),
                # Whether the thread is still open matters: a moderator weighing whether this
                # is one lapse or a pattern is deciding against a conversation that may
                # already have gone quiet on its own, and the two readings differ.
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

        enriched_reports.append({
            "id": str(report.id),
            "message_id": str(report.message_id),
            "body": message.body if message else "[Message not found]",
            # When the message was sent. The screen previously showed only when it was
            # *reported*, so a message could sit in the queue looking like it was sent moments
            # ago however long ago it actually was.
            "message_created_at": serialize_datetime(message.created_at) if message else None,
            "conversation_id": str(message.conversation_id) if message else None,
            "conversation": conversation_context,
            "reporter": {
                "regno": report.reporter_regno,
                "name": reporter.name if reporter else "Unknown"
            },
            "reported_user": {
                "regno": report.reported_user_regno,
                "name": reported_user.name if reported_user else "Unknown"
            },
            "reason": report.reason,
            "reported_at": serialize_datetime(report.reported_at),
            "status": report.status,
            # Carried so the unified screen can show who closed a report and when without
            # a second request, matching the other two queues.
            "reviewed_at": serialize_datetime(report.reviewed_at),
            "reviewed_by_regno": report.reviewed_by_regno,
            # Whether the message this report is about has been taken down. The screen cannot
            # infer it from ``status``, because a content admin can remove a message without a
            # report ever existing, and a moderator needs to see that before deciding.
            "content_removed": bool(message.removed_at) if message else False,
        })

    return {
        "reports": enriched_reports,
        "total": total,
        "skip": skip,
        "limit": limit
    }


@router.patch("/messages/reports/{report_id}")
async def update_message_report_status(
    report_id: str,
    new_status: str = Query(..., alias="status", pattern="^(reviewed|dismissed|removed)$"),
    db: Database = Depends(get_db),
    reviewer: AccountDetails = Depends(require_capability(Capability.REPORTS_MODERATE)),
) -> Dict[str, Any]:
    """Rule on a chat-message report: dismiss it, or take the message down.

    ``reviewed`` is the older spelling of ``dismissed`` and is still accepted; the screen only
    offers the two new words. See ``update_confession_report`` for why.

    ``dismissed`` leaves the thread exactly as it was and tells the reporter it was reviewed.
    ``removed`` marks the message removed so it disappears from every participant read -- and
    moves the conversation's preview forward, since the preview is a copy of the newest
    message's text and would otherwise keep showing it -- and tells both the sender and the
    reporter.

    Returns ``{id, status, content_removed}`` to match the other two close-out endpoints, with
    the third field so the screen can say plainly whether anything came down.
    """
    return resolve_message_report(db, report_id, new_status, reviewer.regno)
