"""Caps the application puts on a single conversation: text lengths, and message counts.

The text lengths are enforced twice on purpose: once by Pydantic, so the caller gets a 422
naming the field, and once by the database, so a write that does not come through the API
(a migration, a maintenance script, an endpoint added later) cannot store more than the
application claims to accept. Only the Pydantic half is a user-facing error; only the
database half is an invariant. Neither alone is sufficient, and a varchar column that is
narrower than the Pydantic cap is a 500 waiting to happen rather than a validation
message.

The message counts are a different kind of cap -- how much of a conversation may be written
at all, rather than how long one turn is -- and they are enforced in one place only,
``message_service``, because they are a statement about how many rows a conversation holds
and not about a column's width. They live here anyway because this module is the one place
a client-mirrored limit belongs, and because ``frontend/src/lib/limits.ts`` restates the
whole set so the browser can stop the input at the same point.

The values are product decisions rather than technical ones. They are sized to the content
each field actually holds: a confession is a short personal note, a comment is a reply to
one, and a chat message is a conversational turn.
"""

# A confession is a short personal note, not an essay. 500 characters is roughly a paragraph
# -- enough for the thing someone actually wants to say and short enough that a feed of them
# stays scannable, which is the entire reason the feed exists.
#
# Lowered from 2000, which was sized as if a confession were a diary entry. The cost of the old
# number was not storage; it was that the card rendering one confession could push everything
# below it off the screen.
#
# Longer feelings have a home already: a love note is a message to one person and is bounded
# separately at 2000. A public anonymous feed is the wrong place for a paragraph nobody chose
# to read.
CONFESSION_BODY_MAX = 500

# A comment replies to a confession, so it is deliberately much shorter.
COMMENT_BODY_MAX = 500

# Chat turns. Sized to the same order as a confession; a real message is a sentence.
MESSAGE_BODY_MAX = 100

# How many messages one conversation may ever hold, counted across both participants.
#
# This is the ceiling on the transcript itself, and it is what makes the transcript readable:
# ``GET /messages/{conversation_id}`` returns the oldest N with no cursor, so a conversation
# long enough to overflow that window would hide its own newest messages from the two people
# in it. With a hard ceiling at or below the read window, every message a conversation holds
# is a message its participants can still fetch.
#
# A conversation also has a four-hour life, so this is a bound on what two people can put in
# one sitting rather than on how much anyone may ever say to each other.
CONVERSATION_MESSAGE_MAX = 200

# How many messages one participant may send into a single conversation.
#
# The conversation ceiling alone would not stop a conversation being entirely one person
# talking: 200 messages is a monologue, and the exchange is the point. This is the number
# that makes the conversation two-sided -- neither party can take the whole allowance, so
# the thread can only be finished when both have had their say.
#
# Twice this is the conversation ceiling, deliberately: the binding limit in normal use is
# this one, and the conversation cap is the backstop that holds even if a conversation was
# filled by a build that did not enforce it.
CONVERSATION_PARTICIPANT_MESSAGE_MAX = 100

# Report reasons are reviewed by an admin, so they need to be readable but not a data dump.
# Matches the existing reports.reason column.
REPORT_REASON_MAX = 1000

# The name a confession is addressed to. Matches users.name and
# confessions.recipient_name, which are both String(160).
RECIPIENT_NAME_MAX = 160

# A reaction is one of a fixed set of keys held in a JSONB column, so the service already
# rejects anything else. This bounds what a client can make it parse and log first.
REACTION_MAX = 24

# Admin-supplied profile fields, sized to the columns they are stored in.
USER_NAME_MAX = 160
USER_BIO_MAX = 500
USER_CLASS_NAME_MAX = 80
USER_EMOJI_MAX = 16
USER_GENDER_MAX = 24
USER_ROLE_MAX = 20
USER_REGNO_MAX = 100
