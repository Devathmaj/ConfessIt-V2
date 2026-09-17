"""Normalized SQLAlchemy schema for PostgreSQL application data.

There are three account tables -- ``users`` for participants, ``admin`` and ``moderator``
for staff -- and an account's privileges are a function of which one its row is in, not of
any column it holds. Nothing in this schema stores a role.
"""
import uuid
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import (
    Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, SmallInteger,
    ARRAY, String, Text, UniqueConstraint, Uuid, func, select, text,
)
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from .limits import (
    COMMENT_BODY_MAX,
    CONFESSION_BODY_MAX,
    INTEREST_VALUES,
    MESSAGE_BODY_MAX,
    REPORT_REASON_MAX,
    USER_GENDER_VALUES,
)
from .accounts import (
    ACCOUNT_TYPE_VALUES,
    AccountType,
    is_valid_account_type,
    normalize_account_type,
)

# Rendered into the CHECK constraints below. The values are literals in trusted source, not
# user input, and quoting them here rather than passing them as bind parameters keeps each
# constraint a single self-contained DDL expression that survives the NOT VALID re-add in
# ``PostgresClient.apply_check_constraints``.
_ACCOUNT_TYPE_SQL_LIST = ", ".join(f"'{value}'" for value in ACCOUNT_TYPE_VALUES)
_GENDER_SQL_LIST = ", ".join(f"'{value}'" for value in USER_GENDER_VALUES)

# The interest vocabulary as a SQL array literal, for the containment check on
# ``users.interests``. Built the same way and for the same reason as the lists above.
#
# The ``::varchar[]`` cast is not decoration. PostgreSQL types a bare ``ARRAY['a', 'b']`` as
# ``text[]``, and there is no ``varchar[] <@ text[]`` operator -- the run fails at startup with
# "operator does not exist" on the very first boot against a fresh database. Casting to
# ``varchar[]`` matches the column, which SQLAlchemy maps to ``VARCHAR(80)[]``; an unconstrained
# ``varchar[]`` is the same family, so the containment resolves.
_INTEREST_SQL_ARRAY = (
    "ARRAY[" + ", ".join(f"'{value}'" for value in INTEREST_VALUES) + "]::varchar[]"
)

# NULL is excluded explicitly wherever a CHECK would otherwise be trivially satisfied by it.
# In SQL a CHECK passes when it evaluates to NULL, so ``gender IN (...)`` alone accepts a
# NULL gender -- which is precisely the value the constraint exists to prevent. ``gender IS NOT
# NULL AND gender IN (...)`` is the form that actually refuses it.
_GENDER_CHECK_SQL = (
    f"gender IS NOT NULL AND gender IN ({_GENDER_SQL_LIST})"
)

def _non_character_code_points() -> list[int]:
    """Every code point Unicode reserves forever, in ascending order.

    Two families, and the second is the one that gets forgotten. Sixty-six of them sit in one
    contiguous block at U+FDD0-U+FDEF; the rest are the last two code points of every plane, so
    U+FFFE and U+FFFF in the BMP but also U+1FFFE, U+2FFFE and so on up to U+10FFFE and U+10FFFF.
    Testing only the BMP pair accepts text that a strict reader is entitled to replace with
    U+FFFD, silently.

    Generated rather than written out because the plane-end entries are exactly the kind of long
    list that gets one entry dropped by accident, and a missing entry fails by accepting text
    rather than by erroring.
    """
    return (
        list(range(0xFDD0, 0xFDEF + 1))
        + [code_point for plane in range(17) for code_point in (plane << 16 | 0xFFFE, plane << 16 | 0xFFFF)]
    )


# The same set, as a PostgreSQL regular expression character class, built from the code points
# rather than written as escapes. That is deliberate and it is the whole reason this is a
# function call instead of a string literal: PostgreSQL's ``\u`` escape takes exactly four hex
# digits, so ``\u1FFFE`` is U+1FFF followed by a literal ``E`` rather than U+1FFFE, and a pattern
# written with escapes looks correct while matching the wrong characters. Interpolating real
# characters sidesteps the escape width entirely.
#
# The constraint text therefore carries invisible code points. That is why it is generated here
# rather than written by hand -- there is nothing readable to write, and a hand-written version
# would be the version that quietly does not match.
_NON_CHARACTERS_PATTERN = "[" + "".join(chr(code_point) for code_point in _non_character_code_points()) + "]"


def _uuid() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


def _now() -> datetime:
    """An aware UTC default, so a row written outside a request still gets a real instant."""
    return datetime.now(timezone.utc)


class AccountColumns:
    """The columns every account table shares.

    Kept as one mixin rather than copied three times so a field that is genuinely common
    cannot drift between the tables -- ``last_login_ip`` being present for participants and
    absent for staff would be a bug no type checker would catch, and the whole point of
    splitting the tables is that the shared shape stays honest.

    Deliberately *not* here: emoji, bio, class_name, interests, matchmaking_enabled and the
    love-note toggles. Those describe a participant, and a staff account is not a participant,
    so storing them would only create columns nothing writes and nothing reads.

    ``email`` *is* here, on all three tables, and it is NOT NULL. It is how a person is
    reached, and that is true of a moderator as much as a student: a staff account with no
    address on it is an account nobody can be contacted about, and the moment that is
    discovered is long after the row was written -- usually when the holder has left and the
    address has to be recovered from somewhere else. The column was nullable on the reasoning
    that creating a staff account is a deliberate act by somebody who already knows the regno,
    which is true and irrelevant: the person filling in the form has an email address in front
    of them, and requiring it costs nothing at that moment. ``PostgresClient.apply_not_null_
    constraints`` backfills any legacy NULL with a visibly-fake placeholder and logs what it
    touched, so an already-deployed database reaches the same state rather than refusing to
    boot.

    ``gender`` is here for the same reason and is also NOT NULL, with ``ck_accounts_gender``
    holding it to the three values in ``limits.USER_GENDER_VALUES``. It is a *filter* --
    matchmaking pairs candidates by it -- so "Male" and "male" being different people to that
    query is not untidiness, it is a participant who never appears in a single pool.

    The constraint on ``gender`` lives in ``_ACCOUNT_TABLE_ARGS`` below and is attached to
    each of the three tables explicitly. It cannot simply sit here: SQLAlchemy treats
    ``__table_args__`` as a single replaceable attribute, so a concrete class that declares
    its own -- as ``UserRecord`` does, for its interests check -- silently drops the mixin's
    rather than merging the two. The first version of this did exactly that and left ``users``,
    the table with the most accounts on it, as the only one without the constraint.

    Uniqueness is per table, and that is the same rule ``regno`` follows: the constraint is
    declared on the column, so PostgreSQL enforces it within one table and never across the
    three. An address may therefore exist as a participant and as a moderator simultaneously.
    Nothing resolves an account by email -- sign-in is regno-based, and sessions are pinned by
    primary key -- so a shared address is a display fact rather than an ambiguity, exactly as
    a shared regno now is.
    """

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    regno: Mapped[str] = mapped_column(String(100), unique=True, index=True)
    name: Mapped[str] = mapped_column(String(160))
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    profile_picture_url: Mapped[Optional[str]] = mapped_column(Text)
    gender: Mapped[str] = mapped_column(String(24))
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_login_ip: Mapped[Optional[str]] = mapped_column(INET)
    last_login_user_agent: Mapped[Optional[str]] = mapped_column(Text)
    # ``onupdate`` is applied by SQLAlchemy on the ORM side, and ``server_default`` covers
    # writes that bypass it. Both are set so ``updated_at`` cannot silently stay at the
    # creation instant for a row written by a script.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )


# Attached to all three account tables by hand rather than declared on ``AccountColumns``.
# See that class's docstring for why a mixin declaration is not enough: SQLAlchemy replaces
# ``__table_args__`` rather than merging, so the moment any concrete table needs a constraint
# of its own the mixin's disappears from that table without a word.
_ACCOUNT_TABLE_ARGS: tuple = (
    CheckConstraint(_GENDER_CHECK_SQL, name="ck_accounts_gender"),
)


class UserRecord(AccountColumns, Base):
    """A participant, and the only account table content can be attributed to.

    **On participants and not on ``AccountColumns``:** the six ``*_report_count`` and
    ``*_removal_count`` columns below describe *what a person has been reported for*, and
    only a participant is ever reported -- a moderator's posts are not in anybody's report
    queue, so a staff row would have six columns nothing ever writes and nothing reads. Same
    reasoning as ``emoji``, ``bio`` and the matchmaking toggles being absent from the mixin.

    **What the two columns per kind mean, and why there are two.** For each of the three kinds
    of content a participant can be reported for, this table holds:

    ``*_report_count``
        Every report filed against this person's content of that kind, counted the moment the
        report row is created. Includes reports a moderator later dismisses, so it is a measure
        of *how often people complained*, not of wrongdoing.
    ``*_removal_count``
        Only those reports where a moderator took the content down. This is the authoritative
        one -- it is the "report identity" of the participant, and the only one anything should
        be decided on -- because every report counted in the first column that was dismissed was
        somebody being wrong about content a moderator found fine.

    The split is the whole point of storing both. A single number cannot distinguish a
    participant whose content was reported once and vindicated from one whose content was taken
    down once, and the second is the person an administrator needs to notice.

    **Confessions and comments share one pair of columns.** A report queue does not separate
    them (``ReportRecord`` discriminates by which nullable foreign key is set, and the Reports
    Review screen is one screen), and a moderator's judgement about somebody writing a comment
    on a confession is the same judgement as about the confession itself. Splitting them would
    mean two numbers that no screen ever shows side by side and an admin would have to add up
    mentally.

    **How they are kept.** ``server_default`` of zero so a row written by a script or a raw
    insert starts at zero rather than NULL, and the increments happen as SQL ``UPDATE ... SET n
    = n + 1`` inside the same transaction as the report row or the removal stamp. Read-modify-
    write through the ORM would lose an increment whenever two reports for the same person were
    filed at once. See ``app.services.report_counters``, which is the only thing that writes
    them.
    """

    __tablename__ = "users"

    # Every element must be one of the twelve values in ``limits.INTEREST_VALUES``, expressed as
    # array containment. Interests used to be free text typed into a comma-separated box, which
    # made "music", "Music" and "musics" three interests that never matched each other and no
    # screen could tell apart; matchmaking scores on them, so a typo is not cosmetic.
    #
    # ``<@`` is "is contained by", so this is the one operator that reads the way the rule
    # does. NULL is excluded explicitly because a CHECK passes when it evaluates to NULL, and
    # ``NULL <@ ARRAY[...]`` is NULL rather than false.
    __table_args__ = _ACCOUNT_TABLE_ARGS + (
        CheckConstraint(
            f"interests IS NOT NULL AND interests <@ {_INTEREST_SQL_ARRAY}",
            name="ck_users_interests_known",
        ),
    )

    emoji: Mapped[Optional[str]] = mapped_column(String(16))
    bio: Mapped[Optional[str]] = mapped_column(String(500))
    class_name: Mapped[str] = mapped_column(String(80), index=True)
    # The element width is the length of the longest entry in the vocabulary, and it is derived
    # from that rather than written as a bare 80 -- so a longer interest cannot be added to
    # ``limits.INTEREST_VALUES`` and then be rejected by the column as an unexplained 500. The
    # CHECK constraint is the real rule; this is just enough room to hold what it permits.
    interests: Mapped[list[str]] = mapped_column(
        ARRAY(String(max(len(value) for value in INTEREST_VALUES))),
        default=list,
        server_default=text("'{}'"),
    )
    matchmaking_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    love_notes_receive_enabled: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    love_notes_send_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    last_matchmaking_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # When this participant last got a confession of their own into the feed. Nullable, and
    # that is the whole state: a participant who has never confessed may confess now, so
    # "no value" is eligibility rather than an uninitialised row. The same shape as
    # ``last_matchmaking_at`` for the same reason -- a cooldown is a comparison against a
    # stored instant, and it is claimed by a conditional UPDATE rather than read-then-written,
    # so two simultaneous posts cannot both pass the check.
    last_confession_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    confession_report_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    confession_removal_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    love_note_report_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    love_note_removal_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    message_report_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    message_removal_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class AdminRecord(AccountColumns, Base):
    """A staff account with the account-lifecycle capabilities. No participant fields."""

    __tablename__ = "admin"

    # An admin row can never be blocked, and that is a fact about the *table* rather than about
    # any one code path -- so it is a CHECK constraint and not a guard in a route. A route-level
    # check is only as good as the number of routes that remember it: `set_user_block_state`
    # resolves its target by id across all three tables, and a raw UPDATE, a future endpoint, a
    # script or an operator at a psql prompt would all walk straight past an `if` statement. The
    # rule that an administrator cannot be silenced by another administrator is exactly the kind
    # of rule that must not depend on everybody remembering it, because the day it is forgotten
    # is the day the last admin who can undo it locks everyone out of account recovery.
    #
    # ``IS NOT TRUE`` rather than ``= false`` because ``is_blocked`` is nullable and a CHECK
    # passes when it evaluates to NULL. ``= false`` would therefore also let a NULL through
    # without saying so; this states the actual rule -- the value may not be true -- and lets a
    # legacy NULL stay put instead of failing a boot over it.
    #
    # Added ``NOT VALID`` on a pre-existing database by ``PostgresClient.apply_check_constraints``,
    # which enforces it on every insert and update from that point on while leaving already-stored
    # rows alone. A blocked admin left over from before this rule therefore survives boot and is
    # reported rather than being silently repaired, and the first write to that row fails loudly.
    __table_args__ = _ACCOUNT_TABLE_ARGS + (
        CheckConstraint("is_blocked IS NOT TRUE", name="ck_admin_not_blockable"),
    )


class ModeratorRecord(AccountColumns, Base):
    """A staff account with the content-moderation capabilities. No participant fields."""

    __tablename__ = "moderator"
    __table_args__ = _ACCOUNT_TABLE_ARGS


# The one place a table name becomes a model. ``AccountType`` is a plain string enum with no
# ORM knowledge, so the join lives here, next to the classes it joins, rather than in
# ``app.accounts`` (which ``db_models`` imports and so cannot import back).
ACCOUNT_MODELS: dict[str, type[Base]] = {
    AccountType.USERS.value: UserRecord,
    AccountType.ADMIN.value: AdminRecord,
    AccountType.MODERATOR.value: ModeratorRecord,
}


class RemovedContentColumns:
    """The columns every *content* table carries so a row can be taken down but kept.

    A reported post is not deleted when a moderator rules on it: it is marked removed. The
    row survives, the report that is about it survives, and the content itself disappears from
    every participant-facing read. Deleting would cascade the report away with it and leave
    nobody able to answer "why was this taken down, and who decided" six months later.

    Three things read these columns, and the split between them is the point:

    * every participant-facing query filters on ``removed_at IS NULL``;
    * staff surfaces do not filter, and carry the flag instead, because a moderator deciding
      whether to remove something has to be able to see what is already removed;
    * ``asset_delivery_service`` uses them to stop serving a removed love note's image, which
      closing the inbox query alone would not do -- an already-minted delivery URL would
      otherwise keep working.

    No index on either column. Every read that matters filters ``removed_at IS NULL``, and this is
    a small table whose rows are added as content arrives rather than by retro-fitting a backlog;
    an index on the ``IS NULL`` side of a low-selectivity predicate costs writes on every insert
    and buys nothing the planner can use. ``removed_by_regno`` is never filtered at all -- it is
    read to answer "who did this", which is a per-report lookup by id. Add indexes here if the feed
    ever grows enough for the planner to want them.

    ``removed_by_regno`` is plain text, like every ``reviewed_by_regno`` in this schema: the
    reviewer is a staff account whose regno lives in ``admin`` or ``moderator`` and never in
    ``users``, so a foreign key could only ever reject a moderator's decision.
    """

    removed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    removed_by_regno: Mapped[Optional[str]] = mapped_column(String(100))

    @property
    def is_removed(self) -> bool:
        return self.removed_at is not None


class ConfessionRecord(RemovedContentColumns, Base):
    __tablename__ = "confessions"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    body: Mapped[str] = mapped_column(Text)
    recipient_name: Mapped[Optional[str]] = mapped_column(String(160))
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    comments_enabled: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    author_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    approved: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"), index=True)
    report_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    reactions: Mapped[dict[str, list[str]]] = mapped_column(JSONB, default=dict, server_default=text("'{}'::jsonb"))
    heart_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    haha_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    whoa_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    heartbreak_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    comment_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    __table_args__ = (
        CheckConstraint(
            f"length(body) <= {CONFESSION_BODY_MAX}",
            # Named for the limit, not just "the length". apply_check_constraints only adds
            # what is missing, so tightening CONFESSION_BODY_MAX while keeping this name would
            # leave every deployed database enforcing the old number forever, with nothing
            # anywhere reporting that the constraint and the model now disagree. The suffix is
            # the new ceiling; dropping a limit means adding a third name beside this one.
            name=f"ck_confessions_body_length_{CONFESSION_BODY_MAX}",
        ),
        # The database half of "only storable characters", covering the half the model cannot:
        # these still hold for writes that never pass through the API.
        #
        # Two characters the model rejects are not this constraint's problem to solve. A NUL byte
        # cannot be inside a row at all, and a lone surrogate has no UTF-8 encoding, so both fail
        # before a CHECK is ever consulted. Control characters, DEL, the C1 block and the Unicode
        # non-characters are the opposite case -- PostgreSQL stores every one of them without
        # complaint, which is exactly why they need to be forbidden explicitly.
        CheckConstraint(
            "body !~ '[\\u0000-\\u0008\\u000B\\u000C\\u000E-\\u001F\\u007F-\\u009F]'",
            name="ck_confessions_body_characters",
        ),
        # Its own constraint rather than a second arm on the one above, because
        # ``apply_check_constraints`` only adds what is missing. Editing the SQL of an
        # installed constraint would leave the running database on the old text forever with no
        # error anywhere, so a rule that grows has to arrive under a new name.
        CheckConstraint(
            f"body !~ '{_NON_CHARACTERS_PATTERN}'",
            # Named for the rule rather than for the first attempt at it, so that any database
            # which already installed an earlier version adds this one instead of keeping a
            # constraint whose pattern no longer matches what this module generates.
            name="ck_confessions_body_unicode_noncharacters",
        ),
    )


class ConfessionCommentRecord(RemovedContentColumns, Base):
    __tablename__ = "confession_comments"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    confession_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("confessions.id", ondelete="CASCADE"), index=True)
    user_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    likes: Mapped[list[str]] = mapped_column(ARRAY(Uuid(as_uuid=False)), default=list, server_default=text("'{}'"))
    dislikes: Mapped[list[str]] = mapped_column(ARRAY(Uuid(as_uuid=False)), default=list, server_default=text("'{}'"))
    like_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))
    dislike_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))

    __table_args__ = (
        CheckConstraint(
            f"length(body) <= {COMMENT_BODY_MAX}",
            # The limit is part of the name, as ``ck_confessions_body_length_500`` does it. It
            # has to be: reconciliation works by name, so a constraint called
            # ``ck_confession_comments_body_length`` is added once and then never checked again.
            # Changing ``COMMENT_BODY_MAX`` afterwards would leave the old ceiling in force
            # forever, and the API would reject a length the database still permitted.
            name=f"ck_confession_comments_body_length_{COMMENT_BODY_MAX}",
        ),
    )
    report_count: Mapped[int] = mapped_column(Integer, default=0, server_default=text("0"))


class ReportRecord(Base):
    """A participant's report that a confession, or a comment on one, breaks the rules.

    One table for both targets, discriminated by which of the two nullable foreign keys is
    set; ``ck_reports_one_target`` makes "both" and "neither" impossible at the database
    level rather than in application code.

    The three lifecycle columns exist so this queue can be closed out the same way the
    message-report and love-note-report queues are. They were added when the unified Reports
    Review screen was built, and every pre-existing row backfills to ``'pending'``: a report
    nobody had looked at is pending, which is what it was being treated as before the
    lifecycle was recorded at all.

    ``status`` carries the whole vocabulary, and it has exactly two closed outcomes:

    ``pending``
        Nobody has ruled on it yet.
    ``removed``
        A moderator ruled that the reported content broke the rules and took it down. The
        content row is marked removed rather than deleted, and both parties are notified.
    ``dismissed``
        A moderator looked and the content stays. The reporter is told it was reviewed.

    ``reviewed`` was the older word for "a human looked at this" and no longer has a button
    behind it; ``reviewed_at``/``reviewed_by_regno`` are what now record that a human looked,
    independently of which of the two outcomes they chose. Rows closed under the old
    vocabulary still read back as ``reviewed`` and are shown as such.

    The reported party is not stored. For a confession it is ``confessions.author_id`` and
    for a comment it is ``confession_comments.user_id`` -- both foreign keys that cascade, and
    both one join away in a table the queue already reads to show the content. A denormalised
    column could only ever disagree with its source.
    """

    __tablename__ = "reports"
    __table_args__ = (
        CheckConstraint("(confession_id IS NOT NULL) <> (comment_id IS NOT NULL)", name="ck_reports_one_target"),
        UniqueConstraint("confession_id", "reported_by_id", name="uq_reports_confession_reporter"),
        UniqueConstraint("comment_id", "reported_by_id", name="uq_reports_comment_reporter"),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    confession_id: Mapped[Optional[str]] = mapped_column(Uuid(as_uuid=False), ForeignKey("confessions.id", ondelete="CASCADE"), index=True)
    comment_id: Mapped[Optional[str]] = mapped_column(Uuid(as_uuid=False), ForeignKey("confession_comments.id", ondelete="CASCADE"), index=True)
    reported_by_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    reported_by_name: Mapped[str] = mapped_column(String(160))
    reason: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), index=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # No foreign key: the value names an account in ``moderator`` or ``admin``, which a
    # foreign key to ``users`` could never point at.
    reviewed_by_regno: Mapped[Optional[str]] = mapped_column(String(100), index=True)


class LoginTokenRecord(Base):
    __tablename__ = "login_tokens"

    # A magic link is minted for an account in whichever of the three tables that account
    # lives in, so there is no single table this id can point at and the old ``users.id``
    # foreign key had to go. The pair below replaces it: the id identifies the row, and
    # ``account_type`` says which table to read it from. The CHECK keeps the two in step --
    # a row id without a resolvable table would be a login that cannot be redeemed.
    #
    # NULL is excluded explicitly for the same reason as in a bare ``IN``: NULL would
    # evaluate to NULL, which a CHECK constraint treats as passing.
    __table_args__ = (
        CheckConstraint(
            f"account_type IS NOT NULL AND account_type IN ({_ACCOUNT_TYPE_SQL_LIST})",
            name="login_tokens_account_type_valid",
        ),
    )

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), index=True)
    # The server default exists only so the column can be added to a table that predates the
    # split, where the identity of the account is genuinely unknown. It backfills ``users``,
    # the least-privileged reading, so a token that outlives this change is refused rather
    # than resolved against a table nobody can confirm.
    account_type: Mapped[str] = mapped_column(String(16), index=True, server_default=text("'users'"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    issued_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    consumed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    used: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    request_ip: Mapped[str] = mapped_column(INET)
    request_user_agent: Mapped[str] = mapped_column(String(512))
    consume_ip: Mapped[Optional[str]] = mapped_column(INET)
    consume_user_agent: Mapped[Optional[str]] = mapped_column(String(512))
    attempt_count: Mapped[int] = mapped_column(SmallInteger, default=0, server_default=text("0"))
    session_status: Mapped[str] = mapped_column(String(16), default="issued", server_default=text("'issued'"), index=True)
    last_seen_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    device: Mapped[Optional[str]] = mapped_column(String(512))


class LoveNoteRecord(RemovedContentColumns, Base):
    __tablename__ = "love_notes"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    sender_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    recipient_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("users.id", ondelete="CASCADE"), index=True)
    image_url: Mapped[str] = mapped_column(Text)
    body: Mapped[str] = mapped_column(String(2000))
    anonymous: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    status: Mapped[str] = mapped_column(String(24), default="pending_review", server_default=text("'pending_review'"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    read_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class LoveNoteReportRecord(Base):
    """A recipient's report that a love note they received breaks the rules.

    Kept in its own table rather than as a flag on ``love_notes`` for the same reason
    ``message_reports`` is separate from ``messages``: the report is a *review queue item*
    with a lifecycle of its own (pending -> reviewed/dismissed) and an audit trail naming
    who looked at it, and a note the recipient has reported is still a note staff must be
    able to read, approve or reject on its own merits.

    A note has exactly one recipient, so ``(love_note_id, reporter_regno)`` is unique in
    practice as well as on paper: the same person cannot file the same complaint twice, and
    nobody else is entitled to file one.

    The reported party is deliberately **not** denormalised into a second regno column.
    ``love_notes.sender_id`` is a foreign key that cascades, so a report can never outlive
    the note it is about, and the sender is one join away in a table that has to be read
    anyway to show the note. Storing it would add a column that can only ever disagree with
    its source.
    """

    __tablename__ = "love_note_reports"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    love_note_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("love_notes.id", ondelete="CASCADE"), index=True)
    # The reporter is always the note's recipient, and the regno rather than the id because
    # this is the column the staff queue groups and displays by.
    reporter_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    # Capped in the database as well as by Pydantic, so a write that does not come through
    # the API cannot store a reason longer than the application claims to accept.
    reason: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), index=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # No foreign key, for the same reason as message_reports.reviewed_by_regno: the reviewer
    # is a staff account whose regno lives in ``admin`` or ``moderator``, never in ``users``,
    # so a FK would reject every moderator's sign-off.
    reviewed_by_regno: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    __table_args__ = (
        UniqueConstraint("love_note_id", "reporter_regno"),
        CheckConstraint(f"length(reason) <= {REPORT_REASON_MAX}", name="ck_love_note_reports_reason_length"),
    )


class LoveNoteTemplateRecord(Base):
    __tablename__ = "love_note_templates"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(100), unique=True)
    image_url: Mapped[str] = mapped_column(Text)
    description: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda: datetime.now(timezone.utc))


class MatchRecord(Base):
    __tablename__ = "matches"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    user_1_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    user_2_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    # Canonical "lower|upper" form of the two regnos, so a pair is identified the same way
    # regardless of who initiated. Unique, which is what stops two concurrent requests
    # from matching the same two people. Cleared to NULL when the match lapses, and NULLs
    # are not compared by a unique index, so an expired match frees the pair for good
    # without needing a background job to sweep it.
    pair_key: Mapped[Optional[str]] = mapped_column(String(201), unique=True, index=True)
    __table_args__ = (CheckConstraint("user_1_regno <> user_2_regno", name="ck_matches_distinct_users"),)


class ConversationRecord(Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    match_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("matches.id", ondelete="CASCADE"), unique=True, index=True)
    initiator_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    receiver_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), index=True)
    requested_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_message_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    last_message_preview: Mapped[Optional[str]] = mapped_column(String(160))
    blocked_by: Mapped[list[str]] = mapped_column(ARRAY(String(100)), default=list, server_default=text("'{}'"))
    terminated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))


class MessageRecord(RemovedContentColumns, Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    conversation_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("conversations.id", ondelete="CASCADE"), index=True)
    sender_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    receiver_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    body: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    __table_args__ = (
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
        CheckConstraint(
            f"length(body) <= {MESSAGE_BODY_MAX}",
            # Value in the name, for the same reason as the comment-body constraint above. A
            # stable name here meant the installed constraint was accepted as-is however stale
            # its definition: this database was holding ``length(body) <= 2000`` against a model
            # that says 100, and a direct write of 101 characters was accepted while the API
            # refused it.
            name=f"ck_messages_body_length_{MESSAGE_BODY_MAX}",
        ),
    )


class NotificationRecord(Base):
    """Something worth telling a participant, and -- when there is one -- what to open.

    ``target_kind`` and ``target_id`` are what a click on the notification resolves to. They
    are nullable and always null together, because a good share of notices have nothing to
    point at: a match that was drawn, a report that was reviewed, content that was taken down.
    The item those notices describe either does not exist any more or was never the
    participant's to open, so a target would send them somewhere that cannot answer the
    notification. The honest answer for those rows is "nothing to open", and a null pair says
    that without the client having to re-guess it from the heading text.

    Nullable also matters for how they arrive: ``apply_missing_columns`` skips a NOT NULL
    column that has no server default, so a non-nullable pair would leave every already-deployed
    database without the columns and silently non-clickable notifications forever.
    """

    __tablename__ = "notifications"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    recipient_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    heading: Mapped[str] = mapped_column(String(120))
    body: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    is_read: Mapped[bool] = mapped_column(Boolean, default=False, server_default=text("false"))
    # The kind names the table the id belongs to, so a client never has to guess how to read
    # the id. Deliberately plain text rather than a foreign key, and deliberately not one
    # column per kind: the target of a message-removal notice does not exist, the target of a
    # conversation notice is a match rather than a conversation, and the set of things a
    # notification can point at is exactly the set of kinds the client knows how to open.
    target_kind: Mapped[Optional[str]] = mapped_column(String(40))
    target_id: Mapped[Optional[str]] = mapped_column(String(100))


class MatchmakingSlotRecord(Base):
    __tablename__ = "matchmaking_slots"

    # A slot defines which classes may match with which. A slot with class_a and class_b means
    # class_a can match with class_b. Multiple slots can overlap (e.g. A-B and B-C means A can
    # match B and C, but A and C cannot match unless there is a separate slot A-C).
    # A slot with a single class (class_a == class_b) means that class can match with itself.
    # Initially there will be a single slot with all classes, meaning any class can match with any.
    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    class_a: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    class_b: Mapped[str] = mapped_column(String(80), index=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_now, onupdate=_now, server_default=func.now()
    )

    # No constraint - self-matching (class_a == class_b) is allowed for single-class slots


class MessageReportRecord(Base):
    __tablename__ = "message_reports"

    id: Mapped[str] = mapped_column(Uuid(as_uuid=False), primary_key=True, default=_uuid)
    message_id: Mapped[str] = mapped_column(Uuid(as_uuid=False), ForeignKey("messages.id", ondelete="CASCADE"), index=True)
    reporter_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    reported_user_regno: Mapped[str] = mapped_column(String(100), ForeignKey("users.regno", ondelete="CASCADE"), index=True)
    reason: Mapped[str] = mapped_column(String(1000))
    status: Mapped[str] = mapped_column(String(20), default="pending", server_default=text("'pending'"), index=True)
    reported_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    reviewed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    # No foreign key. The reviewer is a staff account, so its regno lives in ``admin`` or
    # ``moderator`` and never in ``users`` -- a FK to ``users.regno`` would reject every
    # moderator's sign-off. Kept as plain text so the audit trail survives the reviewer's
    # account being deleted or renamed, which is what ``ON DELETE SET NULL`` was doing.
    reviewed_by_regno: Mapped[Optional[str]] = mapped_column(String(100), index=True)
    __table_args__ = (UniqueConstraint("message_id", "reporter_regno"),)


# ---------------------------------------------------------------------------
# Cross-table account lookup
# ---------------------------------------------------------------------------
# A regno or an id is unique *within* one account table, not across all three. Login says
# which table it means, so the resolution that matters at request time is
# :func:`find_account_in_table`, which reads exactly one named table and never has to break
# a tie.
#
# :func:`resolve_account_by_regno` and :func:`resolve_account_by_id` still exist and still
# search all three, but they are only for the places where no table is known and none is
# needed to be right: showing an admin which conversations a regno is attached to, and
# refusing a create that would reuse a regno on a table where it would be ambiguous to a
# human. Nothing that decides *who is calling* uses them -- a caller is always identified by
# the table and primary key their credential recorded, never by a search.

def find_account_in_table(session, account_type: Optional[str], regno: Optional[str]) -> Optional[tuple[str, Any]]:
    """Find the account holding ``regno`` in one named table, as ``(account_type, row)``.

    The lookup behind login. The table is supplied by the caller -- by the page the person
    chose to sign in through -- so a regno that exists in two tables resolves to the one
    that page was for, and a regno that exists only in a different table is simply not
    found rather than quietly resolving to somebody else's account.

    An unrecognised ``account_type`` returns None instead of falling back to a default
    table. :func:`normalize_account_type` would answer such a value with ``users``, which is
    the right fail-closed answer for a *stored* discriminator whose privileges are being
    read, and exactly the wrong one here: a request that named no real account table must
    not be answered out of the participant table.
    """
    if not regno or not is_valid_account_type(account_type):
        return None
    model = ACCOUNT_MODELS[normalize_account_type(account_type)]
    row = session.scalar(select(model).where(model.regno == regno))
    if row is None:
        return None
    return normalize_account_type(account_type), row


def resolve_account_by_regno(session, regno: Optional[str]) -> Optional[tuple[str, Any]]:
    """Find the account holding ``regno``, as ``(account_type, row)`` or None."""
    if not regno:
        return None
    for account_type, model in ACCOUNT_MODELS.items():
        row = session.scalar(select(model).where(model.regno == regno))
        if row is not None:
            return account_type, row
    return None


def resolve_account_by_id(session, account_id: Optional[str]) -> Optional[tuple[str, Any]]:
    """Find the account with this primary key, as ``(account_type, row)`` or None."""
    if not _is_account_id(account_id):
        return None
    for account_type, model in ACCOUNT_MODELS.items():
        row = session.get(model, str(account_id))
        if row is not None:
            return account_type, row
    return None


def resolve_account(session, account_type: Optional[str], account_id: Optional[str]) -> Optional[Any]:
    """Fetch one account from one named table, or None if that table has no such row.

    Used wherever the table is already known -- a magic-link token carries the account kind
    it was minted for, so redemption reads exactly one table instead of searching three.
    """
    if not _is_account_id(account_id):
        return None
    return session.get(ACCOUNT_MODELS[normalize_account_type(account_type)], str(account_id))


def _is_account_id(value: Optional[str]) -> bool:
    """Whether ``value`` is a well-formed account primary key.

    Duplicated rather than imported from ``app.postgres`` because that module imports this
    one; the check is four lines and importing it would close the cycle.
    """
    if not value:
        return False
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False
