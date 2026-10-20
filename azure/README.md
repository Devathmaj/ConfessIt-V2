# Magic-link email pipeline (Azure Functions)

This Function App is the asynchronous delivery path for login magic links.
The backend still mints the link and prints it to the console for local
debugging; it posts the same link to `POST /api/email` instead of building or
sending email itself.

## Functions

| Function | Trigger | Responsibility |
| --- | --- | --- |
| `enqueue_email` | HTTP `POST /api/email` | Validate `link`, `email`, `name`, `regno`, `type`; enqueue JSON; return `202`. Never sends email. |
| `process_email` | Storage Queue trigger | Validate each message; send `type: "login"` through ACS Email. |

The Python queue SDK sends ordinary UTF-8 JSON. `host.json` therefore sets
`messageEncoding: "none"` so the queue trigger reads the body directly instead
of attempting the Azure Functions default base64 decode. Keep the sender and
trigger settings in sync if either side changes.

Only `type: "login"` is implemented. `notification` and any other type are
rejected with HTTP 400 by `enqueue_email` and are dropped as invalid if they
appear in the queue.

The greeting uses the account's full `name`, not the registration number. The
email states that the link expires in 15 minutes, includes **Open Link** and
**Copy link** buttons plus the raw URL, and provides a plain-text fallback.

Email clients strip JavaScript, so an email button cannot write to the
clipboard directly. The copy button opens the frontend's `/copy-login-link`
page, which copies the exact sign-in URL on load and exposes a manual copy
button when the browser blocks automatic clipboard access.

## Configuration

The Function App reads these settings from the Functions environment (locally,
`local.settings.json`, which is ignored by git and must never be committed):

- `AzureWebJobsStorage`
- `FUNCTIONS_WORKER_RUNTIME`
- `EMAIL_QUEUE_CONNECTION_STRING`
- `EMAIL_QUEUE_NAME` (development queue: `email-requests-dev`)
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
- Logging records request/message identifiers, attempt counts and queue delay.
  It never logs the magic link, recipient address, registration number,
  connection strings, or full queue payloads.
