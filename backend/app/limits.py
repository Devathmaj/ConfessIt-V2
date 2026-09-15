"""Maximum lengths for user-supplied text.

Every limit here is enforced twice on purpose: once by Pydantic, so the caller gets a 422
naming the field, and once by the database, so a write that does not come through the API
(a migration, a maintenance script, an endpoint added later) cannot store more than the
application claims to accept. Only the Pydantic half is a user-facing error; only the
database half is an invariant. Neither alone is sufficient, and a varchar column that is
narrower than the Pydantic cap is a 500 waiting to happen rather than a validation
message.

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
MESSAGE_BODY_MAX = 2000

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
