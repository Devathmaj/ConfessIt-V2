from ..logger import get_logger
from fastapi import HTTPException, status
from sqlalchemy import Float, case, cast, func, or_, select, update
from ..postgres import PostgresClient, get_postgres_client
from ..db_models import ConfessionCommentRecord, ConfessionRecord, ReportRecord, UserRecord
from ..models import Confession, ConfessionComment, ConfessionCreate, CommentCreate, UserDetails, UserInfo, ReportCreate, ConfessionUpdate
import datetime
from datetime import timezone
from typing import Optional, List

from .report_counters import CONFESSION, record_report_filed
from .storage_service import storage_service

logger = get_logger(__name__)

# A participant may post one confession every CONFESSION_COOLDOWN_HOURS. The feed is
# read one page at a time, so the throttle is what stops one account from filling it on
# its own.
CONFESSION_COOLDOWN_HOURS = 2

# Hard ceiling for one feed page. The endpoint clamps anything larger, so no client can
# ask for every confession -- with every comment and user row attached -- and make the
# backend materialise the whole feed in one response.
FEED_MAX_LIMIT = 100

# A single confession's thread opens with this many newest comments, and grows by
# this many per "show more" click. The dialog reads the thread, and an unbounded one
# would let a single viral post blow past the page bound the feed is read under;
# ``comment_count`` still carries the true total beside either window.
FEED_COMMENTS_INITIAL = 10


def _cooldown_message(seconds_remaining: float) -> str:
    """The sentence a user is shown, from the number of seconds left.

    Deliberately quotes the wait rather than a bare timestamp: the cooldown is the answer to
    "why did my post fail", and a wall-clock instant is unreadable to a person who has
    already closed the app for an hour.
    """
    hours, remainder = divmod(int(seconds_remaining), 3600)
    minutes = remainder // 60
    if hours > 0:
        return (
            f"You can post one confession every {CONFESSION_COOLDOWN_HOURS} hours. "
            f"Please try again in {hours} hour{'s' if hours != 1 else ''} and {minutes} minutes."
        )
    return (
        f"You can post one confession every {CONFESSION_COOLDOWN_HOURS} hours. "
        f"Please try again in {minutes} minute{'s' if minutes != 1 else ''}."
    )


def _consume_confession_attempt(session, user_id: str, now: datetime.datetime) -> bool:
    """Atomically claim the caller's one confession for this window.

    The conditional UPDATE is the only thing serialising the cooldown. Concurrent callers
    contend on the same row lock, and because PostgreSQL re-evaluates the predicate against
    the committed version of the row, only the winner sees a row to update. Reading
    ``last_confession_at`` and writing it in a later transaction instead would leave a window
    in which every concurrent request observes "eligible" -- which is the same reasoning, and
    the same mechanism, as ``matchmaking_service._consume_matchmaking_attempt``.

    Returns True if this caller won the window.
    """
    cooldown_cutoff = now - datetime.timedelta(hours=CONFESSION_COOLDOWN_HOURS)
    result = session.execute(
        update(UserRecord)
        .where(
            UserRecord.id == str(user_id),
            or_(
                UserRecord.last_confession_at.is_(None),
                UserRecord.last_confession_at < cooldown_cutoff,
            ),
        )
        .values(last_confession_at=now)
    )
    return result.rowcount == 1


def _seconds_until_confession_eligible(session, user_id: str, now: datetime.datetime) -> float:
    """How long this caller still has to wait. Zero or less means they may post now.

    Read through ``session`` when one is supplied so a caller that has just lost the claim
    reports the wait against the committed row rather than a stale local copy.
    """
    if session is not None:
        last = session.scalar(
            select(UserRecord.last_confession_at).where(UserRecord.id == str(user_id))
        )
    else:
        with get_postgres_client().sessions() as own_session:
            last = own_session.scalar(
                select(UserRecord.last_confession_at).where(UserRecord.id == str(user_id))
            )

    if last is None:
        return 0.0
    # The column is TIMESTAMPTZ, but a row written by a script or a hand-edited fixture can
    # arrive naive. Cooldown arithmetic across a naive and an aware datetime raises, and
    # refusing to post with a 500 because of a timezone is the wrong way to be strict.
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)

    eligible_at = last + datetime.timedelta(hours=CONFESSION_COOLDOWN_HOURS)
    return (eligible_at - now).total_seconds()


def _cooldown_rejection(seconds_remaining: float) -> HTTPException:
    """429 with the wait in the message.

    429 rather than 403: nothing is wrong with the caller's right to confess, only with the
    time. A 403 would be indistinguishable from being refused for good.
    """
    return HTTPException(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        detail=_cooldown_message(max(seconds_remaining, 0)),
        headers={"Retry-After": str(max(int(seconds_remaining), 0))},
    )


def _profile_picture_url(user) -> Optional[str]:
    """The author's photo as a delivery URL, or None when they have not set one.

    Derived at read time from the stored public id rather than cached on the post, so a
    picture changed or removed later is reflected immediately everywhere it is shown.
    Accepts either a ``UserRecord`` (which holds the Cloudinary identity) or a
    ``UserDetails`` (which already carries the derived URL, as the comment-posting caller
    does).
    """
    if user is None:
        return None
    if hasattr(user, "profile_picture_public_id"):
        return storage_service.profile_delivery_url(
            user.profile_picture_public_id, user.profile_picture_format
        )
    return getattr(user, "profile_picture_url", None)


class ConfessionService:
    def __init__(self, client: Optional[PostgresClient] = None):
        # Injected so the service never creates its own engine: an engine per instance
        # would mean an unbounded pool, since this service is constructed per request.
        self.client = client if client is not None else get_postgres_client()
        self.sessions = self.client.sessions
        self.REACTION_TYPES = ["heart", "haha", "whoa", "heartbreak"]

    def _reporter_ids(self, target_id: str) -> List[str]:
        with self.sessions() as session:
            return [str(report.reported_by_id) for report in session.scalars(select(ReportRecord).where(or_(
                ReportRecord.confession_id == str(target_id), ReportRecord.comment_id == str(target_id)
            ))).all()]

    def _get_user_info(self, user_id: str) -> Optional[UserInfo]:
        with self.sessions() as session:
            user_doc = session.get(UserRecord, str(user_id))
        if user_doc:
            return UserInfo(
                id=str(user_doc.id), name=user_doc.name or "Anonymous User",
                profile_picture_url=_profile_picture_url(user_doc),
            )
        return None

    @staticmethod
    def _confession_payload(row):
        return {"id": str(row.id), "body": row.body, "recipient_name": row.recipient_name,
            "anonymous": row.anonymous, "comments_enabled": row.comments_enabled, "created_at": row.created_at,
            "author_id": str(row.author_id), "approved": row.approved, "report_count": row.report_count,
            "reactions": row.reactions or {}, "heart_count": row.heart_count, "haha_count": row.haha_count,
            "whoa_count": row.whoa_count, "heartbreak_count": row.heartbreak_count,
            "comment_count": row.comment_count}

    @staticmethod
    def _comment_payload(row, user=None):
        return {"id": str(row.id), "confession_id": str(row.confession_id), "user_id": str(row.user_id),
            "user_info": {"id": str(row.user_id), "name": user.name if user else "User",
                "profile_picture_url": _profile_picture_url(user)},
            "body": row.body, "created_at": row.created_at, "likes": [str(x) for x in (row.likes or [])],
            "dislikes": [str(x) for x in (row.dislikes or [])], "like_count": row.like_count,
            "dislike_count": row.dislike_count, "report_count": row.report_count}

    def create_confession(self, confession_data: ConfessionCreate, user_id: str):
        # recipient_name arrives already normalized: trimmed, or None when the confession
        # is not addressed to anyone. Both spellings mean the same thing, so both are NULL.
        confession_dict = confession_data.model_dump()
        now = datetime.datetime.now(datetime.timezone.utc)

        # One confession per participant every CONFESSION_COOLDOWN_HOURS. Read-then-write
        # would leave a window in which two simultaneous posts both see "eligible" and both
        # land, so the cooldown is claimed by the same conditional UPDATE that
        # _consume_matchmaking_attempt uses: PostgreSQL re-evaluates the predicate against
        # the committed row under the lock, so only one caller sees a row to update.
        with self.sessions.begin() as session:
            if not _consume_confession_attempt(session, user_id, now):
                raise _cooldown_rejection(
                    _seconds_until_confession_eligible(session, user_id, now)
                )
            row = ConfessionRecord(
                body=confession_dict["body"], recipient_name=confession_dict.get("recipient_name"),
                anonymous=confession_dict["anonymous"], comments_enabled=confession_dict["comments_enabled"],
                created_at=now, author_id=user_id, reactions={reaction: [] for reaction in self.REACTION_TYPES},
            )
            session.add(row)
            session.flush()
            confession_id = str(row.id)
        logger.info(f"Created confession {confession_id} for user {user_id}")
        return self.get_confession(confession_id, user_id)

    def get_confession_cooldown(self, user_id: str) -> dict:
        """Advisory: may this participant confess right now, and if not, when can they?

        The decision itself is made by the conditional UPDATE in ``create_confession``, which
        is the only thing that serialises it. This exists so the compose screen can show the
        wait before the user has typed anything, and it therefore must not be trusted as an
        authorization check -- the write re-derives the answer itself.
        """
        now = datetime.datetime.now(datetime.timezone.utc)
        seconds = _seconds_until_confession_eligible(None, str(user_id), now)
        if seconds <= 0:
            return {
                "status": "eligible",
                "message": f"You can post one confession every {CONFESSION_COOLDOWN_HOURS} hours.",
                "cooldown_hours": CONFESSION_COOLDOWN_HOURS,
                "remaining_minutes": 0,
                "remaining_seconds": 0,
            }
        return {
            "status": "cooldown",
            "message": _cooldown_message(seconds),
            "cooldown_hours": CONFESSION_COOLDOWN_HOURS,
            "remaining_minutes": int(seconds // 60),
            "remaining_seconds": int(seconds),
        }

    def _hydrate_comments(
        self, session, comment_models, user_id: Optional[str], user_map=None,
    ) -> list:
        """Comment rows into response payloads, in input order: authors, reporter ids and
        the caller's own like/dislike, all in bulk.

        One users read and one report read no matter how many comments are handed in --
        the per-row lookups this replaces opened a fresh session for every comment on
        every post. Shared by the feed (which hydrates one page's threads and hands in
        the map it already read) and the single-thread read below.
        """
        if user_map is None:
            user_ids = {str(row.user_id) for row in comment_models}
            user_map = {
                user.id: user for user in session.scalars(
                    select(UserRecord).where(UserRecord.id.in_(user_ids))
                ).all()
            } if user_ids else {}
        ids = [str(row.id) for row in comment_models]
        reporters: dict = {}
        if ids:
            for target_id, reporter_id in session.execute(
                select(ReportRecord.comment_id, ReportRecord.reported_by_id).where(
                    ReportRecord.comment_id.in_(ids)
                )
            ).all():
                reporters.setdefault(str(target_id), []).append(str(reporter_id))
        hydrated = []
        for row in comment_models:
            payload = self._comment_payload(row, user_map.get(row.user_id))
            reporter_ids = reporters.get(str(row.id), [])
            # Participant-scoped (SEC-002): the caller learns only whether *they*
            # reported, never who else did; the count stays so the UI can show it.
            payload["has_reported"] = bool(user_id and user_id in reporter_ids)
            payload["reported_by"] = []
            payload["report_count"] = len(reporter_ids)
            reaction = None
            if user_id:
                if user_id in payload.get('likes', []):
                    reaction = 'like'
                elif user_id in payload.get('dislikes', []):
                    reaction = 'dislike'
            payload['user_reaction'] = reaction
            # Voter ID lists never leave the server on participant reads; counts stay.
            payload['likes'] = []
            payload['dislikes'] = []
            hydrated.append(payload)
        return hydrated

    def get_confession_comments(
        self, confession_id: str, user_id: Optional[str] = None,
        skip: int = 0, limit: int = 10, sort: str = 'latest',
    ) -> Optional[dict]:
        """One thread page for the dialog, with the total beside it.

        The feed embeds only the opening window of each thread; this is what the "show
        more" beneath it reads -- one SQL page plus the same bulk hydration as the feed,
        so opening a thread never loads what the dialog is not showing. ``total`` counts
        every visible comment on the post, loaded or not, under no order at all.

        ``sort`` is ``latest`` (newest first) or ``relevant``: the share of likes among
        all votes, so a comment with 3 likes and no dislikes outranks one with 6 likes
        and 4 dislikes. Votes are the denormalised counters, coalesced for hand-written
        rows; a comment with no votes at all scores zero rather than dividing by it.
        Ties break toward more likes, then newest, then id, so pages are stable.

        None when the confession is missing or taken down: a removed post reads as
        absent rather than as a placeholder, exactly as ``get_confession`` answers it.
        """
        skip = max(0, int(skip or 0))
        limit = max(1, min(int(limit or 10), FEED_MAX_LIMIT))
        if sort not in ("latest", "relevant"):
            from fastapi import HTTPException as _HTTPException, status as _status

            raise _HTTPException(
                status_code=_status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Invalid sort",
            )
        likes = func.coalesce(ConfessionCommentRecord.like_count, 0)
        dislikes = func.coalesce(ConfessionCommentRecord.dislike_count, 0)
        relevance = case(
            ((likes + dislikes) == 0, 0.0),
            else_=cast(likes, Float) / cast(likes + dislikes, Float),
        )
        if sort == "relevant":
            ordering = [
                relevance.desc(), likes.desc(),
                ConfessionCommentRecord.created_at.desc(), ConfessionCommentRecord.id.asc(),
            ]
        else:
            ordering = [ConfessionCommentRecord.created_at.desc(), ConfessionCommentRecord.id.asc()]
        with self.sessions() as session:
            row = session.get(ConfessionRecord, str(confession_id))
            if row is None or row.removed_at is not None:
                return None
            visible = [
                ConfessionCommentRecord.confession_id == str(confession_id),
                ConfessionCommentRecord.removed_at.is_(None),
            ]
            total = session.scalar(
                select(func.count()).select_from(ConfessionCommentRecord).where(*visible)
            ) or 0
            comment_models = session.scalars(
                select(ConfessionCommentRecord)
                .where(*visible)
                .order_by(*ordering)
                .offset(skip).limit(limit)
            ).all()
            payloads = self._hydrate_comments(session, comment_models, user_id)
        return {
            "comments": [ConfessionComment(**payload) for payload in payloads],
            "total": total, "skip": skip, "limit": limit,
        }

    def get_confessions(
        self, sort_by: str = 'time', user_id: Optional[str] = None,
        skip: int = 0, limit: int = 50,
    ) -> dict:
        """One feed page, sorted and counted in SQL.

        The sort picker is a SQL ORDER BY and every page a SQL LIMIT/OFFSET: with
        thousands of confessions the backend reads and serialises at most one page, never
        the whole feed, and ``total`` is a COUNT under the same filter so the screen can
        load more downwards without ever holding the posts it is not showing. Every
        relation behind a page -- comments, authors and commenters, reporter ids -- is one
        bulk read grouped in Python rather than a query per row, which is what the old
        full-table read did once per confession and once per comment on top of loading
        the entire users table for every visitor.
        """
        skip = max(0, int(skip or 0))
        limit = max(1, min(int(limit or 50), FEED_MAX_LIMIT))
        # Both tables are filtered on removed_at rather than deleted: a confession taken
        # down by a moderator still has a report pointing at it, and the row has to stay
        # for that report to mean anything. What changes is that it is no longer a thing
        # a participant can read, react to, or count. Staff surfaces deliberately do not
        # filter -- see app.services.report_resolution_service for why.
        visible = [ConfessionRecord.removed_at.is_(None)]
        if sort_by == "popularity":
            engagement = sum(
                func.coalesce(getattr(ConfessionRecord, f"{kind}_count"), 0)
                for kind in self.REACTION_TYPES
            )
            ordering = [engagement.desc(), ConfessionRecord.created_at.asc(), ConfessionRecord.id.asc()]
        elif sort_by == "comments":
            ordering = [
                func.coalesce(ConfessionRecord.comment_count, 0).desc(),
                ConfessionRecord.created_at.asc(), ConfessionRecord.id.asc(),
            ]
        else:
            # Newest first; the old Python sort broke creation-time ties by author name,
            # which no reader could observe and no index could serve, so the stable tail
            # is the id instead.
            ordering = [ConfessionRecord.created_at.desc(), ConfessionRecord.id.asc()]
        with self.sessions() as session:
            total = session.scalar(
                select(func.count()).select_from(ConfessionRecord).where(*visible)
            ) or 0
            confession_models = session.scalars(
                select(ConfessionRecord).where(*visible)
                .order_by(*ordering).offset(skip).limit(limit)
            ).all()
            confession_ids = [str(row.id) for row in confession_models]
            # Newest N comments per confession on the page, ranked in SQL: two bounded
            # reads no matter how long any single thread is, instead of every comment on
            # every post. ``comment_count`` on the row still carries the true total.
            ranked = (
                select(
                    ConfessionCommentRecord.id,
                    func.row_number().over(
                        partition_by=ConfessionCommentRecord.confession_id,
                        order_by=ConfessionCommentRecord.created_at.desc(),
                    ).label("thread_rank"),
                )
                .where(
                    ConfessionCommentRecord.removed_at.is_(None),
                    ConfessionCommentRecord.confession_id.in_(confession_ids),
                )
                .subquery()
                if confession_ids else None
            )
            kept_ids = session.scalars(
                select(ranked.c.id).where(ranked.c.thread_rank <= FEED_COMMENTS_INITIAL)
            ).all() if ranked is not None else []
            comment_models = session.scalars(
                select(ConfessionCommentRecord)
                .where(ConfessionCommentRecord.id.in_(kept_ids))
                .order_by(ConfessionCommentRecord.created_at.desc())
            ).all() if kept_ids else []
            # Only the accounts this page actually names: its authors and its commenters.
            # Reactors need no rows -- the feed carries counts plus id lists, never names.
            wanted_user_ids = {str(row.author_id) for row in confession_models}
            wanted_user_ids |= {str(row.user_id) for row in comment_models}
            user_map = {
                user.id: user for user in session.scalars(
                    select(UserRecord).where(UserRecord.id.in_(wanted_user_ids))
                ).all()
            } if wanted_user_ids else {}
            # Reporter ids in two grouped reads: one for the page's confessions, one for
            # its comments. The old code opened a fresh session per row for this.
            confession_reporters: dict = {}
            if confession_ids:
                for target_id, reporter_id in session.execute(
                    select(ReportRecord.confession_id, ReportRecord.reported_by_id).where(
                        ReportRecord.confession_id.in_(confession_ids)
                    )
                ).all():
                    confession_reporters.setdefault(str(target_id), []).append(str(reporter_id))
            confession_rows = [self._confession_payload(row) for row in confession_models]
            comments_by_confession = {}
            for comment in self._hydrate_comments(session, comment_models, user_id, user_map):
                comments_by_confession.setdefault(comment["confession_id"], []).append(comment)

        for confession in confession_rows:
            reporter_ids = confession_reporters.get(str(confession["id"]), [])
            # Participant-scoped (SEC-002): only the caller's own report state is
            # disclosed; reporter identities stay server-side. Counts are preserved.
            confession["has_reported"] = bool(user_id and user_id in reporter_ids)
            confession["reported_by"] = []
            confession["report_count"] = len(reporter_ids)
            confession_id = str(confession["id"])
            confession["comments"] = sorted(
                comments_by_confession.get(confession_id, []),
                key=lambda comment: comment.get("created_at") or datetime.datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            author = user_map.get(str(confession.get("author_id")))
            confession["user_info"] = {
                "id": str(confession.get("author_id")),
                "name": author.name if author else "Anonymous User",
                # Withheld on an anonymous post: a photo identifies its owner far more
                # directly than a name does, so it follows the same condition the client
                # uses to hide that name.
                "profile_picture_url": _profile_picture_url(author) if not confession.get("anonymous") else None,
            }

        confessions = []
        for confession_doc in confession_rows:
            comments_list = []
            for comment_doc in sorted(confession_doc.get('comments', []), key=lambda c: c['created_at'], reverse=True):
                # _hydrate_comments already derived this from the full voter lists
                # before stripping them; keep it rather than re-deriving from [].
                if comment_doc.get('user_reaction') is None and user_id:
                    if user_id in comment_doc.get('likes', []):
                        comment_doc['user_reaction'] = 'like'
                    elif user_id in comment_doc.get('dislikes', []):
                        comment_doc['user_reaction'] = 'dislike'
                comments_list.append(ConfessionComment(**comment_doc))
            confession_doc['comments'] = comments_list

            user_reaction = None
            if user_id and 'reactions' in confession_doc:
                for reaction_type, user_ids in confession_doc['reactions'].items():
                    if user_id in user_ids:
                        user_reaction = reaction_type
                        break
            confession_doc['user_reaction'] = user_reaction
            # Participant-scoped (SEC-002): reactor identities never leave the
            # server on participant reads; counts + the caller's own reaction do.
            confession_doc['reactions'] = {}

            confessions.append(Confession(**confession_doc))

        logger.info(f"Retrieved {len(confessions)} confessions sorted by {sort_by}")
        return {"confessions": confessions, "total": total, "skip": skip, "limit": limit}

    def get_confession(self, confession_id: str, user_id: Optional[str] = None) -> Optional[Confession]:
        with self.sessions() as session:
            row = session.get(ConfessionRecord, str(confession_id))
            # A removed confession reads as absent rather than as a placeholder. The row is
            # still there for the report about it, but this is the participant-facing getter
            # and "not found" is the honest answer for a post that has been taken down.
            if row is not None and row.removed_at is not None:
                row = None
            confession_doc = self._confession_payload(row) if row else None
        if confession_doc:
            reporter_ids = self._reporter_ids(confession_id)
            # Participant-scoped (SEC-002): identities stay server-side.
            confession_doc["has_reported"] = bool(user_id and user_id in reporter_ids)
            confession_doc["reported_by"] = []
            confession_doc["report_count"] = len(reporter_ids)
            user_reaction = None
            if user_id and 'reactions' in confession_doc:
                for reaction_type, user_ids in confession_doc['reactions'].items():
                    if user_id in user_ids:
                        user_reaction = reaction_type
                        break

            confession_doc['user_reaction'] = user_reaction
            confession_doc['reactions'] = {}

            with self.sessions() as session:
                comment_models = session.scalars(select(ConfessionCommentRecord).where(
                    ConfessionCommentRecord.confession_id == str(confession_id),
                    ConfessionCommentRecord.removed_at.is_(None),
                ).order_by(ConfessionCommentRecord.created_at.desc())).all()
                comment_users = {u.id: u for u in session.scalars(select(UserRecord).where(
                    UserRecord.id.in_({c.user_id for c in comment_models})
                )).all()} if comment_models else {}
            comments_cursor = [self._comment_payload(c, comment_users.get(c.user_id)) for c in comment_models]
            comments_list = []
            for comment_doc in comments_cursor:
                comment_reporter_ids = self._reporter_ids(comment_doc["id"])
                comment_doc["has_reported"] = bool(user_id and user_id in comment_reporter_ids)
                comment_doc["reported_by"] = []
                comment_doc["report_count"] = len(comment_reporter_ids)
                user_comment_reaction = None
                if user_id:
                    if user_id in comment_doc.get('likes', []):
                        user_comment_reaction = 'like'
                    elif user_id in comment_doc.get('dislikes', []):
                        user_comment_reaction = 'dislike'
                comment_doc['user_reaction'] = user_comment_reaction
                comment_doc['likes'] = []
                comment_doc['dislikes'] = []
                comments_list.append(ConfessionComment(**comment_doc))
            confession_doc['comments'] = comments_list

            # Add user_info to single confession fetch
            author_info = self._get_user_info(confession_doc['author_id'])
            if author_info:
                if confession_doc.get('anonymous'):
                    author_info.profile_picture_url = None
                confession_doc['user_info'] = author_info.model_dump()

            return Confession(**confession_doc)
        return None

    def update_confession(self, confession_id: str, update_data: ConfessionUpdate, user_id: str) -> Optional[Confession]:
        """
        Used to update a confession's settings, such as toggling comments or anonymity.
        """
        with self.sessions() as session:
            row = session.get(ConfessionRecord, str(confession_id))
            confession = self._confession_payload(row) if row else None

        if not confession:
            logger.error(f"Confession {confession_id} not found for update.")
            return None

        if confession.get("author_id") != user_id:
            logger.warning(f"User {user_id} attempted to edit confession {confession_id} owned by {confession.get('author_id')}")
            return None

        update_fields = {}
        update_data_dict = update_data.model_dump(exclude_unset=True)

        # Handle 'comments_enabled' update - this is straightforward
        if "comments_enabled" in update_data_dict:
            update_fields["comments_enabled"] = update_data_dict["comments_enabled"]

        # Handle 'anonymous' update with explicit logic
        if "anonymous" in update_data_dict:
            is_currently_anonymous = confession.get("anonymous", False)
            wants_to_be_anonymous = update_data_dict["anonymous"]

            # The only permitted change is from anonymous (True) to public (False).
            if is_currently_anonymous and not wants_to_be_anonymous:
                update_fields["anonymous"] = False
            # All other cases (e.g., trying to make a public post anonymous) are ignored for security.

        if not update_fields:
            # No valid changes were made, so we can return early.
            return self.get_confession(confession_id, user_id)

        with self.sessions.begin() as session:
            row = session.get(ConfessionRecord, str(confession_id))
            if row:
                if "comments_enabled" in update_fields: row.comments_enabled = update_fields["comments_enabled"]
                if "anonymous" in update_fields: row.anonymous = update_fields["anonymous"]

        logger.info(f"User {user_id} updated confession {confession_id} with data: {update_fields}")
        return self.get_confession(confession_id, user_id)

    def get_total_confessions_count(self) -> int:
        """
        Used to get the total number of confessions in the database.

        Counts what a participant can see. A removed confession is still a row, so counting
        all rows would make this disagree with the feed it is supposed to describe -- and it
        is the number that gets shown next to the feed.
        """
        with self.sessions() as session:
            count = session.scalar(
                select(func.count())
                .select_from(ConfessionRecord)
                .where(ConfessionRecord.removed_at.is_(None))
            ) or 0
        logger.info(f"Retrieved total confession count: {count}")
        return count

    def react_to_confession(self, confession_id: str, reaction: str, user_id: str):
        """Set, switch or clear one user's reaction, then return the updated confession.

        A reaction is a toggle: sending the same kind twice clears it, sending a different
        kind moves the user to that one. A user is in at most one kind at a time.
        """
        if reaction not in self.REACTION_TYPES:
            return None

        with self.sessions.begin() as session:
            row = session.get(ConfessionRecord, str(confession_id))
            # A removed confession no longer exists as far as a participant is concerned, so
            # reacting to it fails the same way reacting to a deleted one does. Leaving the
            # write open would let engagement accumulate on a post nobody can see, and the
            # counts would then disagree with the feed for the same reason as above.
            if not row or row.removed_at is not None:
                return None
            # The map has to be built fresh and assigned. Mutating the dict already loaded
            # on the row in place does not mark the JSONB column dirty, so the flush below
            # would write only the denormalised counts, leaving the map itself empty in the
            # database. The next request would then read that empty map, recompute every
            # count from it, and put them all back to zero, so no confession could ever hold
            # more than one reaction. Same reason _react_to_comment rebuilds its lists.
            stored_reactions = row.reactions or {}
            user_previous_reaction = next(
                (kind for kind, ids in stored_reactions.items() if user_id in ids), None
            )
            updated_reactions = {
                kind: [reacted_by for reacted_by in stored_reactions.get(kind, []) if reacted_by != user_id]
                for kind in self.REACTION_TYPES
            }
            if user_previous_reaction != reaction:
                updated_reactions[reaction].append(user_id)
            row.reactions = updated_reactions
            for kind in self.REACTION_TYPES:
                setattr(row, f"{kind}_count", len(updated_reactions[kind]))

        logger.info(f"User {user_id} reacted to confession {confession_id} with {reaction}")
        return self.get_confession(confession_id, user_id)

    def add_comment_to_confession(self, confession_id: str, comment_data: CommentCreate, user: UserDetails) -> any:
        with self.sessions() as session:
            row = session.get(ConfessionRecord, str(confession_id))
            confession = self._confession_payload(row) if row else None
        # Commenting on removed content is refused for the same reason reacting to it is:
        # the post is not in the participant's view, so a new comment on it is a write into a
        # conversation they cannot reach.
        if not confession or row.removed_at is not None:
            return "CONFESSION_NOT_FOUND"
        if not confession.get("comments_enabled", True):
            logger.warning(f"User {user.id} tried to comment on disabled confession {confession_id}")
            return "COMMENTS_DISABLED"

        user_id = str(user.id)
        with self.sessions() as session:
            existing_comments_count = session.scalar(select(func.count()).select_from(ConfessionCommentRecord).where(
                ConfessionCommentRecord.confession_id == str(confession_id),
                ConfessionCommentRecord.user_id == user_id,
            )) or 0
        if existing_comments_count >= 3:
            logger.warning(f"User {user_id} tried to post more than 3 comments on confession {confession_id}")
            return "COMMENT_LIMIT_REACHED"

        with self.sessions.begin() as session:
            row = ConfessionCommentRecord(confession_id=str(confession_id), user_id=user_id,
                body=comment_data.body, created_at=datetime.datetime.now(datetime.timezone.utc))
            session.add(row)
            confession_row = session.get(ConfessionRecord, str(confession_id))
            if confession_row: confession_row.comment_count += 1
            session.flush()
            new_comment = self._comment_payload(row, user)
            new_comment["user_info"].update({"name": user.name})
            # Participant-scoped (SEC-002): a fresh comment has no reports/votes yet.
            new_comment["has_reported"] = False
            new_comment["reported_by"] = []
            new_comment["likes"] = []
            new_comment["dislikes"] = []
        logger.info(f"Added comment {new_comment['id']} to confession {confession_id}")
        return ConfessionComment(**new_comment)

    def _react_to_comment(self, comment_id: str, user_id: str, reaction_type: str) -> Optional[ConfessionComment]:
        user_new_reaction = None
        with self.sessions.begin() as session:
            row = session.get(ConfessionCommentRecord, str(comment_id))
            # As with a removed confession: no reaction lands on content a participant can no
            # longer see, so the like count cannot quietly drift away from the visible list.
            if not row or row.removed_at is not None:
                return None
            likes = [str(value) for value in (row.likes or [])]
            dislikes = [str(value) for value in (row.dislikes or [])]
            if reaction_type == 'likes':
                if user_id in dislikes: dislikes.remove(user_id)
                if user_id in likes: likes.remove(user_id)
                else: likes.append(user_id); user_new_reaction = 'like'
            else:
                if user_id in likes: likes.remove(user_id)
                if user_id in dislikes: dislikes.remove(user_id)
                else: dislikes.append(user_id); user_new_reaction = 'dislike'
            row.likes = likes
            row.dislikes = dislikes
            row.like_count = len(likes)
            row.dislike_count = len(dislikes)
        
        logger.info(f"User {user_id} reacted to comment {comment_id} with {reaction_type}")
        
        with self.sessions() as session:
            row = session.get(ConfessionCommentRecord, str(comment_id))
            user = session.get(UserRecord, row.user_id) if row else None
        if not row:
            return None
        updated_comment = self._comment_payload(row, user)
        comment_reporter_ids = self._reporter_ids(comment_id)
        # Participant-scoped (SEC-002).
        updated_comment["has_reported"] = bool(user_id and user_id in comment_reporter_ids)
        updated_comment["reported_by"] = []
        updated_comment["report_count"] = len(comment_reporter_ids)
        updated_comment["user_reaction"] = user_new_reaction
        updated_comment["likes"] = []
        updated_comment["dislikes"] = []
        return ConfessionComment(**updated_comment)

    def like_comment(self, comment_id: str, user_id: str) -> Optional[ConfessionComment]:
        return self._react_to_comment(comment_id, user_id, 'likes')

    def dislike_comment(self, comment_id: str, user_id: str) -> Optional[ConfessionComment]:
        return self._react_to_comment(comment_id, user_id, 'dislikes')

    def report_comment(self, comment_id: str, report_data: ReportCreate, user: UserDetails) -> Optional[ConfessionComment]:
        user_id = str(user.id)
        with self.sessions() as session:
            comment = session.get(ConfessionCommentRecord, str(comment_id))
        # Removed content cannot be reported: there is nothing left for a moderator to rule
        # on, and a report against it would sit in the queue forever.
        if not comment or comment.removed_at is not None:
            return None

        with self.sessions() as session:
            duplicate = session.scalar(select(ReportRecord.id).where(
                ReportRecord.comment_id == str(comment_id), ReportRecord.reported_by_id == user_id
            ).limit(1))
        if duplicate:
            return None
        with self.sessions.begin() as session:
            session.add(ReportRecord(comment_id=str(comment_id), reported_by_id=user_id,
                reported_by_name=user.name, reason=report_data.reason,
                created_at=datetime.datetime.now(datetime.timezone.utc)))
            comment_row = session.get(ConfessionCommentRecord, str(comment_id))
            if comment_row:
                comment_row.report_count += 1
            # The complaint is against the comment, but what an administrator needs to see is
            # who is being complained about, so it lands on the comment's author. Same column
            # bucket as the confession itself -- see ``report_counters``.
            record_report_filed(
                session,
                kind=CONFESSION,
                author_user_id=comment_row.user_id if comment_row else None,
            )

        logger.info(f"User {user_id} reported comment {comment_id}")
        with self.sessions() as session:
            row = session.get(ConfessionCommentRecord, str(comment_id))
            author = session.get(UserRecord, row.user_id) if row else None
        if not row:
            return None
        updated_comment_doc = self._comment_payload(row, author)
        comment_reporter_ids = self._reporter_ids(comment_id)
        # Participant-scoped (SEC-002): the reporter just filed, so has_reported is
        # True by construction; identities still stay server-side.
        updated_comment_doc["has_reported"] = True
        updated_comment_doc["reported_by"] = []
        updated_comment_doc["report_count"] = len(comment_reporter_ids)
        updated_comment_doc["likes"] = []
        updated_comment_doc["dislikes"] = []
        return ConfessionComment(**updated_comment_doc)

    def report_confession(self, confession_id: str, report_data: ReportCreate, user: UserDetails) -> Optional[Confession]:
        user_id = str(user.id)
        with self.sessions() as session:
            confession = session.get(ConfessionRecord, str(confession_id))
        # Removed content cannot be reported -- see report_comment.
        if not confession or confession.removed_at is not None:
            return None

        with self.sessions() as session:
            duplicate = session.scalar(select(ReportRecord.id).where(
                ReportRecord.confession_id == str(confession_id), ReportRecord.reported_by_id == user_id
            ).limit(1))
        if duplicate:
            return None
        with self.sessions.begin() as session:
            session.add(ReportRecord(confession_id=str(confession_id), reported_by_id=user_id,
                reported_by_name=user.name, reason=report_data.reason,
                created_at=datetime.datetime.now(datetime.timezone.utc)))
            confession_row = session.get(ConfessionRecord, str(confession_id))
            if confession_row:
                confession_row.report_count += 1
            record_report_filed(
                session,
                kind=CONFESSION,
                author_user_id=confession_row.author_id if confession_row else None,
            )

        logger.info(f"User {user_id} reported confession {confession_id}")
        return self.get_confession(confession_id, user_id)
