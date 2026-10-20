# Email delivery pipeline (Azure Functions)

This Function App is the asynchronous delivery path for login magic links and
notification emails. The backend still mints login links and prints them to the
console for local debugging; it posts either the login link or the notification
title, body and optional target link to `POST /api/email` instead of building or
sending email itself.

## Functions

| Function | Trigger | Responsibility |
| --- | --- | --- |
| `enqueue_email` | HTTP `POST /api/email` | Validate the request shape; enqueue JSON; return `202`. Never sends email. |
| `process_email` | Storage Queue trigger | Validate each message; send either `type: "login"` or `type: "notification"` through ACS Email. |

The Python queue SDK sends ordinary UTF-8 JSON. `host.json` therefore sets
`messageEncoding: "none"` so the queue trigger reads the body directly instead
of attempting the Azure Functions default base64 decode. Keep the sender and
trigger settings in sync if either side changes.

Two request types are supported:

- `login` — requires `link`, `email`, `name`, `regno`.
- `notification` — requires `title`, `body`, `email`, `name`, `regno`; accepts
  an optional absolute http(s) `link` to the page the notice is about.

Any other type, or a request missing the fields for its type, is rejected with
HTTP 400 by `enqueue_email` and is dropped as invalid if it appears in the queue.

The greeting uses the account's full `name`, not the registration number. A
login email states that the link expires in 15 minutes, includes an **Open
Link** button plus the raw URL, and provides a plain-text fallback. A
notification email uses the supplied title as its subject and heading, renders
the supplied body, and, when a `link` is present, includes an **Open in
ConfessIt** button plus the raw URL. Both types provide a plain-text fallback.

## Who receives a notification email

The backend applies every delivery gate before posting to the Function App. The
Function App only renders and sends the title, body and optional link it is
given; it has no access to the application database. Participant notices link to
the same conversation or love note their in-app notification opens. Staff
notices link to the reports review queue, the love-notes review queue, or the
specific game console that needs attention.

- A participant is emailed only when their own `notifications_enabled` column
  is true. There are no participant category preferences.
- An admin or moderator needs both `notifications_enabled` and the matching
  `staff_notification_preferences` switch: `reports_enabled` for `report`,
  `love_notes_enabled` for `love_note`, and `games_enabled` for `game`.
- A moderator is never emailed a `game` notification, even if the stored
  preference row says `games_enabled = true`. The role check runs first.
- The in-app notification feed is not filtered by these delivery switches.

A staff notice intended for both roles is stored once and fanned out to each
eligible staff account. Deduplicated notices are emailed only for the newly
inserted row, not for a retry that finds the existing row.

## Permanent failures and suppression state

The queue trigger separates failures into transient and permanent:

- Transient ACS/network failures are re-raised, so the Functions runtime retries
  the message and eventually moves it to the poison queue.
- A permanent ACS 400 is acknowledged. Retrying the same message cannot make
  it valid.
- Before calling ACS, the trigger checks the recipient domain for an MX record.
  A missing MX record (including a null MX) or a non-existent domain is treated
  as a permanent failure for that message. DNS timeouts and resolver failures
  are treated as transient and allowed through to ACS.
- When ACS identifies the recipient/address/domain as the cause, or the MX check
  fails, a suppression row is written to a persistent table. Later messages for
  that recipient are acknowledged and skipped.
- Domain-wide failures are stored only as a domain row. Domain rows are
  rechecked after `EMAIL_DOMAIN_SUPPRESSION_TTL_SECONDS` (one hour by default).
  If the domain now has MX records, a new message is allowed through; if it is
  still invalid, the row is refreshed. This lets a newly registered domain or a
  newly added MX record recover automatically without retrying the old message.
- Address-level failures are stored only for that exact normalized address and
  do not expire. A different address is checked independently; delete the row
  if the same mailbox is later created or the address is corrected.
- A non-recipient 400 (for example, malformed content) is acknowledged as
  permanent but does not suppress the recipient.

The suppression table defaults to `EmailSuppression`. It uses
`EMAIL_SUPPRESSION_CONNECTION_STRING` when set, then
`EMAIL_QUEUE_CONNECTION_STRING`, then `AzureWebJobsStorage`; the table name can
be overridden with `EMAIL_SUPPRESSION_TABLE`. If Table Storage is unavailable,
the trigger logs the failure and continues with delivery rather than making the
suppression store a new single point of failure.

A new address at a domain that is already suppressed is skipped until that
domain's recheck interval elapses. If the domain is still invalid, the new
address is skipped again; if it has recovered, the message proceeds normally.

ACS accepting a send is not proof of inbox delivery. A mailbox that does not
exist at a valid domain can only be learned from a later bounce; this pipeline
does not consume delivery reports, so those bounces cannot be added to the
suppression table.

## Configuration

The Function App reads these settings from the Functions environment (locally,
`local.settings.json`, which is ignored by git and must never be committed):

- `AzureWebJobsStorage`
- `FUNCTIONS_WORKER_RUNTIME`
- `EMAIL_QUEUE_CONNECTION_STRING`
- `EMAIL_QUEUE_NAME` (development queue: `email-requests-dev`)
- `EMAIL_SUPPRESSION_TABLE` (optional; default `EmailSuppression`)
- `EMAIL_SUPPRESSION_CONNECTION_STRING` (optional; falls back to the queue
  connection string, then `AzureWebJobsStorage`)
- `EMAIL_DOMAIN_SUPPRESSION_TTL_SECONDS` (optional; domain recheck interval,
  default `3600`)
- `ACS_EMAIL_CONNECTION_STRING`
- `ACS_SENDER_ADDRESS`

The backend needs only `EMAIL_FUNCTION_URL`
(`http://localhost:7071/api/email` in development). It never receives the
Storage or ACS connection strings.

## Local start

The app supports the Python versions accepted by Azure Functions 4.x
(3.10-3.14; the backend image uses 3.12). Use the interpreter Core Tools will
launch for the worker:

```powershell
cd azure
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
func start
```

On Windows, Core Tools may resolve its worker through the `py` launcher rather
than the active virtual environment. If that interpreter is a different one,
install the requirements with the same launcher it uses (for example,
`py -3.10 -m pip install --user -r requirements.txt`) or point the worker at the
virtual environment before `func start`.

Start the backend as usual after the Functions host is listening. The queue and
its `<queue>-poison` companion are created by the Storage extension / queue SDK
as needed.

## Delivery semantics

- `host.json` sets `batchSize: 1`, so one message is handled per invocation in
  the local single-instance host. This is not a FIFO guarantee. Azure Storage
  Queue is at-least-once; retries, visibility timeouts, competing consumers and
  scale-out can duplicate or reorder messages.
- A failed ACS send is re-raised. The Functions runtime retries it, and after
  `maxDequeueCount` (5) moves it to the poison queue.
- A permanently invalid message is logged and acknowledged so it is not retried
  forever. It is never reported as a successful delivery.
- A send can succeed while the status poll fails; the retry can then deliver a
  duplicate email. The pipeline does not claim exactly-once delivery.
- Logging records request/message identifiers, email type, attempt counts and
  queue delay. It never logs the magic link, notification title, body or target
  link, recipient address, registration number, connection strings, or full
  queue payloads.
