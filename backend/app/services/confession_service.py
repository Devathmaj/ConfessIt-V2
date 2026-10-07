from ..logger import get_logger
from fastapi import HTTPException, status
from sqlalchemy import Float, case, cast, func, or_, select, update
from ..postgres import PostgresClient, get_postgres_client
from ..db_models import ConfessionCommentRecord, ConfessionRecord, ReportRecord, UserRecord
from ..models import Confession, ConfessionComment, ConfessionCreate, CommentCreate, UserDetails, UserInfo, ReportCreate, ConfessionUpdate
import datetime
from datetime import timezone
from typing import Any, Dict, Optional, List

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

    def _reporter_ids(self, target_id: str) -> List[Any]:
        # Includes automated system reports (reported_by_id IS NULL) in the
        # count: they are real report rows. ``None`` never matches a caller id,
        # so ``has_reported`` stays False for every participant while
        # ``report_count`` (len of this list) still counts the automated row.
        with self.sessions() as session:
            return [report.reported_by_id for report in session.scalars(select(ReportRecord).where(or_(
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
        confession_body_raw = confession_dict["body"]

        # Central moderation: one call handles config + analyze + per-item
        # policy. This path carries no local endpoint/key/timeout handling and
        # never branches on raw severities -- only on the decision flags.
        # Runs BEFORE persistence and BEFORE embedding; failures are unknown,
        # never safe, so abort before any row exists. Never log body/secrets.
        from .content_safety_service import (
            AUTOMATED_REMOVED_BY,
            AUTOMATED_REPORTER_DISPLAY_NAME,
            AUTOMATED_SOURCE,
            CONTENT_CONFESSION,
            ContentSafetyError,
            moderate_content,
            moderation_error_to_http,
        )

        try:
            decision = moderate_content(CONTENT_CONFESSION, confession_body_raw)
        except ContentSafetyError as exc:
            logger.error(
                "Content Safety moderation failed (%s); aborting confession creation",
                exc.code,
            )
            raise moderation_error_to_http(exc, content_type=CONTENT_CONFESSION) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Content Safety moderation failed unexpectedly: %s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Content moderation is temporarily unavailable. Please retry shortly.",
            ) from exc

        cs_scores = decision.scores
        cs_max = decision.max_severity
        cs_action = decision.action

        # One confession per participant every CONFESSION_COOLDOWN_HOURS. Read-then-write
        # would leave a window in which two simultaneous posts both see "eligible" and both
        # land, so the cooldown is claimed by the same conditional UPDATE that
        # _consume_matchmaking_attempt uses: PostgreSQL re-evaluates the predicate against
        # the committed row under the lock, so only one caller sees a row to update.
        #
        # Creation, automated reporting and hiding commit atomically: the
        # high-severity row is born hidden (removed_at set in the same INSERT),
        # so it is never observable in a feed between a visible insert and a
        # later hide. Report/hide failures roll back the whole confession rather
        # than leaving a visible confession that should have been hidden or a
        # hidden confession with no audit trail.
        automated_report_id: Optional[str] = None
        try:
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
                if decision.should_hide:
                    # Born hidden: the same removed_at write the review-screen
                    # hide performs, so every participant read (removed_at IS NULL)
                    # excludes it from the first commit. No second flag.
                    row.removed_at = now
                    row.removed_by_regno = AUTOMATED_REMOVED_BY
                session.add(row)
                session.flush()
                confession_id = str(row.id)
                confession_body = row.body
                confession_recipient = row.recipient_name
                if decision.should_report:
                    # Idempotent: exactly one automated report per confession.
                    # A new confession cannot already have one, but the check
                    # makes retries safe and the partial unique index backstops
                    # races. Manual reports (source="user") are untouched.
                    existing = session.scalar(
                        select(ReportRecord.id).where(
                            ReportRecord.confession_id == str(confession_id),
                            ReportRecord.source == AUTOMATED_SOURCE,
                        ).limit(1)
                    )
                    if existing is None:
                        reason = decision.report_reason
                        report_row = ReportRecord(
                            confession_id=str(confession_id),
                            reported_by_id=None,
                            reported_by_name=AUTOMATED_REPORTER_DISPLAY_NAME,
                            reason=reason,
                            created_at=now,
                            source=AUTOMATED_SOURCE,
                        )
                        session.add(report_row)
                        session.flush()
                        automated_report_id = str(report_row.id)
                        target = session.get(ConfessionRecord, str(confession_id))
                        if target is not None:
                            target.report_count = int(target.report_count or 0) + 1
                        record_report_filed(
                            session, kind=CONFESSION, author_user_id=user_id,
                        )
                        if decision.should_hide:
                            # Same authoritative counter the manual hide writes.
                            # Only on this transition; re-runs find existing and
                            # increment nothing.
                            from .report_counters import record_removal as _record_removal

                            _record_removal(
                                session, kind=CONFESSION, author_user_id=user_id
                            )
                    else:
                        automated_report_id = str(existing)
        except HTTPException:
            raise
        except Exception as exc:
            # Report/hide bookkeeping failed after a successful moderation
            # result: never silently treat as successful. The transaction above
            # rolled back, so no partial visible confession survives; surface a
            # retryable error with the failure in the logs (no body/secrets).
            logger.error(
                "Automated moderation bookkeeping failed for new confession: %s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not complete moderation for this confession.",
            ) from exc

        logger.info(
            f"Created confession {confession_id} for user {user_id} "
            f"(content-safety max={cs_max} action={cs_action} "
            f"scores Hate={int(cs_scores.get('Hate', 0) or 0)} "
            f"SelfHarm={int(cs_scores.get('SelfHarm', 0) or 0)} "
            f"Sexual={int(cs_scores.get('Sexual', 0) or 0)} "
            f"Violence={int(cs_scores.get('Violence', 0) or 0)})"
        )
        # Best-effort staff/author notices: failures never unwind the committed
        # moderation outcome (report + hidden state are already durable).
        if automated_report_id is not None:
            try:
                from .staff_notification_service import notify_report_filed

                notify_report_filed(
                    self.client, kind="confession", report_id=automated_report_id,
                    summary=(
                        "A confession was automatically reported by "
                        "Azure Content Safety. Open Reports review to rule on it."
                    ),
                )
            except Exception:
                logger.warning(
                    "Failed to queue staff notification for automated report %s",
                    automated_report_id,
                )
            if decision.should_hide:
                try:
                    # Same neutral copy the direct-hide path sends: no policy
                    # is cited because no human ruled on a report.
                    from ..db_models import UserRecord as _UserRecord

                    with self.sessions() as _s:
                        _regno = _s.scalar(
                            select(_UserRecord.regno).where(
                                _UserRecord.id == str(user_id)
                            )
                        )
                    if _regno:
                        from .notification_service import (
                            create_notification_service as _notify,
                        )

                        _notify(
                            recipient_regno=str(_regno),
                            heading="Confession removed",
                            body="We removed your confession from ConfessIt.",
                            db=self.client,
                        )
                except Exception:
                    logger.warning(
                        "Failed to queue removal notice for confession %s",
                        confession_id,
                    )
        # Exactly one embedding for this eligible confession, generated
        # synchronously so no worker infrastructure is needed. Runs for every
        # successfully moderated confession, including severity 4 and 6:
        # moderation determines reporting/visibility, never whether the
        # embedding is generated. A provider failure never fails the confession
        # itself: the post stays and is simply not searchable (no backfill).
        # Comments, likes, and reports never generate embeddings (only this path).
        # An embedding retry never creates another report (reports live above).
        self._generate_embedding_for_confession(
            confession_id, confession_body, confession_recipient
        )
        visible = self.get_confession(confession_id, user_id)
        if visible is not None:
            return visible
        if decision.should_hide:
            # The confession is hidden from participant reads by design
            # (get_confession returns None for removed rows). The creation
            # response still answers with the created post so the call does not
            # become a 500; feeds, searches and later reads keep excluding it.
            return self._get_created_hidden_confession(confession_id, user_id)
        return visible

    def _get_created_hidden_confession(self, confession_id: str, user_id: Optional[str]):
        """Build the creation response for a just-hidden confession.

        Participant reads exclude removed rows, so ``get_confession`` answers
        None here. Returning None would turn creation into a response-validation
        500. This builds the same participant-scoped shape from the stored row
        without the removed filter, used only for the creation response;
        subsequent feeds, searches and single reads keep excluding it.
        """
        with self.sessions() as session:
            row = session.get(ConfessionRecord, str(confession_id))
            if row is None:
                return None
            payload = self._confession_payload(row)
            # Participant-scoped: hide other reporters, keep the count (which
            # includes the automated report). The caller did not file it, so
            # has_reported is always False here.
            with self.sessions() as inner:
                reporter_ids = [
                    r.reported_by_id
                    for r in inner.scalars(
                        select(ReportRecord).where(
                            ReportRecord.confession_id == str(confession_id)
                        )
                    ).all()
                ]
            payload["has_reported"] = False
            payload["reported_by"] = []
            payload["report_count"] = len(reporter_ids)
            payload["user_reaction"] = None
            payload["reactions"] = {}
            payload["comments"] = []
            try:
                author = session.get(UserRecord, str(payload.get("author_id")))
                if author is not None:
                    info = UserInfo(
                        id=str(author.id), name=author.name or "Anonymous User",
                        profile_picture_url=_profile_picture_url(author),
                    )
                    if payload.get("anonymous"):
                        info.profile_picture_url = None
                    payload["user_info"] = info.model_dump()
            except Exception:
                pass
            return Confession(**payload)

    def create_automated_comment_safety_report(
        self, comment_id: str, scores: Dict[str, int], maximum: int
    ) -> Optional[str]:
        """Idempotent helper for tests/retries: one automated report per comment."""
        from .content_safety_service import (
            AUTOMATED_REPORTER_DISPLAY_NAME as _ADN,
            AUTOMATED_SOURCE as _AS,
            build_report_reason_for as _reason_for,
            CONTENT_COMMENT as _CC,
        )
        import datetime as _dt

        now = _dt.datetime.now(_dt.timezone.utc)
        with self.sessions.begin() as session:
            target = session.get(ConfessionCommentRecord, str(comment_id))
            if target is None:
                return None
            existing = session.scalar(
                select(ReportRecord.id).where(
                    ReportRecord.comment_id == str(comment_id),
                    ReportRecord.source == _AS,
                ).limit(1)
            )
            if existing is not None:
                return str(existing)
            row = ReportRecord(
                comment_id=str(comment_id),
                reported_by_id=None,
                reported_by_name=_ADN,
                reason=_reason_for(_CC, scores, maximum),
                created_at=now,
                source=_AS,
            )
            session.add(row)
            session.flush()
            report_id = str(row.id)
            target.report_count = int(target.report_count or 0) + 1
            record_report_filed(
                session, kind=CONFESSION, author_user_id=str(target.user_id),
            )
        return report_id

    def create_automated_content_safety_report(
        self, confession_id: str, scores: Dict[str, int], maximum: int
    ) -> Optional[str]:
        """Idempotent helper for tests/retries: one automated report per confession.

        Returns the report id, existing or newly created. Never creates a
        duplicate automated report for the same confession. Manual reports are
        untouched. Used by create_confession; exposed so retry semantics are
        directly testable without re-posting a confession.
        """
        from .content_safety_service import (
            AUTOMATED_REPORTER_DISPLAY_NAME,
            AUTOMATED_SOURCE,
            build_report_reason,
        )
        import datetime as _dt

        now = _dt.datetime.now(_dt.timezone.utc)
        with self.sessions.begin() as session:
            target = session.get(ConfessionRecord, str(confession_id))
            if target is None:
                return None
            existing = session.scalar(
                select(ReportRecord.id).where(
                    ReportRecord.confession_id == str(confession_id),
                    ReportRecord.source == AUTOMATED_SOURCE,
                ).limit(1)
            )
            if existing is not None:
                return str(existing)
            reason = build_report_reason(scores, maximum)
            row = ReportRecord(
                confession_id=str(confession_id),
                reported_by_id=None,
                reported_by_name=AUTOMATED_REPORTER_DISPLAY_NAME,
                reason=reason,
                created_at=now,
                source=AUTOMATED_SOURCE,
            )
            session.add(row)
            session.flush()
            report_id = str(row.id)
            target.report_count = int(target.report_count or 0) + 1
            record_report_filed(
                session, kind=CONFESSION, author_user_id=str(target.author_id),
            )
        return report_id

    def _generate_embedding_for_confession(
        self, confession_id: str, body: Any, recipient_name: Any = None
    ) -> bool:
        """Generate and store the single embedding for a new confession.

        Returns True when a valid embedding was stored. False when search is
        disabled or generation failed: the confession remains created but is
        excluded from semantic search (no valid embedding row). Never raises
        for provider failures so creation is not permanently dependent on Azure.
        """
        from ..config import settings as _settings
        from .embedding_service import (
            EmbeddingError,
            build_confession_document,
            config_from_settings,
            embed_confession,
        )
        from ..db_models import ConfessionEmbeddingRecord

        try:
            config = config_from_settings(_settings)
        except Exception as exc:
            logger.warning("Skipping confession embedding: bad config: %s", exc)
            return False
        if not config.enabled:
            return False
        try:
            document = build_confession_document(body, recipient_name)
            if not document:
                return False
            vector = embed_confession(body, recipient_name, config=config)
        except EmbeddingError as exc:
            # Searchability state is explicit: no row means not searchable.
            # Never log confession text; only the id and the safe code.
            logger.warning(
                "Confession %s not searchable: embedding failed (%s)",
                confession_id, exc.code,
            )
            return False
        except Exception as exc:
            logger.warning(
                "Confession %s not searchable: unexpected embedding failure: %s",
                confession_id, type(exc).__name__,
            )
            return False
        try:
            with self.sessions.begin() as session:
                existing = session.scalar(
                    select(ConfessionEmbeddingRecord).where(
                        ConfessionEmbeddingRecord.confession_id == str(confession_id)
                    )
                )
                if existing is not None:
                    return True
                session.add(
                    ConfessionEmbeddingRecord(
                        confession_id=str(confession_id),
                        embedding=vector,
                        model=config.model,
                    )
                )
            logger.info("Stored embedding for confession %s", confession_id)
            return True
        except Exception as exc:
            logger.warning(
                "Confession %s not searchable: embedding store failed: %s",
                confession_id, type(exc).__name__,
            )
            return False

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

        # Central comment moderation: same principal/logic as confessions
        # (everything >1 flagged+hidden). Runs BEFORE persistence; failures
        # are unknown, never safe, so abort before any row exists.
        from .content_safety_service import (
            AUTOMATED_REMOVED_BY as _AUTO_REMOVED_BY,
            AUTOMATED_REPORTER_DISPLAY_NAME as _AUTO_NAME,
            AUTOMATED_SOURCE as _AUTO_SOURCE,
            CONTENT_COMMENT as _CONTENT_COMMENT,
            ContentSafetyError as _CSErr,
            moderate_content as _moderate,
            moderation_error_to_http as _to_http,
        )

        try:
            decision = _moderate(_CONTENT_COMMENT, comment_data.body or "")
        except _CSErr as exc:
            logger.error(
                "Content Safety comment moderation failed (%s); aborting comment creation",
                exc.code,
            )
            raise _to_http(exc, content_type=_CONTENT_COMMENT) from exc
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Content Safety comment moderation failed unexpectedly: %s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Content moderation is temporarily unavailable. Please retry shortly.",
            ) from exc

        now = datetime.datetime.now(datetime.timezone.utc)
        automated_report_id: Optional[str] = None
        try:
            with self.sessions.begin() as session:
                row = ConfessionCommentRecord(confession_id=str(confession_id), user_id=user_id,
                    body=comment_data.body, created_at=now)
                if decision.should_hide:
                    # Born hidden: same removed_at write hide_comment performs,
                    # so participant reads (removed_at IS NULL) exclude it from
                    # the first commit. No second flag.
                    row.removed_at = now
                    row.removed_by_regno = _AUTO_REMOVED_BY
                session.add(row)
                session.flush()
                comment_id = str(row.id)
                confession_row = session.get(ConfessionRecord, str(confession_id))
                # Visible comments advertise in the parent count; born-hidden
                # ones never do (hide_comment decrements on transition, so
                # not incrementing here lands in the same state).
                if confession_row and not decision.should_hide:
                    confession_row.comment_count += 1
                if decision.should_report:
                    existing = session.scalar(
                        select(ReportRecord.id).where(
                            ReportRecord.comment_id == str(comment_id),
                            ReportRecord.source == _AUTO_SOURCE,
                        ).limit(1)
                    )
                    if existing is None:
                        report_row = ReportRecord(
                            comment_id=str(comment_id),
                            reported_by_id=None,
                            reported_by_name=_AUTO_NAME,
                            reason=decision.report_reason,
                            created_at=now,
                            source=_AUTO_SOURCE,
                        )
                        session.add(report_row)
                        session.flush()
                        automated_report_id = str(report_row.id)
                        target = session.get(ConfessionCommentRecord, str(comment_id))
                        if target is not None:
                            target.report_count = int(target.report_count or 0) + 1
                        record_report_filed(
                            session, kind=CONFESSION, author_user_id=user_id,
                        )
                        if decision.should_hide:
                            from .report_counters import record_removal as _record_removal

                            _record_removal(
                                session, kind=CONFESSION, author_user_id=user_id
                            )
                    else:
                        automated_report_id = str(existing)
                session.flush()
                new_comment = self._comment_payload(row, user)
                new_comment["user_info"].update({"name": user.name})
                # Participant-scoped (SEC-002).
                new_comment["has_reported"] = False
                new_comment["reported_by"] = []
                new_comment["likes"] = []
                new_comment["dislikes"] = []
                # Freshly reported/hidden: surface the automated count.
                if automated_report_id is not None:
                    new_comment["report_count"] = 1
        except HTTPException:
            raise
        except Exception as exc:
            logger.error(
                "Automated comment moderation bookkeeping failed: %s",
                type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Could not complete moderation for this comment.",
            ) from exc
        logger.info(
            f"Added comment {new_comment['id']} to confession {confession_id} "
            f"(content-safety max={decision.max_severity} action={decision.action} "
            f"scores Hate={int(decision.scores.get('Hate', 0) or 0)} "
            f"SelfHarm={int(decision.scores.get('SelfHarm', 0) or 0)} "
            f"Sexual={int(decision.scores.get('Sexual', 0) or 0)} "
            f"Violence={int(decision.scores.get('Violence', 0) or 0)})"
        )
        if automated_report_id is not None:
            try:
                from .staff_notification_service import notify_report_filed

                notify_report_filed(
                    self.client, kind="comment", report_id=automated_report_id,
                    summary=(
                        "A comment was automatically reported by "
                        "Azure Content Safety. Open Reports review to rule on it."
                    ),
                )
            except Exception:
                logger.warning(
                    "Failed to queue staff notification for automated comment report %s",
                    automated_report_id,
                )
            if decision.should_hide:
                try:
                    with self.sessions() as _s:
                        _regno = _s.scalar(
                            select(UserRecord.regno).where(
                                UserRecord.id == str(user_id)
                            )
                        )
                    if _regno:
                        from .notification_service import (
                            create_notification_service as _notify,
                        )

                        _notify(
                            recipient_regno=str(_regno),
                            heading="Comment removed",
                            body="We removed your comment from ConfessIt.",
                            db=self.client,
                        )
                except Exception:
                    logger.warning(
                        "Failed to queue removal notice for comment %s",
                        new_comment.get("id"),
                    )
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
            report_row = ReportRecord(comment_id=str(comment_id), reported_by_id=user_id,
                reported_by_name=user.name, reason=report_data.reason,
                created_at=datetime.datetime.now(datetime.timezone.utc))
            session.add(report_row)
            session.flush()
            comment_report_id = str(report_row.id)
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
        # Staff-facing notice (role-based, best-effort): a failed write here must
        # never unwind the report that was just committed.
        try:
            from .staff_notification_service import notify_report_filed

            notify_report_filed(
                self.client, kind="comment", report_id=comment_report_id,
                summary=f"A comment was reported by {user.name}. Open Reports review to rule on it.",
            )
        except Exception:
            logger.warning("Failed to queue staff notification for comment report %s", comment_id)
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
            confession_report_row = ReportRecord(confession_id=str(confession_id), reported_by_id=user_id,
                reported_by_name=user.name, reason=report_data.reason,
                created_at=datetime.datetime.now(datetime.timezone.utc))
            session.add(confession_report_row)
            session.flush()
            confession_report_id = str(confession_report_row.id)
            confession_row = session.get(ConfessionRecord, str(confession_id))
            if confession_row:
                confession_row.report_count += 1
            record_report_filed(
                session,
                kind=CONFESSION,
                author_user_id=confession_row.author_id if confession_row else None,
            )

        logger.info(f"User {user_id} reported confession {confession_id}")
        # Staff-facing notice (role-based, best-effort): a failed write here must
        # never unwind the report that was just committed.
        try:
            from .staff_notification_service import notify_report_filed

            notify_report_filed(
                self.client, kind="confession", report_id=confession_report_id,
                summary=f"A confession was reported by {user.name}. Open Reports review to rule on it.",
            )
        except Exception:
            logger.warning("Failed to queue staff notification for confession report %s", confession_id)
        return self.get_confession(confession_id, user_id)

    def search_confessions_semantic(
        self, query: str, user_id: Optional[str] = None, limit: int = 10
   ) -> Dict[str, Any]:
        """Semantic search over eligible confessions ranked by cosine distance.

        Ranking uses pgvector cosine distance (``<=>``) ascending: the smallest
        distance is the most semantically similar. Cosine distance for
        length-normalized embeddings equals ``1 - cosine_similarity``; no
        calibrated probability or confidence percentage is implied. Results
        exclude confessions without a valid current embedding and any row that
        is hidden, deleted (hard-deleted rows vanish via CASCADE), or otherwise
        ineligible (``removed_at IS NULL`` is enforced in the database search
        itself). Ties break by newest first, then id, so pages are stable.
        """
        from ..config import settings as _settings
        from .embedding_service import (
            EmbeddingError,
            SEARCH_QUERY_MAX_LENGTH,
            config_from_settings,
            embed_query,
            normalize_text,
        )
        from ..db_models import ConfessionEmbeddingRecord

        normalized = normalize_text(query or "")
        if not normalized:
            raise EmbeddingError(
                "Search query must not be blank.", code="invalid_input"
            )
        if len(normalized) > SEARCH_QUERY_MAX_LENGTH:
            raise EmbeddingError(
                f"Search query is too long (max {SEARCH_QUERY_MAX_LENGTH} characters).",
                code="invalid_input",
            )
        limit = max(1, min(int(limit or 10), FEED_MAX_LIMIT))
        config = config_from_settings(_settings)
        if not config.enabled:
            raise EmbeddingError(
                "Semantic search is disabled (EMBEDDING_SEARCH_ENABLED is not true).",
                code="disabled",
            )
        # Generates the query embedding with the same model; validates 1536 dims.
        # No provider call happens when disabled (checked above).
        query_vector = embed_query(normalized, config=config)

        # Vector ranking in the database: inner join excludes confessions without
        # a valid embedding; removed_at IS NULL enforces moderation/visibility in
        # SQL, not in frontend code. Safe ORM expression, never string SQL.
        try:
            with self.sessions() as session:
                distance = ConfessionEmbeddingRecord.embedding.cosine_distance(query_vector)
                rows = session.execute(
                    select(
                        ConfessionRecord,
                        distance.label("distance"),
                    )
                    .join(
                        ConfessionEmbeddingRecord,
                        ConfessionEmbeddingRecord.confession_id == ConfessionRecord.id,
                    )
                    .where(ConfessionRecord.removed_at.is_(None))
                    .order_by(
                        distance.asc(),
                        ConfessionRecord.created_at.desc(),
                        ConfessionRecord.id.asc(),
                    )
                    .limit(limit)
                ).all()
                ordered_models = [row[0] for row in rows]
                ordered_ids = [str(row.id) for row in ordered_models]
        except Exception as exc:
            message = str(exc).lower()
            if "vector" in message or "embedding" in message or "cosine" in message:
                raise EmbeddingError(
                    "Semantic search is unavailable: the pgvector extension or "
                    "confession_embeddings schema is missing. See README for "
                    "the pgvector/pgvector:pg16 requirement.",
                    code="unavailable",
                ) from exc
            raise
        if not ordered_models:
            return {"confessions": [], "total": 0, "query": normalized, "limit": limit}
        # Hydrate the ranked page with the same bulk reads as the feed: newest N
        # comments per confession, authors, and reporter ids -- one bulk read per
        # relation, never per row. Order is semantic relevance, not time.
        with self.sessions() as session:
            confession_ids = ordered_ids
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
            )
            kept_ids = session.scalars(
                select(ranked.c.id).where(ranked.c.thread_rank <= FEED_COMMENTS_INITIAL)
            ).all()
            comment_models = session.scalars(
                select(ConfessionCommentRecord)
                .where(ConfessionCommentRecord.id.in_(kept_ids))
                .order_by(ConfessionCommentRecord.created_at.desc())
            ).all() if kept_ids else []
            wanted_user_ids = {str(row.author_id) for row in ordered_models}
            wanted_user_ids |= {str(row.user_id) for row in comment_models}
            user_map = {
                user.id: user for user in session.scalars(
                    select(UserRecord).where(UserRecord.id.in_(wanted_user_ids))
                ).all()
            } if wanted_user_ids else {}
            confession_reporters: dict = {}
            for target_id, reporter_id in session.execute(
                select(ReportRecord.confession_id, ReportRecord.reported_by_id).where(
                    ReportRecord.confession_id.in_(confession_ids)
                )
            ).all():
                confession_reporters.setdefault(str(target_id), []).append(str(reporter_id))
            comments_by_confession: dict = {}
            for comment in self._hydrate_comments(session, comment_models, user_id, user_map):
                comments_by_confession.setdefault(comment["confession_id"], []).append(comment)
            # Re-fetch confession rows in this session so payloads read live attrs.
            fresh = {
                str(row.id): row for row in session.scalars(
                    select(ConfessionRecord).where(ConfessionRecord.id.in_(confession_ids))
                ).all()
            }
        confession_rows = []
        for confession_id in ordered_ids:
            row = fresh.get(confession_id)
            if row is None or row.removed_at is not None:
                continue
            payload = self._confession_payload(row)
            reporter_ids = confession_reporters.get(str(payload["id"]), [])
            payload["has_reported"] = bool(user_id and user_id in reporter_ids)
            payload["reported_by"] = []
            payload["report_count"] = len(reporter_ids)
            payload["comments"] = sorted(
                comments_by_confession.get(confession_id, []),
                key=lambda c: c.get("created_at") or datetime.datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            author = user_map.get(str(payload.get("author_id")))
            payload["user_info"] = {
                "id": str(payload.get("author_id")),
                "name": author.name if author else "Anonymous User",
                "profile_picture_url": _profile_picture_url(author) if not payload.get("anonymous") else None,
            }
            user_reaction = None
            if user_id and "reactions" in payload:
                for reaction_type, reactor_ids in payload["reactions"].items():
                    if user_id in reactor_ids:
                        user_reaction = reaction_type
                        break
            payload["user_reaction"] = user_reaction
            payload["reactions"] = {}
            # Reuse the feed's comment hydration for user_reaction preservation.
            comments_list = []
            for comment_doc in sorted(payload.get("comments", []), key=lambda c: c["created_at"], reverse=True):
                comments_list.append(ConfessionComment(**comment_doc))
            payload["comments"] = comments_list
            confession_rows.append(Confession(**payload))
        logger.info("Semantic search returned %s confession(s)", len(confession_rows))
        return {
            "confessions": confession_rows,
            "total": len(confession_rows),
            "query": normalized,
            "limit": limit,
        }

    def _user_keyword_id_page(self, cleaned: str, limit: int) -> tuple[list, int]:
        """Keyword ids over body/recipient only, trigram-relevance ordered.

        Participant-safe by construction: author identity columns are never
        referenced, so a confession cannot be found by who wrote it. Uses the
        same ILIKE + pg_trgm approach as the rest of the app, with
        ``removed_at IS NULL`` enforced in SQL. Relevance is trigram similarity
        (best of body/recipient) with recency as the tiebreak.
        """
        from ..utils import ilike_contains

        match = or_(
            ilike_contains(ConfessionRecord.body, cleaned),
            ilike_contains(ConfessionRecord.recipient_name, cleaned),
        )
        relevance = func.greatest(
            func.similarity(ConfessionRecord.body, cleaned),
            func.similarity(func.coalesce(ConfessionRecord.recipient_name, ""), cleaned),
        )
        ordering = [
            relevance.desc(),
            ConfessionRecord.created_at.desc(),
            ConfessionRecord.id.asc(),
        ]
        with self.sessions() as session:
            total = session.scalar(
                select(func.count())
                .select_from(ConfessionRecord)
                .where(ConfessionRecord.removed_at.is_(None), match)
            ) or 0
            ids = [
                str(row_id)
                for row_id in session.scalars(
                    select(ConfessionRecord.id)
                    .where(ConfessionRecord.removed_at.is_(None), match)
                    .order_by(*ordering)
                    .limit(limit)
                ).all()
            ]
        return ids, total

    def _hydrate_public_page(
        self, ordered_ids: list, user_id: Optional[str] = None
    ) -> list:
        """Bulk-hydrate confession ids into public ``Confession`` models, in order.

        Same participant-scoped shape as the feed (comments window, authors,
        reporter counts, caller's own reaction/report state; no identities, no
        vectors). Removed rows encountered here are skipped, so the page never
        names content its reader cannot open.
        """
        if not ordered_ids:
            return []
        with self.sessions() as session:
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
                    ConfessionCommentRecord.confession_id.in_(ordered_ids),
                )
                .subquery()
            )
            kept_ids = session.scalars(
                select(ranked.c.id).where(ranked.c.thread_rank <= FEED_COMMENTS_INITIAL)
            ).all()
            comment_models = session.scalars(
                select(ConfessionCommentRecord)
                .where(ConfessionCommentRecord.id.in_(kept_ids))
                .order_by(ConfessionCommentRecord.created_at.desc())
            ).all() if kept_ids else []
            fresh = {
                str(row.id): row for row in session.scalars(
                    select(ConfessionRecord).where(ConfessionRecord.id.in_(ordered_ids))
                ).all()
            }
            wanted_user_ids = {
                str(row.author_id) for row in fresh.values()
            } | {str(row.user_id) for row in comment_models}
            user_map = {
                user.id: user for user in session.scalars(
                    select(UserRecord).where(UserRecord.id.in_(wanted_user_ids))
                ).all()
            } if wanted_user_ids else {}
            confession_reporters: dict = {}
            for target_id, reporter_id in session.execute(
                select(ReportRecord.confession_id, ReportRecord.reported_by_id).where(
                    ReportRecord.confession_id.in_(ordered_ids)
                )
            ).all():
                confession_reporters.setdefault(str(target_id), []).append(str(reporter_id))
            comments_by_confession: dict = {}
            for comment in self._hydrate_comments(session, comment_models, user_id, user_map):
                comments_by_confession.setdefault(comment["confession_id"], []).append(comment)
        rows = []
        for confession_id in ordered_ids:
            row = fresh.get(str(confession_id))
            if row is None or row.removed_at is not None:
                continue
            payload = self._confession_payload(row)
            reporter_ids = confession_reporters.get(str(payload["id"]), [])
            payload["has_reported"] = bool(user_id and user_id in reporter_ids)
            payload["reported_by"] = []
            payload["report_count"] = len(reporter_ids)
            payload["comments"] = sorted(
                comments_by_confession.get(str(confession_id), []),
                key=lambda c: c.get("created_at") or datetime.datetime.min.replace(tzinfo=timezone.utc),
                reverse=True,
            )
            author = user_map.get(str(payload.get("author_id")))
            payload["user_info"] = {
                "id": str(payload.get("author_id")),
                "name": author.name if author else "Anonymous User",
                "profile_picture_url": _profile_picture_url(author) if not payload.get("anonymous") else None,
            }
            user_reaction = None
            if user_id and "reactions" in payload:
                for reaction_type, reactor_ids in payload["reactions"].items():
                    if user_id in reactor_ids:
                        user_reaction = reaction_type
                        break
            payload["user_reaction"] = user_reaction
            payload["reactions"] = {}
            payload["comments"] = [
                ConfessionComment(**comment_doc)
                for comment_doc in sorted(
                    payload.get("comments", []), key=lambda c: c["created_at"], reverse=True
                )
            ]
            rows.append(Confession(**payload))
        return rows

    def _user_semantic_section(
        self,
        normalized: str,
        exclude_ids: list,
        cap: int,
        user_id: Optional[str],
        rate_key: str,
    ) -> tuple[list, str]:
        """Semantic half of hybrid search via the shared candidate pipeline."""
        from .semantic_candidates import get_semantic_candidates
        from .semantic_rate_limit import USER_BUCKET

        candidates, status = get_semantic_candidates(
            sessions=self.sessions,
            query=normalized,
            cap=cap,
            include_removed=False,
            extra_filters=None,
            exclude_ids=exclude_ids,
            bucket=USER_BUCKET,
            rate_key=rate_key or user_id or "anon",
        )
        if not candidates:
            return [], status
        return (
            self._hydrate_public_page([cid for cid, _, _ in candidates], user_id),
            status,
        )

    def search_confessions(
        self,
        query: str,
        user_id: Optional[str] = None,
        limit: int = 10,
        rate_key: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Hybrid keyword + semantic search, keyword first and deduped.

        Keyword runs over body/recipient only (never identity) with trigram
        relevance ordering; semantic runs through the shared
        embed + vector-query + threshold + dedupe pipeline capped at
        ``min(limit, EMBEDDING_SEMANTIC_TOP_K)``. The two halves run
        concurrently (keyword hydration overlaps the embedding provider call).
        Semantic degradation (disabled / unavailable / rate-limited / too-short)
        still returns keyword results with ``semantic.status`` naming the reason
        instead of failing the request. Blank or over-long queries raise
        ``invalid_input`` (422).
        """
        from concurrent.futures import ThreadPoolExecutor

        from ..config import settings as _settings
        from .embedding_service import (
            EmbeddingError,
            SEARCH_QUERY_MAX_LENGTH,
            normalize_text,
        )

        normalized = normalize_text(query or "")
        if not normalized:
            raise EmbeddingError("Search query must not be blank.", code="invalid_input")
        if len(normalized) > SEARCH_QUERY_MAX_LENGTH:
            raise EmbeddingError(
                f"Search query is too long (max {SEARCH_QUERY_MAX_LENGTH} characters).",
                code="invalid_input",
            )
        limit = max(1, min(int(limit or 10), FEED_MAX_LIMIT))
        keyword_ids, keyword_total = self._user_keyword_id_page(normalized, limit)
        cap = max(1, min(int(limit), int(_settings.EMBEDDING_SEMANTIC_TOP_K)))
        key = rate_key or user_id or "anon"
        with ThreadPoolExecutor(max_workers=2) as pool:
            keyword_future = pool.submit(self._hydrate_public_page, keyword_ids, user_id)
            semantic_future = pool.submit(
                self._user_semantic_section, normalized, keyword_ids, cap, user_id, key
            )
            keyword_items = keyword_future.result()
            try:
                semantic_items, semantic_status = semantic_future.result()
            except Exception as exc:
                logger.warning("Hybrid semantic half failed: %s", type(exc).__name__)
                semantic_items, semantic_status = [], "unavailable"
        logger.info(
            "Hybrid search returned %s keyword + %s semantic confession(s)",
            len(keyword_items), len(semantic_items),
        )
        return {
            "query": normalized,
            "limit": limit,
            "keyword": {"items": keyword_items, "total": keyword_total},
            "semantic": {"items": semantic_items, "status": semantic_status},
        }
