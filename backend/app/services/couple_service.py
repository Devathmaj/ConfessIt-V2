"""Featuring the Couple Prediction game for a day, and the static question bank it runs on.

The table this writes is the *configuration* of a featured instance and nothing
else -- a day, and who marked it so. There is deliberately no mode, no word, no
answer and no per-day content selection here, because there is nothing per-day
for an administrator to choose: unlike Wordle/Anagram there is no custom-vs-random
choice, no word to type, no answer to store per date and no puzzle to generate.
The 50-question bank is shared application content, seeded once at startup from
the committed list below, and a featured date never copies it.

**The question bank is data, and the database is its home after the first boot.**
``COUPLE_QUESTION_BANK`` below is the committed source the seed reads from, in
sequence order; ``seed_couple_questions`` inserts whatever of it is missing and
touches nothing that already exists. The seed is idempotent by ``stable_key``
(``couple-q-01`` … ``couple-q-50``): restarting the application inserts the rest
of the bank, never a duplicate, and never overwrites a stored row. Nothing in a
request handler carries this list -- a handler that built questions inline would
be a second source competing with the table.

**The ``X`` placeholder is a presentation rule, not a storage one.** Every
template names ``X`` where a person's name goes -- ``What would X prefer for a
perfect date?`` -- and :func:`format_couple_question` substitutes the answering
participant's name at read time. The stored template is never rewritten per
viewer, so the same row serves both directions of a relationship: Alex answers,
Sam sees Alex's name in place of ``X`` while predicting, and vice versa.
"""

import re
from datetime import date, datetime, timezone
from typing import List

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..db_models import CoupleGameQuestionRecord, CoupleGameRecord
from ..limits import (
    COUPLE_OPTION_TEXT_MAX,
    COUPLE_OPTIONS_PER_QUESTION,
    COUPLE_QUESTION_TEXT_MAX,
    COUPLE_QUESTIONS_COUNT,
)
from ..models import CoupleGame, CoupleGameCreate, CoupleGameQuestion
from ..postgres import Database, is_valid_id

# Matches a standalone ``X`` only, so the substitution never rewrites the letter
# inside another word. Compiled once: every future player read formats through it.
_PLACEHOLDER_PATTERN = re.compile(r"\bX\b")


def format_couple_question(question_text: str, display_name: str) -> str:
    """Render a bank template for one viewer, substituting their partner's name for ``X``.

    Presentation-time only: the stored template is returned unchanged everywhere
    else, and this is the single place the replacement happens so the two
    directions of a relationship cannot drift into different spellings. A blank
    name leaves the template untouched rather than writing an empty string into
    the sentence.
    """
    name = (display_name or "").strip()
    if not name:
        return question_text
    return _PLACEHOLDER_PATTERN.sub(name, question_text)


# The fifty templates, in sequence order. Each entry is (question_text, options)
# with exactly four options. The text names ``X`` and never a person, never
# first-person ("I") or second-person ("you") wording: the player surface shows
# "What would you prefer …?" while answering and the partner later sees the same
# template with the answerer's name in place of ``X``.
#
# Kept as data next to the seed that reads it rather than in a request handler:
# no endpoint builds questions, so the table stays the single source of truth
# after the first boot.
COUPLE_QUESTION_BANK: tuple[tuple[str, tuple[str, str, str, str]], ...] = (
    ("What would X prefer for a perfect date?", ("Dinner & movie", "Long drive", "Beach or outdoor trip", "Staying in together")),
    ("What would X choose for a free evening?", ("Watch a movie", "Play games", "Go out", "Just talk")),
    ("What kind of vacation would X prefer?", ("Beach", "Mountains", "City", "Countryside")),
    ("Which food would X pick first?", ("Pizza", "Burger", "Biriyani", "Noodles")),
    ("What kind of gift would X appreciate most?", ("Useful", "Sentimental", "Expensive", "Handmade")),
    ("What would X rather receive from a partner?", ("Handwritten note", "Surprise gift", "Planned date", "Heartfelt message")),
    ("What would annoy X the most?", ("Being ignored", "Being lied to", "Someone being late", "Being misunderstood")),
    ("What would X prefer after a bad day?", ("Advice", "A hug", "Someone listening", "Some alone time")),
    ("What would X value most in a relationship?", ("Communication", "Trust", "Quality time", "Affection")),
    ("How would X most likely react after an argument?", ("Talk immediately", "Need some space", "Act normal first", "Send a message")),
    ("What would X consider an ideal weekend?", ("Exploring somewhere new", "Staying home", "Meeting friends", "Doing something spontaneous")),
    ("Which movie genre would X choose?", ("Comedy", "Romance", "Thriller", "Action")),
    ("What would X rather spend extra money on?", ("Food", "Clothes", "Gadgets", "Experiences")),
    ("What would X rather do together?", ("Cook", "Travel", "Play a game", "Watch something")),
    ("What would X find most romantic?", ("Stargazing", "Candlelight dinner", "Surprise trip", "Personal letter")),
    ("What would X prefer when feeling stressed?", ("Talk about it", "Get distracted", "Be comforted", "Be left alone")),
    ("What kind of surprise would X enjoy most?", ("Surprise date", "Unexpected gift", "Favorite food", "Spontaneous trip")),
    ("Which quality would X value most in a partner?", ("Humor", "Loyalty", "Intelligence", "Kindness")),
    ("What couple activity would X choose?", ("Cooking challenge", "Trivia", "Outdoor adventure", "Movie night")),
    ("What would X rather plan together?", ("Vacation", "Home project", "Special date", "Big celebration")),
    ("What would X most likely do with an unexpected \u20b910,000?", ("Save it", "Buy something wanted", "Spend it on fun", "Use it for a trip")),
    ("What would X choose for a late-night conversation?", ("Childhood memories", "Future plans", "Funny stories", "Deep personal topics")),
    ("What would X most want to hear after accomplishing something important?", ("I'm proud of you", "I missed you", "I believe in you", "I love you")),
    ("Which moment would X most want to relive?", ("First meeting", "First date", "Funniest memory", "Most romantic memory")),
    ("What would X choose for a spontaneous day together?", ("Road trip", "Restaurant hopping", "Movie marathon", "Explore somewhere new")),
    ("What would X rather receive on their birthday?", ("A big party", "A thoughtful gift", "A surprise trip", "A quiet celebration together")),
    ("What would X choose for a midnight snack?", ("Pizza", "Ice cream", "Chips", "Something sweet")),
    ("Which type of message would X be happiest to receive unexpectedly?", ("I miss you", "I'm thinking about you", "I'm proud of you", "Let's go somewhere")),
    ("What would X do first on a completely free day?", ("Sleep in", "Go somewhere", "Binge-watch something", "Meet people")),
    ("Which would X find harder to give up for a month?", ("Social media", "Games", "Junk food", "Movies & shows")),
    ("What would X rather have during a vacation?", ("A packed schedule", "A relaxed schedule", "A mix of both", "No plans at all")),
    ("Which would X choose for a dream date?", ("Rooftop dinner", "Amusement park", "Beach sunset", "Road trip")),
    ("What would X most likely buy impulsively?", ("Food", "Clothes", "Gadgets", "Something for someone else")),
    ("What would X prefer during a disagreement?", ("Solve it immediately", "Take a break", "Text about it", "Wait until things calm down")),
    ("Which would X choose for a cozy evening?", ("Music", "Movie", "Conversation", "Games")),
    ("What would X rather be known for?", ("Being funny", "Being kind", "Being successful", "Being dependable")),
    ("If X could instantly master one skill, what would they choose?", ("Cooking", "Music", "Sports", "A useful professional skill")),
    ("Which would X rather receive unexpectedly?", ("Flowers", "Favorite food", "A small gift", "A handwritten note")),
    ("What would X choose for a long journey?", ("Music", "Podcasts", "Movies", "Talking the entire way")),
    ("Which place would X most likely choose for a date?", ("Café", "Restaurant", "Park", "Somewhere adventurous")),
    ("What would X do if they suddenly had a completely free week?", ("Travel", "Stay home", "Visit friends or family", "Start a personal project")),
    ("What would X rather have more of?", ("Money", "Free time", "Travel opportunities", "Personal space")),
    ("Which compliment would mean the most to X?", ("You're beautiful", "You're funny", "You're smart", "You're a good person")),
    ("What would X most likely do when they can't sleep?", ("Scroll on their phone", "Watch something", "Listen to music", "Think about random things")),
    ("What would X choose for a rainy day together?", ("Movie marathon", "Cooking together", "Sleeping in", "Going out anyway")),
    ("Which would X rather receive from their partner?", ("A planned surprise", "A spontaneous call", "A thoughtful gift", "Quality time")),
    ("What would X choose if they could teleport anywhere right now?", ("Tropical beach", "European city", "Mountain destination", "Somewhere they've never heard of")),
    ("What would X most likely do after receiving good news?", ("Tell their partner first", "Tell friends", "Celebrate alone", "Start planning the next step")),
    ("Which would X rather do on a couple's anniversary?", ("Fancy dinner", "Recreate the first date", "Take a trip", "Stay home and spend the day together")),
    ("What would X choose if they had to pick one perfect date-night activity?", ("Deep conversation", "Watching something", "Going somewhere new", "Doing something competitive")),
)


def _stable_key(sequence_number: int) -> str:
    return f"couple-q-{sequence_number:02d}"


def _validate_bank() -> None:
    """Refuse a malformed bank before a row is written, rather than seeding half of it.

    The committed list is exactly the kind of artifact that gets edited by hand
    one day, and a template silently joining it without the placeholder -- or
    with three options -- would be accepted by the insert and then rejected by
    the column it is written to, or worse, stored in a shape no screen can play.
    """
    if len(COUPLE_QUESTION_BANK) != COUPLE_QUESTIONS_COUNT:
        raise RuntimeError(
            f"The Couple question bank holds {len(COUPLE_QUESTION_BANK)} entries, "
            f"expected {COUPLE_QUESTIONS_COUNT}."
        )
    seen_texts: set[str] = set()
    for index, (question_text, options) in enumerate(COUPLE_QUESTION_BANK, start=1):
        if not question_text or not question_text.strip():
            raise RuntimeError(f"Couple question {index} has no text.")
        if len(question_text) > COUPLE_QUESTION_TEXT_MAX:
            raise RuntimeError(f"Couple question {index} exceeds {COUPLE_QUESTION_TEXT_MAX} characters.")
        if not _PLACEHOLDER_PATTERN.search(question_text):
            raise RuntimeError(
                f"Couple question {index} does not name the standalone placeholder 'X'."
            )
        if len(options) != COUPLE_OPTIONS_PER_QUESTION:
            raise RuntimeError(
                f"Couple question {index} holds {len(options)} options, "
                f"expected {COUPLE_OPTIONS_PER_QUESTION}."
            )
        for option in options:
            if not option or not option.strip():
                raise RuntimeError(f"Couple question {index} holds an empty option.")
            if len(option) > COUPLE_OPTION_TEXT_MAX:
                raise RuntimeError(
                    f"Couple question {index} holds an option exceeding "
                    f"{COUPLE_OPTION_TEXT_MAX} characters."
                )
        if len(set(options)) != len(options):
            raise RuntimeError(f"Couple question {index} holds duplicate options.")
        if question_text in seen_texts:
            raise RuntimeError(f"Couple question {index} repeats an earlier question.")
        seen_texts.add(question_text)


def _today() -> date:
    return datetime.now(timezone.utc).date()


# --- the seed ------------------------------------------------------------------


def seed_couple_questions(db: Database) -> int:
    """Insert whatever of the committed bank is missing, and touch nothing else.

    Idempotent by ``stable_key``: each predefined question is looked up by its
    deterministic identifier first, so a restart inserts only what is absent --
    never a duplicate, and never an overwrite of a stored row. Returns the number
    of rows inserted, so the startup log can say what happened.
    """
    _validate_bank()

    with db.sessions() as session:
        existing = set(
            session.scalars(select(CoupleGameQuestionRecord.stable_key)).all()
        )
    missing = [
        (index, question_text, options)
        for index, (question_text, options) in enumerate(COUPLE_QUESTION_BANK, start=1)
        if _stable_key(index) not in existing
    ]
    if not missing:
        return 0
    with db.sessions.begin() as session:
        # Re-read inside the write transaction: two boots racing past the check
        # above must not both insert the same key. The unique index is the final
        # guard, and the re-read keeps the common case to one insert per key.
        present = set(session.scalars(select(CoupleGameQuestionRecord.stable_key)).all())
        inserted = 0
        for index, question_text, options in missing:
            key = _stable_key(index)
            if key in present:
                continue
            session.add(
                CoupleGameQuestionRecord(
                    stable_key=key,
                    sequence_number=index,
                    question_text=question_text,
                    option_a=options[0],
                    option_b=options[1],
                    option_c=options[2],
                    option_d=options[3],
                )
            )
            present.add(key)
            inserted += 1
    return inserted


# --- persistence ---------------------------------------------------------------


def _as_model(row: CoupleGameRecord) -> CoupleGame:
    return CoupleGame(
        id=str(row.id),
        game_date=row.game_date,
        created_by=row.created_by,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _as_question_model(row: CoupleGameQuestionRecord) -> CoupleGameQuestion:
    return CoupleGameQuestion(
        id=str(row.id),
        stable_key=row.stable_key,
        sequence_number=row.sequence_number,
        question_text=row.question_text,
        options=[row.option_a, row.option_b, row.option_c, row.option_d],
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def create_couple_game(db: Database, payload: CoupleGameCreate, created_by: str) -> CoupleGame:
    """Mark the game featured for one day, refusing a second row for the same day.

    Uniqueness is checked here *and* enforced by the schema, and both matter: the
    check gives a 409 with a message an operator can act on, while the constraint
    is what holds when two saves race past that check. The ``IntegrityError`` path
    is the same 409, so the race reads identically to the ordinary conflict rather
    than as a 500.
    """
    game_date = payload.game_date or _today()

    with db.sessions() as session:
        existing = session.scalar(
            select(CoupleGameRecord.id)
            .where(CoupleGameRecord.game_date == game_date)
            .limit(1)
        )
        if existing is not None:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"A Couple game is already configured for {game_date.isoformat()}.",
            )

    try:
        with db.sessions.begin() as session:
            row = CoupleGameRecord(game_date=game_date, created_by=created_by)
            session.add(row)
            session.flush()
            return _as_model(row)
    except IntegrityError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"A Couple game is already configured for {game_date.isoformat()}.",
        ) from exc


def list_couple_games(db: Database) -> List[CoupleGame]:
    """Every featured date, newest day first."""
    with db.sessions() as session:
        rows = session.scalars(
            select(CoupleGameRecord).order_by(CoupleGameRecord.game_date.desc())
        ).all()
        return [_as_model(row) for row in rows]


def get_couple_game(db: Database, game_id: str) -> CoupleGame:
    """One featured date by id.

    A malformed id is a 400 and a well-formed one that matches nothing is a 404,
    so a typo in a URL and a date that was never featured are told apart rather
    than both reported as "not found".
    """
    if not is_valid_id(game_id):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid Couple game id"
        )
    with db.sessions() as session:
        row = session.get(CoupleGameRecord, str(game_id))
    if row is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Couple game not found"
        )
    return _as_model(row)


def list_couple_questions(db: Database) -> List[CoupleGameQuestion]:
    """The seeded bank in play order, for whatever reads it next.

    No endpoint serves this yet -- the player surface does not exist -- so this
    is the foundation the later participation flow will read, and what tests use
    to pin the seed without going through an HTTP layer that must not exist.
    """
    with db.sessions() as session:
        rows = session.scalars(
            select(CoupleGameQuestionRecord).order_by(
                CoupleGameQuestionRecord.sequence_number.asc()
            )
        ).all()
        return [_as_question_model(row) for row in rows]
